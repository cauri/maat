"""Contradiction detection — pure helpers (#229). No DB, no model: testable in isolation.

`nearest_pairs` is the cheap bi-encoder retrieval step (cosine top-k over claim embeddings) → the
candidate pairs the NLI cross-encoder then judges, so NLI runs on a shortlist, not all O(n²).
`arbitrate` decides, for a confident contradiction, which side a STRONGER cluster refutes — by
grounding first (a primary-supported fact beats an ungrounded one), then by a confidence margin —
or None when it is too close to call (record the contradiction, refute neither).
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import numpy as np

from maat import ids

# Only act on a contradiction at least this confident; below it the relation is recorded but inert.
CONTRADICTION_MIN_SCORE = 0.7
# A cluster must be at least this much more confident to win a same-grounding arbitration.
ARBITRATION_CONFIDENCE_MARGIN = 0.2

# How decisive each grounding verdict is for arbitration (a primary-supported fact outranks one a
# primary doesn't back, which outranks one a primary contradicts). None / "" = ungrounded (neutral).
_GROUNDING_RANK = {"supported": 2, None: 1, "": 1, "not_addressed": 0, "contradicted": -1}


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def pair_id(a: str, b: str, relation: str) -> str:
    """Stable stream_id for an unordered claim pair + relation (so re-runs dedup at the kernel)."""
    return ids.relation_id(a, b, relation)


# Rows of the similarity matrix computed at a time (#419) — bounds the transient block to
# block × n × 4 bytes and keeps peak memory independent of the corpus size.
_SIM_BLOCK = int(os.environ.get("MAAT_SIM_BLOCK", "512"))


def nearest_pairs(
    ids: Sequence[str], embeddings: Sequence[Sequence[float]], *, k: int = 10, min_sim: float = 0.5
) -> list[tuple[str, str]]:
    """Top-k cosine neighbours per item → unordered candidate pairs (a<b), deduped.

    The bi-encoder retrieval step: the NLI cross-encoder only judges these pairs. Pairs below
    `min_sim` are dropped (unrelated claims rarely contradict, and NLI on them is wasted spend).

    **Performance (#419).** This was a pure-Python O(n²·d) double loop with a full sort of all n-1
    neighbours PER ROW. At the live corpus (12,393 claims × 1024 dims) that is ~153M cosine
    computations — ~157 billion float ops in interpreter land — and it did not finish: it wedged at
    100% CPU for 6+ hours. Because the clock ran its steps serially with no timeout, that hang
    deadlocked every step after it AND every subsequent tick, which is how it stopped harvest and
    prevented corroborate from ever being retried. It is the same shape as the allocation that
    killed corroborate: an algorithm sized for the corpus of a year ago.

    Now: the cosines are one blocked BLAS matmul (bounded, never n×n materialised), and top-k uses
    argpartition (O(n) per row) instead of sorting all n-1. Same pairs out — for each item, its
    top-k neighbours at/above ``min_sim``."""
    n = len(ids)
    if n < 2:
        return []
    x = np.asarray(embeddings, dtype=np.float32)
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0  # a zero vector must not divide by zero (it just matches nothing)
    x = x / norms

    kk = min(k, n - 1)
    pairs: set[tuple[str, str]] = set()
    for start in range(0, n, max(1, _SIM_BLOCK)):
        stop = min(start + max(1, _SIM_BLOCK), n)
        sims = x[start:stop] @ x.T  # (block, n) — the only allocation, and it is bounded
        for local in range(stop - start):
            i = start + local
            row = sims[local]
            row[i] = -np.inf  # never a neighbour of itself
            # argpartition puts the kk largest at the end — O(n), no full sort
            for j in np.argpartition(row, -kk)[-kk:].tolist():
                if row[j] < min_sim:
                    continue
                a, b = sorted((ids[i], ids[j]))
                if a != b:
                    pairs.add((a, b))
        del sims
    return sorted(pairs)


def arbitrate(
    grounding_a: str | None, confidence_a: float,
    grounding_b: str | None, confidence_b: float,
    *, margin: float = ARBITRATION_CONFIDENCE_MARGIN,
) -> str | None:
    """Which side a contradiction REFUTES — "a" or "b" — or None when it's too close to call.

    Grounding decides first (a primary-supported fact beats an ungrounded/weaker one); on a tie, a
    clear confidence margin decides; otherwise None — the contradiction is recorded but neither side
    is refuted on the strength of the other (the arbitration cauri signed off, with grounding as the
    tiebreaker so a peer disagreement can't refute a primary-backed fact).
    """
    ra = _GROUNDING_RANK.get(grounding_a, 1)
    rb = _GROUNDING_RANK.get(grounding_b, 1)
    if ra != rb:
        return "a" if ra < rb else "b"
    if abs(confidence_a - confidence_b) >= margin:
        return "a" if confidence_a < confidence_b else "b"
    return None
