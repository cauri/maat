"""Accuracy-axis lifecycle tests (P3, §5) — `maat.learning.accuracy`.

All state transitions are covered, including the time-window decay.  No DB, no I/O, no wall-
clock: timestamps are passed in as fixed epoch floats so the tests are deterministic.
"""

from __future__ import annotations

from maat.learning.accuracy import (
    LifecycleState,
    classify_lifecycle,
    lifecycle_by_fact,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = 1_000_000.0  # reference epoch for all time-sensitive tests
ONE_HOUR = 3_600.0
ONE_DAY = 86_400.0


def ev(
    independent_originators: int = 1,
    has_primary: bool = False,
    corrected: bool = False,
    ts: float | None = None,
    extremity: str = "notable",
    **extra,
) -> dict:
    """Build a minimal cluster.corroborated event dict."""
    d = {
        "independent_originators": independent_originators,
        "has_primary": has_primary,
        "corrected": corrected,
        "extremity": extremity,
    }
    if ts is not None:
        d["ts"] = ts
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# LifecycleState enum
# ---------------------------------------------------------------------------


def test_lifecycle_state_values_are_strings():
    # String subclass — safe to serialise in JSON event payloads
    for state in LifecycleState:
        assert isinstance(state.value, str)


def test_lifecycle_state_all_five_values():
    names = {s.value for s in LifecycleState}
    assert names == {"dormant", "resolving", "resolved", "extended", "decayed"}


# ---------------------------------------------------------------------------
# classify_lifecycle — dormant
# ---------------------------------------------------------------------------


def test_empty_trajectory_is_dormant():
    assert classify_lifecycle([], NOW) == LifecycleState.DORMANT


def test_single_event_no_growth_is_dormant():
    # first seen: 1 independent originator, no primary, no correction
    assert classify_lifecycle([ev(1, False)], NOW) == LifecycleState.DORMANT


def test_multiple_events_no_growth_is_dormant():
    # event repeated twice but independent_originators never grows
    traj = [ev(1, ts=NOW - ONE_HOUR), ev(1, ts=NOW)]
    assert classify_lifecycle(traj, NOW) == LifecycleState.DORMANT


def test_dormant_not_stale_when_recent():
    # only 1 originator but seen within the stale window → still dormant, not decayed
    traj = [ev(1, ts=NOW - ONE_HOUR)]
    assert classify_lifecycle(traj, NOW, stale_after=ONE_DAY) == LifecycleState.DORMANT


# ---------------------------------------------------------------------------
# classify_lifecycle — resolving
# ---------------------------------------------------------------------------


def test_growing_corroboration_is_resolving():
    traj = [ev(1, ts=NOW - ONE_DAY), ev(2, ts=NOW)]
    assert classify_lifecycle(traj, NOW) == LifecycleState.RESOLVING


def test_resolving_below_confirm_threshold():
    # 1 → 2 originators: growing but still below confirm_at=3
    traj = [ev(1), ev(2)]
    assert classify_lifecycle(traj, NOW, confirm_at=3) == LifecycleState.RESOLVING


def test_resolving_recent_activity_not_decayed():
    # evidence is growing and the last event is recent → resolving, not decayed
    traj = [ev(1, ts=NOW - 2 * ONE_HOUR), ev(2, ts=NOW - ONE_HOUR)]
    result = classify_lifecycle(traj, NOW, stale_after=ONE_DAY)
    assert result == LifecycleState.RESOLVING


def test_growing_fact_last_seen_long_ago_is_decayed():
    # gained originators at some point but the last event is older than stale_after → decayed
    # ("no new corroboration within a window" — the growth stopped, fact went quiet)
    traj = [ev(1, ts=NOW - 2 * ONE_DAY), ev(2, ts=NOW - ONE_DAY - ONE_HOUR)]
    assert classify_lifecycle(traj, NOW, stale_after=ONE_DAY) == LifecycleState.DECAYED


# ---------------------------------------------------------------------------
# classify_lifecycle — resolved
# ---------------------------------------------------------------------------


def test_confirmed_by_three_originators_is_resolved():
    # 1 → 3 in a single trajectory snapshot: resolved (confirmed)
    traj = [ev(3, False)]
    assert classify_lifecycle(traj, NOW, confirm_at=3) == LifecycleState.RESOLVED


def test_confirmed_by_primary_source_is_resolved():
    traj = [ev(1, has_primary=True)]
    assert classify_lifecycle(traj, NOW) == LifecycleState.RESOLVED


def test_refuted_by_correction_is_resolved():
    traj = [ev(2, corrected=True)]
    assert classify_lifecycle(traj, NOW) == LifecycleState.RESOLVED


def test_resolved_single_event_when_already_at_threshold():
    # Only one event but it already clears confirm_at — no "extended" yet
    traj = [ev(5, False)]
    assert classify_lifecycle(traj, NOW, confirm_at=3) == LifecycleState.RESOLVED


# ---------------------------------------------------------------------------
# classify_lifecycle — extended
# ---------------------------------------------------------------------------


def test_extended_after_resolution_keeps_accruing():
    # First event: 1 orig (below threshold).  Last event: 4 orig (above) — it resolved AND kept
    # going.  classify sees > 1 event and latest_ind > initial_ind → extended.
    traj = [ev(1, ts=NOW - 2 * ONE_HOUR), ev(2, ts=NOW - ONE_HOUR), ev(4, ts=NOW)]
    assert classify_lifecycle(traj, NOW, confirm_at=3) == LifecycleState.EXTENDED


def test_extended_after_primary_surfaces_with_more_events():
    # Started without primary; a second event brings primary source → resolved, and because there
    # are 2 events + has_primary changed → extended.
    traj = [
        ev(1, has_primary=False, ts=NOW - ONE_HOUR),
        ev(1, has_primary=True, ts=NOW),
    ]
    assert classify_lifecycle(traj, NOW) == LifecycleState.EXTENDED


def test_extended_after_correction_with_multiple_events():
    # Fact gets corrected across two events; second event adds corroboration → extended.
    traj = [
        ev(2, corrected=False, ts=NOW - ONE_HOUR),
        ev(3, corrected=True, ts=NOW),
    ]
    # outcome = refuted (corrected); latest_ind (3) > initial_ind (2) → extended
    assert classify_lifecycle(traj, NOW) == LifecycleState.EXTENDED


# ---------------------------------------------------------------------------
# classify_lifecycle — decayed
# ---------------------------------------------------------------------------


def test_stalled_fact_decays_after_window():
    # Never grew; last event is older than stale_after → decayed
    traj = [ev(1, ts=NOW - ONE_DAY - ONE_HOUR)]
    assert classify_lifecycle(traj, NOW, stale_after=ONE_DAY) == LifecycleState.DECAYED


def test_fact_within_staleness_window_is_not_decayed():
    traj = [ev(1, ts=NOW - ONE_HOUR)]
    assert classify_lifecycle(traj, NOW, stale_after=ONE_DAY) != LifecycleState.DECAYED


def test_zero_timestamp_never_decays():
    # ts=0.0 (missing/unknown) → _ts returns 0 → stale check skipped → not decayed
    traj = [ev(1)]  # no ts key
    result = classify_lifecycle(traj, NOW, stale_after=1.0)  # tiny window
    assert result == LifecycleState.DORMANT  # stale check skipped; no growth → dormant


def test_stalled_no_growth_decays_after_window():
    # No growth ever; last event is older than stale_after → decayed
    traj = [ev(1, ts=NOW - ONE_DAY - ONE_HOUR), ev(1, ts=NOW - ONE_DAY - 30)]
    assert classify_lifecycle(traj, NOW, stale_after=ONE_DAY) == LifecycleState.DECAYED


def test_growing_then_silent_also_decays():
    # Gained an originator but then went quiet past stale_after → decayed (growth stopped)
    traj = [ev(1, ts=NOW - 3 * ONE_DAY), ev(2, ts=NOW - 2 * ONE_DAY)]
    assert classify_lifecycle(traj, NOW, stale_after=ONE_DAY) == LifecycleState.DECAYED


def test_decay_with_custom_stale_after():
    traj = [ev(1, ts=NOW - 7_200.0)]  # 2 hours ago
    assert classify_lifecycle(traj, NOW, stale_after=3_600.0) == LifecycleState.DECAYED  # 1h window
    assert classify_lifecycle(traj, NOW, stale_after=ONE_DAY) == LifecycleState.DORMANT  # 24h window


# ---------------------------------------------------------------------------
# classify_lifecycle — now as datetime
# ---------------------------------------------------------------------------


def test_accepts_datetime_now():
    from datetime import datetime, timezone

    dt_now = datetime.fromtimestamp(NOW, tz=timezone.utc)
    traj = [ev(1, ts=NOW - 2 * ONE_DAY)]
    result_float = classify_lifecycle(traj, NOW, stale_after=ONE_DAY)
    result_dt = classify_lifecycle(traj, dt_now, stale_after=ONE_DAY)
    assert result_float == result_dt == LifecycleState.DECAYED


# ---------------------------------------------------------------------------
# classify_lifecycle — ISO-8601 timestamp keys
# ---------------------------------------------------------------------------


def test_iso_timestamp_string_parsed():
    # ts provided as ISO-8601 string — should parse to the same epoch
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(NOW - 2 * ONE_DAY, tz=timezone.utc)
    iso = dt.isoformat()
    traj = [{"independent_originators": 1, "has_primary": False, "ts": iso}]
    assert classify_lifecycle(traj, NOW, stale_after=ONE_DAY) == LifecycleState.DECAYED


def test_iso_timestamp_z_suffix():
    # "Z" suffix is handled (replace Z → +00:00)
    # Use an epoch past year 2000 so the ISO timestamp is clearly in the past
    year_2001_epoch = 978_307_200.0  # 2001-01-01T00:00:00Z
    future_now = year_2001_epoch + 2 * ONE_DAY   # "now" is 2 days after the event
    traj = [{"independent_originators": 1, "has_primary": False, "ts": "2000-12-31T00:00:00Z"}]
    # event ts ≈ year_2001_epoch - 1 day; future_now - event_ts > ONE_DAY → decayed
    assert classify_lifecycle(traj, future_now, stale_after=ONE_DAY) == LifecycleState.DECAYED


def test_timestamp_key_fallback_epoch():
    # "epoch" key accepted
    traj = [{"independent_originators": 1, "has_primary": False, "epoch": NOW - 2 * ONE_DAY}]
    assert classify_lifecycle(traj, NOW, stale_after=ONE_DAY) == LifecycleState.DECAYED


# ---------------------------------------------------------------------------
# classify_lifecycle — confirm_at knob
# ---------------------------------------------------------------------------


def test_custom_confirm_at():
    # With confirm_at=2: 2 originators confirms; with default (3): still resolving
    traj = [ev(1), ev(2)]
    assert classify_lifecycle(traj, NOW, confirm_at=2) in (
        LifecycleState.RESOLVED, LifecycleState.EXTENDED
    )
    assert classify_lifecycle(traj, NOW, confirm_at=3) == LifecycleState.RESOLVING


# ---------------------------------------------------------------------------
# lifecycle_by_fact — fold over event stream
# ---------------------------------------------------------------------------


def test_lifecycle_by_fact_groups_by_normalised_text():
    events = [
        {"fact": "Minister X resigned", "independent_originators": 1, "has_primary": False, "ts": NOW - ONE_HOUR},
        {"fact": "minister x  resigned", "independent_originators": 2, "has_primary": False, "ts": NOW - 30},
        {"fact": "minister x resigned", "independent_originators": 4, "has_primary": False, "ts": NOW},
        {"fact": "Volcano erupted", "independent_originators": 1, "has_primary": False, "ts": NOW - 2 * ONE_DAY},
    ]
    result = lifecycle_by_fact(events, NOW, stale_after=ONE_DAY)
    # Two distinct normalised facts
    assert len(result) == 2
    keys = list(result.keys())
    assert "minister x resigned" in keys
    assert "volcano erupted" in keys


def test_lifecycle_by_fact_confirmed_fact_is_extended():
    # minister x resigned: 1 → 4 originators across 3 events → extended (resolved + growing)
    events = [
        {"fact": "Minister X resigned", "independent_originators": 1, "has_primary": False, "ts": NOW - ONE_HOUR},
        {"fact": "minister x  resigned", "independent_originators": 2, "has_primary": False, "ts": NOW - 30},
        {"fact": "minister x resigned", "independent_originators": 4, "has_primary": False, "ts": NOW},
    ]
    result = lifecycle_by_fact(events, NOW, confirm_at=3)
    assert result["minister x resigned"] == LifecycleState.EXTENDED


def test_lifecycle_by_fact_stalled_fact_decays():
    events = [
        {"fact": "Volcano erupted", "independent_originators": 1, "has_primary": False, "ts": NOW - 2 * ONE_DAY},
    ]
    result = lifecycle_by_fact(events, NOW, stale_after=ONE_DAY)
    assert result["volcano erupted"] == LifecycleState.DECAYED


def test_lifecycle_by_fact_dormant_fact():
    events = [
        {"fact": "Rain expected tomorrow", "independent_originators": 1, "has_primary": False, "ts": NOW - ONE_HOUR},
    ]
    result = lifecycle_by_fact(events, NOW, stale_after=ONE_DAY)
    assert result["rain expected tomorrow"] == LifecycleState.DORMANT


def test_lifecycle_by_fact_resolving_fact():
    events = [
        {"fact": "Markets fell sharply", "independent_originators": 1, "has_primary": False, "ts": NOW - ONE_HOUR},
        {"fact": "Markets fell sharply", "independent_originators": 2, "has_primary": False, "ts": NOW},
    ]
    result = lifecycle_by_fact(events, NOW, confirm_at=3)
    assert result["markets fell sharply"] == LifecycleState.RESOLVING


def test_lifecycle_by_fact_empty_stream():
    result = lifecycle_by_fact([], NOW)
    assert result == {}


def test_lifecycle_by_fact_refuted_fact():
    events = [
        {"fact": "PM survived no-confidence vote", "independent_originators": 2, "has_primary": False, "corrected": True, "ts": NOW},
    ]
    result = lifecycle_by_fact(events, NOW)
    # corrected → refuted → resolved (single event, so not extended)
    assert result["pm survived no-confidence vote"] == LifecycleState.RESOLVED


def test_lifecycle_by_fact_multiple_states_in_one_stream():
    events = [
        # Fact A: confirmed + still growing → extended
        {"fact": "Fact A", "independent_originators": 1, "has_primary": False, "ts": NOW - 3 * ONE_HOUR},
        {"fact": "Fact A", "independent_originators": 4, "has_primary": False, "ts": NOW - ONE_HOUR},
        {"fact": "Fact A", "independent_originators": 5, "has_primary": False, "ts": NOW},
        # Fact B: gained 1 originator but below threshold → resolving
        {"fact": "Fact B", "independent_originators": 1, "has_primary": False, "ts": NOW - 2 * ONE_HOUR},
        {"fact": "Fact B", "independent_originators": 2, "has_primary": False, "ts": NOW},
        # Fact C: single event, old → decayed
        {"fact": "Fact C", "independent_originators": 1, "has_primary": False, "ts": NOW - 2 * ONE_DAY},
        # Fact D: just seen → dormant
        {"fact": "Fact D", "independent_originators": 1, "has_primary": False, "ts": NOW - ONE_HOUR},
        # Fact E: confirmed by primary → resolved (single event)
        {"fact": "Fact E", "independent_originators": 1, "has_primary": True, "ts": NOW},
    ]
    result = lifecycle_by_fact(events, NOW, stale_after=ONE_DAY, confirm_at=3)

    assert result["fact a"] == LifecycleState.EXTENDED
    assert result["fact b"] == LifecycleState.RESOLVING
    assert result["fact c"] == LifecycleState.DECAYED
    assert result["fact d"] == LifecycleState.DORMANT
    assert result["fact e"] == LifecycleState.RESOLVED


def test_lifecycle_by_fact_returns_lifecycle_state_instances():
    events = [{"fact": "Test", "independent_originators": 1, "has_primary": False}]
    result = lifecycle_by_fact(events, NOW)
    for v in result.values():
        assert isinstance(v, LifecycleState)
