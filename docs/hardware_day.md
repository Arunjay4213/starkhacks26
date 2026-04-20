# Hardware day

First physical bench session. Four phases: network and services bringup, pre-flight, calibration, dry run. Do not skip phases. Do not reorder phases. Abort criteria mean stop, not "make a note and continue."

This session uses the wearable architecture: a Raspberry Pi 5 in the case on the operator's arm runs `hardware/receiver.py`, `pi/camera_service.py`, and `pi/audio_service.py`. The laptop on the bench runs `app/main.py` and talks to the Pi over the phone hotspot network. See [integration_bringup.md](integration_bringup.md) for the network setup; this doc starts after that document's Step 7 succeeded.

## Roles

**Operator.** Runs software, issues voice commands, wears the electrodes and the head-mounted webcam. Hands stay visible at all times so the safety person can see what's happening.

**Safety person.** One job: hand on the kill switch whenever the Belifu is powered. Watches the operator's hand and the wiring. Not debugging code. Not looking at the overlay. If anything looks wrong (unexpected twitch, wrong finger firing, sustained contraction, visible discomfort), flip the kill switch immediately. Do not wait to confirm. Do not ask for permission. Flip first, discuss after.

## Scope limitations (read before Phase 0)

- **Three fingers only.** Index, middle, pinky are actuated. The thumb is passive. Pick demo objects that do not require thumb opposition. A standard 12 oz can or a pen works. A wide glass or a heavy ceramic mug does not.
- **No per-session total-stim-time budget.** The firmware caps any single pulse at 5 seconds (v2.0 max-on cap) and the bridge caps any single command at 1 second, but nothing tracks cumulative on-time. The operator is the limit here. Stop and rest the skin if any channel has been firing for more than a few minutes of aggregate on-time.
- **The receiver binds `0.0.0.0` on the Pi.** Anyone on the hotspot can drive `/stimulate`. Adequate for a two-device session the operator controls. Never leave this running on an untrusted network.

## Known issues to watch for during this session

These are known and accepted for the demo. Recognize them fast so you don't waste time debugging the wrong thing.

- **Phone hotspot client isolation.** Some carrier hotspots block peer-to-peer traffic by default. If `ping` from laptop to Pi fails at Phase 0, this is the first suspect. Check the phone's hotspot settings.
- **mDNS (`monday-pi.local`) may not resolve.** Use the Pi's IP directly via `hostname -I`. Set `MONDAY_RECEIVER_URL`, `MONDAY_CAMERA_URL`, `MONDAY_AUDIO_URL` accordingly.
- **Reconnect log spam during outages.** `app/vision.py` and `app/voice.py` retry at 1 Hz when the Pi drops off. A 10 second outage produces 10 log lines per client. Loud by design so the operator notices. Not an error.
- **Bridge `connected` flag lies on USB drop.** `hardware/receiver.py` reports `connected: true` from the `port.is_open` state, which stays true after a mid-session USB cable unseat until the next write fails. If anything behaves oddly during a dry run, check the USB connection and `/status` before anything else.
- **Haiku confidence flakiness on ambiguous scenes.** Phase 3.1 pins `MONDAY_CLAUDE_MODEL=claude-sonnet-4-5-20250929` to avoid this. Leave it pinned.

## Materials checklist

### Wearable case (goes on operator's arm)

- [ ] Raspberry Pi 5 with microSD card, the monday repo cloned on it
- [ ] USB-C battery pack or wall adapter for the Pi (wall is fine for a bench test)
- [ ] Arduino Micro with firmware flashed, USB cable Arduino-to-Pi
- [ ] Relay module, 3 channels wired to D2/D3/D4 (firmware v2.0)
- [ ] Belifu EMS unit with dual-channel output
- [ ] SPST kill switch, inline with Belifu positive output
- [ ] Four electrode pads: three 1×3cm cut pads + one 3×3cm ground
- [ ] Head-mounted webcam (USB to Pi) with built-in microphone
- [ ] Head-mount hardware: baseball cap with tripod clip, or GoPro-style strap

### Bench

- [ ] Laptop with full software stack installed and tested
- [ ] Phone for hotspot (cellular data enabled, charged)
- [ ] Multimeter with continuity test mode
- [ ] Three demo objects: can (or other ~2 inch cylinder), pen, key
- [ ] Rubbing alcohol and cotton pads for skin prep
- [ ] Light-colored skin marker for electrode position tracing
- [ ] Phone or camera for calibration placement photos
- [ ] `hardware/calibration_log.md` open and ready to fill in

## Wearable setup (head-mount)

The camera is head-mounted on the operator. The webcam's built-in mic is the audio input, captured by the Pi's `audio_service` and streamed to the laptop. These setup steps happen before Phase 0.

**Mount hardware.**
  - Use a baseball cap with a tripod-mount clip, or a head strap compatible with the webcam's mount. Test the fit before wiring anything.
  - The camera pitch should angle slightly downward (roughly 20 to 30 degrees below horizontal) so that when the operator looks at an object on a table at normal reaching distance, the object is centered in frame.

**Cable strain relief.**
  - The webcam USB cable runs from the head to the Pi in the case. Tape or clip it to the operator's shirt between the back of the neck and the case so head movement does not yank the connector.
  - Same advice for any other USB that crosses a joint.

**Opposite-side routing.**
  - The head-mounted camera cable should be on the opposite side of the working hand so the electrode leads and the USB cable do not cross each other. If the operator grabs with their right hand, route the camera cable to the left.

**Audio considerations.**
  - Webcam mic near the mouth helps wake phrase detection, but also picks up breathing, clothing rustle, and mouth noise.
  - On the Pi, the audio service default is the system default input. If `python -m pi.audio_service --list-devices` shows the webcam at a non-default index, pin it with `MONDAY_PI_AUDIO_DEVICE=<index or name substring>` in the Pi's environment.
  - On the laptop, VAD threshold default is 500. If intent capture clips early or triggers on breath, bump `MONDAY_VAD_THRESHOLD` to 800 or 1000.

**Verify camera angle before Phase 0.**
  - Fit the head-mount first. Power on the Pi, start the camera service, then from the laptop `python -m app.vision` to pull a few frames and save them to `/tmp/monday_test_frame_*.jpg`. You can do this check in an earlier terminal session before the rest of bringup.
  - Operator looks at their own reaching hand on a table at normal distance.
  - Adjust camera pitch until the hand and the nearby surface are centered.
  - Operator tilts their head naturally through the range of motion they expect to use during the demo, confirming the camera still captures the working area throughout.
  - If the operator has to hold their neck in an unnatural position to get objects in frame, the mount pitch is wrong. Re-aim.

---

## Phase 0: Network and services

Full network bringup must complete before Phase 1.1. This phase is a condensed checklist; see [integration_bringup.md](integration_bringup.md) for the detailed procedure and failure modes.

### Phase 0.1: Hotspot

- [ ] Phone hotspot on, cellular data on
- [ ] Pi joined the hotspot (`ip addr show wlan0` on the Pi shows an IP)
- [ ] Laptop joined the hotspot
- [ ] `ping -c 2 <pi IP>` from the laptop succeeds

**Abort if:** Laptop cannot ping the Pi. First check the phone's hotspot settings for client isolation (AP isolation). Switch to Pi-as-AP or a travel router if isolation cannot be disabled.

### Phase 0.2: Pi services up

- [ ] SSH to the Pi
- [ ] `cd` into the monday repo
- [ ] `./pi/start_services.sh`
- [ ] All three services report `OK` on their `/health` probes
- [ ] Note the Pi IP printed at the end of the script output

### Phase 0.3: Laptop points at the Pi

- [ ] On the laptop, export the three URLs to the Pi IP:

      export MONDAY_RECEIVER_URL=http://<pi IP>:5001
      export MONDAY_CAMERA_URL=http://<pi IP>:5002
      export MONDAY_AUDIO_URL=http://<pi IP>:5003

- [ ] `curl -s $MONDAY_RECEIVER_URL/health` returns `{"alive": true}`
- [ ] `curl -s $MONDAY_CAMERA_URL/health` returns `{"alive": true, ...}`
- [ ] `curl -s $MONDAY_AUDIO_URL/health` returns `{"alive": true, "stream_active": true, ...}`

**Abort if:** Any `/health` fails. Services are not running or the network is wrong. SSH back to the Pi and check `pi/logs/*.stdout.log` and the rotating service logs.

### Phase 0.4: Laptop internet works on the hotspot

- [ ] `ANTHROPIC_API_KEY` is set in the laptop shell or `.env`
- [ ] `curl -s https://api.anthropic.com/v1/messages | head` returns a response body (auth error is fine, no-network is not)

**Abort if:** Laptop has no upstream internet. Claude calls will all fail at trigger time. Check phone cellular data.

---

## Pre-flight

No current flowing through anyone yet. Belifu stays OFF. Electrodes stay off the body. This phase proves the wiring and software talk to each other correctly before any EMS output is possible.

### Phase 1.1: Software stack up (Pi receiver + laptop GUI)

- [ ] Arduino USB is plugged into the Pi, not the laptop
- [ ] `hardware/receiver.py` is running on the Pi (started in Phase 0.2)
- [ ] Pi receiver log shows `READY` from the Arduino (tail `pi/logs/receiver.stdout.log`)
- [ ] On the laptop, `curl -s $MONDAY_RECEIVER_URL/status` returns `connected: true` with a recent `last_ack`
- [ ] On the laptop, open `calibration/stimGUI.py`. In the Receiver field at the top, enter `$MONDAY_RECEIVER_URL` (or paste the Pi URL) and click Apply
- [ ] Connection dot goes green
- [ ] Click `Verify Safety`. All four checks pass. `/stop` latency under 100 ms (budget 100 ms over network)

**Abort if:** Receiver log does not show `READY`. `/status` shows `connected: false`. stimGUI connection dot stays red. `/stop` latency is over 100 ms (network path problem; check hotspot quality).

### Phase 1.2: Kill switch verification (MOST IMPORTANT STEP)

The kill switch is the ultimate safety. If it doesn't actually break the circuit, every other safety layer is theater.

- [ ] Belifu OFF. Electrodes disconnected.
- [ ] Set multimeter to continuity mode
- [ ] Place probes on the two terminals the kill switch connects
- [ ] With switch OPEN (off): multimeter reads open circuit (no beep)
- [ ] With switch CLOSED (on): multimeter reads closed circuit (beep)
- [ ] Toggle switch five times, confirm each state is correct
- [ ] Leave switch in OPEN position

**Abort if:** Switch does not reliably break the circuit. Switch feels loose or intermittent. Any doubt about continuity in the open state. Do not proceed until the switch is replaced or rewired and re-verified.

### Phase 1.3: Relay polarity check

Firmware v2.0 drives PORTD bits high to energize. If the relay board inverts that logic (common on cheap active-LOW modules), boot state is ON instead of OFF. Every relay would be closed the instant the Arduino powers up.

- [ ] Belifu OFF. Electrodes disconnected. Kill switch OPEN.
- [ ] Multimeter on continuity, probes across one relay's COM and NO terminals (start with the index relay on D2)
- [ ] Power-cycle the Arduino by unplugging its USB from the Pi and replugging
- [ ] Immediately after plug-in: relay should be OPEN (no beep)
- [ ] Wait 5 seconds: relay still OPEN (no beep)
- [ ] Repeat for middle (D3) and pinky (D4)

**Abort if:** Any relay shows closed in boot state. The relay polarity is inverted. Fix by flipping the PORTD writes in the firmware (swap `|=` and `&= ~` pairs) or by swapping the relay board.

### Phase 1.4: Relay firing check (dry)

- [ ] Belifu still OFF. Electrodes still disconnected.
- [ ] Kill switch OPEN.
- [ ] In stimGUI, duration slider to 200 ms
- [ ] Click `Index`. Relay clicks audibly. Multimeter confirms COM-NO continuity during the 200 ms pulse, open again after
- [ ] Repeat for `Middle`, `Pinky`
- [ ] Click `Sequence Test`. All three relays fire in order
- [ ] Try clicking two finger buttons within 100 ms of each other. v2.0 firmware has **no channel mutex**, so both relays may end up closed. Expected in this firmware. See known_issues.md.

**Abort if:** A relay doesn't click when commanded. A relay clicks when a different finger was commanded. Relay sticks ON past the 5 second max-on cap.

### Phase 1.5: Max-on cap check (v2.0)

Firmware v2.0 has no silence watchdog. Instead it has a single 5 second max-on cap that fires when any relay has been energized for longer than that, regardless of whether the host is still sending commands. This test exercises that cap.

- [ ] Arduino still connected to Pi. Receiver running.
- [ ] From the laptop terminal:

      curl -X POST $MONDAY_RECEIVER_URL/stimulate \
        -H 'Content-Type: application/json' \
        -d '{"finger":"INDEX","action":"ON"}'

- [ ] No `duration_ms`. Relay should close and stay closed.
- [ ] Wait up to 5 seconds without sending any further commands.
- [ ] Relay opens automatically. Firmware prints `AUTO-OFF` then `OFF` on serial (visible in Pi receiver log).

**Abort if:** Relay stays closed past 5 seconds. The max-on cap is the last line of defense against a stuck-ON command under this firmware.

### Phase 1.6: Verify max-on cap on middle and pinky

Repeat Phase 1.5 for the middle and pinky channels. v2.0's single cap applies per-finger in the sense that any relay ON past the threshold triggers an `AUTO-OFF`, but there is only one timer; confirm each channel individually fires it.

- [ ] Repeat Phase 1.5 with `FINGER:MIDDLE:ON`. Relay opens within 5 s.
- [ ] Repeat with `FINGER:PINKY:ON`. Relay opens within 5 s.

**Abort if:** Any relay stays closed past 5 seconds.

---

**End of pre-flight. Every box above must be checked before proceeding. If any abort criterion triggered, fix the cause and restart the relevant phase from the top. Do not skip ahead.**

---

## Calibration

Now electrodes go on a human. Start with a minimum intensity and ramp up. Safety person's hand stays on the kill switch the entire time.

### Phase 2.1: Skin prep and electrode placement

- [ ] Clean back of hand with rubbing alcohol. Let dry 30 seconds.
- [ ] Reference: UChicago dorsal-hand EMS paper. Electrodes sit along the metacarpal bones of index, middle, pinky.
- [ ] Place 1×3cm electrode along index metacarpal, roughly halfway between knuckle and wrist
- [ ] Place 1×3cm electrode along middle metacarpal, same longitudinal position
- [ ] Place 1×3cm electrode along pinky metacarpal, same position
- [ ] Place 3×3cm ground electrode on dorsal wrist, centered
- [ ] Trace each electrode outline on the skin with the marker. This lets you reposition to the same spots in future sessions.
- [ ] Photograph placement for `hardware/calibration_log.md`

### Phase 2.2: First power-on

- [ ] Belifu unit powered ON, set to Mode 15, intensity at minimum
- [ ] Kill switch still OPEN. No current flowing yet.
- [ ] Safety person confirms their hand is on the kill switch
- [ ] Operator confirms they are ready
- [ ] Safety person closes kill switch
- [ ] Belifu is now live. No relay is closed yet, so no current is flowing to any electrode. The circuit is armed but idle.

### Phase 2.3: Index finger calibration

- [ ] In stimGUI, duration slider to 500 ms
- [ ] Click `Index`. Observe. At minimum intensity, nothing visible yet.
- [ ] Ramp Belifu intensity up by ONE step
- [ ] Click `Index` again. Observe.
- [ ] Repeat ramp-and-test until you see a clean isolated index finger curl
- [ ] Record the intensity level in `hardware/calibration_log.md`
- [ ] Test three more times at this intensity. Same finger curls cleanly each time with no twitching in middle or pinky.

**Abort if:** No response even at moderate intensity (somewhere around level 15 on most Belifu units). Indicates electrode contact issue. Lift electrode, re-prep skin, reapply. Restart 2.3.

**Abort if:** Wrong finger curls. Indicates electrode placement is off. Reposition the electrode slightly proximal or distal along the metacarpal.

**Abort if:** Multiple fingers curl. Indicates crosstalk between channels, probably from electrodes being too close together or skin conductance being too high. Space electrodes further apart. Check ground contact.

**Abort if:** Painful or uncomfortable. You are at too high an intensity. Safety person flips kill switch. Drop intensity and restart.

### Phase 2.4: Middle finger calibration

Repeat 2.3 process for middle finger. Expected intensity may differ slightly from index. Record the value independently.

### Phase 2.5: Pinky finger calibration

Repeat 2.3 process for pinky finger. Pinky often needs slightly higher intensity because the electrode is smaller and the muscle is smaller. If pinky refuses to isolate cleanly (crosstalk with ring finger or middle), accept it and note in the log. Demo can run on cylindrical and pinch grips only if needed; lateral grip is the one that depends most on pinky isolation.

### Phase 2.6: Sequence verification

- [ ] Intensity locked at the calibrated value
- [ ] Click `Sequence Test` in stimGUI
- [ ] Three fingers curl in order: Pinky, Middle, Index
- [ ] Each curl is clean and isolated
- [ ] Repeat three times

- [ ] Update `hardware/calibration_log.md` with final values:
  - Belifu mode
  - Locked intensity per channel (if different)
  - Electrode position photos (filenames)
  - Notes on twitch quality (clean, crosstalk, etc)
  - Any anomalies

### Phase 2.7: Abort test with electrodes live

- [ ] Operator says "test abort"
- [ ] Operator starts a 1000 ms pulse on Index via stimGUI
- [ ] Safety person flips kill switch during the pulse
- [ ] Current stops immediately. Finger relaxes.
- [ ] Safety person re-closes kill switch
- [ ] Relay is still closed (bridge doesn't know about the kill switch)
- [ ] Pulse completes or firmware 2 s cap ends it, either is fine

This test proves the kill switch works with current actually flowing, not just in bench continuity.

**Abort if:** Kill switch doesn't stop current immediately. This is a fundamental failure. Do not continue under any circumstance until resolved.

### Phase 2.8: Re-shoot fixture photos from head-mount perspective

The fixtures currently checked in were shot third-person on a tabletop. The head-mount camera sees the scene differently: objects are larger in frame, sometimes partially occluded by the reaching hand, sometimes tilted. Re-shoot once per operator or whenever the camera angle changes meaningfully.

- [ ] Back up the current fixtures:

      mkdir -p tests/fixtures/_thirdperson_backup
      cp tests/fixtures/{mug,pen,key,empty,ambiguous,unsafe}.jpg tests/fixtures/_thirdperson_backup/

- [ ] Run `python -m app.vision` from the laptop. This fetches from the Pi's camera service and saves three consecutive frames to `/tmp/monday_test_frame_{1,2,3}.jpg`.
- [ ] For each of the six fixtures:
  - Place the target object on the table at normal reaching distance
  - Look at it as if about to grab
  - Re-run `python -m app.vision` and pick the clearest of the three saved frames
  - Rename it to the fixture filename and copy into `tests/fixtures/`
  - Fixtures to capture: `mug.jpg` (a can is fine), `pen.jpg`, `key.jpg`, `empty.jpg` (clear table), `ambiguous.jpg` (thick marker or small bottle), `unsafe.jpg` (scissors or knife)

- [ ] Validate: `python tests/fixtures/validate_fixtures.py`. All six must pass.
- [ ] Re-run the eval: `MONDAY_CLAUDE_MODEL=claude-haiku-4-5-20251001 python tests/test_brain.py`.
  - Expected: 7 or 8 of 8 pass.
  - If 6 or 7 of 8: acceptable, note which case(s) failed in `hardware/calibration_log.md` and continue.
  - If below 6 of 8: do **not** iterate the system prompt on the fly. Revert to the third-person backup, log the gap in `docs/known_issues.md`, and run the demo with live Claude calls against whatever the head-mount camera actually shows. Static fixtures are for regression testing, not for the live demo path.

**Abort if:** Fixture validation fails. Fixture re-shoot takes more than 20 minutes (the head-mount setup is fighting you in ways worth investigating before loading current on the hand).

---

**End of calibration. Leave the electrodes on for the dry run. Do not touch the Belifu intensity knob from this point. If you need to recalibrate, do so; but don't adjust during operation.**

---

## Dry run

Full system live. Three objects on the table. Run the complete user-facing flow. Safety person's hand stays on the kill switch.

### Phase 3.1: Full stack up

- [ ] Pi services still running (Phase 0.2 output still visible; /health endpoints still responding)
- [ ] Laptop env vars still set from Phase 0.3 (`MONDAY_RECEIVER_URL`, `MONDAY_CAMERA_URL`, `MONDAY_AUDIO_URL`)
- [ ] `MONDAY_CLAUDE_MODEL` set to `claude-sonnet-4-5-20250929` for reproducibility on ambiguous scenes
- [ ] Head-mount fit, camera angle verified, fixtures re-shot (Phase 2.8 done)
- [ ] On the laptop: `python -m app.main`
- [ ] Overlay window opens
- [ ] Startup log shows `vision ready (url=...)`, `voice ready (url=...)`, `orchestrator ready`, `manual trigger ready`
- [ ] Operator's hand with electrodes is visible in the overlay

### Phase 3.2: First run (cylindrical)

- [ ] Place can on the table, in camera view
- [ ] Operator: "hey monday, grab the can"
- [ ] Overlay shows: `listening` / `capturing` / `processing`
- [ ] TTS speaks: "grabbing the can with a cylindrical grip"
- [ ] During the 500 ms abort window: safety person ready, do not abort this time
- [ ] Overlay transitions to `executing`
- [ ] Three fingers fire in sequence: index, middle, pinky
- [ ] Observe: do they curl around or onto the can?
- [ ] Overlay returns to `idle`

**Abort if:** Wrong fingers fire. Unexpected sustained contraction. Pain. Anything visually surprising. Safety person flips kill switch.

### Phase 3.3: Refusal run

- [ ] Remove the can. Nothing in frame.
- [ ] Operator: "hey monday, grab it"
- [ ] Overlay: `listening` / `capturing` / `processing` / back to `idle`
- [ ] TTS speaks a refusal ("no object visible" or similar)
- [ ] Refusal flash appears on overlay for 3 seconds
- [ ] No fingers fire

**Abort if:** Any finger fires during a refusal.

### Phase 3.4: Abort-window test

- [ ] Place can in frame
- [ ] Operator: "hey monday, grab the can"
- [ ] During the `acknowledging` window (the 500 ms after TTS starts speaking): operator says "stop stop stop" or presses ESC
- [ ] Execution does not start
- [ ] State returns to `idle`
- [ ] `/stop` POSTed to receiver (visible in Pi receiver log)

**Abort if:** Fingers fire despite the abort command.

### Phase 3.5: Pinch (pen)

- [ ] Remove can. Place pen in frame.
- [ ] Operator: "hey monday, grab the pen"
- [ ] Verify acknowledgement says pinch
- [ ] Execution fires index and middle. Pinky stays still.
- [ ] Overlay shows cylindrical-vs-pinch distinction correctly

**Abort if:** Pinky fires on a pinch grip. Means the system prompt's grip mapping has drifted, or the brain validator accepted a bad response.

### Phase 3.6: Lateral (key)

- [ ] Remove pen. Place key in frame.
- [ ] Operator: "hey monday, grab the key"
- [ ] Verify acknowledgement says lateral
- [ ] Execution fires middle and pinky. Index stays still.

**Abort if:** Index fires on a lateral grip.

### Phase 3.7: Ten consecutive clean runs

Run the full flow ten times in a row, mixing the three grips. Between each run, wait for overlay to return to `idle`. Safety person stays attentive the entire time, even as runs become routine.

- [ ] Run 1: cylindrical, clean
- [ ] Run 2: pinch, clean
- [ ] Run 3: lateral, clean
- [ ] Run 4: refusal, clean
- [ ] Run 5: cylindrical with abort-window cancel, clean
- [ ] Run 6: pinch, clean
- [ ] Run 7: lateral, clean
- [ ] Run 8: cylindrical, clean
- [ ] Run 9: refusal on unsafe object (hold up scissors or a knife), clean
- [ ] Run 10: pinch, clean

**Periodic health check during the ten runs.** After every third run, run `curl -s $MONDAY_RECEIVER_URL/status` on the laptop. Confirm `connected: true` and `watchdog_remaining_ms` is around 3000. If `connected: true` but `last_ack` stops advancing, the USB between Arduino and Pi may have unseated; see the known-issues box above.

**Skin check during the ten runs.** Between runs 5 and 6, safety person looks at the electrode sites. Mild redness is normal. Blistering, welts, or discolored skin is not and means stop.

**If any single run has anomalous behavior, stop. Investigate before continuing. Do not "just one more run" past a weird result.**

### Phase 3.8: Shutdown

- [ ] Operator presses Q in the overlay
- [ ] `app.main` calls shutdown sequence: abort, stop voice, stop manual, release vision, final `/stop` POST
- [ ] Overlay closes
- [ ] Pi receiver continues running, relays all OFF
- [ ] Safety person opens kill switch
- [ ] Belifu powered OFF
- [ ] Electrodes removed from skin
- [ ] On the Pi, Ctrl-C on `start_services.sh` (stops camera, audio, and receiver cleanly)

### Phase 3.9: Post-session log

- [ ] Fill in session record in `hardware/calibration_log.md`:
  - Date, duration of session
  - Per-phase pass/fail
  - Number of successful runs in Phase 3.7
  - Any anomalies observed, even if they didn't trigger an abort
  - Intensity values used
  - Skin condition after electrode removal (redness is normal, blistering is not)
  - Any reconnects or network hiccups noted in the voice/vision logs
  - Cumulative on-time per finger if you tracked it (worth doing even informally)

- [ ] Commit the updated calibration log to git
- [ ] This document (hardware_day.md) can also be committed with checkmarks as a session record

---

## Red flags that mean stop and don't continue this session

Observed during any phase, even if you're tempted to keep going:

- Sustained burning or stinging sensation beyond the pulse duration
- Skin blistering or welts at an electrode site
- Involuntary movement of fingers not being commanded
- Pulse continuing past the firmware 2 s cap (would indicate firmware bug)
- Unexpected behavior you can't immediately explain
- Kill switch feels sluggish or stuck
- Any error in the Pi receiver logs you don't understand

None of these are likely, but each has happened to someone building an EMS rig. Stop, document, debug. Don't push through.

## Post-session cleanup

- [ ] Back up `hardware/calibration_log.md`
- [ ] Back up the Pi rotating logs from `hardware/logs/`, `pi/logs/`, and the laptop logs from `app/logs/`
- [ ] Note the working calibration values somewhere outside the repo too, in case the repo is cloned fresh before the next demo
- [ ] Power off Pi cleanly (`sudo shutdown -h now`), wait for the green LED to stop, unplug battery
- [ ] Turn off the phone hotspot
- [ ] Check skin the following day for delayed irritation
