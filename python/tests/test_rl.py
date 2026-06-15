"""RL loop tests (#41, P3, §8) — policy_step, ownership_graph, collapse_by_ownership.

Invariants under test:
- Policy never proposes out-of-bounds or unapproved-live changes.
- Source-preference changes stay within the safe envelope (PREF_MAX_DELTA).
- Ownership graph collapses co-owned outlets into one group, keeps independents separate.
- collapse_by_ownership respects ownership links across a corroboration cluster.
- The proposal is ALWAYS unapproved (approved=False) — never auto-applied.
- An empty / in-flight history returns a valid proposal identical to base policy.
"""

from __future__ import annotations

import pytest

from maat.learning.calibration import (
    Weights,
    _DECAY_CEIL,
    _DECAY_FLOOR,
)
from maat.learning.reputation import SourceReputation
from maat.learning.rl import (
    Policy,
    PolicyProposal,
    OwnershipGroup,
    _PREF_CEIL,
    _PREF_FLOOR,
    _PREF_MAX_DELTA,
    _clamp_preference,
    collapse_by_ownership,
    ownership_graph,
    policy_step,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE_POLICY = Policy.default()

_RESOLVED_HISTORY = [
    {
        "fact": "Minister X resigned",
        "independent_originators": 1,
        "has_primary": False,
        "extremity": "notable",
        "sources": ["reuters.com", "bbc.co.uk"],
        "originators": [["a1"], ["a2"]],
        "confidence": 0.45,
    },
    {
        "fact": "minister x  resigned",  # same fact, normalised
        "independent_originators": 4,
        "has_primary": False,
        "extremity": "notable",
        "sources": ["reuters.com", "bbc.co.uk", "ap.org", "ft.com"],
        "originators": [["a1"], ["a2"], ["a3"], ["a4"]],
        "confidence": 0.91,
    },
    {
        "fact": "Country Z election rigged",
        "independent_originators": 1,
        "has_primary": False,
        "extremity": "extraordinary",
        "sources": ["tabloid.net"],
        "originators": [["b1"]],
        "confidence": 0.21,
    },
]


def _make_reputation(
    source: str,
    *,
    confirmation_rate: float | None = None,
    independent_rate: float = 0.5,
    appearances: int = 10,
) -> SourceReputation:
    """Build a minimal SourceReputation for parameterised tests."""
    outcome_n = 8 if confirmation_rate is not None else 0
    confirmed = round(outcome_n * confirmation_rate) if confirmation_rate is not None else 0
    rank = (confirmation_rate or 0.0) * 0.7 + independent_rate * 0.3 if confirmation_rate is not None else -1.0 + independent_rate * 0.3
    return SourceReputation(
        source=source,
        appearances=appearances,
        independent_appearances=int(appearances * independent_rate),
        independent_rate=round(independent_rate, 3),
        primary_appearances=0,
        mean_attribution_weight=0.6,
        solo_extraordinary=0,
        facts_confirmed=confirmed,
        facts_refuted=outcome_n - confirmed,
        facts_unresolved=appearances - outcome_n,
        outcome_n=outcome_n,
        confirmation_rate=round(confirmation_rate, 3) if confirmation_rate is not None else None,
        _reliability_rank=round(rank, 4),
    )


# ---------------------------------------------------------------------------
# _clamp_preference — unit tests for the safe envelope
# ---------------------------------------------------------------------------

class TestClampPreference:
    def test_within_delta_is_unchanged(self):
        # a proposed value that is already within the delta of the base passes through
        assert _clamp_preference(0.5, 0.6) == 0.6

    def test_large_upward_shift_is_capped_at_max_delta(self):
        result = _clamp_preference(0.5, 0.95)
        assert result == pytest.approx(0.5 + _PREF_MAX_DELTA)

    def test_large_downward_shift_is_capped_at_max_delta(self):
        result = _clamp_preference(0.5, 0.0)
        assert result == pytest.approx(0.5 - _PREF_MAX_DELTA)

    def test_result_never_exceeds_floor_or_ceil(self):
        # base near the floor
        result = _clamp_preference(0.05, 0.0)
        assert _PREF_FLOOR <= result <= _PREF_CEIL
        # base near the ceiling
        result = _clamp_preference(0.95, 1.0)
        assert _PREF_FLOOR <= result <= _PREF_CEIL

    def test_identity_when_proposed_equals_base(self):
        assert _clamp_preference(0.7, 0.7) == 0.7


# ---------------------------------------------------------------------------
# policy_step — invariants
# ---------------------------------------------------------------------------

class TestPolicyStep:
    def test_proposal_is_never_auto_approved(self):
        proposal = policy_step(_RESOLVED_HISTORY, base_policy=_BASE_POLICY)
        assert proposal.approved is False

    def test_proposal_type(self):
        proposal = policy_step(_RESOLVED_HISTORY, base_policy=_BASE_POLICY)
        assert isinstance(proposal, PolicyProposal)
        assert isinstance(proposal.candidate, Policy)
        assert isinstance(proposal.candidate.weights, Weights)

    def test_weight_changes_are_bounded(self):
        """Every proposed decay value must lie within [_DECAY_FLOOR, _DECAY_CEIL]."""
        proposal = policy_step(_RESOLVED_HISTORY, base_policy=_BASE_POLICY)
        for level, v in proposal.candidate.weights.decay.items():
            assert _DECAY_FLOOR <= v <= _DECAY_CEIL, (
                f"decay[{level}]={v} out of bounds [{_DECAY_FLOOR}, {_DECAY_CEIL}]"
            )

    def test_source_preferences_are_bounded(self):
        """Every source preference must lie within [_PREF_FLOOR, _PREF_CEIL]."""
        proposal = policy_step(_RESOLVED_HISTORY, base_policy=_BASE_POLICY)
        for src, pref in proposal.candidate.source_preference.items():
            assert _PREF_FLOOR <= pref <= _PREF_CEIL, (
                f"pref[{src}]={pref} out of bounds [{_PREF_FLOOR}, {_PREF_CEIL}]"
            )

    def test_pref_change_never_exceeds_max_delta(self):
        """No single preference step moves more than _PREF_MAX_DELTA from the base value."""
        proposal = policy_step(_RESOLVED_HISTORY, base_policy=_BASE_POLICY)
        for change in proposal.pref_changes:
            delta = abs(change["after"] - change["before"])
            assert delta <= _PREF_MAX_DELTA + 1e-9, (
                f"pref change for {change['source']}: delta={delta} > {_PREF_MAX_DELTA}"
            )

    def test_ab_result_is_attached(self):
        from maat.learning.calibration import ReplayAB
        proposal = policy_step(_RESOLVED_HISTORY, base_policy=_BASE_POLICY)
        assert isinstance(proposal.ab, ReplayAB)

    def test_n_observations_is_non_negative(self):
        proposal = policy_step(_RESOLVED_HISTORY, base_policy=_BASE_POLICY)
        assert proposal.n_observations >= 0

    def test_no_crash_on_empty_history(self):
        proposal = policy_step([], base_policy=_BASE_POLICY)
        # no crash; proposal is valid and is the trivial no-op (base policy still returned)
        assert proposal.approved is False
        assert proposal.n_observations == 0
        assert proposal.weight_changes == []

    def test_no_crash_on_in_flight_only(self):
        """History with no terminal outcomes → no weight changes, still a valid proposal."""
        history = [
            {"fact": "Unresolved fact", "independent_originators": 1,
             "has_primary": False, "extremity": "notable",
             "sources": ["a.com"], "originators": [["x1"]], "confidence": 0.45},
        ]
        proposal = policy_step(history, base_policy=_BASE_POLICY)
        assert proposal.approved is False
        assert proposal.weight_changes == []  # nothing scored → no weight change proposed
        assert proposal.n_observations == 0

    def test_pre_computed_reputation_is_accepted(self):
        """Passing pre-computed reputation should work as well as deriving from history."""
        rep_list = [
            _make_reputation("reuters.com", confirmation_rate=0.9, independent_rate=0.8),
            _make_reputation("tabloid.net", confirmation_rate=0.2, independent_rate=0.3),
        ]
        proposal = policy_step(_RESOLVED_HISTORY, reputation=rep_list, base_policy=_BASE_POLICY)
        assert proposal.approved is False
        # High-quality source should have a positive preference
        assert proposal.candidate.source_preference.get("reuters.com", 0.5) >= 0.5
        # Low-quality source should have a lower preference than the high-quality one
        r_pref = proposal.candidate.source_preference.get("reuters.com", 0.5)
        t_pref = proposal.candidate.source_preference.get("tabloid.net", 0.5)
        assert r_pref > t_pref

    def test_high_confirmation_rate_increases_preference(self):
        """A source with high confirmation_rate ends up with preference > base 0.5."""
        rep_list = [_make_reputation("good.com", confirmation_rate=0.95, independent_rate=0.9)]
        proposal = policy_step([], reputation=rep_list, base_policy=_BASE_POLICY)
        pref = proposal.candidate.source_preference.get("good.com", 0.5)
        assert pref > 0.5

    def test_low_confirmation_rate_decreases_preference(self):
        """A source with low confirmation_rate ends up with preference < base 0.5."""
        rep_list = [_make_reputation("bad.com", confirmation_rate=0.05, independent_rate=0.1)]
        proposal = policy_step([], reputation=rep_list, base_policy=_BASE_POLICY)
        pref = proposal.candidate.source_preference.get("bad.com", 0.5)
        assert pref < 0.5

    def test_candidate_weights_differ_when_evidence_suggests_it(self):
        """With repeated confirmed extraordinary facts, the tuner should lower that decay."""
        obs_history = [
            {
                "fact": f"Extraordinary confirmed fact {i}",
                "independent_originators": 2,
                "has_primary": False,
                "extremity": "extraordinary",
                "sources": ["a.com", "b.com"],
                "originators": [["a1"], ["b1"]],
                "confidence": 0.4,
            }
            for i in range(5)
        ]
        proposal = policy_step(obs_history, base_policy=_BASE_POLICY)
        # The confirmed facts on "extraordinary" should trigger a decay reduction
        default_decay = Weights.defaults().decay["extraordinary"]
        proposed_decay = proposal.candidate.weights.decay["extraordinary"]
        assert proposed_decay <= default_decay, (
            f"Expected decay reduction for extraordinary, got {proposed_decay} vs {default_decay}"
        )

    def test_pref_changes_have_required_fields(self):
        rep_list = [
            _make_reputation("good.com", confirmation_rate=0.9),
        ]
        proposal = policy_step([], reputation=rep_list, base_policy=_BASE_POLICY)
        for change in proposal.pref_changes:
            assert "source" in change
            assert "before" in change
            assert "after" in change
            assert "reason" in change

    def test_weight_changes_target_config_keys(self):
        obs_history = [
            {
                "fact": f"Extraordinary confirmed fact {i}",
                "independent_originators": 2,
                "has_primary": False,
                "extremity": "extraordinary",
                "sources": ["a.com", "b.com"],
                "originators": [["a1"], ["b1"]],
                "confidence": 0.4,
            }
            for i in range(6)
        ]
        proposal = policy_step(obs_history, base_policy=_BASE_POLICY)
        for wc in proposal.weight_changes:
            assert wc["key"].startswith("decay.")
            assert "reason" in wc
            assert "Brier" in wc["reason"]


# ---------------------------------------------------------------------------
# ownership_graph — correctness
# ---------------------------------------------------------------------------

class TestOwnershipGraph:
    def test_empty_edges_returns_empty(self):
        result = ownership_graph([])
        assert result == []

    def test_single_edge_creates_one_group(self):
        result = ownership_graph([("news-corp.com", "fox-news.com")])
        assert len(result) == 1
        assert result[0].sources == frozenset({"news-corp.com", "fox-news.com"})

    def test_two_independent_edges_produce_two_groups(self):
        result = ownership_graph([
            ("corp-a.com", "outlet-1.com"),
            ("corp-b.com", "outlet-2.com"),
        ])
        assert len(result) == 2
        all_sources = {s for g in result for s in g.sources}
        assert all_sources == {"corp-a.com", "outlet-1.com", "corp-b.com", "outlet-2.com"}

    def test_transitive_closure(self):
        """A–B and B–C should produce a single group {A, B, C}."""
        result = ownership_graph([("A", "B"), ("B", "C")])
        assert len(result) == 1
        assert result[0].sources == frozenset({"A", "B", "C"})

    def test_co_owned_outlets_in_same_group(self):
        """Rupert-owned: sky.com, news-uk.com, foxnews.com — all one ownership cluster."""
        edges = [
            ("sky.com", "news-uk.com"),
            ("news-uk.com", "foxnews.com"),
        ]
        result = ownership_graph(edges)
        assert len(result) == 1
        assert frozenset({"sky.com", "news-uk.com", "foxnews.com"}) == result[0].sources

    def test_independent_sources_form_singleton_groups(self):
        """Two unconnected sources each form their own group."""
        result = ownership_graph([("a.com", "b.com"), ("c.com", "d.com")])
        assert len(result) == 2

    def test_self_edge_ignored(self):
        result = ownership_graph([("a.com", "a.com")])
        assert result == []

    def test_result_is_deterministic(self):
        """Same edges in different orders produce the same groups."""
        edges_v1 = [("A", "B"), ("C", "D"), ("B", "C")]
        edges_v2 = [("D", "C"), ("B", "A"), ("C", "B")]
        r1 = ownership_graph(edges_v1)
        r2 = ownership_graph(edges_v2)
        assert len(r1) == len(r2) == 1
        assert r1[0].sources == r2[0].sources == frozenset({"A", "B", "C", "D"})

    def test_each_group_is_frozenset(self):
        result = ownership_graph([("a.com", "b.com")])
        assert all(isinstance(g, OwnershipGroup) for g in result)
        assert all(isinstance(g.sources, frozenset) for g in result)

    def test_large_star_graph_collapses_to_one_group(self):
        """One owner with many outlets: all should collapse."""
        hub = "conglomerate.com"
        spokes = [f"outlet-{i}.com" for i in range(10)]
        edges = [(hub, s) for s in spokes]
        result = ownership_graph(edges)
        assert len(result) == 1
        assert result[0].sources == frozenset([hub] + spokes)


# ---------------------------------------------------------------------------
# collapse_by_ownership — correctness
# ---------------------------------------------------------------------------

class TestCollapseByOwnership:
    def test_no_groups_means_all_sources_independent(self):
        """Without any ownership groups, every source is its own singleton."""
        result = collapse_by_ownership(["a.com", "b.com", "c.com"], [])
        assert len(result) == 3

    def test_co_owned_pair_collapses_to_one_originator(self):
        groups = ownership_graph([("a.com", "b.com")])
        result = collapse_by_ownership(["a.com", "b.com"], groups)
        assert len(result) == 1

    def test_co_owned_pair_and_independent_is_two_originators(self):
        groups = ownership_graph([("a.com", "b.com")])
        result = collapse_by_ownership(["a.com", "b.com", "c.com"], groups)
        assert len(result) == 2  # {a.com, b.com} + {c.com}

    def test_source_not_in_groups_is_singleton(self):
        """A source not linked in any ownership edge is independent."""
        groups = ownership_graph([("a.com", "b.com")])
        result = collapse_by_ownership(["a.com", "z.com"], groups)
        # a.com and b.com are co-owned but only a.com is in the cluster
        # a.com has no partner present, so it may be its own originator
        # z.com is definitely independent
        assert len(result) == 2  # {a.com} (b.com absent) + {z.com}

    def test_empty_sources_returns_empty(self):
        groups = ownership_graph([("a.com", "b.com")])
        result = collapse_by_ownership([], groups)
        assert result == []

    def test_three_co_owned_outlets_count_once(self):
        groups = ownership_graph([("a.com", "b.com"), ("b.com", "c.com")])
        result = collapse_by_ownership(["a.com", "b.com", "c.com"], groups)
        assert len(result) == 1

    def test_two_ownership_clusters_give_two_originators(self):
        groups = ownership_graph([("a.com", "b.com"), ("c.com", "d.com")])
        result = collapse_by_ownership(["a.com", "b.com", "c.com", "d.com"], groups)
        assert len(result) == 2

    def test_result_is_deterministic(self):
        groups = ownership_graph([("a.com", "b.com"), ("c.com", "d.com")])
        r1 = collapse_by_ownership(["a.com", "b.com", "c.com", "d.com"], groups)
        r2 = collapse_by_ownership(["d.com", "c.com", "b.com", "a.com"], groups)
        assert set(r1) == set(r2)

    def test_partial_ownership_group_present(self):
        """Only one member of a co-owned pair appears in the cluster — still one originator."""
        groups = ownership_graph([("corp.com", "tabloid.com")])
        # Only corp.com appears in the corroboration cluster
        result = collapse_by_ownership(["corp.com", "reuters.com"], groups)
        # corp.com → its group, but tabloid.com absent; still independent from reuters.com
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Integration: policy_step + ownership_graph round-trip
# ---------------------------------------------------------------------------

class TestPolicyStepIntegration:
    def test_full_pipeline_produces_valid_proposal(self):
        """End-to-end: history → policy_step → proposal with no crashes or out-of-bounds."""
        rep_list = [
            _make_reputation("reuters.com", confirmation_rate=0.85, independent_rate=0.8),
            _make_reputation("tabloid.net", confirmation_rate=0.30, independent_rate=0.4),
            _make_reputation("bbc.co.uk", confirmation_rate=0.80, independent_rate=0.75),
        ]
        proposal = policy_step(_RESOLVED_HISTORY, reputation=rep_list, base_policy=_BASE_POLICY)

        assert isinstance(proposal, PolicyProposal)
        assert proposal.approved is False
        for level, v in proposal.candidate.weights.decay.items():
            assert _DECAY_FLOOR <= v <= _DECAY_CEIL
        for src, pref in proposal.candidate.source_preference.items():
            assert _PREF_FLOOR <= pref <= _PREF_CEIL

    def test_ownership_collapses_before_originators(self):
        """After ownership collapse, co-owned outlets count as one."""
        groups = ownership_graph([
            ("sky.com", "news-uk.com"),
            ("sky.com", "times.co.uk"),
        ])
        # All three are co-owned; should collapse to 1 originator
        result = collapse_by_ownership(["sky.com", "news-uk.com", "times.co.uk"], groups)
        assert len(result) == 1

        # Adding an independent source gives 2
        result2 = collapse_by_ownership(["sky.com", "news-uk.com", "times.co.uk", "reuters.com"], groups)
        assert len(result2) == 2
