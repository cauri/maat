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
