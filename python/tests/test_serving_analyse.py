"""Tests for the Analyse serving layer (P14 #365) — url identity, public payload discipline
("what, not how"), reasons wording, live-search helpers, and the SSE endpoint end-to-end with
faked engine + DB seams.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from starlette.testclient import TestClient

import maat.serving.analyse as sa
from maat.acquire.fetch import FetchedPage
from maat.learning.story_credibility import StoryScore
from maat.pipeline.analyse import ArticleAnalysis, ClaimReading, LiveCandidate
from maat.pipeline.claim import Claim
from maat.serving.ratelimit import PerIpRateLimiter


def _patch_page(monkeypatch, *, canonical=None, body="A sufficiently long article body for the test."):
    """run_analysis now fetches once before analysing — stub it so tests never hit the network."""
    monkeypatch.setattr(
        sa, "fetch_page", lambda *_a, **_k: FetchedPage(body=body, title="T", canonical=canonical)
    )


# --- url identity ----------------------------------------------------------------------------


def test_normalise_url_strips_tracking_fragment_and_case():
    a = sa.normalise_url("HTTPS://WWW.Example.com/Story/?utm_source=x&fbclid=1&id=7#frag")
    assert a == "https://www.example.com/Story?id=7"
    assert sa.normalise_url("https://example.com/story/") == "https://example.com/story"


def test_analysis_id_stable_across_paste_variants():
    x = sa.analysis_id("https://example.com/story?utm_campaign=news")
    y = sa.analysis_id("https://EXAMPLE.com/story/#top")
    assert x == y
    assert x.startswith("an-")


# --- public payload discipline -----------------------------------------------------------------


def _reading(**kw):
    claim = kw.pop("claim", Claim(text="The minister resigned", voice="own",
                                  evidence_span="x", in_headline=kw.pop("central", False)))
    return ClaimReading(
        claim=claim,
        extremity=kw.pop("extremity", "notable"),
        confidence=kw.pop("confidence", 0.76),
        independent_originators=kw.pop("originators", 3),
        has_primary=kw.pop("has_primary", False),
        disputed=kw.pop("disputed", False),
        grounding=kw.pop("grounding", None),
        verdict=kw.pop("verdict", "Corroborated"),
        tier=kw.pop("tier", "mid"),
        matched_cluster_id=kw.pop("matched_cluster_id", "cl-secret"),
    )


def _analysis(facts, score, **kw):
    return ArticleAnalysis(
        url="https://example.com/story", source=kw.pop("source", "example.com"),
        title=kw.pop("title", "T"), language="en", image=None, date=None,
        facts=facts, projections=kw.pop("projections", []),
        score=score, publisher_score=kw.pop("publisher_score", None), live=None,
    )


def test_public_claim_never_leaks_mechanism():
    p = sa.public_claim(_reading())
    assert set(p) == {"text", "voice", "speaker", "central", "extremity", "score",
                      "verdict", "tier"}
    flat = str(p)
    assert "cl-secret" not in flat and "originator" not in flat and "cluster" not in flat


def test_public_payload_shape_and_publisher():
    score = StoryScore(70, "corroborated", "Corroborated", ["x"], False, False)
    payload = sa.public_payload(_analysis([_reading()], score, publisher_score=0.82), "an-1")
    assert payload["publisher"] == {"domain": "example.com", "rated": True, "score": 82,
                                    "review_started": False}
    assert payload["overall"]["band"] == "corroborated"
    assert payload["scope"] == sa.SCOPE_LINE
    unrated = sa.public_payload(_analysis([_reading()], score), "an-2")
    assert unrated["publisher"]["rated"] is False
    assert unrated["publisher"]["score"] is None


def test_public_reasons_disqualified_passthrough_and_lone_central():
    dq = StoryScore(12, "disqualified", "Fails verification",
                    ["a central extraordinary claim rests on a single unsupported source"],
                    False, False)
    assert sa.public_reasons(_analysis([_reading(central=True)], dq)) == list(dq.why)

    ok = StoryScore(58, "developing", "Developing", [], False, False)
    lone = _reading(central=True, originators=1, verdict="Only this source so far")
    reasons = sa.public_reasons(_analysis([lone], ok))
    assert reasons[0] == "the central claim has only this source so far"


def test_public_reasons_capped_and_support():
    capped = StoryScore(70, "corroborated", "Corroborated", [], True, False)
    facts = [_reading(central=True, verdict="Well corroborated", confidence=0.9),
             _reading(confidence=0.8), _reading(confidence=0.75)]
    reasons = sa.public_reasons(_analysis(facts, capped))
    assert "its central claim is well corroborated" in reasons
    assert any("2 corroborating facts" in r for r in reasons)
    assert any("track record" in r for r in reasons)


# --- live helpers --------------------------------------------------------------------------------


def test_gdelt_query_significant_terms():
    q = sa.gdelt_query("The central bank secretly sold half of its gold reserves")
    assert "central" in q and "gold" in q
    assert "the" not in q.split()
    assert len(q.split()) <= 6


def test_make_accept_filters_junk_and_denied():
    accept = sa.make_accept({"denied.example"})
    ok = LiveCandidate(url="https://bbc.co.uk/x", domain="bbc.co.uk", title="t", body="b")
    junk = LiveCandidate(url="https://reddit.com/x", domain="reddit.com", title="t", body="b")
    denied = LiveCandidate(url="https://denied.example/x", domain="denied.example",
                           title="t", body="b")
    assert accept(ok) is True
    assert accept(junk) is False
    assert accept(denied) is False


def test_gate_hard_rejects_non_news_domains_without_llm():
    gate = sa.make_gate()
    msg = gate("reddit.com", "a thread about news")
    assert msg is not None and "isn't a news publisher" in msg


def test_registry_review_kickoff_for_unrated_publisher(monkeypatch):
    """An unrated publisher enters the existing #241 review pipeline: source.registered is
    published once, and the payload says the review is under way."""
    import asyncio

    monkeypatch.setattr(sa, "_LIMITER", PerIpRateLimiter(capacity=100, refill_per_sec=100))
    sa._RESULTS.clear()

    async def host_ok(_h, _p):
        return True

    monkeypatch.setattr(sa, "_host_is_public", host_ok)
    _patch_page(monkeypatch)
    reading = _reading(central=True, matched_cluster_id=None)
    analysis = _analysis([reading], StoryScore(58, "developing", "Developing", [], False, False))
    monkeypatch.setattr(sa, "analyse_article", lambda url, **kw: analysis)

    published: list[str] = []

    class FakeNats:
        async def publish(self, subject, payload):
            published.append(subject)

    class State:
        pool = FakePool()
        nats = FakeNats()

    payload = asyncio.run(sa.run_analysis(State(), "https://example.com/story", refresh=True))
    assert payload["publisher"]["rated"] is False
    assert payload["publisher"]["review_started"] is True
    assert "maat.events.source.registered" in published
    assert "maat.events.analysis.completed" in published


def test_no_registry_kickoff_when_publisher_already_rated(monkeypatch):
    import asyncio

    sa._RESULTS.clear()

    async def host_ok(_h, _p):
        return True

    monkeypatch.setattr(sa, "_host_is_public", host_ok)
    _patch_page(monkeypatch)
    reading = _reading(central=True, matched_cluster_id=None)
    analysis = _analysis([reading], StoryScore(70, "corroborated", "Corroborated", [], False, False),
                         publisher_score=0.8)
    monkeypatch.setattr(sa, "analyse_article", lambda url, **kw: analysis)

    published: list[str] = []

    class FakeNats:
        async def publish(self, subject, payload):
            published.append(subject)

    class State:
        pool = FakePool()
        nats = FakeNats()

    payload = asyncio.run(sa.run_analysis(State(), "https://example.com/story2", refresh=True))
    assert payload["publisher"]["rated"] is True
    assert payload["publisher"]["review_started"] is False
    assert "maat.events.source.registered" not in published


# --- share copy (templated, defensible, "what not how") --------------------------------------


def test_share_copy_is_defensible_and_leaks_no_mechanism():
    dq = StoryScore(20, "disqualified", "Fails verification",
                    ["a central significant claim rests on a single unsupported source"],
                    False, False)
    analysis = _analysis(
        [_reading(central=True, verdict="Only this source — below the bar for a significant claim",
                  tier="floor")],
        dq, title="Bank sold its gold, insider says", source="chronicle.example",
    )
    share = sa.share_copy(analysis, sa.public_reasons(analysis))
    blob = " ".join([share["og_title"], share["og_description"], share["twitter_text"],
                     share["linkedin_text"], share["instagram_caption"]])
    # measured + accurate: carries the verdict, never calls it "false", never leaks mechanism
    assert "Fails verification · 20/100" in share["headline"]
    assert "false" not in blob.lower() and "fake" not in blob.lower()
    for leak in ("originator", "cluster", "corroborate_fixed", "embedding", "§"):
        assert leak not in blob.lower()
    assert "independent reporting" in share["og_description"]
    assert len(share["twitter_text"]) <= 240
    assert "link in bio" in share["instagram_caption"]
    assert "#" in share["instagram_caption"]


def test_share_copy_tally_and_publisher_record():
    ok = StoryScore(72, "corroborated", "Corroborated", [], False, False)
    facts = [
        _reading(central=True, verdict="Well corroborated", tier="hi"),
        _reading(verdict="Corroborated", tier="mid"),
        _reading(verdict="Only this source so far", tier="lo"),
    ]
    analysis = _analysis(facts, ok, publisher_score=0.83)
    share = sa.share_copy(analysis, sa.public_reasons(analysis))
    assert share["tally"] == {"total": 3, "corroborated": 2, "single_source": 1,
                              "disputed": 0, "primary": 0}
    assert "83/100" in share["linkedin_text"]          # publisher track record surfaced
    assert share["headline"] == "Corroborated · 72/100"


def test_share_copy_forecast_only_has_no_number():
    fc = StoryScore(0, "forecast", "Forecast / opinion", ["no checkable facts"], False, True)
    share = sa.share_copy(_analysis([], fc), ["no checkable factual claims yet"])
    assert share["headline"] == "Forecast / opinion"
    assert "/100" not in share["headline"]


def test_share_copy_from_payload_rebuilds_from_a_stored_dict():
    old = {
        "title": "Bank sold its gold, insider says", "source": "chronicle.example",
        "publisher": {"domain": "chronicle.example", "rated": True, "score": 74},
        "overall": {"score": 20, "band": "disqualified", "label": "Fails verification",
                    "reasons": ["a central significant claim rests on a single unsupported source"],
                    "forecast_only": False},
        "claims": [{"verdict": "Only this source so far"}, {"verdict": "Corroborated"},
                   {"verdict": "Well corroborated"}],
    }
    share = sa.share_copy_from_payload(old)
    assert share["headline"] == "Fails verification · 20/100"
    assert share["tally"] == {"total": 3, "corroborated": 2, "single_source": 1,
                              "disputed": 0, "primary": 0}
    assert "74/100" in share["linkedin_text"]
    assert "false" not in share["og_description"].lower()


def test_cached_payload_backfills_share_for_pre_feature_entries():
    import asyncio

    sa._RESULTS.clear()
    old = {
        "analysis_id": "an-old", "title": "T", "source": "bbc.co.uk",
        "publisher": {"domain": "bbc.co.uk", "rated": False, "score": None},
        "overall": {"score": 20, "band": "disqualified", "label": "Fails verification",
                    "reasons": ["a central significant claim rests on a single unsupported source"],
                    "capped": False, "forecast_only": False},
        "claims": [{"verdict": "Only this source so far"}, {"verdict": "Corroborated"}],
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }

    class Pool(FakePool):
        async def fetchrow(self, query, *args):
            if "analysis.completed" in query:
                return {"data": {"analysis": old}}
            return None

    got = asyncio.run(sa.cached_payload(Pool(), "an-old"))
    assert got is not None and "share" in got  # backfilled on read
    assert got["share"]["headline"] == "Fails verification · 20/100"
    assert got["share"]["tally"]["total"] == 2


def test_public_payload_includes_share_block():
    ok = StoryScore(70, "corroborated", "Corroborated", ["x"], False, False)
    payload = sa.public_payload(_analysis([_reading(central=True)], ok), "an-1")
    assert "share" in payload
    assert set(payload["share"]) >= {"headline", "tally", "og_title", "og_description",
                                     "twitter_text", "linkedin_text", "instagram_caption"}


# --- canonical-URL collapse (same article, variant URLs → one cache entry) --------------------


def test_same_publisher_variants_and_registry():
    assert sa._same_publisher("amp.cnn.com", "www.cnn.com") is True
    assert sa._same_publisher("m.example.com", "example.com") is True
    assert sa._same_publisher("news.example.com", "example.com") is True   # subdomain
    assert sa._same_publisher("bbc.co.uk", "bbc.com") is True              # registry-known outlet
    assert sa._same_publisher("evil.example", "nytimes.com") is False


def test_identity_url_same_publisher_canonical_collapses():
    page = FetchedPage(body="x", canonical="https://www.example.com/story")
    ident = sa.identity_url(sa.normalise_url("https://amp.example.com/story?utm_source=t"), page)
    assert ident == "https://www.example.com/story"


def test_identity_url_cross_publisher_canonical_is_ignored_laundering_guard():
    # A page must not borrow another outlet's identity by declaring its canonical.
    pasted = sa.normalise_url("https://spam.example/story")
    page = FetchedPage(body="x", canonical="https://www.nytimes.com/2026/real-story")
    assert sa.identity_url(pasted, page) == pasted


def test_identity_url_no_canonical_uses_pasted():
    pasted = sa.normalise_url("https://example.com/story")
    assert sa.identity_url(pasted, FetchedPage(body="x", canonical=None)) == pasted


def test_amp_variant_reuses_the_canonical_analysis_skipping_the_llm(monkeypatch):
    """Pasting an AMP variant of an already-analysed article serves the stored canonical result
    (one fetch, no re-analysis)."""
    sa._RESULTS.clear()

    async def host_ok(_h, _p):
        return True

    monkeypatch.setattr(sa, "_host_is_public", host_ok)
    canon_url = "https://www.example.com/story"
    _patch_page(monkeypatch, canonical=canon_url)
    canon_aid = sa.analysis_id(canon_url)

    fresh_payload = {
        "analysed_at": datetime.now(timezone.utc).isoformat(),
        "overall": {"band": "corroborated", "score": 71, "label": "Corroborated"},
        "publisher": {"domain": "example.com", "rated": False, "score": None},
    }

    class CachePool(FakePool):
        async def fetchrow(self, query, *args):
            if "analysis.completed" in query and args and args[0] == canon_aid:
                return {"data": {"analysis_id": canon_aid, "analysis": fresh_payload}}
            return None

    called: list[int] = []

    def must_not_run(*_a, **_k):
        called.append(1)
        raise AssertionError("analyse_article must not run for a cache hit")

    monkeypatch.setattr(sa, "analyse_article", must_not_run)

    class State:
        pool = CachePool()
        nats = None

    import asyncio

    got = asyncio.run(sa.run_analysis(State(), "https://amp.example.com/story?utm_source=x"))
    assert called == []                      # the expensive path never ran
    assert got["overall"]["score"] == 71     # served the stored canonical analysis


def test_check_url_rejects_bad_schemes_and_ports():
    import asyncio

    assert asyncio.run(sa.check_url("ftp://example.com/x")) is not None
    assert asyncio.run(sa.check_url("https://example.com:8443/x")) is not None
    assert asyncio.run(sa.check_url("https:///nohost")) is not None


# --- freshness -----------------------------------------------------------------------------------


def test_fresh_window():
    now = datetime.now(timezone.utc)
    assert sa._fresh(now.isoformat()) is True
    old = (now - timedelta(seconds=sa._TTL_S + 60)).isoformat()
    assert sa._fresh(old) is False
    assert sa._fresh(None) is False
    assert sa._fresh("not-a-date") is False


# --- the SSE endpoint, end-to-end with faked seams ------------------------------------------------


class FakePool:
    async def fetch(self, *_a, **_k):
        return []

    async def fetchrow(self, *_a, **_k):
        return None

    async def fetchval(self, *_a, **_k):
        return 1


def _app():
    app = FastAPI()
    app.include_router(sa.analyse_router)
    app.state.pool = FakePool()
    app.state.nats = None
    return app


def test_post_analyse_streams_claims_then_done(monkeypatch):
    monkeypatch.setattr(sa, "_LIMITER", PerIpRateLimiter(capacity=100, refill_per_sec=100))
    sa._RESULTS.clear()

    async def host_ok(_h, _p):
        return True

    monkeypatch.setattr(sa, "_host_is_public", host_ok)
    _patch_page(monkeypatch)

    reading = _reading(central=True, matched_cluster_id=None)
    analysis = _analysis([reading], StoryScore(70, "corroborated", "Corroborated",
                                               ["x"], False, False))

    def fake_engine(url, **kw):
        progress = kw.get("progress")
        progress("fetched", {"title": "T", "source": "example.com", "language": "en",
                             "date": None})
        progress("claim", {"index": 0, "total": 1, "reading": reading})
        progress("scored", {"score": 70})
        return analysis

    monkeypatch.setattr(sa, "analyse_article", fake_engine)

    client = TestClient(_app())
    r = client.post("/api/v2/analyse", json={"url": "https://example.com/story"})
    assert r.status_code == 200
    text = r.text
    assert "event: start" in text
    assert "event: fetched" in text
    assert "event: claim" in text
    assert "event: done" in text
    assert "event: scored" not in text          # folded into done
    assert "cl-secret" not in text              # mechanism never crosses the wire
    assert "matched_cluster_id" not in text
    # …and the finished analysis is now served by id.
    aid = sa.analysis_id("https://example.com/story")
    got = client.get(f"/api/v2/analyse/{aid}")
    assert got.status_code == 200
    assert got.json()["analysis"]["overall"]["band"] == "corroborated"


def test_internal_errors_never_leak_to_the_wire(monkeypatch):
    monkeypatch.setattr(sa, "_LIMITER", PerIpRateLimiter(capacity=100, refill_per_sec=100))
    sa._RESULTS.clear()

    async def host_ok(_h, _p):
        return True

    monkeypatch.setattr(sa, "_host_is_public", host_ok)
    _patch_page(monkeypatch)

    def exploding_engine(url, **kw):
        raise ValueError("no JSON array in model output: 'SECRET INTERNALS'")

    monkeypatch.setattr(sa, "analyse_article", exploding_engine)
    client = TestClient(_app())
    r = client.post("/api/v2/analyse", json={"url": "https://example.com/story"})
    assert "event: error" in r.text
    assert "SECRET INTERNALS" not in r.text
    assert "analysis failed" in r.text


def test_post_analyse_bad_url_yields_error_event(monkeypatch):
    monkeypatch.setattr(sa, "_LIMITER", PerIpRateLimiter(capacity=100, refill_per_sec=100))
    sa._RESULTS.clear()
    client = TestClient(_app())
    r = client.post("/api/v2/analyse", json={"url": "ftp://example.com/story"})
    assert r.status_code == 200  # SSE stream carries the error event
    assert "event: error" in r.text
    assert "only http(s)" in r.text


def test_post_analyse_rate_limited(monkeypatch):
    monkeypatch.setattr(sa, "_LIMITER", PerIpRateLimiter(capacity=0.0, refill_per_sec=0.001))
    client = TestClient(_app())
    r = client.post("/api/v2/analyse", json={"url": "https://example.com/story"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_get_unknown_analysis_404s():
    sa._RESULTS.clear()
    client = TestClient(_app())
    assert client.get("/api/v2/analyse/an-nope").status_code == 404
