"""Tests for the projection-harvester pure function (scripts/harvest.py).

No DB, no NATS — covers the `harvest()` transform only, per the project convention
(pure core tested without IO; async main() wires it to infrastructure).
"""

from __future__ import annotations

from datetime import datetime, timezone

from maat.learning.harvest import _snapshot_id, harvest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_AT = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

_CLUSTER_ROW = {
    "id": "clust-abc123",
    "tenant_id": "cauri",
    "fact": "Central bank raised rates by 50bp",
    "independent_originators": 3,
    "has_primary": True,
    "extremity": "notable",
    "confidence": 0.72,
}

_CLUSTER_ROW_MINIMAL = {
    "id": "clust-xyz999",
    "fact": "A rumour that never grew",
    "independent_originators": 1,
    "has_primary": False,
    # extremity and confidence intentionally absent — defaults kick in
}


# ---------------------------------------------------------------------------
# harvest() — basic shape
# ---------------------------------------------------------------------------


def test_harvest_returns_one_event_per_cluster():
    rows = [_CLUSTER_ROW, _CLUSTER_ROW_MINIMAL]
    evs = harvest(rows, at=_AT)
    assert len(evs) == 2


def test_harvest_empty_input():
    assert harvest([], at=_AT) == []


def test_event_type_is_cluster_snapshot():
    evs = harvest([_CLUSTER_ROW], at=_AT)
    assert evs[0]["type"] == "cluster.snapshot"


def test_event_data_carries_all_required_fields():
    evs = harvest([_CLUSTER_ROW], at=_AT)
    data = evs[0]["data"]
    assert data["cluster_id"] == "clust-abc123"
    assert data["fact"] == "Central bank raised rates by 50bp"
    assert data["independent_originators"] == 3
    assert data["has_primary"] is True
    assert data["extremity"] == "notable"
    assert data["confidence"] == 0.72
    assert data["harvested_at"] == _AT.isoformat()


def test_event_data_defaults_for_optional_columns():
    """Rows without extremity/confidence columns use sensible defaults."""
    evs = harvest([_CLUSTER_ROW_MINIMAL], at=_AT)
    data = evs[0]["data"]
    assert data["extremity"] == "notable"
    assert data["confidence"] == 0.0


def test_tenant_id_propagated():
    evs = harvest([_CLUSTER_ROW], at=_AT)
    assert evs[0]["tenant_id"] == "cauri"


def test_tenant_id_defaults_to_cauri_when_absent():
    evs = harvest([_CLUSTER_ROW_MINIMAL], at=_AT)
    assert evs[0]["tenant_id"] == "cauri"


# ---------------------------------------------------------------------------
# Idempotency — same (cluster_id, harvest_date) → same stream_id
# ---------------------------------------------------------------------------


def test_stream_id_stable_for_same_cluster_and_date():
    at1 = datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)
    at2 = datetime(2026, 6, 15, 23, 59, tzinfo=timezone.utc)
    ev1 = harvest([_CLUSTER_ROW], at=at1)[0]
    ev2 = harvest([_CLUSTER_ROW], at=at2)[0]
    assert ev1["stream_id"] == ev2["stream_id"], (
        "same cluster_id + calendar day should yield the same stream_id (dedup guard)"
    )


def test_stream_id_differs_across_days():
    at_monday = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    at_tuesday = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
    ev1 = harvest([_CLUSTER_ROW], at=at_monday)[0]
    ev2 = harvest([_CLUSTER_ROW], at=at_tuesday)[0]
    assert ev1["stream_id"] != ev2["stream_id"], (
        "snapshots on different days must have distinct stream_ids"
    )


def test_stream_id_differs_across_clusters():
    row_b = {**_CLUSTER_ROW, "id": "clust-other"}
    ev_a = harvest([_CLUSTER_ROW], at=_AT)[0]
    ev_b = harvest([row_b], at=_AT)[0]
    assert ev_a["stream_id"] != ev_b["stream_id"]


def test_stream_id_prefix():
    sid = _snapshot_id("clust-abc123", "2026-06-15")
    assert sid.startswith("snap-")


def test_stream_id_compact():
    """stream_id should be short enough for event log stream keys."""
    sid = _snapshot_id("clust-abc123", "2026-06-15")
    assert len(sid) < 40  # "snap-" + 18 hex chars = 23


# ---------------------------------------------------------------------------
# Multiple clusters — each gets its own independent stream_id
# ---------------------------------------------------------------------------


def test_all_stream_ids_unique_within_one_run():
    rows = [
        {**_CLUSTER_ROW, "id": f"clust-{i}"} for i in range(10)
    ]
    evs = harvest(rows, at=_AT)
    stream_ids = [ev["stream_id"] for ev in evs]
    assert len(set(stream_ids)) == len(stream_ids), "duplicate stream_ids in a single harvest run"


# ---------------------------------------------------------------------------
# Data types — defensively cast asyncpg numeric types
# ---------------------------------------------------------------------------


def test_independent_originators_is_int():
    row = {**_CLUSTER_ROW, "independent_originators": "3"}  # string from some DB drivers
    evs = harvest([row], at=_AT)
    assert isinstance(evs[0]["data"]["independent_originators"], int)


def test_has_primary_is_bool():
    row = {**_CLUSTER_ROW, "has_primary": 1}  # int from some DB drivers
    evs = harvest([row], at=_AT)
    assert evs[0]["data"]["has_primary"] is True


def test_confidence_is_float():
    row = {**_CLUSTER_ROW, "confidence": "0.72"}  # string from some DB drivers
    evs = harvest([row], at=_AT)
    assert isinstance(evs[0]["data"]["confidence"], float)
