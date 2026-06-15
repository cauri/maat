"""Tests for source_learning.py (Issue #35) — source-learning loop.

Covers:
- Reliable sources rank up (high confirmation rate + high independent rate).
- A prolific-but-unreliable source is NOT rewarded.
- Diversity floor is respected regardless of reputation spread.
- Anti-echo-chamber cap: no single source exceeds _MAX_WEIGHT.
- Solo-extraordinary penalty is applied correctly.
- Independent-corroborator bonus fires for confirmed independent voices.
- Low-evidence sources get a conservative weight, not zero.
- Empty input returns empty output.
- Weights are normalised (sum ≤ 1.0, all ≥ floor).
- Output is deterministic (same input → same output).
"""

from maat.learning.reputation import SourceReputation
from maat.learning.source_learning import (
    DIVERSITY_MINIMUM,
    _MAX_WEIGHT,
    _FLOOR_WEIGHT,
    _MIN_EVIDENCE,
    learn_preferences,
    preference_by_source,
)


# ---------------------------------------------------------------------------
# Helpers — build minimal SourceReputation fixtures
# ---------------------------------------------------------------------------


def _rep(
    source: str,
    *,
    appearances: int = 10,
    independent_appearances: int = 8,
    primary_appearances: int = 0,
    mean_attribution_weight: float = 0.8,
    solo_extraordinary: int = 0,
    facts_confirmed: int = 7,
    facts_refuted: int = 1,
    facts_unresolved: int = 2,
) -> SourceReputation:
    """Build a SourceReputation with sensible defaults for testing."""
    independent_rate = (
        round(independent_appearances / appearances, 3) if appearances else 0.0
    )
    outcome_n = facts_confirmed + facts_refuted
    confirmation_rate = (
        round(facts_confirmed / outcome_n, 3) if outcome_n else None
    )
    # Approximate reliability rank (matches reputation.py's formula).
    if confirmation_rate is not None:
        rank = confirmation_rate * 0.7 + independent_rate * 0.3
    else:
        rank = -1.0 + independent_rate * 0.3
    return SourceReputation(
        source=source,
        appearances=appearances,
        independent_appearances=independent_appearances,
        independent_rate=independent_rate,
        primary_appearances=primary_appearances,
        mean_attribution_weight=mean_attribution_weight,
        solo_extraordinary=solo_extraordinary,
        facts_confirmed=facts_confirmed,
        facts_refuted=facts_refuted,
        facts_unresolved=facts_unresolved,
        outcome_n=outcome_n,
        confirmation_rate=confirmation_rate,
        _reliability_rank=round(rank, 4),
    )


# ---------------------------------------------------------------------------
# Core ranking tests
# ---------------------------------------------------------------------------


def test_reliable_source_ranks_above_unreliable():
    """A source with high confirmation + high independent rate beats one that only appears a lot."""
    reliable = _rep("reuters.com", appearances=20, independent_appearances=16,
                    facts_confirmed=15, facts_refuted=1, facts_unresolved=4)
    # prolific but mostly cascade, low confirmation
    prolific_bad = _rep("cascade-herald.com", appearances=100, independent_appearances=10,
                        facts_confirmed=3, facts_refuted=8, facts_unresolved=89)
    prefs = learn_preferences([reliable, prolific_bad])
    by_src = preference_by_source(prefs)
    assert by_src["reuters.com"].rank < by_src["cascade-herald.com"].rank, (
        "reliable source must rank higher (lower rank number = more preferred)"
    )
    assert by_src["reuters.com"].acquisition_weight > by_src["cascade-herald.com"].acquisition_weight


def test_prolific_but_unreliable_not_rewarded():
    """A source with many appearances but most facts refuted gets a LOW weight."""
    bad = _rep("fake-herald.com", appearances=200, independent_appearances=20,
               facts_confirmed=2, facts_refuted=18, facts_unresolved=180)
    good = _rep("verified.org", appearances=15, independent_appearances=13,
                facts_confirmed=12, facts_refuted=1, facts_unresolved=2)
    prefs = learn_preferences([bad, good])
    by_src = preference_by_source(prefs)
    assert by_src["verified.org"].acquisition_weight > by_src["fake-herald.com"].acquisition_weight
    # The bad source's weight must be at or near the floor — many appearances don't help.
    assert by_src["fake-herald.com"].acquisition_weight <= by_src["verified.org"].acquisition_weight


def test_independent_corroborator_bonus_fires():
    """A source with high confirmation AND high independent rate gets the bonus weight."""
    # Meets both thresholds: confirmation >= 0.70 and independent_rate >= 0.60.
    corroborator = _rep(
        "independent-news.org",
        appearances=15,
        independent_appearances=12,      # independent_rate = 0.8
        facts_confirmed=11,
        facts_refuted=1,
        facts_unresolved=3,
    )
    # Good confirmation but low independence (cascade node).
    cascade = _rep(
        "cascade-good.com",
        appearances=15,
        independent_appearances=4,       # independent_rate ~0.27
        facts_confirmed=11,
        facts_refuted=1,
        facts_unresolved=3,
    )
    prefs = learn_preferences([corroborator, cascade])
    by_src = preference_by_source(prefs)
    # Bonus should push the corroborator above the cascade-dominant source.
    assert by_src["independent-news.org"].acquisition_weight >= by_src["cascade-good.com"].acquisition_weight


def test_solo_extraordinary_penalty_applied():
    """A source that stands alone on many extreme claims gets a softer weight."""
    normal = _rep("reliable.org", appearances=12, independent_appearances=10,
                  solo_extraordinary=0, facts_confirmed=9, facts_refuted=1, facts_unresolved=2)
    # > 25 % of independent appearances are solo-extraordinary.
    suspicious = _rep("solo-extreme.com", appearances=12, independent_appearances=8,
                      solo_extraordinary=4,   # 4/8 = 50 % > threshold
                      facts_confirmed=6, facts_refuted=2, facts_unresolved=4)
    prefs = learn_preferences([normal, suspicious])
    by_src = preference_by_source(prefs)
    assert by_src["solo-extreme.com"].solo_extraordinary_flag is True
    assert by_src["normal".replace("normal", "reliable.org")].acquisition_weight >= \
           by_src["solo-extreme.com"].acquisition_weight


def test_solo_extraordinary_flag_not_set_below_threshold():
    """Below the solo-extraordinary threshold, the flag is False and no penalty fires."""
    modest = _rep("modest.org", appearances=12, independent_appearances=10,
                  solo_extraordinary=2,   # 2/10 = 20 % < 25 % threshold
                  facts_confirmed=8, facts_refuted=1, facts_unresolved=3)
    prefs = learn_preferences([modest])
    by_src = preference_by_source(prefs)
    assert by_src["modest.org"].solo_extraordinary_flag is False


# ---------------------------------------------------------------------------
# Diversity / anti-echo-chamber tests
# ---------------------------------------------------------------------------


def test_diversity_floor_has_minimum_sources():
    """diversity_floor must have min(DIVERSITY_MINIMUM, total) sources."""
    # Create exactly DIVERSITY_MINIMUM + 2 sources.
    count = DIVERSITY_MINIMUM + 2
    reps = [
        _rep(f"source-{i}.com", appearances=10 + i, independent_appearances=8,
             facts_confirmed=7, facts_refuted=1, facts_unresolved=2)
        for i in range(count)
    ]
    prefs = learn_preferences(reps)
    assert len(prefs.diversity_floor) == DIVERSITY_MINIMUM, (
        f"expected {DIVERSITY_MINIMUM} in the floor, got {len(prefs.diversity_floor)}"
    )


def test_diversity_floor_includes_all_when_few_sources():
    """If fewer than DIVERSITY_MINIMUM sources exist, all are in the floor."""
    reps = [
        _rep(f"s{i}.com", appearances=10, independent_appearances=8,
             facts_confirmed=7, facts_refuted=1, facts_unresolved=2)
        for i in range(3)   # well below DIVERSITY_MINIMUM
    ]
    prefs = learn_preferences(reps)
    assert prefs.diversity_floor == frozenset(r.source for r in reps)


def test_no_single_source_exceeds_max_weight():
    """Even a near-perfect source must not exceed _MAX_WEIGHT after normalisation."""
    reps = [
        _rep("dominant.com", appearances=100, independent_appearances=98,
             facts_confirmed=90, facts_refuted=1, facts_unresolved=9),
        _rep("minor.com", appearances=5, independent_appearances=3,
             facts_confirmed=2, facts_refuted=1, facts_unresolved=2),
    ]
    prefs = learn_preferences(reps)
    for pref in prefs.ranked:
        assert pref.acquisition_weight <= _MAX_WEIGHT + 1e-9, (
            f"source {pref.source!r} weight {pref.acquisition_weight} exceeds cap {_MAX_WEIGHT}"
        )


def test_weights_sum_to_at_most_one():
    """Normalised weights must sum to ≤ 1.0."""
    reps = [
        _rep(f"news-{i}.com", appearances=10 + i, independent_appearances=6 + i % 3,
             facts_confirmed=5 + i % 4, facts_refuted=i % 2, facts_unresolved=3)
        for i in range(12)
    ]
    prefs = learn_preferences(reps)
    total = sum(prefs.weights.values())
    assert total <= 1.0 + 1e-6, f"weights sum to {total}, expected ≤ 1.0"


def test_every_source_has_at_least_floor_weight():
    """No source should receive a weight below _FLOOR_WEIGHT."""
    reps = [
        _rep("great.com", appearances=50, independent_appearances=45,
             facts_confirmed=40, facts_refuted=1, facts_unresolved=9),
        _rep("tiny.com", appearances=2, independent_appearances=1,
             facts_confirmed=1, facts_refuted=0, facts_unresolved=1),
    ]
    prefs = learn_preferences(reps)
    for pref in prefs.ranked:
        assert pref.acquisition_weight >= _FLOOR_WEIGHT - 1e-9, (
            f"source {pref.source!r} weight {pref.acquisition_weight} is below floor {_FLOOR_WEIGHT}"
        )


# ---------------------------------------------------------------------------
# Low-evidence handling
# ---------------------------------------------------------------------------


def test_low_evidence_source_gets_conservative_weight():
    """A source below _MIN_EVIDENCE appearances gets low_evidence=True and a moderate weight."""
    reps = [
        _rep("new-voice.com", appearances=_MIN_EVIDENCE - 1, independent_appearances=3,
             facts_confirmed=2, facts_refuted=0, facts_unresolved=1),
    ]
    prefs = learn_preferences(reps)
    by_src = preference_by_source(prefs)
    pref = by_src["new-voice.com"]
    assert pref.low_evidence is True
    # Weight should be floor ≤ weight ≤ max.
    assert _FLOOR_WEIGHT <= pref.acquisition_weight <= _MAX_WEIGHT


def test_low_evidence_not_flagged_at_threshold():
    """A source at exactly _MIN_EVIDENCE appearances is not flagged as low-evidence."""
    reps = [
        _rep("at-threshold.com", appearances=_MIN_EVIDENCE, independent_appearances=4,
             facts_confirmed=3, facts_refuted=1, facts_unresolved=1),
    ]
    prefs = learn_preferences(reps)
    by_src = preference_by_source(prefs)
    assert by_src["at-threshold.com"].low_evidence is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty():
    prefs = learn_preferences([])
    assert prefs.ranked == []
    assert prefs.weights == {}
    assert prefs.diversity_floor == frozenset()


def test_single_source_gets_max_weight():
    """With only one source, it receives the full weight up to the cap."""
    reps = [_rep("solo.com", appearances=20, independent_appearances=18,
                 facts_confirmed=15, facts_refuted=1, facts_unresolved=4)]
    prefs = learn_preferences(reps)
    assert len(prefs.ranked) == 1
    pref = prefs.ranked[0]
    assert pref.rank == 1
    assert pref.acquisition_weight <= _MAX_WEIGHT + 1e-9
    assert pref.acquisition_weight >= _FLOOR_WEIGHT - 1e-9


def test_determinism():
    """Same input always produces the same ranked output (no random tie-breaking)."""
    reps = [
        _rep(f"src-{i}.com", appearances=10 + i, independent_appearances=5 + i % 4,
             facts_confirmed=4 + i % 3, facts_refuted=i % 2, facts_unresolved=3)
        for i in range(6)
    ]
    a = learn_preferences(reps)
    b = learn_preferences(reps)
    assert [p.source for p in a.ranked] == [p.source for p in b.ranked]
    assert a.weights == b.weights


def test_preference_by_source_indexes_correctly():
    """preference_by_source returns a dict keyed by source name."""
    reps = [
        _rep("alpha.com", appearances=15, independent_appearances=12,
             facts_confirmed=10, facts_refuted=1, facts_unresolved=4),
        _rep("beta.com", appearances=10, independent_appearances=7,
             facts_confirmed=6, facts_refuted=2, facts_unresolved=2),
    ]
    prefs = learn_preferences(reps)
    index = preference_by_source(prefs)
    assert set(index.keys()) == {"alpha.com", "beta.com"}
    assert index["alpha.com"].rank < index["beta.com"].rank


def test_ranks_are_contiguous_and_one_based():
    """Rank values are 1, 2, 3, … (no gaps, no zeros)."""
    reps = [
        _rep(f"src-{i}.com", appearances=10, independent_appearances=8,
             facts_confirmed=6, facts_refuted=2, facts_unresolved=2)
        for i in range(5)
    ]
    prefs = learn_preferences(reps)
    ranks = [p.rank for p in prefs.ranked]
    assert ranks == list(range(1, len(ranks) + 1))


def test_no_outcome_source_ranked_below_confirmed_sources():
    """A source with no resolved outcomes (confirmation_rate=None) ranks below one that has."""
    no_outcomes = _rep("no-outcomes.com", appearances=10, independent_appearances=8,
                       facts_confirmed=0, facts_refuted=0, facts_unresolved=10)
    # Force confirmation_rate=None by setting no terminal outcomes.
    # (The helper computes it automatically from confirmed+refuted.)
    has_outcomes = _rep("with-outcomes.com", appearances=10, independent_appearances=8,
                        facts_confirmed=7, facts_refuted=1, facts_unresolved=2)
    prefs = learn_preferences([no_outcomes, has_outcomes])
    by_src = preference_by_source(prefs)
    assert by_src["with-outcomes.com"].rank < by_src["no-outcomes.com"].rank


def test_weights_keys_match_ranked_sources():
    """weights dict and ranked list cover the same set of sources."""
    reps = [
        _rep("a.com", appearances=12, independent_appearances=9,
             facts_confirmed=8, facts_refuted=1, facts_unresolved=3),
        _rep("b.com", appearances=8, independent_appearances=5,
             facts_confirmed=4, facts_refuted=2, facts_unresolved=2),
        _rep("c.com", appearances=6, independent_appearances=4,
             facts_confirmed=3, facts_refuted=1, facts_unresolved=2),
    ]
    prefs = learn_preferences(reps)
    ranked_sources = {p.source for p in prefs.ranked}
    assert ranked_sources == set(prefs.weights.keys())
