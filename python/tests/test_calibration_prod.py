"""Tests for calibration_prod.production_calibration (P7, Issue #60).

All tests are pure — no DB, no I/O.  The status builder is exercised over three scenarios:

  healthy      — a realistic mix of confirmed and unconfirmed facts;
  under-confident — reads consistently low vs confirmed outcomes;
  no-data      — empty history (the honest answer before the clock accrues).

Extra coverage:
  - refutation_bias flag (all resolved = confirmed, none refuted);
  - freshness timestamp parsing (ISO string, epoch float, missing);
  - format_status output smoke-test (no exceptions, key strings present).
"""

from __future__ import annotations

import datetime

import pytest

from maat.learning.calibration_prod import (
    CalibrationStatus,
    _parse_ts,
    format_status,
    production_calibration,
)


# ---------------------------------------------------------------------------
# Fixtures — event payloads in cluster.corroborated shape
# ---------------------------------------------------------------------------

_NOW = datetime.datetime(2026, 6, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)

# A "well-behaved" event: high corroboration, resolves confirmed quickly.
_CONFIRMED_FACT = {
    "fact": "Minister X resigned",
    "independent_originators": 4,
    "has_primary": False,
    "extremity": "notable",
    # two events (first read, then confirmation) share the same normalised fact key
}

_CONFIRMED_FACT_INITIAL = {
    "fact": "Minister X resigned",
    "independent_originators": 1,
    "has_primary": False,
    "extremity": "notable",
    "occurred_at": "2026-06-15T10:00:00Z",
}

_CONFIRMED_FACT_LATER = {
    "fact": "Minister X resigned",
    "independent_originators": 4,
    "has_primary": False,
    "extremity": "notable",
    "occurred_at": "2026-06-15T11:00:00Z",
}

_UNCONFIRMED_FACT = {
    "fact": "secret treaty signed",
    "independent_originators": 1,
    "has_primary": False,
    "extremity": "extraordinary",
    "occurred_at": "2026-06-15T09:00:00Z",
}

_REFUTED_FACT_INITIAL = {
    "fact": "rates raised by 50bp",
    "independent_originators": 2,
    "has_primary": False,
    "extremity": "ordinary",
    "occurred_at": "2026-06-15T08:00:00Z",
}

_REFUTED_FACT_LATER = {
    "fact": "rates raised by 50bp",
    "independent_originators": 2,
    "has_primary": False,
    "extremity": "ordinary",
    "corrected": True,
    "occurred_at": "2026-06-15T09:30:00Z",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _status(events, *, now=_NOW) -> CalibrationStatus:
    return production_calibration(events, now=now)


# ---------------------------------------------------------------------------
# Empty / no-data scenario
# ---------------------------------------------------------------------------


def test_no_data_returns_none_brier():
    s = _status([])
    assert s.brier is None
    assert s.n_observations == 0
    assert s.n_scored == 0
    assert s.bins == []
    assert s.proposals == []
    assert not s.refutation_bias
    assert s.freshness_seconds is None
    assert s.as_of == _NOW


def test_no_data_format_mentions_no_resolution():
    s = _status([])
    out = format_status(s)
    assert "Nothing has resolved" in out


# ---------------------------------------------------------------------------
# Healthy scenario — mix of outcomes
# ---------------------------------------------------------------------------


def test_healthy_brier_is_float():
    """With confirmed + unconfirmed facts, Brier is a non-None float."""
    events = [_CONFIRMED_FACT_INITIAL, _CONFIRMED_FACT_LATER, _UNCONFIRMED_FACT]
    s = _status(events)
    # the confirmed fact reaches >=3 independent originators -> confirmed
    # the unconfirmed fact stays at 1 originator -> unconfirmed (not terminal)
    assert s.brier is not None
    assert isinstance(s.brier, float)
    assert 0.0 <= s.brier <= 1.0


def test_healthy_n_counts():
    events = [_CONFIRMED_FACT_INITIAL, _CONFIRMED_FACT_LATER, _UNCONFIRMED_FACT]
    s = _status(events)
    assert s.n_observations == 2  # two distinct facts
    # only the confirmed fact is terminal (unconfirmed is still in-flight)
    assert s.n_scored == 1


def test_healthy_bins_non_empty():
    events = [_CONFIRMED_FACT_INITIAL, _CONFIRMED_FACT_LATER, _UNCONFIRMED_FACT]
    s = _status(events)
    # at least one bin should be populated since a fact confirmed
    assert len(s.bins) >= 1
    for b in s.bins:
        assert b.n >= 1
        assert 0.0 <= b.predicted <= 1.0
        assert 0.0 <= b.actual <= 1.0


def test_healthy_no_refutation_bias_when_mix_present():
    """refutation_bias is False when refuted facts are in the history."""
    events = [
        _REFUTED_FACT_INITIAL,
        _REFUTED_FACT_LATER,
        _CONFIRMED_FACT_INITIAL,
        _CONFIRMED_FACT_LATER,
    ]
    s = _status(events)
    assert not s.refutation_bias


def test_refutation_bias_set_when_all_confirmed():
    """All resolved facts confirmed and none refuted -> refutation_bias True."""
    events = [_CONFIRMED_FACT_INITIAL, _CONFIRMED_FACT_LATER]
    s = _status(events)
    assert s.refutation_bias


def test_refutation_bias_cleared_when_refuted_present():
    events = [
        _CONFIRMED_FACT_INITIAL,
        _CONFIRMED_FACT_LATER,
        _REFUTED_FACT_INITIAL,
        _REFUTED_FACT_LATER,
    ]
    s = _status(events)
    assert not s.refutation_bias


# ---------------------------------------------------------------------------
# Under-confident scenario — proposals should surface
# ---------------------------------------------------------------------------


def _make_confirmed(fact: str, initial_n: int, later_n: int, extremity: str = "notable"):
    """Two events for one fact that resolves confirmed (later_n >= 3)."""
    return [
        {
            "fact": fact,
            "independent_originators": initial_n,
            "has_primary": False,
            "extremity": extremity,
        },
        {
            "fact": fact,
            "independent_originators": later_n,
            "has_primary": False,
            "extremity": extremity,
        },
    ]


def test_under_confident_proposals_may_surface():
    """When the default weights consistently under-predict confirmed facts, tune_proposals
    may return decay adjustments.  We don't hard-code which level; just verify the shape."""
    events = []
    # ten facts each starting at n=1 (low read), all confirm at n=4 (well above bar)
    for i in range(10):
        events.extend(_make_confirmed(f"fact-{i}", initial_n=1, later_n=4, extremity="notable"))

    s = _status(events)
    assert s.n_scored == 10
    # proposals is a list of dicts (may be empty if default weights already fit)
    assert isinstance(s.proposals, list)
    for p in s.proposals:
        assert "key" in p
        assert "value" in p
        assert "reason" in p
        assert p["key"].startswith("decay.")


# ---------------------------------------------------------------------------
# Proposals are suppressed when no facts have resolved
# ---------------------------------------------------------------------------


def test_no_proposals_when_nothing_scored():
    # Two in-flight facts that haven't hit the confirm bar
    events = [
        {"fact": "thin claim A", "independent_originators": 1, "has_primary": False, "extremity": "notable"},
        {"fact": "thin claim B", "independent_originators": 1, "has_primary": False, "extremity": "notable"},
    ]
    s = _status(events)
    assert s.n_scored == 0
    assert s.proposals == []


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def test_freshness_from_occurred_at():
    event_time = "2026-06-15T11:00:00Z"  # 1 h before _NOW
    events = [
        {**_CONFIRMED_FACT_INITIAL, "occurred_at": event_time},
        {**_CONFIRMED_FACT_LATER, "occurred_at": event_time},
    ]
    s = _status(events)
    assert s.freshness_seconds is not None
    # latest event is at 11:00, now is 12:00 -> 3600 seconds
    assert abs(s.freshness_seconds - 3600.0) < 5


def test_freshness_from_ts_epoch_float():
    epoch = _NOW.timestamp() - 7200  # 2 h ago
    events = [
        {**_CONFIRMED_FACT_INITIAL, "occurred_at": None, "ts": epoch},
        {**_CONFIRMED_FACT_LATER, "occurred_at": None, "ts": epoch},
    ]
    s = _status(events)
    assert s.freshness_seconds is not None
    assert abs(s.freshness_seconds - 7200.0) < 5


def test_freshness_none_when_no_timestamps():
    events = [
        {k: v for k, v in _CONFIRMED_FACT_INITIAL.items() if k not in ("occurred_at", "ts")},
        {k: v for k, v in _CONFIRMED_FACT_LATER.items() if k not in ("occurred_at", "ts")},
    ]
    s = _status(events)
    # freshness is None when no parseable timestamps exist
    assert s.freshness_seconds is None


def test_freshness_none_on_empty_history():
    s = _status([])
    assert s.freshness_seconds is None


# ---------------------------------------------------------------------------
# as_of respected
# ---------------------------------------------------------------------------


def test_as_of_matches_now_arg():
    custom_now = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    s = _status([], now=custom_now)
    assert s.as_of == custom_now


# ---------------------------------------------------------------------------
# _parse_ts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected_hour", [
    ("2026-06-15T10:00:00Z", 10),
    ("2026-06-15T10:00:00+00:00", 10),
    ("2026-06-15T10:00:00", 10),  # naive -> assumed UTC
])
def test_parse_ts_iso_strings(raw, expected_hour):
    dt = _parse_ts(raw)
    assert dt is not None
    assert dt.hour == expected_hour


def test_parse_ts_epoch_float():
    epoch = datetime.datetime(2026, 6, 15, 10, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    dt = _parse_ts(epoch)
    assert dt is not None
    assert dt.hour == 10


def test_parse_ts_none():
    assert _parse_ts(None) is None
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None


# ---------------------------------------------------------------------------
# format_status smoke test
# ---------------------------------------------------------------------------


def test_format_status_healthy_no_exception():
    events = [_CONFIRMED_FACT_INITIAL, _CONFIRMED_FACT_LATER]
    s = _status(events)
    out = format_status(s)
    assert "Brier score" in out
    assert "reliability bins" in out
    assert "as_of" in out


def test_format_status_refutation_bias_caveat():
    events = [_CONFIRMED_FACT_INITIAL, _CONFIRMED_FACT_LATER]
    s = _status(events)
    out = format_status(s)
    assert "CAVEAT" in out


def test_format_status_no_data_no_exception():
    s = _status([])
    out = format_status(s)
    assert "Nothing has resolved" in out


def test_format_status_with_refuted_no_caveat():
    events = [
        _CONFIRMED_FACT_INITIAL,
        _CONFIRMED_FACT_LATER,
        _REFUTED_FACT_INITIAL,
        _REFUTED_FACT_LATER,
    ]
    s = _status(events)
    out = format_status(s)
    assert "CAVEAT" not in out
