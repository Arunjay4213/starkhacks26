"""
tests/test_voice_http.py

Network-level tests for app.voice:
  * PiAudioSource: yields exact-size chunks, reconnects on stream end,
    stop() terminates in-flight.
  * VoiceListener.is_reachable: happy path, stream-inactive path,
    unreachable path.

Transcription and wake detection need a real audio file plus faster-whisper
to exercise meaningfully; those paths live behind is_reachable and
PiAudioSource in the listen loop and are validated on hardware day, not
here.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import pytest
from flask import Flask, Response, jsonify
from werkzeug.serving import make_server

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.voice import CHUNK_BYTES, PiAudioSource, VoiceListener


MOCK_HOST = "127.0.0.1"
MOCK_PORT = 5997
MOCK_URL = f"http://{MOCK_HOST}:{MOCK_PORT}"
CLOSED_URL = "http://127.0.0.1:1"   # reliably refused


# -----------------------------------------------------------------------------
# Threaded werkzeug server
# -----------------------------------------------------------------------------


class _ThreadedServer:
    def __init__(self, app: Flask):
        self.srv = make_server(MOCK_HOST, MOCK_PORT, app, threaded=True)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.srv.shutdown()
        self.thread.join(timeout=2.0)


# -----------------------------------------------------------------------------
# Fake audio service
# -----------------------------------------------------------------------------


def _chunk_with(byte_value: int) -> bytes:
    return bytes([byte_value]) * CHUNK_BYTES


def _build_streaming_service(
    chunks_per_session: int = 3,
    chunks_sequence: Optional[List[bytes]] = None,
    connect_counter: Optional[dict] = None,
    stream_active: bool = True,
) -> Flask:
    """
    Fake Pi audio service. Each /audio connection yields
    `chunks_per_session` chunks then ends the response. A reconnect from
    the client produces another session with a fresh chunk slice.
    `connect_counter["n"]` is incremented on every /audio request so tests
    can assert on reconnect behavior.
    """
    app = Flask("fake_audio_service")

    default_seq = [_chunk_with(0xA0 + i) for i in range(16)]
    seq = chunks_sequence if chunks_sequence is not None else default_seq

    @app.get("/audio")
    def audio():
        if connect_counter is not None:
            connect_counter["n"] = connect_counter.get("n", 0) + 1
            session_index = connect_counter["n"] - 1
        else:
            session_index = 0

        def generate():
            start = (session_index * chunks_per_session) % max(1, len(seq))
            for i in range(chunks_per_session):
                idx = (start + i) % len(seq)
                yield seq[idx]

        return Response(generate(), mimetype="application/octet-stream")

    @app.get("/health")
    def health():
        return jsonify({
            "alive": True,
            "stream_active": stream_active,
            "listener_count": 0,
            "samplerate": 16000,
            "channels": 1,
            "chunk_ms": 100,
        }), 200

    return app


@pytest.fixture
def server_ok():
    app = _build_streaming_service(chunks_per_session=3)
    server = _ThreadedServer(app)
    server.start()
    time.sleep(0.05)
    try:
        yield MOCK_URL
    finally:
        server.stop()


@pytest.fixture
def server_with_reconnect():
    """Closes the stream after each session so the client must reconnect."""
    counter: dict = {}
    # Two sessions, 2 chunks each, then test stops.
    seq = [_chunk_with(0x11), _chunk_with(0x22), _chunk_with(0x33), _chunk_with(0x44)]
    app = _build_streaming_service(
        chunks_per_session=2, chunks_sequence=seq, connect_counter=counter,
    )
    server = _ThreadedServer(app)
    server.start()
    time.sleep(0.05)
    try:
        yield MOCK_URL, counter
    finally:
        server.stop()


@pytest.fixture
def server_stream_inactive():
    app = _build_streaming_service(stream_active=False)
    server = _ThreadedServer(app)
    server.start()
    time.sleep(0.05)
    try:
        yield MOCK_URL
    finally:
        server.stop()


# -----------------------------------------------------------------------------
# PiAudioSource tests
# -----------------------------------------------------------------------------


def test_pi_audio_source_yields_chunks(server_ok):
    """
    Three chunks arrive in order. Each is exactly CHUNK_BYTES. The
    iterator keeps waiting (server would reconnect) so we stop() after
    collecting three.
    """
    source = PiAudioSource(server_ok, reconnect_delay_s=0.05)
    received: List[bytes] = []

    def consume():
        for c in source:
            received.append(c)
            if len(received) >= 3:
                source.stop()
                return

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "consumer thread hung"

    assert len(received) == 3
    assert all(len(c) == CHUNK_BYTES for c in received)
    # Exact bytes from the fake service's sequence.
    assert received[0] == _chunk_with(0xA0)
    assert received[1] == _chunk_with(0xA1)
    assert received[2] == _chunk_with(0xA2)


def test_pi_audio_source_reconnects(server_with_reconnect):
    """
    Server closes after 2 chunks per session. Client should reconnect
    automatically and keep yielding. Assert we see at least 4 chunks
    and that the server observed at least 2 connect attempts.
    """
    url, counter = server_with_reconnect
    source = PiAudioSource(url, reconnect_delay_s=0.05)
    received: List[bytes] = []

    def consume():
        for c in source:
            received.append(c)
            if len(received) >= 4:
                source.stop()
                return

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "consumer thread hung"

    assert len(received) >= 4
    assert counter.get("n", 0) >= 2, f"expected >=2 connects, got {counter}"


def test_pi_audio_source_stop_terminates(server_ok):
    """
    Start iterating in a thread. Call source.stop() from the main thread.
    The iterator terminates within 2 s.
    """
    source = PiAudioSource(server_ok, reconnect_delay_s=0.05)
    done = threading.Event()

    def consume():
        try:
            for _ in source:
                pass
        finally:
            done.set()

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    # Let it connect and pull a chunk or two.
    time.sleep(0.3)

    source.stop()
    assert done.wait(timeout=2.0), "iterator did not terminate after stop()"
    t.join(timeout=1.0)


# -----------------------------------------------------------------------------
# VoiceListener.is_reachable tests
# -----------------------------------------------------------------------------


def test_voice_is_reachable_success(server_ok):
    vl = VoiceListener(audio_url=server_ok)
    assert vl.is_reachable() is True


def test_voice_is_reachable_stream_inactive(server_stream_inactive):
    vl = VoiceListener(audio_url=server_stream_inactive)
    # /health returns alive=true but stream_active=false. Both must be
    # true for is_reachable() to return True.
    assert vl.is_reachable() is False


def test_voice_is_reachable_unreachable():
    vl = VoiceListener(audio_url=CLOSED_URL)
    assert vl.is_reachable() is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
