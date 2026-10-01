"""A tiny client for Jaeger's query API, for asserting on traces in tests.

Jaeger (v1 and v2) serves a finished trace as JSON at
``GET {JAEGER_QUERY_URL}/api/traces/{trace_id}``. This module fetches it and
wraps it in a small, readable API:

    spans = get_trace(trace_id, expect=["create_task"])
    add = spans.child_of("add task")          # everything under that span
    assert add.has("POST /api/tasks")
    assert add.has("create_task", attrs={"task.id": 7})
    assert all(s.status != "ERROR" for s in spans)
    assert spans.op("create_task").duration_ms < 50
"""

import json
import os
import time
import urllib.error
import urllib.request

JAEGER_QUERY_URL = os.environ.get("JAEGER_QUERY_URL", "http://localhost:16686").rstrip("/")


class Span:
    def __init__(self, raw, service):
        self.name = raw["operationName"]
        self.span_id = raw["spanID"]
        self.service = service
        self.parent_id = next(
            (r["spanID"] for r in raw.get("references", []) if r.get("refType") == "CHILD_OF"),
            None,
        )
        self.start_us = raw["startTime"]
        self.duration_ms = raw["duration"] / 1000.0
        self.attrs = {t["key"]: t["value"] for t in raw.get("tags", [])}
        # Jaeger maps the OTel span status onto the `otel.status_code` tag.
        self.status = self.attrs.get("otel.status_code", "UNSET")

    def __repr__(self):
        return f"<Span {self.service}:{self.name} {self.duration_ms:.1f}ms {self.status}>"


class Spans(list):
    """A list of spans with trace-shaped queries."""

    def named(self, name):
        return Spans(s for s in self if s.name == name)

    def op(self, name):
        """The single span with this name (fails loudly if there isn't exactly one)."""
        found = self.named(name)
        assert len(found) == 1, f"expected exactly one {name!r} span, got {len(found)}: {self}"
        return found[0]

    def has(self, name, attrs=None):
        """True if a span with this name (and, optionally, these attributes) exists."""
        return any(
            all(s.attrs.get(k) == v for k, v in (attrs or {}).items()) for s in self.named(name)
        )

    def child_of(self, name):
        """All descendants of the (single) span with this name."""
        root = self.op(name)
        children = {}
        for s in self:
            children.setdefault(s.parent_id, []).append(s)
        out, stack = Spans(), [root.span_id]
        while stack:
            for c in children.get(stack.pop(), []):
                out.append(c)
                stack.append(c.span_id)
        return out

    def count(self, name):
        return len(self.named(name))

    def order(self, *names):
        """True if the first span of each name starts in the given order."""
        starts = []
        for n in names:
            found = self.named(n)
            if not found:
                return False
            starts.append(min(s.start_us for s in found))
        return starts == sorted(starts)


def _fetch(trace_id):
    url = f"{JAEGER_QUERY_URL}/api/traces/{trace_id}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return Spans()
        raise
    spans = Spans()
    for t in body.get("data") or []:
        procs = {pid: p["serviceName"] for pid, p in t.get("processes", {}).items()}
        spans.extend(Span(s, procs.get(s.get("processID"), "?")) for s in t["spans"])
    return spans


def get_trace(trace_id, expect=(), timeout=20.0):
    """Fetch a trace, polling until every span name in ``expect`` has arrived.

    Spans are exported in batches (the app's BatchSpanProcessor flushes every
    few seconds), so a trace can be incomplete immediately after the test.
    """
    deadline = time.time() + timeout
    while True:
        spans = _fetch(trace_id)
        if spans and all(spans.has(n) for n in expect):
            return spans
        if time.time() > deadline:
            missing = [n for n in expect if not spans.has(n)]
            raise AssertionError(f"trace {trace_id} incomplete after {timeout}s; missing {missing}")
        time.sleep(0.5)
