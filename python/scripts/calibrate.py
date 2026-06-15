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
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat import events
from maat.bus import connect
from maat.learning.calibration import (
    Observation,
    Weights,
    brier_score,
    calibration_bins,
    observations_from_history,
    tune_decay,
    tune_proposals,
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

    if by_outcome.get("confirmed") and not by_outcome.get("refuted"):
        lines.append(
            "\n⚠ every resolved fact CONFIRMED — no refutations in view. Until a refutation signal"
            " (a retraction, or a contradicting fact) feeds the loop, the tuner sees only upside and"
            " its suggestions skew toward higher confidence — treat them as provisional.")

    tuned, tuned_b = tune_decay(obs, base=base)
    lines.append(f"\nSUGGESTED weights — Brier {tuned_b}  (proposal only; needs sign-off)")
    for level in base.decay:
        a, t = base.decay[level], tuned.decay[level]
        if a != t:
            lines.append(f"    decay.{level}: {a} → {t}")
    if tuned.decay == dict(base.decay):
        lines.append("    (no change — the starting points already fit the resolved history)")
    return "\n".join(lines)


async def _file_proposals(obs: list[Observation]) -> int:
    """File the tuner's suggestions as admin.threshold.changed proposals (operator signs off)."""
    proposals = tune_proposals(obs)
    if not proposals:
        print("\nno change to propose — the starting points already fit.")
        return 0
    nc = await connect()
    for p in proposals:
        await events.publish(
            nc, events.ADMIN_THRESHOLD_CHANGED, p["key"],
            events.admin_event(p["key"], actor="auto-tune", reason=p["reason"],
                               key=p["key"], value=p["value"]),
        )
    await nc.flush()
    await nc.close()
    print(f"\nfiled {len(proposals)} proposal(s) → review + sign off in the Config panel:")
    for p in proposals:
        print(f"    {p['key']} → {p['value']}  ({p['reason']})")
    return len(proposals)


async def main() -> None:
    load_dotenv(ROOT / ".env")
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    rows = await pool.fetch(
        "select data from events where type = 'cluster.corroborated' order by id"
    )
    await pool.close()
    history = [json.loads(r["data"]) if isinstance(r["data"], str) else r["data"] for r in rows]
    obs = observations_from_history(history)
    print(_report(obs))
    if "--propose" in sys.argv:  # opt-in: file the suggestions as pending proposals
        await _file_proposals(obs)


if __name__ == "__main__":
    asyncio.run(main())
