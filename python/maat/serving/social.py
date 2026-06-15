"""Social layer (P5, issue #49): comments + pins, event-sourced.

Users comment on stories and pin them to their reading list.  All state is
stored as events on the shared append-only log and folded into views here —
no separate schema or migration required.

Event types (local constants — never import from maat.events):
    COMMENT_ADDED    — a new comment attached to a story cluster
    COMMENT_DELETED  — a comment removed (soft-delete; the body is gone)
    STORY_PINNED     — a user added a story/cluster to their pin list
    STORY_UNPINNED   — a user removed a story from their pin list

Tenancy model:
    - Every event carries a ``tenant_id``.  Views are scoped by tenant so
      different tenants' data never leaks across boundaries.
    - Comments are per-tenant (all users within a tenant share a comment
      thread on a cluster).
    - Pins are per-user *within* a tenant — ``user_id`` is the isolation
      key for pins.
    - Ordering: comments are returned oldest-first (chronological read
      order); pins return the cluster_id list in pin order (most recent
      first — the user sees what they pinned last at the top).

Builder functions are pure (no I/O) and testable without a DB.  The event
dict shape mirrors what the kernel writes:
    {
        "type": str,
        "tenant_id": str,
        "data": {
            "user_id": str,
            "cluster_id": str,          # or "story_id" alias
            "comment_id": str,          # COMMENT_ADDED / COMMENT_DELETED
            "body": str,                # COMMENT_ADDED only
            ...
        },
        "created_at": datetime | None,  # optional; used for ordering
    }
"""

from __future__ import annotations

import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Local event-type constants (do NOT import from maat.events)
# ---------------------------------------------------------------------------

COMMENT_ADDED = "comment.added"
COMMENT_DELETED = "comment.deleted"
STORY_PINNED = "story.pinned"
STORY_UNPINNED = "story.unpinned"

SOCIAL_EVENT_TYPES = frozenset(
    {COMMENT_ADDED, COMMENT_DELETED, STORY_PINNED, STORY_UNPINNED}
)

# ---------------------------------------------------------------------------
# Event builders — return the dict payload; the caller publishes it
# ---------------------------------------------------------------------------


def make_comment_added(
    *,
    tenant_id: str,
    user_id: str,
    cluster_id: str,
    body: str,
    comment_id: str | None = None,
) -> dict[str, Any]:
    """Build a ``comment.added`` event payload.

    Args:
        tenant_id:  Tenant scope for isolation.
        user_id:    Author of the comment.
        cluster_id: Story cluster being commented on.
        body:       Comment text (non-empty; callers should validate).
        comment_id: Stable id for this comment.  Auto-generated when omitted.

    Returns:
        Event dict ready for ``maat.events.envelope()`` / direct DB insert.
    """
    return {
        "type": COMMENT_ADDED,
        "tenant_id": tenant_id,
        "data": {
            "comment_id": comment_id or str(uuid.uuid4()),
            "user_id": user_id,
            "cluster_id": cluster_id,
            "body": body,
        },
    }


def make_comment_deleted(
    *,
    tenant_id: str,
    user_id: str,
    cluster_id: str,
    comment_id: str,
) -> dict[str, Any]:
    """Build a ``comment.deleted`` event payload.

    Only the original author (or an admin) should delete; callers enforce
    that policy — this builder is intentionally dumb about authorisation.
    """
    return {
        "type": COMMENT_DELETED,
        "tenant_id": tenant_id,
        "data": {
            "comment_id": comment_id,
            "user_id": user_id,
            "cluster_id": cluster_id,
        },
    }


def make_story_pinned(
    *,
    tenant_id: str,
    user_id: str,
    cluster_id: str,
) -> dict[str, Any]:
    """Build a ``story.pinned`` event payload."""
    return {
        "type": STORY_PINNED,
        "tenant_id": tenant_id,
        "data": {
            "user_id": user_id,
            "cluster_id": cluster_id,
        },
    }


def make_story_unpinned(
    *,
    tenant_id: str,
    user_id: str,
    cluster_id: str,
) -> dict[str, Any]:
    """Build a ``story.unpinned`` event payload."""
    return {
        "type": STORY_UNPINNED,
        "tenant_id": tenant_id,
        "data": {
            "user_id": user_id,
            "cluster_id": cluster_id,
        },
    }


# ---------------------------------------------------------------------------
# Pure view builders — fold an event list into current state
# ---------------------------------------------------------------------------


def build_comments(
    events: list[dict[str, Any]],
    *,
    tenant_id: str,
    cluster_id: str,
) -> list[dict[str, Any]]:
    """Fold ``events`` into the current comment thread for a cluster, per-tenant.

    Events are processed in list order (oldest first = the order the DB
    returns them with ``ORDER BY id ASC`` or ``ORDER BY created_at ASC``).

    Deleted comment ids are removed; surviving comments are returned in
    chronological order with the fields:
        - comment_id (str)
        - user_id    (str)
        - cluster_id (str)
        - body       (str)
        - created_at (datetime | None)

    Tenant isolation: events for other tenants are silently skipped.
    Cross-cluster events (same tenant, different cluster) are also skipped.

    Args:
        events:     Raw event dicts from the ``events`` table.
        tenant_id:  Scope the view to this tenant.
        cluster_id: Scope the view to this story cluster.

    Returns:
        List of comment dicts, oldest first.  Empty when no comments exist.
    """
    live: dict[str, dict[str, Any]] = {}  # comment_id -> comment
    seen: set[str] = set()                 # all ids ever added (for stable order)
    order: list[str] = []                  # first-add insertion order

    for ev in events:
        if ev.get("tenant_id") != tenant_id:
            continue
        ev_type = ev.get("type")
        data = ev.get("data", {})

        if data.get("cluster_id") != cluster_id:
            continue

        if ev_type == COMMENT_ADDED:
            cid = data.get("comment_id")
            if not cid:
                continue
            if cid not in seen:
                seen.add(cid)
                order.append(cid)
            live[cid] = {
                "comment_id": cid,
                "user_id": data.get("user_id", ""),
                "cluster_id": cluster_id,
                "body": data.get("body", ""),
                "created_at": ev.get("created_at"),
            }

        elif ev_type == COMMENT_DELETED:
            cid = data.get("comment_id")
            if cid and cid in live:
                del live[cid]

    return [live[cid] for cid in order if cid in live]


def build_user_pins(
    events: list[dict[str, Any]],
    *,
    tenant_id: str,
    user_id: str,
) -> list[str]:
    """Fold ``events`` into the set of cluster_ids pinned by a user, per-tenant.

    Returns cluster ids in reverse pin-order (most recently pinned first).
    Unpinning removes the cluster from the list entirely; re-pinning after
    an unpin re-adds it at the top.

    Tenant isolation: events for other tenants are silently skipped.
    User isolation: events for other users within the same tenant are skipped.

    Args:
        events:    Raw event dicts from the ``events`` table, oldest first.
        tenant_id: Scope the view to this tenant.
        user_id:   Scope the view to this user.

    Returns:
        Ordered list of cluster_ids (most recently pinned first).
    """
    pinned: list[str] = []   # ordered oldest first; reverse at the end

    for ev in events:
        if ev.get("tenant_id") != tenant_id:
            continue
        data = ev.get("data", {})
        if data.get("user_id") != user_id:
            continue
        ev_type = ev.get("type")
        cid = data.get("cluster_id", "")

        if ev_type == STORY_PINNED:
            # Remove any prior pin of the same cluster so re-pin moves it to top
            if cid in pinned:
                pinned.remove(cid)
            pinned.append(cid)

        elif ev_type == STORY_UNPINNED:
            if cid in pinned:
                pinned.remove(cid)

    # Reverse so the most recently pinned is first
    pinned.reverse()
    return pinned
