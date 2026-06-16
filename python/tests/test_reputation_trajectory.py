"""#192 — reputation_score (0..1 outcome-anchored standing) + reputation_trajectories (sparkline).

These replace the /api/sources last-N-confidences proxy with a real fold of the §6 truth-over-time
signal. Pure: no DB, no I/O.
"""

from maat.learning.reputation import (
    fold_reputation,
    reputation_by_source,
    reputation_score,
    reputation_trajectories,
)


def _corr_ev(fact, sources, originators, *, has_primary=False, extremity="notable", confidence=0.5):
    return {
        "fact": fact, "sources": sources, "originators": originators,
        "independent_originators": len(originators), "has_primary": has_primary,
        "extremity": extremity, "confidence": confidence,
    }


# A fact that strengthens over time → CONFIRMED, lifting its independent originators.
_CONFIRMED = [
    _corr_ev("Minister X resigned", ["reuters", "bbc"], [["a1"], ["a2"]], confidence=0.45),
    _corr_ev("Minister X resigned", ["reuters", "bbc", "afp", "dpa"],
             [["a1"], ["a2"], ["a3"], ["a4"]], confidence=0.9),
]


def test_score_in_unit_interval_and_outcome_anchored():
    reps = reputation_by_source(fold_reputation(_CONFIRMED))
    s = reputation_score(reps["reuters"])
    assert 0.0 <= s <= 1.0
    # reuters rode a confirmed fact as an independent originator → above the provisional 0.5 band.
    assert s > 0.5


def test_score_cold_source_sits_in_provisional_band():
    # A lone tabloid on an extraordinary claim: no terminal outcome resolves → confirmation_rate None.
    reps = reputation_by_source(fold_reputation(
        [_corr_ev("Aliens landed", ["tabloid"], [["b1"]], extremity="extraordinary")]
    ))
    rec = reps["tabloid"]
    assert rec.confirmation_rate is None
    assert 0.0 <= reputation_score(rec) <= 0.5  # provisional band, not a verdict


def test_trajectory_is_sparkline_per_source():
    traj = reputation_trajectories(_CONFIRMED, buckets=8)
    assert "reuters" in traj
    # Distinct expanding prefixes here are [:1] and [:2] → 2 sample points.
    assert len(traj["reuters"]) == 2
    assert all(0.0 <= v <= 1.0 for v in traj["reuters"])
    # The confirmation lands on the second tick, so the standing rises across the window.
    assert traj["reuters"][-1] >= traj["reuters"][0]


def test_trajectory_empty_history():
    assert reputation_trajectories([]) == {}


def test_trajectory_late_source_has_shorter_series():
    history = [
        _corr_ev("A", ["reuters"], [["a1"]]),
        _corr_ev("A", ["reuters"], [["a1"]]),
        _corr_ev("B", ["reuters", "newcomer"], [["c1"], ["c2"]]),
    ]
    traj = reputation_trajectories(history, buckets=3)
    # newcomer only appears in the final bucket, so its sparkline is shorter than reuters'.
    assert len(traj["newcomer"]) < len(traj["reuters"])
