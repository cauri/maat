"""GDELT acquisition params (#33) + historical-window backfill plumbing (#40). Pure: no network."""

from datetime import datetime, timedelta, timezone

from maat.acquire.gdelt import build_params, gdelt_stamp, week_windows


def test_build_params_defaults_to_rolling_timespan():
    p = build_params("interest rate", maxrecords=15)
    assert p["timespan"] == "3d"
    assert "startdatetime" not in p and "enddatetime" not in p
    assert p["query"] == "interest rate" and p["maxrecords"] == "15"


def test_build_params_window_replaces_timespan():
    p = build_params(
        "interest rate", startdatetime="20240101000000", enddatetime="20240108000000"
    )
    assert p["startdatetime"] == "20240101000000"
    assert p["enddatetime"] == "20240108000000"
    assert "timespan" not in p  # a full window wins over the rolling timespan


def test_build_params_partial_window_keeps_timespan():
    # Only one bound given → not a valid window; fall back to the rolling timespan.
    p = build_params("x", startdatetime="20240101000000")
    assert p["timespan"] == "3d" and "startdatetime" not in p


def test_build_params_appends_source_filters():
    p = build_params("art", sourcelang="French", sourcecountry="FR")
    assert "sourcelang:French" in p["query"] and "sourcecountry:FR" in p["query"]


def test_gdelt_stamp_formats_utc():
    assert gdelt_stamp(datetime(2024, 3, 9, 5, 6, 7, tzinfo=timezone.utc)) == "20240309050607"


def test_gdelt_stamp_converts_tz_to_utc():
    # 03:00 at +03:00 is 00:00 UTC.
    tz = timezone(timedelta(hours=3))
    assert gdelt_stamp(datetime(2024, 3, 9, 3, 0, 0, tzinfo=tz)) == "20240309000000"


def test_week_windows_walks_back_non_overlapping():
    now = datetime(2024, 3, 29, tzinfo=timezone.utc)
    wins = week_windows(now, 3)
    assert len(wins) == 3
    # Most recent first; each window is exactly 7 days; consecutive windows tile without gaps.
    assert wins[0][1] == now
    for start, end in wins:
        assert end - start == timedelta(days=7)
    assert wins[0][0] == wins[1][1]  # window 1's start == window 2's end (contiguous)


def test_week_windows_zero_is_empty():
    assert week_windows(datetime(2024, 1, 1, tzinfo=timezone.utc), 0) == []
