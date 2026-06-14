"""Classification agent: claims.extracted -> claims.classified (BRIEF §5.3).

Tags each claim fact|projection (+ is_synthesis, horizon). Run:
uv run python -m maat.agents.classify_agent
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from maat.bus import run_agent
from maat.events import publish
from maat.pipeline.claim import Claim
from maat.pipeline.classify import classify_claims


async def handle(nc: Any, event: dict[str, Any]) -> None:
    data = event.get("data", {})
    article_id = data.get("article_id", event["stream_id"])
    claims = [Claim.model_validate(c) for c in data.get("claims", [])]
    if not claims:
        return
    classified = await asyncio.to_thread(
        classify_claims, claims, article_text=data.get("article_text", "")
    )
    await publish(
        nc,
        "claims.classified",
        article_id,
        {
            "article_id": article_id,
            "classifications": [
                {"id": c.id, "kind": c.kind, "is_synthesis": c.is_synthesis, "horizon": c.horizon}
                for c in classified
            ],
        },
    )
    print(f"[classify] {article_id}: {len(classified)} classified", flush=True)


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    asyncio.run(run_agent("classify", "maat.events.claims.extracted", handle))


if __name__ == "__main__":
    main()
