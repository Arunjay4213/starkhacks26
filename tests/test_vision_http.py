"""
tests/test_vision_http.py

HTTP-client tests for app.vision.VisionCapture. A tiny Flask app stands
in for pi/camera_service. Covers the happy paths and the two failure
modes VisionCapture must surface cleanly (503 from Pi, network unreachable).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pytest
from flask import Flask, Response, jsonify
from werkzeug.serving import make_server

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.vision import VisionCapture


MOCK_HOST = "127.0.0.1"
MOCK_PORT = 5998
MOCK_URL = f"http://{MOCK_HOST}:{MOCK_PORT}"
CLOSED_URL = "http://127.0.0.1:1"   # port 1 is reserved, reliably refused


# -----------------------------------------------------------------------------
# Fake camera service
# -----------------------------------------------------------------------------


class _ThreadedServer:
    def __init__(self, app):
        self.srv = make_server(MOCK_HOST, MOCK_PORT, app)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.srv.shutdown()
        self.thread.join(timeout=2.0)


def _real_jpeg_bytes() -> bytes:
    """Read a real fixture JPEG so tests exercise the full decode path."""
    with (_REPO_ROOT / "tests" / "fixtures" / "mug.jpg").open("rb") as f:
        return f.read()


def _build_fake_service(jpeg_bytes: Optional[bytes] = None, fail_frame: bool = False,
                       health_alive: bool = True, health_status: int = 200) -> Flask:
    app = Flask("fake_camera_service")

    @app.get("/frame")
    def frame():
        if fail_frame:
            return jsonify({"status": "error", "reason": "no frame captured"}), 503
        data = jpeg_bytes if jpeg_bytes is not None else b""
        resp = Response(data, mimetype="image/jpeg")
        resp.headers["Content-Length"] = str(len(data))
        return resp

    @app.get("/health")
    def health():
        body = {"alive": health_alive, "frames_served": 0, "last_frame_age_ms": 10}
        return jsonify(body), health_status

    return app


@pytest.fixture
def jpeg_bytes():
    return _real_jpeg_bytes()


@pytest.fixture
def server_ok(jpeg_bytes):
    app = _build_fake_service(jpeg_bytes=jpeg_bytes)
    server = _ThreadedServer(app)
    server.start()
    time.sleep(0.05)
    try:
        yield MOCK_URL
    finally:
        server.stop()


@pytest.fixture
def server_503():
    app = _build_fake_service(fail_frame=True)
    server = _ThreadedServer(app)
    server.start()
    time.sleep(0.05)
    try:
        yield MOCK_URL
    finally:
        server.stop()


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_get_latest_frame_success(server_ok, jpeg_bytes):
    vc = VisionCapture(camera_url=server_ok)
    frame = vc.get_latest_frame()

    assert isinstance(frame, np.ndarray)
    assert frame.ndim == 3
    assert frame.shape[2] == 3  # BGR

    # Shape should match what cv2.imdecode produces for this exact JPEG.
    import cv2
    expected = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert frame.shape == expected.shape
    assert frame.dtype == expected.dtype


def test_get_latest_frame_b64_success(server_ok, jpeg_bytes):
    import base64
    vc = VisionCapture(camera_url=server_ok)
    b64 = vc.get_latest_frame_b64()

    # The fast path must hand back the exact bytes the Pi sent, base64
    # encoded. No decode / re-encode in the middle.
    assert b64 == base64.b64encode(jpeg_bytes).decode("ascii")


def test_get_latest_frame_503(server_503):
    vc = VisionCapture(camera_url=server_503)
    with pytest.raises(RuntimeError) as exc:
        vc.get_latest_frame()
    assert "camera service" in str(exc.value).lower()
    assert "503" in str(exc.value)


def test_get_latest_frame_network_error():
    vc = VisionCapture(camera_url=CLOSED_URL)
    with pytest.raises(RuntimeError) as exc:
        vc.get_latest_frame()
    msg = str(exc.value).lower()
    assert "unreachable" in msg
    assert "pi" in msg


def test_is_reachable(server_ok):
    # Reachable case.
    vc_up = VisionCapture(camera_url=server_ok)
    assert vc_up.is_reachable() is True

    # Closed-port case.
    vc_down = VisionCapture(camera_url=CLOSED_URL)
    assert vc_down.is_reachable() is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
