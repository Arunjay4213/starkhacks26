# Sinew Hardware Bridge

Flask process that owns the USB serial port to the Arduino firmware. Listens on `127.0.0.1:5001`. Everything above this layer talks to the hardware through HTTP, never by opening the serial port directly.

## Running

```
cd hardware
pip install flask pyserial
python receiver.py
```

Override port detection with an env var if auto-detect picks the wrong device:

```
SINEW_SERIAL_PORT=/dev/ttyACM0 python receiver.py
```

Logs land in `hardware/logs/receiver.log` (rotating, 5 MB per file, 3 backups) and also echo to stderr.

## Manual testing with curl

Health check. Always returns 200 even when the serial port is down.

```
curl -s http://127.0.0.1:5001/health
```

Status snapshot. Shows serial connection state, last ACK seen from firmware, and the watchdog countdown in milliseconds.

```
curl -s http://127.0.0.1:5001/status
```

Fire the index finger relay for 500 ms. The bridge sends `FINGER:INDEX:ON`, sleeps, then sends `FINGER:INDEX:OFF`.

```
curl -s -X POST http://127.0.0.1:5001/stimulate \
  -H 'Content-Type: application/json' \
  -d '{"finger": "INDEX", "action": "ON", "duration_ms": 500}'
```

Latch a relay on without an auto-off. The firmware's 2 second per finger cap will turn it off if nothing else does.

```
curl -s -X POST http://127.0.0.1:5001/stimulate \
  -H 'Content-Type: application/json' \
  -d '{"finger": "MIDDLE", "action": "ON"}'
```

Turn it off explicitly.

```
curl -s -X POST http://127.0.0.1:5001/stimulate \
  -H 'Content-Type: application/json' \
  -d '{"finger": "MIDDLE", "action": "OFF"}'
```

Emergency stop. Sends `ALL:OFF` to the firmware.

```
curl -s -X POST http://127.0.0.1:5001/stop -H 'Content-Type: application/json' -d '{}'
```

Bad input gets 400 with a reason.

```
curl -s -i -X POST http://127.0.0.1:5001/stimulate \
  -H 'Content-Type: application/json' \
  -d '{"finger": "THUMB", "action": "ON"}'
```

A serial drop returns 503. To test it, unplug the Arduino after `python receiver.py` has connected, then hit `/stimulate`. The bridge will try to reconnect once. If the Arduino comes back, the retry succeeds. If not, 503.

## What this layer enforces

The firmware is the authoritative safety layer. This bridge adds:

- `duration_ms` capped at 1000 at the HTTP boundary.
- Strict enum check on `finger` and `action`. Unknown values return 400.
- One lock serializes every write and its ACK read so two requests cannot interleave commands.
- Auto reconnect on serial drop, retried once per request.

The bridge does NOT implement the watchdog itself. It only reports the watchdog countdown based on local send timestamps. The firmware runs its own 3 second watchdog and will force all relays OFF if this bridge ever goes silent.
