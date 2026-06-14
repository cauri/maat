"""Observability must be a true no-op without an OTLP endpoint — the pipeline runs identically."""

import maat.obs as obs
from maat.obs import llm_span, record_completion


def test_llm_span_is_noop_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    obs._state.clear()  # drop any cached tracer
    with llm_span("judge", "model-x", "a prompt") as span:
        assert span is None
        record_completion(span, "output", input_tokens=10, output_tokens=5)  # must not raise


def test_truncate_caps_large_text():
    big = "x" * 10_000
    out = obs._truncate(big)
    assert len(out) < len(big)
    assert out.endswith("…[truncated]")
    assert obs._truncate("short") == "short"
