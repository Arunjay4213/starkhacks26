"""
tests/test_pi_audio_service.py

Tests for pi.audio_service.create_app. FakeAudioCapture stands in for the
real sounddevice-backed AudioCapture.

Streaming endpoint tests invoke the WSGI app directly (via EnvironBuilder)
rather than through Flask's test client, because the test client tends to
consume streaming responses eagerly into .data. Direct WSGI iteration
gives us chunk-by-chunk control.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import pytest
from werkzeug.test import EnvironBuilder

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from pi.audio_service import create_app, CHUNK_BYTES


# -----------------------------------------------------------------------------
# Test doubles
# -----------------------------------------------------------------------------


class FakeAudioCapture:
    """
    Duck-types AudioCapture for testing. Tests can set `stream_active`,
    watch `listener_count()`, and push chunks directly into a registered
    listener's queue.
    """

    def __init__(self, stream_active: bool = True) -> None:
        self.stream_active = stream_active
        self._listeners: list["queue.Queue[bytes]"] = []
        self._lock = threading.Lock()
        # Expose the most recently registered queue for tests that want
        # to push chunks into it without monkeypatching register_listener.
        self.last_registered: Optional["queue.Queue[bytes]"] = None

    def register_listener(self) -> "queue.Queue[bytes]":
        q: "queue.Queue[bytes]" = queue.Queue(maxsize=50)
        with self._lock:
            self._listeners.append(q)
            self.last_registered = q
        return q

    def unregister_listener(self, q: "queue.Queue[bytes]") -> None:
        with self._lock:
            try:
                self._listeners.remove(q)
            except ValueError:
                pass

    def listener_count(self) -> int:
        with self._lock:
            return len(self._listeners)

    def release(self) -> None:
        with self._lock:
            self._listeners.clear()
        self.stream_active = False


# -----------------------------------------------------------------------------
# WSGI helper for streaming tests
# -----------------------------------------------------------------------------


def _wsgi_iter(app, path: str, method: str = "GET"):
    """
    Invoke app.wsgi_app directly and return (app_iter, status, headers).
    The caller iterates app_iter chunk-by-chunk and calls close() when done.
    """
    builder = EnvironBuilder(method=method, path=path)
    env = builder.get_environ()
    state: dict = {}

    def start_response(status, headers, exc_info=None):
        state["status"] = status
        state["headers"] = dict(headers)

    app_iter = app.wsgi_app(env, start_response)
    return app_iter, state


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def active_app():
    cap = FakeAudioCapture(stream_active=True)
    app = create_app(capture=cap)
    app.config["TESTING"] = True
    return app, cap


@pytest.fixture
def inactive_app():
    cap = FakeAudioCapture(stream_active=False)
    app = create_app(capture=cap)
    app.config["TESTING"] = True
    return app, cap


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_health_alive_with_stream_inactive(inactive_app):
    app, _ = inactive_app
    r = app.test_client().get("/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["alive"] is True
    assert body["stream_active"] is False


def test_health_with_stream_active(active_app):
    app, _ = active_app
    r = app.test_client().get("/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["alive"] is True
    assert body["stream_active"] is True
    assert body["samplerate"] == 16000
    assert body["channels"] == 1
    assert body["chunk_ms"] == 100
    assert body["listener_count"] == 0


def test_audio_endpoint_returns_503_when_stream_down(inactive_app):
    app, _ = inactive_app
    r = app.test_client().get("/audio")
    assert r.status_code == 503
    body = r.get_json()
    assert body["status"] == "error"
    assert "stream" in body["reason"].lower()


def test_audio_endpoint_streams_chunks(active_app):
    """
    Start a GET /audio. Registration happens inside the generator on
    first iteration, so we drive the iterator from a background thread
    and push chunks into the registered queue from the main thread.
    Three distinct 3200-byte chunks go in, three come out, in order.
    Close. Confirm the unregister path ran.
    """
    app, cap = active_app
    app_iter, state = _wsgi_iter(app, "/audio")
    assert state["status"].startswith("200"), state["status"]
    assert state["headers"]["Content-Type"] == "application/octet-stream"

    received: list[bytes] = []
    it = iter(app_iter)

    def consume() -> None:
        for chunk in it:
            received.append(chunk)
            if len(received) >= 3:
                return

    t = threading.Thread(target=consume, daemon=True, name="audio-consumer")

    try:
        t.start()

        # Registration happens when the generator body starts executing,
        # which is when `consume` calls next() the first time.
        deadline = time.monotonic() + 2.0
        while cap.last_registered is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cap.last_registered is not None, "listener never registered"
        assert cap.listener_count() == 1

        chunks = [b"\x01" * CHUNK_BYTES, b"\x02" * CHUNK_BYTES, b"\x03" * CHUNK_BYTES]
        for c in chunks:
            cap.last_registered.put(c)

        t.join(timeout=3.0)
        assert not t.is_alive(), "consumer thread hung"
        assert received == chunks
    finally:
        app_iter.close()

    # close() at the yield point triggers GeneratorExit; finally runs.
    deadline = time.monotonic() + 1.0
    while cap.listener_count() > 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cap.listener_count() == 0, "listener not unregistered after close"


def test_listener_count_increments(active_app):
    """
    /health listener_count goes 0 -> 1 while an /audio request is open
    and actively iterating, then back to 0 after close(). Proves the
    finally block actually runs.
    """
    app, cap = active_app
    client = app.test_client()

    assert client.get("/health").get_json()["listener_count"] == 0

    app_iter, state = _wsgi_iter(app, "/audio")
    assert state["status"].startswith("200")

    it = iter(app_iter)

    def consume() -> None:
        for _ in it:
            return  # first chunk is enough, leaves generator at yield

    t = threading.Thread(target=consume, daemon=True, name="count-consumer")

    try:
        t.start()
        # Wait for the generator to register its listener.
        deadline = time.monotonic() + 2.0
        while cap.last_registered is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cap.last_registered is not None

        # Unblock the consumer by pushing one chunk.
        cap.last_registered.put(b"\x00" * CHUNK_BYTES)
        t.join(timeout=2.0)

        assert client.get("/health").get_json()["listener_count"] == 1
    finally:
        app_iter.close()

    deadline = time.monotonic() + 1.0
    while cap.listener_count() > 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.get("/health").get_json()["listener_count"] == 0


def test_invalid_method_on_audio(active_app):
    app, _ = active_app
    r = app.test_client().post("/audio")
    assert r.status_code == 405


def test_health_with_no_capture():
    """
    Startup-failure path: capture is None. /health still alive, /audio 503.
    """
    app = create_app(capture=None)
    app.config["TESTING"] = True
    client = app.test_client()

    h = client.get("/health").get_json()
    assert h["alive"] is True
    assert h["stream_active"] is False
    assert h["listener_count"] == 0

    r = client.get("/audio")
    assert r.status_code == 503


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
