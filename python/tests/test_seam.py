"""Provider-seam tests — Mistral embeddings batching + transient-failure retry.

Two regressions:
  * Batching: mistral_embed sent the whole corpus in one request, which 400'd once the live feed
    grew past ~74 claims. It must chunk the inputs.
  * Retry: a single un-retried 429 (Mistral rate-limits under the pipeline's bursty load) crashed
    a whole corroborate run. Transient statuses must back off and retry; non-retryable ones must
    still fail fast.
"""

from __future__ import annotations

import httpx
import pytest

from maat.providers import seam


class _FakeResp:
    def __init__(self, n: int = 0, *, status_code: int = 200, headers: dict | None = None,
                 payload: dict | None = None):
        self._n = n
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self.request = httpx.Request("POST", "https://example.test")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=self.request, response=self  # type: ignore[arg-type]
            )

    def json(self) -> dict:
        if self._payload is not None:
            return self._payload
        # One small embedding per input; usage echoes the chunk size.
        return {
            "data": [{"embedding": [float(i), 0.0, 0.0]} for i in range(self._n)],
            "usage": {"prompt_tokens": self._n},
        }


def test_mistral_embed_batches_large_input(monkeypatch):
    """130 inputs are chunked into 64/64/2 requests and concatenated in order."""
    calls: list[int] = []

    def fake_post(url, *, headers=None, json=None, timeout=None):
        calls.append(len(json["input"]))
        return _FakeResp(len(json["input"]))

    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    monkeypatch.setattr(seam.httpx, "post", fake_post)
    monkeypatch.setattr(seam, "_MISTRAL_LIMIT", seam._RateLimiter(0, 0))  # throttle off: no real-sleep here

    out = seam.mistral_embed([f"claim {i}" for i in range(130)])

    assert len(out) == 130
    assert calls == [64, 64, 2]  # _EMBED_BATCH = 64, no single over-limit request
    assert all(len(vec) == 3 for vec in out)


def test_mistral_embed_empty_makes_no_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the API for empty input")

    monkeypatch.setenv("MISTRAL_API_KEY", "test")
    monkeypatch.setattr(seam.httpx, "post", boom)

    assert seam.mistral_embed([]) == []


def test_post_json_retries_on_429_then_succeeds(monkeypatch):
    statuses = [429, 503, 200]
    calls = {"n": 0}

    def fake_post(url, *, headers=None, json=None, timeout=None):
        s = statuses[calls["n"]]
        calls["n"] += 1
        return _FakeResp(status_code=s, headers={}, payload={"ok": True})

    monkeypatch.setattr(seam.httpx, "post", fake_post)
    monkeypatch.setattr(seam.time, "sleep", lambda *_: None)  # don't actually wait

    out = seam._post_json("https://x", headers={}, payload={}, timeout=seam._TIMEOUT)
    assert out == {"ok": True}
    assert calls["n"] == 3  # 429 → 503 → 200


def test_post_json_raises_after_exhausting_retries(monkeypatch):
    calls = {"n": 0}

    def always_429(*a, **k):
        calls["n"] += 1
        return _FakeResp(status_code=429, headers={})

    monkeypatch.setattr(seam.httpx, "post", always_429)
    monkeypatch.setattr(seam.time, "sleep", lambda *_: None)
    monkeypatch.setattr(seam, "_MAX_RETRIES", 2)

    with pytest.raises(httpx.HTTPStatusError):
        seam._post_json("https://x", headers={}, payload={}, timeout=seam._TIMEOUT)
    assert calls["n"] == 3  # initial + 2 retries


def test_post_json_does_not_retry_client_errors(monkeypatch):
    calls = {"n": 0}

    def bad_request(*a, **k):
        calls["n"] += 1
        return _FakeResp(status_code=400, headers={})

    monkeypatch.setattr(seam.httpx, "post", bad_request)
    monkeypatch.setattr(seam.time, "sleep", lambda *_: None)

    with pytest.raises(httpx.HTTPStatusError):
        seam._post_json("https://x", headers={}, payload={}, timeout=seam._TIMEOUT)
    assert calls["n"] == 1  # 400 is not transient — fail fast, no retry


def test_post_json_honors_retry_after_header(monkeypatch):
    slept: list[float] = []
    statuses = [429, 200]
    calls = {"n": 0}

    def fake_post(*a, **k):
        s = statuses[calls["n"]]
        calls["n"] += 1
        return _FakeResp(status_code=s, headers={"retry-after": "7"}, payload={"ok": 1})

    monkeypatch.setattr(seam.httpx, "post", fake_post)
    monkeypatch.setattr(seam.time, "sleep", lambda d: slept.append(d))

    seam._post_json("https://x", headers={}, payload={}, timeout=seam._TIMEOUT)
    assert slept == [7.0]  # server-directed delay used verbatim


def _fake_clock():
    """An injected monotonic+sleep pair driving a deterministic clock (never touches global time,
    which would leak across the suite and real-sleep it)."""
    clock = {"t": 100.0}
    slept: list[float] = []

    def sleep(d):
        slept.append(d)
        clock["t"] += d  # time only moves forward by what we sleep

    return (lambda: clock["t"]), sleep, slept


def test_token_bucket_caps_sustained_rate():
    # A full bucket bursts immediately; once drained, the refill rate caps the SUSTAINED rate (#300).
    monotonic, sleep, slept = _fake_clock()
    bucket = seam._TokenBucket(rate_per_sec=10.0, capacity=10.0, monotonic=monotonic, sleep=sleep)

    for _ in range(10):  # full bucket → 10 immediate, no wait
        bucket.acquire(1)
    assert slept == []

    for _ in range(5):  # drained → each token refills at 10/sec → 0.1s apart, 0.5s total
        bucket.acquire(1)
    assert sum(slept) == pytest.approx(0.5)


def test_token_bucket_rate_zero_is_disabled():
    slept: list[float] = []
    bucket = seam._TokenBucket(rate_per_sec=0.0, capacity=0.0, sleep=lambda d: slept.append(d))
    for _ in range(100):
        bucket.acquire(1)  # disabled → never throttles
    assert slept == []


def test_rate_limiter_throttles_once_request_budget_drains():
    monotonic, sleep, slept = _fake_clock()
    lim = seam._RateLimiter(rpm=60.0, tpm=0.0, monotonic=monotonic, sleep=sleep)  # 60/min → burst 60
    for _ in range(60):  # drain the full burst — no wait
        lim.acquire()
    assert slept == []
    lim.acquire()  # 61st within the minute → must wait for a refill (1/sec)
    assert sum(slept) == pytest.approx(1.0)


def test_rate_limiter_disabled_when_zero():
    def boom(_):
        raise AssertionError("slept")

    seam._RateLimiter(rpm=0, tpm=0, sleep=boom).acquire(est_tokens=99999)  # both arms off → no-op


# ── Per-stage, multi-endpoint Claude routing (#300) ──────────────────────────────────────────────
# Spread LLM calls over several Anthropic endpoints (each its own _RateLimiter) so aggregate RPM/TPM
# = the sum of the tiers, routed per stage. No live calls — selection + env parsing only.


def test_endpoint_headers_x_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    h = seam._Endpoint("a").headers()
    assert h["x-api-key"] == "sk-test"
    assert h["anthropic-version"] == seam._DEFAULT_ANTHROPIC_VERSION
    assert "Authorization" not in h


def test_endpoint_headers_bearer_and_custom_key_env(monkeypatch):
    # "bearer" (an Anthropic-compatible gateway) + a per-endpoint key var + an extra header.
    monkeypatch.setenv("GW_KEY", "gw-secret")
    ep = seam._Endpoint("eu", auth="bearer", key_env="GW_KEY", extra_headers=(("x-region", "eu"),))
    h = ep.headers()
    assert h["Authorization"] == "Bearer gw-secret"
    assert "x-api-key" not in h
    assert h["x-region"] == "eu"


def test_endpoint_rejects_unknown_auth():
    with pytest.raises(ValueError, match="auth"):
        seam._Endpoint("a", auth="sigv4")


def test_endpoint_key_read_lazily_at_call_time(monkeypatch):
    # No key at construction is fine; headers() raises KeyError exactly like the old single path.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ep = seam._Endpoint("a")
    with pytest.raises(KeyError):
        ep.headers()


def test_default_router_is_one_anthropic_endpoint(monkeypatch):
    monkeypatch.delenv("MAAT_CLAUDE_ENDPOINTS", raising=False)
    monkeypatch.delenv("MAAT_CLAUDE_STAGE_ROUTES", raising=False)
    router = seam._build_claude_router()
    ep = router.pick("whatever")  # unknown stage → default route
    assert ep.name == "anthropic" and ep.url == seam.ANTHROPIC_URL
    # Every stage resolves to the same single endpoint — behaviour is unchanged.
    assert router.pick("extract") is router.pick("judge")


def test_build_router_from_env_endpoints(monkeypatch):
    monkeypatch.setenv(
        "MAAT_CLAUDE_ENDPOINTS",
        '[{"name":"a","rpm":120},'
        '{"name":"b","key_env":"K2","auth":"bearer","url":"https://gw/x"}]',
    )
    monkeypatch.delenv("MAAT_CLAUDE_STAGE_ROUTES", raising=False)
    router = seam._build_claude_router()
    assert {ep.name for ep in router.default.endpoints} == {"a", "b"}
    b = next(ep for ep in router.default.endpoints if ep.name == "b")
    assert b.auth == "bearer" and b.key_env == "K2" and b.url == "https://gw/x"


def test_endpoints_invalid_json_raises(monkeypatch):
    monkeypatch.setenv("MAAT_CLAUDE_ENDPOINTS", "{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        seam._build_claude_router()


def test_endpoints_duplicate_name_raises(monkeypatch):
    monkeypatch.setenv("MAAT_CLAUDE_ENDPOINTS", '[{"name":"a"},{"name":"a"}]')
    with pytest.raises(ValueError, match="duplicate"):
        seam._build_claude_router()


def test_stage_route_unknown_endpoint_raises(monkeypatch):
    monkeypatch.setenv("MAAT_CLAUDE_ENDPOINTS", '[{"name":"a"}]')
    monkeypatch.setenv("MAAT_CLAUDE_STAGE_ROUTES", '{"extract":{"endpoints":["zzz"]}}')
    with pytest.raises(ValueError, match="unknown endpoint"):
        seam._build_claude_router()


def test_stage_route_bad_policy_raises(monkeypatch):
    monkeypatch.setenv("MAAT_CLAUDE_ENDPOINTS", '[{"name":"a"}]')
    monkeypatch.setenv("MAAT_CLAUDE_STAGE_ROUTES", '{"extract":{"endpoints":["a"],"policy":"nope"}}')
    with pytest.raises(ValueError, match="policy"):
        seam._build_claude_router()


def test_round_robin_cycles_endpoints():
    route = seam._Route([seam._Endpoint(n) for n in "abc"], "round-robin")
    assert [route.pick().name for _ in range(7)] == ["a", "b", "c", "a", "b", "c", "a"]


def test_least_loaded_shifts_to_endpoint_with_more_spare_budget():
    mono, sleep, _ = _fake_clock()
    a = seam._Endpoint("a", limiter=seam._RateLimiter(120, 0, monotonic=mono, sleep=sleep))
    b = seam._Endpoint("b", limiter=seam._RateLimiter(120, 0, monotonic=mono, sleep=sleep))
    route = seam._Route([a, b], "least-loaded")
    for _ in range(100):  # drain b's budget (120 → 20); a stays full at 120
        b.acquire()
    assert route.pick().name == "a"  # a now has the most spare budget → least likely to block


def test_least_loaded_ties_rotate():
    # Two unthrottled endpoints (both ∞ available) tie → fall through to round-robin, still spread.
    route = seam._Route([seam._Endpoint("a"), seam._Endpoint("b")], "least-loaded")
    assert [route.pick().name for _ in range(4)] == ["a", "b", "a", "b"]


def test_aggregate_budget_is_the_sum_of_endpoint_tiers():
    # Two 60-rpm endpoints, round-robin: the pool absorbs 120 immediate requests with no wait, where
    # a single 60-rpm tier blocks at the 61st (see test_rate_limiter_throttles…). Aggregate = sum.
    mono, sleep, slept = _fake_clock()
    a = seam._Endpoint("a", limiter=seam._RateLimiter(60, 0, monotonic=mono, sleep=sleep))
    b = seam._Endpoint("b", limiter=seam._RateLimiter(60, 0, monotonic=mono, sleep=sleep))
    route = seam._Route([a, b], "round-robin")
    for _ in range(120):
        route.pick().acquire()
    assert slept == []  # 120 within the minute, none blocked — the two tiers summed
    route.pick().acquire()  # 121st drains a's half → must wait one refill (60/min → 1/sec)
    assert sum(slept) == pytest.approx(1.0)


def test_per_stage_routes_isolate_endpoints(monkeypatch):
    monkeypatch.setenv("MAAT_CLAUDE_ENDPOINTS", '[{"name":"a"},{"name":"b"}]')
    monkeypatch.setenv(
        "MAAT_CLAUDE_STAGE_ROUTES",
        '{"extract":{"endpoints":["a","b"]},"default":{"endpoints":["a"]}}',
    )
    router = seam._build_claude_router()
    assert {router.pick("judge").name for _ in range(10)} == {"a"}  # low-volume → pinned to one
    assert {router.pick("extract").name for _ in range(10)} == {"a", "b"}  # heavy stage spreads


def test_unconfigured_stage_routes_spread_across_all_endpoints(monkeypatch):
    monkeypatch.setenv("MAAT_CLAUDE_ENDPOINTS", '[{"name":"a"},{"name":"b"}]')
    monkeypatch.delenv("MAAT_CLAUDE_STAGE_ROUTES", raising=False)
    router = seam._build_claude_router()
    assert {router.pick("judge").name for _ in range(8)} == {"a", "b"}  # no cfg → spread everything


def test_stage_route_accepts_bare_list_form(monkeypatch):
    monkeypatch.setenv("MAAT_CLAUDE_ENDPOINTS", '[{"name":"a"},{"name":"b"}]')
    monkeypatch.setenv("MAAT_CLAUDE_STAGE_ROUTES", '{"extract":["a","b"]}')
    router = seam._build_claude_router()
    assert {router.pick("extract").name for _ in range(6)} == {"a", "b"}


def test_with_zero_limits_disables_throttling():
    def boom(_):
        raise AssertionError("slept")

    a = seam._Endpoint("a", limiter=seam._RateLimiter(1, 0, sleep=boom))  # rpm 1 → throttles fast
    router = seam._Router({}, seam._Route([a], "round-robin"))
    for _ in range(50):
        router.with_zero_limits().pick("x").acquire(1)  # zeroed clone → never throttles
    with pytest.raises(AssertionError):  # but the original still would (proves the clone differs)
        for _ in range(5):
            a.acquire(1)


def test_claude_complete_routes_to_configured_bearer_endpoint(monkeypatch):
    # End-to-end: a configured stage route sends the call to its endpoint's URL with its auth.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "primary")
    monkeypatch.setenv("GW_KEY", "gw-secret")
    monkeypatch.setenv(
        "MAAT_CLAUDE_ENDPOINTS",
        '[{"name":"gw","auth":"bearer","key_env":"GW_KEY","url":"https://gw.test/v1/messages"}]',
    )
    monkeypatch.delenv("MAAT_CLAUDE_STAGE_ROUTES", raising=False)
    monkeypatch.setattr(seam, "_CLAUDE_ROUTER", seam._build_claude_router())  # rebuild with env

    seen: dict = {}

    def fake_post(url, *, headers=None, json=None, timeout=None):
        seen["url"], seen["headers"] = url, headers
        return _FakeResp(payload={"content": [{"text": "ok"}], "usage": {}})

    monkeypatch.setattr(seam.httpx, "post", fake_post)
    reply = seam.claude_complete("hi", stage="extract")
    assert reply.text == "ok"
    assert seen["url"] == "https://gw.test/v1/messages"
    assert seen["headers"]["Authorization"] == "Bearer gw-secret"
    assert "x-api-key" not in seen["headers"]


def test_claude_complete_default_endpoint_path_unchanged(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "primary")
    monkeypatch.delenv("MAAT_CLAUDE_ENDPOINTS", raising=False)
    monkeypatch.delenv("MAAT_CLAUDE_STAGE_ROUTES", raising=False)
    monkeypatch.setattr(seam, "_CLAUDE_ROUTER", seam._build_claude_router())

    seen: dict = {}

    def fake_post(url, *, headers=None, json=None, timeout=None):
        seen["url"], seen["headers"] = url, headers
        return _FakeResp(payload={"content": [{"text": "ok"}], "usage": {}})

    monkeypatch.setattr(seam.httpx, "post", fake_post)
    seam.claude_complete("hi", stage="extract")  # any stage → the one default endpoint
    assert seen["url"] == seam.ANTHROPIC_URL
    assert seen["headers"]["x-api-key"] == "primary"
