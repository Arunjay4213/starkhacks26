# Monday

Vision-guided assistive grasping prototype for upper motor neuron injury research.

## What this is

A research prototype that helps a patient grip objects by combining a voice trigger, a camera feed, a Claude-based grip planner, and electrical muscle stimulation of three fingers on the dorsal hand. The patient says what they want, the camera sees the scene, Claude picks a grip, the system acknowledges with a short abort window, then fires the EMS channels. Upper motor neuron injury patients can reach for objects but cannot complete the hand closure; Monday closes that gap while the pathway rebuilds.

Not a medical device. Research use only, with explicit consent and a physical kill switch inline with the EMS positive lead.

## Repo layout

```
firmware/     Arduino Micro sketch. Relay control with mutex, watchdog, per-finger caps.
hardware/     Flask HTTP bridge on 127.0.0.1:5001. Owns the USB serial port to Arduino.
app/          Python application layer.
              state.py         shared enums and dataclasses, wire-contract truth
              vision.py        webcam grabber thread
              voice.py         wake-phrase + VAD intent capture, ManualTrigger fallback
              brain.py         Claude vision call and JSON validator
              orchestrator.py  state machine that glues the layers together
              main.py          entry point, cv2 overlay window, hotkeys, shutdown
calibration/  PyQt5 tool for electrode placement and intensity tuning
prompts/      system_prompt.txt for the brain layer
tests/        pytest suite, mock receiver, fixtures, integration scripts
docs/         known_issues.md and other operator notes
```

## Quick start (software only, no hardware)

```bash
pip install -r requirements.txt
python tests/mock_receiver.py &
python -m app.main
```

In the overlay window, press SPACE, type `grab the cup`, and press Enter. Watch the mock receiver terminal log the `/stimulate` POSTs. Press Q or ESC to quit.

`ANTHROPIC_API_KEY` must be set in the environment or in a `.env` file for the brain layer to work. Copy `.env_empty` to `.env` and fill in the key.

## Full setup (with hardware)

The end-to-end cold-start procedure lives in [docs/integration_bringup.md](docs/integration_bringup.md). Summary:

1. **Flash the firmware.** See [firmware/README.md](firmware/README.md) for board settings, pinout, and the bench checklist before any session with a subject.
2. **Wire the electrodes.** Three dorsal-hand channels (index, middle, pinky metacarpals), shared ground on the wrist, physical kill switch inline with the EMS positive lead. Detailed wiring notes live in [hardware/README.md](hardware/README.md).
3. **Calibrate.** Run [calibration/stimGUI.py](calibration/stimGUI.py) to find clean twitch placement and the right Belifu dial per channel. Record the session in [hardware/calibration_log.md](hardware/calibration_log.md) and run Verify Safety before every session.
4. **Bring the Pi services up.** SSH to the Pi and run `./pi/start_services.sh`. Full procedure in the integration guide.
5. **Bring the laptop up.** Join the phone hotspot, set the three `MONDAY_*_URL` env vars to the Pi's IP, then `python -m app.main`.

## Configuration

Edit [config.yaml](config.yaml) for settings that don't change often (camera resolution, voice wake phrase, confidence scaling, overlay geometry). Environment variables override individual components:

| variable | default | effect |
|----------|---------|--------|
| `ANTHROPIC_API_KEY` | _required_ | brain calls Claude |
| `MONDAY_CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | brain model choice |
| `MONDAY_SERIAL_PORT` | auto-detected by VID | bridge serial port override |
| `MONDAY_CAMERA_INDEX` | 0 | which `/dev/videoN` vision opens |
| `MONDAY_CAMERA_WIDTH` / `MONDAY_CAMERA_HEIGHT` | 1280 x 720 | capture size |
| `MONDAY_AUDIO_INPUT` | system default | mic pin by index or name substring |
| `MONDAY_WAKE_PHRASE` | `hey monday` | voice trigger |
| `MONDAY_WHISPER_MODEL` | `base` | faster-whisper model |
| `MONDAY_WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` |
| `MONDAY_VAD_THRESHOLD` | 500 | RMS floor for silence detection |

## Hardware constraint: three fingers only

The current EMS harness drives three relays for index, middle, and pinky. The thumb is not actuated. Grip types are defined accordingly (cylindrical = all three on, pinch = index + middle, lateral = middle + pinky). See [docs/known_issues.md](docs/known_issues.md).

## Troubleshooting

### Camera

- **Won't open.** Check `/dev/video0` exists and the user is in the `video` group. On WSL2 the host webcam is not forwarded by default. Override the index with `camera_index` in `config.yaml` or `MONDAY_CAMERA_INDEX`.
- **Three test frames are identical.** Run `python -m app.vision` and compare `/tmp/monday_test_frame_{1,2,3}.jpg`. If they match, the grabber thread is not draining new frames. Usually a driver or buffer-depth quirk.

### Voice

- **"Voice disabled: typing fallback active" on startup.** `faster-whisper`, `sounddevice`, or PortAudio missing. Install per the voice deps section below. The demo still works because `ManualTrigger` fires on SPACE + typed intent.
- **Nothing triggers when speaking.** Run `python -m app.voice --list-devices` to confirm the mic is visible. Pin it with `MONDAY_AUDIO_INPUT=<index or name>` or `voice.input_device` in `config.yaml`.
- **Wake phrase misses.** Bump the model (`MONDAY_WHISPER_MODEL=small`) or lower `MONDAY_VAD_THRESHOLD` if the mic has a noisy floor.
- **Using a phone as the mic.** Install WO Mic on the phone and the matching driver on the laptop. Connect over USB (more reliable than WiFi for live demos). Verify with `--list-devices`, then either make the phone the system default input or pin via `MONDAY_AUDIO_INPUT`. Hold the phone 15 to 30 cm from the mouth.

### TTS

- **No spoken acknowledgements.** `pyttsx3` missing or no audio output device. The orchestrator prints `[TTS] ...` to stdout instead. Safe to ignore for a dry run.

### Receiver

- **503 on every request.** Arduino is not reachable. Check the cable, then `dmesg` or `lsusb` for the board. Bridge will retry one reconnect automatically; beyond that you need to restart it.
- **Arduino not detected.** The bridge looks for VID `0x2341`. Non-genuine boards may use a different VID; set `MONDAY_SERIAL_PORT=/dev/ttyACM0` to bypass auto-detect.
- **Bridge not responding at all.** Make sure `hardware/receiver.py` or `tests/mock_receiver.py` is running on the URL in `config.yaml` (default `http://127.0.0.1:5001`).

### Overlay

- **`cv2.error` or window does not open.** You are running in a headless environment. `app.main` needs a real display or X-forwarding. Use `tests/test_orchestrator_integration.py` instead for state-machine smoke testing without a window.

### Voice and vision dependencies

Not everyone needs these. Installed via `requirements.txt` but skip the install if the target machine is headless or has no mic.

```bash
pip install faster-whisper sounddevice
sudo apt install libportaudio2   # Linux system package needed by sounddevice
```

## Install map

| file | contents | when |
|------|----------|------|
| `requirements.txt` | demo runtime: bridge, brain, vision, voice, TTS | always |
| `requirements-dev.txt` | pytest and fixture validator (Pillow) | running the test suite |
| `requirements-calibration.txt` | PyQt5 for the calibration GUI | operator doing electrode placement |

`requirements-calibration.txt` is separate because PyQt5 has no wheel on PyPI for aarch64. Linux ARM installs build from source and need `qtbase5-dev` first; x86_64 Linux, macOS, and Windows get wheels.

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Passes 22 tests in about 10 seconds on a laptop. All local, no API calls.

The following scripts are not pytest and cost real API money to run:

- `python tests/test_brain.py` — eight-case prompt eval. Requires `ANTHROPIC_API_KEY`. One Claude call per case.
- `python tests/test_orchestrator_integration.py` — end-to-end trace against the mock receiver. Three Claude calls per run.
- `python tests/measure_stop_latency.py` — priority-stop HTTP latency benchmark. No API calls, just local timing.

## Safety

The full safety checklist lives at [docs/safety_checklist.md](docs/safety_checklist.md) — read it before powering the Belifu. The short version of the runtime invariants, also summarized at the top of [firmware/monday_ems/monday_ems.ino](firmware/monday_ems/monday_ems.ino) and [hardware/receiver.py](hardware/receiver.py):

1. Physical kill switch on the EMS positive lead is the primary safety.
2. Firmware enforces a channel mutex, a 3 s serial watchdog, a 2 s per-finger on-time cap, and boot-OFF.
3. Bridge caps `duration_ms` at 1000, validates enum values, and serializes writes. `/stop` bypasses the command lock and returns in microseconds.
4. Orchestrator adds a 500 ms abort window after every acknowledgement and scales durations down by confidence.
5. Every layer assumes the layers above and below may fail.
