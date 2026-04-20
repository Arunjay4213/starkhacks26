"""
Pi webcam service. Captures frames, pre-encodes JPEG, serves on :5002.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import cv2
from flask import Flask, jsonify, make_response

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "camera_service.log"

log = logging.getLogger("monday.pi.camera")
if not log.handlers:
    log.setLevel(logging.INFO)
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_fh)
    _ch = logging.StreamHandler()
    _ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_ch)

GRABBER_RETRY_SLEEP_S = 0.05
GET_FRAME_TIMEOUT_S = 2.0


# =============================================================================
# CameraGrabber
# =============================================================================


class CameraGrabber:
    """
    Opens a cv2.VideoCapture in __init__, starts a daemon thread that
    continuously reads frames, JPEG-encodes each one, and stores the bytes
    under a lock. Failure to open raises RuntimeError.
    """

    def __init__(
        self,
        device_index: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        jpeg_quality: int = 80,
    ) -> None:
        self.device_index = device_index
        self.width = width
        self.height = height
        self.fps = fps
        self.jpeg_quality = int(jpeg_quality)

        self._cap: Optional[cv2.VideoCapture] = None
        self._latest_jpeg: bytes = b""
        self._last_capture_monotonic: float = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._open_camera()
        self._start_grabber()

    def _open_camera(self) -> None:
        cap = cv2.VideoCapture(self.device_index, cv2.CAP_ANY)
        if not cap.isOpened():
            raise RuntimeError(
                f"could not open camera at index {self.device_index}. Check "
                f"that /dev/video{self.device_index} exists and the user has "
                f"read access. On Linux run `v4l2-ctl --list-devices` to see "
                f"what's wired up."
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        log.info(
            "camera opened index=%s requested=%dx%d@%d actual=%dx%d@%.1f jpeg_q=%d",
            self.device_index, self.width, self.height, self.fps,
            actual_w, actual_h, actual_fps, self.jpeg_quality,
        )
        self._cap = cap

    def _start_grabber(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._grabber_loop, daemon=True, name="pi-camera-grabber"
        )
        self._thread.start()

    def _grabber_loop(self) -> None:
        assert self._cap is not None
        enc_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        consecutive_failures = 0

        while self._running:
            ok, frame = self._cap.read()
            if not ok or frame is None:
                consecutive_failures += 1
                if consecutive_failures == 1 or consecutive_failures % 60 == 0:
                    log.warning("camera read failed x%d", consecutive_failures)
                time.sleep(GRABBER_RETRY_SLEEP_S)
                continue
            consecutive_failures = 0

            ok, buf = cv2.imencode(".jpg", frame, enc_params)
            if not ok:
                log.warning("cv2.imencode returned false, skipping frame")
                continue
            data = buf.tobytes()
            with self._lock:
                self._latest_jpeg = data
                self._last_capture_monotonic = time.monotonic()

    def get_latest_jpeg(self) -> bytes:
        """
        Return the most recent JPEG bytes. Bytes are immutable so no copy
        needed. Raises RuntimeError if no frame has been captured yet
        after GET_FRAME_TIMEOUT_S since construction.
        """
        with self._lock:
            if self._latest_jpeg:
                return self._latest_jpeg
        # No frame yet. Spin briefly in case the grabber is still warming up.
        deadline = time.monotonic() + GET_FRAME_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(0.02)
            with self._lock:
                if self._latest_jpeg:
                    return self._latest_jpeg
        raise RuntimeError(
            f"no frame captured after {GET_FRAME_TIMEOUT_S}s. Grabber is "
            f"running but camera is producing no frames."
        )

    def last_frame_age_ms(self) -> int:
        """
        Milliseconds since the grabber last wrote a frame. Returns -1 if
        no frame has ever been captured.
        """
        with self._lock:
            if self._last_capture_monotonic == 0.0:
                return -1
            return int((time.monotonic() - self._last_capture_monotonic) * 1000)

    def release(self) -> None:
        """Stop the grabber thread and release the capture. Idempotent."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        log.info("CameraGrabber released")


# =============================================================================
# Flask app
# =============================================================================


def create_app(grabber: Optional[CameraGrabber] = None) -> Flask:
    """
    Build the Flask app. `grabber` may be None: the service still starts and
    /health works, but /frame returns 503 until a grabber is attached via
    app.config["grabber"] = ...
    """
    app = Flask("monday_pi_camera_service")
    app.config["grabber"] = grabber
    app.config["frames_served"] = 0

    @app.get("/frame")
    def frame():
        g: Optional[CameraGrabber] = app.config.get("grabber")
        if g is None:
            log.warning("GET /frame -> 503 (no grabber attached)")
            return (
                jsonify({"status": "error", "reason": "camera not initialized"}),
                503,
            )
        try:
            data = g.get_latest_jpeg()
        except RuntimeError as e:
            log.warning("GET /frame -> 503 (%s)", e)
            return jsonify({"status": "error", "reason": str(e)}), 503

        age_ms = g.last_frame_age_ms()
        app.config["frames_served"] = app.config.get("frames_served", 0) + 1
        resp = make_response(data)
        resp.headers["Content-Type"] = "image/jpeg"
        resp.headers["Content-Length"] = str(len(data))
        log.info(
            "GET /frame -> 200 bytes=%d age_ms=%d served=%d",
            len(data), age_ms, app.config["frames_served"],
        )
        return resp

    @app.get("/health")
    def health():
        g: Optional[CameraGrabber] = app.config.get("grabber")
        age_ms = g.last_frame_age_ms() if g is not None else -1
        body = {
            "alive": True,
            "frames_served": app.config.get("frames_served", 0),
            "last_frame_age_ms": age_ms,
        }
        return jsonify(body), 200

    return app


# =============================================================================
# Entrypoint
# =============================================================================


def _build_grabber_from_env() -> Optional[CameraGrabber]:
    """Try to open the camera. Return None on failure after logging details."""
    try:
        return CameraGrabber(
            device_index=int(os.environ.get("MONDAY_PI_CAMERA_INDEX", "0")),
            width=int(os.environ.get("MONDAY_PI_CAMERA_WIDTH", "1280")),
            height=int(os.environ.get("MONDAY_PI_CAMERA_HEIGHT", "720")),
            fps=int(os.environ.get("MONDAY_PI_CAMERA_FPS", "30")),
            jpeg_quality=int(os.environ.get("MONDAY_PI_JPEG_QUALITY", "80")),
        )
    except RuntimeError as e:
        log.error(
            "camera init failed: %s. Flask will still start; /frame returns 503 "
            "until the camera is fixed.",
            e,
        )
        return None
    except Exception as e:
        log.exception("unexpected camera init failure: %s", e)
        return None


def _install_signal_handlers(grabber: Optional[CameraGrabber]) -> None:
    def handler(signum, frame):
        log.info("signal %s received, releasing camera", signum)
        if grabber is not None:
            grabber.release()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def main() -> int:
    port = int(os.environ.get("MONDAY_PI_CAMERA_PORT", "5002"))
    grabber = _build_grabber_from_env()
    app = create_app(grabber)
    _install_signal_handlers(grabber)

    log.info("camera_service starting on 0.0.0.0:%d (grabber=%s)",
             port, "ok" if grabber is not None else "none")
    app.run(host="0.0.0.0", port=port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
