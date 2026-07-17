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


def test_same_fact_default_is_sourced_from_the_live_constant():
    """#436 — the knob registry must READ the code, never mirror it.

    config.py's own docstring promises defaults are sourced from live code so the admin view can
    never drift — but cluster.same_fact was a mirrored string literal, and the default duplicated
    into SEVEN call sites (registry, corroborate(), four analyse signatures, serving _Assets).
    When the bar changed 0.82 → 0.90 (measured percolation, cauri 2026-07-17), every copy had to be
    hunted by hand. This pins the registry to corroborate.SAME_FACT_THRESHOLD; the signature
    defaults reference the same constant, so the next change is one line.
    """
    from maat.config import KNOBS_BY_KEY
    from maat.pipeline import analyse as pa
    from maat.pipeline.corroborate import SAME_FACT_THRESHOLD, corroborate
    import inspect

    assert float(KNOBS_BY_KEY["cluster.same_fact"]["default"]) == SAME_FACT_THRESHOLD
    # and the pipeline signatures genuinely bind the constant, not a re-typed literal
    assert inspect.signature(corroborate).parameters["same_fact_threshold"].default == SAME_FACT_THRESHOLD
    assert inspect.signature(pa.analyse_article).parameters["same_fact_threshold"].default == SAME_FACT_THRESHOLD
    assert inspect.signature(pa.check_one_claim).parameters["same_fact_threshold"].default == SAME_FACT_THRESHOLD
    assert inspect.signature(pa.consolidate_claims).parameters["threshold"].default == SAME_FACT_THRESHOLD
    assert inspect.signature(pa.match_claims).parameters["threshold"].default == SAME_FACT_THRESHOLD


def test_a_promoted_same_fact_override_still_beats_the_new_default():
    """Raising the default must not disturb the operator-override path: a promoted
    admin.config.promoted for cluster.same_fact wins over SAME_FACT_THRESHOLD, exactly as before."""
    from maat.config import active_config, pipeline_overrides

    cfg = active_config([{"key": "cluster.same_fact", "value": "0.85"}])
    assert pipeline_overrides(cfg)["same_fact_threshold"] == 0.85
