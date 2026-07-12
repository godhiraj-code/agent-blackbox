# The Agent That Autopsies Itself

A deterministic, standard-library incident lab. A real optimistic-concurrency API rejects stale version `6` with HTTP 409 while the deliberately buggy first-pass UI displays **Saved**. The replay checks `response.ok`, refreshes version `7`, retries once, and independently reads backend state.

## Run (Windows, Python 3.11+)

```powershell
cd examples/sol-self-autopsy
python server.py
```

Open <http://127.0.0.1:8765>, press **Run Autopsy** (or Space). Controls: `R` restart, `→` advance in step mode, `P` pause/resume, Space start. The animation is deterministic; API calls and persisted in-memory server state are real. No network service, package install, or telemetry is used.

## Verify

```powershell
cd examples/sol-self-autopsy
python verify.py
python -m py_compile server.py verify.py
```

The verifier starts an ephemeral loopback server and asserts the genuine 409, unchanged backend, refresh/retry success, persisted value, and required trace evidence. It overwrites `run-trace.jsonl` with a sanitized normalized run. While the visual server is running, inspect truth directly:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/state
Invoke-RestMethod http://127.0.0.1:8765/api/trace
```

## Record

Capture a 1920×1080 browser window at 100% zoom, muted. Reload, begin recording, and press Space. Autoplay runs roughly 14 seconds of deliberately time-compressed evidence; pause on the diagnosis or use Arrow Right for presenter-paced stepping. Follow [VIDEO_SCRIPT.md](VIDEO_SCRIPT.md) for the 60–75 second edit. Keep the real `/api/state` proof visible in the browser/devtools cutaway.

`sample-trace.jsonl` is a checked-in sanitized reference. `run-trace.jsonl` is the most recent executable verification/browser run.

