"""Extraction agent: consumes `article.ingested`, emits `claims.extracted` (BRIEF §5.1-5.2).

Run: uv run python -m maat.agents.extract_agent
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv

from maat import prompts
from maat.acquire.clean import is_index_page
from maat.bus import run_agent
from maat.events import publish
from maat.pipeline.extract import extract_claims

_pool = None  # set in main(); used to resolve the operator's active prompt (P8)


async def handle(nc: Any, event: dict[str, Any]) -> None:
    article_id = event["stream_id"]
    data = event.get("data", {})
    # Skip section / index / landing pages — an amalgam of links to other stories, not an article
    # (#33). The obvious ones are caught here by a heuristic; the extract prompt's LLM check is the
    # second net. No claims → no cluster → it never becomes a story.
    if is_index_page(data.get("title", ""), data.get("body", "")):
        print(f"[extract] {article_id}: skipped — section/index page, not an article", flush=True)
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
    _pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    await run_agent("extract", "maat.events.article.ingested", handle)


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
