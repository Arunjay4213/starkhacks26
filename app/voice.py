"""
Voice input: VoiceListener (Pi audio stream + wake-phrase + whisper)
and ManualTrigger (stdin fallback). Both fire TriggerEvents to the
orchestrator.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Iterator, Optional

import numpy as np
import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.state import TriggerEvent

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Audio constants. These match what the Pi sends; changing them without
# also changing the Pi's audio_service breaks the wire format.
# -----------------------------------------------------------------------------

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2   # int16
CHUNK_MS = 100
CHUNK_BYTES = SAMPLE_RATE * CHUNK_MS // 1000 * SAMPLE_WIDTH_BYTES  # 3200

# Wake detection parameters. Transcribe every TRANSCRIBE_EVERY_CHUNKS
# chunks over a rolling window of ROLLING_WINDOW_CHUNKS chunks.
# With CHUNK_MS=100: window is 3 s, transcribe at 2 Hz.
ROLLING_WINDOW_CHUNKS = 30
TRANSCRIBE_EVERY_CHUNKS = 5
WAKE_WARMUP_CHUNKS = 10   # don't transcribe until at least 1 s buffered

# VAD / intent capture parameters. Same numbers as the old sounddevice
# implementation so existing threshold tuning carries over.
SILENCE_TAIL_S = 1.5
INTENT_MAX_S = 6.0

# Reasonable defaults; can be changed by tests via constructor args.
DEFAULT_AUDIO_URL = "http://monday-pi.local:5003"
HEALTH_TIMEOUT_S = 1.0
STREAM_CONNECT_TIMEOUT_S = 5.0
RECONNECT_DELAY_S = 1.0


TriggerCallback = Callable[[TriggerEvent], None]


def rms_int16(pcm: bytes) -> int:
    """RMS of raw int16 mono PCM."""
    if not pcm:
        return 0
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return 0
    return int(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


# =============================================================================
# PiAudioSource: network chunk iterator with reconnect
# =============================================================================


class PiAudioSource:
    """
    PCM chunk iterator from the Pi's /audio stream with auto-reconnect.
    """

    def __init__(
        self,
        audio_url: str,
        chunk_bytes: int = CHUNK_BYTES,
        reconnect_delay_s: float = RECONNECT_DELAY_S,
    ) -> None:
        self.audio_url = audio_url.rstrip("/")
        self.chunk_bytes = chunk_bytes
        self.reconnect_delay_s = reconnect_delay_s
        self._stop_flag = False
        self._response: Optional[requests.Response] = None
        self._response_lock = threading.Lock()
        self._connect_count = 0   # tests assert on this

    def __iter__(self) -> Iterator[bytes]:
        while not self._stop_flag:
            r = self._open_stream()
            if r is None:
                # Connect failed and stop was set during the delay.
                return
            try:
                while not self._stop_flag:
                    data = _read_exact(r.raw, self.chunk_bytes)
                    if len(data) < self.chunk_bytes:
                        # Short read = server closed or stream ended.
                        logger.warning(
                            "pi audio stream ended after partial chunk (%d of %d bytes), "
                            "reconnecting", len(data), self.chunk_bytes,
                        )
                        break
                    yield data
            except requests.RequestException as e:
                logger.warning("pi audio read error: %s, reconnecting", e)
            except Exception as e:  # noqa: BLE001
                # urllib3 raises its own types on closed connections. Treat
                # any unexpected read failure as a reconnect trigger.
                logger.warning("pi audio unexpected error: %s, reconnecting", e)
            finally:
                self._close_response()

            if self._stop_flag:
                return
            time.sleep(self.reconnect_delay_s)

    def _open_stream(self) -> Optional[requests.Response]:
        """Open /audio as a streaming GET. Retries until success or stop."""
        url = f"{self.audio_url}/audio"
        while not self._stop_flag:
            try:
                r = requests.get(
                    url,
                    stream=True,
                    timeout=(STREAM_CONNECT_TIMEOUT_S, None),
                )
            except requests.RequestException as e:
                logger.warning(
                    "pi audio connect failed: %s, retrying in %.1fs",
                    e, self.reconnect_delay_s,
                )
                if self._stop_flag:
                    return None
                time.sleep(self.reconnect_delay_s)
                continue

            if r.status_code != 200:
                logger.warning(
                    "pi audio /audio returned HTTP %d, retrying in %.1fs",
                    r.status_code, self.reconnect_delay_s,
                )
                r.close()
                if self._stop_flag:
                    return None
                time.sleep(self.reconnect_delay_s)
                continue

            with self._response_lock:
                self._response = r
            self._connect_count += 1
            logger.info(
                "pi audio connected to %s (connect #%d)",
                url, self._connect_count,
            )
            return r
        return None

    def _close_response(self) -> None:
        with self._response_lock:
            r = self._response
            self._response = None
        if r is not None:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        """Signal the iterator to exit. Unblocks any in-flight read."""
        self._stop_flag = True
        self._close_response()


def _read_exact(raw, n: int) -> bytes:
    """Read exactly n bytes from a file-like. Short read => returns what we got."""
    buf = bytearray()
    while len(buf) < n:
        data = raw.read(n - len(buf))
        if not data:
            break
        buf.extend(data)
    return bytes(buf)


# =============================================================================
# VoiceListener
# =============================================================================


class VoiceListener:
    """Wake-phrase detection + VAD intent capture over Pi audio stream."""

    def __init__(
        self,
        wake_phrase: Optional[str] = None,
        model_size: Optional[str] = None,
        device: Optional[str] = None,
        vad_threshold: Optional[int] = None,
        on_trigger: Optional[TriggerCallback] = None,
        audio_url: Optional[str] = None,
        input_device: Optional[Any] = None,   # legacy, ignored
    ) -> None:
        self.wake_phrase = (
            wake_phrase or os.environ.get("MONDAY_WAKE_PHRASE", "hey monday")
        ).lower().strip()
        self.model_size = model_size or os.environ.get("MONDAY_WHISPER_MODEL", "base")
        self.device = device or os.environ.get("MONDAY_WHISPER_DEVICE", "cpu")
        self.vad_threshold = (
            vad_threshold if vad_threshold is not None
            else int(os.environ.get("MONDAY_VAD_THRESHOLD", "500"))
        )
        self.audio_url = (
            audio_url or os.environ.get("MONDAY_AUDIO_URL", DEFAULT_AUDIO_URL)
        ).rstrip("/")
        self.on_trigger = on_trigger

        self._legacy_input_device = input_device

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._model = None
        self._source: Optional[PiAudioSource] = None

    # ------------------------ lifecycle ------------------------

    def start(self) -> None:
        if self._running:
            return
        self._load_model()
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="voice-listener"
        )
        self._thread.start()
        logger.info(
            "VoiceListener started wake=%r model=%s device=%s vad=%d audio_url=%s",
            self.wake_phrase, self.model_size, self.device, self.vad_threshold,
            self.audio_url,
        )

    def stop(self) -> None:
        self._running = False
        if self._source is not None:
            self._source.stop()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        logger.info("VoiceListener stopped")

    def _load_model(self) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as e:
            raise ImportError(
                "faster-whisper is required for VoiceListener. Install with "
                "`pip install faster-whisper`."
            ) from e
        compute_type = "int8" if self.device == "cpu" else "float16"
        logger.info(
            "loading faster-whisper %s on %s compute_type=%s",
            self.model_size, self.device, compute_type,
        )
        self._model = WhisperModel(
            self.model_size, device=self.device, compute_type=compute_type
        )

    # ------------------------ listen loop ------------------------

    def _run(self) -> None:
        try:
            self._source = PiAudioSource(self.audio_url)
            source_iter = iter(self._source)
            while self._running:
                detected = self._detect_wake(source_iter)
                if not self._running:
                    break
                if detected:
                    self._capture_and_fire_intent(source_iter)
        except Exception:
            logger.exception("VoiceListener crashed")
        finally:
            if self._source is not None:
                self._source.stop()

    def _detect_wake(self, source_iter: Iterator[bytes]) -> bool:
        """Read chunks until the wake phrase is spotted. Returns True on match."""
        rolling: Deque[bytes] = deque(maxlen=ROLLING_WINDOW_CHUNKS)
        chunks_since_transcribe = 0
        for chunk in source_iter:
            if not self._running:
                return False
            rolling.append(chunk)
            chunks_since_transcribe += 1
            if len(rolling) < WAKE_WARMUP_CHUNKS:
                continue
            if chunks_since_transcribe < TRANSCRIBE_EVERY_CHUNKS:
                continue
            chunks_since_transcribe = 0
            window = b"".join(rolling)
            text = self._transcribe_pcm16(window)
            if text:
                logger.debug("rolling transcript: %s", text)
                if self.wake_phrase in text.lower():
                    logger.info("wake detected in: %r", text)
                    return True
        return False

    def _capture_and_fire_intent(self, source_iter: Iterator[bytes]) -> None:
        """Accumulate chunks until VAD silence or hard cap, transcribe, fire."""
        logger.info(
            "capturing intent audio (max %.1fs, silence tail %.1fs)",
            INTENT_MAX_S, SILENCE_TAIL_S,
        )
        buf = bytearray()
        t0 = time.monotonic()
        last_voice_t = t0

        for chunk in source_iter:
            if not self._running:
                return
            now = time.monotonic()
            buf.extend(chunk)
            if rms_int16(chunk) >= self.vad_threshold:
                last_voice_t = now

            # Wall-clock silence timer so network hiccups don't mask the
            # silence cutoff. If the Pi stalls for 2 s and then sends
            # loud chunks, we should already have ended on the wall-clock
            # criterion rather than waiting for the silent chunks to arrive.
            if now - last_voice_t >= SILENCE_TAIL_S:
                logger.info("VAD silence tail reached at %.2fs", now - t0)
                break
            if now - t0 >= INTENT_MAX_S:
                logger.info("INTENT_MAX_S cap reached")
                break

        transcript = self._transcribe_pcm16(bytes(buf))
        transcript_l = transcript.lower().strip()
        if not transcript_l:
            logger.info("intent transcript empty, ignoring")
            return
        # If we heard only the wake phrase with nothing after, drop it.
        if self.wake_phrase in transcript_l and len(transcript_l) <= len(self.wake_phrase) + 3:
            logger.info("intent was just the wake phrase, ignoring")
            return
        event = TriggerEvent(transcript=transcript.strip(), timestamp=time.time())
        logger.info("intent fired: %r", event.transcript)
        if self.on_trigger is not None:
            try:
                self.on_trigger(event)
            except Exception:
                logger.exception("on_trigger callback raised")

    # ------------------------ transcription ------------------------

    def _transcribe_pcm16(self, pcm: bytes) -> str:
        """Transcribe raw int16 mono PCM bytes at SAMPLE_RATE. Returns joined text."""
        if not pcm or self._model is None:
            return ""
        try:
            audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
            segments, _info = self._model.transcribe(
                audio, language="en", beam_size=1, vad_filter=False,
            )
            return " ".join(seg.text for seg in segments).strip()
        except Exception:
            logger.exception("whisper transcribe failed")
            return ""

    # ------------------------ health probe ------------------------

    def is_reachable(self) -> bool:
        """
        True if GET {audio_url}/health returns 200 with alive=true AND
        stream_active=true. False on any failure. Never raises.
        """
        try:
            r = requests.get(f"{self.audio_url}/health", timeout=HEALTH_TIMEOUT_S)
        except requests.RequestException:
            return False
        if r.status_code != 200:
            return False
        try:
            body = r.json()
        except ValueError:
            return False
        return bool(body.get("alive") and body.get("stream_active"))


# =============================================================================
# ManualTrigger (unchanged)
# =============================================================================


class ManualTrigger:
    """
    Fallback trigger for boxes where VoiceListener does not work. Reads
    intent lines from stdin. Same implementation as the single-host era;
    no audio, no deps.
    """

    def __init__(self, on_trigger: Optional[TriggerCallback] = None) -> None:
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._on_trigger: Optional[TriggerCallback] = on_trigger

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="manual-trigger"
        )
        self._thread.start()
        logger.info("ManualTrigger started. Type an intent and press Enter.")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        print("ManualTrigger stopping, press Enter to unblock input()")
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("ManualTrigger stopped")

    def _run(self) -> None:
        while self._running:
            try:
                line = input("Intent (or 'quit' to stop): ").strip()
            except EOFError:
                self._running = False
                return
            if not self._running:
                return
            if not line or line.lower() == "quit":
                continue
            event = TriggerEvent(transcript=line, timestamp=time.time())
            if self._on_trigger is not None:
                try:
                    self._on_trigger(event)
                except Exception:
                    logger.exception("on_trigger callback raised")


# =============================================================================
# CLI
# =============================================================================


def _probe_pi_audio(audio_url: str) -> int:
    """Hit Pi /health and print a summary. Replaces old local --list-devices."""
    url = audio_url.rstrip("/")
    print(f"probing {url}/health ...")
    try:
        r = requests.get(f"{url}/health", timeout=HEALTH_TIMEOUT_S * 2)
    except requests.RequestException as e:
        print(f"FAIL: could not reach {url}/health: {e}")
        print("Is pi/audio_service.py running and on the same hotspot?")
        return 1
    if r.status_code != 200:
        print(f"FAIL: /health returned HTTP {r.status_code}: {r.text[:200]}")
        return 1
    try:
        body = r.json()
    except ValueError:
        print(f"FAIL: /health body is not JSON: {r.text[:200]}")
        return 1
    print(f"alive:          {body.get('alive')}")
    print(f"stream_active:  {body.get('stream_active')}")
    print(f"listener_count: {body.get('listener_count')}")
    print(f"samplerate:     {body.get('samplerate')}")
    print(f"channels:       {body.get('channels')}")
    print(f"chunk_ms:       {body.get('chunk_ms')}")
    if not body.get("stream_active"):
        print()
        print("Pi audio service is up but stream is not active.")
        print("Run `python -m pi.audio_service --list-devices` on the Pi to see")
        print("available input devices, then set MONDAY_PI_AUDIO_DEVICE there.")
    return 0


def _print_list_devices_help() -> int:
    print(
        "--list-devices on the laptop now probes the Pi's audio service at\n"
        f"MONDAY_AUDIO_URL (default {DEFAULT_AUDIO_URL}) instead of enumerating\n"
        "local sounddevice inputs. Since the wearable rework the laptop does\n"
        "not capture audio locally.\n"
        "\n"
        "To see physical input devices, run this on the Pi:\n"
        "    python -m pi.audio_service --list-devices\n"
        "and pin the right one with MONDAY_PI_AUDIO_DEVICE there."
    )
    return 0


def _main() -> int:
    parser = argparse.ArgumentParser(description="Monday voice listener")
    parser.add_argument(
        "--list-devices", action="store_true",
        help="Probe the Pi's audio service /health and print its state.",
    )
    parser.add_argument(
        "--list-devices-help", action="store_true",
        help="Explain what --list-devices does now.",
    )
    args = parser.parse_args()

    if args.list_devices_help:
        return _print_list_devices_help()

    audio_url = os.environ.get("MONDAY_AUDIO_URL", DEFAULT_AUDIO_URL)

    if args.list_devices:
        return _probe_pi_audio(audio_url)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    def on_trigger(event: TriggerEvent) -> None:
        print(f"\n=== TRIGGERED at {event.timestamp:.2f}: {event.transcript!r} ===\n")

    wake = os.environ.get("MONDAY_WAKE_PHRASE", "hey monday")
    print(f"Say '{wake}' then your intent. Runs for 60 seconds.")
    print(f"Audio source: {audio_url}")

    listener = VoiceListener(on_trigger=on_trigger)
    try:
        listener.start()
    except ImportError as e:
        print(f"FAIL: {e}")
        return 1
    except Exception as e:
        print(f"FAIL starting listener: {type(e).__name__}: {e}")
        return 1

    try:
        time.sleep(60)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        listener.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
