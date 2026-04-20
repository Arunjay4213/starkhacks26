"""
tests/test_pi_camera_service.py

Flask-level tests for pi.camera_service.create_app. A FakeGrabber stands
in for the real CameraGrabber so tests run without a camera.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from pi.camera_service import create_app


# -----------------------------------------------------------------------------
# Test double
# -----------------------------------------------------------------------------


class FakeGrabber:
    """
    Duck-types CameraGrabber. Defaults to returning a tiny fixed JPEG blob.
    Set .raise_on_get = True to simulate a grabber that can't produce
    frames, which should make /frame return 503.
    """

    FIXED_JPEG = b"\xff\xd8\xff\xe0FAKEJPEG\xff\xd9"

    def __init__(self) -> None:
        self.raise_on_get = False
        self.age_ms = 42
        self.get_calls = 0

    def get_latest_jpeg(self) -> bytes:
        self.get_calls += 1
        if self.raise_on_get:
            raise RuntimeError("no frame captured yet")
        return self.FIXED_JPEG

    def last_frame_age_ms(self) -> int:
        return self.age_ms

    def release(self) -> None:
        pass


@pytest.fixture
def client_and_grabber():
    grabber = FakeGrabber()
    app = create_app(grabber=grabber)
    app.config["TESTING"] = True
    return app.test_client(), grabber, app


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_frame_endpoint_returns_jpeg(client_and_grabber):
    client, grabber, _ = client_and_grabber
    r = client.get("/frame")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "image/jpeg"
    assert r.data == FakeGrabber.FIXED_JPEG
    assert r.headers["Content-Length"] == str(len(FakeGrabber.FIXED_JPEG))
    assert grabber.get_calls == 1


def test_frame_endpoint_returns_503_when_grabber_down(client_and_grabber):
    client, grabber, _ = client_and_grabber
    grabber.raise_on_get = True
    r = client.get("/frame")
    assert r.status_code == 503
    body = r.get_json()
    assert body["status"] == "error"
    assert "no frame" in body["reason"].lower()


def test_health_always_alive(client_and_grabber):
    client, grabber, _ = client_and_grabber
    # Even when the grabber can't produce frames, /health must still report alive.
    grabber.raise_on_get = True
    r = client.get("/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["alive"] is True
    assert "frames_served" in body
    assert "last_frame_age_ms" in body


def test_health_reports_frame_counter(client_and_grabber):
    client, _, _ = client_and_grabber

    r = client.get("/health")
    assert r.get_json()["frames_served"] == 0

    # Three successful /frame calls increment the counter.
    for _ in range(3):
        assert client.get("/frame").status_code == 200

    r = client.get("/health")
    body = r.get_json()
    assert body["frames_served"] == 3
    assert body["last_frame_age_ms"] == 42


def test_invalid_method_on_frame(client_and_grabber):
    client, _, _ = client_and_grabber
    r = client.post("/frame")
    assert r.status_code == 405


def test_health_with_no_grabber():
    """
    Startup-failure path: grabber is None (camera init raised at module
    load, Flask still started). /frame should 503, /health should still
    report alive with age -1.
    """
    app = create_app(grabber=None)
    app.config["TESTING"] = True
    client = app.test_client()

    h = client.get("/health")
    assert h.status_code == 200
    body = h.get_json()
    assert body["alive"] is True
    assert body["last_frame_age_ms"] == -1
    assert body["frames_served"] == 0

    f = client.get("/frame")
    assert f.status_code == 503
    assert f.get_json()["status"] == "error"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
