"""Production calibration runner (P7, Issue #60).

Reads the `cluster.corroborated` event history from the DB, runs the full calibration
pipeline via `production_calibration`, and prints (or returns) a CalibrationStatus.

This is a thin I/O shell — the logic lives in `maat/learning/calibration_prod.py` and is
fully testable without a DB. Nothing is auto-applied; the sign-off gate is preserved.

Usage
-----
    uv run python scripts/calibrate_prod.py
    uv run python scripts/calibrate_prod.py --propose   # file pending proposals

Scheduling
----------
Run from a cron job or systemd timer (e.g. daily) to keep the P8 dashboard fresh.
The script is idempotent — repeated runs are safe.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat import events
from maat.bus import connect
from maat.learning.calibration_prod import format_status, production_calibration
from maat.learning.trajectory import load_trajectory

ROOT = Path(__file__).resolve().parents[2]


async def main() -> None:
    load_dotenv(ROOT / ".env")
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    history = await load_trajectory(pool)
    await pool.close()

    status = production_calibration(history)
    print(format_status(status))

    if "--propose" in sys.argv:
        await _file_proposals(status)


async def _file_proposals(status) -> None:  # type: ignore[no-untyped-def]
    """File the tuner's suggestions as admin.threshold.changed proposals."""
    if not status.proposals:
        print("\nno change to propose — the starting points already fit.")
        return

    if status.refutation_bias:
        print(
            "\nWARNING: refutation_bias is set — proposals skew optimistic (no refutations"
            " in history). Filing anyway; treat as provisional until refutation data feeds in."
        )

    nc = await connect()
    for p in status.proposals:
        await events.publish(
            nc,
            events.ADMIN_THRESHOLD_CHANGED,
            p["key"],
            events.admin_event(
                p["key"],
                actor="auto-tune",
                reason=p["reason"],
                key=p["key"],
                value=p["value"],
            ),
        )
    await nc.flush()
    await nc.close()
    print(f"\nfiled {len(status.proposals)} proposal(s) -> review + sign off in the Config panel:")
    for p in status.proposals:
        print(f"    {p['key']} -> {p['value']}  ({p['reason']})")


if __name__ == "__main__":
    asyncio.run(main())
