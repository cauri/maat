"""Corroboration pass (§5.5): read the claims, cluster + collapse to independent
originators, emit `cluster.corroborated` events the kernel projects.

A batch pass over the current claims (re-runnable; clusters upsert by a stable id). Run:
uv run python -m maat.agents.corroborate_agent
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat.bus import connect
from maat.events import publish
from maat.pipeline.corroborate import ClaimRow, corroborate


def _cluster_id(claim_ids: list[str]) -> str:
    return hashlib.sha1("|".join(sorted(claim_ids)).encode()).hexdigest()[:24]


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    arts = await pool.fetch("select id, source, body from articles")
    src = {r["id"]: r["source"] for r in arts}
    bodies = {r["id"]: r["body"] for r in arts}
    rows = await pool.fetch("select id, text, article_id from claims")
    await pool.close()

    claims = [
        ClaimRow(id=str(r["id"]), text=r["text"], article_id=r["article_id"], source=src.get(r["article_id"], ""))
        for r in rows
    ]
    results = corroborate(claims, bodies)

    nc = await connect()
    for r in results:
        cid = _cluster_id(r.claim_ids)
        await publish(
            nc,
            "cluster.corroborated",
            cid,
            {
                "id": cid,
                "fact": r.fact,
                "sources": r.sources,
                "originators": r.originators,
                "independent_originators": r.independent_originators,
                "has_primary": r.has_primary,
                "extremity": r.extremity,
                "confidence": r.confidence,
                "claim_ids": r.claim_ids,
            },
        )
    await nc.flush()
    await nc.close()
    print(f"emitted {len(results)} corroborated cluster(s)")


if __name__ == "__main__":
    asyncio.run(main())
