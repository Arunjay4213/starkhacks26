# Cold-start integration

Bring the full Monday system up from nothing. Assumes the code is deployed to both Pi and laptop, the phone is available as a hotspot, and the wearable-case hardware is wired but not powered.

For the purpose of this document "not powered" means the Belifu unit is off and the kill switch is open. The Pi, laptop, and Arduino are free to be on.

Different audience from [safety_checklist.md](safety_checklist.md) (invariants) and [hardware_day.md](hardware_day.md) (session procedure). This doc is the network and process bringup, nothing safety-critical.

## Order

1. Phone hotspot
2. Pi
3. Pi services
4. Laptop network join
5. Laptop network probe
6. Laptop app stack
7. End-to-end verification

## 1. Phone hotspot

- Enable personal hotspot on phone. Note the SSID and password.
- Confirm cellular data is enabled. The laptop needs upstream internet for Claude API calls.
- Keep the phone charged. Budget roughly 20% battery per hour with hotspot active.

## 2. Pi

- Power the Pi via the USB-C battery pack in the wearable case. Wall power works for a bench test.
- Wait for boot, about 30 seconds.
- SSH in from the laptop, or use an attached keyboard and monitor for first-time setup.
- Verify the Pi joined the hotspot:

  ```bash
  ip addr show wlan0    # should show an IP on the phone's subnet
  ping -c 2 <phone's gateway IP>
  ```

- If the Pi did not auto-join, connect via `nmcli` or `raspi-config`.

## 3. Pi services

- SSH to the Pi.
- `cd` into the monday repo.
- Run:

  ```bash
  ./pi/start_services.sh
  ```

- All three services should report `OK`.
- Note the Pi IP address(es) printed at the end.

## 4. Laptop network join

- Disconnect from any other WiFi.
- Join the phone hotspot.
- Confirm upstream internet works:

  ```bash
  curl -s https://api.anthropic.com/v1/messages | head
  ```

  An auth error in the body is fine. A network-layer error (connection refused, no route) means the hotspot has no upstream.

- Confirm the Pi is reachable:

  ```bash
  ping -c 2 <pi IP>
  ```

## 5. Laptop network probe

- Export the three service URLs (or set them in `.env` / `config.yaml`):

  ```bash
  export MONDAY_RECEIVER_URL=http://<pi IP>:5001
  export MONDAY_CAMERA_URL=http://<pi IP>:5002
  export MONDAY_AUDIO_URL=http://<pi IP>:5003
  ```

- Probe each:

  ```bash
  curl -s $MONDAY_RECEIVER_URL/health
  curl -s $MONDAY_CAMERA_URL/health
  curl -s $MONDAY_AUDIO_URL/health
  ```

- All three should return `{"alive": true, ...}`.

## 6. Laptop app stack

- Ensure `ANTHROPIC_API_KEY` is set (env or `.env`).
- Run:

  ```bash
  python -m app.main
  ```

- The overlay window opens.
- Startup log should show, in order:

  ```
  build_stack: vision ready (url=http://<pi>:5002)
  build_stack: orchestrator ready
  VoiceListener started ... audio_url=http://<pi>:5003
  build_stack: voice ready (url=http://<pi>:5003)
  build_stack: manual trigger ready
  ```

- If any line is replaced by a warning about `did not respond to /health`, go back to step 5 and check that specific service.

## 7. End-to-end verification

Run the full command path with the Belifu off and the kill switch open. No current flows. This is a safe dry run of the network and software path.

- Press SPACE in the overlay window.
- In the terminal, type `grab the cup` and press Enter.
- Overlay should walk through `capturing`, `processing`, `acknowledging`, then `executing`.
- The Pi receiver log (tail `pi/logs/receiver.stdout.log`) should show three `POST /stimulate` entries.
- The Arduino relays should audibly click.
- State returns to `idle`.

If that works, the network, software, and hardware wiring are all correct. Proceed to [hardware_day.md](hardware_day.md) for the session procedure with actual EMS current.

## Common failures

- **`ping` to Pi times out.** Phone hotspot may have client isolation (AP isolation) enabled. Check phone settings. Some carrier hotspots block peer-to-peer traffic by default.
- **`curl` to Pi services times out but `ping` works.** Services are not running. SSH back to the Pi and check `pi/logs/*.stdout.log` and `pi/logs/*.log`.
- **`ANTHROPIC_API_KEY` works on normal WiFi but not on the hotspot.** Phone cellular data may be paused or exhausted. Check the phone.
- **Audio stream drops repeatedly during a session.** Hotspot bandwidth instability. Move closer to the phone or switch phone hotspot to 5 GHz if supported. `app/voice.py` will reconnect automatically and log each reconnect.
- **Vision fetches but takes more than one second per frame.** Hotspot latency or interference. Try another WiFi channel on the phone. Last resort, lower `MONDAY_PI_CAMERA_WIDTH` / `HEIGHT` on the Pi to shrink JPEG payloads.
- **Receiver accepts commands but Arduino does not click.** Check `pi/logs/receiver.stdout.log` for `ERR:` responses. Most often a USB cable issue or the Arduino reset into the bootloader after a sketch re-flash.

## Shutdown order

1. Quit laptop `app.main` (Q or ESC in the overlay).
2. SSH to Pi, Ctrl-C on `start_services.sh`.
3. Power off the Pi: `sudo shutdown -h now`, wait for the green LED to stop, unplug battery.
4. Turn off the phone hotspot.
