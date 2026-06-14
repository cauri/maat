"""OpenTelemetry instrumentation → cat-cafe OTLP sink (#32).

Every LLM call through the provider seam emits a span (capability, model, token usage, and the
prompt/completion, truncated) to whatever ``OTEL_EXPORTER_OTLP_ENDPOINT`` names — cat-cafe at
http://localhost:4318 in dev (`make obs-up`). cat-cafe normalises the traces and runs the LLM
judges you define in its UI.

With no endpoint set this is a complete no-op: the pipeline runs identically and cat-cafe is
entirely optional, so the OpenTelemetry packages live in the ``obs`` extra, not the core deps.
"""

from __future__ import annotations

import atexit
import os
from contextlib import contextmanager

_MAX_ATTR = 4000  # cap captured prompt/completion text so spans stay small
_state: dict = {}


def _truncate(s: str) -> str:
    return s if len(s) <= _MAX_ATTR else s[:_MAX_ATTR] + "…[truncated]"


def _tracer():
    """Lazily build a tracer once. Returns None (a no-op) without an endpoint or OTel."""
    if "tracer" in _state:
        return _state["tracer"]
    tracer = None
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(
                resource=Resource.create(
                    {"service.namespace": "maat", "service.name": "maat-pipeline"}
                )
            )
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
            )
            trace.set_tracer_provider(provider)
            atexit.register(provider.shutdown)  # flush on exit — the agents are short-lived
            tracer = trace.get_tracer("maat")
        except Exception:  # OTel not installed / misconfigured -> stay a no-op
            tracer = None
    _state["tracer"] = tracer
    return tracer


@contextmanager
def llm_span(capability: str, model: str, prompt: str = ""):
    """Trace one LLM call (OTel GenAI semantic conventions). No-op without an OTLP endpoint.

    Yields the span (or None) so the caller can record the completion + token usage.
    """
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(f"llm.{capability}") as span:
        span.set_attribute("gen_ai.operation.name", capability)
        span.set_attribute("gen_ai.request.model", model)
        if prompt:
            span.set_attribute("gen_ai.prompt", _truncate(prompt))
        yield span


def record_completion(span, text: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
    """Record an LLM completion + token usage on a span (no-op if span is None)."""
    if span is None:
        return
    if text:
        span.set_attribute("gen_ai.completion", _truncate(text))
    if input_tokens:
        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    if output_tokens:
        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
