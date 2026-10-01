"""A tiny, fully-instrumented task-list web app.

The point of the app is to produce real, observable user actions:
create / complete / delete a task. Every request is auto-traced by the
Flask instrumentation (which also extracts an incoming W3C `traceparent`,
so browser/test-initiated traces continue here), plus we add manual spans,
metrics and correlated logs.
"""

import time
from itertools import count

from flask import Flask, jsonify, render_template, request
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.trace import SpanKind

from app import telemetry
from app.telemetry import log, setup_telemetry

setup_telemetry()
tracer = telemetry.tracer  # bound after setup_telemetry()

app = Flask(__name__)
# Auto-instrument Flask: server spans + W3C context extraction from headers.
FlaskInstrumentor().instrument_app(app)

# In-memory "database" (demo only).
_ids = count(1)
_tasks = {}


def _record_duration(route: str, start: float) -> None:
    telemetry.request_duration.record(
        (time.time() - start) * 1000.0, {"route": route}
    )


@app.route("/")
def index():
    return render_template("index.html")


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
