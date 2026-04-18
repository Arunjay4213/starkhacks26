#!/usr/bin/env python3
"""
Brain evaluation harness — iterate on prompts/system_prompt.txt until all cases pass.

This is NOT a unit test suite. Run it manually, read the output, fix the prompt, repeat.

Usage:
    python3 tests/test_brain.py

For each test case a camera preview window opens. Point the camera at the
described object and press SPACE to capture and send to Claude. Press Q to
skip the case, ESC to quit the whole run.

Requires:
    ANTHROPIC_API_KEY in environment or .env file
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import time
from dataclasses import dataclass
from typing import Optional

import cv2

from app.brain import plan_grasp
from app.state import Action, BrainResponse, Confidence, Finger
from app.vision import VisionCapture

# ------------------------------------------------------------------
# Test case definitions
# ------------------------------------------------------------------

@dataclass
class TestCase:
    label: str       # what to point the camera at
    transcript: str  # what the voice layer would say
    expected: str    # one of the PASS_CRITERIA keys below


TEST_CASES: list[TestCase] = [
    TestCase("a cup or mug",                   "grab the cup",    "cylindrical_high"),
    TestCase("the same cup or mug again",       "grab it",         "cylindrical_high"),
    TestCase("a pen or pencil",                "grab the pen",    "pinch_high"),
    TestCase("a key",                          "grab the key",    "lateral_high"),
    TestCase("a kitchen knife",                "grab the knife",  "refusal"),
    TestCase("empty table with no objects",    "grab it",         "refusal"),
    TestCase("a thick marker or highlighter",  "grab it",         "low_confidence"),
    TestCase("a cup or mug (off-topic test)",  "what time is it", "refusal"),
]

# ------------------------------------------------------------------
# Pass criteria
# ------------------------------------------------------------------

def _on_fingers(resp: BrainResponse) -> set[Finger]:
    return {c.finger for c in resp.commands if c.action == Action.ON}


def check_cylindrical_high(resp: Optional[BrainResponse]) -> tuple[bool, str]:
    if resp is None:
        return False, "got None"
    if resp.confidence != Confidence.HIGH:
        return False, f"confidence={resp.confidence.value}, want high"
    on = _on_fingers(resp)
    if on != {Finger.INDEX, Finger.MIDDLE, Finger.PINKY}:
        return False, f"ON fingers={[f.value for f in on]}, want all three"
    if resp.is_refusal:
        return False, f"unexpected refusal: {resp.refusal}"
    return True, "ok"


def check_pinch_high(resp: Optional[BrainResponse]) -> tuple[bool, str]:
    if resp is None:
        return False, "got None"
    if resp.confidence != Confidence.HIGH:
        return False, f"confidence={resp.confidence.value}, want high"
    on = _on_fingers(resp)
    if Finger.INDEX not in on or Finger.MIDDLE not in on:
        return False, f"INDEX or MIDDLE not ON: {[f.value for f in on]}"
    if Finger.PINKY in on:
        return False, "PINKY should not be ON for pinch"
    if resp.is_refusal:
        return False, f"unexpected refusal: {resp.refusal}"
    return True, "ok"


def check_lateral_high(resp: Optional[BrainResponse]) -> tuple[bool, str]:
    if resp is None:
        return False, "got None"
    if resp.confidence != Confidence.HIGH:
        return False, f"confidence={resp.confidence.value}, want high"
    on = _on_fingers(resp)
    if Finger.MIDDLE not in on or Finger.PINKY not in on:
        return False, f"MIDDLE or PINKY not ON: {[f.value for f in on]}"
    if Finger.INDEX in on:
        return False, "INDEX should not be ON for lateral"
    if resp.is_refusal:
        return False, f"unexpected refusal: {resp.refusal}"
    return True, "ok"


def check_refusal(resp: Optional[BrainResponse]) -> tuple[bool, str]:
    if resp is None:
        return False, "got None (API/parse failure, not a refusal)"
    if not resp.is_refusal:
        return False, f"expected refusal, got grip={resp.grip_type.value} conf={resp.confidence.value}"
    if resp.commands:
        return False, f"refusal but commands non-empty: {[c.to_dict() for c in resp.commands]}"
    return True, "ok"


def check_low_confidence(resp: Optional[BrainResponse]) -> tuple[bool, str]:
    if resp is None:
        return False, "got None"
    if resp.confidence != Confidence.LOW:
        return False, f"confidence={resp.confidence.value}, want low"
    if not resp.commands:
        return False, "expected commands for low-confidence response"
    if resp.is_refusal:
        return False, f"unexpected refusal: {resp.refusal}"
    return True, "ok"


PASS_CRITERIA = {
    "cylindrical_high": check_cylindrical_high,
    "pinch_high":       check_pinch_high,
    "lateral_high":     check_lateral_high,
    "refusal":          check_refusal,
    "low_confidence":   check_low_confidence,
}

# ------------------------------------------------------------------
# Live frame capture
# ------------------------------------------------------------------

def capture_frame(vision: VisionCapture, label: str) -> Optional[str]:
    """
    Show camera preview with instructions overlay. Returns base64 frame on
    SPACE, None on Q skip, exits process on ESC.
    """
    print(f"  Point camera at: {label}")
    print("  SPACE=capture  Q=skip  ESC=quit")

    while True:
        frame = vision.get_latest_frame()
        if frame is not None:
            display = frame.copy()
            cv2.putText(display, f"Point at: {label}", (10, 32),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 0), 2)
            cv2.putText(display, "SPACE=capture  Q=skip  ESC=quit",
                        (10, display.shape[0] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
            cv2.imshow("Sinew Test Harness", display)

        key = cv2.waitKey(30) & 0xFF
        if key == 32:  # SPACE
            return vision.encode_for_claude(vision.get_latest_frame())
        elif key == ord("q") or key == ord("Q"):
            print("  Skipped.")
            return None
        elif key == 27:  # ESC
            print("Quit by user.")
            vision.stop()
            cv2.destroyAllWindows()
            sys.exit(0)

# ------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------

def main() -> None:
    vision = VisionCapture(fallback_camera_index=0)
    vision.start()
    time.sleep(0.5)  # let camera warm up

    print("\nSinew Brain Evaluation Harness — live camera mode")
    print("=" * 65)

    results: list[tuple[bool, TestCase]] = []

    for i, case in enumerate(TEST_CASES):
        print(f"\nCase {i+1}/{len(TEST_CASES)}: '{case.transcript}' (expect {case.expected})")

        frame_b64 = capture_frame(vision, case.label)
        if frame_b64 is None:
            results.append((False, case))
            continue

        print("  Sending to Claude...")
        resp = plan_grasp(frame_b64, case.transcript)

        checker = PASS_CRITERIA[case.expected]
        passed, reason = checker(resp)
        status = "PASS" if passed else "FAIL"

        print(f"  Result: {status}")
        if not passed:
            print(f"  Reason: {reason}")
        if resp:
            print(f"  conf={resp.confidence.value}  refusal={resp.refusal is not None}"
                  f"  grip={resp.grip_type.value}")
            print(f"  ack='{resp.acknowledgement[:70]}'")
        else:
            print("  resp=None")

        results.append((passed, case))

    vision.stop()
    cv2.destroyAllWindows()

    passed_count = sum(1 for p, _ in results if p)
    total = len(results)

    print("\n" + "=" * 65)
    print(f"Result: {passed_count}/{total} passed")
    if passed_count == total:
        print("All cases passed. Prompt is ready.")
    else:
        failed = [c for p, c in results if not p]
        print("Failed cases:")
        for c in failed:
            print(f"  '{c.transcript}' pointed at '{c.label}' expected={c.expected}")
        print("\nEdit prompts/system_prompt.txt and re-run until all 8 pass consistently across 3 runs.")
    print()


if __name__ == "__main__":
    main()
