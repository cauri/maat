"""Tests for maat.serving.social (P5, issue #49).

All tests are pure — no DB, no NATS, no I/O.

Coverage:
    - Event builder return shapes (type, tenant_id, data keys)
    - Comment lifecycle: add → view → delete → gone
    - Re-add after delete restores the comment
    - Deleted comment whose id was never added is a no-op
    - Pin / unpin lifecycle
    - Re-pin after unpin moves cluster to top
    - Unpin a never-pinned cluster is a no-op
    - Tenant isolation: events from tenant B invisible to tenant A views
    - User isolation: pins from user B invisible to user A's pin view
    - Cross-cluster isolation: comments from cluster B invisible in cluster A view
    - Ordering: comments are oldest-first; pins are most-recent-first
    - Event constants are stable strings, not reused from maat.events
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from maat.serving.social import (
    COMMENT_ADDED,
    COMMENT_DELETED,
    SOCIAL_EVENT_TYPES,
    STORY_PINNED,
    STORY_UNPINNED,
    build_comments,
    build_user_pins,
    make_comment_added,
    make_comment_deleted,
    make_story_pinned,
    make_story_unpinned,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

T1 = "tenant-alpha"
T2 = "tenant-beta"
U1 = "user-alice"
U2 = "user-bob"
CL1 = "cluster-001"
CL2 = "cluster-002"


def _ts(offset_secs: int = 0) -> dt.datetime:
    return dt.datetime(2026, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc) + dt.timedelta(
        seconds=offset_secs
    )


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------


def test_event_type_strings_are_stable():
    assert COMMENT_ADDED == "comment.added"
    assert COMMENT_DELETED == "comment.deleted"
    assert STORY_PINNED == "story.pinned"
    assert STORY_UNPINNED == "story.unpinned"


def test_social_event_types_frozenset_contains_all():
    assert SOCIAL_EVENT_TYPES == {
        COMMENT_ADDED,
        COMMENT_DELETED,
        STORY_PINNED,
        STORY_UNPINNED,
    }


# ---------------------------------------------------------------------------
# Builder shapes
# ---------------------------------------------------------------------------


def test_make_comment_added_shape():
    ev = make_comment_added(
        tenant_id=T1, user_id=U1, cluster_id=CL1, body="great story"
    )
    assert ev["type"] == COMMENT_ADDED
    assert ev["tenant_id"] == T1
    d = ev["data"]
    assert d["user_id"] == U1
    assert d["cluster_id"] == CL1
    assert d["body"] == "great story"
    assert "comment_id" in d and d["comment_id"]  # auto-generated uuid


def test_make_comment_added_explicit_comment_id():
    ev = make_comment_added(
        tenant_id=T1, user_id=U1, cluster_id=CL1, body="hi", comment_id="cmt-42"
    )
    assert ev["data"]["comment_id"] == "cmt-42"


def test_make_comment_deleted_shape():
    ev = make_comment_deleted(
        tenant_id=T1, user_id=U1, cluster_id=CL1, comment_id="cmt-1"
    )
    assert ev["type"] == COMMENT_DELETED
    assert ev["tenant_id"] == T1
    d = ev["data"]
    assert d["comment_id"] == "cmt-1"
    assert d["user_id"] == U1
    assert d["cluster_id"] == CL1


def test_make_story_pinned_shape():
    ev = make_story_pinned(tenant_id=T1, user_id=U1, cluster_id=CL1)
    assert ev["type"] == STORY_PINNED
    assert ev["tenant_id"] == T1
    assert ev["data"] == {"user_id": U1, "cluster_id": CL1}


def test_make_story_unpinned_shape():
    ev = make_story_unpinned(tenant_id=T1, user_id=U1, cluster_id=CL1)
    assert ev["type"] == STORY_UNPINNED
    assert ev["tenant_id"] == T1
    assert ev["data"] == {"user_id": U1, "cluster_id": CL1}


# ---------------------------------------------------------------------------
# Comment lifecycle
# ---------------------------------------------------------------------------


def _comment_ev(
    type_: str,
    *,
    tenant: str = T1,
    user: str = U1,
    cluster: str = CL1,
    body: str = "",
    comment_id: str = "cmt-1",
    created_at: dt.datetime | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "comment_id": comment_id,
        "user_id": user,
        "cluster_id": cluster,
    }
    if body:
        data["body"] = body
    return {"type": type_, "tenant_id": tenant, "data": data, "created_at": created_at}


def test_empty_event_list_returns_no_comments():
    assert build_comments([], tenant_id=T1, cluster_id=CL1) == []


def test_single_comment_appears():
    ev = _comment_ev(COMMENT_ADDED, body="hello", comment_id="c1", created_at=_ts())
    result = build_comments([ev], tenant_id=T1, cluster_id=CL1)
    assert len(result) == 1
    c = result[0]
    assert c["comment_id"] == "c1"
    assert c["user_id"] == U1
    assert c["cluster_id"] == CL1
    assert c["body"] == "hello"
    assert c["created_at"] == _ts()


def test_deleted_comment_disappears():
    events = [
        _comment_ev(COMMENT_ADDED, body="oops", comment_id="c1"),
        _comment_ev(COMMENT_DELETED, comment_id="c1"),
    ]
    assert build_comments(events, tenant_id=T1, cluster_id=CL1) == []


def test_delete_nonexistent_comment_is_noop():
    events = [
        _comment_ev(COMMENT_ADDED, body="hi", comment_id="c1"),
        _comment_ev(COMMENT_DELETED, comment_id="c-ghost"),  # never existed
    ]
    result = build_comments(events, tenant_id=T1, cluster_id=CL1)
    assert len(result) == 1
    assert result[0]["comment_id"] == "c1"


def test_readd_after_delete_is_new_comment():
    """Re-adding a comment after deleting it should restore it (different body possible)."""
    events = [
        _comment_ev(COMMENT_ADDED, body="first draft", comment_id="c1"),
        _comment_ev(COMMENT_DELETED, comment_id="c1"),
        _comment_ev(COMMENT_ADDED, body="better version", comment_id="c1"),
    ]
    result = build_comments(events, tenant_id=T1, cluster_id=CL1)
    assert len(result) == 1
    assert result[0]["body"] == "better version"


def test_multiple_comments_preserve_insertion_order():
    events = [
        _comment_ev(COMMENT_ADDED, body="first", comment_id="c1", created_at=_ts(0)),
        _comment_ev(COMMENT_ADDED, body="second", comment_id="c2", created_at=_ts(10)),
        _comment_ev(COMMENT_ADDED, body="third", comment_id="c3", created_at=_ts(20)),
    ]
    result = build_comments(events, tenant_id=T1, cluster_id=CL1)
    assert [c["body"] for c in result] == ["first", "second", "third"]


def test_delete_middle_comment_preserves_others():
    events = [
        _comment_ev(COMMENT_ADDED, body="first", comment_id="c1"),
        _comment_ev(COMMENT_ADDED, body="second", comment_id="c2"),
        _comment_ev(COMMENT_ADDED, body="third", comment_id="c3"),
        _comment_ev(COMMENT_DELETED, comment_id="c2"),
    ]
    result = build_comments(events, tenant_id=T1, cluster_id=CL1)
    assert [c["comment_id"] for c in result] == ["c1", "c3"]


# ---------------------------------------------------------------------------
# Tenant isolation — comments
# ---------------------------------------------------------------------------


def test_comments_tenant_isolation():
    """Comments from tenant B must not appear in tenant A's view."""
    events = [
        _comment_ev(COMMENT_ADDED, body="tenant A comment", comment_id="ca1", tenant=T1),
        _comment_ev(COMMENT_ADDED, body="tenant B comment", comment_id="cb1", tenant=T2),
    ]
    result = build_comments(events, tenant_id=T1, cluster_id=CL1)
    assert len(result) == 1
    assert result[0]["comment_id"] == "ca1"


def test_comments_tenant_b_isolated_from_tenant_a():
    events = [
        _comment_ev(COMMENT_ADDED, body="A's comment", comment_id="ca1", tenant=T1),
        _comment_ev(COMMENT_ADDED, body="B's comment", comment_id="cb1", tenant=T2),
    ]
    result = build_comments(events, tenant_id=T2, cluster_id=CL1)
    assert len(result) == 1
    assert result[0]["comment_id"] == "cb1"


def test_cross_tenant_delete_does_not_remove_comment():
    """A delete event from tenant B must not remove tenant A's comment."""
    events = [
        _comment_ev(COMMENT_ADDED, body="A's comment", comment_id="c1", tenant=T1),
        # Tenant B sends a delete for the same comment_id — should be ignored for T1
        _comment_ev(COMMENT_DELETED, comment_id="c1", tenant=T2),
    ]
    result = build_comments(events, tenant_id=T1, cluster_id=CL1)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Cross-cluster isolation — comments
# ---------------------------------------------------------------------------


def test_comments_cluster_isolation():
    """Comments on CL2 must not appear in the CL1 view."""
    events = [
        _comment_ev(COMMENT_ADDED, body="about CL1", comment_id="c1", cluster=CL1),
        _comment_ev(COMMENT_ADDED, body="about CL2", comment_id="c2", cluster=CL2),
    ]
    result = build_comments(events, tenant_id=T1, cluster_id=CL1)
    assert len(result) == 1
    assert result[0]["comment_id"] == "c1"


# ---------------------------------------------------------------------------
# Pin lifecycle
# ---------------------------------------------------------------------------


def _pin_ev(
    type_: str,
    *,
    tenant: str = T1,
    user: str = U1,
    cluster: str = CL1,
) -> dict[str, Any]:
    return {
        "type": type_,
        "tenant_id": tenant,
        "data": {"user_id": user, "cluster_id": cluster},
    }


def test_empty_event_list_returns_no_pins():
    assert build_user_pins([], tenant_id=T1, user_id=U1) == []


def test_single_pin_appears():
    ev = _pin_ev(STORY_PINNED, cluster=CL1)
    result = build_user_pins([ev], tenant_id=T1, user_id=U1)
    assert result == [CL1]


def test_unpin_removes_story():
    events = [
        _pin_ev(STORY_PINNED, cluster=CL1),
        _pin_ev(STORY_UNPINNED, cluster=CL1),
    ]
    assert build_user_pins(events, tenant_id=T1, user_id=U1) == []


def test_unpin_nonexistent_cluster_is_noop():
    events = [
        _pin_ev(STORY_PINNED, cluster=CL1),
        _pin_ev(STORY_UNPINNED, cluster=CL2),  # CL2 was never pinned
    ]
    result = build_user_pins(events, tenant_id=T1, user_id=U1)
    assert result == [CL1]


def test_repin_after_unpin_moves_to_top():
    """Pinning CL1, then CL2, then unpinning CL1, then pinning CL1 again
    should place CL1 at the top of the list (most recently pinned)."""
    events = [
        _pin_ev(STORY_PINNED, cluster=CL1),
        _pin_ev(STORY_PINNED, cluster=CL2),
        _pin_ev(STORY_UNPINNED, cluster=CL1),
        _pin_ev(STORY_PINNED, cluster=CL1),   # re-pin → top
    ]
    result = build_user_pins(events, tenant_id=T1, user_id=U1)
    assert result == [CL1, CL2]


def test_pins_most_recently_pinned_first():
    """Pins are returned most-recent-first (reverse chronological)."""
    events = [
        _pin_ev(STORY_PINNED, cluster=CL1),
        _pin_ev(STORY_PINNED, cluster=CL2),
    ]
    result = build_user_pins(events, tenant_id=T1, user_id=U1)
    # CL2 was pinned last → should appear first
    assert result == [CL2, CL1]


def test_pinning_same_cluster_twice_no_duplicate():
    """Pinning the same cluster twice should not produce a duplicate."""
    events = [
        _pin_ev(STORY_PINNED, cluster=CL1),
        _pin_ev(STORY_PINNED, cluster=CL1),
    ]
    result = build_user_pins(events, tenant_id=T1, user_id=U1)
    assert result == [CL1]


# ---------------------------------------------------------------------------
# Tenant isolation — pins
# ---------------------------------------------------------------------------


def test_pins_tenant_isolation():
    """Pins from tenant B must not appear in tenant A's view."""
    events = [
        _pin_ev(STORY_PINNED, cluster=CL1, tenant=T1),
        _pin_ev(STORY_PINNED, cluster=CL2, tenant=T2),
    ]
    result = build_user_pins(events, tenant_id=T1, user_id=U1)
    assert result == [CL1]


def test_cross_tenant_unpin_does_not_remove_pin():
    """An unpin event from tenant B must not remove tenant A's pin."""
    events = [
        _pin_ev(STORY_PINNED, cluster=CL1, tenant=T1),
        _pin_ev(STORY_UNPINNED, cluster=CL1, tenant=T2),
    ]
    result = build_user_pins(events, tenant_id=T1, user_id=U1)
    assert result == [CL1]


# ---------------------------------------------------------------------------
# User isolation — pins within the same tenant
# ---------------------------------------------------------------------------


def test_pins_user_isolation():
    """User B's pins must not appear in user A's view within the same tenant."""
    events = [
        _pin_ev(STORY_PINNED, cluster=CL1, user=U1, tenant=T1),
        _pin_ev(STORY_PINNED, cluster=CL2, user=U2, tenant=T1),
    ]
    result_a = build_user_pins(events, tenant_id=T1, user_id=U1)
    result_b = build_user_pins(events, tenant_id=T1, user_id=U2)
    assert result_a == [CL1]
    assert result_b == [CL2]


def test_cross_user_unpin_does_not_remove_pin():
    """User B's unpin must not remove user A's pin."""
    events = [
        _pin_ev(STORY_PINNED, cluster=CL1, user=U1, tenant=T1),
        _pin_ev(STORY_UNPINNED, cluster=CL1, user=U2, tenant=T1),
    ]
    result = build_user_pins(events, tenant_id=T1, user_id=U1)
    assert result == [CL1]


# ---------------------------------------------------------------------------
# Mixed social event stream (realistic query result)
# ---------------------------------------------------------------------------


def test_mixed_stream_builds_correct_views():
    """All four event types together produce consistent comment + pin views."""
    events: list[dict[str, Any]] = [
        # Tenant A: Alice pins CL1
        _pin_ev(STORY_PINNED, cluster=CL1, user=U1, tenant=T1),
        # Tenant A: Bob adds a comment on CL1
        _comment_ev(COMMENT_ADDED, body="Bob's take", comment_id="b1", user=U2, cluster=CL1, tenant=T1),
        # Tenant A: Alice adds a comment on CL1
        _comment_ev(COMMENT_ADDED, body="Alice's take", comment_id="a1", user=U1, cluster=CL1, tenant=T1),
        # Tenant A: Bob pins CL2
        _pin_ev(STORY_PINNED, cluster=CL2, user=U2, tenant=T1),
        # Tenant B: Eve pins CL1 (should not bleed into T1)
        _pin_ev(STORY_PINNED, cluster=CL1, user="user-eve", tenant=T2),
        # Tenant A: Bob deletes his comment
        _comment_ev(COMMENT_DELETED, comment_id="b1", user=U2, cluster=CL1, tenant=T1),
    ]

    comments = build_comments(events, tenant_id=T1, cluster_id=CL1)
    assert len(comments) == 1
    assert comments[0]["comment_id"] == "a1"
    assert comments[0]["body"] == "Alice's take"

    alice_pins = build_user_pins(events, tenant_id=T1, user_id=U1)
    assert alice_pins == [CL1]

    bob_pins = build_user_pins(events, tenant_id=T1, user_id=U2)
    assert bob_pins == [CL2]

    # Tenant B view: Eve's pin is isolated
    eve_pins = build_user_pins(events, tenant_id=T2, user_id="user-eve")
    assert eve_pins == [CL1]
    # And tenant B has no comments on CL1
    t2_comments = build_comments(events, tenant_id=T2, cluster_id=CL1)
    assert t2_comments == []
