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


def test_scoring_knobs_registered_and_enactable():
    # #412 — the S1/S2/S4/S5 scoring weights are in the registry (the Config panel renders it
    # generically) AND enactable, so a promote actually applies on the analyse path.
    from maat.config import _ENACTABLE

    new = {"reputation.unrated", "reputation.floor", "weight.own",
           "entailment.floor", "publisher.ceiling_floor"}
    assert new <= set(KNOBS_BY_KEY)
    assert new <= _ENACTABLE
    for key in new:
        k = KNOBS_BY_KEY[key]
        assert k["type"] == "float" and k["core"] is True and k["help"]
        float(k["default"])  # defaults are read from live code and parse as numbers


def test_analyse_overrides_maps_promotes_to_scoring_knobs():
    from maat.config import analyse_overrides

    ov = analyse_overrides(active_config([
        {"key": "reputation.unrated", "value": "0.95"},
        {"key": "weight.own", "value": "0.8"},
        {"key": "cluster.same_fact", "value": "0.85"},
    ]))
    assert ov["same_fact_threshold"] == 0.85
    kn = ov["knobs"]
    assert kn["rep_unrated"] == 0.95 and kn["w_own"] == 0.8          # promoted
    assert kn["rep_floor"] == float(KNOBS_BY_KEY["reputation.floor"]["default"])  # untouched → default
    assert kn["entail_floor"] == float(KNOBS_BY_KEY["entailment.floor"]["default"])
    assert kn["publisher_floor"] == float(KNOBS_BY_KEY["publisher.ceiling_floor"]["default"])
    # the dict is exactly the ScoringKnobs constructor's shape
    from maat.pipeline.analyse import ScoringKnobs

    ScoringKnobs(**kn)
