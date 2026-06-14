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

import os
from dataclasses import dataclass

import httpx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_EMBED_URL = "https://api.mistral.ai/v1/embeddings"

# Defaults; callers override per call (the whole point of the seam).
CLAUDE_JUDGE = "claude-haiku-4-5-20251001"  # cheap default; the hard-judgement model is set per stage
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
    resp = httpx.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return Reply(text=resp.json()["content"][0]["text"], model=model)


def mistral_complete(prompt: str, *, model: str = MISTRAL_BULK, max_tokens: int = 256) -> Reply:
    """Mistral — bulk / near-mechanical stages (and EU-sovereign)."""
    key = os.environ["MISTRAL_API_KEY"]
    resp = httpx.post(
        MISTRAL_CHAT_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return Reply(text=resp.json()["choices"][0]["message"]["content"], model=model)


def mistral_embed(texts: list[str], *, model: str = MISTRAL_EMBED) -> list[list[float]]:
    """Multilingual embeddings for clustering / dedup / identity (1024-dim)."""
    key = os.environ["MISTRAL_API_KEY"]
    resp = httpx.post(
        MISTRAL_EMBED_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "input": texts},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return [item["embedding"] for item in resp.json()["data"]]
