"""
State machine: trigger -> capture -> brain -> ack -> execute.

IDLE -> CAPTURING -> PROCESSING -> ACKNOWLEDGING -> EXECUTING -> IDLE
abort() at any time POSTs /stop and returns to IDLE.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Deque, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import requests

from app.brain import plan_grasp as default_plan_grasp
from app.state import (
    BrainResponse,
    Command,
    Confidence,
    SystemState,
    TriggerEvent,
)
from app.vision import VisionCapture

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

LOG_DIR = _REPO_ROOT / "app" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "orchestrator.log"

log = logging.getLogger("monday.orchestrator")
if not log.handlers:
    log.setLevel(logging.INFO)
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(_fh)
    _ch = logging.StreamHandler()
    _ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_ch)


# -----------------------------------------------------------------------------
# Types
# -----------------------------------------------------------------------------

PlanFn = Callable[[str, str], Optional[BrainResponse]]

DEFAULT_CONFIDENCE_SCALE: dict = {
    Confidence.HIGH: 1.0,
    Confidence.MEDIUM: 0.75,
    Confidence.LOW: 0.5,
}

STIMULATE_TIMEOUT_S = 2.0
STOP_TIMEOUT_S = 1.0
ABORT_POLL_INTERVAL_S = 0.05
RECENT_COMMANDS_MAX = 16  # ring buffer size for get_recent_commands


# -----------------------------------------------------------------------------
# Orchestrator
# -----------------------------------------------------------------------------


class Orchestrator:
    """
    Monday's runtime glue. Construct once per process, reuse across triggers.

    Public methods are thread-safe. Worker thread is daemonized.
    """

    def __init__(
        self,
        vision: VisionCapture,
        receiver_url: str = "http://127.0.0.1:5001",
        tts_engine: Any = None,
        abort_window_ms: int = 500,
        confidence_scale: Optional[dict] = None,
        planner: Optional[PlanFn] = None,
    ) -> None:
        self._vision = vision
        self._receiver_url = receiver_url.rstrip("/")
        self._tts_engine = tts_engine
        self._abort_window_ms = int(abort_window_ms)
        self._confidence_scale = (
            confidence_scale if confidence_scale is not None else DEFAULT_CONFIDENCE_SCALE
        )
        self._planner: PlanFn = planner if planner is not None else default_plan_grasp

        self._lock = threading.Lock()
        self._state: SystemState = SystemState.IDLE
        self._abort_event = threading.Event()

        self._last_response: Optional[BrainResponse] = None
        self._last_trigger: Optional[TriggerEvent] = None
        self._current_command_index: int = 0

        self._recent_commands: Deque[Command] = deque(maxlen=RECENT_COMMANDS_MAX)
        self._recent_lock = threading.Lock()

        self._worker: Optional[threading.Thread] = None

    # ------------------------ public API ------------------------

    def on_trigger(self, event: TriggerEvent) -> None:
        """Fire a trigger. Drops if not IDLE."""
        with self._lock:
            if self._state is not SystemState.IDLE:
                log.info("trigger ignored, state=%s transcript=%r",
                         self._state.value, event.transcript)
                return

        self._last_trigger = event
        self._last_response = None
        self._current_command_index = 0
        self._abort_event.clear()

        self._worker = threading.Thread(
            target=self._handle_trigger, args=(event,),
            daemon=True, name="orchestrator-worker",
        )
        self._worker.start()

    def abort(self, reason: str) -> None:
        """Set abort event and POST /stop to the bridge."""
        with self._lock:
            if self._state is SystemState.IDLE and not self._abort_event.is_set():
                log.info("abort called in IDLE, reason=%s (no-op)", reason)
                return

        self._abort_event.set()
        log.warning("ABORT reason=%s", reason)

        try:
            r = requests.post(f"{self._receiver_url}/stop", json={}, timeout=STOP_TIMEOUT_S)
            log.info("/stop returned %s %s", r.status_code, _safe_body(r))
        except requests.RequestException as e:
            log.error("/stop POST failed: %s (firmware watchdog is the backstop)", e)

        self._transition(SystemState.IDLE, via="abort")

    def get_state(self) -> SystemState:
        with self._lock:
            return self._state

    def get_last_response(self) -> Optional[BrainResponse]:
        return self._last_response

    def get_last_trigger(self) -> Optional[TriggerEvent]:
        return self._last_trigger

    def get_current_command_index(self) -> int:
        return self._current_command_index

    def get_recent_commands(self, n: int = 3) -> List[Command]:
        """
        Return the last n commands that successfully POSTed to the receiver,
        oldest first. Aborted and failed POSTs are not included.
        """
        with self._recent_lock:
            items = list(self._recent_commands)
        return items[-n:]

    def wait_for_idle(self, timeout_s: float = 10.0) -> bool:
        """Test helper: spin until state returns to IDLE or timeout. Returns True on success."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if self._state is SystemState.IDLE:
                    return True
            time.sleep(0.02)
        return False

    # ------------------------ state machine core ------------------------

    def _transition(self, new_state: SystemState, via: str = "") -> None:
        """Take the lock, log, update state. Does not check preconditions."""
        with self._lock:
            old = self._state
            if old is new_state:
                return
            self._state = new_state
        log.info("state %s -> %s%s", old.value, new_state.value,
                 f" via {via}" if via else "")

    def _handle_trigger(self, event: TriggerEvent) -> None:
        """Worker thread body. Full state walk for one trigger."""
        try:
            # ------------------------ CAPTURING ------------------------
            self._transition(SystemState.CAPTURING)
            try:
                # Fast path: Pi is already caching pre-encoded JPEG. Fetch
                # those bytes and base64 them directly. No decode / re-encode
                # roundtrip for the nominal trigger.
                frame_b64 = self._vision.get_latest_frame_b64()
            except Exception as e:
                log.error("capture failed: %s", e)
                self._speak("camera not available")
                self._transition(SystemState.IDLE, via="capture error")
                return

            if self._abort_event.is_set():
                self._transition(SystemState.IDLE, via="abort after capture")
                return

            # ------------------------ PROCESSING ------------------------
            self._transition(SystemState.PROCESSING)
            response = self._planner(frame_b64, event.transcript)
            self._last_response = response

            if response is None:
                log.warning("brain returned None for transcript=%r", event.transcript)
                self._speak("I didn't understand that")
                self._transition(SystemState.IDLE, via="brain None")
                return

            if self._abort_event.is_set():
                self._transition(SystemState.IDLE, via="abort after brain")
                return

            # ------------------------ ACKNOWLEDGING ------------------------
            self._transition(SystemState.ACKNOWLEDGING)
            ack_text = response.acknowledgement or response.refusal or ""
            self._speak(ack_text)

            if response.is_refusal:
                log.info("REFUSAL refusal=%r ack=%r",
                         response.refusal, response.acknowledgement)
                self._transition(SystemState.IDLE, via="refusal")
                return

            if not response.commands:
                log.info("no commands and no refusal, nothing to execute")
                self._transition(SystemState.IDLE, via="empty commands")
                return

            aborted = self._wait_abort_window()
            if aborted:
                self._transition(SystemState.IDLE, via="abort during window")
                return

            # ------------------------ EXECUTING ------------------------
            self._transition(SystemState.EXECUTING)
            scale = self._confidence_scale.get(response.confidence, 1.0)
            log.info("executing %d command(s) confidence=%s scale=%.2f",
                     len(response.commands), response.confidence.value, scale)

            for i, cmd in enumerate(response.commands):
                if self._abort_event.is_set():
                    log.info("abort observed before command %d", i)
                    break
                self._current_command_index = i
                scaled_duration = int(cmd.duration_ms * scale)
                payload = {
                    "finger": cmd.finger.value,
                    "action": cmd.action.value,
                    "duration_ms": scaled_duration,
                }
                log.info("POST /stimulate [%d/%d] %s (raw %d ms scaled to %d)",
                         i + 1, len(response.commands), payload,
                         cmd.duration_ms, scaled_duration)
                try:
                    r = requests.post(
                        f"{self._receiver_url}/stimulate",
                        json=payload, timeout=STIMULATE_TIMEOUT_S,
                    )
                    log.info("stimulate resp %s %s", r.status_code, _safe_body(r))
                    if r.status_code != 200:
                        log.error("stimulate returned non-200, aborting")
                        self.abort("receiver non-200")
                        return
                except requests.RequestException as e:
                    log.error("stimulate POST failed: %s", e)
                    self.abort("receiver error")
                    return
                with self._recent_lock:
                    self._recent_commands.append(cmd)

            self._transition(SystemState.IDLE, via="execution complete")

        except Exception:
            log.exception("orchestrator worker crashed, forcing IDLE")
            try:
                self.abort("worker exception")
            except Exception:
                log.exception("abort during crash cleanup also failed")
            self._transition(SystemState.IDLE, via="crash")

    def _wait_abort_window(self) -> bool:
        """
        Sleep for abort_window_ms in ABORT_POLL_INTERVAL_S slices, returning
        True if the abort event fires. Gives the human a chance to veto.
        """
        if self._abort_window_ms <= 0:
            return self._abort_event.is_set()
        deadline = time.monotonic() + self._abort_window_ms / 1000.0
        while time.monotonic() < deadline:
            if self._abort_event.is_set():
                return True
            remaining = deadline - time.monotonic()
            time.sleep(min(ABORT_POLL_INTERVAL_S, max(0.0, remaining)))
        return self._abort_event.is_set()

    # ------------------------ TTS ------------------------

    def _speak(self, text: str) -> None:
        if not text:
            return
        log.info("SPEAK %r", text)
        if self._tts_engine is None:
            print(f"[TTS] {text}")
            return
        threading.Thread(
            target=self._speak_blocking, args=(text,),
            daemon=True, name="orchestrator-tts",
        ).start()

    def _speak_blocking(self, text: str) -> None:
        try:
            self._tts_engine.say(text)
            self._tts_engine.runAndWait()
        except Exception:
            log.exception("TTS engine raised")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _safe_body(r: "requests.Response") -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:200]
