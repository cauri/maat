"""Tests for the Analyse serving layer (P14 #365) — url identity, public payload discipline
("what, not how"), reasons wording, live-search helpers, and the SSE endpoint end-to-end with
faked engine + DB seams.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from starlette.testclient import TestClient

import maat.serving.analyse as sa
from maat.learning.story_credibility import StoryScore
from maat.pipeline.analyse import ArticleAnalysis, ClaimReading, LiveCandidate
from maat.pipeline.claim import Claim
from maat.serving.ratelimit import PerIpRateLimiter

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
        url="https://example.com/story", source="example.com",
        title="T", language="en", image=None, date=None,
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
