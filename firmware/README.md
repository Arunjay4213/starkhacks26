# Sinew EMS Firmware

Arduino Micro sketch that drives three relays gating the positive output of a Belifu EMS unit into the dorsal-hand electrodes for index, middle, and pinky. This is the lowest safety layer of the Sinew stack. Everything above it (Flask bridge, brain, voice) talks to it over USB serial.

## Safety is layered

1. **Physical kill switch** inline with the EMS positive lead. This is the primary. If anything feels wrong, throw it.
2. **This firmware** enforces a channel mutex, a 3 second serial watchdog, and a 2 second per-finger cap.
3. **Flask bridge** above this adds its own watchdog and a 1000 ms validator on incoming commands.
4. **Brain / validator** caps commands per sequence and requires acknowledgement.

Every layer assumes the layers above and below may fail. Never remove a check because another layer already does it.

## Pinout

| Finger | Arduino Pin | Relay IN |
|--------|-------------|----------|
| Index  | D4          | relay 1  |
| Middle | D5          | relay 2  |
| Pinky  | D6          | relay 3  |

Relay modules are assumed **active-LOW**. Driving the IN pin LOW energizes the coil and closes the EMS channel. If your relay board is active-HIGH, flip `RELAY_ON` and `RELAY_OFF` at the top of the sketch.

Shared ground electrode sits on the wrist. The Arduino's ground is **not** connected to the EMS ground. The relay contacts are the only electrical path between the two systems.

## Flashing

1. Install the Arduino IDE (2.x or 1.8.x both fine).
2. Tools menu: Board set to **Arduino Micro**. Port set to whatever your OS assigns when you plug it in (often `/dev/ttyACM0` on Linux, `COMx` on Windows).
3. Open `sinew_ems/sinew_ems.ino`.
4. Click Upload. The sketch compiles and flashes in a few seconds.

Before flashing, unplug the EMS unit from the relay board. Flash with the relays disconnected from any live stim source. Only reconnect the EMS output after you have tested the relays click correctly on bench power.

## Testing with the Serial Monitor

1. Open Tools -> Serial Monitor.
2. Set baud rate to **115200** in the bottom-right dropdown.
3. Set line ending to **Newline** (not "No line ending", not "Both NL and CR"). The firmware tolerates CRLF but Newline is cleanest.
4. You should see `READY` within a second or two of opening the monitor. If the board reset when the monitor opened (Arduino Micro does on some hosts), you may need to reopen it.

### Handshake

Type:

```
PING
```

Expect:

```
PONG
```

### Single-finger toggle

```
FINGER:INDEX:ON
```

Expect the index relay to click ON (LED on the relay module lights up), and the monitor prints:

```
OK:FINGER:INDEX:ON
```

Within 2 seconds, without any further command, the firmware will auto-OFF the relay and print:

```
TIMEOUT:INDEX
```

That is the per-finger cap working. Good.

If you send `FINGER:INDEX:OFF` before the 2 second cap, you get `OK:FINGER:INDEX:OFF` and no TIMEOUT.

### Mutex

```
FINGER:INDEX:ON
FINGER:MIDDLE:ON
```

You should hear index click ON, then MIDDLE click ON and INDEX click OFF in the same instant. Only one relay is ever closed at a time. This is enforced on the `ON` path, before the target relay is energized.

### Watchdog

Send any valid command, then stop typing. After 3 seconds of silence you will see:

```
WATCHDOG
```

And any relay that was still ON will be forced OFF. The watchdog latches, so you only get one `WATCHDOG` message per silent period. Send any valid command to re-arm.

### All-off

```
ALL:OFF
```

Forces every relay OFF regardless of prior state. Always safe to send.

### Bad commands

```
FINGER:THUMB:ON    -> ERR:unknown_finger
FINGER:INDEX:MAYBE -> ERR:unknown_action
HELLO              -> ERR:unknown_command
```

No state change on any error response.

## Bench checklist before going near a person

- Confirm `READY` on boot.
- Confirm `PONG` round-trips.
- Confirm each relay clicks ON and OFF independently.
- Confirm the mutex by sending two `ON` commands back to back.
- Confirm the 2 second per-finger cap fires on a lone `ON`.
- Confirm the 3 second watchdog fires when the host goes silent.
- Confirm a fresh power cycle boots with every relay OFF (watch the relay module LEDs during plug-in).
- Confirm the physical kill switch opens the positive lead. Verify with a multimeter before connecting to skin.
