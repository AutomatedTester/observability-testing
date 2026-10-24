"""OpenTelemetry setup for the demo app.

Signals:
  - traces  -> OTLP/HTTP  -> Jaeger  (view at http://localhost:16686)
  - metrics -> console    (periodic export to stdout)
  - logs    -> console    (OTel log records, correlated with the active trace)

Endpoints are configurable via OTEL_EXPORTER_OTLP_ENDPOINT (default
http://localhost:4318, the Jaeger OTLP/HTTP receiver).
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    ConsoleLogExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry._logs import set_logger_provider

SERVICE = "todo-app"

# Base OTLP endpoint (no signal path); we append the signal-specific path below.
_OTLP_BASE = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")

OTLP_BASE = _OTLP_BASE

_resource = Resource.create(
    {
        SERVICE_NAME: SERVICE,
        SERVICE_VERSION: "0.1.0",
        "deployment.environment": os.environ.get("DEPLOY_ENV", "demo"),
    }
)

# Module-level instruments, populated by setup_telemetry().
tracer = None
tasks_created_counter = None
tasks_completed_counter = None
tasks_deleted_counter = None
request_duration = None
log = logging.getLogger("todo-app")


def setup_telemetry() -> None:
    """Wire up the three signals. Call once at process start (idempotent-ish)."""
    global tracer, tasks_created_counter, tasks_completed_counter
    global tasks_deleted_counter, request_duration

    # ---- Traces -> Jaeger via OTLP/HTTP ----
    tracer_provider = TracerProvider(resource=_resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{_OTLP_BASE}/v1/traces"))
    )
    trace.set_tracer_provider(tracer_provider)
    tracer = trace.get_tracer(SERVICE)

    # ---- Metrics -> console ----
    metric_reader = PeriodicExportingMetricReader(
        ConsoleMetricExporter(),
        export_interval_millis=5000,
    )
    meter_provider = MeterProvider(resource=_resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    meter = metrics.get_meter(SERVICE)

    tasks_created_counter = meter.create_counter(
        "tasks.created", unit="1", description="Number of tasks created"
    )
    tasks_completed_counter = meter.create_counter(
        "tasks.completed", unit="1", description="Number of tasks completed"
    )
    tasks_deleted_counter = meter.create_counter(
        "tasks.deleted", unit="1", description="Number of tasks deleted"
    )
    request_duration = meter.create_histogram(
        "http.server.duration", unit="ms", description="Request handler duration"
    )

    # ---- Logs -> console (correlated with active trace) ----
    logger_provider = LoggerProvider(resource=_resource)
    logger_provider.add_log_record_processor(
        SimpleLogRecordProcessor(ConsoleLogExporter())
    )
    set_logger_provider(logger_provider)

    otel_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    log.setLevel(logging.INFO)
    log.addHandler(otel_handler)
    # Also keep human-readable logs on the console.
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(stream)
    log.propagate = False
