"""Tests for the public-API hardening (#280): per-IP rate limiter + body-size cap, and the wiring
of both onto the real reader app's open endpoints (/api/translate, /api/feedback).

Two layers:
  * unit — the token bucket, the per-IP registry (LRU + injected clock), client-ip resolution, and
    each ASGI middleware against a tiny throwaway app (deterministic, no DB);
  * wiring — the REAL app via TestClient with no lifespan (the test_social_api pattern), asserting
    oversized text → 422, oversized body → 413, a per-IP burst → 429, and the happy paths still 200.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from maat.serving.ratelimit import (
    MaxBodySizeMiddleware,
    PerIpRateLimiter,
    RateLimitMiddleware,
    _TokenBucket,
    client_ip,
)

# ── token bucket / per-IP limiter (deterministic clock) ──────────────────────────────────────────


def test_token_bucket_allows_capacity_then_blocks():
    b = _TokenBucket(capacity=2, refill_per_sec=1.0, tokens=2, updated=0.0)
    assert b.allow(0.0)
    assert b.allow(0.0)
    assert not b.allow(0.0)  # drained


def test_token_bucket_refills_over_time():
    b = _TokenBucket(capacity=2, refill_per_sec=1.0, tokens=0, updated=0.0)
    assert not b.allow(0.0)
    assert b.allow(1.0)  # one token after a second
    assert not b.allow(1.0)
    assert b.allow(3.0)  # caps at capacity, not unbounded accrual
    assert b.allow(3.0)
    assert not b.allow(3.0)


def test_per_ip_independent():
    t = [0.0]
    lim = PerIpRateLimiter(capacity=1, refill_per_sec=0.0, monotonic=lambda: t[0])
    assert lim.allow("a")
    assert not lim.allow("a")  # a drained
    assert lim.allow("b")  # b has its own bucket


def test_per_ip_lru_eviction():
    t = [0.0]
    lim = PerIpRateLimiter(capacity=1, refill_per_sec=0.0, max_ips=2, monotonic=lambda: t[0])
    assert lim.allow("a")  # a drained
    assert lim.allow("b")  # b drained
    assert lim.allow("c")  # inserting c evicts the coldest (a); c is fresh → allowed
    assert len(lim._buckets) == 2
    # a was evicted, so it gets a fresh full bucket — if it hadn't been evicted it'd still be drained.
    assert lim.allow("a")


def test_retry_after_hint():
    assert PerIpRateLimiter(refill_per_sec=1.0).retry_after() == 1
    assert PerIpRateLimiter(refill_per_sec=0.5).retry_after() == 2
    assert PerIpRateLimiter(refill_per_sec=0.0).retry_after() == 1  # guarded against div-by-zero


# ── client-ip resolution ─────────────────────────────────────────────────────────────────────────


def test_client_ip_prefers_forwarded_for():
    scope = {"headers": [(b"x-forwarded-for", b"1.2.3.4, 10.0.0.1")], "client": ("10.0.0.9", 55)}
    assert client_ip(scope) == "1.2.3.4"


def test_client_ip_falls_back_to_peer():
    assert client_ip({"headers": [], "client": ("9.8.7.6", 55)}) == "9.8.7.6"
    assert client_ip({"headers": []}) == "unknown"


# ── tiny ASGI apps for the middleware unit tests ─────────────────────────────────────────────────


async def _echo(scope, receive, send):
    """Drain the request body and reply 200 with its byte count."""
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": str(len(body)).encode()})


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_body_cap_allows_under_limit():
    client = TestClient(MaxBodySizeMiddleware(_echo, max_bytes=10))
    r = client.post("/", content=b"xxxxx")
    assert r.status_code == 200
    assert r.text == "5"


def test_body_cap_rejects_oversized_content_length():
    client = TestClient(MaxBodySizeMiddleware(_echo, max_bytes=10))
    r = client.post("/", content=b"x" * 50)  # httpx sets Content-Length → fast reject
    assert r.status_code == 413


def test_body_cap_rejects_oversized_streamed_without_length():
    client = TestClient(MaxBodySizeMiddleware(_echo, max_bytes=10))

    def gen():
        yield b"x" * 6
        yield b"x" * 6  # 12 > 10, chunked (no Content-Length) → streamed reject

    r = client.post("/", content=gen())
    assert r.status_code == 413


def test_rate_limit_429_after_burst_with_retry_after():
    t = [0.0]
    lim = PerIpRateLimiter(capacity=2, refill_per_sec=0.0, monotonic=lambda: t[0])
    client = TestClient(RateLimitMiddleware(_ok, limiter=lim, prefixes=("/lim",)))
    h = {"x-forwarded-for": "5.5.5.5"}
    assert client.post("/lim", headers=h).status_code == 200
    assert client.post("/lim", headers=h).status_code == 200
    blocked = client.post("/lim", headers=h)
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after") == "1"


def test_rate_limit_leaves_other_paths_alone():
    t = [0.0]
    lim = PerIpRateLimiter(capacity=1, refill_per_sec=0.0, monotonic=lambda: t[0])
    client = TestClient(RateLimitMiddleware(_ok, limiter=lim, prefixes=("/lim",)))
    h = {"x-forwarded-for": "5.5.5.5"}
    for _ in range(5):
        assert client.get("/elsewhere", headers=h).status_code == 200  # never throttled


def test_rate_limit_distinct_ips_independent():
    t = [0.0]
    lim = PerIpRateLimiter(capacity=1, refill_per_sec=0.0, monotonic=lambda: t[0])
    client = TestClient(RateLimitMiddleware(_ok, limiter=lim, prefixes=("/lim",)))
    assert client.post("/lim", headers={"x-forwarded-for": "1.1.1.1"}).status_code == 200
    assert client.post("/lim", headers={"x-forwarded-for": "1.1.1.1"}).status_code == 429
    assert client.post("/lim", headers={"x-forwarded-for": "2.2.2.2"}).status_code == 200  # other IP fine


# ── wiring onto the real app (no lifespan; the test_social_api pattern) ───────────────────────────


def _client() -> TestClient:
    from maat.web import app as appmod

    return TestClient(appmod.app)


def test_translate_oversized_text_is_422():
    r = _client().post(
        "/api/translate", json={"text": "x" * 3000}, headers={"x-forwarded-for": "3.0.0.1"}
    )
    assert r.status_code == 422  # Pydantic max_length — never reaches the provider


def test_feedback_oversized_text_is_422():
    r = _client().post(
        "/api/feedback", json={"text": "x" * 6000}, headers={"x-forwarded-for": "3.0.0.2"}
    )
    assert r.status_code == 422


def test_oversized_body_is_413_on_the_real_app():
    # Just over the 1 MiB default cap → the body middleware rejects before parsing (so the bytes need
    # not even be valid JSON). Proves the cap is wired onto the real app.
    big = b"x" * (1024 * 1024 + 1)
    r = _client().post(
        "/api/feedback", content=big,
        headers={"content-type": "application/json", "x-forwarded-for": "3.0.0.3"},
    )
    assert r.status_code == 413


def test_translate_happy_path_still_works(monkeypatch):
    from maat.web import app as appmod

    monkeypatch.setattr(appmod, "translate_text", lambda text, target, source: ("hola", "stub"))
    r = TestClient(appmod.app).post(
        "/api/translate", json={"text": "hello", "target": "es"}, headers={"x-forwarded-for": "1.0.0.1"}
    )
    assert r.status_code == 200
    assert r.json()["translated"] == "hola"


def test_feedback_happy_path_still_works(monkeypatch):
    from maat.web import app as appmod

    async def _fake_record(pool, nats, *, text, category_hint, source):
        return "fb-123"

    monkeypatch.setattr(appmod, "feedback_record", _fake_record)
    appmod.app.state.pool = object()  # not touched by the fake
    r = TestClient(appmod.app).post(
        "/api/feedback", json={"text": "great app"}, headers={"x-forwarded-for": "2.0.0.1"}
    )
    assert r.status_code == 200
    assert r.json()["item_id"] == "fb-123"


def test_per_ip_burst_is_throttled_on_the_real_app(monkeypatch):
    from maat.web import app as appmod

    monkeypatch.setattr(appmod, "translate_text", lambda text, target, source: ("x", "stub"))
    client = TestClient(appmod.app)
    h = {"x-forwarded-for": "9.9.9.9"}  # a dedicated IP so this can't interfere with other tests
    codes = [
        client.post("/api/translate", json={"text": "hi"}, headers=h).status_code
        for _ in range(int(appmod.ratelimit.DEFAULT_BURST) + 5)
    ]
    assert codes[0] == 200  # the first request is allowed
    assert 429 in codes  # a sustained burst from one IP is eventually throttled
