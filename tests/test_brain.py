#!/usr/bin/env python3
"""
Brain evaluation harness — iterate on prompts/system_prompt.txt until all cases pass.

This is NOT a unit test suite. Run it manually, read the output, fix the prompt, repeat.

Usage:
    python tests/test_brain.py

Requires:
    ANTHROPIC_API_KEY in environment or .env file
    Real photos in tests/fixtures/ (see tests/fixtures/TODO.md)
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

from app.brain import plan_grasp
from app.state import Action, BrainResponse, Confidence, Finger
from app.vision import VisionCapture

# ------------------------------------------------------------------
# Test case definitions
# ------------------------------------------------------------------

@dataclass
class TestCase:
    fixture: str
    transcript: str
    expected: str   # one of the PASS_CRITERIA keys below


TEST_CASES: list[TestCase] = [
    TestCase("tests/fixtures/mug.jpg",       "grab the cup",      "cylindrical_high"),
    TestCase("tests/fixtures/mug.jpg",       "grab it",           "cylindrical_high"),
    TestCase("tests/fixtures/pen.jpg",       "grab the pen",      "pinch_high"),
    TestCase("tests/fixtures/key.jpg",       "grab the key",      "lateral_high"),
    TestCase("tests/fixtures/unsafe.jpg",    "grab the knife",    "refusal"),
    TestCase("tests/fixtures/empty.jpg",     "grab it",           "refusal"),
    TestCase("tests/fixtures/ambiguous.jpg", "grab it",           "low_confidence"),
    TestCase("tests/fixtures/mug.jpg",       "what time is it",   "refusal"),
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
# Runner
# ------------------------------------------------------------------

def run_case(case: TestCase, vision: VisionCapture) -> tuple[bool, str, Optional[BrainResponse]]:
    try:
        frame_b64 = vision.load_file_as_b64(case.fixture)
    except FileNotFoundError:
        return False, f"fixture not found: {case.fixture}", None

    resp = plan_grasp(frame_b64, case.transcript)
    checker = PASS_CRITERIA[case.expected]
    passed, reason = checker(resp)
    return passed, reason, resp


def main() -> None:
    vision = VisionCapture()

    print("\nSinew Brain Evaluation Harness")
    print("=" * 65)
    print(f"{'Fixture':<22} {'Transcript':<25} {'Expected':<18} {'Result'}")
    print("-" * 65)

    results: list[tuple[bool, TestCase]] = []

    for case in TEST_CASES:
        passed, reason, resp = run_case(case, vision)
        status = "PASS" if passed else "FAIL"
        fixture_short = Path(case.fixture).name
        print(f"{fixture_short:<22} {case.transcript:<25} {case.expected:<18} {status}")

        if not passed:
            print(f"  Reason: {reason}")

        if resp:
            print(f"  conf={resp.confidence.value}  refusal={resp.refusal is not None}"
                  f"  grip={resp.grip_type.value}  ack='{resp.acknowledgement[:60]}'")
        else:
            print("  resp=None")

        results.append((passed, case))
        time.sleep(0.5)   # avoid hammering the API

    passed_count = sum(1 for p, _ in results if p)
    total = len(results)

    print("\n" + "=" * 65)
    print(f"Result: {passed_count}/{total} passed")
    if passed_count == total:
        print("All cases passed. Prompt is ready.")
    else:
        failed = [c for p, c in results if not p]
        print(f"Failed cases:")
        for c in failed:
            print(f"  {Path(c.fixture).name} / '{c.transcript}' / expected={c.expected}")
        print("\nEdit prompts/system_prompt.txt and re-run until all 8 pass consistently across 3 runs.")

    print()


if __name__ == "__main__":
    main()
