"""
Orchestrator — state machine that drives the end-to-end flow.

States: IDLE -> CAPTURING -> PROCESSING -> ACKNOWLEDGING -> EXECUTING -> IDLE

on_trigger() is called by voice.py and drives one full cycle.
abort() can be called from any thread at any time and short-circuits execution.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

import requests

from app import brain as brain_module
from app.intent_detector import IntentDetector
from app.state import (
    BrainResponse,
    Confidence,
    SystemState,
    TriggerEvent,
)

logger = logging.getLogger(__name__)

LOG_PATH = "app/logs/orchestrator.log"

import os
from pathlib import Path
Path("app/logs").mkdir(parents=True, exist_ok=True)
_fh = logging.FileHandler(LOG_PATH)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)
logger.setLevel(logging.DEBUG)

DURATION_SCALE: dict[Confidence, float] = {
    Confidence.HIGH: 1.0,
    Confidence.MEDIUM: 0.75,
    Confidence.LOW: 0.5,
}

HTTP_TIMEOUT = 3.0   # seconds per /stimulate or /stop request


class _TTSWorker(threading.Thread):
    """
    Dedicated thread for pyttsx3.

    pyttsx3.runAndWait() is synchronous and blocks the calling thread for the
    full duration of the speech. Running it inline on the orchestrator/voice
    thread prevents the voice listener from detecting "stop" during playback.

    This worker keeps pyttsx3 on its own thread. say() enqueues text and clears
    the done event. wait_done() blocks the CALLER until speech finishes — so the
    abort window only opens after the patient has heard the acknowledgement.
    """

    def __init__(self, engine) -> None:
        super().__init__(daemon=True, name="tts-worker")
        self._engine = engine
        self._queue: queue.Queue[str] = queue.Queue()
        self._done = threading.Event()
        self._done.set()   # starts in done state (nothing playing)
        self.start()

    def say(self, text: str) -> None:
        self._done.clear()
        self._queue.put(text)

    def wait_done(self, timeout: float = 10.0) -> None:
        """Block until the current utterance finishes."""
        self._done.wait(timeout=timeout)

    def run(self) -> None:
        while True:
            text = self._queue.get()
            try:
                self._engine.say(text)
                self._engine.runAndWait()
            except Exception as exc:
                logger.warning("TTS worker error: %s", exc)
            finally:
                self._done.set()


class Orchestrator:
    """
    Drives the full cycle from TriggerEvent to EMS execution.

    Thread safety: on_trigger() runs on the voice thread.
    abort() may be called from the UI/main thread at any time.
    """

    def __init__(
        self,
        vision,                                    # VisionCapture
        receiver_url: str = "http://localhost:5001",
        tts_engine=None,
        abort_window_ms: int = 500,
    ) -> None:
        self.vision = vision
        self.receiver_url = receiver_url
        self.tts = tts_engine
        self.abort_window_ms = abort_window_ms

        self._state = SystemState.IDLE
        self._last_response: Optional[BrainResponse] = None
        self._state_lock = threading.Lock()
        self._abort_event = threading.Event()
        self._execution_lock = threading.Lock()   # only one cycle at a time

        self._tts_worker = _TTSWorker(tts_engine) if tts_engine is not None else None
        self._detector = IntentDetector()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_receiver(self) -> bool:
        """
        Call GET /health on the receiver. Returns True if alive, False otherwise.
        Call this once at startup before entering the main loop so the operator
        knows immediately if the hardware bridge is not running.
        """
        try:
            resp = requests.get(f"{self.receiver_url}/health", timeout=2.0)
            alive = resp.status_code == 200 and resp.json().get("alive", False)
            if alive:
                logger.info("Receiver healthy at %s", self.receiver_url)
            else:
                logger.warning("Receiver at %s returned unexpected response: %s", self.receiver_url, resp.text)
            return alive
        except requests.exceptions.RequestException as exc:
            logger.warning("Receiver not reachable at %s: %s", self.receiver_url, exc)
            return False

    def on_trigger(self, event: TriggerEvent) -> None:
        """Entry point called by voice.py. Runs the full state machine cycle."""
        if not self._execution_lock.acquire(blocking=False):
            logger.info("on_trigger ignored: already executing")
            return
        try:
            self._abort_event.clear()
            self._run_cycle(event)
        finally:
            self._execution_lock.release()
            self._set_state(SystemState.IDLE)

    def abort(self, reason: str = "user abort") -> None:
        """Cancel in-flight execution. Safe to call from any thread."""
        logger.info("abort: %s", reason)
        self._abort_event.set()
        self._post_stop()

    def get_state(self) -> SystemState:
        with self._state_lock:
            return self._state

    def get_last_response(self) -> Optional[BrainResponse]:
        with self._state_lock:
            return self._last_response

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _run_cycle(self, event: TriggerEvent) -> None:
        # CAPTURING
        self._set_state(SystemState.CAPTURING)
        frame_b64 = self.vision.encode_for_claude()
        if frame_b64 is None:
            logger.warning("No frame available. Aborting cycle.")
            self._speak("Camera not ready. Try again.")
            return

        if self._aborted():
            return

        # PROCESSING
        self._set_state(SystemState.PROCESSING)
        response = brain_module.plan_grasp(frame_b64, event.transcript)

        with self._state_lock:
            self._last_response = response

        intent = self._detector.evaluate(response, fingers_closing=False)

        if self._aborted():
            return

        # ACKNOWLEDGING
        self._set_state(SystemState.ACKNOWLEDGING)

        if response is None:
            self._speak("I did not understand that.")
            return

        self._speak(response.acknowledgement)

        if response.is_refusal or not intent.detected:
            logger.info("Not executing: %s", intent.blocked_reason or "refusal")
            return

        # Abort window — user can say "stop" or press ESC here
        aborted = self._abort_event.wait(timeout=self.abort_window_ms / 1000.0)
        if aborted or self._aborted():
            logger.info("Aborted during acknowledgement window")
            return

        # EXECUTING
        self._set_state(SystemState.EXECUTING)
        self._execute_commands(response)

    def _execute_commands(self, response: BrainResponse) -> None:
        scale = DURATION_SCALE[response.confidence]

        for cmd in response.commands:
            if self._aborted():
                logger.info("Execution interrupted by abort")
                self._post_stop()
                return

            scaled_duration = int(cmd.duration_ms * scale)
            payload = {
                "finger": cmd.finger.value,
                "action": cmd.action.value,
                "duration_ms": scaled_duration,
            }
            logger.info("POST /stimulate %s", payload)

            try:
                resp = requests.post(
                    f"{self.receiver_url}/stimulate",
                    json=payload,
                    timeout=HTTP_TIMEOUT,
                )
                logger.info("/stimulate response: %s %s", resp.status_code, resp.text)
            except requests.exceptions.RequestException as exc:
                logger.error("/stimulate failed: %s. Aborting sequence.", exc)
                self.abort(reason=f"HTTP failure: {exc}")
                return

            # Wait the duration, but remain interruptible
            aborted = self._abort_event.wait(timeout=scaled_duration / 1000.0)
            if aborted:
                self._post_stop()
                return

    def _post_stop(self) -> None:
        try:
            requests.post(f"{self.receiver_url}/stop", timeout=HTTP_TIMEOUT)
            logger.info("POST /stop sent")
        except requests.exceptions.RequestException as exc:
            logger.warning("POST /stop failed: %s", exc)

    def _speak(self, text: str) -> None:
        """
        Enqueue text for TTS and block until speech finishes.
        Blocking here is intentional: the abort window must not open until
        the patient has heard the acknowledgement.
        """
        logger.info("TTS: '%s'", text)
        if self._tts_worker is not None:
            self._tts_worker.say(text)
            self._tts_worker.wait_done(timeout=10.0)

    def _set_state(self, state: SystemState) -> None:
        with self._state_lock:
            prev = self._state
            self._state = state
        logger.info("State: %s -> %s", prev.value, state.value)

    def _aborted(self) -> bool:
        return self._abort_event.is_set()


# ------------------------------------------------------------------
# Manual test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from app.vision import VisionCapture

    class _FakeVision:
        """Serves a saved fixture image instead of polling the Pi."""
        def encode_for_claude(self) -> Optional[str]:
            path = "tests/fixtures/mug.jpg"
            try:
                return VisionCapture().load_file_as_b64(path)
            except FileNotFoundError:
                return None

    print("Starting orchestrator test against mock receiver on localhost:5001")
    print("Make sure: python tests/mock_receiver.py is running in another terminal\n")

    orch = Orchestrator(vision=_FakeVision(), receiver_url="http://localhost:5001")

    event = TriggerEvent(transcript="grab the cup")
    print(f"Firing TriggerEvent: '{event.transcript}'")
    orch.on_trigger(event)
    print(f"\nFinal state: {orch.get_state().value}")
    resp = orch.get_last_response()
    if resp:
        print(f"Last response: confidence={resp.confidence.value} grip={resp.grip_type.value}")
