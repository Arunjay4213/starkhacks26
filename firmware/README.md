# Monday EMS Firmware (v2.0)

Arduino Micro sketch that drives three relays gating the positive output of a Belifu EMS unit into the dorsal-hand electrodes for index, middle, and pinky. Uses direct PORTD manipulation for fast switching. Everything above it (Flask bridge, brain, voice) talks to it over USB serial.

## Safety is layered

1. **Physical kill switch** inline with the EMS positive lead. This is the primary. If anything feels wrong, throw it.
2. **This firmware** enforces a 5 second max-on cap. Any relay ON longer than that is forced OFF with an `AUTO-OFF` message. Long built-in sequences are interruptible by any incoming serial byte.
3. **Flask bridge** above this adds its own validation and a 1000 ms per-command duration cap.
4. **Brain / validator** caps commands per sequence and requires acknowledgement.

Every layer assumes the layers above and below may fail. Never remove a check because another layer already does it.

**Accepted safety trade-offs in v2.0** (documented in `docs/known_issues.md`):
- No channel mutex. Multiple fingers can be energized simultaneously. The receiver protocol still sends one at a time, but the firmware will honor `CHORD` or overlapping `FINGER:X:ON` calls if they arrive.
- Single 5 s max-on cap replaces the previous per-finger 2 s cap.
- No silence watchdog. If the host goes silent mid-pulse, the pulse runs until the 5 s cap fires.

## Pinout

| Finger | Arduino Pin | PORTD bit | Relay IN |
|--------|-------------|-----------|----------|
| Index  | D2          | 0x04      | relay 1  |
| Middle | D3          | 0x08      | relay 2  |
| Pinky  | D4          | 0x10      | relay 3  |

Relay modules are assumed **active-HIGH** at the port level. PORTD bit high drives the relay IN pin high, energizing the coil. If your relay board behaves inversely (most common cheap modules are active-LOW), flip the PORTD writes in the firmware (`PORTD |= ...` and `PORTD &= ~...`).

Shared ground electrode sits on the wrist. The Arduino's ground is **not** connected to the EMS ground. The relay contacts are the only electrical path between the two systems.

## Flashing

1. Install the Arduino IDE (2.x or 1.8.x both fine).
2. Tools menu: Board set to **Arduino Micro**. Port set to whatever your OS assigns (often `/dev/ttyACM0` on Linux, `COMx` on Windows).
3. Open `monday_ems/monday_ems.ino`.
4. Click Upload.

Before flashing, unplug the EMS unit from the relay board. Flash with the relays disconnected from any live stim source. Only reconnect the EMS output after you have tested the relays click correctly on bench power.

## Serial protocol

115200 baud, `\n`-terminated, auto-uppercased by the firmware.

### Receiver-protocol commands (used in production)

```
FINGER:INDEX:ON      -> OK
FINGER:MIDDLE:OFF    -> OK
FINGER:PINKY:ON      -> OK
ALL:OFF              -> OFF
```

The response is `OK` on success (not `OK:<original>` as in v1). On auto-off, `AUTO-OFF` arrives asynchronously.

### Legacy / debug commands

```
1 / 2 / 3     -> ON index / middle / pinky (single char)
4 / 5 / 6     -> OFF index / middle / pinky
ALL           -> all three ON
OFF           -> immediate all-off
```

### Built-in sequences (bonus, not wired through the receiver)

```
DANCE                 -> walks the three relays up and back
SEQ:f:d:f:d:...       -> custom sequence of (finger, duration ms) pairs
CHORD:mask:dur        -> simultaneous bitmask (1..7) for dur ms (max 2000)
PIANO:MARY            -> Mary Had a Little Lamb
PIANO:HOTCROSS        -> Hot Cross Buns
PIANO:SCALE           -> finger exercise
PIANO:TRILL           -> alternating exercise
PIANO:ARPEGGIO        -> chord exercise
SIGN:WORD:hold_ms     -> ASL fingerspelling (max 12 chars)
RAPID:f:reps:on:off   -> rapid pulse stress test
STRESS                -> full hardware stress test
```

All long sequences are **interruptible**: send any byte on the serial port to abort immediately.

## Testing with the Serial Monitor

1. Open Tools -> Serial Monitor.
2. Baud: **115200**. Line ending: **Newline**.
3. You should see `READY` within a second or two of opening the monitor. If the board reset when the monitor opened (Arduino Micro does on some hosts), reopen it.

### Handshake

There is no `PING`/`PONG` in v2.0. Use `ALL:OFF` as a no-op heartbeat; it always returns `OFF`.

### Single-finger toggle

```
FINGER:INDEX:ON
```

Expect the index relay to click ON (LED on the relay module lights up), monitor prints:

```
OK
```

Within 5 seconds, without any further command, the firmware auto-OFF fires:

```
AUTO-OFF
OFF
```

Send `FINGER:INDEX:OFF` before the 5 second cap and you get `OK` with no `AUTO-OFF`.

### All-off

```
ALL:OFF
```

Forces every relay OFF. Always safe to send. Prints `OFF`.

### Bad commands

Anything unrecognized is silently ignored (no `ERR:` line) except for the specific error classes the handlers emit: `ERR:FINGER`, `ERR:ACTION`, `ERR:MASK`, `ERR:SONG`.

## Bench checklist before going near a person

- Confirm `READY` on boot.
- Confirm each relay clicks ON and OFF independently via `FINGER:X:ON` / `FINGER:X:OFF`.
- Confirm the 5 second max-on cap fires on a lone `ON`.
- Confirm a fresh power cycle boots with every relay OFF (watch the relay module LEDs during plug-in).
- Confirm the physical kill switch opens the positive lead. Verify with a multimeter before connecting to skin.
