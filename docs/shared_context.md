# Shared context

Canonical project context for future prompts. If a prompt asks to "update the shared context", edit this file. The block below can be pasted verbatim into new prompts.

---

PROJECT: Monday. Vision-guided assistive grasping prototype for upper motor neuron injury research. Voice-triggered, Claude-planned, EMS-actuated three-finger grip system.

ARCHITECTURE: Split across two machines connected over a phone hotspot network.

  Wearable case (on operator's arm): Raspberry Pi 5 + Arduino Micro (USB to Pi) + Belifu EMS unit + 3-channel relay board + battery + head-mounted webcam with built-in mic (USB to Pi).

  Laptop (on bench): runs the app stack. Vision and voice are HTTP/network clients that fetch from Pi services. Orchestrator, brain (Claude API), overlay, TTS all on laptop. Laptop uses the same phone hotspot for internet (Claude API calls).

  Pi runs three Flask services, all bound to 0.0.0.0:
    hardware/receiver.py (port 5001): Arduino serial bridge.
    pi/camera_service.py (port 5002): webcam capture, GET /frame returns JPEG.
    pi/audio_service.py (port 5003): microphone audio streaming.

  Laptop runs:
    app/vision.py: HTTP client for camera_service.
    app/voice.py: network audio consumer, does wake detection + VAD + whisper transcription locally.
    app/brain.py, app/orchestrator.py, app/main.py: unchanged from single-host design.

HARDWARE: Arduino Micro drives 3 active-LOW relays on pins D4/D5/D6 gating Belifu positive output into three dorsal-hand electrodes (index, middle, pinky metacarpals). Shared ground on wrist. Physical SPST kill switch inline with Belifu positive lead, operator-independent, bypasses all software.

THUMB: Not actuated. Three-finger topology is a fixed hardware constraint.

STACK:
  Pi: Python 3.11, Flask, pyserial, opencv-python, numpy, sounddevice.
  Laptop: same plus anthropic, faster-whisper, pyyaml, pyttsx3, PyQt5 (calibration only), requests, python-dotenv.

SHARED TYPES: app/state.py defines Finger, Action, GripType, Confidence, SystemState enums; Command, TriggerEvent, BrainResponse dataclasses. Both laptop modules and any Pi code that returns domain objects import from here.

RECEIVER HTTP CONTRACT (Pi port 5001):
  POST /stimulate {finger, action, duration_ms} → {status, ack}
  POST /stop {} → {status}
  GET /status → {connected, last_ack, watchdog_remaining_ms}
  GET /health → {alive}

CAMERA SERVICE HTTP CONTRACT (Pi port 5002):
  GET /frame → image/jpeg bytes
  GET /health → {alive, frames_served, last_frame_age_ms}

AUDIO SERVICE HTTP CONTRACT (Pi port 5003):
  GET /audio → application/octet-stream chunked; 3200-byte chunks, 16kHz mono int16 PCM, 100ms per chunk
  GET /health → {alive, stream_active, listener_count, samplerate, channels, chunk_ms}

BRAIN CONTRACT (Claude's JSON output):
  {
    "acknowledgement": str,
    "confidence": "high"|"medium"|"low",
    "refusal": str|null,
    "commands": [{finger, action, duration_ms}, ...]
  }
  Rules: refusal and non-empty commands are mutually exclusive. Max 1000ms per command. Max 8 commands per sequence.

GRIPS:
  cylindrical = all three fingers on (can, bottle, mug)
  pinch = index + middle on, pinky off (pen, small object)
  lateral = index off, middle + pinky on (key, card)

SAFETY: Kill switch is hardware, bypasses everything. Firmware adds 3s watchdog, 2s per-finger cap, channel mutex. Bridge has priority /stop path bypassing the serial command lock (benchmarked 2.7ms on localhost, budget 100ms over network). Orchestrator adds 500ms abort window after TTS acknowledgement with voice/keyboard/kill-switch abort paths. Brain validator enforces refusal/commands exclusivity, duration caps, sequence length cap. Claude system prompt refuses sharp, hot, no-object, off-topic, impossible-motion.

CONFIG: config.yaml on laptop holds all tunables. Env vars override: MONDAY_RECEIVER_URL, MONDAY_CAMERA_URL, MONDAY_AUDIO_URL, MONDAY_RECEIVER_HOST, MONDAY_CAMERA_INDEX, MONDAY_AUDIO_INPUT, MONDAY_WAKE_PHRASE, MONDAY_WHISPER_MODEL, MONDAY_CLAUDE_MODEL, MONDAY_VAD_THRESHOLD, ANTHROPIC_API_KEY.

MY TONE PREFERENCES: Active voice, direct language, no dashes or semicolons or emojis, concrete and specific, natural conversational tone.
