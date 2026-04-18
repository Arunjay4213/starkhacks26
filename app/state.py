"""
Shared types for the Sinew project.
All modules import from here. Never define these elsewhere.
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
    CYLINDRICAL = "CYLINDRICAL"  # index + middle + pinky ON
    PINCH = "PINCH"              # index + middle ON, pinky OFF
    LATERAL = "LATERAL"          # middle + pinky ON, index OFF
    NONE = "NONE"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SystemState(str, Enum):
    IDLE = "IDLE"
    CAPTURING = "CAPTURING"
    PROCESSING = "PROCESSING"
    ACKNOWLEDGING = "ACKNOWLEDGING"
    EXECUTING = "EXECUTING"


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
