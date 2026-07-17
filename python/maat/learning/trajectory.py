"""The truth-over-time trajectory the learning folds read (#39, closing the loop).

Source of truth = the `cluster_snapshots` projection: the projection-harvester
(`scripts/harvest.py`) emits one `cluster.snapshot` per live cluster per run, and maat-kerneld
folds them into `cluster_snapshots` — one row per (cluster_id, calendar day), idempotent. Grouped
by fact, oldest→newest, that table IS the trajectory `observations_from_history`,
`fold_reputation`, `lifecycle_by_fact`, and `policy_step` fold over.

Falls back to the legacy `cluster.corroborated` event stream when the snapshot table is empty
(fresh DB / before the first harvest tick), so the learning views never blank on cutover.

Returns the same dict shape the folds already expect (`fact`, `independent_originators`,
`has_primary`, `extremity`, `confidence`, `sources`, `originators`, `corrected`, plus `ts` from
the snapshot's `harvested_at` for accuracy-staleness / calibration-freshness).
"""

from __future__ import annotations

import json
from typing import Any

# One row per (cluster, day); order by day so the per-fact fold sees oldest→newest.
_SNAPSHOT_QUERY = (
    "select fact, independent_originators, has_primary, extremity, confidence, "
    "sources, originators, corrected, grounding, harvested_at, cluster_id "
    "from cluster_snapshots order by snapshot_day, id"
)
_FALLBACK_QUERY = "select data from events where type = 'cluster.corroborated' order by id"


def _as_list(v: Any) -> list:
    """asyncpg returns jsonb as text here (no codec registered) — parse to a plain list."""
    if isinstance(v, str):
        return json.loads(v) if v else []
    return list(v) if v else []


def _snapshot_to_dict(r: Any) -> dict:
    """Map a cluster_snapshots row to the event-dict shape the folds consume."""
    ts = r["harvested_at"]
    return {
        "fact": r["fact"],
        "independent_originators": r["independent_originators"],
        "has_primary": r["has_primary"],
        "extremity": r["extremity"],
        "confidence": r["confidence"],
        "sources": _as_list(r["sources"]),
        "originators": _as_list(r["originators"]),
        "corrected": r["corrected"],
        "grounding": r["grounding"],
        "cluster_id": r["cluster_id"],
        "ts": ts.isoformat() if hasattr(ts, "isoformat") else ts,
    }


def _jobj(v: Any) -> dict:
    return json.loads(v) if isinstance(v, str) else (v or {})


async def load_trajectory(pool: Any) -> list[dict]:
    """Load the trajectory the learning folds read, oldest→newest.

    Snapshot projection first; the legacy `cluster.corroborated` stream as a fallback until the
    first harvest. Pure I/O shell — the folds take it from here.
    """
    rows = await pool.fetch(_SNAPSHOT_QUERY)
    if rows:
        return [_snapshot_to_dict(r) for r in rows]
    rows = await pool.fetch(_FALLBACK_QUERY)  # nothing harvested yet — keep the views alive
    return [_jobj(r["data"]) for r in rows]


async def load_hindsight(pool: Any) -> list[dict]:
    """The ``fact.hindsight`` outcomes (#435), latest-per-stream — the exogenous rows
    ``fold_reputation(hindsight=…)`` anchors reputation with.

    Latest-per-stream is the re-assert contract: a re-run of the backfill for one outlet emits the
    same stream ids, so an updated resolution REPLACES the old outcome instead of double-counting.
    Bounded: one indexed type-scan over a stream that grows by design in the hundreds, not the
    millions. Missing table / no events → empty (reputation then runs exactly as before)."""
    try:
        rows = await pool.fetch(
            "select stream_id, data from events where type = 'fact.hindsight' order by id"
        )
    except Exception:  # noqa: BLE001 - events table unavailable → no hindsight, never a crash
        return []
    latest: dict[str, dict] = {}
    for r in rows:
        d = r["data"]
        latest[r["stream_id"]] = json.loads(d) if isinstance(d, str) else d
    return list(latest.values())
