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

**Bounded per tick (#419).** NLI is the expensive leg and it is measured, not guessed: 15.4 ms per
call on the box, and this agent judges both directions, so **30.8 ms per pair** — about 29,200 pairs
in a 900s step. Retrieval over the live corpus yields ~486,800 candidate pairs, so a cold start is
~17 ticks of work. Judged pairs persist (``claim_relations`` → ``seen``), so progress accrues and
this converges; but WITHOUT a budget each tick would be killed mid-work at the timeout and reported
as TIMEOUT — for every tick until the backlog cleared. That is a false alarm that looks exactly like
the 27-day outage, and it would trip the watchdog continuously while the agent was in fact working.
So each tick judges at most ``MAAT_CONTRADICTION_MAX_PAIRS`` pairs and exits cleanly, reporting the
backlog it left. Bounded time, same as corroborate is now bounded in memory.
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
from maat.pipeline.embed_cache import embeddings_for

ROOT = Path(__file__).resolve().parents[3]

# Pairs judged per tick. At a measured 30.8 ms/pair (two NLI directions) 20,000 pairs is ~616s,
# inside the step's 900s timeout with headroom for retrieval. The rest waits for the next tick —
# judged pairs persist, so the backlog drains monotonically instead of restarting each time.
_MAX_PAIRS = int(os.environ.get("MAAT_CONTRADICTION_MAX_PAIRS", "20000"))


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
    # The persistent embedding cache (#286), NOT a bare mistral_embed: this used to re-embed every
    # live claim on EVERY tick — paying Mistral again for vectors corroborate had already cached.
    embeddings = await embeddings_for([text_of[i] for i in ids])

    nc = await connect()
    related = disputed = 0
    judged = 0
    candidates = nearest_pairs(ids, embeddings)
    backlog = sum(1 for p in candidates if p not in seen)
    for a, b in candidates:
        if (a, b) in seen:
            continue  # already judged this pair on a prior tick — don't pay for NLI again
        if judged >= _MAX_PAIRS:
            break  # tick budget spent — the rest is judged next tick (see the module docstring)
        judged += 1
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
    left = max(0, backlog - judged)
    # Always state the backlog. A bounded run that reports only what it DID is indistinguishable
    # from a complete one — that is the shape of the failure this whole issue is about.
    print(
        f"contradiction: judged {judged:,}/{backlog:,} new pair(s) → {related} relation(s), "
        f"{disputed} dispute(s)"
        + (f"; {left:,} pair(s) left for the next tick" if left else "; backlog clear")
    )


if __name__ == "__main__":
    asyncio.run(main())
