"""#189 — DRAFT hot-path topic enrichment (memoised bulk-model interest expansion).

Covers: enriched_interest unions LLM terms onto the deterministic parse, memoises per interest
(one model call, not per request), falls back to the pure parse on error, and that the feed
filter with use_llm=True matches a story the pure terms would miss.
"""

import maat.providers.seam as seam
import maat.serving.topics as topics
from maat.serving.feed import _filter_by_topics
from maat.serving.topics import enriched_interest


class _Reply:
    def __init__(self, text):
        self.text = text


def _clear():
    # topics imports the provider lazily (no providers import at module load), so the LLM seam is
    # patched on maat.providers.seam; the cache must be cleared between tests to avoid bleed.
    topics._enriched_cached.cache_clear()


def test_enrichment_unions_llm_terms_onto_base(monkeypatch):
    _clear()
    monkeypatch.setattr(
        seam, "claude_complete",
        lambda *a, **k: _Reply('{"terms": ["sahel security", "coup"], "query": "sahel"}'),
    )
    spec = enriched_interest("West African politics")
    # LLM additions present...
    assert "sahel security" in spec.terms and "coup" in spec.terms
    # ...and the deterministic baseline is preserved (union, not replace).
    base = topics.parse_interest("West African politics")
    assert set(base.terms).issubset(set(spec.terms))


def test_enrichment_is_memoised(monkeypatch):
    _clear()
    calls = {"n": 0}

    def _count(*a, **k):
        calls["n"] += 1
        return _Reply('{"terms": ["frontier model"], "query": "ai"}')

    monkeypatch.setattr(seam, "claude_complete", _count)
    enriched_interest("artificial intelligence")
    enriched_interest("artificial intelligence")  # same interest → served from cache
    enriched_interest("Artificial Intelligence  ")  # normalised to the same key
    assert calls["n"] == 1


def test_enrichment_falls_back_to_pure_on_error(monkeypatch):
    _clear()

    def _boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(seam, "claude_complete", _boom)
    spec = enriched_interest("semiconductor supply chains")
    assert spec.terms == topics.parse_interest("semiconductor supply chains").terms


def test_filter_use_llm_matches_via_enriched_terms(monkeypatch):
    _clear()
    # The pure parse of "AI" wouldn't match "frontier model"; the enriched terms do.
    monkeypatch.setattr(
        seam, "claude_complete",
        lambda *a, **k: _Reply('{"terms": ["frontier model", "large language model"], "query": "ai"}'),
    )
    payload = {
        "count": 2,
        "stories": [
            {"id": "m", "fact": "A new frontier model is released",
             "claims": [{"text": "The lab published benchmark scores."}]},
            {"id": "x", "fact": "Local bakery wins an award",
             "claims": [{"text": "The pastry chef thanked the town."}]},
        ],
    }
    out_llm = _filter_by_topics(payload, "AI", use_llm=True)
    assert [s["id"] for s in out_llm["stories"]] == ["m"]
    # Pure path can't make that leap, so it keeps nothing (proves the LLM path did the work).
    out_pure = _filter_by_topics(payload, "AI", use_llm=False)
    assert out_pure["stories"] == []
