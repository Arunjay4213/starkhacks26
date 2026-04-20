# Tests

## Mock receiver

Simulates the hardware bridge on port 5001. Use this to develop and test the app stack without the Pi or Arduino.

    python tests/mock_receiver.py

Logs every request to stdout and returns `{"status": "ok"}` for writes. It never touches serial or hardware. Tests import `create_mock_app()` directly for in-process use.

## Brain evaluation harness

Runs 8 test cases against the real Claude API to validate the system prompt.

    python tests/test_brain.py

Set `ANTHROPIC_API_KEY` in the environment or in `.env` before running. Results print to stdout. Iterate on `prompts/system_prompt.txt` until all 8 pass consistently across 3 runs.

## Fixtures

`tests/fixtures/` contains six JPEGs used by the evaluation harness. Real photos since 2026-04-17. Placeholder originals are archived in `tests/fixtures/_placeholder_backup/` for historical comparison.

Run `python tests/fixtures/validate_fixtures.py` before running `test_brain.py` to confirm size, JPEG magic, and pixel variance pass.

## Testing streaming endpoints

Flask's test client eagerly consumes streaming responses into `response.data`, which hides disconnect-cleanup bugs. For any endpoint that returns a generator-backed `Response`, drive the WSGI app directly:

```python
from werkzeug.test import EnvironBuilder

def _wsgi_iter(app, path: str, method: str = "GET"):
    env = EnvironBuilder(method=method, path=path).get_environ()
    state = {}
    def start_response(status, headers, exc_info=None):
        state["status"] = status
        state["headers"] = dict(headers)
    return app.wsgi_app(env, start_response), state
```

Drive iteration from a background thread and push into whatever queue the generator pulls from on the main thread. Call `app_iter.close()` to trigger the generator's `finally` block, then assert the cleanup side effects (unregister, listener count back to zero, etc).

See `tests/test_pi_audio_service.py` for working examples. The listener-leak bug we caught there was invisible through Flask's buffered test client.

## Non-API suite

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ \
  --ignore=tests/test_brain.py \
  --ignore=tests/test_integration_mock.py \
  --ignore=tests/test_orchestrator_integration.py \
  --ignore=tests/measure_stop_latency.py \
  -q
```

Local, no API calls, no hardware. The three ignored harness scripts either hit Claude (cost money) or measure latency against a running service.
