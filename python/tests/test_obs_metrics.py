"""Tests for maat.obs_metrics — pure pipeline-health summaries (issue #61).

Covers healthy, stalled, dead-letters-present, and empty-pipeline scenarios.
All tests inject a frozen ``as_of`` clock so timing assertions are deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from maat.obs_metrics import (
    STALE_THRESHOLD_S,
    STALLED_THRESHOLD_S,
    calibration_health,
    dead_letter_summary,
    pipeline_health,
    projection_sizes,
    stage_health,
    throughput_freshness,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
RECENT = NOW - timedelta(minutes=10)
STALE = NOW - timedelta(hours=2)
STALLED_TS = NOW - timedelta(hours=30)


def _event(etype: str, ts: datetime) -> dict:
    return {"type": etype, "created_at": ts}


def _dead(error: str = "timeout", ts: datetime | None = None) -> dict:
    return {"type": "claims.extracted", "error": error, "created_at": ts or RECENT}


def _cluster(conf: float, ind: int = 2, primary: bool = False, extremity: str = "notable") -> dict:
    return {
        "confidence": conf,
        "independent_originators": ind,
        "has_primary": primary,
        "extremity": extremity,
    }


# ---------------------------------------------------------------------------
# stage_health
# ---------------------------------------------------------------------------


class TestStageHealth:
    def test_empty_event_log_returns_four_stages_all_never(self):
        stages = stage_health([], as_of=NOW)
        assert len(stages) == 4
        assert all(s["freshness"] == "never" for s in stages)
        assert all(s["count"] == 0 for s in stages)
        assert all(s["last_seen"] is None for s in stages)

    def test_stage_order_is_pipeline_order(self):
        stages = stage_health([], as_of=NOW)
        names = [s["stage"] for s in stages]
        assert names == ["acquire", "extract", "classify", "cluster"]

    def test_counts_and_last_seen_per_stage(self):
        events = [
            _event("article.ingested", RECENT),
            _event("article.ingested", STALE),
            _event("claims.extracted", RECENT),
        ]
        stages = stage_health(events, as_of=NOW)
        by = {s["stage"]: s for s in stages}
        assert by["acquire"]["count"] == 2
        assert by["acquire"]["last_seen"] == RECENT  # max of the two
        assert by["extract"]["count"] == 1
        assert by["classify"]["count"] == 0
        assert by["cluster"]["count"] == 0

    def test_fresh_label_under_one_hour(self):
        events = [_event("article.ingested", RECENT)]
        stages = stage_health(events, as_of=NOW)
        acquire = next(s for s in stages if s["stage"] == "acquire")
        assert acquire["freshness"] == "fresh"
        assert acquire["age_s"] is not None
        assert acquire["age_s"] < STALE_THRESHOLD_S

    def test_stale_label_between_one_and_24_hours(self):
        events = [_event("article.ingested", STALE)]
        stages = stage_health(events, as_of=NOW)
        acquire = next(s for s in stages if s["stage"] == "acquire")
        assert acquire["freshness"] == "stale"
        assert STALE_THRESHOLD_S <= acquire["age_s"] < STALLED_THRESHOLD_S

    def test_stalled_label_over_24_hours(self):
        events = [_event("article.ingested", STALLED_TS)]
        stages = stage_health(events, as_of=NOW)
        acquire = next(s for s in stages if s["stage"] == "acquire")
        assert acquire["freshness"] == "stalled"
        assert acquire["age_s"] >= STALLED_THRESHOLD_S

    def test_unrecognised_event_types_are_ignored(self):
        events = [_event("admin.threshold.changed", RECENT)]
        stages = stage_health(events, as_of=NOW)
        assert all(s["count"] == 0 for s in stages)

    def test_naive_datetime_handled_without_error(self):
        naive_ts = datetime(2024, 6, 1, 11, 50, 0)  # no tzinfo
        events = [_event("article.ingested", naive_ts)]
        # Should not raise; treated as UTC
        stages = stage_health(events, as_of=NOW)
        acquire = next(s for s in stages if s["stage"] == "acquire")
        assert acquire["count"] == 1


# ---------------------------------------------------------------------------
# dead_letter_summary
# ---------------------------------------------------------------------------


class TestDeadLetterSummary:
    def test_empty_returns_zero_total(self):
        d = dead_letter_summary([])
        assert d["total"] == 0
        assert d["recent"] == []
        assert d["error_preview"] == ""

    def test_total_matches_input_length(self):
        rows = [_dead(f"err-{i}") for i in range(20)]
        d = dead_letter_summary(rows)
        assert d["total"] == 20

    def test_recent_capped_at_limit(self):
        rows = [_dead(f"err-{i}") for i in range(20)]
        d = dead_letter_summary(rows, limit=5)
        assert len(d["recent"]) == 5

    def test_recent_default_limit_is_10(self):
        rows = [_dead(f"err-{i}") for i in range(15)]
        d = dead_letter_summary(rows)
        assert len(d["recent"]) == 10

    def test_error_preview_from_first_entry(self):
        rows = [_dead("first error"), _dead("second error")]
        d = dead_letter_summary(rows)
        assert d["error_preview"] == "first error"

    def test_error_preview_truncated_at_200_chars(self):
        long_err = "x" * 500
        rows = [_dead(long_err)]
        d = dead_letter_summary(rows)
        assert len(d["error_preview"]) == 201  # 200 chars + ellipsis char
        assert d["error_preview"].endswith("…")

    def test_error_preview_short_not_truncated(self):
        rows = [_dead("short")]
        d = dead_letter_summary(rows)
        assert d["error_preview"] == "short"

    def test_missing_error_key_gives_empty_preview(self):
        rows = [{"type": "claims.extracted", "created_at": RECENT}]
        d = dead_letter_summary(rows)
        assert d["error_preview"] == ""


# ---------------------------------------------------------------------------
# projection_sizes
# ---------------------------------------------------------------------------


class TestProjectionSizes:
    def test_zero_defaults_for_missing_keys(self):
        p = projection_sizes({})
        assert p == {"articles": 0, "claims": 0, "clusters": 0}

    def test_counts_passed_through(self):
        p = projection_sizes({"articles": 10, "claims": 50, "clusters": 5})
        assert p == {"articles": 10, "claims": 50, "clusters": 5}

    def test_extra_keys_ignored(self):
        p = projection_sizes({"articles": 3, "claims": 7, "clusters": 1, "events": 100})
        assert "events" not in p

    def test_none_values_coerced_to_zero(self):
        p = projection_sizes({"articles": None, "claims": 0, "clusters": None})
        assert p["articles"] == 0
        assert p["clusters"] == 0


# ---------------------------------------------------------------------------
# throughput_freshness
# ---------------------------------------------------------------------------


class TestThroughputFreshness:
    def test_empty_event_log_is_never(self):
        t = throughput_freshness([], as_of=NOW)
        assert t["freshness"] == "never"
        assert t["newest_event_age_s"] is None
        assert t["newest_event_at"] is None

    def test_recent_event_is_fresh(self):
        t = throughput_freshness([_event("article.ingested", RECENT)], as_of=NOW)
        assert t["freshness"] == "fresh"
        assert t["newest_event_age_s"] is not None
        assert t["newest_event_age_s"] < STALE_THRESHOLD_S

    def test_old_event_is_stalled(self):
        t = throughput_freshness([_event("article.ingested", STALLED_TS)], as_of=NOW)
        assert t["freshness"] == "stalled"

    def test_picks_newest_across_all_types(self):
        events = [
            _event("article.ingested", STALLED_TS),
            _event("claims.extracted", RECENT),
        ]
        t = throughput_freshness(events, as_of=NOW)
        assert t["newest_event_at"] == RECENT
        assert t["freshness"] == "fresh"


# ---------------------------------------------------------------------------
# calibration_health
# ---------------------------------------------------------------------------


class TestCalibrationHealth:
    def test_empty_clusters(self):
        ch = calibration_health([])
        assert ch["n"] == 0
        assert ch["mean_confidence"] is None
        assert ch["confidence_distribution"] == {"hi": 0, "mid": 0, "lo": 0, "floor": 0}

    def test_single_well_corroborated_cluster(self):
        ch = calibration_health([_cluster(0.90)])
        assert ch["n"] == 1
        assert ch["well_corroborated"] == 1
        assert ch["thinly_sourced"] == 0
        assert ch["confidence_distribution"]["hi"] == 1

    def test_single_thinly_sourced_cluster(self):
        ch = calibration_health([_cluster(0.32)])
        assert ch["thinly_sourced"] == 1
        assert ch["well_corroborated"] == 0
        assert ch["confidence_distribution"]["floor"] == 1

    def test_distribution_bands(self):
        clusters = [
            _cluster(0.90),  # hi
            _cluster(0.75),  # mid
            _cluster(0.55),  # lo
            _cluster(0.30),  # floor
        ]
        ch = calibration_health(clusters)
        d = ch["confidence_distribution"]
        assert d == {"hi": 1, "mid": 1, "lo": 1, "floor": 1}

    def test_mean_confidence_correct(self):
        clusters = [_cluster(0.4), _cluster(0.6)]
        ch = calibration_health(clusters)
        assert ch["mean_confidence"] == pytest.approx(0.5, abs=1e-3)

    def test_single_source_count(self):
        clusters = [
            _cluster(0.45, ind=1),
            _cluster(0.70, ind=2),
            _cluster(0.85, ind=1),
        ]
        ch = calibration_health(clusters)
        assert ch["single_source"] == 2

    def test_has_primary_count(self):
        clusters = [
            _cluster(0.80, primary=True),
            _cluster(0.60, primary=False),
            _cluster(0.90, primary=True),
        ]
        ch = calibration_health(clusters)
        assert ch["has_primary_count"] == 2

    def test_boundary_at_0_7_is_well_corroborated(self):
        ch = calibration_health([_cluster(0.7)])
        assert ch["well_corroborated"] == 1

    def test_boundary_at_0_5_is_not_thinly_sourced(self):
        ch = calibration_health([_cluster(0.5)])
        assert ch["thinly_sourced"] == 0

    def test_boundary_below_0_5_is_thinly_sourced(self):
        ch = calibration_health([_cluster(0.499)])
        assert ch["thinly_sourced"] == 1


# ---------------------------------------------------------------------------
# pipeline_health — integrated scenarios
# ---------------------------------------------------------------------------


class TestPipelineHealth:
    def _healthy_events(self):
        return [
            _event("article.ingested", RECENT),
            _event("article.ingested", RECENT),
            _event("claims.extracted", RECENT),
            _event("claims.classified", RECENT),
            _event("cluster.corroborated", RECENT),
        ]

    def test_healthy_pipeline(self):
        summary = pipeline_health(
            self._healthy_events(),
            dead_rows=[],
            projection_counts={"articles": 10, "claims": 25, "clusters": 5},
            as_of=NOW,
        )
        assert summary["status"] == "healthy"
        assert summary["alerts"] == []

    def test_summary_keys_present(self):
        summary = pipeline_health([], [], {}, as_of=NOW)
        for key in ("as_of", "status", "stages", "dead_letters", "projections",
                    "throughput", "calibration", "alerts"):
            assert key in summary

    def test_as_of_is_iso_string(self):
        summary = pipeline_health([], [], {}, as_of=NOW)
        assert summary["as_of"] == NOW.isoformat()

    def test_empty_pipeline_status(self):
        summary = pipeline_health([], [], {"articles": 0, "claims": 0, "clusters": 0}, as_of=NOW)
        assert summary["status"] == "empty"

    def test_dead_letters_degrade_status(self):
        summary = pipeline_health(
            self._healthy_events(),
            dead_rows=[_dead("parse failure")],
            projection_counts={"articles": 5, "claims": 10, "clusters": 2},
            as_of=NOW,
        )
        assert summary["status"] == "degraded"
        assert any("dead-letter" in a for a in summary["alerts"])

    def test_stalled_stage_changes_status_to_stalled(self):
        stalled_events = [
            _event("article.ingested", STALLED_TS),
            _event("claims.extracted", STALLED_TS),
            _event("claims.classified", STALLED_TS),
            _event("cluster.corroborated", STALLED_TS),
        ]
        summary = pipeline_health(
            stalled_events,
            dead_rows=[],
            projection_counts={"articles": 10, "claims": 25, "clusters": 5},
            as_of=NOW,
        )
        assert summary["status"] == "stalled"
        assert any("last ran" in a for a in summary["alerts"])

    def test_never_run_stages_produce_alerts(self):
        # Only acquire has run; extract/classify/cluster have never run
        events = [_event("article.ingested", RECENT)]
        summary = pipeline_health(
            events,
            dead_rows=[],
            projection_counts={"articles": 2, "claims": 0, "clusters": 0},
            as_of=NOW,
        )
        alerts = summary["alerts"]
        assert any("extract" in a and "never" in a for a in alerts)
        assert any("classify" in a and "never" in a for a in alerts)
        assert any("cluster" in a and "never" in a for a in alerts)

    def test_no_articles_alert(self):
        summary = pipeline_health(
            [],
            dead_rows=[],
            projection_counts={"articles": 0, "claims": 0, "clusters": 0},
            as_of=NOW,
        )
        assert any("No articles ingested" in a for a in summary["alerts"])

    def test_articles_but_no_clusters_alert(self):
        events = [_event("article.ingested", RECENT)]
        summary = pipeline_health(
            events,
            dead_rows=[],
            projection_counts={"articles": 5, "claims": 0, "clusters": 0},
            as_of=NOW,
        )
        assert any("no clusters" in a.lower() for a in summary["alerts"])

    def test_calibration_embedded_in_summary(self):
        clusters = [_cluster(0.85, ind=3, primary=True)]
        summary = pipeline_health(
            self._healthy_events(),
            dead_rows=[],
            projection_counts={"articles": 5, "claims": 10, "clusters": 1},
            clusters=clusters,
            as_of=NOW,
        )
        assert summary["calibration"]["n"] == 1
        assert summary["calibration"]["well_corroborated"] == 1

    def test_projection_sizes_embedded(self):
        summary = pipeline_health(
            self._healthy_events(),
            dead_rows=[],
            projection_counts={"articles": 7, "claims": 14, "clusters": 3},
            as_of=NOW,
        )
        assert summary["projections"] == {"articles": 7, "claims": 14, "clusters": 3}

    def test_dead_letter_limit_respected(self):
        dead_rows = [_dead(f"err-{i}") for i in range(20)]
        summary = pipeline_health(
            self._healthy_events(),
            dead_rows=dead_rows,
            projection_counts={"articles": 5, "claims": 10, "clusters": 2},
            dead_letter_limit=3,
            as_of=NOW,
        )
        assert summary["dead_letters"]["total"] == 20
        assert len(summary["dead_letters"]["recent"]) == 3

    def test_stalled_throughput_produces_alert(self):
        events = [_event("article.ingested", STALLED_TS)]
        summary = pipeline_health(
            events,
            dead_rows=[],
            projection_counts={"articles": 3, "claims": 6, "clusters": 1},
            as_of=NOW,
        )
        assert any("No pipeline activity" in a for a in summary["alerts"])

    def test_stages_list_has_correct_count_values(self):
        events = [
            _event("article.ingested", RECENT),
            _event("article.ingested", RECENT),
            _event("claims.extracted", RECENT),
        ]
        summary = pipeline_health(
            events,
            dead_rows=[],
            projection_counts={"articles": 2, "claims": 3, "clusters": 0},
            as_of=NOW,
        )
        by = {s["stage"]: s for s in summary["stages"]}
        assert by["acquire"]["count"] == 2
        assert by["extract"]["count"] == 1
        assert by["classify"]["count"] == 0
        assert by["cluster"]["count"] == 0
