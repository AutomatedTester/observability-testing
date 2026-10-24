"""A tiny, fully-instrumented task-list web app.

The point of the app is to produce real, observable user actions:
create / complete / delete a task. Every request is auto-traced by the
Flask instrumentation (which also extracts an incoming W3C `traceparent`,
so browser/test-initiated traces continue here), plus we add manual spans,
metrics and correlated logs.
"""

import os
import time
import urllib.error
import urllib.request
from itertools import count

from flask import Flask, Response, jsonify, render_template, request
from opentelemetry import propagate
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.trace import SpanKind, Status, StatusCode

from app import telemetry
from app.telemetry import log, setup_telemetry

setup_telemetry()
tracer = telemetry.tracer  # bound after setup_telemetry()

app = Flask(__name__)
# Auto-instrument Flask: server spans + W3C context extraction from headers.
# The browser's OTLP proxy is excluded so exporting spans doesn't create spans.
FlaskInstrumentor().instrument_app(app, excluded_urls="otel/v1")

INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://127.0.0.1:5001")

# In-memory "database" (demo only).
_ids = count(1)
_tasks = {}


def _record_duration(route: str, start: float) -> None:
    telemetry.request_duration.record(
        (time.time() - start) * 1000.0, {"route": route}
    )


@app.route("/")
def index():
    # Hand the server span's context to the page so browser spans from the
    # initial load join the same trace as the document request.
    carrier = {}
    propagate.inject(carrier)
    return render_template("index.html", traceparent=carrier.get("traceparent", ""))


@app.post("/otel/v1/traces")
def proxy_browser_traces():
    """Forward browser OTLP/HTTP trace exports to the collector (same-origin, no CORS)."""
    upstream = urllib.request.Request(
        f"{telemetry.OTLP_BASE}/v1/traces",
        data=request.get_data(),
        headers={"Content-Type": request.content_type or "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(upstream, timeout=5) as resp:
            return Response(resp.read(), status=resp.status,
                            content_type=resp.headers.get("Content-Type"))
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code, content_type=e.headers.get("Content-Type"))
    except (urllib.error.URLError, TimeoutError):
        return Response(status=502)


@app.get("/api/sync")
def sync_inventory():
    """Call the (always failing) inventory-service and pass its 500 through."""
    start = time.time()
    with tracer.start_as_current_span("sync_inventory", kind=SpanKind.CLIENT) as span:
        headers = {}
        propagate.inject(headers)  # carry this span's traceparent downstream
        upstream = urllib.request.Request(f"{INVENTORY_URL}/api/stock", headers=headers)
        try:
            with urllib.request.urlopen(upstream, timeout=5) as resp:
                span.set_attribute("http.status_code", resp.status)
                _record_duration("sync_inventory", start)
                return Response(resp.read(), status=resp.status, content_type="application/json")
        except urllib.error.HTTPError as e:
            span.set_attribute("http.status_code", e.code)
            span.set_status(Status(StatusCode.ERROR, f"inventory-service returned {e.code}"))
            log.error("inventory-service returned %d", e.code)
            _record_duration("sync_inventory", start)
            return Response(e.read(), status=e.code, content_type="application/json")
        except (urllib.error.URLError, TimeoutError) as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, "inventory-service unreachable"))
            log.error("inventory-service unreachable: %s", e)
            _record_duration("sync_inventory", start)
            return jsonify({"error": "inventory-service unreachable"}), 502


@app.get("/api/tasks")
def list_tasks():
    start = time.time()
    tasks = sorted(_tasks.values(), key=lambda t: t["id"])
    log.info("Listed %d tasks", len(tasks))
    _record_duration("list_tasks", start)
    return jsonify(tasks)


@app.post("/api/tasks")
def create_task():
    start = time.time()
    with tracer.start_as_current_span("create_task", kind=SpanKind.INTERNAL) as span:
        payload = request.get_json(silent=True) or {}
        title = (payload.get("title") or "").strip()
        if not title:
            span.set_attribute("task.valid", False)
            log.warning("Rejected task with empty title")
            _record_duration("create_task", start)
            return jsonify({"error": "title required"}), 400

        task = {"id": next(_ids), "title": title, "done": False}
        _tasks[task["id"]] = task
        span.set_attribute("task.id", task["id"])
        span.set_attribute("task.title", title)
        telemetry.tasks_created_counter.add(1)
        log.info("Created task %d: %s", task["id"], title)
        _record_duration("create_task", start)
        return jsonify(task), 201


@app.post("/api/tasks/<int:task_id>/complete")
def complete_task(task_id: int):
    start = time.time()
    with tracer.start_as_current_span("complete_task", kind=SpanKind.INTERNAL) as span:
        span.set_attribute("task.id", task_id)
        task = _tasks.get(task_id)
        if not task:
            log.warning("Complete failed, task %d not found", task_id)
            _record_duration("complete_task", start)
            return jsonify({"error": "not found"}), 404
        task["done"] = True
        telemetry.tasks_completed_counter.add(1)
        log.info("Completed task %d", task_id)
        _record_duration("complete_task", start)
        return jsonify(task)


@app.delete("/api/tasks/<int:task_id>")
def delete_task(task_id: int):
    start = time.time()
    with tracer.start_as_current_span("delete_task", kind=SpanKind.INTERNAL) as span:
        span.set_attribute("task.id", task_id)
        task = _tasks.pop(task_id, None)
        if not task:
            log.warning("Delete failed, task %d not found", task_id)
            _record_duration("delete_task", start)
            return jsonify({"error": "not found"}), 404
        telemetry.tasks_deleted_counter.add(1)
        log.info("Deleted task %d", task_id)
        _record_duration("delete_task", start)
        return jsonify({"status": "deleted", "id": task_id})


if __name__ == "__main__":
    # Threaded so a single browser session can make concurrent calls.
    app.run(host="127.0.0.1", port=5000, threaded=True)
