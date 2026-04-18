"""
app/state.py

Shared types for the Sinew stack. Every layer imports from here so that
enum values cannot drift between the firmware grammar, the hardware bridge,
the brain, and the vision and voice layers.

The string values of Finger and Action match the Arduino firmware's command
grammar exactly. Changing them breaks the wire protocol. Do not lowercase.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Finger(Enum):
    INDEX = "INDEX"
    MIDDLE = "MIDDLE"
    PINKY = "PINKY"


class Action(Enum):
    ON = "ON"
    OFF = "OFF"


class GripType(Enum):
    CYLINDRICAL = "cylindrical"
    PINCH = "pinch"
    LATERAL = "lateral"


class Confidence(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SystemState(Enum):
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


@dataclass
class TriggerEvent:
    transcript: str
    timestamp: float


@dataclass
class BrainResponse:
    acknowledgement: str
    confidence: Confidence
    refusal: Optional[str]
    commands: list[Command]
