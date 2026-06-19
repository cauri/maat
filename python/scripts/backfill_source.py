"""Per-source reputation backfill (#241) — the real entry point, manual + cost-instrumented.

    uv run python scripts/backfill_source.py <source> [depth] [--wait SECONDS]

Pulls the outlet's history, ingests it through the normal pipeline (tagged with a backfill_run_id),
and reports the cost of EVERY pipeline step. ``--wait`` polls until the agents have extracted +
classified the run's articles, then reports the ACTUAL cost (extract + classify + embed measured
from the run's own events); without it you get the up-front estimate. Acquisition cost is exact
(channel counts). The same ``run_backfill`` core is what the registry agent calls automatically for
newly-registered sources — this script just adds the manual trigger + measurement + step log.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat import prompts
from maat.bus import connect
from maat.events import SOURCE_STATE_CHANGED, publish
from maat.learning.source_backfill import run_backfill, run_id_for
from maat.serving import spend
from maat.serving.source_flags import denied_sources

ROOT = Path(__file__).resolve().parents[2]


async def _run_counts(pool, run_id: str) -> dict:
    """Measured pipeline footprint of a backfill run, from its own articles' downstream events."""
    aids = [r["stream_id"] for r in await pool.fetch(
        "select stream_id from events where type='article.ingested' and data->>'backfill_run_id'=$1",
        run_id,
    )]
    if not aids:
        return {"articles": 0, "articles_with_claims": 0, "claims": 0, "clusters": 0}
    n_articles = await pool.fetchval("select count(*) from articles where id = any($1::text[])", aids) or 0
    n_with = await pool.fetchval(
        "select count(distinct article_id) from claims where article_id = any($1::text[])", aids) or 0
    n_claims = await pool.fetchval(
        "select count(*) from claims where article_id = any($1::text[])", aids) or 0
    n_clusters = await pool.fetchval(
        "select count(distinct c.id) from clusters c "
        "join claims cl on cl.id::text in (select jsonb_array_elements_text(c.claim_ids)) "
        "where cl.article_id = any($1::text[])", aids) or 0
    return {"articles": n_articles, "articles_with_claims": n_with, "claims": n_claims, "clusters": n_clusters}


async def main() -> None:
    load_dotenv(ROOT / ".env")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: backfill_source.py <source> [depth] [--wait SECONDS]")
        return
    source = args[0].strip().lower().removeprefix("www.")
    depth = int(args[1]) if len(args) > 1 else int(os.environ.get("MAAT_BACKFILL_DEPTH", "100"))
    wait_s = 0
    for a in sys.argv[1:]:
        if a.startswith("--wait"):
            wait_s = int(a.split("=", 1)[1]) if "=" in a else 600

    pool = await get_pool()
    gate_prompt = await prompts.active_text(pool, "source_gate", prompts.seed_default("source_gate"))
    known_good = frozenset(
        (r["source"] or "").lower().removeprefix("www.")
        for r in await pool.fetch("select distinct source from articles where source is not null"))
    denied = denied_sources(
        (json.loads(r["data"]) if isinstance(r["data"], str) else r["data"])
        for r in await pool.fetch("select data from events where type='admin.source.flagged' order by id"))
    seen = {r["url"] for r in await pool.fetch("select url from articles where url is not null")}

    at = datetime.now(timezone.utc).isoformat()
    run_id = run_id_for(source, at)
    nc = await connect()
    print(f"[backfill] {source} — run {run_id}, depth {depth} — start {at}", flush=True)
    res = await run_backfill(nc, source, run_id=run_id, at=at, depth=depth,
                             gate_prompt=gate_prompt, known_good=known_good, denied=denied, seen=seen)
    await nc.flush()
    print(f"[backfill] acquired {res.fetched} → ingested {res.ingested} "
          f"(gated out {res.gated_out}, dup {res.duplicate}, no-body {res.no_body}); "
          f"channels={res.by_channel}; acquisition ${res.acquisition_usd:.4f}", flush=True)

    if wait_s > 0:
        print(f"[backfill] waiting up to {wait_s}s for extract+classify to drain…", flush=True)
        deadline = wait_s
        prev = -1
        while deadline > 0:
            c = await _run_counts(pool, run_id)
            if c["articles_with_claims"] >= res.ingested or c["articles_with_claims"] == prev and prev > 0:
                break
            prev = c["articles_with_claims"]
            await asyncio.sleep(20)
            deadline -= 20

    counts = await _run_counts(pool, run_id)
    cost = spend.backfill_cost(
        n_articles=counts["articles"], n_articles_with_claims=counts["articles_with_claims"],
        n_claims=counts["claims"], n_clusters=counts["clusters"], acquisition_usd=res.acquisition_usd)
    mode = "ACTUAL (post-drain)" if wait_s > 0 else "ESTIMATE (pre-drain)"

    # record the run cost on the source so it shows on /sources + /spend (state stays backfilling;
    # the registry agent flips it active once its clusters score).
    await publish(nc, SOURCE_STATE_CHANGED, source,
                  {"source": source, "state": "backfilling", "run_id": run_id,
                   "cost_usd": cost.total_usd, "at": datetime.now(timezone.utc).isoformat()})
    await nc.flush()
    await nc.close()
    await pool.close()

    print(f"\n=== backfill cost log — {source} ({mode}) ===", flush=True)
    print(f"  run_id              {run_id}", flush=True)
    print(f"  history fetched     {res.fetched}  (channels: {res.by_channel})", flush=True)
    print(f"  ingested            {res.ingested}", flush=True)
    print(f"  → articles in DB    {counts['articles']}", flush=True)
    print(f"  → with claims       {counts['articles_with_claims']}", flush=True)
    print(f"  → claims            {counts['claims']}", flush=True)
    print(f"  → clusters touched  {counts['clusters']}", flush=True)
    print(f"  acquisition  ${cost.acquisition_usd:.4f}", flush=True)
    print(f"  extract      ${cost.extract_usd:.4f}", flush=True)
    print(f"  classify     ${cost.classify_usd:.4f}", flush=True)
    print(f"  embed        ${cost.embed_usd:.6f}", flush=True)
    print(f"  extremity    ${cost.extremity_usd:.4f}", flush=True)
    print(f"  TOTAL        ${cost.total_usd:.4f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
