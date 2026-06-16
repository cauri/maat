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

import numpy as np

from maat.pipeline.extremity import rate_extremity
from maat.pipeline.identity import canonical_source
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


def _agglomerate(sim, threshold: float) -> list[list[int]]:
    """Average-linkage (UPGMA) agglomerative clustering over a similarity matrix (§5.4).

    Repeatedly merge the two clusters with the highest MEAN cross-similarity, while that
    mean clears `threshold`. This is the fix for #20: single-linkage / connected components
    chains transitively — one mid-similarity bridge claim drags two otherwise unrelated
    stories into a single cluster — whereas a mean requirement won't merge groups joined by
    a lone bridge. DRAFT choice of linkage (average vs complete); revisit with cauri.

    Lance-Williams group-average update: when two clusters merge, the merged cluster's
    similarity to every other cluster is the size-weighted mean of the two — which is EXACTLY
    the mean of all cross pairs, so this is the same average linkage as before, but it never
    recomputes a cross-product. The merge step is O(n) (a vectorised row update) instead of the
    old O(|a|·|b|) cross-product, which is what made the previous version O(n^3) — it HUNG at
    ~1k claims, so corroborate deleted the clusters and never re-emitted them (the empty-feed
    bug). The per-merge global argmax is still ~O(n^2), but on numpy it clears the whole claim
    corpus (thousands of claims) in under a second. `sim` is a nested list (tests) or an ndarray
    (the live path); we copy it and never mutate the caller's.
    """
    s = np.array(sim, dtype=np.float64)  # copy — merges mutate this in place
    n = s.shape[0]
    if n <= 1:
        return [[0]] if n else []
    members: list[list[int] | None] = [[i] for i in range(n)]
    sizes = np.ones(n, dtype=np.float64)
    np.fill_diagonal(s, -np.inf)  # a cluster is never its own most-similar neighbour
    while True:
        flat = int(np.argmax(s))  # most-similar active pair (retired rows/cols are -inf)
        a, b = divmod(flat, n)
        if s[a, b] < threshold:
            break  # the best remaining mean is below the bar — done merging
        i, j = (a, b) if a < b else (b, a)  # fold the higher index into the lower (deterministic)
        # group-average recurrence: sim(i∪j, k) = (|i|·sim(i,k) + |j|·sim(j,k)) / (|i|+|j|)
        merged = (sizes[i] * s[i] + sizes[j] * s[j]) / (sizes[i] + sizes[j])
        s[i] = merged
        s[:, i] = merged  # keep the matrix symmetric
        s[i, i] = -np.inf
        sizes[i] += sizes[j]
        members[i].extend(members[j])  # type: ignore[union-attr]
        members[j] = None  # retire j
        s[j] = -np.inf
        s[:, j] = -np.inf
    return [m for m in members if m is not None]


def group_by_similarity(texts: list[str], threshold: float) -> list[list[int]]:
    """Cluster same-fact claims by embedding cosine, average-linkage (§5.4, fixes #20)."""
    if len(texts) <= 1:
        return [[0]] if texts else []
    x = np.asarray(mistral_embed(texts), dtype=np.float64)
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0  # a missing embedding (zero vector) must not divide by zero
    x /= norms
    sim = x @ x.T  # the full cosine matrix in one BLAS call (was an O(n^2·d) Python loop)
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
    article_ids: list[str], bodies: dict[str, str], sources: dict[str, str],
    lex_threshold: float = 0.40, *, ownership: dict[str, str] | None = None,
) -> list[list[int]]:
    """Collapse near-verbatim reprints (lexical) and citation cascades (explicit attribution)
    into single originator nodes. Independent articles on one event stay separate.

    Source identity (§6.7, #36): two articles whose sources resolve to the SAME canonical
    originator (Reuters / reuters.com / Thomson Reuters → "reuters") are one originator, not
    several — so wire-service reprints that differ only in source-string FORM no longer inflate
    the independent-originator count (and thus confidence)."""
    n = len(article_ids)
    if n <= 1:
        return [[0]] if n else []
    shingles = [_shingles(bodies[a]) for a in article_ids]
    # Canonicalise each source once (#36). The same_source check below compares canonical ids;
    # _cites still uses the RAW source string so a body that names the outlet in full is matched.
    canon = {a: canonical_source(sources[a]) for a in article_ids if sources.get(a) is not None}
    # Ownership (#41): operator `admin.source.grouped` assigns co-owned outlets a shared group
    # label (keyed by canonical source). Articles in the same ownership group are ONE originator
    # — a conglomerate's outlets must not count as several independent corroborators.
    owner = {a: ownership.get(canon[a]) for a in canon} if ownership else {}
    edges: list[tuple[int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            lexical = _jaccard(shingles[i], shingles[j]) >= lex_threshold
            cascade = _cites(bodies[article_ids[i]], sources[article_ids[j]]) or _cites(
                bodies[article_ids[j]], sources[article_ids[i]]
            )
            # An outlet is not independent of itself: articles from one source — variant
            # source-strings resolving to the same canonical originator (#36), or co-owned
            # outlets sharing an ownership group (#41) — are one originator, not several.
            c_i = canon.get(article_ids[i])
            same_source = c_i is not None and c_i == canon.get(article_ids[j])
            o_i = owner.get(article_ids[i])
            same_owner = o_i is not None and o_i == owner.get(article_ids[j])
            if lexical or cascade or same_source or same_owner:
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
# The acquired source is often a bare domain (e.g. "ecb.europa.eu"), which the name markers
# above miss. An issuer's own domain IS the primary source for its own release (#108): government
# / military domains in any country (a "gov" or "mil" label), and these intergovernmental
# bodies. Matched on domain labels, not substrings, so "governance.com" is not a false hit.
_PRIMARY_DOMAINS = (
    "europa.eu", "un.org", "imf.org", "worldbank.org", "bis.org", "oecd.org",
    "who.int", "nato.int", "icc-cpi.int", "wto.org",
)


def is_primary_source(source: str) -> bool:
    s = source.lower()
    if any(k in s for k in _PRIMARY_MARKERS):
        return True
    labels = set(s.split("."))
    return bool({"gov", "mil"} & labels) or s.endswith(_PRIMARY_DOMAINS)


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


# How much doubt each independent originator leaves, by the claim's prior (§5.6): a more
# extraordinary claim earns less from the same corroboration. Five-point scale; the bar was
# raised (cauri) so it takes a bit more corroboration to clear 0.80. ~originators to reach 0.80:
# routine ~2, ordinary ~2-3, notable ~3, significant ~4, extraordinary ~6. DRAFT — knobs to tune.
_DECAY = {
    "routine": 0.35,
    "ordinary": 0.45,
    "notable": 0.55,
    "significant": 0.66,
    "extraordinary": 0.76,
}

# A primary source closes this fraction of the remaining gap to certainty; confidence is capped
# below 1.0 — nothing is ever certain (§5.7). DRAFT — surfaced in the admin Config panel.
_PRIMARY_LIFT = 0.5
_CONFIDENCE_CAP = 0.97
# Primary-source grounding (#228): a primary that CONTRADICTS the claim multiplies the read down
# (a strong negative — the issuer's own record disputes it). DRAFT — surfaced for tuning.
_GROUNDING_CONTRADICTED_PENALTY = 0.4


def confidence_read(
    independent_originators: int, has_primary: bool, extremity: str = "notable", *,
    decay: dict[str, float] | None = None,
    primary_lift: float | None = None,
    cap: float | None = None,
    grounding: str | None = None,
) -> float:
    """The confidence read on a corroborated fact (§5.6-5.7) — DRAFT, review on return.

    Diminishing returns on independent corroboration (each further independent originator
    matters less), a primary source closes half the remaining gap, and the per-originator
    doubt is scaled by the claim's prior — an extraordinary claim needs more independent
    originators to reach the same confidence. Capped below certainty.

    `grounding` (#228) refines the primary lift once a primary-source check has run: "supported"
    earns the lift, "not_addressed" WITHHOLDS it (a primary that doesn't actually back the claim
    no longer inflates confidence), "contradicted" withholds it and multiplies the read down.
    None means no grounding signal — unchanged behaviour, so all existing callers are unaffected.

    The weights default to the live constants; passing `decay`/`primary_lift`/`cap` overrides
    them, so the calibration harness can score this exact function under a candidate weight-set
    (and a future live config-read can feed operator-set weights through the same seam).
    """
    d = (decay or _DECAY).get(extremity, 0.55)  # default to "notable" if unrecognised
    base = 1.0 - d ** max(0, independent_originators)
    # The primary lift is EARNED only when the primary actually backs the claim (#228).
    if has_primary and grounding not in ("not_addressed", "contradicted"):
        base += (1.0 - base) * (_PRIMARY_LIFT if primary_lift is None else primary_lift)
    if grounding == "contradicted":
        base *= _GROUNDING_CONTRADICTED_PENALTY
    return round(min(base, _CONFIDENCE_CAP if cap is None else cap), 2)


def confidence_label(
    conf: float,
    *,
    independent_originators: int | None = None,
    has_primary: bool | None = None,
    extremity: str | None = None,
) -> tuple[str, str]:
    """Gate-the-floor verdict (§5.7): a verbal label + colour tier for a confidence read.

    Strong reads get a positive verdict; for weak reads, when the cluster's signals are passed,
    the label NAMES the failure mode (cauri: be specific — single source, not just "thin") so a
    reader sees *why* it's weak. A bare call (conf only) returns the generic tiers, so existing
    callers/eval are unchanged. We always SHOW the claim and flag it — never hide. Cut-points
    and wording are DRAFT (cauri: start here, adjust on real data).
    """
    if conf >= 0.85:
        return ("Well corroborated", "hi")
    if conf >= 0.60:
        return ("Corroborated", "mid")
    tier = "floor" if conf < 0.40 else "lo"
    if independent_originators is not None:  # name the failure mode
        big = extremity in ("significant", "extraordinary")
        if independent_originators <= 1 and not has_primary:
            return ("Single source · extraordinary claim" if big else "Single source", tier)
        if big:
            return ("Not yet established · extraordinary claim", tier)
        return ("Thinly corroborated", tier)
    return ("Limited corroboration" if conf >= 0.40 else "Thinly sourced", tier)


def corroborate(
    claims: list[ClaimRow],
    bodies: dict[str, str],
    *,
    same_fact_threshold: float = 0.82,
    duplicate_source_threshold: float = 0.40,
    min_corroboration: int = 2,
    extremity_of: Callable[[str], str] = rate_extremity,
    ownership: dict[str, str] | None = None,
    decay: dict[str, float] | None = None,
    primary_lift: float | None = None,
    cap: float | None = None,
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
        groups_idx = collapse_originators(
            article_ids, bodies, art_source, duplicate_source_threshold, ownership=ownership
        )
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
                confidence=confidence_read(
                    eff, primary, extremity, decay=decay, primary_lift=primary_lift, cap=cap
                ),
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
    ownership: dict[str, str] | None = None,
    decay: dict[str, float] | None = None,
    primary_lift: float | None = None,
    cap: float | None = None,
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
        confidence=confidence_read(
                    eff, primary, extremity, decay=decay, primary_lift=primary_lift, cap=cap
                ),
    )
