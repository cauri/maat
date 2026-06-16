"""Contradiction detection — pure helpers (#229). No DB, no model: testable in isolation.

`nearest_pairs` is the cheap bi-encoder retrieval step (cosine top-k over claim embeddings) → the
candidate pairs the NLI cross-encoder then judges, so NLI runs on a shortlist, not all O(n²).
`arbitrate` decides, for a confident contradiction, which side a STRONGER cluster refutes — by
grounding first (a primary-supported fact beats an ungrounded one), then by a confidence margin —
or None when it is too close to call (record the contradiction, refute neither).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

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
    key = "|".join([*sorted((a, b)), relation])
    return "rel-" + hashlib.sha1(key.encode()).hexdigest()[:20]


def nearest_pairs(
    ids: Sequence[str], embeddings: Sequence[Sequence[float]], *, k: int = 10, min_sim: float = 0.5
) -> list[tuple[str, str]]:
    """Top-k cosine neighbours per item → unordered candidate pairs (a<b), deduped.

    The bi-encoder retrieval step: the NLI cross-encoder only judges these pairs. Pairs below
    `min_sim` are dropped (unrelated claims rarely contradict, and NLI on them is wasted spend).
    """
    n = len(ids)
    pairs: set[tuple[str, str]] = set()
    for i in range(n):
        sims = sorted(
            ((_cosine(embeddings[i], embeddings[j]), j) for j in range(n) if j != i),
            reverse=True,
        )
        for sim, j in sims[:k]:
            if sim < min_sim:
                break
            a, b = sorted((ids[i], ids[j]))
            if a != b:
                pairs.add((a, b))
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
