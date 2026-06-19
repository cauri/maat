"""Curation geo-tagger (#189, P6) — fills the de-US re-ranker's country gaps with the bulk LLM.

The feed's curation step (``agents.curation.curate``) balances the feed across countries so no
single region dominates, but only for stories whose country the TLD/language heuristic
(``serving.feed._infer_country``) can place. English-language wire copy about a non-Anglophone
event often carries no country signal, so it slips the de-US cap entirely. This agent runs the
SAME heuristic the feed runs, and for each cluster it leaves UNPLACED it asks the bulk model for
the country, emitting ``story.geo_inferred`` events the feed folds at read time.

Heuristic-first: the LLM is consulted ONLY for clusters the heuristic can't place and that we
haven't inferred before, so spend scales with the genuinely-ambiguous tail, not the whole corpus.
Gated OFF unless ``MAAT_CURATION_LLM=1`` — without the flag this is a no-op.

Run: uv run python -m maat.agents.geotag_agent  (scheduled after story-graph in the clock).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat.bus import connect
from maat.events import STORY_GEO_INFERRED, publish
from maat.pipeline.geotag import llm_country
from maat.serving.feed import _infer_country  # reuse the EXACT heuristic the feed applies

ROOT = Path(__file__).resolve().parents[3]


def _jload(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


async def main() -> None:
    if os.environ.get("MAAT_CURATION_LLM") != "1":
        print("geotag: MAAT_CURATION_LLM != 1 — heuristic-only, nothing to do")
        return
    load_dotenv(ROOT / ".env")
    tenant = os.environ.get("MAAT_TENANT_ID", "cauri")
    pool = await get_pool()
    crows = await pool.fetch(
        "select id, fact, claim_ids, originators from clusters where tenant_id = $1", tenant
    )
    claims = await pool.fetch("select id, article_id from claims")
    arts = await pool.fetch("select id, source, language from articles")
    # Clusters already geo-inferred — don't pay to re-infer them.
    done = {
        r["cid"]
        for r in await pool.fetch(
            "select distinct stream_id cid from events where type = $1", STORY_GEO_INFERRED
        )
    }
    await pool.close()

    # article_meta / claims shaped exactly as serving.feed builds them, so _infer_country behaves
    # identically here: it reads each claim's language (attached from its article) and each
    # originator article's source TLD.
    article_meta = {str(r["id"]): {"source": r["source"], "language": r["language"]} for r in arts}
    claims_by_id = {
        str(r["id"]): {
            "article_id": str(r["article_id"]),
            "language": article_meta.get(str(r["article_id"]), {}).get("language") or "en",
        }
        for r in claims
    }

    todo: list[tuple[str, str]] = []
    for r in crows:
        if r["id"] in done:
            continue
        cl_claims = [claims_by_id[c] for c in (str(x) for x in _jload(r["claim_ids"])) if c in claims_by_id]
        if _infer_country(cl_claims, article_meta, r["originators"]):
            continue  # heuristic already places it — no LLM spend
        todo.append((r["id"], r["fact"] or ""))

    if not todo:
        print("geotag: no unplaced clusters (heuristic covered everything)")
        return

    nc = await connect()
    inferred = 0
    for cid, fact in todo:
        country = llm_country(fact)
        if not country:
            continue
        await publish(
            nc, STORY_GEO_INFERRED, cid,
            {"cluster_id": cid, "country": country, "method": "llm"}, tenant,
        )
        inferred += 1
    await nc.flush()
    await nc.close()
    print(f"geotag: inferred country for {inferred}/{len(todo)} unplaced cluster(s)")


if __name__ == "__main__":
    asyncio.run(main())
