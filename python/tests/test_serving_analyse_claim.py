"""Tests for claim-mode serving (P16 #450) — input routing, claim payload discipline, the SSE
endpoint end-to-end with a faked engine, and the ``cl-`` cache identity.

The wire discipline under test: the reader's RAW input never crosses back out (only the
canonical "We checked" line does), no publisher block exists in claim mode, and one POST body
field decides the mode — with URL-shaped text routed to the article path, because "weigh this
URL as a string of words" is never what a reader who pasted a link meant.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

import maat.serving.analyse as sa
from maat.learning.story_credibility import StoryScore
from maat.pipeline.analyse import ClaimAnalysis, ClaimReading
from maat.pipeline.claim import Claim
from maat.serving.ratelimit import PerIpRateLimiter

_RAW = "i heard elon and tim cook merged spacex and apple???"
_CANON = "Elon Musk and Tim Cook agreed to merge SpaceX and Apple."


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


def _reading(**kw):
    return ClaimReading(
        claim=Claim(id="typed-0", text=kw.pop("text", _CANON.rstrip(".")), voice="own",
                    in_headline=True, evidence_span="", kind="fact"),
        extremity=kw.pop("extremity", "extraordinary"),
        confidence=kw.pop("confidence", 0.0),
        independent_originators=kw.pop("originators", 0),
        has_primary=kw.pop("has_primary", False),
        disputed=kw.pop("disputed", False),
        grounding=kw.pop("grounding", None),
        verdict=kw.pop("verdict", "No credible support found — below the bar for an extraordinary claim"),
        tier=kw.pop("tier", "floor"),
        matched_cluster_id=kw.pop("matched_cluster_id", None),
    )


def _claim_analysis(**kw):
    return ClaimAnalysis(
        input_text=kw.pop("input_text", _RAW),
        display=kw.pop("display", _CANON),
        language="en",
        facts=kw.pop("facts", [_reading()]),
        projections=kw.pop("projections", []),
        score=kw.pop("score", StoryScore(8, "disqualified", "No credible support",
                                         ["a extraordinary claim with no independent support"],
                                         False, False)),
        live=kw.pop("live", None),
    )


# --- ids --------------------------------------------------------------------------------------


def test_claim_analysis_id_stable_across_case_and_spacing():
    a = sa.claim_analysis_id("The vote  was postponed")
    b = sa.claim_analysis_id("the vote was POSTPONED")
    assert a == b
    assert a.startswith("cl-")
    assert sa.claim_analysis_id("another claim") != a


# --- payload discipline -----------------------------------------------------------------------


def test_claim_public_payload_shape_and_raw_input_never_leaks():
    analysis = _claim_analysis()
    p = sa.claim_public_payload(analysis, "cl-abc")
    assert p["kind"] == "claim"
    assert p["checked"] == {"display": _CANON, "language": "en"}
    assert "publisher" not in p          # nobody is publishing — the reader asked
    assert "url" not in p
    assert p["overall"]["unchecked"] == 0
    assert p["scope"] == sa.CLAIM_SCOPE_LINE
    assert set(p["share"]) == {"headline", "tally", "og_title", "og_description",
                               "twitter_text", "linkedin_text", "instagram_caption"}
    # The reader's raw wording stays server-side — canonical claim only, everywhere.
    import json as _json

    dumped = _json.dumps(p)
    assert _RAW not in dumped
    assert _CANON in dumped


def test_claim_share_copy_quotes_the_canonical_claim():
    share = sa.claim_share_copy(_claim_analysis(), ["no independent support"])
    assert "No credible support · 8/100" in share["headline"]
    assert _CANON[:40] in share["og_title"]
    assert "maat.press/analyse" in share["linkedin_text"]


def test_article_payload_now_says_kind_article():
    # The UI switches on ``kind`` — the article payload declares itself too.
    import inspect

    src = inspect.getsource(sa.public_payload)
    assert '"kind": "article"' in src


# --- endpoint routing -------------------------------------------------------------------------


def _post(client, body):
    return client.post("/api/v2/analyse", json=body)


def test_url_and_text_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(sa, "_LIMITER", PerIpRateLimiter(capacity=100, refill_per_sec=100))
    client = TestClient(_app())
    both = _post(client, {"url": "https://example.com/story", "text": "a claim"})
    neither = _post(client, {})
    assert both.status_code == 422
    assert neither.status_code == 422
    assert "exactly one" in both.json()["detail"]


def test_url_shaped_text_routes_to_the_article_path(monkeypatch):
    monkeypatch.setattr(sa, "_LIMITER", PerIpRateLimiter(capacity=100, refill_per_sec=100))
    seen = {}

    async def fake_run_analysis(_state, url, *, refresh=False, progress=None):
        seen["url"] = url
        return {"kind": "article", "analysed_at": "now"}

    async def must_not_run(*_a, **_k):  # pragma: no cover - the assertion
        raise AssertionError("claim path must not run for URL-shaped text")

    monkeypatch.setattr(sa, "run_analysis", fake_run_analysis)
    monkeypatch.setattr(sa, "run_claim_analysis", must_not_run)

    client = TestClient(_app())
    r = _post(client, {"text": "www.example.com/story-about-a-vote"})
    assert r.status_code == 200
    assert seen["url"] == "https://www.example.com/story-about-a-vote"
    assert "event: done" in r.text
    # …and the start event carries the ARTICLE id for it.
    assert sa.analysis_id("https://www.example.com/story-about-a-vote") in r.text


def test_typed_claim_streams_understood_then_done(monkeypatch):
    monkeypatch.setattr(sa, "_LIMITER", PerIpRateLimiter(capacity=100, refill_per_sec=100))
    sa._RESULTS.clear()

    reading = _reading()
    analysis = _claim_analysis(facts=[reading])

    def fake_engine(text, **kw):
        progress = kw.get("progress")
        progress("understood", {"display": _CANON, "language": "en",
                                "claims": [{"text": _CANON.rstrip("."), "kind": "fact"}]})
        progress("searching", {"claims": 1})
        progress("checking", {"index": 0})
        progress("claim", {"index": 0, "total": 1, "reading": reading})
        progress("scored", {"score": 8})
        return analysis

    async def fake_load_assets(_pool):
        return sa._Assets(facts=[], claim_ids={}, embeds=None, reputation={}, denied=set(),
                          ownership={})

    monkeypatch.setattr(sa, "_load_assets", fake_load_assets)
    monkeypatch.setattr(sa, "make_nli", lambda: None)
    monkeypatch.setattr(sa, "analyse_claim", fake_engine)

    client = TestClient(_app())
    r = _post(client, {"text": _RAW})
    assert r.status_code == 200
    text = r.text
    assert "event: start" in text
    assert sa.claim_analysis_id(_RAW) in text
    assert "event: understood" in text
    assert "event: claim" in text
    assert "event: done" in text
    assert "event: scored" not in text     # folded into done
    assert _RAW not in text                # raw input never crosses back out
    assert _CANON in text

    # …and the finished claim analysis is served by its cl- id (in-process cache).
    aid = sa.claim_analysis_id(_RAW)
    got = client.get(f"/api/v2/analyse/{aid}")
    assert got.status_code == 200
    payload = got.json()["analysis"]
    assert payload["kind"] == "claim"
    assert payload["overall"]["label"] == "No credible support"


def test_claim_cache_serves_fresh_repeat_without_engine(monkeypatch):
    monkeypatch.setattr(sa, "_LIMITER", PerIpRateLimiter(capacity=100, refill_per_sec=100))
    sa._RESULTS.clear()
    calls = {"n": 0}

    def fake_engine(text, **kw):
        calls["n"] += 1
        return _claim_analysis()

    async def fake_load_assets(_pool):
        return sa._Assets(facts=[], claim_ids={}, embeds=None, reputation={}, denied=set(),
                          ownership={})

    monkeypatch.setattr(sa, "_load_assets", fake_load_assets)
    monkeypatch.setattr(sa, "make_nli", lambda: None)
    monkeypatch.setattr(sa, "analyse_claim", fake_engine)

    client = TestClient(_app())
    first = _post(client, {"text": "The Vote Was Postponed"})
    second = _post(client, {"text": "the vote  was postponed"})  # same claim, different typing
    assert first.status_code == second.status_code == 200
    assert calls["n"] == 1                 # the second run came from cache
    assert "event: done" in second.text


def test_claim_error_is_user_facing_on_the_stream(monkeypatch):
    monkeypatch.setattr(sa, "_LIMITER", PerIpRateLimiter(capacity=100, refill_per_sec=100))
    sa._RESULTS.clear()

    async def fake_run(_state, _text, *, refresh=False, progress=None):
        raise sa.AnalyseError(
            "Maat weighs claims about public figures, organisations, and events — it doesn't "
            "check claims about private individuals."
        )

    monkeypatch.setattr(sa, "run_claim_analysis", fake_run)
    client = TestClient(_app())
    r = _post(client, {"text": "my neighbour stole a car"})
    assert r.status_code == 200
    assert "event: error" in r.text
    assert "private individuals" in r.text


def test_understood_progress_is_mapped_not_passed_through():
    out = sa._public_progress("understood", {
        "display": _CANON, "language": "en",
        "claims": [{"text": "X", "kind": "fact", "internal_debug": "leak"}],
        "internal": "leak",
    })
    assert out == {"display": _CANON, "language": "en",
                   "claims": [{"text": "X", "kind": "fact"}]}
