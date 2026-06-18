"""Per-source backfill (#241): cost model, the ingest orchestrator (gate/tag/dedup), history fallback."""

from __future__ import annotations

import asyncio
import json

from maat.acquire import history
from maat.acquire.history import HistoryArticle
from maat.learning import source_backfill
from maat.serving import spend


def test_backfill_cost_sums_and_scales_with_volume():
    c = spend.backfill_cost(n_articles=100, n_articles_with_claims=90, n_claims=800,
                            n_clusters=120, acquisition_usd=0.50)
    # total is exactly the sum of the parts
    assert c.total_usd == round(
        c.acquisition_usd + c.extract_usd + c.classify_usd + c.embed_usd + c.extremity_usd, 4)
    assert c.extract_usd > 0 and c.classify_usd > 0 and c.acquisition_usd == 0.50
    # extract scales with articles; classify with articles-that-yielded-claims
    c2 = spend.backfill_cost(n_articles=200, n_articles_with_claims=180, n_claims=1600,
                             n_clusters=240, acquisition_usd=0.0)
    assert c2.extract_usd > c.extract_usd and c2.classify_usd > c.classify_usd


class _FakeNC:
    def __init__(self):
        self.pub: list[tuple[str, dict]] = []

    async def publish(self, subject, payload):
        self.pub.append((subject, json.loads(payload)))

    async def flush(self):
        pass


def test_run_backfill_gates_tags_and_dedups(monkeypatch):
    arts = [
        HistoryArticle("https://bbc.com/1", "A", "bbc.com", "en", "body one long enough here", "", "gdelt"),
        HistoryArticle("https://bbc.com/2", "B", "bbc.com", "en", "body two long enough here", "", "apify"),
        HistoryArticle("https://bad.com/3", "C", "bad.com", "en", "denied body here", "", "gdelt"),   # deny-listed
        HistoryArticle("https://bbc.com/1", "dup", "bbc.com", "en", "dup", "", "gdelt"),               # duplicate url
        HistoryArticle("https://bbc.com/4", "D", "bbc.com", "en", "", "", "gdelt"),                    # no body
    ]

    async def fake_hist(source, *, depth=100, **k):
        return arts

    class _V:
        def __init__(self, accept):
            self.accept = accept

    def fake_gate(domain, title, **k):
        return _V(domain != "bad.com")  # gate rejects bad.com

    monkeypatch.setattr(source_backfill, "fetch_source_history", fake_hist)
    monkeypatch.setattr(source_backfill, "accept_source", fake_gate)

    nc = _FakeNC()
    res = asyncio.run(source_backfill.run_backfill(
        nc, "bbc.com", run_id="bf-test", at="t0", depth=100,
        gate_prompt="p", denied={"bad.com"}, seen=set()))

    assert res.ingested == 2 and res.gated_out == 1 and res.duplicate == 1 and res.no_body == 1
    assert res.by_channel == {"gdelt": 1, "apify": 1}
    # a backfilling state event was emitted up front
    assert any(s.endswith("source.state_changed") for s, _ in nc.pub)
    # the two survivors were published as article.ingested, tagged for cost attribution
    ingested = [d for s, d in nc.pub if s.endswith("article.ingested")]
    assert len(ingested) == 2
    for env in ingested:
        assert env["data"]["provider"] == "backfill"
        assert env["data"]["backfill_run_id"] == "bf-test"
        assert env["data"]["source"] == "bbc.com"


def test_run_backfill_apify_acquisition_cost(monkeypatch):
    arts = [HistoryArticle(f"https://x.com/{i}", "t", "x.com", "en", "long enough body here", "", "apify")
            for i in range(4)]

    async def fake_hist(source, *, depth=100, **k):
        return arts

    monkeypatch.setattr(source_backfill, "fetch_source_history", fake_hist)
    monkeypatch.setattr(source_backfill, "accept_source", lambda d, t, **k: type("V", (), {"accept": True})())

    res = asyncio.run(source_backfill.run_backfill(
        _FakeNC(), "x.com", run_id="r", at="t", depth=100, gate_prompt="p", seen=set()))
    assert res.ingested == 4
    assert res.acquisition_usd == round(4 * source_backfill._APIFY_PER_RESULT_USD, 4)  # only apify counts


def test_fetch_history_tops_up_across_channels(monkeypatch):
    # GDELT returns 2, Apify tops up to the cap, NewsData not needed — and URLs dedupe across channels.
    async def fake_gdelt(source, *, depth, months, fetch_conc):
        return [HistoryArticle(f"https://g/{i}", "g", source, "en", "b", "", "gdelt") for i in range(2)]

    async def fake_apify(source, *, depth):
        return [HistoryArticle(f"https://g/0", "dup", source, "en", "b", "", "apify")] + [  # noqa: F541 - dup url
            HistoryArticle(f"https://a/{i}", "a", source, "en", "b", "", "apify") for i in range(depth)]

    async def fake_newsdata(source, *, depth):
        raise AssertionError("should not be reached once depth is met")

    monkeypatch.setattr(history, "_gdelt_history", fake_gdelt)
    monkeypatch.setattr(history, "_apify_history", fake_apify)
    monkeypatch.setattr(history, "_newsdata_history", fake_newsdata)

    out = asyncio.run(history.fetch_source_history("bbc.com", depth=5))
    assert len(out) == 5                          # capped at depth
    assert len({a.url for a in out}) == 5         # the duplicate g/0 was dropped
    assert out[0].channel == "gdelt"              # gdelt first, then apify top-up
