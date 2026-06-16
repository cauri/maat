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
from maat.events import ADMIN_SOURCE_GROUPED, publish
from maat.pipeline.corroborate import ClaimRow, corroborate
from maat.pipeline.extremity import rate_extremity
from maat.pipeline.identity import canonical_source


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
    # Ownership grouping (#41): fold the operator's admin.source.grouped events (latest per
    # source) into a {canonical_source: group} map so co-owned outlets collapse to one
    # independent originator in corroboration instead of inflating the count.
    grps = await pool.fetch(
        "select distinct on (data->>'source') data->>'source' s, data->>'group' g "
        "from events where type = $1 order by data->>'source', id desc",
        ADMIN_SOURCE_GROUPED,
    )
    ownership = {canonical_source(r["s"]): r["g"] for r in grps if r["s"] and r["g"]}
    # Batch recompute: reset the clusters projection so a re-run REPLACES rather than
    # accumulates. Cluster ids are keyed on their claim_ids, so a changed claim set (or
    # changed collapse) would otherwise leave orphan clusters behind.
    await pool.execute("delete from clusters")
    await pool.close()

    claims = [
        ClaimRow(id=str(r["id"]), text=r["text"], article_id=r["article_id"], source=src.get(r["article_id"], ""))
        for r in rows
    ]
    results = corroborate(
        claims, bodies, extremity_of=partial(rate_extremity, prompt=extremity_prompt), ownership=ownership
    )

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
