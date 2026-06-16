"""#189 — DRAFT LLM triage refinement, gated by MAAT_TRIAGE_LLM (rules stay the fallback)."""

import maat.agents.triage as t
import maat.providers.seam as seam


class _Reply:
    def __init__(self, text):
        self.text = text


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("MAAT_TRIAGE_LLM", raising=False)
    assert t._llm_triage("the confidence on the Reyes story is wrong") is None


def test_classifies_when_enabled(monkeypatch):
    monkeypatch.setenv("MAAT_TRIAGE_LLM", "1")
    monkeypatch.setattr(seam, "mistral_complete", lambda *a, **k: _Reply('{"category": "bug", "reason": "UI broke"}'))
    assert t._llm_triage("the page crashes when I tap a story") == ("bug", "UI broke")


def test_falls_back_to_rules_on_bad_output(monkeypatch):
    monkeypatch.setenv("MAAT_TRIAGE_LLM", "1")
    monkeypatch.setattr(seam, "mistral_complete", lambda *a, **k: _Reply("not json at all"))
    assert t._llm_triage("x") is None


def test_classify_uses_llm_and_routes_ui_to_autofix(monkeypatch):
    monkeypatch.setenv("MAAT_TRIAGE_LLM", "1")
    monkeypatch.setattr(seam, "mistral_complete", lambda *a, **k: _Reply('{"category": "ui", "reason": "layout off"}'))
    r = t.classify("the spacing looks weird on the detail screen")
    assert r.category == "ui" and r.auto_fixable is True


def test_classify_routes_dispute_to_review(monkeypatch):
    monkeypatch.setenv("MAAT_TRIAGE_LLM", "1")
    monkeypatch.setattr(
        seam, "mistral_complete",
        lambda *a, **k: _Reply('{"category": "veracity-dispute", "reason": "score wrong"}'),
    )
    r = t.classify("the confidence on this looks far too high")
    assert r.category == "veracity-dispute" and r.route == "review" and r.auto_fixable is False
