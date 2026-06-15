"""Calibration report (P3, §5.8) — have the confidence reads held up, and what weights fit better?

Reads the `cluster.corroborated` history from the event log, resolves each fact's truth-over-time
outcome (did it accrue independent corroboration, stall, or draw a correction?), scores the
current weights against those outcomes, and suggests the decay constants that would have fit
best. READ-ONLY: it prints a proposal — promoting it needs operator sign-off and an
A/B-on-replay pass (the same guardrail the Config panel enforces). Nothing is auto-applied.

Run: uv run python scripts/calibrate.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat.learning.calibration import (
    Observation,
    Weights,
    brier_score,
    calibration_bins,
    observations_from_history,
    tune_decay,
)

ROOT = Path(__file__).resolve().parents[2]


def _report(obs: list[Observation]) -> str:
    by_outcome: dict[str, int] = {}
    for o in obs:
        by_outcome[o.outcome] = by_outcome.get(o.outcome, 0) + 1
    lines = [
        f"facts seen: {len(obs)}",
        "  " + ", ".join(f"{k}={v}" for k, v in sorted(by_outcome.items())) if obs else "  (none)",
    ]
    base = Weights.defaults()
    base_b = brier_score(obs, base)
    if base_b is None:
        lines.append("\nNothing has resolved yet — the loop activates as the clock keeps acquiring.")
        return "\n".join(lines)

    lines.append(f"\ncurrent weights — Brier {base_b} (lower is better-calibrated)")
    lines.append("  reliability (read vs actually-confirmed):")
    for b in calibration_bins(obs, base):
        flag = "  under-confident" if b.predicted + 0.1 < b.actual else (
            "  over-confident" if b.predicted > b.actual + 0.1 else "")
        lines.append(f"    [{b.lo:.2f},{b.hi:.2f})  n={b.n}  read≈{b.predicted}  confirmed={b.actual}{flag}")

    tuned, tuned_b = tune_decay(obs, base=base)
    lines.append(f"\nSUGGESTED weights — Brier {tuned_b}  (proposal only; needs sign-off)")
    for level in base.decay:
        a, t = base.decay[level], tuned.decay[level]
        if a != t:
            lines.append(f"    decay.{level}: {a} → {t}")
    if tuned.decay == dict(base.decay):
        lines.append("    (no change — the starting points already fit the resolved history)")
    return "\n".join(lines)


async def main() -> None:
    load_dotenv(ROOT / ".env")
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    rows = await pool.fetch(
        "select data from events where type = 'cluster.corroborated' order by id"
    )
    await pool.close()
    events = [json.loads(r["data"]) if isinstance(r["data"], str) else r["data"] for r in rows]
    print(_report(observations_from_history(events)))


if __name__ == "__main__":
    asyncio.run(main())
