"""
Confidence gate and safety check before firing EMS.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.state import BrainResponse, Confidence, GripType

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = Confidence.LOW   # LOW and above pass by default


@dataclass
class IntentEvent:
    detected: bool
    grip_type: GripType
    confidence: Confidence
    timestamp: float
    response: Optional[BrainResponse] = None
    blocked_reason: Optional[str] = None


class IntentDetector:

    def __init__(
        self,
        min_confidence: Confidence = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self.min_confidence = min_confidence
        self._confidence_rank = {
            Confidence.LOW: 0,
            Confidence.MEDIUM: 1,
            Confidence.HIGH: 2,
        }

    def evaluate(
        self,
        response: Optional[BrainResponse],
        fingers_closing: bool = False,
    ) -> IntentEvent:
        """Check if EMS execution is safe to proceed."""
        now = time.time()

        if response is None:
            return IntentEvent(
                detected=False,
                grip_type=GripType.NONE,
                confidence=Confidence.LOW,
                timestamp=now,
                blocked_reason="brain returned None",
            )

        if response.is_refusal:
            logger.info("Intent blocked: refusal='%s'", response.refusal)
            return IntentEvent(
                detected=False,
                grip_type=GripType.NONE,
                confidence=response.confidence,
                timestamp=now,
                response=response,
                blocked_reason=f"refusal: {response.refusal}",
            )

        if fingers_closing:
            logger.info("Intent blocked: patient self-initiating grip, not firing")
            return IntentEvent(
                detected=False,
                grip_type=response.grip_type,
                confidence=response.confidence,
                timestamp=now,
                response=response,
                blocked_reason="fingers_closing=True, patient self-initiating",
            )

        if not response.commands:
            return IntentEvent(
                detected=False,
                grip_type=GripType.NONE,
                confidence=response.confidence,
                timestamp=now,
                response=response,
                blocked_reason="empty command list",
            )

        if self._confidence_rank[response.confidence] < self._confidence_rank[self.min_confidence]:
            logger.info(
                "Intent blocked: confidence %s below threshold %s",
                response.confidence.value,
                self.min_confidence.value,
            )
            return IntentEvent(
                detected=False,
                grip_type=response.grip_type,
                confidence=response.confidence,
                timestamp=now,
                response=response,
                blocked_reason=f"confidence {response.confidence.value} below threshold",
            )

        logger.info(
            "Intent approved: grip=%s confidence=%s commands=%d",
            response.grip_type.value,
            response.confidence.value,
            len(response.commands),
        )
        return IntentEvent(
            detected=True,
            grip_type=response.grip_type,
            confidence=response.confidence,
            timestamp=now,
            response=response,
        )
