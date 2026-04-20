"""
HTTP client for the Pi camera service. Fetches frames over the hotspot
network and base64-encodes them for the brain layer.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import cv2
import numpy as np
import requests

logger = logging.getLogger(__name__)

DEFAULT_CAMERA_URL = "http://monday-pi.local:5002"
FRAME_TIMEOUT_S = 2.0
HEALTH_TIMEOUT_S = 1.0
JPEG_QUALITY_FOR_CLAUDE = 80


class VisionCapture:
    """HTTP client for pi/camera_service."""

    def __init__(
        self,
        camera_index: Optional[int] = None,   # legacy, ignored
        width: Optional[int] = None,          # legacy, ignored
        height: Optional[int] = None,         # legacy, ignored
        fps: int = 30,                        # legacy, ignored
        camera_url: Optional[str] = None,
    ) -> None:
        self.camera_url = (camera_url or os.environ.get("MONDAY_CAMERA_URL", DEFAULT_CAMERA_URL)).rstrip("/")
        logger.info("VisionCapture HTTP client, url=%s", self.camera_url)

    # ---------------------------------------------------------------------
    # Frame fetch
    # ---------------------------------------------------------------------

    def _fetch_jpeg_bytes(self) -> bytes:
        """GET /frame, return raw JPEG bytes."""
        url = f"{self.camera_url}/frame"
        try:
            r = requests.get(url, timeout=FRAME_TIMEOUT_S, stream=False)
        except requests.RequestException as e:
            raise RuntimeError(
                f"camera service unreachable at {self.camera_url}: {e}. "
                f"Check the Pi is powered, on the same hotspot, and that "
                f"pi/camera_service.py is running."
            ) from e

        if r.status_code == 503:
            # Service up but camera down on the Pi side.
            try:
                reason = r.json().get("reason", r.text[:200])
            except ValueError:
                reason = r.text[:200]
            raise RuntimeError(
                f"camera service at {self.camera_url} returned 503: {reason}. "
                f"Check GET /health on the Pi for last_frame_age_ms."
            )
        if r.status_code != 200:
            raise RuntimeError(
                f"camera service returned HTTP {r.status_code}: {r.text[:200]}"
            )
        return r.content

    def get_latest_frame(self) -> np.ndarray:
        """Fetch and decode the latest frame as BGR ndarray."""
        data = self._fetch_jpeg_bytes()
        buf = np.frombuffer(data, dtype=np.uint8)
        frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError(
                "cv2.imdecode returned None. Pi sent something that isn't "
                "a valid JPEG. Check pi/camera_service.py logs."
            )
        return frame

    def get_latest_frame_b64(self) -> str:
        """JPEG bytes from Pi, base64 encoded. No decode roundtrip."""
        data = self._fetch_jpeg_bytes()
        return base64.b64encode(data).decode("ascii")

    def encode_for_claude(self, frame: np.ndarray) -> str:
        """Re-encode a BGR frame as base64 JPEG."""
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY_FOR_CLAUDE])
        if not ok:
            raise RuntimeError("cv2.imencode failed on frame")
        return base64.b64encode(buf.tobytes()).decode("ascii")

    # ---------------------------------------------------------------------
    # Health probe
    # ---------------------------------------------------------------------

    def is_reachable(self) -> bool:
        """Quick /health check."""
        try:
            r = requests.get(f"{self.camera_url}/health", timeout=HEALTH_TIMEOUT_S)
        except requests.RequestException:
            return False
        if r.status_code != 200:
            return False
        try:
            return bool(r.json().get("alive"))
        except ValueError:
            return False

    # ---------------------------------------------------------------------
    # Lifecycle (no-op, kept for API compat)
    # ---------------------------------------------------------------------

    def release(self) -> None:
        """No-op. Nothing local to release. Kept for API compatibility."""
        logger.info("VisionCapture.release() no-op (HTTP client)")

    # ---------------------------------------------------------------------
    # Static helper (brain eval harness)
    # ---------------------------------------------------------------------

    @staticmethod
    def load_file_as_b64(path: str) -> str:
        """Read a JPEG file and return its raw base64 encoding. No camera needed."""
        with Path(path).open("rb") as f:
            return base64.b64encode(f.read()).decode("ascii")


# =============================================================================
# Operator smoke test
# =============================================================================

def _main() -> int:
    """Probe camera service and save three test frames."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    url = os.environ.get("MONDAY_CAMERA_URL", DEFAULT_CAMERA_URL)
    if "MONDAY_CAMERA_URL" not in os.environ:
        print(f"(no MONDAY_CAMERA_URL set, using default {url})")
    print(f"probing camera service at {url}...")

    vc = VisionCapture()
    if not vc.is_reachable():
        print(f"FAIL: {url}/health did not respond. Is pi/camera_service.py running?")
        return 1
    print("/health OK")

    for i in (1, 2, 3):
        try:
            frame = vc.get_latest_frame()
        except RuntimeError as e:
            print(f"FAIL frame {i}: {e}")
            return 1
        out_path = f"/tmp/monday_test_frame_{i}.jpg"
        cv2.imwrite(out_path, frame)
        if i == 1:
            b64 = vc.get_latest_frame_b64()
            print(f"frame {i}  shape={frame.shape}  saved={out_path}")
            print(f"base64 prefix: {b64[:50]}...")
        else:
            print(f"frame {i}  shape={frame.shape}  saved={out_path}")
        if i < 3:
            time.sleep(0.5)

    print()
    print("open the three saved JPEGs and confirm they differ. Identical")
    print("files mean the Pi grabber is not advancing, not a laptop-side issue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
