# Pi services

All code in this directory runs on the Raspberry Pi that sits in the wearable case. The Pi hosts three services.

- [hardware/receiver.py](../hardware/receiver.py) (port 5001): Arduino serial bridge. See [hardware/README.md](../hardware/README.md).
- [pi/camera_service.py](camera_service.py) (port 5002): webcam capture over HTTP.
- [pi/audio_service.py](audio_service.py) (port 5003): microphone audio streaming. `GET /audio` returns a chunked response of 3200-byte chunks at 16 kHz mono int16 PCM, 100 ms per chunk.

All three bind to `0.0.0.0` so the laptop on the shared hotspot network can reach them. The laptop runs the application stack (brain, orchestrator, vision client, voice client, overlay, TTS) and pulls from these services.

## Deployment

1. Clone this repo onto the Pi.
2. Install Pi-side dependencies:
   ```bash
   pip install -r pi/requirements.txt
   ```
3. Install OpenCV and PortAudio system packages (faster and more reliable than pip on ARM):
   ```bash
   sudo apt install python3-opencv libportaudio2
   ```
   `libportaudio2` is required by `sounddevice` for the audio service.
   On older Pi OS (Bullseye and earlier) you also needed `libatlas-base-dev`
   for numpy's BLAS backend. Bookworm and later ship numpy wheels with
   OpenBLAS built in, and the atlas apt package was dropped. If your
   system has it available, installing it is harmless but not required.
4. Verify the camera:
   ```bash
   v4l2-ctl --list-devices
   fswebcam test.jpg
   ```
5. Start all three services at once with the launcher script:
   ```bash
   ./pi/start_services.sh
   ```
   The script backgrounds all three services into `pi/logs/*.stdout.log`, probes each `/health`, prints the Pi's IP addresses for the laptop's `MONDAY_*_URL` env vars, and waits. Ctrl-C cleanly stops all three.

   Or run each service in its own terminal:
   ```bash
   python hardware/receiver.py
   python -m pi.camera_service
   python -m pi.audio_service
   ```
6. From the laptop on the same network, verify all three:
   ```bash
   curl http://<pi-ip>:5001/health
   curl -s -o test.jpg http://<pi-ip>:5002/frame && file test.jpg
   curl http://<pi-ip>:5003/health
   # Pull ~1 second of raw audio (expect around 32000 bytes = 10 chunks).
   curl -s --max-time 1 http://<pi-ip>:5003/audio | head -c 32000 | wc -c
   ```

## Network setup

Turn on the phone hotspot, join it from both the Pi and the laptop, then find the Pi's IP:

```bash
hostname -I
```

On the laptop, point the environment at that IP:

```bash
export MONDAY_RECEIVER_URL=http://<pi-ip>:5001
export MONDAY_CAMERA_URL=http://<pi-ip>:5002
export MONDAY_AUDIO_URL=http://<pi-ip>:5003
```

Or set the same values in `config.yaml`.

## Environment variables

`pi/camera_service.py`:

| variable | default | effect |
|----------|---------|--------|
| `MONDAY_PI_CAMERA_INDEX` | 0 | which `/dev/videoN` to open |
| `MONDAY_PI_CAMERA_WIDTH` | 1280 | requested capture width |
| `MONDAY_PI_CAMERA_HEIGHT` | 720 | requested capture height |
| `MONDAY_PI_CAMERA_FPS` | 30 | requested capture FPS |
| `MONDAY_PI_JPEG_QUALITY` | 80 | JPEG quality 1-100 |
| `MONDAY_PI_CAMERA_PORT` | 5002 | Flask listen port |

`pi/audio_service.py`:

| variable | default | effect |
|----------|---------|--------|
| `MONDAY_PI_AUDIO_DEVICE` | system default | int index or substring of device name |
| `MONDAY_PI_AUDIO_PORT` | 5003 | Flask listen port |

Sample rate (16 kHz), channels (1), dtype (int16), and chunk duration (100 ms) are fixed. The laptop voice layer assumes those exact values.

## Troubleshooting

- **Camera init fails at startup.** Flask still starts, `/health` returns `alive: true` with `last_frame_age_ms: -1`, `/frame` returns 503. Check `/dev/video0` exists and the user is in the `video` group.
- **`/frame` returns 503 with "no frame captured".** Grabber thread is running but `cap.read()` returns nothing. Often a USB bandwidth issue on the Pi. Lower resolution (`MONDAY_PI_CAMERA_WIDTH=640 MONDAY_PI_CAMERA_HEIGHT=480`) or FPS.
- **Audio init fails at startup, no input device.** Run `python -m pi.audio_service --list-devices` to see what PortAudio enumerated. Pin the right one with `MONDAY_PI_AUDIO_DEVICE=<index or name>`.
- **`OSError: PortAudio library not found` or similar.** `sudo apt install libportaudio2`, then reboot or re-plug the USB mic.
- **`/audio` returns 503.** `GET /health` first. If `stream_active: false`, the sounddevice stream did not start; check `pi/logs/audio_service.log` for the underlying error.
- **Laptop can't reach the Pi on the hotspot.** Confirm both devices show up in the phone's hotspot client list. Some phones block client-to-client traffic by default; turn AP isolation off.
