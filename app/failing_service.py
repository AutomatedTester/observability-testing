"""A second service that always fails, to see how errors look in Jaeger.

Runs as its own OTel service ("inventory-service") on port 5001. The todo-app
calls it after every task is created, forwarding the W3C `traceparent`, so the failure
appears as a child of the todo-app span in the same trace.

Endpoints:
  GET /api/stock   always HTTP 500; manual span marked ERROR with the
                   exception recorded as a span event
  GET /health      200 OK (the one thing that works)
"""

import os

from flask import Flask, jsonify
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

SERVICE = "inventory-service"
OTLP_BASE = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")

provider = TracerProvider(resource=Resource.create({SERVICE_NAME: SERVICE}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP_BASE}/v1/traces")))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(SERVICE)

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/stock")
def stock():
    with tracer.start_as_current_span("load_stock") as span:
        span.set_attribute("stock.sku", "demo-sku")
        err = RuntimeError("inventory database connection refused")
        span.record_exception(err)  # shows up as an exception event in Jaeger
        span.set_status(trace.Status(trace.StatusCode.ERROR, "inventory db unavailable"))
        return jsonify({"error": "inventory db unavailable"}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, threaded=True)
