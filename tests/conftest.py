"""Pytest fixtures: test-side OpenTelemetry + a BiDi-enabled Chrome driver.

The test process is itself an OTel service ("selenium-tests") that exports its
spans to the same Jaeger instance as the app. Because the test injects its
active span's traceparent into the browser's requests, the app's server spans
nest under the test spans and the whole interaction shows up in Jaeger as one
end-to-end trace: selenium-tests -> browser request -> todo-app.
"""

import os

import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from tests.bidi_trace import TraceparentInjector

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:5000")
OTLP_BASE = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
HEADLESS = os.environ.get("HEADLESS", "1") != "0"


@pytest.fixture(scope="session")
def tracer():
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: "selenium-tests"}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP_BASE}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    yield trace.get_tracer("selenium-tests")
    provider.force_flush()
    provider.shutdown()


@pytest.fixture
def driver():
    options = Options()
    options.enable_bidi = True  # required for driver.network (BiDi websocket)
    if HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    drv = webdriver.Chrome(options=options)
    drv.set_page_load_timeout(30)
    yield drv
    drv.quit()


@pytest.fixture
def injector(driver):
    inj = TraceparentInjector(driver).start()
    yield inj
    inj.stop()


@pytest.fixture
def app_url():
    return APP_URL
