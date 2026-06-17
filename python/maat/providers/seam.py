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
from collections.abc import AsyncIterator
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


# Mistral enforces a per-minute REQUEST budget (the live response carries
# ``x-ratelimit-limit-req-minute: 60``). A single corroborate pass bursts ~10 embedding batches
# PLUS up to ~120 per-claim pivot translations back-to-back, which sails past 60/min and 429s
# faster than backoff can recover — the retry above clears blips, not a sustained over-rate. So
# pace Mistral calls PROACTIVELY, process-wide, to stay under the limit (default ~50/min). Claude
# has its own, separate limits and is not throttled here. MAAT_MISTRAL_MIN_INTERVAL=0 disables.
_MISTRAL_MIN_INTERVAL = float(os.environ.get("MAAT_MISTRAL_MIN_INTERVAL", "1.2"))  # s between calls
_mistral_lock = threading.Lock()
_mistral_next = [0.0]  # monotonic time at which the next Mistral request may proceed


def _mistral_pace() -> None:
    """Block until the next Mistral request is allowed, keeping calls ≥ _MISTRAL_MIN_INTERVAL apart
    across all threads in this process. Called via asyncio.to_thread, so the event loop is free."""
    if _MISTRAL_MIN_INTERVAL <= 0:
        return
    with _mistral_lock:
        now = time.monotonic()
        wait = _mistral_next[0] - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _mistral_next[0] = now + _MISTRAL_MIN_INTERVAL


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
    _mistral_pace()  # stay under Mistral's per-minute request budget (a pivot run bursts ~120 calls)
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
        _mistral_pace()  # one batch per interval — keeps the ~10-batch burst under the rate limit
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
