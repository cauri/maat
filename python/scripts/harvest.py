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
import os
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat.bus import connect
from maat.events import publish
from maat.learning.harvest import harvest

ROOT = Path(__file__).resolve().parents[2]


async def main() -> None:
    load_dotenv(ROOT / ".env")
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    rows = await pool.fetch(
        "select id, tenant_id, fact, independent_originators, has_primary, extremity, confidence"
        " from clusters"
        " where tenant_id = $1",
        os.environ.get("MAAT_TENANT_ID", "cauri"),
    )
    await pool.close()

    cluster_rows = [dict(r) for r in rows]
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
