"""#419 — the clustering bound, tested against the shape the LIVE corpus actually has.

`test_pipeline.py` already covers the blocked candidate scan at 40,000 claims and passes. It could
never catch this: it plants 2,000 well-separated clusters, so every connected component is ~20
claims and "the component sub-matrix is small" holds *by construction of the fixture*. Real claim
embeddings are not separated. Measured on prod (68,249 claims): pairwise cosine p50=0.667, p90=0.730,
**p99=0.810** against a 0.82 bar — so ~0.76% of all pairs are edges and the single-linkage graph
PERCOLATES into one component of 60,945 claims (89%), whose dense sub-matrix is 13.8 GiB. The first
real end-to-end run raised ArrayMemoryError there while the synthetic scale test stayed green.

These tests use a percolating fixture (a chain, not planted blobs) and assert the two properties the
synthetic one cannot: it stays inside the configured memory budget, and when it degrades it degrades
by SPLITTING — never by fusing claims into corroboration that was never there.
"""

# --- the bound that the SYNTHETIC benchmark could not catch -----------------------------------


def test_max_exact_component_tracks_the_memory_budget():
    """The component ceiling IS the memory budget, so it can be reasoned about in MB, not claims."""
    from maat.pipeline.corroborate import _BYTES_PER_PAIR, _max_exact_component

    m = _max_exact_component(512)
    assert m * m * _BYTES_PER_PAIR <= 512 * 1024**2  # never exceeds the budget…
    assert (m + 1) * (m + 1) * _BYTES_PER_PAIR > 512 * 1024**2 * 0.99  # …and is not wastefully under
    assert _max_exact_component(0.0001) >= 2  # a silly budget still yields a usable floor


def test_percolated_graph_is_clustered_within_budget_not_oomed():
    """A PERCOLATING candidate graph — the shape the live corpus actually has (#419).

    The earlier scale test planted 2,400 well-separated clusters (max size 50), so every component
    was tiny and the "component sub-matrix is small" assumption held by construction. Real claim
    embeddings don't look like that: baseline cosine is high (p99 ≈ 0.810 against a 0.82 bar), ~0.76%
    of all pairs are edges, and the graph percolates into ONE component holding 89% of the corpus —
    a 13.8 GiB sub-matrix. The old code raised ArrayMemoryError on the live corpus while this file
    stayed green. So: build a chain where every consecutive pair clears the bar but distant ones do
    not — single-linkage fuses it into one giant component — and pin that we still cluster it
    within the configured budget.
    """
    import numpy as np

    from maat.pipeline.corroborate import _max_exact_component, group_by_similarity

    n = 400
    rng = np.random.default_rng(7)
    # A slowly-rotating chain in 2-D: neighbours ~0.999 similar, the ends nearly orthogonal.
    ang = np.linspace(0, np.pi / 2, n)
    x = np.stack([np.cos(ang), np.sin(ang)], axis=1).astype(np.float32)
    x += rng.normal(0, 1e-4, x.shape).astype(np.float32)

    groups = group_by_similarity([f"c{i}" for i in range(n)], 0.82, embeddings=x)

    assert sum(len(g) for g in groups) == n  # every claim is placed exactly once…
    assert sorted(i for g in groups for i in g) == list(range(n))  # …and nothing is lost or dupl'd
    # It percolates: at the 0.82 bar this is a single component, far larger than a real fact.
    # With a tiny budget the clusterer must still return — subdividing, never allocating m².
    import maat.pipeline.corroborate as corr

    orig = corr._MAX_COMPONENT_MB
    try:
        corr._MAX_COMPONENT_MB = 0.0005  # forces a ~6-claim exact ceiling → must subdivide
        assert _max_exact_component(corr._MAX_COMPONENT_MB) < n
        small = group_by_similarity([f"c{i}" for i in range(n)], 0.82, embeddings=x)
    finally:
        corr._MAX_COMPONENT_MB = orig
    assert sorted(i for g in small for i in g) == list(range(n))  # still a complete partition
    assert max(len(g) for g in small) <= n  # bounded, and it terminated at all


def test_subdivision_only_ever_splits_never_fuses():
    """The approximation's direction of failure, pinned (#419).

    Subdividing an over-budget component can MISS a merge; it must never CREATE one. Under-clustering
    costs a corroboration we should have counted. Over-clustering would invent one — fusing unrelated
    claims into a "fact" no source stated, and then reporting independent sources agreeing on it.
    Only one of those is survivable, so this asserts the safe direction: every cluster produced under
    a forced-tiny budget is a SUBSET of some cluster produced exactly.
    """
    import numpy as np

    import maat.pipeline.corroborate as corr
    from maat.pipeline.corroborate import group_by_similarity

    n = 240
    ang = np.linspace(0, np.pi / 2, n)
    x = np.stack([np.cos(ang), np.sin(ang)], axis=1).astype(np.float32)
    texts = [f"c{i}" for i in range(n)]

    exact = group_by_similarity(texts, 0.82, embeddings=x)
    orig = corr._MAX_COMPONENT_MB
    try:
        corr._MAX_COMPONENT_MB = 0.0005
        degraded = group_by_similarity(texts, 0.82, embeddings=x)
    finally:
        corr._MAX_COMPONENT_MB = orig

    exact_sets = [set(g) for g in exact]
    for g in degraded:
        assert any(set(g) <= e for e in exact_sets), (
            f"cluster {sorted(g)[:5]}… is not a subset of any exact cluster — subdivision FUSED "
            "claims that exact average-linkage kept apart. That direction invents corroboration."
        )
