"""
Voice listener — wake phrase detection and command transcription.

Uses faster-whisper with a two-stage approach:
  tiny model   — continuous scanning for the wake phrase (fast, cheap)
  base model   — full command transcription after wake phrase fires (accurate)

Spacebar fallback is wired from main.py via inject_transcript().
"""
from __future__ import annotations

import audioop
import logging
import threading
from typing import Callable, Optional

import numpy as np
import pyaudio
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

# Audio capture settings
CHUNK = 1024
RATE = 16000
CHANNELS = 1
FORMAT = pyaudio.paInt16

# Silence / VAD settings
SILENCE_RMS_THRESHOLD = 500      # RMS below this is considered silence
SILENCE_DURATION_S = 1.5         # consecutive seconds of silence ends recording
MIN_COMMAND_DURATION_S = 0.4     # ignore recordings shorter than this

# Wake phrase window: scan in 2-second rolling chunks
WAKE_SCAN_DURATION_S = 2.0


class VoiceListener:
    """
    Background thread that listens for the wake phrase then captures a command.

    Usage:
        listener = VoiceListener(on_trigger=my_callback)
        listener.start()
        # ... later ...
        listener.stop()
    """

    def __init__(
        self,
        on_trigger: Callable[[str], None],
        wake_phrase: str = "hey sinew",
    ) -> None:
        self.on_trigger = on_trigger
        self.wake_phrase = wake_phrase.lower().strip()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._audio: Optional[pyaudio.PyAudio] = None

        logger.info("Loading faster-whisper tiny model for wake detection...")
        self._wake_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        logger.info("Loading faster-whisper base model for command transcription...")
        self._command_model = WhisperModel("base", device="cpu", compute_type="int8")
        logger.info("faster-whisper models ready.")

    def start(self) -> None:
        self._running = True
        self._audio = pyaudio.PyAudio()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True, name="voice-listener")
        self._thread.start()
        logger.info("VoiceListener started, listening for '%s'", self.wake_phrase)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=4.0)
        if self._audio:
            self._audio.terminate()
        logger.info("VoiceListener stopped")

    def inject_transcript(self, transcript: str) -> None:
        """
        Spacebar fallback — called from main.py when the user presses SPACE
        and types a command in the console. Bypasses mic entirely.
        """
        transcript = transcript.strip()
        if transcript:
            logger.info("Manual inject: '%s'", transcript)
            self.on_trigger(transcript)

    def _listen_loop(self) -> None:
        stream = self._audio.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK,
        )
        try:
            wake_chunks = int(RATE / CHUNK * WAKE_SCAN_DURATION_S)
            while self._running:
                # Collect a short window to scan for the wake phrase
                frames = []
                for _ in range(wake_chunks):
                    if not self._running:
                        break
                    data = stream.read(CHUNK, exception_on_overflow=False)
                    frames.append(data)

                if not frames:
                    continue

                raw = b"".join(frames)
                wake_transcript = self._transcribe(raw, self._wake_model)
                logger.debug("Wake scan: '%s'", wake_transcript)

                if self.wake_phrase not in wake_transcript.lower():
                    continue

                logger.info("Wake phrase detected in: '%s'", wake_transcript)

                # Check if the command was already in the same utterance
                # e.g. "hey sinew grab the cup" all in one breath
                inline = wake_transcript.lower().replace(self.wake_phrase, "").strip()

                if inline:
                    # Full command was in the same audio window
                    logger.info("Inline command: '%s'", inline)
                    self.on_trigger(inline)
                else:
                    # Wait for the next utterance
                    command_audio = self._record_until_silence(stream)
                    if command_audio:
                        command_text = self._transcribe(command_audio, self._command_model)
                        command_text = command_text.strip()
                        if command_text:
                            logger.info("Command: '%s'", command_text)
                            self.on_trigger(command_text)
                        else:
                            logger.info("Wake phrase heard but no command followed")

        finally:
            stream.stop_stream()
            stream.close()

    def _record_until_silence(self, stream: pyaudio.Stream) -> Optional[bytes]:
        """
        Record from the open stream until SILENCE_DURATION_S of silence.
        Returns raw PCM bytes or None if nothing meaningful was captured.
        """
        frames = []
        silence_chunks = 0
        silence_limit = int(RATE / CHUNK * SILENCE_DURATION_S)
        min_chunks = int(RATE / CHUNK * MIN_COMMAND_DURATION_S)

        while self._running:
            data = stream.read(CHUNK, exception_on_overflow=False)
            frames.append(data)
            rms = audioop.rms(data, 2)

            if rms < SILENCE_RMS_THRESHOLD:
                silence_chunks += 1
            else:
                silence_chunks = 0

            if len(frames) >= min_chunks and silence_chunks >= silence_limit:
                break

        if not frames:
            return None
        return b"".join(frames)

    def _transcribe(self, audio_bytes: bytes, model: WhisperModel) -> str:
        """Convert raw 16kHz int16 PCM bytes to a transcript string."""
        try:
            audio_array = (
                np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            )
            segments, _ = model.transcribe(audio_array, language="en", beam_size=1)
            return " ".join(seg.text for seg in segments).strip()
        except Exception as exc:
            logger.warning("Transcription error: %s", exc)
            return ""
