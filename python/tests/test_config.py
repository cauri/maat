"""#183/#184 — operator config enactment: folding the promoted stream into live pipeline knobs."""

from maat.config import KNOBS_BY_KEY, active_config, pipeline_overrides


def test_active_config_latest_promote_per_key_wins():
    events = [
        {"key": "decay.notable", "value": "0.5"},
        {"key": "confidence.primary_lift", "value": "0.6"},
        {"key": "decay.notable", "value": "0.45"},  # operator promoted a newer value
        {"key": "model.judge", "value": "claude-haiku-4-5-20251001"},  # not an enactable knob → ignored
    ]
    assert active_config(events) == {"decay.notable": 0.45, "confidence.primary_lift": 0.6}


def test_active_config_ignores_unparseable_values():
    assert active_config([{"key": "confidence.primary_lift", "value": "notanumber"}]) == {}


def test_pipeline_overrides_fills_defaults_for_untouched_keys():
    ov = pipeline_overrides({"decay.notable": 0.45, "cluster.min_corroboration": 3})
    assert ov["decay"]["notable"] == 0.45  # promoted
    assert ov["decay"]["routine"] == float(KNOBS_BY_KEY["decay.routine"]["default"])  # untouched → default
    assert ov["primary_lift"] == float(KNOBS_BY_KEY["confidence.primary_lift"]["default"])
    assert ov["cap"] == float(KNOBS_BY_KEY["confidence.cap"]["default"])
    assert ov["min_corroboration"] == 3 and isinstance(ov["min_corroboration"], int)


def test_pipeline_overrides_shape_matches_corroborate_kwargs():
    ov = pipeline_overrides({})
    assert set(ov) == {
        "decay", "primary_lift", "cap",
        "same_fact_threshold", "duplicate_source_threshold", "min_corroboration",
    }
    assert set(ov["decay"]) == {"routine", "ordinary", "notable", "significant", "extraordinary"}
