# Safety checklist

## Summary

Monday drives electrical muscle stimulation current into three dorsal-hand electrodes (index, middle, pinky) under software control, triggered by voice. Current flows through a human. Software errors must not cause harm, so safety is layered: the most trusted layer is mechanical, each software layer assumes the ones below it may fail.

## The safety layers

1. **Physical kill switch.** Single SPST switch inline with the Belifu positive lead. Bypasses everything. Primary safety. The safety person keeps a hand on it whenever the Belifu is powered.
2. **Firmware 5 second max-on cap (v2.0).** Arduino forces all relays OFF if any relay has been ON longer than 5 seconds, regardless of host state. Emits `AUTO-OFF` then `OFF` on serial. Catches stuck-ON commands and host crashes. Replaces the v1 per-finger 2 second cap and 3 second silence watchdog.
3. **Bridge priority `/stop`.** HTTP `/stop` bypasses the serial command lock and returns in under 100 ms even while a timed pulse is in flight. Measured worst-case 2.7 ms in benchmarks.
4. **Bridge per-command duration cap.** The receiver caps any `/stimulate` duration at 1000 ms before the command hits the wire.
5. **Orchestrator abort window.** 500 ms pause after the TTS acknowledgement before execution starts. An abort during this window (voice "stop", hotkey S or ESC, or the kill switch) prevents any current from flowing.
6. **Claude refusal categories.** Sharp, hot, no-object, off-topic, and impossible-motion requests produce a refusal with zero commands. The brain validator enforces refusal/commands exclusivity before the orchestrator ever sees the response.

### Accepted safety trade-offs in v2.0 firmware

See `docs/known_issues.md` for the full entry. Summary:

- **No channel mutex.** Multiple fingers can be energized simultaneously. The receiver protocol still sends one command at a time, but the firmware no longer forces the other two OFF on every `ON`.
- **No silence watchdog.** If the host goes silent mid-pulse, the pulse runs until the 5 second max-on cap. The v1 behavior (all OFF after 3 seconds of silence) is gone.
- **5 second max-on cap replaces the 2 second per-finger cap.** Longer unsafe window before auto-OFF.

## Pre-session verification

Before the Belifu is powered, all four must be true. Record results in [../hardware/calibration_log.md](../hardware/calibration_log.md).

- **Kill switch breaks the circuit.** Multimeter continuity across the positive-lead switch in both positions. OFF = infinite, ON = near zero.
- **Receiver `/stop` returns in under 100 ms.** Run the `Verify Safety` button in [../calibration/stimGUI.py](../calibration/stimGUI.py). Must PASS. Note the measured latency.
- **Firmware max-on cap works.** Issue a `FINGER:INDEX:ON` from the Serial Monitor, do not send OFF, confirm `AUTO-OFF` arrives within 5 s and the relay opens. Repeat for middle and pinky.

## Who does what during a session

### Operator

- Runs the software and wears the electrodes.
- Hands visible at all times.
- Can call "stop stop stop" verbally or press ESC to abort.

### Safety person

- **One job:** hand on the kill switch whenever the Belifu is powered.
- Watches the operator's hand and the wiring, not the overlay.
- Not debugging code.
- If anything unexpected happens, flip first, discuss after.
- Does not need permission to abort.

## Red flags

Any of these means stop immediately and investigate before continuing.

- Sustained burning or stinging that persists past the pulse duration.
- Blistering, welts, or skin discoloration at an electrode site.
- Involuntary movement of a finger that was not commanded.
- Pulse continuing past the firmware 5 second max-on cap.
- Any behavior you cannot immediately explain.
- Kill switch feels sluggish, sticky, or has audible mechanical play.

## What is not covered

- Monday is a research prototype, not a medical device.
- It has not been tested on the target population (upper motor neuron injury patients).
- Every safety layer above assumes a neurotypical operator. Impaired limbs may have altered skin conductance, spasticity, or sensory changes that shift the safe intensity range.
- IRB approval is required before any testing on actual patients.

## References

- [docs/hardware_day.md](hardware_day.md) — full setup and session procedure.
- [../hardware/calibration_log.md](../hardware/calibration_log.md) — per-session record template.
- [docs/known_issues.md](known_issues.md) — accepted constraints and tracked issues.
- [../firmware/README.md](../firmware/README.md) — firmware v2.0 protocol and behavior.
