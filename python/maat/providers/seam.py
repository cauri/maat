"""The provider seam: Claude + Mistral behind one interface (DECISIONS D7).

LLMs are one kind of "Source" (PLAN §2.3). Agent logic names a *capability*
(judge / bulk / embed), never a provider, and the model is selectable **per call** —
which is what lets us route per-stage and per-language, and reserve Claude for the
hardest corroboration judgement while Mistral carries the bulk.

This is a seed built on the stable REST endpoints (no SDK version risk). The full
event-bus Source seam — where tools, MCP servers, and sub-agents are *also* Sources —
grows from here.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx

from maat.obs import llm_span, record_completion

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_EMBED_URL = "https://api.mistral.ai/v1/embeddings"

# External LLM/embedding APIs rate-limit (429) under the pipeline's bursty load — Mistral's
# embeddings especially. A single un-retried 429 used to crash a whole corroborate run (no cluster
# recompute → a stale/empty feed), and likewise broke per-claim translation. Retry the transient
# statuses with bounded exponential backoff + jitter (honoring Retry-After when the server sends
# it). MAAT_LLM_RETRIES=0 disables. Tunable so a hard quota fails fast rather than hanging.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = int(os.environ.get("MAAT_LLM_RETRIES", "4"))
_BACKOFF_CAP = 30.0


def _retry_delay(attempt: int, resp: httpx.Response) -> float:
    """Seconds to wait before the next attempt. Prefers the server's Retry-After; otherwise
    exponential (1, 2, 4, 8 …) capped, with jitter to avoid synchronized retries."""
    ra = resp.headers.get("retry-after", "").strip()
    if ra.isdigit():
        return min(float(ra), _BACKOFF_CAP)
    return min(2.0**attempt, _BACKOFF_CAP) + random.uniform(0.0, 0.5)


def _post_json(url: str, *, headers: dict[str, str], payload: dict, timeout: httpx.Timeout) -> dict:
    """POST → parsed JSON, retrying 429/5xx with backoff. Non-retryable errors raise immediately
    via ``raise_for_status``; a persistent retryable error raises after the final attempt."""
    for attempt in range(_MAX_RETRIES + 1):
        resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
            time.sleep(_retry_delay(attempt, resp))
            continue
        resp.raise_for_status()
        return resp.json()
    raise AssertionError("unreachable")  # loop either returns or raises


# Proactive client-side throttle (#300). The retry/backoff above (#256) clears transient 429 blips,
# but the bounded worker pool (#296) can hold a SUSTAINED over-rate that backoff can't recover from
# (Mistral 429s past ~60/min; Anthropic past its tier RPM/TPM). A token bucket caps the *sustained*
# rate at the configured budget — calls block until a token refills — so parallel workers saturate
# the tier without a 429 storm. Process-wide; with N horizontally-scaled replicas (#298) set each
# replica's budget to tier/N. Tune to the tier via env; rate 0 = disabled (no throttle).


class _TokenBucket:
    """Thread-safe token bucket: ``capacity`` tokens, refilled ``rate_per_sec``; ``acquire(n)``
    blocks (time.sleep, off the event loop via the caller's to_thread) until n tokens are available.
    The refill rate caps the SUSTAINED throughput; ``capacity`` is the one-off burst (#256 absorbs it)."""

    def __init__(self, rate_per_sec: float, capacity: float, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] = time.sleep) -> None:
        self._rate = rate_per_sec
        self._capacity = max(capacity, 1.0)
        self._tokens = self._capacity
        # Injectable clock/sleep so tests drive a deterministic fake without patching the global
        # `time` module (which leaks across tests and real-sleeps the suite).
        self._now = monotonic
        self._sleep = sleep
        self._last = self._now()
        self._lock = threading.Lock()

    def acquire(self, n: float = 1.0) -> None:
        if self._rate <= 0:
            return  # disabled
        n = min(n, self._capacity)  # a single request can never exceed the bucket
        while True:
            with self._lock:
                now = self._now()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= n - 1e-9:  # epsilon: a refill of `wait*rate` lands a hair under n
                    self._tokens -= n          # in float, which would otherwise re-loop on sub-ULP waits
                    return
                wait = (n - self._tokens) / self._rate
            self._sleep(wait)  # outside the lock so other threads can refill-check meanwhile

    def available(self) -> float:
        """Tokens available right now, after a lazy refill (∞ when disabled). Does not consume —
        it is the peek that least-loaded routing ranks endpoints by."""
        if self._rate <= 0:
            return float("inf")
        with self._lock:
            now = self._now()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            return self._tokens


class _RateLimiter:
    """Per-provider request (RPM) + token (TPM) throttle. Either budget at 0 disables that arm."""

    def __init__(self, rpm: float, tpm: float, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] = time.sleep) -> None:
        self._req = _TokenBucket(rpm / 60.0, rpm, monotonic=monotonic, sleep=sleep) if rpm > 0 else None
        self._tok = _TokenBucket(tpm / 60.0, tpm, monotonic=monotonic, sleep=sleep) if tpm > 0 else None

    def acquire(self, est_tokens: float = 0.0) -> None:
        if self._req is not None:
            self._req.acquire(1.0)
        if self._tok is not None and est_tokens > 0:
            self._tok.acquire(est_tokens)

    def available(self) -> float:
        """Spare REQUEST budget now (∞ if the request arm is disabled) — the least-loaded signal."""
        return self._req.available() if self._req is not None else float("inf")


# Anthropic: no throttle by default (operator sets RPM/TPM to the account tier — cost is not a
# constraint, so provision the tier for headroom; #300). Mistral: default ~50 RPM, matching the old
# 1.2s-interval pacing's average but allowing a burst, so its 60/min budget is respected by default.
_CLAUDE_LIMIT = _RateLimiter(
    float(os.environ.get("MAAT_CLAUDE_RPM", "0")), float(os.environ.get("MAAT_CLAUDE_TPM", "0"))
)
_MISTRAL_LIMIT = _RateLimiter(
    float(os.environ.get("MAAT_MISTRAL_RPM", "50")), float(os.environ.get("MAAT_MISTRAL_TPM", "0"))
)


# ── Per-stage, multi-endpoint Claude routing (#300, DECISIONS D7) ────────────────────────────────
# A single Anthropic tier's RPM/TPM is the throughput ceiling: the bounded worker pool (#296) +
# throttle above keep us *under* one tier, they don't raise it. To raise it, the seam can hold
# SEVERAL Anthropic(-compatible) endpoints — in practice multiple API keys (separate
# workspaces/accounts, each its own tier), all on api.anthropic.com — each with its OWN _RateLimiter.
# A call picks one (round-robin, or least-loaded for unequal tiers), so the AGGREGATE budget is the
# SUM of the tiers: add an endpoint or raise a tier and sustained throughput rises proportionally.
# Routing is per-stage — the high-volume Sonnet stages (extract/classify/…) spread across every
# endpoint, while low-volume judge calls can stay pinned to one. All env-driven; with nothing
# configured there is ONE endpoint = today's single ANTHROPIC_API_KEY, so behaviour is unchanged.
#
# D7 left CLAUDE_ROUTE=anthropic|bedrock-eu|vertex-eu as a TBD EU-region preference. Maat runs on
# neither AWS nor GCP, so there is no SigV4/OAuth here: an endpoint is just a URL + key + an auth
# header style — "x-api-key" (Anthropic native) or "bearer" (an Anthropic-compatible regional
# gateway/proxy). "bedrock-eu"/"vertex-eu" are merely names an operator could give such an endpoint.

_DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
_VALID_AUTH = ("x-api-key", "bearer")
_VALID_POLICY = ("round-robin", "least-loaded")


class _Endpoint:
    """One Anthropic(-compatible) Messages endpoint with its own rate budget.

    The API key is read from ``key_env`` at call time, so a missing key raises KeyError exactly as
    the single-endpoint path always did and import never needs every key present.
    """

    def __init__(self, name: str, *, url: str = ANTHROPIC_URL, auth: str = "x-api-key",
                 key_env: str = "ANTHROPIC_API_KEY", version: str = _DEFAULT_ANTHROPIC_VERSION,
                 extra_headers: tuple[tuple[str, str], ...] = (),
                 limiter: _RateLimiter | None = None) -> None:
        if auth not in _VALID_AUTH:
            raise ValueError(f"endpoint {name!r}: auth must be one of {_VALID_AUTH}, got {auth!r}")
        self.name = name
        self.url = url
        self.auth = auth
        self.key_env = key_env
        self.version = version
        self.extra_headers = tuple(extra_headers)
        self.limiter = limiter if limiter is not None else _RateLimiter(0, 0)

    def headers(self) -> dict[str, str]:
        key = os.environ[self.key_env]
        h = {"anthropic-version": self.version, "content-type": "application/json"}
        if self.auth == "bearer":
            h["Authorization"] = f"Bearer {key}"
        else:
            h["x-api-key"] = key
        h.update(self.extra_headers)
        return h

    def acquire(self, est_tokens: float = 0.0) -> None:
        self.limiter.acquire(est_tokens)

    def available(self) -> float:
        """Spare request budget now (∞ if unthrottled) — the least-loaded ranking key."""
        return self.limiter.available()


class _Route:
    """An ordered set of endpoints + a selection policy, serving one stage (or the default)."""

    def __init__(self, endpoints: list[_Endpoint], policy: str = "round-robin") -> None:
        if not endpoints:
            raise ValueError("a route needs at least one endpoint")
        if policy not in _VALID_POLICY:
            raise ValueError(f"policy must be one of {_VALID_POLICY}, got {policy!r}")
        self.endpoints = endpoints
        self.policy = policy
        self._rr = 0
        self._lock = threading.Lock()

    def pick(self) -> _Endpoint:
        if len(self.endpoints) == 1:
            return self.endpoints[0]
        if self.policy == "least-loaded":
            # Most spare request budget = least likely to block. Send a raised tier proportionally
            # more traffic automatically (#300 acceptance). Ties — including all-∞ unthrottled
            # endpoints — fall through to the round-robin rotation so load still spreads evenly.
            avail = [ep.available() for ep in self.endpoints]
            hi = max(avail)
            candidates = [i for i, a in enumerate(avail) if a >= hi - 1e-9]
        else:
            candidates = list(range(len(self.endpoints)))
        with self._lock:
            ep = self.endpoints[candidates[self._rr % len(candidates)]]
            self._rr += 1
            return ep


class _Router:
    """Resolves a pipeline stage to its _Route; an unknown stage uses the default route."""

    def __init__(self, routes: dict[str, _Route], default: _Route) -> None:
        self.routes = routes
        self.default = default

    def route(self, stage: str) -> _Route:
        return self.routes.get(stage, self.default)

    def pick(self, stage: str) -> _Endpoint:
        return self.route(stage).pick()

    def with_zero_limits(self) -> _Router:
        """A copy whose endpoints never throttle — used by the test fixture so the suite, which
        exercises the real seam over a faked transport, never real-sleeps on a drained bucket."""
        zero = _RateLimiter(0, 0)
        clones: dict[str, _Endpoint] = {}

        def clone(ep: _Endpoint) -> _Endpoint:
            if ep.name not in clones:
                clones[ep.name] = _Endpoint(
                    ep.name, url=ep.url, auth=ep.auth, key_env=ep.key_env,
                    version=ep.version, extra_headers=ep.extra_headers, limiter=zero,
                )
            return clones[ep.name]

        routes = {s: _Route([clone(e) for e in r.endpoints], r.policy)
                  for s, r in self.routes.items()}
        default = _Route([clone(e) for e in self.default.endpoints], self.default.policy)
        return _Router(routes, default)


def _parse_endpoints(raw: str) -> list[_Endpoint]:
    """Endpoints from MAAT_CLAUDE_ENDPOINTS (JSON array of objects). Unset → ONE endpoint = today's
    single ANTHROPIC_API_KEY throttled by MAAT_CLAUDE_RPM/TPM (_CLAUDE_LIMIT)."""
    raw = raw.strip()
    if not raw:
        return [_Endpoint("anthropic", limiter=_CLAUDE_LIMIT)]
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"MAAT_CLAUDE_ENDPOINTS is not valid JSON: {e}") from e
    if not isinstance(spec, list) or not spec:
        raise ValueError("MAAT_CLAUDE_ENDPOINTS must be a non-empty JSON array of endpoint objects")
    endpoints: list[_Endpoint] = []
    seen: set[str] = set()
    for i, item in enumerate(spec):
        if not isinstance(item, dict) or "name" not in item:
            raise ValueError(f"MAAT_CLAUDE_ENDPOINTS[{i}] must be an object with a 'name'")
        name = str(item["name"])
        if name in seen:
            raise ValueError(f"MAAT_CLAUDE_ENDPOINTS: duplicate endpoint name {name!r}")
        seen.add(name)
        # Per-endpoint rpm/tpm; absent → the shared MAAT_CLAUDE_RPM/TPM defaults (0 = no throttle).
        rpm = float(item.get("rpm", os.environ.get("MAAT_CLAUDE_RPM", "0")))
        tpm = float(item.get("tpm", os.environ.get("MAAT_CLAUDE_TPM", "0")))
        headers = item.get("headers") or {}
        endpoints.append(_Endpoint(
            name,
            url=str(item.get("url", ANTHROPIC_URL)),
            auth=str(item.get("auth", "x-api-key")),
            key_env=str(item.get("key_env", "ANTHROPIC_API_KEY")),
            version=str(item.get("version", _DEFAULT_ANTHROPIC_VERSION)),
            extra_headers=tuple((str(k), str(v)) for k, v in headers.items()),
            limiter=_RateLimiter(rpm, tpm),
        ))
    return endpoints


def _route_from_cfg(stage: str, cfg: object, by_name: dict[str, _Endpoint]) -> _Route:
    """One stage's route from its config — a bare list of endpoint names, or an object
    {"endpoints": [...], "policy": "round-robin"|"least-loaded"}."""
    if isinstance(cfg, list):
        names: object = cfg
        policy = "round-robin"
    elif isinstance(cfg, dict):
        names = cfg.get("endpoints")
        policy = str(cfg.get("policy", "round-robin"))
    else:
        raise ValueError(f"stage {stage!r}: route must be a list of names or an object")
    if not isinstance(names, list) or not names:
        raise ValueError(f"stage {stage!r}: 'endpoints' must be a non-empty list of endpoint names")
    try:
        eps = [by_name[str(n)] for n in names]
    except KeyError as e:
        raise ValueError(
            f"stage {stage!r} references unknown endpoint {e.args[0]!r}; known: {sorted(by_name)}"
        ) from e
    return _Route(eps, policy)


def _build_claude_router() -> _Router:
    """Assemble the Claude router from env (MAAT_CLAUDE_ENDPOINTS + MAAT_CLAUDE_STAGE_ROUTES).

    With no stage routing configured, every stage spreads across ALL endpoints (round-robin); with
    the single default endpoint that is a no-op, so the out-of-the-box path is unchanged. Pin a
    stage (e.g. judge) to one endpoint — or choose "least-loaded" — via MAAT_CLAUDE_STAGE_ROUTES; a
    "default" key there overrides the catch-all for unlisted stages.
    """
    endpoints = _parse_endpoints(os.environ.get("MAAT_CLAUDE_ENDPOINTS", ""))
    by_name = {ep.name: ep for ep in endpoints}
    spread_all = _Route(endpoints, "round-robin")
    raw = os.environ.get("MAAT_CLAUDE_STAGE_ROUTES", "").strip()
    if not raw:
        return _Router({}, spread_all)
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"MAAT_CLAUDE_STAGE_ROUTES is not valid JSON: {e}") from e
    if not isinstance(spec, dict):
        raise ValueError("MAAT_CLAUDE_STAGE_ROUTES must be a JSON object {stage: route}")
    routes = {stage: _route_from_cfg(stage, cfg, by_name) for stage, cfg in spec.items()}
    default = routes.pop("default", spread_all)
    return _Router(routes, default)


_CLAUDE_ROUTER = _build_claude_router()


# Defaults; callers override per call (the whole point of the seam).
# cauri: the "judge" default → Opus (was Haiku — "haiku is terrible"). This backs the careful
# non-pipeline calls that take the seam default — acquisition query-gen (news_queries), the
# source-credibility gate, and the console assistant — all low-volume, so Opus here is cheap. The
# veracity PIPELINE stages pin their own model (extract/classify/extremity = Sonnet) and are
# unaffected by this default.
CLAUDE_JUDGE = "claude-opus-4-8"
MISTRAL_BULK = "mistral-small-latest"
MISTRAL_EMBED = "mistral-embed"

_TIMEOUT = httpx.Timeout(60.0)


@dataclass(frozen=True)
class Reply:
    text: str
    model: str


def claude_complete(prompt: str, *, model: str = CLAUDE_JUDGE, max_tokens: int = 256,
                    stage: str = "default") -> Reply:
    """Claude (Anthropic) — reserved for the hardest judgement stages.

    ``stage`` selects the routing group (#300): the high-volume Sonnet stages (extract/classify/…)
    spread across every configured endpoint for aggregate RPM/TPM, while the default group serves
    low-volume judge calls. Unconfigured, every stage maps to the one default endpoint.
    """
    ep = _CLAUDE_ROUTER.pick(stage)
    ep.acquire(max_tokens)  # this endpoint's RPM/TPM throttle so parallel workers don't 429-storm (#300)
    with llm_span("judge", model, prompt) as span:
        if span is not None:
            span.set_attribute("maat.llm.endpoint", ep.name)
        data = _post_json(
            ep.url,
            headers=ep.headers(),
            payload={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=_TIMEOUT,
        )
        text = data["content"][0]["text"]
        u = data.get("usage", {})
        record_completion(span, text, input_tokens=u.get("input_tokens", 0),
                          output_tokens=u.get("output_tokens", 0))
        return Reply(text=text, model=model)


async def claude_stream(
    prompt: str, *, model: str = CLAUDE_JUDGE, max_tokens: int = 1024, stage: str = "default"
) -> AsyncIterator[str]:
    """Streaming Claude: yields text deltas as they arrive (Anthropic SSE, ``stream: true``).

    The async counterpart to ``claude_complete`` for interactive surfaces (the console chat) — same
    request shape, routing (``stage``), telemetry (one span, completion recorded with the assembled
    text + usage) and throttle, just incremental. Raises like ``claude_complete`` (KeyError without
    the key, HTTP/transport errors on a bad response); callers wrap it for graceful degradation.
    """
    ep = _CLAUDE_ROUTER.pick(stage)
    await asyncio.to_thread(ep.acquire, float(max_tokens))  # throttle, but off the event loop (#300)
    parts: list[str] = []
    in_tok = out_tok = 0
    with llm_span("judge", model, prompt) as span:
        if span is not None:
            span.set_attribute("maat.llm.endpoint", ep.name)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "POST",
                ep.url,
                headers=ep.headers(),
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "messages": [{"role": "user", "content": prompt}],
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    evt = json.loads(payload)
                    etype = evt.get("type")
                    if etype == "content_block_delta":
                        text = (evt.get("delta") or {}).get("text", "")
                        if text:
                            parts.append(text)
                            yield text
                    elif etype == "message_start":
                        in_tok = ((evt.get("message") or {}).get("usage") or {}).get("input_tokens", 0)
                    elif etype == "message_delta":
                        out_tok = (evt.get("usage") or {}).get("output_tokens", out_tok)
        record_completion(span, "".join(parts), input_tokens=in_tok, output_tokens=out_tok)


def mistral_complete(prompt: str, *, model: str = MISTRAL_BULK, max_tokens: int = 256) -> Reply:
    """Mistral — bulk / near-mechanical stages (and EU-sovereign)."""
    key = os.environ["MISTRAL_API_KEY"]
    _MISTRAL_LIMIT.acquire(max_tokens)  # stay under Mistral's per-minute budget (#300; a pivot run bursts ~120 calls)
    with llm_span("bulk", model, prompt) as span:
        data = _post_json(
            MISTRAL_CHAT_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            payload={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=_TIMEOUT,
        )
        text = data["choices"][0]["message"]["content"]
        u = data.get("usage", {})
        record_completion(span, text, input_tokens=u.get("prompt_tokens", 0),
                          output_tokens=u.get("completion_tokens", 0))
        return Reply(text=text, model=model)


# Mistral's embeddings endpoint caps the total tokens per request, so a single call with the whole
# corpus 400s once the claim set grows (it worked at ~74 claims, broke at ~239). Batch the inputs to
# stay safely under the limit — order is preserved within and across batches.
_EMBED_BATCH = 64


def mistral_embed(texts: list[str], *, model: str = MISTRAL_EMBED) -> list[list[float]]:
    """Multilingual embeddings for clustering / dedup / identity (1024-dim). Batched (#scale)."""
    if not texts:
        return []
    key = os.environ["MISTRAL_API_KEY"]
    out: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH):
        chunk = texts[start : start + _EMBED_BATCH]
        _MISTRAL_LIMIT.acquire()  # one request per refill — keeps the ~10-batch burst under the rate limit (#300)
        with llm_span("embed", model, f"{len(chunk)} texts") as span:
            data = _post_json(
                MISTRAL_EMBED_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                payload={"model": model, "input": chunk},
                timeout=_TIMEOUT,
            )
            if span is not None:
                span.set_attribute(
                    "gen_ai.usage.input_tokens", data.get("usage", {}).get("prompt_tokens", 0)
                )
                span.set_attribute("maat.embed.count", len(chunk))
            out.extend(item["embedding"] for item in data["data"])
    return out
