"""Corroboration pass (§5.5): read the claims, cluster + collapse to independent
originators, emit `cluster.corroborated` events the kernel projects.

A batch pass over the current claims (re-runnable; clusters upsert by a stable id). Run:
uv run python -m maat.agents.corroborate_agent
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from functools import partial
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat import prompts
from maat.bus import connect
from maat.events import publish
from maat.pipeline.corroborate import ClaimRow, corroborate
from maat.pipeline.extremity import rate_extremity


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
    # Resolve the operator's active extremity prompt (P8) before closing the pool.
    extremity_prompt = await prompts.active_text(pool, "extremity", prompts.seed_default("extremity"))
    # The clusters currently on show — so after recompute we can RETIRE only the ones that no
    # longer exist, instead of wiping the projection up front. (The recompute below takes
    # minutes — clustering + a per-cluster extremity rating — and deleting first left the feed
    # EMPTY for that whole window on every tick.)
    prev_ids = {r["id"] for r in await pool.fetch("select id from clusters")}
    await pool.close()

    claims = [
        ClaimRow(id=str(r["id"]), text=r["text"], article_id=r["article_id"], source=src.get(r["article_id"], ""))
        for r in rows
    ]
    results = corroborate(claims, bodies, extremity_of=partial(rate_extremity, prompt=extremity_prompt))

    # Upsert the new clusters, then retire the superseded ones — both through the kernel's own
    # events (it upserts on cluster.corroborated, deletes on cluster.removed), so the single
    # writer stays the single writer and the feed is never blank: old clusters keep serving until
    # the new ones land, then the stale ids are removed. Cluster ids are keyed on their claim_ids,
    # so a changed claim set yields a new id and the old one falls into `prev_ids - keep`.
    nc = await connect()
    keep: set[str] = set()
    for r in results:
        cid = _cluster_id(r.claim_ids)
        keep.add(cid)
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
    superseded = prev_ids - keep
    for cid in superseded:
        await publish(nc, "cluster.removed", cid, {"id": cid})
    await nc.flush()
    await nc.close()
    print(f"emitted {len(results)} corroborated cluster(s); retired {len(superseded)} superseded")


if __name__ == "__main__":
    asyncio.run(main())
