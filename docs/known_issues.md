# Known issues

Living list. New items at the top, historical items at the bottom. Each entry should say what the issue is, whether it is currently fixed or outstanding, and what a future change should watch for.

## Firmware v2.0 safety regressions (accepted)

**Status:** accepted trade-off on 2026-04-18.

Swapped from the v1 firmware (`docs/shared_context.md` refers to v1 behavior) to v2.0 as-is. The v2.0 sketch trades safety features for speed and added functionality. Regressions accepted by the operator:

- **No channel mutex.** v1 forced the other two relays OFF before closing any one. v2.0 does not. Multiple fingers can fire simultaneously. The production receiver protocol still sends one command at a time, but nothing at the firmware layer prevents concurrent energization if two commands arrive within a short window.
- **No silence watchdog.** v1 forced all relays OFF after 3 seconds of no serial activity. v2.0 has no such behavior. If the host crashes mid-pulse, the pulse runs until the max-on cap fires.
- **Max-on cap extended from 2 s (per-finger) to 5 s (global).** Any relay held ON longer than 5 seconds is forced OFF via `AUTO-OFF`. Doubles the worst-case unsafe window.
- **ACK format changed.** v1 returned `OK:FINGER:INDEX:ON`. v2.0 returns `OK`. Receiver treats last_ack as opaque, no code change needed, but `/status` output is less informative.
- **No `WATCHDOG` or `TIMEOUT:<FINGER>` messages.** Only `AUTO-OFF` + `OFF`. Receiver can no longer distinguish "watchdog tripped" from "per-finger cap tripped" from generic stop.
- **Pin change.** D2/D3/D4 replaces D4/D5/D6. Physical wiring changed accordingly before the swap.

Mitigations that remain: physical kill switch, bridge priority `/stop`, bridge `duration_ms` cap (1000 ms), orchestrator 500 ms abort window, Claude refusal categories. The operator accepted this trade because the bench rig already met those constraints and the v2.0 feature set (CHORD, PIANO, SIGN) is desired for post-demo work.

## Receiver binds `0.0.0.0` on the Pi and trusts every caller

**Status:** accepted for demo.

`hardware/receiver.py` binds `0.0.0.0:5001` so the laptop on the shared hotspot can reach it. There is no authentication, no TLS, no rate limiting. Any client on the same network can POST `/stimulate` and drive the EMS relays. Adequate for a two-device hotspot the operator controls end-to-end. Not adequate for any deployment on a network with untrusted peers. Fix would be bearer-token auth or TLS with a pinned cert; neither is implemented. Set `MONDAY_RECEIVER_HOST=127.0.0.1` for single-host testing or if you need to loopback-only the bind for any reason.

## Phone hotspot client isolation may block Pi to laptop traffic

**Status:** environmental.

Some carrier hotspots enable AP isolation by default, which blocks peer-to-peer traffic even between two devices both joined to the hotspot. Symptom: `ping` between laptop and Pi times out despite both showing IPs in the same subnet. Test with `ping` before assuming the Pi services are broken. If isolation cannot be disabled on the carrier's hotspot, fall back to a Pi-as-AP setup or a travel router.

## Reconnect log spam during Pi outage

**Status:** accepted for demo.

Both `app/vision.py` and `app/voice.py`'s `PiAudioSource` retry at 1 Hz during a Pi outage. A 30-second outage produces roughly 30 log lines per client. Loud by design so the operator notices. If this ever becomes an ops concern, switch to exponential backoff (1s, 2s, 4s, capped).

## mDNS (`monday-pi.local`) may not resolve on some phone hotspots

**Status:** outstanding, operator falls back to the IP.

`config.yaml` defaults `pi_services.camera_url` (and later `audio_url`) to `http://monday-pi.local:5002`. Many phone hotspots drop mDNS/Bonjour traffic or isolate clients, which breaks `.local` resolution. If the laptop cannot reach the Pi by hostname, find the Pi's IP with `hostname -I` on the Pi and either edit `config.yaml` or export `MONDAY_CAMERA_URL=http://<ip>:5002`. Same pattern will apply to the audio service once it lands on the laptop side.

## Fixtures: shot third-person, demo uses head-mount perspective

**Status:** outstanding, flagged on hardware day.

`tests/fixtures/*.jpg` were shot third-person on a tabletop. The live demo uses a head-mounted camera, which sees the scene at a different scale, angle, and with occasional occlusion by the reaching hand. The `test_brain.py` eval may drop slightly on re-shot first-person fixtures. Procedure for re-shooting lives in `docs/hardware_day.md` Phase 2.8. If the head-mount eval drops meaningfully (below 6 of 8), keep the third-person set as the capability baseline and add a dedicated first-person set as a second eval rather than overwriting the baseline.

## Hardware: thumb is not actuated

**Status:** accepted constraint, not a defect.

The three-finger topology (index, middle, pinky via D4/D5/D6) does not include the thumb. This is a fixed hardware design, not a software limitation.

Implications:
- All three supported grips (cylindrical, pinch, lateral) actuate only the three named fingers. The thumb is passive throughout.
- Grasps are weaker than a true opposed grip. Heavy objects may slip. Demo objects must be light.
- Adding a fourth grip type that assumes thumb control (for example, a precision pinch needing thumb-to-index opposition) is not supported. Do not add grip types to the system prompt without verifying the actuation maps to available fingers.
- A production version would add thumb actuation as the first priority improvement, requiring a fourth relay channel, a fourth electrode on the thenar eminence, and a fourth finger value in `app/state.py`.

## Brain: Haiku run-to-run variance on ambiguous objects

**Status:** accepted, switch model if it matters for a given session.

Haiku 4.5 flips between `MEDIUM` (with verbal hedge) and `HIGH` (no hedge) on the `ambiguous.jpg` case across runs. Both outputs are safe, but the two behaviors look different on the overlay. If a demo needs reproducible confidence classification on ambiguous cases, set `MONDAY_CLAUDE_MODEL=claude-sonnet-4-5-20250929`. Sonnet has called `LOW` consistently on this fixture in earlier cross-checks.

## Overlay: `cv2.namedWindow` fails on headless boxes

**Status:** expected, clear error at the right layer.

Running `python -m app.main` in a headless environment raises `cv2.error` at `namedWindow`. `run_overlay_loop` logs the error with a useful message before re-raising. For state-machine smoke testing without a display, use `tests/test_orchestrator_integration.py` which does not open a window.

## Voice: `audioop` removed in Python 3.13

**Status:** fixed 2026-04-18, numpy-based RMS replaced stdlib `audioop.rms`.

Historical. `app/voice.rms_int16` is numerically equivalent to `audioop.rms(pcm, 2)` for int16 input, so existing `MONDAY_VAD_THRESHOLD` values (default 500) still apply. Verified by `tests/test_voice_rms.py`.

## Voice: `keyboard` library root requirement

**Status:** fixed 2026-04-18, `ManualTrigger` now uses `input()` on stdin.

Historical. The previous `ManualTrigger` relied on the `keyboard` Python library, which watches `/dev/input` globally and typically needs root on Linux. Replaced with a plain blocking `input()` loop. No extra deps, no permissions problem.

## Tests: `"grabbing with"` hedge-phrase overlap

**Status:** fixed 2026-04-18, removed from the hedge list.

Historical. The hedge-phrase allow-list in `tests/test_brain.py` formerly included `"grabbing with"`, which appears in every HIGH-confidence ack template in the system prompt. A future drift could have false-passed an `uncertain` case with a HIGH-confidence ack. If the hedge list grows again, screen each new entry against the three HIGH-confidence ack templates in `prompts/system_prompt.txt`.

## Bridge: connection state is not actively probed

**Status:** outstanding, low priority for demo.

`hardware/receiver.py` reports `connected=True` from `port.is_open`, which stays true after a mid-session cable cut until the next write fails. A periodic `PING` on `/health` or a background pinger would detect drops sooner. Not fixed because the firmware's 3 s watchdog guarantees safe state regardless.

## Bridge: no per-session total-stim-time budget

**Status:** outstanding, flagged before human trials.

Nothing tracks cumulative stim time per finger per session. For research use on healthy volunteers this should be added before extended sessions. Not in scope for the current demo.

## Firmware: relay polarity assumed active-LOW, not verified in code

**Status:** outstanding, bench-checklist covers it.

The sketch uses `RELAY_ON = LOW` based on the common active-LOW relay module. If the actual module is active-HIGH, boot state is ON, which is catastrophic. The firmware README bench checklist catches this manually during first bring-up. A future change could drive the pin, read back the onboard LED state, or wire a current-sense line; out of scope for the prototype.

## Supervisor

**Status:** outstanding.

If `hardware/receiver.py` crashes, nothing restarts it. The firmware watchdog forces relays OFF after 3 s so the failure is safe, but silent. A systemd unit or similar would close the loop. Not in scope yet.

## Python env gotcha on the dev machine

**Status:** environmental, documented.

Two pip installs coexist on the current dev box (system Python 3.12 and Anaconda Python 3.12 at `/home/arunj/anaconda-env/bin/python`). Tests run under the Anaconda interpreter. Install deps with that pip, not system pip, or the imports won't resolve at test time.
