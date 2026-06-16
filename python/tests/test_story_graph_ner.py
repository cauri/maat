"""#42 — DRAFT LLM entity spine for the story graph. Runs on Sonnet (cauri); heuristic stays the
fallback. The module binds claude_complete at import, so patch the name in the ner module."""

import maat.pipeline.story_graph_ner as ner


class _Reply:
    def __init__(self, text):
        self.text = text


def test_parses_entity_array(monkeypatch):
    monkeypatch.setattr(
        ner, "claude_complete",
        lambda *a, **k: _Reply('["nelson reyes", "valoria", "ministry of finance"]'),
    )
    out = ner.llm_entity_spine("Nelson Reyes resigned from Valoria's finance ministry")
    assert out == ["nelson reyes", "valoria", "ministry of finance"]


def test_uses_sonnet_not_the_cheap_tier(monkeypatch):
    seen = {}

    def _capture(prompt, *, model="?", **k):
        seen["model"] = model
        return _Reply('["x"]')

    monkeypatch.setattr(ner, "claude_complete", _capture)
    ner.llm_entity_spine("some event text")
    assert seen["model"] == "claude-sonnet-4-6"


def test_falls_back_to_heuristic_on_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(ner, "claude_complete", _boom)
    # Falls through to entity_spine_heuristic — proper nouns extracted deterministically, no raise.
    out = ner.llm_entity_spine("Nelson Reyes met Angela Mertz in Berlin")
    assert isinstance(out, list) and out  # heuristic returned something rather than erroring


def test_empty_text_no_call(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not call the model for empty text")

    monkeypatch.setattr(ner, "claude_complete", _boom)
    assert ner.llm_entity_spine("   ") == []
