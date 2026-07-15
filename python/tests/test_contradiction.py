"""Automated contradiction detection (#229) — pure retrieval + arbitration; NLI seam is mockable."""

from __future__ import annotations

from maat.pipeline import nli
from maat.pipeline.contradiction import arbitrate, nearest_pairs, pair_id


# --- bi-encoder retrieval (the cheap candidate step) -------------------------------------


def test_nearest_pairs_links_close_drops_far():
    ids = ["a", "b", "c"]
    embs = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]]  # a,b near; c orthogonal
    pairs = nearest_pairs(ids, embs, k=2, min_sim=0.5)
    assert ("a", "b") in pairs
    assert ("a", "c") not in pairs and ("b", "c") not in pairs


def test_nearest_pairs_unordered_and_deduped():
    assert nearest_pairs(["x", "y"], [[1.0, 0.0], [1.0, 0.0]], k=5) == [("x", "y")]


def test_pair_id_stable_and_order_independent():
    assert pair_id("a", "b", "contradicts") == pair_id("b", "a", "contradicts")
    assert pair_id("a", "b", "contradicts") != pair_id("a", "b", "entails")
    assert pair_id("a", "b", "contradicts").startswith("rel-")


# --- arbitration (which side a contradiction refutes) ------------------------------------


def test_arbitrate_grounding_beats_ungrounded():
    # The primary-supported fact wins regardless of the other's confidence — grounding is the tiebreaker.
    assert arbitrate("supported", 0.5, None, 0.9) == "b"
    assert arbitrate(None, 0.9, "supported", 0.5) == "a"


def test_arbitrate_confidence_margin_when_grounding_ties():
    assert arbitrate(None, 0.9, None, 0.6) == "b"  # clearly less confident loses
    assert arbitrate(None, 0.6, None, 0.9) == "a"


def test_arbitrate_none_when_too_close():
    assert arbitrate(None, 0.70, None, 0.72) is None  # within margin, same grounding → refute neither


def test_arbitrate_contradicted_grounding_loses_to_supported():
    assert arbitrate("supported", 0.5, "contradicted", 0.9) == "b"


# --- the NLI seam: inert (None) until a model is configured + verified -------------------


def test_nli_seam_inert_without_a_model():
    assert nli.available() is False
    assert nli.classify_pair("the ECB raised rates 50bp", "the ECB cut rates") is None


def test_nearest_pairs_is_fast_and_bounded_at_the_corpus_that_hung_it():
    """#419 — the regression test for the 6-hour hang.

    `nearest_pairs` was a pure-Python O(n²·d) double loop with a FULL SORT of all n-1 neighbours per
    row. At the live corpus (12,393 claims × 1024 dims) that is ~153M cosine computations in
    interpreter land; it ran 6h+ at 100% CPU and never finished. Because the clock ran steps
    serially with NO timeout, that hang deadlocked every later step and every subsequent tick — it
    is why harvest stopped and why corroborate was never retried.

    Asserts what actually broke: it completes, quickly, in bounded memory, at that exact size."""
    import resource
    import sys
    import time

    import numpy as np

    from maat.pipeline.contradiction import nearest_pairs

    n, d = 12_393, 256  # the corpus size that hung it
    rng = np.random.default_rng(9)
    x = rng.normal(size=(n, d)).astype(np.float32)
    ids = [f"c{i}" for i in range(n)]

    unit = 1024**3 if sys.platform == "darwin" else 1024**2  # maxrss: bytes on macOS, KB on linux
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / unit
    t0 = time.time()
    pairs = nearest_pairs(ids, x, k=10, min_sim=0.5)
    secs = time.time() - t0
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / unit

    assert secs < 60, f"took {secs:.0f}s — the O(n^2) python scan is back"
    assert peak - before < 2.0, f"memory grew {peak - before:.2f} GB — an n×n alloc is back"
    assert isinstance(pairs, list)


def test_nearest_pairs_matches_the_original_semantics():
    """#419 — the rewrite must return exactly what the old scan did: each item's top-k neighbours
    at/above min_sim, as deduped unordered pairs."""
    import numpy as np

    from maat.pipeline.contradiction import _cosine, nearest_pairs

    def original(ids, emb, *, k, min_sim):
        n = len(ids)
        pairs = set()
        for i in range(n):
            sims = sorted(((_cosine(emb[i], emb[j]), j) for j in range(n) if j != i), reverse=True)
            for sim, j in sims[:k]:
                if sim < min_sim:
                    break
                a, b = sorted((ids[i], ids[j]))
                if a != b:
                    pairs.add((a, b))
        return sorted(pairs)

    rng = np.random.default_rng(4)
    for _ in range(8):
        n = int(rng.integers(2, 30))
        x = rng.normal(size=(n, 12))
        ids = [f"c{i}" for i in range(n)]
        for k, ms in ((5, 0.5), (10, 0.3), (3, 0.7)):
            assert nearest_pairs(ids, x, k=k, min_sim=ms) == original(ids, x.tolist(), k=k, min_sim=ms)
