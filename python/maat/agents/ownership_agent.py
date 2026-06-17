"""Source ownership resolution agent (#41 / #254) — auto-fill the ownership graph from Wikidata.

Gated by ``MAAT_OWNERSHIP_LOOKUP=1`` (no-op otherwise). For each source Maat has ingested but not yet
resolved, it looks the outlet up on Wikidata (disambiguating by the source's domain), reads its
DIRECT controlling owners (parent organization / owned by), and emits ``source.ownership.resolved``.
corroborate folds these into the ownership map alongside the operator's manual groups (manual wins),
so co-owned outlets collapse to one independent originator without the operator typing each group.

Conservative + operator-overridable; one resolution per source (deduped against prior events — a
source that resolves to no owner is still recorded, so we never re-query it).
Run: uv run python -m maat.agents.ownership_agent
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat.acquire import wikidata
from maat.bus import connect
from maat.events import SOURCE_OWNERSHIP_RESOLVED, publish
from maat.pipeline.identity import canonical_source
from maat.pipeline.ownership import direct_owners, domain_of, pick_entity

ROOT = Path(__file__).resolve().parents[3]


def _resolve(source: str, domain: str) -> dict:
    """Look up one source on Wikidata → its direct owners. Always returns a marker (owners may be
    empty / entity None) so the caller records the attempt and never re-queries this source."""
    base = {"source": source, "canonical": canonical_source(source), "entity": None,
            "entity_label": "", "owners": [], "provenance": "wikidata"}
    candidates = wikidata.search_entities(source, limit=5)
    if not candidates and domain:  # domain-style source (e.g. "nypost.com") — try its second level
        candidates = wikidata.search_entities(domain.split(".")[0], limit=5)
    if not candidates:
        return base
    # Fetch the top candidates' claims so pick_entity can disambiguate on the official-site domain.
    claims_by_qid = {c["id"]: wikidata.entity_claims(c["id"]) for c in candidates[:3]}
    qid = pick_entity(candidates, domain, claims_by_qid)
    if not qid:
        return base
    claims = claims_by_qid.get(qid) or wikidata.entity_claims(qid)
    owners = [
        {"qid": oq, "label": wikidata.entity_claims(oq).get("label") or oq}
        for oq in direct_owners(claims)
    ]
    return {**base, "entity": qid, "entity_label": claims.get("label", ""), "owners": owners}


async def main() -> None:
    if os.environ.get("MAAT_OWNERSHIP_LOOKUP") != "1":
        print("ownership: MAAT_OWNERSHIP_LOOKUP != 1 — disabled, nothing to do")
        return
    load_dotenv(ROOT / ".env")
    tenant = os.environ.get("MAAT_TENANT_ID", "cauri")
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    arts = await pool.fetch(
        "select source, max(url) url from articles "
        "where source is not null and tenant_id = $1 group by source",
        tenant,
    )
    done = {
        r["s"]
        for r in await pool.fetch(
            "select distinct data->>'source' s from events where type = $1", SOURCE_OWNERSHIP_RESOLVED
        )
    }
    await pool.close()

    todo = [(r["source"], domain_of(r["url"] or r["source"])) for r in arts if r["source"] not in done]
    if not todo:
        print("ownership: no new sources to resolve")
        return

    nc = await connect()
    grouped = 0
    for source, domain in todo:
        info = _resolve(source, domain)
        if info["owners"]:
            grouped += 1
        await publish(nc, SOURCE_OWNERSHIP_RESOLVED, source, info, tenant)
    await nc.flush()
    await nc.close()
    print(f"ownership: resolved {len(todo)} new source(s); {grouped} with a controlling owner")


if __name__ == "__main__":
    asyncio.run(main())
