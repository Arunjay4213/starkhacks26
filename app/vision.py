"""
Vision capture — pulls JPEG frames from Person A's frame server.

Runs a background polling thread so the latest frame is always ready.
The main thread never blocks waiting for a frame.
"""
from __future__ import annotations

import base64
import logging
import threading
import time
from typing import Optional

import cv2
import numpy as np
import requests

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.05   # 20 fps
FETCH_TIMEOUT = 2.0    # seconds per HTTP request


class VisionCapture:
    """
    Polls GET /frame from the Pi frame server.
    Falls back to a local webcam when the Pi is unreachable, so
    encode_for_claude() always returns a frame during development.
    Call start() to begin background polling, stop() to shut down.
    """

    def __init__(
        self,
        frame_url: str = "http://localhost:5002/frame",
        fallback_camera_index: Optional[int] = 0,
    ):
        self.frame_url = frame_url
        self.fallback_camera_index = fallback_camera_index
        self._latest_frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._local_cap: Optional[cv2.VideoCapture] = None
        self._pi_reachable = True    # tracked so we only log transitions, not every poll

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="vision-poller")
        self._thread.start()
        logger.info("VisionCapture started, polling %s (fallback camera=%s)",
                    self.frame_url, self.fallback_camera_index)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._local_cap is not None:
            self._local_cap.release()
            self._local_cap = None
        logger.info("VisionCapture stopped")

    def _poll_loop(self) -> None:
        while self._running:
            frame = self._fetch_from_pi()

            if frame is None:
                # Pi unreachable — fall back to local webcam so encode_for_claude()
                # keeps working during development without the Pi connected.
                if self._pi_reachable:
                    logger.warning("Pi frame server unreachable, switching to local webcam fallback")
                    self._pi_reachable = False
                if self.fallback_camera_index is not None:
                    frame = self._fetch_from_local()
            else:
                if not self._pi_reachable:
                    logger.info("Pi frame server reconnected")
                    self._pi_reachable = True

            if frame is not None:
                with self._lock:
                    self._latest_frame = frame

            time.sleep(POLL_INTERVAL)

    def _fetch_from_pi(self) -> Optional[np.ndarray]:
        try:
            resp = requests.get(self.frame_url, timeout=FETCH_TIMEOUT)
            if resp.status_code == 200:
                buf = np.frombuffer(resp.content, dtype=np.uint8)
                return cv2.imdecode(buf, cv2.IMREAD_COLOR)
            logger.warning("Pi frame server returned HTTP %s", resp.status_code)
        except requests.exceptions.ConnectionError:
            pass   # expected when Pi is not yet connected
        except Exception as exc:
            logger.warning("Pi frame fetch error: %s", exc)
        return None

    def _fetch_from_local(self) -> Optional[np.ndarray]:
        if self._local_cap is None:
            self._local_cap = cv2.VideoCapture(self.fallback_camera_index)
        ret, frame = self._local_cap.read()
        return frame if ret else None

    def get_latest_frame(self) -> Optional[np.ndarray]:
        """Return the most recent frame as a numpy (H, W, 3) BGR array, or None."""
        with self._lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def encode_for_claude(self, frame: Optional[np.ndarray] = None) -> Optional[str]:
        """
        Return a base64-encoded JPEG string suitable for Claude's vision API.
        Uses the latest polled frame if none is provided.
        Returns None if no frame is available.
        """
        if frame is None:
            frame = self.get_latest_frame()
        if frame is None:
            return None
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return base64.b64encode(buf.tobytes()).decode("utf-8")

    def load_file_as_b64(self, path: str) -> str:
        """Load a JPEG file from disk and return base64-encoded string. Used in tests."""
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
