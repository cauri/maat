"""Projection-harvester pure transform (#39, §5/§8, P3).

Converts `clusters` projection rows into snapshot event dicts, stamped with a harvest
timestamp. Used by `scripts/harvest.py` (the async runner) and unit-tested without DB/NATS.

Idempotency guarantee: each (cluster_id, harvest_date) pair maps to a fixed stream_id,
so re-running the script on the same calendar day emits the same stream_ids and the kernel
treats the second publish as a no-op (same stream_id, event already in log).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

# Registered event type; maat-kerneld folds cluster.snapshot into the cluster_snapshots
# projection, idempotent per (cluster_id, calendar-day) (#39).
from maat.events import CLUSTER_SNAPSHOT


def _snapshot_id(cluster_id: str, harvest_date: str) -> str:
    """Stable, compact stream_id for a (cluster_id, calendar-date) pair.

    A sha1 prefix keeps it URL-safe and bounded.  Keying on the calendar date (not the
    full timestamp) means a second run on the same day produces the same stream_id, so the
    event log does not accumulate duplicates.
    """
    key = f"{cluster_id}:{harvest_date}"
    return "snap-" + hashlib.sha1(key.encode()).hexdigest()[:18]


def harvest(
    cluster_rows: list[dict[str, Any]],
    *,
    at: datetime,
) -> list[dict[str, Any]]:
    """Pure transform: cluster projection rows → event dicts ready to publish.

    Args:
        cluster_rows: Each dict must carry at minimum:
            ``id``  — the cluster's stable primary key.
            ``fact`` — the canonical fact text.
            ``independent_originators`` — int count of collapsed originator groups.
            ``has_primary`` — bool, whether a primary/authoritative source was matched.
            Optional (with defaults):
            ``extremity``   — "notable" | "extraordinary" | … (default "notable").
            ``confidence``  — float 0–1 (default 0.0).
            ``sources``     — list[str] of source names for the cluster (default []).
            ``originators`` — list[list[str]] collapsed originator groups (default []).
            ``corrected``   — bool: any member claim corrected / laundering-flagged (default False).
            ``tenant_id``   — (default "cauri").
        at: The harvest timestamp (UTC). Embedded in the event payload and used as the
            deduplication date (YYYY-MM-DD, so two runs in the same day are idempotent).

    Returns:
        List of dicts with keys ``stream_id``, ``type``, ``data``, ``tenant_id``.
        Each dict maps 1-to-1 with a NATS publish call in the async runner.
    """
    harvest_date = at.strftime("%Y-%m-%d")
    events: list[dict[str, Any]] = []
    for row in cluster_rows:
        cluster_id = row["id"]
        data: dict[str, Any] = {
            "cluster_id": cluster_id,
            "fact": row["fact"],
            "independent_originators": int(row["independent_originators"]),
            "has_primary": bool(row["has_primary"]),
            "extremity": row.get("extremity", "notable"),
            "confidence": float(row.get("confidence", 0.0)),
            # Sourcing detail the reputation fold needs (per-source independence + outcomes).
            "sources": list(row.get("sources") or []),
            "originators": list(row.get("originators") or []),
            # Operator/reader refutation already on the member claims (corrected / laundering_flag),
            # surfaced so resolve_outcome can see it — previously dropped before reaching calibration.
            "corrected": bool(row.get("corrected", False)),
            # Primary-source grounding verdict (#228), carried into cluster_snapshots so a
            # contradiction resolves the fact to REFUTED over time. None until the cluster is judged.
            "grounding": row.get("grounding"),
            "harvested_at": at.isoformat(),
        }
        events.append(
            {
                "stream_id": _snapshot_id(cluster_id, harvest_date),
                "type": CLUSTER_SNAPSHOT,
                "data": data,
                "tenant_id": row.get("tenant_id", "cauri"),
            }
        )
    return events
