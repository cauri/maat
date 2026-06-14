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

import hashlib
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


def _agglomerate(sim: list[list[float]], threshold: float) -> list[list[int]]:
    """Average-linkage agglomerative clustering over a similarity matrix (§5.4).

    Repeatedly merge the two clusters with the highest MEAN cross-similarity, while that
    mean clears `threshold`. This is the fix for #20: single-linkage / connected components
    chains transitively — one mid-similarity bridge claim drags two otherwise unrelated
    stories into a single cluster — whereas a mean requirement won't merge groups joined by
    a lone bridge. DRAFT choice of linkage (average vs complete); revisit with cauri.

    O(n^3)-ish; fine at current corpus scale, revisit (Lance-Williams) for P2 volume.
    """
    n = len(sim)
    if n <= 1:
        return [[0]] if n else []
    clusters = [[i] for i in range(n)]
    while len(clusters) > 1:
        best, bi, bj = threshold, -1, -1
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                pairs = [sim[i][j] for i in clusters[a] for j in clusters[b]]
                avg = sum(pairs) / len(pairs)
                if avg >= best:
                    best, bi, bj = avg, a, b
        if bi == -1:
            break
        clusters[bi].extend(clusters[bj])
        del clusters[bj]
    return clusters


def group_by_similarity(texts: list[str], threshold: float) -> list[list[int]]:
    """Cluster same-fact claims by embedding cosine, average-linkage (§5.4, fixes #20)."""
    if len(texts) <= 1:
        return [[0]] if texts else []
    embs = mistral_embed(texts)
    n = len(texts)
    sim = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            sim[i][j] = sim[j][i] = _cosine(embs[i], embs[j])
    return _agglomerate(sim, threshold)


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
            # An outlet is not independent of itself: many articles from one source (one
            # domain publishing several pieces — exposed by real GDELT data) are one originator.
            s_i = sources.get(article_ids[i])
            same_source = s_i is not None and s_i == sources.get(article_ids[j])
            if lexical or cascade or same_source:
                edges.append((i, j))
    return _components(n, edges)


# Markers that the source IS the issuing body / a primary record, not an outlet relaying it.
# A stand-in for proper identity resolution (§6.7) — DRAFT, widened from real-data testing
# (it had missed central banks: the ECB is the primary source for its own rate decision).
# Deliberately excludes "agency" and "report" — news agencies (AFP, Reuters) are wire, not primary.
_PRIMARY_MARKERS = (
    "statement", "press release", "communiqué", "communique", "official",
    "ministry", "department", "commission", "authority", "regulator",
    "central bank", "reserve bank", "federal reserve",
    "document", "dataset", "filing", "transcript",
)


def is_primary_source(source: str) -> bool:
    s = source.lower()
    return any(k in s for k in _PRIMARY_MARKERS)


# §5.2 laundering — endorsement / dropped attribution. A good outlet states WHERE its
# information comes from (even "a source who declined to be named"). An article that asserts a
# claim with NO stated provenance is low-quality — we cannot tell independent reporting from
# laundered repetition — so it counts as LESS than a full independent originator (cauri's
# call). A primary source is its own provenance. DRAFT markers + weight; co-design with cauri.
_PROVENANCE_MARKERS = (
    "according to", "said", "says", "told", "reported", "reports", "stated", "stating",
    "announced", "confirm", "revealed", "cited", "citing", "alleged", "denied", "wrote",
    "source", "sources", "official", "spokesperson", "spokesman", "statement", "briefing",
    "documents", "filing", "study", "survey", "data show", "we found", "this paper",
    "our investigation", "has learned", "understands", "interview", '"', "“",
)
# Anonymous-but-stated sourcing — attributed, but to an unnamed source. Weighted between a
# named source and a bald assertion (cauri's gradient: named > anonymous > none).
_ANONYMOUS_MARKERS = (
    "sources said", "sources told", "sources say", "a source", "two sources", "three sources",
    "sources familiar", "people familiar", "person familiar", "people briefed", "person briefed",
    "people close to", "person close to", "officials said", "official said", "officials told",
    "on condition of anonymity", "speaking on condition", "declined to be named",
    "did not want to be named", "asked not to be named", "wished to remain", "anonymity",
    "insiders", "an insider", "a senior official", "people with knowledge",
)
_W_NAMED = 1.0      # primary source, or a named person / organisation / document
_W_ANONYMOUS = 0.6  # attributed, but to an unnamed source
_W_BALD = 0.3       # no attribution at all — stated in the outlet's own voice


def has_provenance(body: str) -> bool:
    """Does the article state where its information comes from (attribution / sourcing)?

    Lenient by design — we only want to flag a TRULY bald assertion (no attribution of any
    kind), since wrongly discounting a real report is worse than missing one launderer.
    """
    low = body.lower()
    return any(m in low for m in _PROVENANCE_MARKERS)


def _is_anonymous(body: str) -> bool:
    low = body.lower()
    return any(m in low for m in _ANONYMOUS_MARKERS)


def attribution_weight(body: str, source: str) -> float:
    """How much one article counts as an independent originator, by sourcing quality (§5.2):
    a primary or NAMED source counts fully; an ANONYMOUS but stated source counts less; a BALD
    assertion with no attribution counts least. (cauri: good outlets say where it came from;
    the more specific the attribution, the more it corroborates.) DRAFT tiers + weights."""
    if is_primary_source(source):
        return _W_NAMED
    if not has_provenance(body):
        return _W_BALD
    return _W_ANONYMOUS if _is_anonymous(body) else _W_NAMED


def effective_originators(
    groups: list[list[str]], bodies: dict[str, str], sources: dict[str, str]
) -> float:
    """Independent-originator count weighted by sourcing quality (§5.2). Each originator counts
    by its best-attributed article — a named/primary source fully, an anonymous source less, a
    bald assertion least — so spread behind weak sourcing adds little corroboration."""
    total = 0.0
    for g in groups:
        total += max(
            (attribution_weight(bodies.get(a, ""), sources.get(a, "")) for a in g), default=_W_BALD
        )
    return round(total, 2)


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


def confidence_label(conf: float) -> tuple[str, str]:
    """Gate-the-floor labelling (§5.7): a verbal verdict + colour tier for a confidence read.

    The bottom tier is the gate — below it a claim is flagged 'thinly sourced', not presented
    as established. DRAFT thresholds; co-design with cauri.
    """
    if conf >= 0.85:
        return ("Well corroborated", "hi")
    if conf >= 0.60:
        return ("Corroborated", "mid")
    if conf >= 0.40:
        return ("Limited corroboration", "lo")
    return ("Thinly sourced", "floor")


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
        eff = effective_originators(originators, bodies, art_source)
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
                confidence=confidence_read(eff, primary, extremity),
            )
        )
    results.sort(key=lambda r: r.independent_originators, reverse=True)
    return results


def cluster_id(claim_ids: list[str]) -> str:
    """Stable id for a cluster = hash of its member claim ids (matches the corroborate agent)."""
    return hashlib.sha1("|".join(sorted(claim_ids)).encode()).hexdigest()[:24]


def corroborate_fixed(
    claims: list[ClaimRow],
    bodies: dict[str, str],
    extremity: str = "notable",
    *,
    duplicate_source_threshold: float = 0.40,
) -> Corroboration:
    """Recompute ONE cluster over a FIXED claim set (operator-decided) — no same-fact
    re-clustering, no LLM. The admin console (P8 F3) uses this when an operator splits,
    merges, or moves claims: take the given claims AS a single cluster, collapse to
    independent originators (§5.5), and read confidence (§5.6-5.7). Extremity is carried
    over from the original cluster rather than re-rated — deterministic, free, testable.
    """
    if not claims:
        raise ValueError("corroborate_fixed needs at least one claim")
    art_source = {c.article_id: c.source for c in claims}
    article_ids = list(dict.fromkeys(c.article_id for c in claims))
    groups_idx = collapse_originators(article_ids, bodies, art_source, duplicate_source_threshold)
    originators = [[article_ids[i] for i in g] for g in groups_idx]
    ind = len(originators)
    eff = effective_originators(originators, bodies, art_source)
    primary = any(is_primary_source(s) for s in {c.source for c in claims})
    return Corroboration(
        fact=claims[0].text,
        claim_ids=[c.id for c in claims],
        sources=sorted({c.source for c in claims}),
        originators=originators,
        independent_originators=ind,
        has_primary=primary,
        extremity=extremity,
        confidence=confidence_read(eff, primary, extremity),
    )
