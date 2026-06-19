"""Per-stage durable-consumer health (#299) — where is the pipeline backing up?

Reads JetStream ``consumer_info`` for each stage's durable on ``MAAT_EVENTS`` (the authoritative
queue depth, not a DB proxy) so the operator sees lag / in-flight / redelivered / throughput per
stage at a glance, plus that stage's dead-letter count. Consumed by the operator console (current
/runs and console_api metrics) and emitted to cat-cafe. Robust: a stage whose consumer isn't bound
yet, or a NATS hiccup, shows as ``present=False`` rather than failing the page.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from maat.bus import EVENTS_STREAM

# Durable consumers on MAAT_EVENTS: the kernel (single writer) + the streaming agent stages
# (#296/#298). The batch agents (corroborate/geotag/...) run per-tick, not as durable consumers.
STAGES: tuple[str, ...] = ("kerneld", "extract", "classify")


@dataclass
class StageHealth:
    stage: str
    pending: int       # LAG — events accepted but not yet delivered to this stage
    in_flight: int     # delivered, awaiting ack (being worked right now)
    redelivered: int   # currently in redelivery (transient handler failures)
    delivered: int     # total delivered to this stage (a throughput odometer)
    dead_letters: int  # poison events recorded for this stage
    present: bool      # False when the consumer isn't bound yet / NATS is unreachable


async def consumer_health(nc: Any, dead_by_stage: dict[str, int] | None = None) -> list[StageHealth]:
    """One :class:`StageHealth` per stage. ``nc`` may be None (NATS down) → all stages absent."""
    dead_by_stage = dead_by_stage or {}
    out: list[StageHealth] = []
    try:
        js = nc.jetstream() if nc is not None else None
    except Exception:  # noqa: BLE001 - nc without a JetStream context (e.g. a test stub) → all absent
        js = None
    for stage in STAGES:
        dead = int(dead_by_stage.get(stage, 0))
        try:
            info = await js.consumer_info(EVENTS_STREAM, stage)
            delivered = getattr(getattr(info, "delivered", None), "consumer_seq", 0) or 0
            out.append(StageHealth(
                stage=stage,
                pending=int(getattr(info, "num_pending", 0) or 0),
                in_flight=int(getattr(info, "num_ack_pending", 0) or 0),
                redelivered=int(getattr(info, "num_redelivered", 0) or 0),
                delivered=int(delivered),
                dead_letters=dead,
                present=True,
            ))
        except Exception:  # noqa: BLE001 - consumer not bound / NATS hiccup: show absent, never 500
            out.append(StageHealth(stage, 0, 0, 0, 0, dead, present=False))
    return out


def health_as_dicts(rows: list[StageHealth]) -> list[dict[str, Any]]:
    """JSON-friendly form for the console API + OTEL."""
    return [asdict(r) for r in rows]


async def dead_letters_by_stage(pool: Any) -> dict[str, int]:
    """Per-stage dead-letter counts. Agent poison carries ``stage`` (#299); the kernel's folding
    failures predate the column (stage NULL) and are bucketed under 'kerneld'."""
    try:
        rows = await pool.fetch(
            "select coalesce(stage, 'kerneld') stage, count(*) n from dead_letters group by 1"
        )
    except Exception:  # noqa: BLE001 - table/column not migrated yet
        return {}
    return {r["stage"]: int(r["n"]) for r in rows}
