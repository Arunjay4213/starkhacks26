# Sinew

Vision-guided assistive grasping prototype for upper motor neuron injury rehabilitation.
Voice-triggered, Claude-planned, EMS-actuated three-finger grip system.

## Architecture

```
Laptop (Person B)                    Pi (Person A)
─────────────────────────────        ──────────────────────────
app/main.py         ─ OpenCV UI      hardware/receiver.py :5001
app/voice.py        ─ faster-whisper hardware/frame_server.py :5002
app/brain.py        ─ Claude API     firmware/sinew_ems.ino
app/orchestrator.py ─ state machine  Arduino Micro → relays → EMS
app/session_logger.py               Belifu EMS → electrodes
                           │
               HTTP POST /stimulate
               HTTP GET  /frame
               ───────────────────
```

## Running the demo

### Step 1 — Start the mock receiver (no hardware needed)

```bash
python tests/mock_receiver.py
```

Logs every command to stdout. Swap for `python hardware/receiver.py` once the Pi is ready.

### Step 2 — Copy and fill in your API key

```bash
cp .env_empty .env
# edit .env and add your ANTHROPIC_API_KEY
```

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

faster-whisper will download the `tiny` and `base` models (~150MB total) on first run.

### Step 4 — Run

```bash
python app/main.py
```

A window opens showing the camera feed with an overlay.

### Step 5 — Trigger a grasp

Say: **"hey sinew, grab the cup"**

Or press SPACE and type the intent in the console.

Press S to stop. Press ESC to quit.

## Hotkeys

| Key | Action |
|-----|--------|
| SPACE | Manual trigger (prompts in console) |
| S | POST /stop, abort current execution |
| ESC | Abort and exit |

## Developing without hardware

1. Run `python tests/mock_receiver.py`
2. Replace real fixture photos per `tests/fixtures/TODO.md`
3. Run `python tests/test_brain.py` to validate the system prompt
4. Run `python app/main.py` — it falls back to your local webcam if the Pi is unreachable

## Testing the system prompt

```bash
python tests/test_brain.py
```

8 test cases covering all grip types, refusal paths, and low-confidence handling.
Iterate on `prompts/system_prompt.txt` until all 8 pass across 3 consecutive runs.

## Key files

| File | Purpose |
|------|---------|
| `app/state.py` | Shared enums and dataclasses — the contract between all modules |
| `app/brain.py` | Claude API call + response validator |
| `app/orchestrator.py` | State machine: IDLE → CAPTURING → PROCESSING → ACKNOWLEDGING → EXECUTING |
| `app/session_logger.py` | Rep tracking. `self_initiated_pct` is the recovery metric |
| `prompts/system_prompt.txt` | Claude's instruction — iterate this to improve grip accuracy |
| `tests/mock_receiver.py` | Full API mock — develop without Pi |
| `tests/test_brain.py` | 8-case prompt evaluation harness |

## The recovery metric

`self_initiated_pct` in session reports tracks what fraction of grips the patient
completed without EMS assistance. Trending upward over weeks means the motor pathway
is rebuilding. This is the clinical value of the system.
