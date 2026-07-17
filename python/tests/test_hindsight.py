"""Hindsight reputation (#435) — history as reputation's exogenous anchor.

Pins the three properties the design turns on: hindsight outcomes outweigh corroboration-derived
ones in the RATE (the circle-breaker) while the FLOOR counts raw outcomes; resolutions only score
when an NLI model agrees with the quoted evidence (an LLM's say-so is never an outcome); and
re-runs re-assert (stable stream ids + latest-per-stream) instead of double-counting.
"""

from __future__ import annotations

from maat.learning.hindsight import (
    HINDSIGHT_RESOLVE_PROMPT,
    HINDSIGHT_TRIAGE_PROMPT,
    hindsight_stream_id,
    latest_by_stream,
    resolve_claim,
    triage_claims,
)
from maat.learning.reputation import fold_reputation, reputation_score

_FACT = "The central bank sold half its gold reserves"


def _corroborated(fact, sources, n=3, corrected=False):
    """A minimal cluster.corroborated-shaped event that resolve_outcome can CONFIRM (>=3) or
    REFUTE (corrected)."""
    return {
        "fact": fact, "independent_originators": n, "has_primary": False,
        "extremity": "notable", "confidence": 0.7, "sources": sources,
        "originators": [[s] for s in sources], "corrected": corrected,
    }


# --- the circle-breaker: provenance weights -------------------------------------------------------


def test_hindsight_outcomes_outweigh_corroboration_derived_ones():
    """One corroboration-CONFIRMED fact + one hindsight-REFUTED fact. Raw counts: 1–1 (rate 0.5 if
    unweighted). Weighted: confirmed 0.5 vs refuted 1.0 → 0.333. The exogenous outcome moves the
    rate twice as far — reputation is anchored outside the loop that also feeds it."""
    recs = fold_reputation(
        [_corroborated("fact one", ["x.com", "a.com", "b.com"])],
        hindsight=[{"source": "x.com", "fact": "fact two", "outcome": "refuted"}],
    )
    x = next(r for r in recs if r.source == "x.com")
    assert x.facts_confirmed == 1 and x.facts_refuted == 1  # raw counts stay honest
    assert x.outcome_n == 2                                  # the FLOOR reads raw outcomes
    assert x.confirmation_rate == round(0.5 / 1.5, 3)        # the RATE is provenance-weighted
    assert x.hindsight_refuted == 1 and x.hindsight_confirmed == 0


def test_all_corroboration_outcomes_leave_the_rate_unchanged():
    """With a single provenance the discount cancels: pure-corroboration reputation reads exactly
    as before this change (backwards-compatible for every existing source)."""
    recs = fold_reputation([
        _corroborated("f1", ["x.com", "a.com", "b.com"]),
        _corroborated("f2", ["x.com", "a.com", "b.com"], corrected=True),
    ])
    x = next(r for r in recs if r.source == "x.com")
    assert x.outcome_n == 2 and x.confirmation_rate == 0.5


def test_hindsight_never_touches_presence_signals():
    """Hindsight rows carry no cluster structure — they must not count as appearances or move
    independent_rate (a fabricated 'presence' would leak into the cold-start score)."""
    recs = fold_reputation([], hindsight=[
        {"source": "x.com", "fact": "f", "outcome": "confirmed"},
    ])
    x = recs[0]
    assert x.appearances == 0 and x.independent_rate == 0.0
    assert x.outcome_n == 1
    assert reputation_score(x) == 0.7  # 0.7*rate(1.0) + 0.3*independent_rate(0) — outcome-anchored


def test_malformed_hindsight_rows_are_ignored():
    recs = fold_reputation([], hindsight=[
        {"source": "", "outcome": "confirmed"},          # no source
        {"source": "x.com", "outcome": "maybe"},         # junk outcome
        {"source": "x.com", "outcome": "unresolved"},    # counted, but never a terminal outcome
    ])
    x = next((r for r in recs if r.source == "x.com"), None)
    assert x is not None and x.outcome_n == 0 and x.facts_unresolved == 1


# --- re-assert semantics --------------------------------------------------------------------------


def test_stream_id_is_stable_and_latest_wins():
    a = hindsight_stream_id("bbc.com", _FACT)
    assert a == hindsight_stream_id("bbc.com", _FACT)          # re-run → same id → re-assert
    assert a != hindsight_stream_id("cnn.com", _FACT)          # per-source
    assert a != hindsight_stream_id("bbc.com", _FACT + " x")   # per-fact
    rows = [
        (a, {"outcome": "confirmed"}),
        (a, {"outcome": "refuted"}),   # a later, better-evidenced resolution replaces the first
    ]
    assert latest_by_stream(rows) == [{"outcome": "refuted"}]


# --- the NLI agreement gate -----------------------------------------------------------------------


def _nli(premise, hypothesis):
    both = (premise + " " + hypothesis).lower()
    if "denied" in both:
        return ("contradiction", 0.9)
    if "gold" in premise.lower() and "gold" in hypothesis.lower():
        return ("entailment", 0.95)
    return ("neutral", 0.8)


def _ws_returning(obj_json):
    def fake(prompt, tools=None, model=None, stage=None, **kw):
        return [{"type": "text", "text": obj_json}]
    return fake


def test_resolution_scores_only_when_nli_agrees(monkeypatch):
    import maat.learning.hindsight as hs

    # LLM says TRUE and the quoted evidence ENTAILS → confirmed.
    monkeypatch.setattr(hs, "claude_web_search", _ws_returning(
        '{"outcome": "true", "url": "https://reg.example/f", "domain": "reg.example",'
        ' "quote": "the bank confirms the sale of half its gold reserves", "tier": 1}'))
    got = resolve_claim(_FACT, source="bbc.com", published="2025-01-01", nli=_nli)
    assert got["outcome"] == "confirmed" and got["evidence"]["tier"] == 1

    # LLM says TRUE but the evidence CONTRADICTS → unresolved, never scored on say-so.
    monkeypatch.setattr(hs, "claude_web_search", _ws_returning(
        '{"outcome": "true", "url": "https://reg.example/s", "domain": "reg.example",'
        ' "quote": "the central bank denied selling any gold reserves", "tier": 1}'))
    assert resolve_claim(_FACT, source="bbc.com", published="2025-01-01", nli=_nli)["outcome"] == "unresolved"

    # LLM says FALSE and the evidence contradicts → refuted (the agreement holds in both directions).
    assert resolve_claim(
        _FACT, source="bbc.com", published="2025-01-01", nli=_nli,
        prompt=HINDSIGHT_RESOLVE_PROMPT,
    )["outcome"] == "unresolved"  # same contradiction evidence but LLM said "true" above; now flip:
    monkeypatch.setattr(hs, "claude_web_search", _ws_returning(
        '{"outcome": "false", "url": "https://reg.example/s", "domain": "reg.example",'
        ' "quote": "the central bank denied selling any gold reserves", "tier": 2}'))
    got = resolve_claim(_FACT, source="bbc.com", published="2025-01-01", nli=_nli)
    assert got["outcome"] == "refuted" and got["evidence"]["tier"] == 2


def test_no_nli_means_no_outcomes(monkeypatch):
    import maat.learning.hindsight as hs

    monkeypatch.setattr(hs, "claude_web_search", _ws_returning(
        '{"outcome": "true", "url": "https://reg.example/f", "domain": "reg.example",'
        ' "quote": "the bank confirms the sale of half its gold reserves", "tier": 1}'))
    assert resolve_claim(_FACT, source="bbc.com", published="2025", nli=None)["outcome"] == "unresolved"


def test_triage_parses_keeps_and_fails_closed(monkeypatch):
    import maat.learning.hindsight as hs

    class _R:
        text = "keep these: [1, 3]"
        model = "m"

    monkeypatch.setattr(hs, "claude_complete", lambda *a, **k: _R())
    assert triage_claims(["a", "b", "c"]) == [True, False, True]

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(hs, "claude_complete", boom)
    # fails CLOSED: unscreened claims are never resolved (spend without signal)
    assert triage_claims(["a", "b"]) == [False, False]
    assert triage_claims([]) == []


# --- registry + script shape ----------------------------------------------------------------------


def test_hindsight_prompts_registered_as_drafts_with_placeholders():
    from maat.prompts import PROMPTS_BY_KEY

    for key, seed in (("hindsight_triage", HINDSIGHT_TRIAGE_PROMPT),
                      ("hindsight_resolve", HINDSIGHT_RESOLVE_PROMPT)):
        entry = PROMPTS_BY_KEY[key]
        assert entry["status"] == "draft" and entry["default"] == seed
        for ph in entry["placeholders"]:
            assert ph in seed, f"{key}: declared placeholder {ph} missing from seed"


def test_backfill_windows_spread_evenly():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "hindsight_backfill", Path(__file__).parents[1] / "scripts" / "hindsight_backfill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ws = mod._windows(18, 10)
    assert len(ws) == 10
    assert all(a < b for a, b in ws)                      # each window is well-formed
    assert ws[0][0] < ws[-1][0]                           # oldest first
    spans = {round((b - a).days) for a, b in ws}
    assert len(spans) == 1                                # EVEN spread — the decorrelation contract
