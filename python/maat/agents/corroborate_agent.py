"""Corroboration pass (§5.5): read the claims, cluster + collapse to independent
originators, emit `cluster.corroborated` events the kernel projects.

A batch pass over the current claims (re-runnable; clusters upsert by a stable id). Run:
uv run python -m maat.agents.corroborate_agent
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from functools import partial
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat import prompts
from maat.bus import connect
from maat.config import active_config, pipeline_overrides
from maat.events import ADMIN_SOURCE_GROUPED, publish
from maat.pipeline.corroborate import ClaimRow, corroborate
from maat.pipeline.extremity import rate_extremity
from maat.pipeline.identity import canonical_source
from maat.serving.translate import translate_text


def _cluster_id(claim_ids: list[str]) -> str:
    return hashlib.sha1("|".join(sorted(claim_ids)).encode()).hexdigest()[:24]


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    arts = await pool.fetch("select id, source, body, language from articles")
    src = {r["id"]: r["source"] for r in arts}
    bodies = {r["id"]: r["body"] for r in arts}
    lang_by_art = {r["id"]: (r["language"] or "") for r in arts}  # #240: claim language for the pivot
    rows = await pool.fetch("select id, text, article_id from claims")
    # Existing English pivots (#240): claim_id → English translation, cached so each non-English
    # claim is translated at most once ever (read straight off the event log — no projection).
    pivots: dict[str, str] = {}
    for r in await pool.fetch("select data from events where type = 'claim.pivot'"):
        d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
        if d.get("claim_id") and d.get("text_en"):
            pivots[d["claim_id"]] = d["text_en"]
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
    # Operator config enactment (#183/#184): the pipeline runs on the PROMOTED thresholds
    # (sign-off-gated), falling back to code defaults for anything not promoted.
    promoted = await pool.fetch(
        "select data from events where type = 'admin.config.promoted' order by id"
    )
    overrides = pipeline_overrides(
        active_config(
            (json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]) for r in promoted
        )
    )
    # The clusters currently on show — so after recompute we can RETIRE only the ones that no
    # longer exist, instead of wiping the projection up front. (The recompute below takes
    # minutes — clustering + a per-cluster extremity rating — and deleting first left the feed
    # EMPTY for that whole window on every tick.)
    prev_ids = {r["id"] for r in await pool.fetch("select id from clusters")}
    await pool.close()

    # Cross-lingual pivot (#240): translate non-English claims to English FOR CLUSTERING ONLY, so a
    # fact reported across languages corroborates as one. Cached (claim.pivot) → each claim
    # translated once ever; bounded per run (MAAT_PIVOT_MAX) so a backlog converges over ticks
    # without stalling one. English / no-budget / translation failure → cluster on the original
    # text (identical to pre-#240). The displayed fact always stays the original text.
    def _english(lang: str) -> bool:
        low = (lang or "").strip().lower()
        return (not low) or low.startswith("en") or low == "english"

    budget = int(os.environ.get("MAAT_PIVOT_MAX", "120"))
    embed_by_claim: dict[str, str] = {}
    new_pivots: dict[str, str] = {}
    for r in rows:
        cid = str(r["id"])
        if _english(lang_by_art.get(r["article_id"], "")):
            continue
        if cid in pivots:
            embed_by_claim[cid] = pivots[cid]
            continue
        if budget > 0:
            en, _engine = await asyncio.to_thread(
                translate_text, r["text"], "en", lang_by_art.get(r["article_id"])
            )
            if en and en.strip() and en != r["text"]:
                embed_by_claim[cid] = en
                new_pivots[cid] = en
                budget -= 1

    claims = [
        ClaimRow(
            id=str(r["id"]), text=r["text"], article_id=r["article_id"],
            source=src.get(r["article_id"], ""), embed_text=embed_by_claim.get(str(r["id"]), ""),
        )
        for r in rows
    ]
    results = corroborate(
        claims, bodies, extremity_of=partial(rate_extremity, prompt=extremity_prompt),
        ownership=ownership, **overrides,
    )

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
    # Persist the freshly-computed English pivots (#240) so they're never re-translated.
    for cid, en in new_pivots.items():
        await publish(nc, "claim.pivot", cid, {"claim_id": cid, "text_en": en})
    await nc.flush()
    await nc.close()
    print(
        f"emitted {len(results)} corroborated cluster(s); retired {len(superseded)} superseded; "
        f"+{len(new_pivots)} new pivots ({len(embed_by_claim)} claims clustered via English pivot)"
    )


if __name__ == "__main__":
    asyncio.run(main())
