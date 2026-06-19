"""Extraction agent: consumes `article.ingested`, emits `claims.extracted` (BRIEF §5.1-5.2).

Run: uv run python -m maat.agents.extract_agent
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from maat.db import get_pool
from maat import prompts
from maat.acquire.clean import is_index_page
from maat.bus import run_agent
from maat.events import publish
from maat.pipeline.extract import extract_claims

_pool = None  # set in main(); used to resolve the operator's active prompt (P8)


async def _has_claims(pool: Any, article_id: str) -> bool:
    """True if this article already produced claims (so a redelivery can skip re-extraction)."""
    return bool(await pool.fetchval("select 1 from claims where article_id = $1 limit 1", article_id))


async def handle(nc: Any, event: dict[str, Any]) -> None:
    article_id = event["stream_id"]
    data = event.get("data", {})
    # Skip section / index / landing pages — an amalgam of links to other stories, not an article
    # (#33). The obvious ones are caught here by a heuristic; the extract prompt's LLM check is the
    # second net. No claims → no cluster → it never becomes a story.
    if is_index_page(data.get("title", ""), data.get("body", "")):
        print(f"[extract] {article_id}: skipped — section/index page, not an article", flush=True)
        return
    # Idempotency (#297): claim ids are content-random, so re-running extract on an at-least-once
    # redelivery (worker crash / AckWait) would append a SECOND set of claims — the kernel dedups
    # claims.extracted by claim id, which a fresh extraction doesn't collide on. If this article
    # already has claims the work is done, so skip. (Every OTHER stage is keyed by a deterministic
    # id — claims.classified / cluster.corroborated / claim.related all upsert — so extract is the
    # one stage that needs an explicit guard.)
    if _pool is not None and await _has_claims(_pool, article_id):
        print(f"[extract] {article_id}: already extracted — skipping (idempotent redelivery, #297)", flush=True)
        return
    prompt = await prompts.active_text(_pool, "extract", prompts.seed_default("extract"))
    # extract_claims is sync (LLM call); keep the event loop free.
    claims = await asyncio.to_thread(
        extract_claims,
        data.get("body", ""),
        source_metadata=data.get("source", ""),
        language=data.get("language", "unknown"),
        prompt=prompt,
    )
    await publish(
        nc,
        "claims.extracted",
        article_id,
        {"article_id": article_id, "claims": [c.model_dump() for c in claims]},
    )
    print(f"[extract] {article_id}: {len(claims)} claims", flush=True)


async def _run() -> None:
    global _pool
    _pool = await get_pool()
    await run_agent("extract", "maat.events.article.ingested", handle)


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
