"""Event envelope + publish helpers, matching the Rust kernel's contract (maat-kerneld).

Subjects are `maat.events.<type>`; the JSON payload is the EventEnvelope the kernel decodes
and appends to the log.
"""

from __future__ import annotations

import json
from typing import Any

SUBJECT_PREFIX = "maat.events"

# --- Admin / operator-console actions (P8) ---------------------------------------------
# Every operator mutation is a typed event on the same append-only log the agents write to
# (D5/D20): the console publishes these; maat-kerneld is the single writer that folds them
# into the projections. The event log is therefore the audit trail for free.
ADMIN_CLASSIFICATION_CORRECTED = "admin.classification.corrected"  # F3: fix kind/voice/speaker
ADMIN_LAUNDERING_FLAGGED = "admin.laundering.flagged"  # F3: §5.2 abuse the classifier missed
ADMIN_CLUSTER_SPLIT = "admin.cluster.split"  # F3: an over-merged cluster, pulled apart (#20)
ADMIN_CLUSTER_MERGED = "admin.cluster.merged"  # F3: distinct clusters that are one fact
ADMIN_CLAIM_MOVED = "admin.claim.moved"  # F3: a claim moved between clusters
ADMIN_THRESHOLD_CHANGED = "admin.threshold.changed"  # F5 (reserved)
ADMIN_RUN_TRIGGERED = "admin.run.triggered"  # F4 (reserved)

ADMIN_EVENT_TYPES = frozenset(
    {
        ADMIN_CLASSIFICATION_CORRECTED,
        ADMIN_LAUNDERING_FLAGGED,
        ADMIN_CLUSTER_SPLIT,
        ADMIN_CLUSTER_MERGED,
        ADMIN_CLAIM_MOVED,
        ADMIN_THRESHOLD_CHANGED,
        ADMIN_RUN_TRIGGERED,
    }
)


def admin_event(
    target: str, *, actor: str = "operator", reason: str = "", **fields: Any
) -> dict[str, Any]:
    """Build the data payload for an admin action event.

    `target` is the primary subject (a claim or cluster id, also used as the event stream_id);
    `actor`/`reason` make the audit line answerable ("who, why"); `fields` carry the change
    itself (e.g. kind=, voice=, abuse=, into=). Pure — the caller publishes it.
    """
    return {"target": target, "actor": actor, "reason": reason, **fields}


def envelope(stream_id: str, type_: str, data: dict[str, Any], tenant_id: str = "cauri") -> bytes:
    return json.dumps(
        {"stream_id": stream_id, "type": type_, "data": data, "tenant_id": tenant_id}
    ).encode()


async def publish(
    nc: Any, type_: str, stream_id: str, data: dict[str, Any], tenant_id: str = "cauri"
) -> None:
    await nc.publish(f"{SUBJECT_PREFIX}.{type_}", envelope(stream_id, type_, data, tenant_id))
