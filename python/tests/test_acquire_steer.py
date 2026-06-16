"""Tests for acquire/steer.py (Issue #35) — the acquisition-steering actuation.

The source-learning loop produces capped, diversity-floored acquisition weights; this module is the
load-bearing half that lets the live ingestion clock CONSULT them. Covers:

- rank_for_fetch: cold start is a pass-through (no learned weights → original order, untouched).
- rank_for_fetch: reward sources are fetched before low-reward / unknown ones.
- rank_for_fetch: diversity is structural — every source present is represented before any source
  gets a second slot (phase 1), so re-ranking never silences a source.
- rank_for_fetch: the per-source cap stops one source dominating a query's budget...
- rank_for_fetch: ...but the cap is relaxed rather than leaving fetch budget idle when few sources.
- rank_for_fetch: a budget narrows the fetch set; determinism.
- deepening_plan: top proven sources only (low-evidence / solo-flagged excluded), bounded, domain-scoped.
- steer_summary: shape + the cold-start (inactive) case.
"""

from collections import Counter

from maat.acquire.steer import (
    PER_QUERY_FETCH_BUDGET,
    deepening_plan,
    rank_for_fetch,
    steer_summary,
)
from maat.learning.source_learning import (
    SourcePreference,
    SourcePreferences,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Cand:
    """A minimal acquisition candidate — exposes ``.domain`` like GdeltArticle / Apify items."""

    def __init__(self, domain: str, url: str):
        self.domain = domain
        self.url = url

    def __repr__(self) -> str:  # readable assertion failures
        return f"_Cand({self.domain!r}, {self.url!r})"


def _pref(
    source: str,
    *,
    rank: int = 1,
    weight: float = 0.1,
    low_evidence: bool = False,
    solo: bool = False,
    in_floor: bool = True,
) -> SourcePreference:
    return SourcePreference(
        source=source,
        rank=rank,
        acquisition_weight=weight,
        confirmation_rate=0.8,
        independent_rate=0.7,
        mean_attribution_weight=0.8,
        solo_extraordinary_flag=solo,
        low_evidence=low_evidence,
        in_diversity_floor=in_floor,
    )


def _prefs(*pairs: tuple[str, float], ranked: list[SourcePreference] | None = None) -> SourcePreferences:
    """Build SourcePreferences from (source, weight) pairs. ``ranked`` overrides the ranked list."""
    weights = {s: w for s, w in pairs}
    if ranked is None:
        ranked = [
            _pref(s, rank=i + 1, weight=w)
            for i, (s, w) in enumerate(sorted(pairs, key=lambda p: -p[1]))
        ]
    return SourcePreferences(
        ranked=ranked,
        weights=weights,
        diversity_floor=frozenset(weights),
    )


def _domains(cands: list[_Cand]) -> list[str]:
    return [c.domain for c in cands]


# ---------------------------------------------------------------------------
# rank_for_fetch
# ---------------------------------------------------------------------------


def test_cold_start_is_passthrough():
    """No learned weights yet → original order preserved exactly (system behaves as before steering)."""
    cands = [_Cand("a.com", "u1"), _Cand("b.com", "u2"), _Cand("c.com", "u3")]
    empty = SourcePreferences(ranked=[], weights={}, diversity_floor=frozenset())
    assert rank_for_fetch(cands, empty) == cands
    # Even with a budget, cold start only truncates — never reorders.
    assert rank_for_fetch(cands, empty, budget=2) == cands[:2]


def test_reward_sources_ranked_first():
    """Higher-weight sources are fetched before lower-weight ones."""
    prefs = _prefs(("good.com", 0.30), ("mid.com", 0.10), ("low.com", 0.02))
    cands = [_Cand("low.com", "u1"), _Cand("mid.com", "u2"), _Cand("good.com", "u3")]
    assert _domains(rank_for_fetch(cands, prefs)) == ["good.com", "mid.com", "low.com"]


def test_unknown_sources_rank_after_known_but_are_not_dropped():
    """A source with no reputation sits at the floor weight — last preference, but still present
    (new voices are never silenced)."""
    prefs = _prefs(("good.com", 0.30))
    cands = [_Cand("unknown.com", "u1"), _Cand("good.com", "u2")]
    out = rank_for_fetch(cands, prefs)
    assert _domains(out) == ["good.com", "unknown.com"]
    assert "unknown.com" in _domains(out)


def test_diversity_first_every_source_represented_before_seconds():
    """Phase 1: with one candidate per source, every distinct source appears before any source
    gets a second slot — even when one source is far higher-weighted."""
    prefs = _prefs(("good.com", 0.30), ("b.com", 0.05), ("c.com", 0.05))
    cands = [
        _Cand("good.com", "g1"), _Cand("good.com", "g2"), _Cand("good.com", "g3"),
        _Cand("b.com", "b1"), _Cand("c.com", "c1"),
    ]
    out = _domains(rank_for_fetch(cands, prefs))
    # First three slots are the three distinct sources (good wins the lead but doesn't monopolise).
    assert set(out[:3]) == {"good.com", "b.com", "c.com"}
    assert out[0] == "good.com"


def test_per_source_cap_limits_domination_when_alternatives_exist():
    """When many sources are available, no single source exceeds its capped share of the budget."""
    # 6 distinct sources, top one has lots of candidates; budget 8 → cap = ceil(0.30*8) = 3.
    prefs = _prefs(
        ("top.com", 0.30), ("s2", 0.10), ("s3", 0.09), ("s4", 0.08), ("s5", 0.07), ("s6", 0.06)
    )
    cands = [_Cand("top.com", f"t{i}") for i in range(8)]
    cands += [_Cand(f"s{i}", f"u{i}") for i in range(2, 7)]
    out = _domains(rank_for_fetch(cands, prefs, budget=8))
    assert len(out) == 8
    assert Counter(out)["top.com"] <= 3  # capped — does not eat the whole budget


def test_cap_relaxed_rather_than_wasting_budget_when_few_sources():
    """With only one source available, refusing articles buys no diversity — fill the budget."""
    prefs = _prefs(("only.com", 0.30))
    cands = [_Cand("only.com", f"u{i}") for i in range(8)]
    out = rank_for_fetch(cands, prefs, budget=6)
    assert len(out) == 6  # not capped down to ceil(0.30*6)=2 — nothing more diverse to fetch


def test_budget_narrows_fetch_set():
    """A budget truncates to the most-preferred candidates."""
    prefs = _prefs(("good.com", 0.30), ("mid.com", 0.10), ("low.com", 0.02))
    cands = [_Cand("low.com", "u1"), _Cand("mid.com", "u2"), _Cand("good.com", "u3")]
    out = rank_for_fetch(cands, prefs, budget=2)
    assert _domains(out) == ["good.com", "mid.com"]


def test_rank_for_fetch_is_deterministic():
    prefs = _prefs(("a.com", 0.2), ("b.com", 0.2), ("c.com", 0.05))
    cands = [_Cand("c.com", "1"), _Cand("a.com", "2"), _Cand("b.com", "3"), _Cand("a.com", "4")]
    first = _domains(rank_for_fetch(cands, prefs))
    second = _domains(rank_for_fetch(cands, prefs))
    assert first == second


def test_empty_candidates():
    prefs = _prefs(("a.com", 0.2))
    assert rank_for_fetch([], prefs) == []
    assert rank_for_fetch([], prefs, budget=5) == []


# ---------------------------------------------------------------------------
# deepening_plan
# ---------------------------------------------------------------------------


def test_deepening_targets_top_sources_and_scopes_by_domain():
    ranked = [
        _pref("reuters.com", rank=1, weight=0.30),
        _pref("apnews.com", rank=2, weight=0.20),
        _pref("bbc.com", rank=3, weight=0.15),
        _pref("kleinod.de", rank=4, weight=0.05),
    ]
    prefs = _prefs(
        ("reuters.com", 0.30), ("apnews.com", 0.20), ("bbc.com", 0.15), ("kleinod.de", 0.05),
        ranked=ranked,
    )
    plan = deepening_plan(prefs, ["central bank rates"], top_n=3, max_queries=3)
    sources = [s for s, _ in plan]
    assert sources == ["reuters.com", "apnews.com", "bbc.com"]  # top 3, not the 4th
    for src, q in plan:
        assert f"domain:{src}" in q
        assert q.startswith("central bank rates")


def test_deepening_excludes_low_evidence_and_solo_flagged():
    ranked = [
        _pref("solo.com", rank=1, weight=0.30, solo=True),
        _pref("new.com", rank=2, weight=0.20, low_evidence=True),
        _pref("proven.com", rank=3, weight=0.15),
    ]
    prefs = _prefs(
        ("solo.com", 0.30), ("new.com", 0.20), ("proven.com", 0.15), ranked=ranked
    )
    plan = deepening_plan(prefs, ["topic"], top_n=3)
    assert [s for s, _ in plan] == ["proven.com"]  # only the proven, non-flagged source


def test_deepening_is_bounded_and_dedups_identical_queries():
    ranked = [_pref("a.com", rank=1, weight=0.3), _pref("b.com", rank=2, weight=0.2)]
    prefs = _prefs(("a.com", 0.3), ("b.com", 0.2), ranked=ranked)
    # One topic, two sources, high cap → at most 2 unique (source, topic) queries, no duplicates.
    plan = deepening_plan(prefs, ["only-topic"], top_n=2, max_queries=10)
    assert len(plan) == 2
    assert len(set(plan)) == 2
    # max_queries caps the count across multiple topics.
    plan2 = deepening_plan(prefs, ["t1", "t2", "t3"], top_n=2, max_queries=3)
    assert len(plan2) == 3


def test_deepening_empty_when_no_topics_or_no_qualifying_sources():
    prefs = _prefs(("a.com", 0.3))
    assert deepening_plan(prefs, []) == []
    cold = SourcePreferences(ranked=[], weights={}, diversity_floor=frozenset())
    assert deepening_plan(cold, ["topic"]) == []


# ---------------------------------------------------------------------------
# steer_summary
# ---------------------------------------------------------------------------


def test_steer_summary_shape():
    ranked = [_pref("a.com", rank=1, weight=0.3), _pref("b.com", rank=2, weight=0.2)]
    prefs = _prefs(("a.com", 0.3), ("b.com", 0.2), ranked=ranked)
    plan = [("a.com", "topic domain:a.com")]
    summary = steer_summary(
        prefs,
        per_query_budget=PER_QUERY_FETCH_BUDGET,
        deepen_plan=plan,
        deepened_articles=4,
        reranked_queries=2,
    )
    assert summary["active"] is True
    assert summary["per_query_budget"] == PER_QUERY_FETCH_BUDGET
    assert summary["deepen_sources"] == ["a.com"]
    assert summary["deepen_queries"] == 1
    assert summary["deepened_articles"] == 4
    assert summary["reranked_queries"] == 2
    assert {s["source"] for s in summary["top_sources"]} == {"a.com", "b.com"}


def test_steer_summary_inactive_on_cold_start():
    cold = SourcePreferences(ranked=[], weights={}, diversity_floor=frozenset())
    summary = steer_summary(
        cold, per_query_budget=PER_QUERY_FETCH_BUDGET, deepen_plan=[], deepened_articles=0, reranked_queries=0
    )
    assert summary["active"] is False
    assert summary["top_sources"] == []
