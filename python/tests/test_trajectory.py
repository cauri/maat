"""Integration test: the harvester → cluster_snapshots → learning-fold loop (#39, closing it).

End-to-end at the data-contract level (no DB/NATS, per the convention that the pure core is
tested without IO):
  - the harvester payload carries everything the folds need;
  - a `cluster_snapshots` row maps back to that same shape (`load_trajectory._snapshot_to_dict`);
  - two snapshots of a fact whose corroboration GREW form a 2-point trajectory that
    `observations_from_history` resolves and `fold_reputation` folds;
  - the loader prefers the snapshot projection and falls back to `cluster.corroborated`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from maat.learning.calibration import observations_from_history
from maat.learning.harvest import harvest
from maat.learning.reputation import fold_reputation
from maat.learning.trajectory import _snapshot_to_dict, load_trajectory

_DAY1 = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
_DAY2 = _DAY1 + timedelta(days=1)


def _tick(at, *, independent, sources, originators, corrected=False):
    """One harvester tick over a single cluster; return the event `data` the kernel logs/folds."""
    rows = [{
        "id": "clust-1",  # same cluster across ticks
        "fact": "Central bank raised rates by 50bp",
        "independent_originators": independent,
        "has_primary": False,
        "extremity": "notable",
        "confidence": 0.3,
        "sources": sources,
        "originators": originators,
        "corrected": corrected,
    }]
    return harvest(rows, at=at)[0]["data"]


def _grown():
    """Two ticks for the SAME fact, corroboration growing 1 → 3 independent originators."""
    return [
        _tick(_DAY1, independent=1, sources=["alpha"], originators=[["a1"]]),
        _tick(_DAY2, independent=3, sources=["alpha", "beta", "gamma"],
              originators=[["a1"], ["a2"], ["a3"]]),
    ]


# --- the trajectory the folds see -------------------------------------------------------


def test_two_snapshots_form_a_two_point_trajectory_calibration_sees():
    obs = observations_from_history(_grown())
    assert len(obs) == 1  # one fact, regardless of tick count
    o = obs[0]
    assert o.independent_originators == 1  # INITIAL read = the first snapshot
    assert o.outcome == "confirmed"        # resolved from the last (grew 1 → 3 ≥ confirm_at)


def test_two_snapshots_form_a_two_point_trajectory_reputation_sees():
    reps = {r.source: r for r in fold_reputation(_grown())}
    assert {"alpha", "beta", "gamma"} <= set(reps)
    assert reps["beta"].facts_confirmed == 1
    assert reps["beta"].facts_refuted == 0


def test_single_snapshot_is_not_yet_resolved():
    # One point is not a trajectory — the over-time growth is what carries the signal.
    assert observations_from_history(_grown()[:1])[0].outcome != "confirmed"


def test_operator_refutation_flows_through_the_snapshot():
    # The enriched snapshot carries `corrected` (any member claim corrected / laundering-flagged);
    # a flagged fact resolves REFUTED — wiring the existing operator/reader refutation into the
    # trajectory, which the legacy cluster.corroborated payload never carried.
    traj = _grown()
    traj[-1] = {**traj[-1], "corrected": True}
    assert observations_from_history(traj)[0].outcome == "refuted"
    assert fold_reputation(traj)[0].facts_refuted == 1


# --- the loader: cluster_snapshots projection, corroborated fallback --------------------


class _FakePool:
    """Minimal asyncpg-pool stub: routes the snapshot query to table rows, else to event rows."""

    def __init__(self, snapshot_rows, event_rows):
        self._snapshots = snapshot_rows
        self._events = event_rows

    async def fetch(self, query, *args):
        return self._snapshots if "cluster_snapshots" in query else self._events


def _snap_row(day, *, fact, independent, sources, originators, corrected=False):
    return {
        "fact": fact, "independent_originators": independent, "has_primary": False,
        "extremity": "notable", "confidence": 0.5, "sources": sources,
        "originators": originators, "corrected": corrected, "grounding": None, "cluster_id": "clust-1",
        "harvested_at": datetime(2026, 6, day, 12, 0, tzinfo=timezone.utc),
    }


def test_snapshot_row_maps_to_fold_shape():
    d = _snapshot_to_dict(_snap_row(15, fact="F", independent=2,
                                    sources=["alpha", "beta"], originators=[["a1"], ["a2"]]))
    assert d["sources"] == ["alpha", "beta"]
    assert d["originators"] == [["a1"], ["a2"]]
    assert d["ts"] == "2026-06-15T12:00:00+00:00"  # from harvested_at, for accuracy/freshness


def test_snapshot_row_parses_jsonb_text():
    # asyncpg can hand jsonb back as a string — the loader must parse it to a list.
    d = _snapshot_to_dict(_snap_row(15, fact="F", independent=1,
                                    sources='["alpha"]', originators='[["a1"]]'))
    assert d["sources"] == ["alpha"]
    assert d["originators"] == [["a1"]]


def test_loader_prefers_snapshot_projection():
    pool = _FakePool(
        snapshot_rows=[
            _snap_row(15, fact="F", independent=1, sources=["alpha"], originators=[["a1"]]),
            _snap_row(16, fact="F", independent=3, sources=["alpha", "beta", "gamma"],
                      originators=[["a1"], ["a2"], ["a3"]]),
        ],
        event_rows=[{"data": {"fact": "SHOULD-NOT-BE-READ", "independent_originators": 9}}],
    )
    history = asyncio.run(load_trajectory(pool))
    assert [h["fact"] for h in history] == ["F", "F"]  # snapshots, not the fallback
    assert observations_from_history(history)[0].outcome == "confirmed"


def test_loader_falls_back_to_corroborated_when_no_snapshots():
    pool = _FakePool(
        snapshot_rows=[],
        event_rows=[{"data": {"fact": "legacy", "independent_originators": 1}}],
    )
    history = asyncio.run(load_trajectory(pool))
    assert history and history[0]["fact"] == "legacy"
