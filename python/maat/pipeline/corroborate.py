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

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

from maat import ids
from maat.pipeline.extremity import rate_extremity
from maat.pipeline.identity import canonical_source
from maat.providers.seam import mistral_embed


@dataclass
class ClaimRow:
    id: str
    text: str
    article_id: str
    source: str
    # Optional English pivot used ONLY for same-fact clustering (#240): a non-English claim
    # translated to English so a fact reported across languages clusters as one (mistral_embed is
    # multilingual but cross-lingual same-fact sits right at the 0.82 bar; the pivot lifts it
    # clear). Empty → cluster on `text` (identical to pre-#240 behaviour). Display/fact stay `text`.
    embed_text: str = ""


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


# Rows of the similarity matrix computed at a time (#417). Bounds the transient block to
# block × n × 4 bytes — at 512 × 68k that is ~139 MB, vs the 30.1 GiB the full matrix wanted.
_SIM_BLOCK = int(os.environ.get("MAAT_SIM_BLOCK", "512"))


def _candidate_pairs(x: np.ndarray, threshold: float, block: int) -> list[tuple[int, int]]:
    """Index pairs whose cosine clears ``threshold``, found WITHOUT ever materialising N×N (#417).

    Computed in row-blocks: each block is (block × n), so peak memory is bounded by ``block`` and
    independent of the corpus size. Only the upper triangle is kept, and only pairs at/above the
    bar survive — at a 0.82 bar that is a vanishingly small fraction of n², so the returned edge
    list is tiny even on a corpus where the dense matrix would be tens of gigabytes."""
    n = x.shape[0]
    edges: list[tuple[int, int]] = []
    for start in range(0, n, block):
        stop = min(start + block, n)
        sims = x[start:stop] @ x.T  # (block, n) — the ONLY allocation, and it is bounded
        hits = np.nonzero(sims >= threshold)
        for local, j in zip(hits[0].tolist(), hits[1].tolist()):
            i = start + local
            if j > i:  # upper triangle only; i == j is the self-match
                edges.append((i, j))
        del sims  # release the block before the next one is allocated
    return edges


# Memory ceiling for the exact dense agglomeration of ONE component (#419). A component of m
# claims costs m² × (4 B for the float32 cosine sub-matrix + 8 B for _agglomerate's float64
# working copy) — so this budget IS the component-size limit: m_max = sqrt(budget / 12).
_MAX_COMPONENT_MB = float(os.environ.get("MAAT_CLUSTER_MAX_MB", "512"))
_BYTES_PER_PAIR = 12
# When a component exceeds that, re-cut it at a stricter bar, in these steps, up to this ceiling.
_SUBDIVIDE_STEP = 0.02
_SUBDIVIDE_MAX = 0.98


def _max_exact_component(budget_mb: float = _MAX_COMPONENT_MB) -> int:
    """Largest component whose exact dense agglomeration fits the memory budget."""
    return max(2, int((budget_mb * 1024**2 / _BYTES_PER_PAIR) ** 0.5))


def _cluster_component(
    x: np.ndarray, idx: np.ndarray, threshold: float, *, bar: float | None = None
) -> list[list[int]]:
    """Exact average-linkage within one candidate component — subdividing if it cannot fit (#419).

    **Why this exists.** ``group_by_similarity``'s component step is exact, but exactness alone does
    not bound memory: it assumed a component is small because the bar is strict. Measured on the
    live corpus, that is false. In this embedding space the pairwise cosine distribution is
    ``p50=0.667, p90=0.730, p99=0.810`` — the 0.82 bar sits at about **p99.2**, so ~0.76% of ALL
    pairs are edges, and a graph that dense **percolates**: at 20k claims the largest component is
    15,794 (79% of the corpus), at 68,249 it is **60,945 (89%)**, with every other component tiny
    (62, 52, 46…). One giant hairball plus the real facts. The hairball's dense sub-matrix is
    **13.8 GiB** — so the exact computation is impossible on this box at any component granularity,
    and the original ``x @ x.T`` (30.1 GiB) was the same wall one step earlier.

    **What it does.** Components within budget are agglomerated EXACTLY, as before — that is the
    normal path and covers every real same-fact cluster. A component that cannot fit is re-cut at a
    stricter bar (+0.02 a step, to 0.98) and each piece recurses; raising the bar only ever removes
    edges, so the pieces shrink monotonically and this terminates.

    **This is an approximation, and it is deliberately the conservative one.** Re-cutting can miss a
    merge that average linkage would have made at the original bar across two pieces, so it
    **under-clusters: it can only split a fact, never fuse two.** Under-clustering costs a
    corroboration we should have counted; over-clustering would INVENT one — it would fuse unrelated
    claims and report independent sources agreeing on a "fact" nobody stated. Only one of those is
    survivable, so when the exact answer is unavailable this fails toward silence, not toward
    fabrication. It is never silent to the operator: each subdivision logs.
    """
    limit = _max_exact_component()
    m = len(idx)
    if m <= limit:
        sub = x[idx] @ x[idx].T  # bounded by `limit` — this is the exact path
        return [[int(idx[k]) for k in part] for part in _agglomerate(sub, threshold)]

    at = threshold if bar is None else bar
    nxt = round(min(at + _SUBDIVIDE_STEP, _SUBDIVIDE_MAX), 4)
    if nxt <= at:  # bar exhausted: m near-identical claims that still won't fit — chunk, bounded
        log.warning(
            "corroborate: component of %d claims still exceeds the %d-claim budget at the strictest "
            "bar (%.2f); splitting into fixed chunks — these claims are near-identical, so this is "
            "a floor on an already-degenerate group",
            m, limit, _SUBDIVIDE_MAX,
        )
        return [
            [int(v) for v in idx[s : s + limit]] for s in range(0, m, limit)
        ]

    parts = _components(m, _candidate_pairs(x[idx], nxt, max(1, _SIM_BLOCK)))
    log.warning(
        "corroborate: component of %d claims exceeds the exact budget (%d claims / %.0f MB); "
        "re-cut at bar %.2f into %d pieces (largest %d). This UNDER-clusters — a real merge may be "
        "missed — but never over-clusters.",
        m, limit, _MAX_COMPONENT_MB, nxt, len(parts), max((len(p) for p in parts), default=0),
    )
    out: list[list[int]] = []
    for p in parts:
        sel = idx[np.asarray(sorted(p))]
        if len(sel) == 1:
            out.append([int(sel[0])])
        else:
            out.extend(_cluster_component(x, sel, threshold, bar=nxt))
    return out


def group_by_similarity(
    texts: list[str], threshold: float, *, embeddings: np.ndarray | None = None
) -> list[list[int]]:
    """Cluster same-fact claims by embedding cosine, average-linkage (§5.4, fixes #20).

    ``embeddings`` (rows aligned with ``texts``) lets a caller pass vectors it already holds — the
    corroborate agent's chunked, cached reuse path (#286) — so Mistral is not re-queried. Omitted →
    embed the texts here (the small / direct-call path).

    **Memory (#417).** This used to be ``sim = x @ x.T`` — the full cosine matrix in one BLAS call.
    That is O(n²) memory: at 68,249 claims it asked for **30.1 GiB on an 8 GB box** and raised
    ArrayMemoryError on every tick, which killed the corroboration engine for 27 days. (The previous
    fix in this same function traded an O(n³) HANG for that allocation — time was re-checked, space
    was not.) The scale test was pinned at 1,000 claims — a 7.6 MiB matrix — so it could never fail.

    The partition is now computed EXACTLY, in bounded memory, via a property of average linkage:
    a merge needs the MEAN cross-similarity ≥ threshold, and **mean ≤ max**, so two claims can only
    ever end up together if some pair between them already clears the bar. Therefore:

      1. find the candidate pairs at/above the bar in row-blocks (never the full matrix);
      2. take connected components of that sparse graph — nothing outside a component can merge into
         it, because every cross pair is below the bar and so is every possible mean;
      3. run the exact dense agglomeration WITHIN each component (``_cluster_component``).

    Steps 1–2 are exact and bounded. Step 3 is exact but **not** bounded by them: the components are
    single-linkage, and single linkage chains. Measured on the live corpus, the candidate graph
    percolates — one component holds 89% of all claims (see ``_cluster_component``) — so "the
    sub-matrix is small" was wrong, and the honest bound is O(block × n) for the search plus a
    CONFIGURED ceiling for the agglomeration, which ``_cluster_component`` enforces by re-cutting an
    oversized component at a stricter bar. For every component that fits (all the real same-fact
    clusters), the answer is identical to the dense version, because merge order inside a component
    is unaffected by claims it can never merge with."""
    if len(texts) <= 1:
        return [[0]] if texts else []
    x = embeddings if embeddings is not None else mistral_embed(texts)
    # float32 halves the footprint and is far finer than the precision a 0.82 cosine bar needs.
    x = np.asarray(x, dtype=np.float32)
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0  # a missing embedding (zero vector) must not divide by zero
    x = x / norms  # NOT in-place: never mutate a caller-supplied array (the reuse cache, #286)

    n = x.shape[0]
    comps = _components(n, _candidate_pairs(x, threshold, max(1, _SIM_BLOCK)))
    out: list[list[int]] = []
    for comp in comps:
        if len(comp) == 1:
            out.append(comp)
            continue
        out.extend(_cluster_component(x, np.asarray(sorted(comp)), threshold))
    return out


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
# Domain-structural labels that carry NO originator identity — a source is very often a bare
# domain ("dw.com", "news.sky.com") on the live/feed paths, and its TLD/host noise must never be
# treated as a distinctive name token. Without this, the TLD "com" is a "significant token" of
# EVERY .com source, and since "com" is a substring of countless words (be·com·e, out·com·e) the
# citation-cascade check below fired between essentially any two domain sources — collapsing all
# independent originators into one (the "Only this source" bug, #381). ccTLDs and 2-letter labels
# are dropped by the length-≥3 token rule; these are the common multi-letter gTLDs/host bits.
_DOMAIN_STOP = {
    "com", "org", "net", "int", "edu", "gov", "mil", "info", "biz", "www", "co", "io",
}
_CASCADE_MARKERS = ("according to", "reported", "cited", " per ", "wrote", "citing")


def _significant_tokens(source: str) -> list[str]:
    """Distinctive name tokens of a source, for citation-cascade matching. Tokens must be ≥3
    letters (2-letter labels — ccTLDs like "uk"/"ru", "dw" — are too collision-prone to match by
    word) and neither generic news-words nor domain-structural labels (TLDs / "www")."""
    return [
        t for t in (m.lower() for m in re.findall(r"[A-Za-z]{3,}", source))
        if t not in _GENERIC and t not in _DOMAIN_STOP
    ]


def _cites(body: str, source: str) -> bool:
    """Does `body` explicitly attribute to `source` (a citation cascade)?

    A distinctive source token must appear as a WHOLE WORD — matching "sky" as a substring of
    "whiskey" (or the old bug, the TLD "com" inside "become") is not an attribution."""
    low = body.lower()
    if not any(m in low for m in _CASCADE_MARKERS):
        return False
    return any(re.search(rf"\b{re.escape(t)}\b", low) for t in _significant_tokens(source))


# ── shared-wire syndication (S6 #404) ─────────────────────────────────────────────────────────────
# Two outlets carrying the SAME agency copy are one originator, even when the rewrite falls under
# the lexical threshold and neither cites the OTHER outlet (the cascade check compares each body
# against the other article's SOURCE, so a shared third-party wire slips through pairwise).
# Detection is deliberately conservative — a bare mention ("the Reuters building") never collapses:
#   * an EXPLICIT credit: an agency dateline "(AP) —", a byline "By Jane Doe, Associated Press",
#     or a contribution line "The Associated Press contributed…"; or
#   * a citation-cascade ATTRIBUTION — the agency name sitting next to an attribution verb
#     ("according to Reuters" / "Reuters reported"), NOT a cascade marker merely present elsewhere.
# Short, collision-prone aliases (AP, PA, AFP, …: "PA" is also Pennsylvania) count ONLY in the
# explicit-credit shapes; distinctive names (reuters, bloomberg, …) may also match the cascade
# shape. Aliases fold to one wire id so "AP" and "Associated Press" are the same agency.
_WIRE_ALIASES: dict[str, str] = {
    "associated press": "ap", "ap": "ap",
    "reuters": "reuters",
    "agence france-presse": "afp", "agence france presse": "afp", "afp": "afp",
    "bloomberg": "bloomberg",
    "deutsche presse-agentur": "dpa", "dpa": "dpa",
    "press association": "pa", "pa media": "pa", "pa": "pa",
    "canadian press": "cp",
    "united press international": "upi", "upi": "upi",
    "kyodo": "kyodo", "xinhua": "xinhua", "interfax": "interfax",
    "tass": "tass", "ansa": "ansa", "efe": "efe",
}
# Aliases distinctive enough to count via the cascade shape ("according to Reuters …").
_WIRE_CASCADE_OK = frozenset(
    {"associated press", "reuters", "agence france-presse", "agence france presse", "bloomberg",
     "deutsche presse-agentur", "press association", "pa media", "canadian press",
     "united press international", "kyodo", "xinhua", "interfax"}
)
_WIRE_ALIAS_RX = "|".join(sorted((re.escape(a) for a in _WIRE_ALIASES), key=len, reverse=True))
_WIRE_EXPLICIT = re.compile(
    rf"\((?:{_WIRE_ALIAS_RX})\)"                                  # dateline "(AP)" / "(Reuters)"
    rf"|\bby [^.\n]{{0,80}}?,\s*(?:the\s+)?(?:{_WIRE_ALIAS_RX})\b"  # byline "By Jane Doe, AP"
    rf"|\b(?:the\s+)?(?:{_WIRE_ALIAS_RX})\s+contributed\b",         # "The AP contributed…"
    re.IGNORECASE,
)
# A citation-cascade CREDIT — the wire name ADJACENT to an attribution verb, in either order:
# "according to Reuters", "cited by AFP", "Reuters reported/said/wrote". The name and the verb must
# be next to each other; a bare mention where a cascade marker merely appears ELSEWHERE in the body
# ("the Reuters building … police reported three dead") must NOT match — the loose "marker anywhere
# AND name anywhere" gate collapsed independent originators on the shared feed path (review #1).
_WIRE_CASCADE_NAMES = "|".join(sorted((re.escape(a) for a in _WIRE_CASCADE_OK), key=len, reverse=True))
_WIRE_CASCADE_RX = re.compile(
    rf"(?:according to|cited by|reported by|citing|per)\s+(?:the\s+)?({_WIRE_CASCADE_NAMES})\b"
    rf"|\b({_WIRE_CASCADE_NAMES})\s+(?:reported|reports|said|says|wrote|writes|noted|notes|confirmed)\b",
    re.IGNORECASE,
)


def wire_credit(body: str) -> str | None:
    """The wire agency this article credits its copy to (a canonical wire id), or None. A credit is
    an EXPLICIT shape (dateline / byline / "X contributed") or a cascade ATTRIBUTION where the wire
    name sits next to an attribution verb ("according to Reuters" / "Reuters reported"). A bare
    mention of the agency is never a credit."""
    if not body:
        return None
    m = _WIRE_EXPLICIT.search(body)
    if m:
        hit = m.group(0).lower()
        for alias in sorted(_WIRE_ALIASES, key=len, reverse=True):
            if alias in hit:
                return _WIRE_ALIASES[alias]
    c = _WIRE_CASCADE_RX.search(body)
    if c:
        return _WIRE_ALIASES[(c.group(1) or c.group(2)).lower()]
    return None


def collapse_originators(
    article_ids: list[str], bodies: dict[str, str], sources: dict[str, str],
    lex_threshold: float = 0.40, *, ownership: dict[str, str] | None = None,
) -> list[list[int]]:
    """Collapse near-verbatim reprints (lexical), citation cascades (explicit attribution), and
    shared-wire pickups (both credit the same agency, S6 #404) into single originator nodes.
    Independent articles on one event stay separate.

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
    # Shared wire (S6 #404): articles crediting the SAME agency carry one originator's copy.
    wires = {a: wire_credit(bodies.get(a, "")) for a in article_ids}
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
            w_i = wires.get(article_ids[i])
            same_wire = w_i is not None and w_i == wires.get(article_ids[j])
            if lexical or cascade or same_source or same_owner or same_wire:
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
_W_OWN = 0.7        # the outlet's OWN-voice reporting (a real originator, but unattributed to an
                    # external source) in a piece that IS otherwise sourced — weaker than a named
                    # source, stronger than an anonymous one or a wholly-unsourced assertion (S2)
_W_ANONYMOUS = 0.6  # attributed, but to an unnamed source
_W_BALD = 0.3       # no attribution at all — an own-voice assertion in a provenance-free piece

# Collective / role speakers that name no actual source — "officials said", "sources familiar".
# An attribution to one of these is anonymous, not named (S2 #400).
_GENERIC_SPEAKERS = (
    "source", "official", "spokesperson", "spokesman", "spokeswoman", "insider", "analyst",
    "expert", "authorities", "witness", "people", "person", "someone", "observer", "aide",
)


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
    the more specific the attribution, the more it corroborates.) DRAFT tiers + weights.

    Body-scan form — used for OUTSIDE corroborating articles, whose per-claim voice we don't have.
    For the analysed article's OWN claim, use ``claim_attribution_weight`` (the claim's voice/speaker
    is a far better signal than a whole-body scan, which reads every claim at 1.0)."""
    if is_primary_source(source):
        return _W_NAMED
    if not has_provenance(body):
        return _W_BALD
    return _W_ANONYMOUS if _is_anonymous(body) else _W_NAMED


def _named_speaker(speaker: str | None) -> bool:
    """Is the attributed speaker an actual named source (a person / organisation), not a collective
    role like "officials" / "sources"?"""
    if not speaker or not speaker.strip():
        return False
    low = speaker.strip().lower()
    return not any(g in low for g in _GENERIC_SPEAKERS)


def claim_attribution_weight(
    voice: str, speaker: str | None, body: str, source: str, *, w_own: float | None = None,
) -> float:
    """The analysed claim's attribution weight from ITS OWN voice/speaker (S2 #400), not a whole-body
    scan — every article body has SOME provenance marker, so the scan read every claim at 1.0 and
    made lone claims indistinguishable (the flat 55/65). A claim ATTRIBUTED to a NAMED source counts
    fully, to an unnamed one less; an OWN-voice claim is the outlet's own reporting — a real
    originator (``_W_OWN``), weaker than a named external source, and only laundered-weak (``_W_BALD``)
    when the whole piece states no provenance at all."""
    if is_primary_source(source):
        return _W_NAMED
    if voice == "attributed":
        return _W_NAMED if _named_speaker(speaker) else _W_ANONYMOUS
    return (_W_OWN if w_own is None else w_own) if has_provenance(body) else _W_BALD


# §S1 (#399) — reputation of the corroborating outlet also scales its contribution: a proven-strong
# outlet is worth more than an unknown, an unknown more than a proven-weak one, WITH A FLOOR (cauri:
# continuous with a floor). Kept modest and cold-start-neutral: most sources on a young system are
# not yet rated, so an unrated source counts NEARLY-full — reputation is a bonus for a proven track
# record and a penalty for a proven-bad one, never a cliff that tanks the whole (young) corpus.
# DRAFT weights, tune on real data.
_REP_FLOOR = 0.5      # a PROVEN-unreliable outlet still counts this much (never zero)
_REP_UNRATED = 0.85   # no track record yet — cold-start neutral, a hair below a proven-strong outlet
#                       (gentle: most sources are unrated; only a PROVEN record earns full 1.0 or
#                       the floor). _REP_UNRATED / _REP_FLOOR are the two calibration knobs.


def _rep_score(reputation: dict[str, float], source: str) -> float | None:
    """Reputation lookup that survives source-string variants (§6.7): raw, then canonical — so
    bbc.co.uk finds a record stored under bbc.com / 'BBC News'. None → not yet rated."""
    hit = reputation.get(source)
    if hit is not None:
        return hit
    return reputation.get(canonical_source(source))


def reputation_weight(
    source: str, reputation: dict[str, float] | None,
    *, unrated: float | None = None, floor: float | None = None,
) -> float:
    """Continuous reputation multiplier on an originator's contribution (S1 #399), in
    [``_REP_FLOOR``, 1.0]. A proven-strong outlet → ~1.0; an unrated one → ``_REP_UNRATED``
    (cold-start neutral); a proven-weak one → the floor. ``None`` reputation map → 1.0 (feature
    off; existing callers unaffected). ``unrated``/``floor`` override the constants — the
    operator-promoted knob seam (#412), same pattern as ``confidence_read``'s decay/cap."""
    if reputation is None:
        return 1.0
    lo = _REP_FLOOR if floor is None else floor
    score = _rep_score(reputation, source)
    if score is None:
        return _REP_UNRATED if unrated is None else unrated
    return round(lo + (1.0 - lo) * max(0.0, min(1.0, score)), 2)


def effective_originators(
    groups: list[list[str]], bodies: dict[str, str], sources: dict[str, str],
    *, reputation: dict[str, float] | None = None, attribution: dict[str, float] | None = None,
    rep_unrated: float | None = None, rep_floor: float | None = None,
) -> float:
    """Independent-originator count weighted by sourcing quality (§5.2) AND, when a ``reputation``
    map is supplied, by each originator's track record (S1 #399). Each originator counts by its
    best-attributed article — a named/primary source fully, an anonymous source less, a bald
    assertion least — then that is scaled by the originator's reputation weight (a proven outlet
    counts more than an unknown, floored so unknowns still count). So spread behind weak sourcing
    OR unproven outlets adds little corroboration. ``reputation=None`` → attribution-only (unchanged
    for every existing caller).

    ``attribution`` (S2 #400) overrides the body-scan for specific article_ids with a precomputed
    weight — the analyse path passes the pasted article's CLAIM-aware weight (its voice/speaker),
    which the whole-body scan cannot see. Articles absent from the map fall back to the body scan."""
    def _attrib(a: str) -> float:
        if attribution is not None and a in attribution:
            return attribution[a]
        return attribution_weight(bodies.get(a, ""), sources.get(a, ""))

    def _joint(a: str) -> float:  # one article's (attribution × reputation) — the two weights
        return _attrib(a) * reputation_weight(  # of the SAME article, never mixed across the group
            sources.get(a, ""), reputation, unrated=rep_unrated, floor=rep_floor
        )

    total = 0.0
    for g in groups:
        # A collapsed originator counts by its BEST single article's joint weight — not max
        # attribution × max reputation independently (which could credit one member's attribution
        # with another member's reputation, over-crediting the group; review #3).
        total += max((_joint(a) for a in g), default=_W_BALD)
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
    embeddings: np.ndarray | None = None,
) -> list[Corroboration]:
    """Cluster same-fact claims; count independent originators per cluster (§5.5).

    ``embeddings`` (rows aligned with ``claims``, same order) are reused for the same-fact clustering
    instead of re-embedding — the corroborate agent supplies them from the persistent cache (#286).
    """
    if not claims:
        return []
    art_source = {c.article_id: c.source for c in claims}
    # Cluster on the English pivot when present (#240, cross-lingual), else the original text.
    clusters = group_by_similarity(
        [(c.embed_text or c.text) for c in claims], same_fact_threshold, embeddings=embeddings
    )
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
    """Stable id for a cluster = hash of its member claim ids. Canonical impl in maat.ids (#289)."""
    return ids.cluster_id(claim_ids)


def corroborate_fixed(
    claims: list[ClaimRow],
    bodies: dict[str, str],
    extremity: str = "notable",
    *,
    duplicate_source_threshold: float = 0.40,
    ownership: dict[str, str] | None = None,
    reputation: dict[str, float] | None = None,
    attribution: dict[str, float] | None = None,
    rep_unrated: float | None = None,
    rep_floor: float | None = None,
    decay: dict[str, float] | None = None,
    primary_lift: float | None = None,
    cap: float | None = None,
    grounding: str | None = None,
) -> Corroboration:
    """Recompute ONE cluster over a FIXED claim set (operator-decided) — no same-fact
    re-clustering, no LLM. The admin console (P8 F3) uses this when an operator splits,
    merges, or moves claims: take the given claims AS a single cluster, collapse to
    independent originators (§5.5), and read confidence (§5.6-5.7). Extremity is carried
    over from the original cluster rather than re-rated — deterministic, free, testable.
    ``grounding`` (#228) likewise carries a cluster's existing grounding verdict through to
    ``confidence_read`` — the Analyse surface (P14) folds a pasted article into a cluster's read.
    ``reputation`` (S1 #399): when supplied, each originator's contribution is scaled by its track
    record — corroboration by established outlets weighs more than by unknowns. None → unchanged.
    ``attribution`` (S2 #400): per-article_id weight overrides for the sourcing scan — the analyse
    path passes the pasted article's CLAIM-aware attribution (voice/speaker). None → body-scan.
    """
    if not claims:
        raise ValueError("corroborate_fixed needs at least one claim")
    art_source = {c.article_id: c.source for c in claims}
    article_ids = list(dict.fromkeys(c.article_id for c in claims))
    groups_idx = collapse_originators(
        article_ids, bodies, art_source, duplicate_source_threshold, ownership=ownership
    )
    originators = [[article_ids[i] for i in g] for g in groups_idx]
    ind = len(originators)
    eff = effective_originators(
        originators, bodies, art_source, reputation=reputation, attribution=attribution,
        rep_unrated=rep_unrated, rep_floor=rep_floor,
    )
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
                    eff, primary, extremity, decay=decay, primary_lift=primary_lift, cap=cap,
                    grounding=grounding,
                ),
    )
