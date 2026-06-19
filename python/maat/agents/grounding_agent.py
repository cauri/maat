"""Primary-source grounding agent (#228, P3) — does each cluster's primary source back its fact?

Gated by ``MAAT_GROUNDING_LLM=1`` (no-op otherwise). For each cluster with a primary source that we
haven't grounded yet, it judges the fact against that primary source's article body (SUPPORTED /
CONTRADICTED / NOT_ADDRESSED), recomputes the confidence with that verdict (the primary lift is
earned only on genuine support; a contradiction multiplies it down), and emits ``cluster.grounded``
— maat-kerneld updates the cluster row (grounding + confidence), and the harvester carries the
verdict into ``cluster_snapshots`` so a contradiction resolves the fact to REFUTED over time.

Heuristic-first like ``agents.geotag_agent``: the model is consulted only for primary-bearing
clusters we haven't judged before, so spend scales with the genuinely-checkable tail. Runs after
corroborate and before harvest in the clock loop.

Run: uv run python -m maat.agents.grounding_agent
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat import prompts
from maat.bus import connect
from maat.events import CLUSTER_GROUNDED, publish
from maat.pipeline.corroborate import confidence_read, effective_originators, is_primary_source
from maat.pipeline.grounding import judge_grounding

ROOT = Path(__file__).resolve().parents[3]


def _jload(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def _primary_article(article_ids: list[str], arts: dict[str, dict]) -> tuple[str, str] | None:
    """Pick the primary-source article among a cluster's articles → (source_name, body).

    Prefers the longest body when several primary articles are present (more to ground against).
    """
    best: tuple[str, str] | None = None
    for aid in article_ids:
        a = arts.get(aid)
        if a and is_primary_source(a.get("source") or ""):
            body = a.get("body") or ""
            if best is None or len(body) > len(best[1]):
                best = (a.get("source") or "", body)
    return best


async def main() -> None:
    if os.environ.get("MAAT_GROUNDING_LLM") != "1":
        print("grounding: MAAT_GROUNDING_LLM != 1 — disabled, nothing to do")
        return
    load_dotenv(ROOT / ".env")
    tenant = os.environ.get("MAAT_TENANT_ID", "cauri")
    pool = await get_pool()
    crows = await pool.fetch(
        "select id, fact, claim_ids, originators, has_primary, extremity, confidence "
        "from clusters where tenant_id = $1 and has_primary",
        tenant,
    )
    claims = await pool.fetch("select id, article_id from claims")
    arts_rows = await pool.fetch("select id, source, body from articles")
    grounding_prompt = await prompts.active_text(pool, "grounding", prompts.seed_default("grounding"))
    # Clusters already grounded — don't pay to re-judge them (a changed claim set yields a new
    # cluster id, so a grown fact IS re-grounded; a stable one is judged once).
    done = {
        r["cid"]
        for r in await pool.fetch(
            "select distinct stream_id cid from events where type = $1", CLUSTER_GROUNDED
        )
    }
    await pool.close()

    art_of_claim = {str(r["id"]): str(r["article_id"]) for r in claims}
    arts = {str(r["id"]): {"source": r["source"] or "", "body": r["body"] or ""} for r in arts_rows}
    bodies = {aid: a["body"] for aid, a in arts.items()}
    srcs = {aid: a["source"] for aid, a in arts.items()}

    nc = await connect()
    judged = 0
    for r in crows:
        if r["id"] in done:
            continue
        claim_ids = [str(x) for x in _jload(r["claim_ids"])]
        article_ids = list(dict.fromkeys(art_of_claim[c] for c in claim_ids if c in art_of_claim))
        primary = _primary_article(article_ids, arts)
        if primary is None:
            continue  # flagged has_primary but no resolvable primary article body — leave ungrounded
        source_name, primary_body = primary
        verdict, evidence = judge_grounding(
            r["fact"] or "", source_name, primary_body, prompt=grounding_prompt
        )
        if not verdict:
            continue  # uncertain / error — leave the cluster ungrounded (confidence unchanged)
        # Recompute confidence with the verdict, consistently with corroborate: weight the
        # originator groups by sourcing quality, then read with the grounding signal.
        originators = [[str(a) for a in g] for g in _jload(r["originators"])]
        eff = effective_originators(originators, bodies, srcs)
        conf = confidence_read(eff, bool(r["has_primary"]), r["extremity"] or "notable", grounding=verdict)
        await publish(
            nc,
            CLUSTER_GROUNDED,
            r["id"],
            {
                "cluster_id": r["id"],
                "grounding": verdict,
                "confidence": conf,
                "evidence": evidence,
                "source": source_name,
                "method": "llm",
            },
            tenant,
        )
        judged += 1
    await nc.flush()
    await nc.close()
    print(f"grounding: judged {judged} primary-bearing cluster(s)")


if __name__ == "__main__":
    asyncio.run(main())
