"""Source registry agent (#241) — fold the lifecycle, decide transitions, emit the events.

One idempotent batch pass (re-runnable on the clock, like corroborate):

  1. Fold the current registry from ``source.*`` events.
  2. Gather what's true now — every source seen in ``articles``, which of them appear in a
     ``clusters`` row (i.e. are genuinely in the feed), each source's provider, and each source's
     reputation (#37 fold over the corroboration history).
  3. ``plan_registry`` decides the transitions: grandfather feed sources straight to ``active`` on
     first sight, hold genuinely-new sources at ``registered`` until their articles corroborate,
     activate pending sources once they do, refresh moved reputations.
  4. Publish ``source.registered`` / ``source.state_changed`` for each transition.

Emits nothing when nothing changed. Run:  uv run python scripts/source_registry_agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat import prompts
from maat.bus import connect
from maat.events import SOURCE_REGISTERED, SOURCE_STATE_CHANGED, publish
from maat.learning.reputation import fold_reputation, reputation_score
from maat.learning.source_backfill import run_backfill, run_id_for
from maat.learning.source_registry import REGISTERED, fold_sources, plan_registry
from maat.serving.source_flags import denied_sources

ROOT = Path(__file__).resolve().parents[2]


def _rows_data(rows) -> list:
    return [json.loads(r["data"]) if isinstance(r["data"], str) else r["data"] for r in rows]


async def main() -> None:
    load_dotenv(ROOT / ".env")
    pool = await get_pool()

    # Sources seen + their latest provider (provider lives in the article.ingested event, not the
    # articles projection). One scan over the ingest events gives both.
    provider_by_source: dict[str, str] = {}
    sources_seen: set[str] = set()
    for d in _rows_data(await pool.fetch(
        "select data from events where type = 'article.ingested' order by id"
    )):
        src = (d.get("source") or "").strip()
        if not src:
            continue
        sources_seen.add(src)
        if d.get("provider"):
            provider_by_source[src] = d["provider"]
    # Also count anything in the projection that predates provider tagging.
    for r in await pool.fetch("select distinct source from articles where source is not null"):
        if (r["source"] or "").strip():
            sources_seen.add(r["source"].strip())

    # Sources that are genuinely in the feed = those appearing in a clusters row.
    with_clusters: set[str] = set()
    for r in await pool.fetch("select sources from clusters"):
        srcs = r["sources"]
        if isinstance(srcs, str):
            srcs = json.loads(srcs)
        for s in srcs or []:
            if s:
                with_clusters.add(s)

    # Per-source reputation (#37) over the corroboration history.
    history = _rows_data(await pool.fetch(
        "select data from events where type = 'cluster.corroborated' order by id"
    ))
    reputation_by_source = {rec.source: reputation_score(rec) for rec in fold_reputation(history)}

    # Current registry.
    records = fold_sources(_rows_data(await pool.fetch(
        "select data from events where type in ($1, $2) order by id",
        SOURCE_REGISTERED, SOURCE_STATE_CHANGED,
    )))

    # Auto-backfill (#241): when enabled, kick a bounded number of not-yet-backfilled REGISTERED
    # sources through the real per-source backfill each pass — the production "registering a source
    # kicks off backfill + reputation" path. OFF by default + bounded (a wrong burst would backfill
    # the whole pending pool at once); the operator turns it on deliberately. The manual CLI
    # (scripts/backfill_source.py) is always available regardless.
    on_register_max = int(os.environ.get("MAAT_BACKFILL_ON_REGISTER", "0"))
    backfill_cfg = None
    if on_register_max > 0:
        backfill_cfg = {
            "gate_prompt": await prompts.active_text(pool, "source_gate", prompts.seed_default("source_gate")),
            "known_good": frozenset(
                (r["source"] or "").lower().removeprefix("www.")
                for r in await pool.fetch("select distinct source from articles where source is not null")),
            "denied": denied_sources(_rows_data(await pool.fetch(
                "select data from events where type='admin.source.flagged' order by id"))),
            "seen": {r["url"] for r in await pool.fetch("select url from articles where url is not null")},
            "depth": int(os.environ.get("MAAT_BACKFILL_DEPTH", "100")),
        }
    await pool.close()

    transitions = plan_registry(
        records=records,
        sources_seen=sources_seen,
        provider_by_source=provider_by_source,
        sources_with_clusters=with_clusters,
        reputation_by_source=reputation_by_source,
    )
    # Registered sources not yet backfilled — the queue the auto-trigger draws from (bounded below).
    will_register = {t.source for t in transitions if t.is_new and t.state == REGISTERED}
    eligible = [s for s, r in records.items() if r.state == REGISTERED and not r.backfill_run_id]
    eligible = list(dict.fromkeys(eligible + sorted(will_register)))
    to_backfill = eligible[: (int(os.environ.get("MAAT_BACKFILL_ON_REGISTER", "0")))] if backfill_cfg else []

    if not transitions and not to_backfill:
        print(f"[registry] no changes ({len(records)} sources, {len(sources_seen)} seen)")
        return

    now = datetime.now(timezone.utc).isoformat()
    nc = await connect()
    grandfathered = activated = registered = refreshed = 0
    for t in transitions:
        data = {"source": t.source, "state": t.state, "provider": t.provider, "at": now}
        if t.reputation is not None:
            data["reputation"] = round(t.reputation, 4)
        typ = SOURCE_REGISTERED if t.is_new else SOURCE_STATE_CHANGED
        await publish(nc, typ, t.source, data)
        if t.is_new and t.state == "active":
            grandfathered += 1
        elif t.is_new:
            registered += 1
        elif t.state == "active" and records.get(t.source) and records[t.source].state != "active":
            activated += 1
        else:
            refreshed += 1

    # Auto-backfill the bounded slice of registered sources (production lifecycle: register → backfill).
    backfilled = 0
    for src in to_backfill:
        res = await run_backfill(
            nc, src, run_id=run_id_for(src, now), at=now, depth=backfill_cfg["depth"],
            gate_prompt=backfill_cfg["gate_prompt"], known_good=backfill_cfg["known_good"],
            denied=backfill_cfg["denied"], seen=backfill_cfg["seen"],
        )
        backfilled += 1
        print(f"[registry] auto-backfill {src}: ingested {res.ingested}/{res.fetched}", flush=True)

    await nc.flush()
    await nc.close()
    print(
        f"[registry] {len(transitions)} transitions: {grandfathered} grandfathered active, "
        f"{registered} newly registered (pending), {activated} activated, {refreshed} reputation refresh"
        + (f"; auto-backfilled {backfilled}" if backfilled else "")
    )


if __name__ == "__main__":
    asyncio.run(main())
