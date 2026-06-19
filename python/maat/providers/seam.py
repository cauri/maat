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


# Anthropic: no throttle by default (operator sets RPM/TPM to the account tier — cost is not a
# constraint, so provision the tier for headroom; #300). Mistral: default ~50 RPM, matching the old
# 1.2s-interval pacing's average but allowing a burst, so its 60/min budget is respected by default.
_CLAUDE_LIMIT = _RateLimiter(
    float(os.environ.get("MAAT_CLAUDE_RPM", "0")), float(os.environ.get("MAAT_CLAUDE_TPM", "0"))
)
_MISTRAL_LIMIT = _RateLimiter(
    float(os.environ.get("MAAT_MISTRAL_RPM", "50")), float(os.environ.get("MAAT_MISTRAL_TPM", "0"))
)


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


def claude_complete(prompt: str, *, model: str = CLAUDE_JUDGE, max_tokens: int = 256) -> Reply:
    """Claude (Anthropic) — reserved for the hardest judgement stages."""
    key = os.environ["ANTHROPIC_API_KEY"]
    _CLAUDE_LIMIT.acquire(max_tokens)  # proactive RPM/TPM throttle so parallel workers don't 429-storm (#300)
    with llm_span("judge", model, prompt) as span:
        data = _post_json(
            ANTHROPIC_URL,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
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
    prompt: str, *, model: str = CLAUDE_JUDGE, max_tokens: int = 1024
) -> AsyncIterator[str]:
    """Streaming Claude: yields text deltas as they arrive (Anthropic SSE, ``stream: true``).

    The async counterpart to ``claude_complete`` for interactive surfaces (the console chat) — same
    request shape, same telemetry (one span, completion recorded with the assembled text + usage),
    just incremental. Raises like ``claude_complete`` (KeyError without the key, HTTP/transport
    errors on a bad response); callers wrap it for graceful degradation.
    """
    key = os.environ["ANTHROPIC_API_KEY"]
    parts: list[str] = []
    in_tok = out_tok = 0
    with llm_span("judge", model, prompt) as span:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "POST",
                ANTHROPIC_URL,
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
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
