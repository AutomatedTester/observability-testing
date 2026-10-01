"""End-to-end Selenium test that drives the app while propagating trace context.

Each user action is wrapped in a ``traced_action`` span. Inside the block the
injector is pointed at that span, so every request the browser makes during the
action carries the span's ``traceparent`` -- and the app's server spans become
children of the test span in Jaeger.
"""

import time
import uuid
from contextlib import contextmanager

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from opentelemetry import trace

from tests import jaeger

# The app rebuilds the whole task list on every reload, so element handles go
# stale between lookups; tolerate that everywhere we poll or act on a row.
STALE = (StaleElementReferenceException,)


@contextmanager
def traced_action(tracer, injector, name):
    with tracer.start_as_current_span(name) as span:
        injector.set_context()  # capture this span's traceparent for the browser
        try:
            yield span
        finally:
            injector.set_context({})  # stop injecting once the action is done


def _navigate(driver, url):
    # Navigate via BiDi, not classic driver.get(): with a BiDi network intercept
    # active, classic navigation never receives its load signal and times out.
    driver.browsing_context.navigate(driver.current_window_handle, url, wait="complete")


def _css(testid):
    return (By.CSS_SELECTOR, f'[data-testid="{testid}"]')


def _task_titles(driver):
    return [
        el.text
        for el in driver.find_elements(*_css("task-title-text"))
    ]


def _task_row(driver, title):
    """Return the <li> whose title matches, or None. Scopes actions to one task."""
    for li in driver.find_elements(By.CSS_SELECTOR, 'li[data-testid="task"]'):
        try:
            if li.find_element(*_css("task-title-text")).text == title:
                return li
        except StaleElementReferenceException:
            continue
    return None


def _click_in_row(driver, title, testid):
    """Click a button within a task's row, retrying if the list re-renders."""
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            row = _task_row(driver, title)
            if row is not None:
                row.find_element(*_css(testid)).click()
                return
        except StaleElementReferenceException:
            pass
        time.sleep(0.1)
    raise AssertionError(f"could not click {testid} in row {title!r}")


def _row_class(driver, title):
    row = _task_row(driver, title)
    return row.get_attribute("class") if row is not None else ""


def test_full_task_lifecycle(driver, injector, tracer, app_url):
    wait = WebDriverWait(driver, 10, ignored_exceptions=STALE)
    # Unique title so the test is re-runnable without restarting the app.
    title = f"Ship the demo {uuid.uuid4().hex[:8]}"

    with tracer.start_as_current_span("test: full task lifecycle"):
        # Load the page (also traced/injected).
        with traced_action(tracer, injector, "open app"):
            _navigate(driver, app_url)
            wait.until(EC.presence_of_element_located(_css("task-title")))

        # Create a task.
        with traced_action(tracer, injector, "add task"):
            driver.find_element(*_css("task-title")).send_keys(title)
            driver.find_element(*_css("add-task")).click()
            wait.until(lambda d: title in _task_titles(d))

        assert title in _task_titles(driver)

        # Complete it (scoped to this task's row).
        with traced_action(tracer, injector, "complete task"):
            _click_in_row(driver, title, "complete-task")
            wait.until(lambda d: "done" in _row_class(d, title))

        assert "done" in _row_class(driver, title)

        # Delete it (scoped to this task's row).
        with traced_action(tracer, injector, "delete task"):
            _click_in_row(driver, title, "delete-task")
            wait.until(lambda d: title not in _task_titles(d))

        assert title not in _task_titles(driver)


def test_add_task_trace(driver, injector, tracer, app_url):
    """Trace-based test: assert on the journey the backend took, not just the UI."""
    wait = WebDriverWait(driver, 10, ignored_exceptions=STALE)
    title = f"Trace me {uuid.uuid4().hex[:8]}"

    with tracer.start_as_current_span("test: add task trace") as root:
        trace_id = format(root.get_span_context().trace_id, "032x")

        with traced_action(tracer, injector, "open app"):
            _navigate(driver, app_url)
            wait.until(EC.presence_of_element_located(_css("task-title")))

        with traced_action(tracer, injector, "add task"):
            driver.find_element(*_css("task-title")).send_keys(title)
            driver.find_element(*_css("add-task")).click()
            wait.until(lambda d: title in _task_titles(d))

        ui_task_id = int(_task_row(driver, title).get_attribute("data-task-id"))

    # Test spans are batched too: push them to Jaeger before querying.
    trace.get_tracer_provider().force_flush()
    spans = jaeger.get_trace(trace_id, expect=["add task", "create_task"])

    # Shape: the click reached the handler, exactly once, under the test step.
    add = spans.child_of("add task")
    assert add.has("POST /api/tasks")
    assert add.count("create_task") == 1
    # Identity: the id on the span is the id the UI shows.
    assert add.has("create_task", attrs={"task.id": ui_task_id})
    # Health: no error spans anywhere in the journey.
    assert all(s.status != "ERROR" for s in spans), [s for s in spans if s.status == "ERROR"]
    # Budget: the backend work stayed fast.
    assert spans.op("create_task").duration_ms < 50
