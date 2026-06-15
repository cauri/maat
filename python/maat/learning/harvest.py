"""Projection-harvester pure transform (#39, §5/§8, P3).

Converts `clusters` projection rows into snapshot event dicts, stamped with a harvest
timestamp. Used by `scripts/harvest.py` (the async runner) and unit-tested without DB/NATS.

Idempotency guarantee: each (cluster_id, harvest_date) pair maps to a fixed stream_id,
so re-running the script on the same calendar day emits the same stream_ids and the kernel
treats the second publish as a no-op (same stream_id, event already in log).
"""

# follow-up: register type 'cluster.snapshot' in events.py + kernel projection

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

# Local event-type constant — not yet in events.py (see follow-up comment above).
CLUSTER_SNAPSHOT = "cluster.snapshot"


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
            ``extremity``  — "notable" | "extraordinary" | … (default "notable").
            ``confidence`` — float 0–1 (default 0.0).
            ``tenant_id``  — (default "cauri").
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
