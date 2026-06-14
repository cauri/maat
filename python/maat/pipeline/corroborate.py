"""Corroboration (BRIEF §5.4-5.5) — the heart of the product.

Cluster same-fact claims, then collapse the sources behind a fact to INDEPENDENT
ORIGINATORS. Spread counts for almost nothing; independent corroboration is what bears on
whether a claim holds. Two collapse signals, by design:
  - wire syndication / near-verbatim reprints -> LEXICAL near-duplication (word-shingle
    Jaccard): they share the same words;
  - citation cascades ("according to AFP") -> EXPLICIT attribution to another originator.
Same-fact CLUSTERING (§5.4) uses semantic embeddings; collapse (§5.5) does NOT — two
independent articles on one event are semantically alike but lexically distinct.

DRAFT — review on return. Thresholds, the source-name matching (a stand-in for proper
identity resolution, §6.7), and primary-source detection are first cuts.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from maat.pipeline.extremity import rate_extremity
from maat.providers.seam import mistral_embed


@dataclass
class ClaimRow:
    id: str
    text: str
    article_id: str
    source: str


@dataclass
class Corroboration:
    fact: str
    claim_ids: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    originators: list[list[str]] = field(default_factory=list)  # each inner list = one collapsed originator
    independent_originators: int = 0
    has_primary: bool = False
    extremity: str = "notable"
    confidence: float = 0.0


def _components(n: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


# --- same-fact clustering (§5.4): semantic ---


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def group_by_similarity(texts: list[str], threshold: float) -> list[list[int]]:
    """Connected components where cosine(emb_i, emb_j) >= threshold (semantic same-fact)."""
    if len(texts) <= 1:
        return [[0]] if texts else []
    embs = mistral_embed(texts)
    edges = [
        (i, j)
        for i in range(len(texts))
        for j in range(i + 1, len(texts))
        if _cosine(embs[i], embs[j]) >= threshold
    ]
    return _components(len(texts), edges)


# --- originator collapse (§5.5): lexical near-duplication + citation cascade ---


def _shingles(text: str, k: int = 4) -> set[str]:
    words = text.lower().split()
    if len(words) < k:
        return {" ".join(words)}
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_GENERIC = {
    "the", "of", "and", "a", "an", "news", "press", "times", "daily", "post", "herald",
    "ministry", "official", "statement", "finance", "media", "agency", "group", "valoria",
}
_CASCADE_MARKERS = ("according to", "reported", "cited", " per ", "wrote", "citing")


def _significant_tokens(source: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z]{2,}", source) if t.lower() not in _GENERIC]


def _cites(body: str, source: str) -> bool:
    """Does `body` explicitly attribute to `source` (a citation cascade)?"""
    low = body.lower()
    if not any(m in low for m in _CASCADE_MARKERS):
        return False
    return any(t.lower() in low for t in _significant_tokens(source))


def collapse_originators(
    article_ids: list[str], bodies: dict[str, str], sources: dict[str, str], lex_threshold: float = 0.40
) -> list[list[int]]:
    """Collapse near-verbatim reprints (lexical) and citation cascades (explicit attribution)
    into single originator nodes. Independent articles on one event stay separate."""
    n = len(article_ids)
    if n <= 1:
        return [[0]] if n else []
    shingles = [_shingles(bodies[a]) for a in article_ids]
    edges: list[tuple[int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            lexical = _jaccard(shingles[i], shingles[j]) >= lex_threshold
            cascade = _cites(bodies[article_ids[i]], sources[article_ids[j]]) or _cites(
                bodies[article_ids[j]], sources[article_ids[i]]
            )
            if lexical or cascade:
                edges.append((i, j))
    return _components(n, edges)


def is_primary_source(source: str) -> bool:
    s = source.lower()
    return any(k in s for k in ("statement", "ministry", "document", "dataset", "filing", "official"))


# How much doubt each independent originator leaves, by the claim's prior (§5.6): an
# extraordinary claim earns less from the same corroboration than an ordinary one.
_DECAY = {"ordinary": 0.40, "notable": 0.50, "extraordinary": 0.68}


def confidence_read(
    independent_originators: int, has_primary: bool, extremity: str = "notable"
) -> float:
    """The confidence read on a corroborated fact (§5.6-5.7) — DRAFT, review on return.

    Diminishing returns on independent corroboration (each further independent originator
    matters less), a primary source closes half the remaining gap, and the per-originator
    doubt is scaled by the claim's prior — an extraordinary claim needs more independent
    originators to reach the same confidence. Capped below certainty.
    """
    decay = _DECAY.get(extremity, 0.50)
    base = 1.0 - decay ** max(0, independent_originators)
    if has_primary:
        base += (1.0 - base) * 0.5
    return round(min(base, 0.97), 2)


def corroborate(
    claims: list[ClaimRow],
    bodies: dict[str, str],
    *,
    same_fact_threshold: float = 0.82,
    duplicate_source_threshold: float = 0.40,
    min_corroboration: int = 2,
    extremity_of: Callable[[str], str] = rate_extremity,
) -> list[Corroboration]:
    """Cluster same-fact claims; count independent originators per cluster (§5.5)."""
    if not claims:
        return []
    art_source = {c.article_id: c.source for c in claims}
    clusters = group_by_similarity([c.text for c in claims], same_fact_threshold)
    results: list[Corroboration] = []
    for comp in clusters:
        members = [claims[i] for i in comp]
        if len(members) < min_corroboration:
            continue  # uncorroborated — not a corroboration cluster
        article_ids = list(dict.fromkeys(m.article_id for m in members))
        groups_idx = collapse_originators(article_ids, bodies, art_source, duplicate_source_threshold)
        originators = [[article_ids[i] for i in g] for g in groups_idx]
        ind = len(originators)
        primary = any(is_primary_source(s) for s in {m.source for m in members})
        extremity = extremity_of(members[0].text)
        results.append(
            Corroboration(
                fact=members[0].text,
                claim_ids=[m.id for m in members],
                sources=sorted({m.source for m in members}),
                originators=originators,
                independent_originators=ind,
                has_primary=primary,
                extremity=extremity,
                confidence=confidence_read(ind, primary, extremity),
            )
        )
    results.sort(key=lambda r: r.independent_originators, reverse=True)
    return results
