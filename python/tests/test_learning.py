"""Learning/calibration tests (P3, §5.8) — truth-over-time resolution, scoring, auto-tune. No DB."""

from maat.learning.calibration import (
    Observation,
    Weights,
    brier_score,
    calibration_bins,
    observations_from_history,
    replay_ab,
    resolve_outcome,
    tune_decay,
    tune_proposals,
)
from maat.pipeline.corroborate import confidence_read


def test_weights_read_scores_the_live_function():
    # the harness must score the REAL confidence_read, not a divergent copy
    w = Weights.defaults()
    assert w.read(2, False, "notable") == confidence_read(2, False, "notable")
    assert w.read(1, True, "extraordinary") == confidence_read(1, True, "extraordinary")


def test_resolve_outcome_reads_the_trajectory():
    # reached independent corroboration → confirmed; primary surfacing also confirms
    assert resolve_outcome(1, 4, latest_has_primary=False, corrected=False) == "confirmed"
    assert resolve_outcome(1, 1, latest_has_primary=True, corrected=False) == "confirmed"
    # a correction refutes regardless of corroboration
    assert resolve_outcome(3, 3, latest_has_primary=True, corrected=True) == "refuted"
    # gaining ground but below the bar is not yet terminal
    assert resolve_outcome(1, 2, latest_has_primary=False, corrected=False) == "corroborating"
    # never grew past its thin start
    assert resolve_outcome(1, 1, latest_has_primary=False, corrected=False) == "unconfirmed"


def test_brier_is_none_until_something_resolves():
    assert brier_score([]) is None
    # in-flight outcomes are not scorable against a 0/1 target
    assert brier_score([Observation(1, False, "notable", "corroborating")]) is None


def test_tune_decay_corrects_underconfident_reads():
    # extraordinary facts that keep confirming on modest corroboration: the default decay (0.76)
    # reads them ~0.42, far below their 1.0 outcome — the tuner should lower that decay to fit.
    obs = [Observation(2, False, "extraordinary", "confirmed") for _ in range(5)]
    base_b = brier_score(obs)
    tuned, tuned_b = tune_decay(obs)
    assert base_b is not None and tuned_b is not None
    assert tuned_b < base_b
    assert tuned.decay["extraordinary"] < Weights.defaults().decay["extraordinary"]
    # levels with no evidence keep their starting point — the tuner never invents from nothing
    assert tuned.decay["routine"] == Weights.defaults().decay["routine"]


def test_tune_decay_returns_base_with_no_resolved_history():
    base, b = tune_decay([])
    assert b is None
    assert base == Weights.defaults()


def test_calibration_bins_expose_the_gap():
    # all four reads land in one band and all confirm — the table shows predicted << actual
    obs = [Observation(2, False, "extraordinary", "confirmed") for _ in range(4)]
    bins = calibration_bins(obs)
    assert len(bins) == 1
    assert bins[0].n == 4
    assert bins[0].actual == 1.0  # every one confirmed
    assert bins[0].predicted < bins[0].actual  # under-confident, by construction


def test_observations_from_history_groups_by_fact_and_keeps_initial_read():
    events = [  # oldest → newest, as the event log returns them
        {"fact": "Minister X resigned", "independent_originators": 1, "has_primary": False, "extremity": "notable"},
        {"fact": "minister x  resigned", "independent_originators": 2, "has_primary": False, "extremity": "notable"},
        {"fact": "Minister X resigned", "independent_originators": 4, "has_primary": False, "extremity": "notable"},
        {"fact": "Country Z rigged its vote", "independent_originators": 1, "has_primary": False, "extremity": "extraordinary"},
    ]
    obs = observations_from_history(events)
    assert len(obs) == 2  # grouped by normalised fact (case / whitespace insensitive)
    by = {o.extremity: o for o in obs}
    assert by["notable"].independent_originators == 1  # the INITIAL read, not the latest
    assert by["notable"].outcome == "confirmed"        # it accrued corroboration over the ticks
    assert by["extraordinary"].outcome == "unconfirmed"  # a lone claim that never grew


def test_tune_proposals_target_config_keys_with_rationale():
    # the suggestions file directly onto Config-panel knob rows (decay.<level>), with the why
    obs = [Observation(2, False, "extraordinary", "confirmed") for _ in range(5)]
    props = {p["key"]: p for p in tune_proposals(obs)}
    assert "decay.extraordinary" in props
    assert float(props["decay.extraordinary"]["value"]) < Weights.defaults().decay["extraordinary"]
    assert "Brier" in props["decay.extraordinary"]["reason"]


def test_tune_proposals_empty_when_nothing_resolved():
    assert tune_proposals([]) == []


def test_tune_decay_never_proposes_worse_than_current_with_sparse_grid():
    # a sparse grid can miss the current weight; the tuner must still never raise Brier.
    # (regression: it used to adopt the first grid point unconditionally, ignoring the base.)
    obs = [Observation(1, False, "notable", "confirmed"), Observation(1, False, "notable", "refuted")]
    base_b = brier_score(obs)
    _, tuned_b = tune_decay(obs, grid=(0.30, 0.82))
    assert base_b is not None and tuned_b is not None
    assert tuned_b <= base_b


def test_tune_decay_brier_never_increases_on_mixed_outcomes():
    obs = (
        [Observation(2, False, "extraordinary", "confirmed")] * 4
        + [Observation(5, False, "routine", "refuted")] * 3
        + [Observation(3, True, "notable", "confirmed")] * 3
    )
    base_b = brier_score(obs)
    _, tuned_b = tune_decay(obs)
    assert base_b is not None and tuned_b is not None
    assert tuned_b <= base_b


def test_replay_ab_counts_verdict_flips():
    # replay-before-promote: a more-confident candidate should PROMOTE these confirmed reads,
    # and that downstream impact is reported (not just the Brier delta).
    obs = [Observation(2, False, "extraordinary", "confirmed") for _ in range(3)]
    base = Weights.defaults()
    candidate = Weights({**base.decay, "extraordinary": 0.40}, base.primary_lift, base.cap)
    ab = replay_ab(obs, base, candidate)
    assert ab.n_scored == 3
    assert ab.flips == 3 and ab.promoted == 3 and ab.demoted == 0
    assert ab.brier_candidate < ab.brier_base  # more confident on facts that confirmed → better


def test_tune_decay_respects_step_bounds():
    # the same facts want a big swing (decay → ~0.30), but a single step is bounded (max-delta),
    # so the suggestion moves toward confidence without leaping there on thin evidence.
    obs = [Observation(2, False, "extraordinary", "confirmed") for _ in range(8)]
    cur = Weights.defaults().decay["extraordinary"]
    tuned, _ = tune_decay(obs)
    assert tuned.decay["extraordinary"] >= cur - 0.25 - 1e-9  # never jumps more than the max delta
    assert tuned.decay["extraordinary"] < cur  # but it did move
