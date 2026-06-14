"""Extraction agent: consumes `article.ingested`, emits `claims.extracted` (BRIEF §5.1-5.2).

Run: uv run python -m maat.agents.extract_agent
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from maat.bus import run_agent
from maat.events import publish
from maat.pipeline.extract import extract_claims


async def handle(nc: Any, event: dict[str, Any]) -> None:
    article_id = event["stream_id"]
    data = event.get("data", {})
    # extract_claims is sync (LLM call); keep the event loop free.
    claims = await asyncio.to_thread(
        extract_claims,
        data.get("body", ""),
        source_metadata=data.get("source", ""),
        language=data.get("language", "unknown"),
    )
    await publish(
        nc,
        "claims.extracted",
        article_id,
        {"article_id": article_id, "claims": [c.model_dump() for c in claims]},
    )
    print(f"[extract] {article_id}: {len(claims)} claims", flush=True)


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    asyncio.run(run_agent("extract", "maat.events.article.ingested", handle))


if __name__ == "__main__":
    main()
