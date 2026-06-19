"""Contradiction agent (#229) — NLI between a claim and its nearest neighbours → claim.related.

Gated by ``MAAT_CONTRADICTION_NLI=1`` AND a loadable NLI model (``pipeline.nli``); a no-op otherwise.
For the claims in the live clusters it embeds them (bi-encoder, mistral) for cheap cosine retrieval
of each claim's nearest neighbours, runs the NLI cross-encoder on those candidate pairs, and emits
one ``claim.related`` per detected contradiction / entailment with the model's score. For a
high-confidence contradiction it arbitrates by the two claims' cluster grounding / confidence and,
when one side is clearly stronger, flags the weaker claim ``disputed`` (``claim.disputed``) — which
the harvester folds into the cluster's ``corrected`` → REFUTED.

Runs after corroborate / grounding in the clock loop.
Run: uv run python -m maat.agents.contradiction_agent
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat.bus import connect
from maat.events import CLAIM_DISPUTED, CLAIM_RELATED, publish
from maat.pipeline import nli
from maat.pipeline.contradiction import CONTRADICTION_MIN_SCORE, arbitrate, nearest_pairs, pair_id
from maat.providers.seam import mistral_embed

ROOT = Path(__file__).resolve().parents[3]


def _jload(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


async def main() -> None:
    if os.environ.get("MAAT_CONTRADICTION_NLI") != "1" or not nli.available():
        print("contradiction: MAAT_CONTRADICTION_NLI != 1 or NLI model unavailable — nothing to do")
        return
    load_dotenv(ROOT / ".env")
    tenant = os.environ.get("MAAT_TENANT_ID", "cauri")
    pool = await get_pool()
    crows = await pool.fetch(
        "select id, claim_ids, confidence, grounding from clusters where tenant_id = $1", tenant
    )
    claim_rows = await pool.fetch("select id, text from claims")
    seen = {
        tuple(sorted((str(r["a"]), str(r["b"]))))
        for r in await pool.fetch("select claim_a a, claim_b b from claim_relations")
    }
    await pool.close()

    text_of = {str(r["id"]): r["text"] or "" for r in claim_rows}
    # Each claim's owning cluster (confidence + grounding), for arbitration. A claim sits in at most
    # one live cluster (corroborate keys clusters on their claim_ids).
    cluster_of: dict[str, dict] = {}
    for c in crows:
        for cid in (str(x) for x in _jload(c["claim_ids"])):
            cluster_of[cid] = {
                "id": c["id"], "confidence": float(c["confidence"] or 0.0), "grounding": c["grounding"]
            }

    # Only claims that are in a live cluster are worth checking (the facts on show).
    ids = [cid for cid in cluster_of if text_of.get(cid)]
    if len(ids) < 2:
        print("contradiction: <2 live claims — nothing to compare")
        return
    embeddings = mistral_embed([text_of[i] for i in ids])
    emb_of = dict(zip(ids, embeddings))

    nc = await connect()
    related = disputed = 0
    for a, b in nearest_pairs(ids, [emb_of[i] for i in ids]):
        if (a, b) in seen:
            continue  # already judged this pair on a prior tick — don't pay for NLI again
        # NLI both directions; keep the strongest reading.
        best = None
        for prem, hyp in ((a, b), (b, a)):
            res = nli.classify_pair(text_of[prem], text_of[hyp])
            if res and (best is None or res[1] > best[1]):
                best = res
        if not best or best[0] not in ("contradiction", "entailment"):
            continue
        label, score = best
        relation = "contradicts" if label == "contradiction" else "entails"
        await publish(
            nc, CLAIM_RELATED, pair_id(a, b, relation),
            {"claim_a": a, "claim_b": b, "relation": relation, "score": round(score, 4)}, tenant,
        )
        related += 1
        # Veracity effect: a confident contradiction, arbitrated by the clusters' grounding/confidence.
        if relation == "contradicts" and score >= CONTRADICTION_MIN_SCORE:
            ca, cb = cluster_of.get(a), cluster_of.get(b)
            if ca and cb and ca["id"] != cb["id"]:
                loser = arbitrate(ca["grounding"], ca["confidence"], cb["grounding"], cb["confidence"])
                if loser is not None:
                    loser_claim = a if loser == "a" else b
                    await publish(
                        nc, CLAIM_DISPUTED, loser_claim,
                        {"claim_id": loser_claim, "by_claim": b if loser == "a" else a,
                         "score": round(score, 4)}, tenant,
                    )
                    disputed += 1
    await nc.flush()
    await nc.close()
    print(f"contradiction: {related} relation(s), {disputed} dispute(s)")


if __name__ == "__main__":
    asyncio.run(main())
