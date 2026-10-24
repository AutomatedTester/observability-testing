# Observable Web App + Trace-Propagating Selenium Tests

A small demo showing two things together:

1. A web app instrumented with **OpenTelemetry** (traces, metrics, logs) so you
   can see how people use it.
2. A **Selenium** test suite that hooks into that observability by injecting a
   W3C `traceparent` into the browser's requests **via Selenium BiDi**, so a
   single user action is one end-to-end trace: **test → browser → backend**.
   The browser itself is instrumented too (OTel web SDK), so frontend spans
   sit in the middle of that trace.

```
Selenium test (root span per action)
      │  BiDi network intercept adds `traceparent` to browser requests
      ▼
   Chrome ──fetch/navigate──▶ Flask backend (auto + manual OTel spans)
      │                              │
      │                              ├─ traces  ─▶ Jaeger  (UI :16686)
      │                              ├─ metrics ─▶ console (stdout)
      │                              └─ logs    ─▶ console (trace-correlated)
      │
      └─ test spans ─▶ Jaeger  (same trace id → nested under the test)
```

The app is a tiny in-memory to-do list (create / complete / delete) — just
enough real user actions to observe.

## Layout

```
app/
  main.py            Flask routes (auto-instrumented) + manual spans/metrics/logs
  failing_service.py Second service (inventory-service, :5001) that always returns HTTP 500
  telemetry.py       OTel wiring: traces→Jaeger, metrics→console, logs→console
  templates/index.html   UI with stable data-testid hooks; fetch() calls wrapped in user-action spans
  static/otel.js     BUILT browser OTel bundle (committed; do not edit, see frontend/)
frontend/
  otel.js            Browser OTel source: web SDK, fetch instrumentation, userAction()
  package.json       `npm install && npm run build` -> app/static/otel.js
tests/
  conftest.py        BiDi-enabled Chrome driver + test-side OTel ("selenium-tests")
  bidi_trace.py      TraceparentInjector: BiDi network intercept that injects traceparent
  jaeger.py          Tiny Jaeger query-API client for asserting on traces
  test_task_flow.py  End-to-end scenario (one span per user action) + a trace-based test
scripts/
  run_jaeger.sh      Downloads + runs the Jaeger all-in-one native binary (no Docker)
  run_app.sh         Runs the Flask app
  run_failing_service.sh  Runs the always-500 inventory-service on :5001
```

## Prerequisites

- Python 3.9+ and Google Chrome (chromedriver is fetched automatically by
  Selenium Manager on first run).
- Network access on first run (to download Jaeger + chromedriver).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run it (three terminals)

```bash
# 1. Jaeger (traces UI at http://localhost:16686, OTLP in on :4318)
./scripts/run_jaeger.sh

# 2. The app (http://127.0.0.1:5000)
./scripts/run_app.sh

# 3. The tests
.venv/bin/python -m pytest tests/ -v
```

Then open **http://localhost:16686**, pick the `selenium-tests` service, and
open the newest trace. You'll see one trace spanning both services:

```
[selenium-tests] test: full task lifecycle
  [selenium-tests] add task
    [todo-app-frontend] ui.add_task
      [todo-app-frontend] HTTP POST
        [todo-app] POST /api/tasks
          [todo-app] create_task
  ...
```

## Frontend telemetry

`frontend/otel.js` sets up the OpenTelemetry web SDK (service
`todo-app-frontend`) with fetch auto-instrumentation. Each click/submit runs in
a `ui.*` span (`userAction()`), and fetches made inside it are its children; the
browser sends its own `traceparent`, so the Flask spans nest under them.

- **Export path:** the browser posts OTLP to same-origin `/otel/v1/traces`, which
  Flask forwards to `OTEL_EXPORTER_OTLP_ENDPOINT`. No CORS config on Jaeger, and
  the route is excluded from server tracing.
- **Test linkage:** the server renders the current span as
  `<meta name="traceparent">` for the initial load, and the BiDi injector sets
  `window.__traceCarrier` before each test action. The injector leaves a
  browser-set `traceparent` alone (it only fills in requests that lack one).
- **Rebuilding:** after editing `frontend/otel.js`, run
  `cd frontend && npm install && npm run build`.
- **Why bundled, not a CDN:** CDN ESM builds load as dozens of modules (and
  duplicate `@opentelemetry/api`). Each request passes through the serialized BiDi
  intercept, which hung Selenium clicks. One same-origin file avoids that.
- The exporter is forced onto XHR (explicit headers) rather than `sendBeacon`.

Metrics (`tasks.created`, `http.server.duration`, …) and logs print to the
app's console; each log line carries the active `trace_id`, so you can pivot
from a log straight to its trace in Jaeger.

To watch the app in a real browser instead of headless: `HEADLESS=0 .venv/bin/pytest tests/ -v`.

## Trace-based assertions

`test_add_task_trace` goes one step further than the UI checks. After adding a
task it flushes the test's spans, fetches the whole trace from Jaeger's query
API (`GET /api/traces/{trace_id}`) and asserts on the journey itself:

```python
spans = jaeger.get_trace(trace_id, expect=["add task", "create_task"])
add = spans.child_of("add task")
assert add.has("POST /api/tasks")                              # shape
assert add.has("create_task", attrs={"task.id": ui_task_id})   # identity
assert all(s.status != "ERROR" for s in spans)                 # health
assert spans.op("create_task").duration_ms < 50                # budget
```

`get_trace` polls until the expected spans arrive, because the app exports
spans in batches every few seconds. This test needs Jaeger running.

When pasting a `trace_id` from the app's console logs into Jaeger's search,
drop the `0x` prefix.

## How the test hooks into observability

`tests/bidi_trace.py` registers a BiDi `network.beforeRequestSent` handler.
For every browser request it calls `continue_request(headers=...)`, adding a
`traceparent` derived from the test's currently-active OpenTelemetry span. The
Flask instrumentation extracts that header on the server side, so backend spans
become children of the test's span — the whole interaction is one trace.

## Two non-obvious gotchas (both handled in the code)

- **Navigate with BiDi, not `driver.get()`.** With a BiDi network intercept
  active, classic WebDriver navigation never receives its load signal and times
  out with *"Timed out receiving message from renderer"*. The test navigates via
  `driver.browsing_context.navigate(..., wait="complete")` instead.
- **Serialize `continue_request()`.** Selenium's Python BiDi websocket is not
  thread-safe, and each intercepted request runs its handler on its own thread.
  Concurrent `continue_request()` calls interleave `ws.send()` frames and can
  drop the connection (*"Connection to remote host was lost"*), leaving requests
  hung. `bidi_trace.py` guards the call with a process-wide lock.

## Seeing a 500 in Jaeger

`inventory-service` (`scripts/run_failing_service.sh`, port 5001) always fails
`GET /api/stock` with HTTP 500. Creating a task makes the todo-app call it (as the
`sync_inventory` span, `traceparent` forwarded) and swallow the error, so the UI
works and `test_full_task_lifecycle` passes. `test_server_error_trace` asserts the
UI succeeded *and* that the trace contains the failure (`GET /api/stock`,
`load_stock`, `sync_inventory` flagged with the red error icon, `http.status_code=500`,
and `load_stock` carrying the recorded exception event). `test_add_task_trace`'s
"no error spans" check now fails for the same reason: only the trace reveals the
hidden error. Run the service in a fourth terminal before the tests; the todo-app
finds it via `INVENTORY_URL` (default `http://127.0.0.1:5001`).

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OTLP/HTTP base for Jaeger |
| `JAEGER_QUERY_URL` | `http://localhost:16686` | Jaeger query API used by trace assertions |
| `APP_URL` | `http://127.0.0.1:5000` | App URL the tests drive |
| `HEADLESS` | `1` | Set `0` to watch Chrome |
| `JAEGER_VERSION` | latest | Pin a Jaeger release for `run_jaeger.sh` |
