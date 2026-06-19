"""Projection-harvester clock (#39, §5/§8, P3) — snapshot the clusters projection into the event log.

The calibration/accuracy-over-time loop (`calibrate.py` + `maat/learning/calibration.py`) needs a
trajectory: how did a fact's corroboration evolve *over time*? The kernel updates the `clusters`
projection in-place (last-write-wins), so point-in-time snapshots must be captured explicitly.
This script emits one `cluster.snapshot` event per live cluster, stamped with the run timestamp.
Re-running is safe: events are deduplicated on a stable key (`cluster_id + harvest_date`), so
a retry or a second run on the same calendar day produces no duplicates.

Mirrors `scripts/clock.py` in structure: pure transform in `maat/learning/harvest.py` →
async `main()` here wires DB + NATS.

Run: uv run python scripts/harvest.py   (or `make harvest`, on a schedule alongside the clock)
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat.bus import connect
from maat.events import publish
from maat.learning.harvest import harvest

ROOT = Path(__file__).resolve().parents[2]


def _as_list(v: object) -> list:
    """asyncpg returns jsonb as text here (no codec registered) — parse to a plain list."""
    if isinstance(v, str):
        return json.loads(v) if v else []
    return list(v) if v else []


async def main() -> None:
    load_dotenv(ROOT / ".env")
    pool = await get_pool()
    rows = await pool.fetch(
        "select c.id, c.tenant_id, c.fact, c.independent_originators, c.has_primary,"
        " c.extremity, c.confidence, c.sources, c.originators, c.grounding,"
        " exists ("
        "   select 1 from jsonb_array_elements_text(c.claim_ids) as t(cid)"
        "   join claims cl on cl.id = t.cid::uuid"
        "   where cl.corrected or cl.laundering_flag is not null or cl.disputed"
        " ) as corrected"
        " from clusters c"
        " where c.tenant_id = $1",
        os.environ.get("MAAT_TENANT_ID", "cauri"),
    )
    await pool.close()

    cluster_rows = []
    for r in rows:
        d = dict(r)
        d["sources"] = _as_list(d.get("sources"))
        d["originators"] = _as_list(d.get("originators"))
        cluster_rows.append(d)
    at = datetime.now(tz=timezone.utc)
    event_dicts = harvest(cluster_rows, at=at)

    if not event_dicts:
        print("harvest: no clusters in projection — nothing to snapshot")
        return

    nc = await connect()
    for ev in event_dicts:
        await publish(nc, ev["type"], ev["stream_id"], ev["data"], ev["tenant_id"])
    await nc.flush()
    await nc.close()
    print(f"harvest: {len(event_dicts)} cluster snapshot(s) published at {at.isoformat()}")


if __name__ == "__main__":
    asyncio.run(main())
