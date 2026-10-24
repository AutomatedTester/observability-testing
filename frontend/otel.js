// Browser-side OpenTelemetry for the to-do app.
//
// - Traces go to same-origin /otel/v1/traces (Flask forwards to the OTLP
//   endpoint), so no CORS configuration is needed on Jaeger.
// - fetch() is auto-instrumented: each API call becomes a client span and the
//   browser sends its own W3C `traceparent`, so backend spans nest under it.
// - `userAction()` wraps a user interaction in a span that is the parent of the
//   fetches made inside it.
// - The root parent comes from, in order: `window.__traceCarrier` (set by the
//   Selenium test before each action) or the `<meta name="traceparent">` tag
//   the server renders (links the initial page load to the document request).
import { context, propagation, trace, ROOT_CONTEXT, SpanStatusCode } from "@opentelemetry/api";
import { WebTracerProvider, BatchSpanProcessor, StackContextManager } from "@opentelemetry/sdk-trace-web";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { FetchInstrumentation } from "@opentelemetry/instrumentation-fetch";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { Resource } from "@opentelemetry/resources";

const OTLP_PATH = "/otel/v1/traces";

const provider = new WebTracerProvider({
  resource: new Resource({
    "service.name": "todo-app-frontend",
    "service.version": "0.1.0",
  }),
  spanProcessors: [
    // Explicit headers make the exporter use XHR instead of sendBeacon, which
    // does not cooperate with Selenium's BiDi request interception.
    new BatchSpanProcessor(new OTLPTraceExporter({
      url: new URL(OTLP_PATH, window.location.origin).href,
      headers: { "Content-Type": "application/json" },
    }), {
      scheduledDelayMillis: 1000,
    }),
  ],
});
provider.register({ contextManager: new StackContextManager() });

registerInstrumentations({
  instrumentations: [
    new FetchInstrumentation({ ignoreUrls: [/\/otel\//] }),
  ],
});

// Flush buffered spans when the tab is hidden or closed.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") provider.forceFlush();
});

const tracer = trace.getTracer("todo-app-frontend");

function documentContext() {
  const meta = document.querySelector('meta[name="traceparent"]');
  const carrier = meta && meta.content ? { traceparent: meta.content } : {};
  return propagation.extract(ROOT_CONTEXT, carrier);
}

const initialContext = documentContext();

function parentContext(first) {
  const carrier = window.__traceCarrier;
  if (carrier && carrier.traceparent) return propagation.extract(ROOT_CONTEXT, carrier);
  return first ? initialContext : ROOT_CONTEXT;
}

/**
 * Run `fn(ctx)` inside a span named `name`. Pass `ctx` to `withContext()`
 * around any fetch() so the request is parented to this span.
 */
export async function userAction(name, attributes, fn, { initial = false } = {}) {
  const parent = parentContext(initial);
  const span = tracer.startSpan(name, { attributes }, parent);
  const ctx = trace.setSpan(parent, span);
  try {
    return await fn(ctx);
  } catch (err) {
    span.recordException(err);
    span.setStatus({ code: SpanStatusCode.ERROR, message: String(err && err.message) });
    throw err;
  } finally {
    span.end();
  }
}

export function withContext(ctx, fn) {
  return context.with(ctx, fn);
}
