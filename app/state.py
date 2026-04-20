"""
Shared types. String values match the firmware wire protocol.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Finger(str, Enum):
    INDEX = "INDEX"
    MIDDLE = "MIDDLE"
    PINKY = "PINKY"


class Action(str, Enum):
    ON = "ON"
    OFF = "OFF"


class GripType(str, Enum):
    CYLINDRICAL = "cylindrical"  # index + middle + pinky ON
    PINCH = "pinch"              # index + middle ON, pinky OFF
    LATERAL = "lateral"          # middle + pinky ON, index OFF
    NONE = "none"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SystemState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    CAPTURING = "capturing"
    PROCESSING = "processing"
    ACKNOWLEDGING = "acknowledging"
    EXECUTING = "executing"


@dataclass
class Command:
    finger: Finger
    action: Action
    duration_ms: int

    def to_dict(self) -> dict:
        return {
            "finger": self.finger.value,
            "action": self.action.value,
            "duration_ms": self.duration_ms,
        }


@dataclass
class TriggerEvent:
    transcript: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class BrainResponse:
    acknowledgement: str
    confidence: Confidence
    refusal: Optional[str]
    commands: list[Command]

    @property
    def is_refusal(self) -> bool:
        return self.refusal is not None

    @property
    def grip_type(self) -> GripType:
        if not self.commands:
            return GripType.NONE
        on_fingers = {c.finger for c in self.commands if c.action == Action.ON}
        if on_fingers == {Finger.INDEX, Finger.MIDDLE, Finger.PINKY}:
            return GripType.CYLINDRICAL
        if on_fingers == {Finger.INDEX, Finger.MIDDLE}:
            return GripType.PINCH
        if on_fingers == {Finger.MIDDLE, Finger.PINKY}:
            return GripType.LATERAL
        return GripType.NONE
