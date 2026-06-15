"""Feedback intake (P7, issue #58) — record and read user feedback via the event log.

Feedback items are stored as ``feedback.submitted`` events on the shared append-only log;
the queue is assembled by reading those events back directly (no projection table, no schema).
This keeps the intake path consistent with the event-sourced contract the rest of Maat uses
(D5/D20): the event IS the record.

Usage (programmatic — agents / serving routes call these):

    # record
    await record(pool, nc, item_id="f-001", text="story X has wrong confidence",
                 category_hint="veracity-dispute", source="reader-app")

    # read the queue (all submitted items, latest first)
    items = await queue(pool)

    # read only items with a specific triage outcome
    routed = await routed_queue(pool, route="review")
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from maat.events import envelope, publish

# ---------------------------------------------------------------------------
# Event-type constants (local — never edit events.py)
# ---------------------------------------------------------------------------

FEEDBACK_SUBMITTED = "feedback.submitted"
FEEDBACK_TRIAGED = "feedback.triaged"

# ---------------------------------------------------------------------------
# Stream-id prefix convention: "fb-<uuid4-short>"
# ---------------------------------------------------------------------------

_PREFIX = "fb"


def new_item_id() -> str:
    return f"{_PREFIX}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


async def record(
    pool: Any,
    nc: Any | None,
    *,
    item_id: str | None = None,
    text: str,
    category_hint: str = "",
    source: str = "unknown",
    tenant_id: str = "cauri",
) -> str:
    """Publish a ``feedback.submitted`` event and return the item_id.

    If ``nc`` (NATS connection) is None the event is written directly to the
    Postgres ``events`` table (sync / batch path, e.g. in tests or batch API).
    If ``nc`` is provided we go via NATS so the kernel is the single writer (D5).
    """
    fid = item_id or new_item_id()
    data: dict[str, Any] = {
        "item_id": fid,
        "text": text,
        "category_hint": category_hint,
        "source": source,
    }
    if nc is not None:
        await publish(nc, FEEDBACK_SUBMITTED, fid, data, tenant_id)
    else:
        # Direct-write path (no NATS) — used by tests and batch scripts.
        raw = envelope(fid, FEEDBACK_SUBMITTED, data, tenant_id)
        payload = json.loads(raw)
        await pool.execute(
            "insert into events (stream_id, type, data, tenant_id) values ($1,$2,$3,$4)",
            payload["stream_id"],
            payload["type"],
            json.dumps(payload["data"]),
            payload["tenant_id"],
        )
    return fid


async def record_triage(
    pool: Any,
    nc: Any | None,
    *,
    item_id: str,
    category: str,
    route: str,
    confidence: float = 1.0,
    reason: str = "",
    auto_fixable: bool = False,
    tenant_id: str = "cauri",
) -> None:
    """Publish a ``feedback.triaged`` event for an already-submitted item."""
    data: dict[str, Any] = {
        "item_id": item_id,
        "category": category,
        "route": route,
        "confidence": confidence,
        "reason": reason,
        "auto_fixable": auto_fixable,
    }
    if nc is not None:
        await publish(nc, FEEDBACK_TRIAGED, item_id, data, tenant_id)
    else:
        raw = envelope(item_id, FEEDBACK_TRIAGED, data, tenant_id)
        payload = json.loads(raw)
        await pool.execute(
            "insert into events (stream_id, type, data, tenant_id) values ($1,$2,$3,$4)",
            payload["stream_id"],
            payload["type"],
            json.dumps(payload["data"]),
            payload["tenant_id"],
        )


# ---------------------------------------------------------------------------
# Read path — direct ``events`` queries (no schema, no projection table)
# ---------------------------------------------------------------------------


async def queue(
    pool: Any,
    *,
    limit: int = 200,
    tenant_id: str = "cauri",
) -> list[dict[str, Any]]:
    """Return all ``feedback.submitted`` events, latest first.

    Each item is a plain dict with the submitted data fields plus ``submitted_at``.
    """
    rows = await pool.fetch(
        "select stream_id, data, created_at from events "
        "where type = $1 and tenant_id = $2 "
        "order by id desc limit $3",
        FEEDBACK_SUBMITTED,
        tenant_id,
        limit,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = json.loads(r["data"]) if isinstance(r["data"], str) else dict(r["data"])
        d["submitted_at"] = r["created_at"]
        out.append(d)
    return out


async def routed_queue(
    pool: Any,
    *,
    route: str,
    limit: int = 200,
    tenant_id: str = "cauri",
) -> list[dict[str, Any]]:
    """Return triage outcomes filtered by ``route`` ('review' or 'auto-fix').

    Joins the latest triage event per item back to the original submission so
    the caller gets the full picture in one call.
    """
    # Grab the most-recent triage event per item_id
    triage_rows = await pool.fetch(
        "select distinct on (data->>'item_id') "
        "data->>'item_id' item_id, data, created_at "
        "from events "
        "where type = $1 and tenant_id = $2 "
        "and data->>'route' = $3 "
        "order by data->>'item_id', id desc",
        FEEDBACK_TRIAGED,
        tenant_id,
        route,
    )
    if not triage_rows:
        return []

    item_ids = [r["item_id"] for r in triage_rows]

    # Fetch the originating submission for each item
    sub_rows = await pool.fetch(
        "select stream_id, data from events "
        "where type = $1 and tenant_id = $2 "
        "and stream_id = any($3::text[]) "
        "order by id asc",
        FEEDBACK_SUBMITTED,
        tenant_id,
        item_ids,
    )
    sub_by_id: dict[str, dict] = {}
    for r in sub_rows:
        d = json.loads(r["data"]) if isinstance(r["data"], str) else dict(r["data"])
        sub_by_id[r["stream_id"]] = d

    out: list[dict[str, Any]] = []
    for tr in triage_rows:
        td = json.loads(tr["data"]) if isinstance(tr["data"], str) else dict(tr["data"])
        fid = td.get("item_id", tr["item_id"])
        sub = sub_by_id.get(fid, {})
        out.append({
            **sub,
            "triage": td,
            "triaged_at": tr["created_at"],
        })
    return out[:limit]
