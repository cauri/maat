"""Veracity-core invariants (verification gate, §5.4-5.7).

The properties that must hold for ANY input — the stand-in for the formal proofs Maat skipped
(Gamelan's gold standard: laws proved for all inputs, not just the golden fixtures). All over
pure functions — no LLM, no DB, fully deterministic — so they double as the determinism gate.
"""

import itertools

from maat.pipeline.corroborate import (
    _CONFIDENCE_CAP,
    _W_BALD,
    _W_NAMED,
    ClaimRow,
    _agglomerate,
    attribution_weight,
    collapse_originators,
    confidence_read,
    corroborate_fixed,
    effective_originators,
)

LEVELS = ("routine", "ordinary", "notable", "significant", "extraordinary")


def _is_partition(groups: list[list[int]], n: int) -> bool:
    """Every index 0..n-1 appears in exactly one group (disjoint + complete)."""
    return sorted(i for g in groups for i in g) == list(range(n))


# ---- §5.4 same-fact clustering (_agglomerate) ----


def test_agglomerate_always_partitions_the_indices():
    mats = [
        [[1.0]],
        [[1.0, 0.0], [0.0, 1.0]],
        [[1.0, 0.9, 0.1], [0.9, 1.0, 0.2], [0.1, 0.2, 1.0]],
    ]
    for sim in mats:
        for thr in (0.1, 0.5, 0.82, 0.95):
            assert _is_partition(_agglomerate(sim, thr), len(sim))


def test_agglomerate_is_order_independent():
    # the result partition must not depend on input ordering (permute rows+cols, map back)
    sim = [
        [1.0, 0.95, 0.1, 0.0],
        [0.95, 1.0, 0.0, 0.1],
        [0.1, 0.0, 1.0, 0.95],
        [0.0, 0.1, 0.95, 1.0],
    ]
    base = {frozenset(g) for g in _agglomerate(sim, 0.82)}
    for perm in itertools.permutations(range(4)):
        p = [[sim[perm[i]][perm[j]] for j in range(4)] for i in range(4)]
        got = {frozenset(perm[i] for i in g) for g in _agglomerate(p, 0.82)}
        assert got == base


def test_agglomerate_does_not_chain_through_a_single_bridge():
    # #20 regression: two tight pairs joined by ONE mid-similarity bridge must NOT merge —
    # average-linkage requires the MEAN cross-similarity to clear the bar, not a lone edge.
    sim = [
        [1.0, 0.95, 0.83, 0.0],
        [0.95, 1.0, 0.0, 0.0],
        [0.83, 0.0, 1.0, 0.95],
        [0.0, 0.0, 0.95, 1.0],
    ]
    assert {frozenset(g) for g in _agglomerate(sim, 0.82)} == {frozenset({0, 1}), frozenset({2, 3})}


# ---- §5.5 originator collapse ----


def _bundle(specs):
    ids = [s[0] for s in specs]
    return ids, {s[0]: s[2] for s in specs}, {s[0]: s[1] for s in specs}


def test_collapse_partitions_and_keeps_independents_separate():
    ids, bodies, sources = _bundle([
        ("a1", "Alpha News", "alpha beta gamma delta epsilon"),
        ("a2", "Beta Times", "zeta eta theta iota kappa"),
        ("a3", "Gamma Post", "lambda mu nu xi omicron"),
    ])
    groups = collapse_originators(ids, bodies, sources)
    assert _is_partition(groups, 3)
    assert len(groups) == 3  # distinct sources, disjoint wording, no citation → all independent


def test_collapse_merges_same_source():
    ids, bodies, sources = _bundle([
        ("a1", "Wire Co", "alpha beta gamma"),
        ("a2", "Wire Co", "totally different wording here"),  # same outlet → one originator
        ("a3", "Indie", "unrelated distinct lexicon entirely"),
    ])
    groups = collapse_originators(ids, bodies, sources)
    assert _is_partition(groups, 3)
    assert len(groups) == 2


def test_collapse_is_order_independent():
    specs = [
        ("a1", "Alpha", "alpha beta gamma delta"),
        ("a2", "Alpha", "another unrelated set of words"),  # same source as a1
        ("a3", "Beta", "independent distinct lexicon block"),
    ]
    base = None
    for perm in itertools.permutations(range(3)):
        ids = [specs[i][0] for i in perm]
        bodies = {specs[i][0]: specs[i][2] for i in perm}
        sources = {specs[i][0]: specs[i][1] for i in perm}
        norm = {frozenset(ids[k] for k in g) for g in collapse_originators(ids, bodies, sources)}
        if base is None:
            base = norm
        assert norm == base


# ---- §5.2 attribution weighting ----


def test_attribution_weight_stays_in_band():
    cases = [("", "Daily"), ("according to officials", "Daily"),
             ("sources said, on condition of anonymity", "Daily"), ("x", "Federal Reserve")]
    for body, src in cases:
        assert _W_BALD <= attribution_weight(body, src) <= _W_NAMED


def test_attribution_primary_full_bald_least():
    assert attribution_weight("anything at all", "ecb.europa.eu") == _W_NAMED  # issuer domain
    assert attribution_weight("", "Daily Blog") == _W_BALD  # no provenance at all


def test_effective_originators_bounded_by_group_count():
    groups = [["a1"], ["a2", "a3"], ["a4"]]
    bodies = {"a1": "according to the ministry statement", "a2": "sources said", "a3": "", "a4": ""}
    sources = {"a1": "Gov", "a2": "X", "a3": "Y", "a4": "Z"}
    eff = effective_originators(groups, bodies, sources)
    assert len(groups) * _W_BALD <= eff <= len(groups) * _W_NAMED


# ---- §5.6-5.7 confidence read ----


def test_confidence_read_is_bounded():
    for n in range(0, 12):
        for hp in (False, True):
            for e in LEVELS:
                assert 0.0 <= confidence_read(n, hp, e) <= _CONFIDENCE_CAP


def test_confidence_read_is_monotonic_in_corroboration():
    for hp in (False, True):
        for e in LEVELS:
            seq = [confidence_read(n, hp, e) for n in range(0, 10)]
            assert all(b >= a for a, b in zip(seq, seq[1:]))  # never falls with more corroboration


def test_confidence_read_primary_never_lowers():
    for n in range(0, 10):
        for e in LEVELS:
            assert confidence_read(n, True, e) >= confidence_read(n, False, e)


def test_confidence_read_respects_extremity_order():
    # for fixed corroboration, a more extraordinary prior never reads HIGHER than a tamer one
    for n in range(1, 10):
        for hp in (False, True):
            seq = [confidence_read(n, hp, e) for e in LEVELS]
            assert all(a >= b for a, b in zip(seq, seq[1:]))


# ---- determinism gate (pure pipeline) ----


def test_corroborate_fixed_is_deterministic():
    claims = [ClaimRow(f"c{i}", "Minister resigned", f"a{i}", s)
              for i, s in enumerate(["AFP", "Reuters", "Local Post"])]
    bodies = {"a0": "according to officials", "a1": "the minister announced", "a2": "staff confirmed"}
    r1 = corroborate_fixed(claims, bodies, "notable")
    r2 = corroborate_fixed(claims, bodies, "notable")
    assert r1 == r2  # same inputs ⇒ identical output (the event-sourcing replay contract)
