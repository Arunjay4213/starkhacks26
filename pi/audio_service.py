"""
Pi microphone streaming service. Streams 16kHz mono PCM over HTTP on :5003.
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "audio_service.log"

log = logging.getLogger("monday.pi.audio")
if not log.handlers:
    log.setLevel(logging.INFO)
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_fh)
    _ch = logging.StreamHandler()
    _ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_ch)


# -----------------------------------------------------------------------------
# AudioCapture
# -----------------------------------------------------------------------------


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2         # int16
CHUNK_MS = 100
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000     # 1600
CHUNK_BYTES = CHUNK_SAMPLES * SAMPLE_WIDTH_BYTES   # 3200

INTERNAL_QUEUE_MAX = 100        # 10 s of chunks if fan-out stalls
LISTENER_QUEUE_MAX = 50         # 5 s per listener before we drop oldest

OVERFLOW_LOG_INTERVAL_S = 1.0
LISTENER_GET_TIMEOUT_S = 5.0   # bounds how long after client disconnect
                               # we take to run the finally block


class AudioCapture:
    """
    Owns the sounddevice stream. Runs a fan-out thread. Tracks listeners.

    The stream_active flag is set True once sounddevice.start() succeeds.
    It stays False if the open raised. Callers check before pulling.
    """

    def __init__(self, device: Optional[Any] = None) -> None:
        self.device = device
        self.samplerate = SAMPLE_RATE
        self.channels = CHANNELS
        self.chunk_ms = CHUNK_MS
        self.chunk_bytes = CHUNK_BYTES

        self._internal: "queue.Queue[bytes]" = queue.Queue(maxsize=INTERNAL_QUEUE_MAX)
        self._listeners: list["queue.Queue[bytes]"] = []
        self._listeners_lock = threading.Lock()

        self._stream: Any = None
        self._stream_active: bool = False
        self._running: bool = False
        self._fanout_thread: Optional[threading.Thread] = None
        self._last_overflow_log: float = 0.0
        self._internal_drops: int = 0

        self._start()

    # ------------------------ lifecycle ------------------------

    def _start(self) -> None:
        try:
            import sounddevice as sd  # type: ignore
        except ImportError as e:
            log.error(
                "sounddevice not installed: %s. Install with "
                "`pip install sounddevice` and `apt install libportaudio2`.",
                e,
            )
            return

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="int16",
                blocksize=CHUNK_SAMPLES,
                device=self.device,
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as e:
            log.error(
                "sounddevice stream failed: %s. "
                "Check input devices with `python -m pi.audio_service --list-devices` "
                "and verify libportaudio2 is installed.",
                e,
            )
            self._stream = None
            return

        self._stream_active = True
        self._running = True
        self._fanout_thread = threading.Thread(
            target=self._fanout_loop, daemon=True, name="audio-fanout"
        )
        self._fanout_thread.start()
        log.info(
            "audio stream started device=%r samplerate=%d channels=%d chunk_bytes=%d",
            self.device, self.samplerate, self.channels, self.chunk_bytes,
        )

    def release(self) -> None:
        """Stop the stream, drain fan-out, clear listeners. Idempotent."""
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.warning("stream stop/close raised: %s", e)
        self._stream = None
        self._stream_active = False
        if self._fanout_thread is not None:
            self._fanout_thread.join(timeout=2.0)
            self._fanout_thread = None
        with self._listeners_lock:
            self._listeners.clear()
        log.info("audio stream released")

    # ------------------------ state query ------------------------

    @property
    def stream_active(self) -> bool:
        return self._stream_active

    def listener_count(self) -> int:
        with self._listeners_lock:
            return len(self._listeners)

    # ------------------------ sounddevice callback ------------------------

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        """
        Runs on PortAudio's own thread. Must be fast and non-blocking.
        Python's GIL makes `put_nowait` on a thread-safe queue.Queue safe
        here. Anything that could block on a lock Python is holding
        (logging, lock-heavy code paths) is risky.
        """
        if status:
            now = time.monotonic()
            if now - self._last_overflow_log >= OVERFLOW_LOG_INTERVAL_S:
                log.warning("sounddevice status flag: %s", status)
                self._last_overflow_log = now
        try:
            # indata is a cffi buffer. bytes() copies into a Python bytes
            # object, which is what we want to hand off across threads.
            self._internal.put_nowait(bytes(indata))
        except queue.Full:
            # Fan-out fell behind. Drop this chunk. Counter bumped; the
            # fan-out loop logs the total at a sensible interval.
            self._internal_drops += 1

    # ------------------------ fan-out thread ------------------------

    def _fanout_loop(self) -> None:
        last_drop_report = time.monotonic()
        while self._running:
            try:
                chunk = self._internal.get(timeout=0.5)
            except queue.Empty:
                # Periodic log of dropped-at-internal-queue counter, rate limited.
                now = time.monotonic()
                if self._internal_drops and now - last_drop_report > 5.0:
                    log.warning(
                        "fan-out dropped %d chunks since last report",
                        self._internal_drops,
                    )
                    self._internal_drops = 0
                    last_drop_report = now
                continue

            # Snapshot listeners under lock so a concurrent register/unregister
            # can't surprise the loop body.
            with self._listeners_lock:
                snapshot = list(self._listeners)

            for lq in snapshot:
                try:
                    lq.put_nowait(chunk)
                except queue.Full:
                    # On listener queue full: drop oldest, push new. Race with
                    # consumer is benign: if consumer drains between our get
                    # and put, we push into empty space. Invariant holds either
                    # way (listener never grows unbounded, never blocks fan-out).
                    try:
                        lq.get_nowait()
                        lq.put_nowait(chunk)
                    except (queue.Empty, queue.Full):
                        pass

    # ------------------------ listener API ------------------------

    def register_listener(self) -> "queue.Queue[bytes]":
        lq: "queue.Queue[bytes]" = queue.Queue(maxsize=LISTENER_QUEUE_MAX)
        with self._listeners_lock:
            self._listeners.append(lq)
            count = len(self._listeners)
        log.info("listener registered, count=%d", count)
        return lq

    def unregister_listener(self, lq: "queue.Queue[bytes]") -> None:
        with self._listeners_lock:
            try:
                self._listeners.remove(lq)
            except ValueError:
                return
            count = len(self._listeners)
        log.info("listener unregistered, count=%d", count)


# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------


def create_app(capture: Optional[AudioCapture] = None) -> Flask:
    """
    Build the Flask app. Tests pass a FakeAudioCapture here; production
    passes a real AudioCapture. If None, /audio returns 503 and /health
    still reports alive.
    """
    app = Flask("monday_pi_audio_service")
    app.config["audio"] = capture

    @app.get("/health")
    def health():
        cap: Optional[AudioCapture] = app.config.get("audio")
        stream_active = bool(cap and cap.stream_active)
        listener_count = cap.listener_count() if cap is not None else 0
        return jsonify({
            "alive": True,
            "stream_active": stream_active,
            "listener_count": listener_count,
            "samplerate": SAMPLE_RATE,
            "channels": CHANNELS,
            "chunk_ms": CHUNK_MS,
        }), 200

    @app.get("/audio")
    def audio():
        cap: Optional[AudioCapture] = app.config.get("audio")
        if cap is None or not cap.stream_active:
            log.warning("GET /audio -> 503 (stream inactive)")
            return (
                jsonify({"status": "error", "reason": "audio stream not active"}),
                503,
            )

        # Registration is deferred into the generator body so the try/finally
        # around the yield loop reliably unregisters. Registering in the view
        # body leaks listeners when the client disconnects before first yield.
        # Do not move this back up.
        def generate():
            listener_q = cap.register_listener()
            try:
                while True:
                    try:
                        chunk = listener_q.get(timeout=LISTENER_GET_TIMEOUT_S)
                    except queue.Empty:
                        # Stream went idle. Exit cleanly so the client sees EOF.
                        log.info("listener idle %.1fs, ending stream",
                                 LISTENER_GET_TIMEOUT_S)
                        return
                    yield chunk
            finally:
                cap.unregister_listener(listener_q)

        return Response(generate(), mimetype="application/octet-stream")

    return app


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def _list_devices() -> int:
    try:
        import sounddevice as sd  # type: ignore
    except ImportError as e:
        print(f"sounddevice not installed: {e}")
        print("Install with `pip install sounddevice` and `apt install libportaudio2`.")
        return 1
    try:
        devices = sd.query_devices()
    except Exception as e:
        print(f"sounddevice.query_devices failed: {e}")
        return 1
    try:
        default_in, _ = sd.default.device
    except Exception:
        default_in = None
    print(f"{'idx':>4}  {'in':>3}  {'out':>3}  name")
    print("-" * 72)
    for i, dev in enumerate(devices):
        marker = " *" if default_in == i else "  "
        in_ch = dev.get("max_input_channels", 0)
        out_ch = dev.get("max_output_channels", 0)
        name = dev.get("name", "?")
        print(f"{i:>4}{marker}  {in_ch:>3}  {out_ch:>3}  {name}")
    print()
    print(f"default input index: {default_in}")
    print("Set MONDAY_PI_AUDIO_DEVICE to an index or a substring of the name.")
    return 0


def _build_capture_from_env() -> Optional[AudioCapture]:
    env_val = os.environ.get("MONDAY_PI_AUDIO_DEVICE")
    device: Optional[Any] = None
    if env_val is not None and env_val != "":
        try:
            device = int(env_val)
        except ValueError:
            device = env_val
    try:
        cap = AudioCapture(device=device)
    except Exception as e:
        log.exception("unexpected AudioCapture init failure: %s", e)
        return None
    return cap


def _install_signal_handlers(cap: Optional[AudioCapture]) -> None:
    def handler(signum, frame):
        log.info("signal %s received, releasing audio", signum)
        if cap is not None:
            cap.release()
        sys.exit(0)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Monday Pi audio service")
    parser.add_argument("--list-devices", action="store_true",
                        help="Print input devices and exit.")
    args = parser.parse_args()

    if args.list_devices:
        return _list_devices()

    port = int(os.environ.get("MONDAY_PI_AUDIO_PORT", "5003"))
    cap = _build_capture_from_env()
    app = create_app(cap)
    _install_signal_handlers(cap)

    log.info(
        "audio_service starting on 0.0.0.0:%d (stream_active=%s)",
        port, bool(cap and cap.stream_active),
    )
    app.run(host="0.0.0.0", port=port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
