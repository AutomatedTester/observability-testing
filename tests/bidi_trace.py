"""Inject a W3C ``traceparent`` into browser requests using Selenium BiDi.

Classic WebDriver cannot set arbitrary request headers, so we use the BiDi
``network`` module: we intercept every request in the ``beforeRequestSent``
phase and continue it with a ``traceparent`` (and ``tracestate``) header
derived from the test's currently-active OpenTelemetry span.

Threading note: BiDi request callbacks run on the websocket receiver thread,
which does *not* share the test thread's contextvar-based OTel context. So we
don't read the active context inside the callback — instead the test thread
captures its context into a shared carrier (``set_context``) and the callback
just applies whatever is currently there.
"""

import threading

from opentelemetry import propagate

# Selenium's BiDi websocket (WebSocketConnection) is not thread-safe: each
# intercepted request is dispatched on its own thread, and concurrent
# continue_request() calls interleave ws.send() frames and can drop the
# connection. Serialize all continue_request() calls across the process.
_SEND_LOCK = threading.Lock()


class TraceparentInjector:
    def __init__(self, driver):
        self._driver = driver
        self._carrier = {}
        self._lock = threading.Lock()
        self._callback_id = None

    def start(self):
        """Register the BiDi request handler (intercepts all requests)."""
        self._callback_id = self._driver.network.add_request_handler(
            "before_request", self._handle_request
        )
        return self

    def stop(self):
        if self._callback_id is not None:
            self._driver.network.remove_request_handler("before_request", self._callback_id)
            self._callback_id = None

    def set_context(self, carrier=None):
        """Capture the trace context to inject onto subsequent requests.

        With no argument, captures the test thread's currently-active span.
        Pass an empty dict to stop injecting.
        """
        if carrier is None:
            carrier = {}
            propagate.inject(carrier)  # reads the active OTel context on THIS thread
        with self._lock:
            self._carrier = dict(carrier)
        # Also hand the context to the page: the frontend OTel SDK parents its
        # user-action spans on it (and sends its own traceparent on fetches).
        try:
            self._driver.execute_script("window.__traceCarrier = arguments[0];", dict(carrier))
        except Exception:
            pass  # no page loaded yet; the document request itself is still injected

    def _handle_request(self, request):
        # Must always continue the request, or the browser hangs on this intercept.
        try:
            with self._lock:
                carrier = dict(self._carrier)
            existing = {h.get("name", "").lower() for h in request.headers or []}
            if "traceparent" in existing:
                # The frontend SDK already set one (parented on our context):
                # leave it, or we'd orphan the browser spans from the trace.
                carrier = {}
            headers = list(request.headers or [])
            for name, value in carrier.items():
                headers.append({"name": name, "value": {"type": "string", "value": value}})
            with _SEND_LOCK:
                request.continue_request(headers=headers)
        except Exception:
            try:
                with _SEND_LOCK:
                    request.continue_request()
            except Exception:
                pass
