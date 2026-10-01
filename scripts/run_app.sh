#!/usr/bin/env bash
# Run the Flask app with OpenTelemetry wired up.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  echo "No .venv found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4318}"
echo "Starting app on http://127.0.0.1:5000 (traces -> $OTEL_EXPORTER_OTLP_ENDPOINT)" >&2
exec .venv/bin/python -m app.main
