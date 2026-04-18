"""
Sinew — main entry point.

Wires all components together and runs the OpenCV overlay UI.

Start order:
    1. python tests/mock_receiver.py    (or real hardware/receiver.py on Pi)
    2. python app/main.py
    3. Say "hey sinew, grab the cup"

Hotkeys:
    SPACE   manual trigger — prompts for intent in console
    S       POST /stop and abort current execution
    ESC     abort and exit
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import cv2
import pyttsx3
import requests
import yaml
from dotenv import load_dotenv

# Add project root to path so `app` imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

from app.orchestrator import Orchestrator
from app.session_logger import RepEvent, SessionLogger
from app.state import BrainResponse, Confidence, SystemState, TriggerEvent
from app.vision import VisionCapture
from app.voice import VoiceListener

# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------

Path("app/logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("app/logs/main.log"),
    ],
)
logger = logging.getLogger("sinew.main")

# ------------------------------------------------------------------
# Overlay colors (BGR)
# ------------------------------------------------------------------

COLOR_IDLE = (0, 200, 0)          # green
COLOR_ACTIVE = (0, 200, 255)      # yellow
COLOR_ACKNOWLEDGING = (255, 180, 0)  # blue-ish
COLOR_EXECUTING = (0, 0, 220)     # red
COLOR_REFUSAL = (0, 100, 255)     # orange

STATE_COLORS = {
    SystemState.IDLE: COLOR_IDLE,
    SystemState.CAPTURING: COLOR_ACTIVE,
    SystemState.PROCESSING: COLOR_ACTIVE,
    SystemState.ACKNOWLEDGING: COLOR_ACKNOWLEDGING,
    SystemState.EXECUTING: COLOR_EXECUTING,
}

CONFIDENCE_BORDER = {
    Confidence.HIGH: (0, 200, 0),
    Confidence.MEDIUM: (0, 200, 255),
    Confidence.LOW: (0, 0, 220),
}


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def draw_overlay(
    frame,
    state: SystemState,
    last_transcript: str,
    last_response: Optional[BrainResponse],
    rep_count: int,
    refusal_text: Optional[str],
    refusal_expires: float,
) -> None:
    """Draw all HUD elements onto the frame in-place."""
    h, w = frame.shape[:2]
    now = time.time()

    # Confidence-coded border
    if last_response and not last_response.is_refusal:
        border_color = CONFIDENCE_BORDER.get(last_response.confidence, COLOR_IDLE)
        cv2.rectangle(frame, (0, 0), (w - 1, h - 1), border_color, 6)

    state_color = STATE_COLORS.get(state, COLOR_IDLE)

    # Top-left: state
    cv2.putText(frame, f"STATE: {state.value}", (10, 35),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, state_color, 2)

    # Top-right: last transcript
    transcript_display = last_transcript[-40:] if last_transcript else "--"
    cv2.putText(frame, f"HEARD: {transcript_display}", (10, 70),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (220, 220, 220), 1)

    # Bottom-left: current command / grip
    if last_response and not last_response.is_refusal and state == SystemState.EXECUTING:
        grip_text = f"GRIP: {last_response.grip_type.value}  conf={last_response.confidence.value}"
        cv2.putText(frame, grip_text, (10, h - 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, COLOR_EXECUTING, 2)
    else:
        cv2.putText(frame, "idle", (10, h - 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (150, 150, 150), 1)

    # Bottom-right: rep counter
    cv2.putText(frame, f"REPS: {rep_count}", (w - 140, h - 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (220, 220, 220), 2)

    # Center: refusal flash (3 seconds)
    if refusal_text and now < refusal_expires:
        cv2.putText(frame, "REFUSED", (w // 2 - 90, h // 2 - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, COLOR_REFUSAL, 3)
        lines = refusal_text[:60].split(". ")
        for i, line in enumerate(lines[:2]):
            cv2.putText(frame, line, (30, h // 2 + 20 + i * 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, COLOR_REFUSAL, 1)

    # Hotkey legend
    cv2.putText(frame, "SPACE=trigger  S=stop  ESC=quit", (10, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (100, 100, 100), 1)


def main() -> None:
    config = load_config()

    camera_index: int = config.get("camera_index", 0)
    receiver_url: str = config.get("receiver_url", "http://localhost:5001")
    frame_url: str = config.get("frame_url", "http://localhost:5002/frame")
    wake_phrase: str = config.get("wake_phrase", "hey sinew")
    abort_window_ms: int = config.get("abort_window_ms", 500)

    # ------------------------------------------------------------------
    # Component init
    # ------------------------------------------------------------------

    vision = VisionCapture(frame_url=frame_url)
    vision.start()

    tts = pyttsx3.init()
    tts.setProperty("rate", 165)

    session = SessionLogger()
    session_id = session.start_session()
    rep_count = 0
    trigger_times: dict[str, float] = {}

    # Shared UI state
    last_transcript = ""
    refusal_text: Optional[str] = None
    refusal_expires: float = 0.0

    def handle_trigger(transcript: str) -> None:
        nonlocal last_transcript, refusal_text, refusal_expires, rep_count
        last_transcript = transcript
        trigger_start = time.time()
        trigger_times["last"] = trigger_start

        event = TriggerEvent(transcript=transcript, timestamp=trigger_start)
        orchestrator.on_trigger(event)

        resp = orchestrator.get_last_response()
        if resp:
            if resp.is_refusal:
                refusal_text = resp.refusal
                refusal_expires = time.time() + 3.0
            else:
                rep_count += 1
                latency_ms = (time.time() - trigger_start) * 1000

                # self_initiated: True when the patient's fingers closed WITHOUT EMS assistance.
                # This is the primary recovery metric — trending upward week-over-week means
                # the motor pathway is rebuilding.
                #
                # Requires Person A's IMU data to detect movement onset before EMS fires.
                # When the IMU reports "movement detected but fingers already closing before
                # trigger", that rep should be logged as self_initiated=True.
                #
                # TODO (Person A integration): replace this with:
                #   self_initiated = imu_client.fingers_closed_before_trigger(trigger_start)
                self_initiated = False

                rep = RepEvent(
                    timestamp=trigger_start,
                    trigger_latency_ms=latency_ms,
                    ems_duration_ms=sum(c.duration_ms for c in resp.commands),
                    grip_type=resp.grip_type,
                    claude_confidence=resp.confidence,
                    target_object=resp.acknowledgement[:30],
                    patient_completed_reach=True,
                    self_initiated=self_initiated,
                )
                session.log_rep(rep)

    orchestrator = Orchestrator(
        vision=vision,
        receiver_url=receiver_url,
        tts_engine=tts,
        abort_window_ms=abort_window_ms,
    )

    voice = VoiceListener(on_trigger=handle_trigger, wake_phrase=wake_phrase)
    voice.start()

    # ------------------------------------------------------------------
    # Startup health check (GET /health from HTTP contract)
    # ------------------------------------------------------------------

    receiver_alive = orchestrator.check_receiver()
    if not receiver_alive:
        print(f"\nWARNING: receiver not reachable at {receiver_url}")
        print("Start the mock:  python tests/mock_receiver.py")
        print("Or the real receiver on the Pi.")
        print("Continuing anyway. Triggers will fail until the receiver is up.\n")
    else:
        print(f"Receiver OK at {receiver_url}")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    logger.info("Sinew running. Say '%s' to trigger.", wake_phrase)
    print(f"\nSinew ready. Say '{wake_phrase}, grab the cup' or press SPACE.\n")

    # Use local webcam as fallback if Pi frame server is unavailable
    local_cap: Optional[cv2.VideoCapture] = None
    blank = None

    try:
        while True:
            # Try to get a frame from the Pi frame server first
            frame = vision.get_latest_frame()

            if frame is None:
                # Fallback: try local webcam
                if local_cap is None:
                    local_cap = cv2.VideoCapture(camera_index)
                ret, frame = local_cap.read()
                if not ret or frame is None:
                    # Last resort: show a blank grey frame
                    if blank is None:
                        blank = cv2.rectangle(
                            __import__("numpy").zeros((480, 640, 3), dtype="uint8") + 50,
                            (0, 0), (639, 479), (80, 80, 80), 2
                        )
                        cv2.putText(blank, "Waiting for camera...", (150, 240),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 2)
                    frame = blank.copy()

            state = orchestrator.get_state()
            last_response = orchestrator.get_last_response()

            draw_overlay(
                frame,
                state=state,
                last_transcript=last_transcript,
                last_response=last_response,
                rep_count=rep_count,
                refusal_text=refusal_text,
                refusal_expires=refusal_expires,
            )

            cv2.imshow("Sinew", frame)
            key = cv2.waitKey(30) & 0xFF

            if key == 27:  # ESC
                logger.info("ESC pressed — exiting")
                orchestrator.abort("user ESC")
                break

            elif key == ord("s") or key == ord("S"):
                orchestrator.abort("user S key")

            elif key == 32:  # SPACE
                transcript = input("Intent (press enter): ").strip()
                if transcript:
                    import threading
                    threading.Thread(
                        target=handle_trigger,
                        args=(transcript,),
                        daemon=True,
                    ).start()

    finally:
        voice.stop()
        vision.stop()
        if local_cap:
            local_cap.release()
        cv2.destroyAllWindows()
        session.end_session()
        logger.info("Sinew shut down cleanly")


if __name__ == "__main__":
    main()
