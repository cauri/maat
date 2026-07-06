"""Analyse — Maat's veracity core pointed at ONE pasted article, on demand (P14: #365, #366).

The feed reads a corpus; this reads one URL a person pastes. Fetch → extract claims → classify
(facts vs projections) → read each fact:

  * CORPUS-INHERIT — a claim Maat has already corroborated inherits that cluster's standing,
    FOLDED via ``corroborate_fixed`` so the pasted article itself counts as an originator — and a
    wire reprint collapses rather than double-counting (§5.5).
  * LIVE CORROBORATION — a novel claim goes out to the open web (the ``search`` seam): candidate
    articles are fetched, their claims extracted with the SAME extractor, same-fact matched at the
    SAME §5.4 bar, and folded with the SAME collapse — so on-demand evidence is weighed exactly
    like the feed's. A claim nobody else asserts stands as a single originator, weighted by its
    own attribution quality (§5.2).

Extremity is FIRST-CLASS in the result: the page exposes that Maat holds a routine claim and an
extraordinary claim to different bars (cauri, 2026-07-06). "Corroborated" wording is RESERVED for
claims with actual outside confirmation — a lone-source claim says so plainly, whatever its score.

Pure orchestration over injected seams (fetch, extract, classify, extremity, embed, corpus lookup,
live search, reputation) — fully testable offline. The serving layer (serving/analyse.py) wires
the real corpus, search, cache and endpoints. Analysed articles never enter the canonical store.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import numpy as np

from maat.acquire.fetch import FetchedPage, fetch_page
from maat.learning.article_credibility import ArticleClaim, score_article
from maat.learning.story_credibility import StoryScore
from maat.pipeline.claim import Claim
from maat.pipeline.classify import classify_claims
from maat.pipeline.corroborate import ClaimRow, confidence_label, corroborate_fixed
from maat.pipeline.extract import extract_claims
from maat.pipeline.extremity import rate_extremity
from maat.pipeline.identity import canonical_source
from maat.providers.seam import mistral_embed

_BIG = ("significant", "extraordinary")
_EXTREMITY_RANK = {"routine": 0, "ordinary": 1, "notable": 2, "significant": 3, "extraordinary": 4}
# article_id for the pasted article inside a scoped fold — never a real corpus id.
_ANALYSED = "analysed"


@dataclass(frozen=True)
class CorpusFact:
    """One existing Maat cluster, hydrated as a fold target for a matched claim.

    The serving layer assembles these from the projections (cluster + member claims + article
    bodies); tests build them by hand. ``embed_text`` is the English pivot when the fact is
    non-English (#240), else empty → match on ``fact``.
    """

    cluster_id: str
    fact: str
    extremity: str = "notable"
    grounding: str | None = None  # #228: "supported" | "not_addressed" | "contradicted"
    disputed: bool = False        # #229: refuted by a stronger contradicting claim
    member_claims: list[ClaimRow] = field(default_factory=list)
    bodies: dict[str, str] = field(default_factory=dict)  # member article_id -> body
    originator_sources: list[list[str]] = field(default_factory=list)  # source names per group
    embed_text: str = ""


@dataclass(frozen=True)
class LiveCandidate:
    """One article found on the open web that may corroborate (or fail to) a novel claim."""

    url: str
    domain: str
    title: str
    body: str


@dataclass(frozen=True)
class LiveMeta:
    """What the live pass actually covered — surfaced, never a silent cap."""

    searched_claims: int
    skipped_claims: int          # novel claims not searched (over the cap) — resolved as lone
    candidates_considered: int   # after dedupe/filters, before the total cap
    candidates_used: int         # actually fetched + claim-extracted


@dataclass(frozen=True)
class ClaimReading:
    """One analysed claim: the claim itself, its extremity (public — the bar it must clear),
    and how far it clears it."""

    claim: Claim
    extremity: str
    confidence: float
    independent_originators: int
    has_primary: bool
    disputed: bool
    grounding: str | None
    verdict: str
    tier: str
    matched_cluster_id: str | None  # internal provenance — never exposed publicly (what, not how)


@dataclass(frozen=True)
class ArticleAnalysis:
    """The full result for one pasted URL — the Analyse page renders exactly this."""

    url: str
    source: str          # publisher domain
    title: str | None
    language: str
    image: str | None
    date: str | None
    facts: list[ClaimReading]
    projections: list[ClaimReading]  # forecasts/opinions — shown, never scored as truth
    score: StoryScore                # the one overall read (band hero, number behind)
    publisher_score: float | None    # reputation_score() when rated; None = "not yet rated"
    live: LiveMeta | None = None     # live-corroboration coverage, when the pass ran


# Seam types. ``CorpusLookup`` returns, per claim text, a HYDRATED CorpusFact or None — the
# serving layer implements it as cached-embedding match + one bounded DB fetch for the matches.
CorpusLookup = Callable[[Sequence[str]], Sequence[CorpusFact | None]]
SearchFn = Callable[[str], list[LiveCandidate]]
ProgressFn = Callable[[str, dict[str, Any]], None]


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except ValueError:
        return ""


def _detect_language(text: str) -> str:
    try:  # lazy — ccnews sits next to WARC-stream machinery this path doesn't need
        from maat.acquire.ccnews import detect_lang
    except Exception:  # noqa: BLE001 - language is display metadata, never fatal
        return "unknown"
    return detect_lang(text)


def match_claims(
    texts: Sequence[str],
    corpus: Sequence[CorpusFact],
    *,
    embed: Callable[[list[str]], list[list[float]]] = mistral_embed,
    corpus_embeddings: np.ndarray | None = None,
    threshold: float = 0.82,
) -> list[int | None]:
    """Best same-fact corpus match per claim text (cosine ≥ threshold), else None.

    The §5.4 same-fact bar, applied claim→cluster-fact instead of claim↔claim — no clustering,
    just nearest-neighbour. ``corpus_embeddings`` (rows aligned with ``corpus``) lets the serving
    layer reuse a cached matrix instead of re-embedding the whole corpus per request."""
    if not texts or not corpus:
        return [None] * len(texts)
    if corpus_embeddings is None:
        vecs = np.asarray(
            embed([*texts, *[c.embed_text or c.fact for c in corpus]]), dtype=np.float64
        )
        q, k = vecs[: len(texts)], vecs[len(texts) :]
    else:
        q = np.asarray(embed(list(texts)), dtype=np.float64)
        k = np.asarray(corpus_embeddings, dtype=np.float64)

    sim = _unit(q) @ _unit(k).T
    out: list[int | None] = []
    for row in sim:
        best = int(np.argmax(row))
        out.append(best if row[best] >= threshold else None)
    return out


def _unit(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return x / norms


def claim_verdict(
    confidence: float,
    independent_originators: int,
    has_primary: bool,
    extremity: str,
    *,
    disputed: bool = False,
    grounding: str | None = None,
) -> tuple[str, str]:
    """Verdict wording for ONE analysed claim — (label, tier).

    Differs from the feed's ``confidence_label`` in one deliberate way (locked with cauri):
    "corroborated" is RESERVED for claims with actual outside confirmation. A lone-source claim
    says so plainly — the score is unchanged; only the words stop overpromising. The feed rarely
    surfaces lone claims (min_corroboration gates them), so this lives here, not there."""
    if disputed or grounding == "contradicted":
        return "Disputed — contradicted by stronger reporting", "floor"
    if independent_originators <= 1:
        if has_primary:
            return "Stated by the primary source", "mid"
        if extremity in _BIG:
            return f"Only this source — below the bar for a {extremity} claim", "floor"
        return "Only this source so far", ("mid" if confidence >= 0.60 else "lo")
    return confidence_label(
        confidence,
        independent_originators=independent_originators,
        has_primary=has_primary,
        extremity=extremity,
    )


def _corpus_reading(
    claim: Claim, body: str, source: str, match: CorpusFact,
    reputation: Mapping[str, float],
) -> tuple[ClaimReading, bool]:
    """Fold the pasted article into an existing cluster's read (reprints collapse, an independent
    report counts) and inherit the cluster's extremity/grounding/disputed standing."""
    row = ClaimRow(id=claim.id, text=claim.text, article_id=_ANALYSED, source=source)
    cor = corroborate_fixed(
        [*match.member_claims, row],
        {**match.bodies, _ANALYSED: body},
        match.extremity,
        grounding=match.grounding,
    )
    rated = source in reputation or any(
        s in reputation for grp in match.originator_sources for s in grp
    )
    verdict, tier = claim_verdict(
        cor.confidence, cor.independent_originators, cor.has_primary, match.extremity,
        disputed=match.disputed, grounding=match.grounding,
    )
    reading = ClaimReading(
        claim=claim, extremity=match.extremity, confidence=cor.confidence,
        independent_originators=cor.independent_originators, has_primary=cor.has_primary,
        disputed=match.disputed, grounding=match.grounding, verdict=verdict, tier=tier,
        matched_cluster_id=match.cluster_id,
    )
    return reading, rated


def _lone_reading(
    claim: Claim, body: str, source: str, extremity: str, reputation: Mapping[str, float]
) -> tuple[ClaimReading, bool]:
    """A claim with no outside evidence: a single originator, weighted by its own attribution
    quality (§5.2) — honest, never inflated."""
    row = ClaimRow(id=claim.id, text=claim.text, article_id=_ANALYSED, source=source)
    cor = corroborate_fixed([row], {_ANALYSED: body}, extremity)
    verdict, tier = claim_verdict(
        cor.confidence, cor.independent_originators, cor.has_primary, extremity
    )
    reading = ClaimReading(
        claim=claim, extremity=extremity, confidence=cor.confidence,
        independent_originators=cor.independent_originators, has_primary=cor.has_primary,
        disputed=False, grounding=None, verdict=verdict, tier=tier, matched_cluster_id=None,
    )
    return reading, source in reputation


def _live_reading(
    claim: Claim, body: str, source: str, extremity: str,
    matched_rows: list[ClaimRow], bodies: dict[str, str],
    reputation: Mapping[str, float],
) -> tuple[ClaimReading, bool]:
    """Fold live-found corroborating claims with the pasted article's own assertion — the same
    §5.5 collapse and §5.6 read the feed uses, over evidence found minutes ago."""
    own = ClaimRow(id=claim.id, text=claim.text, article_id=_ANALYSED, source=source)
    cor = corroborate_fixed([own, *matched_rows], {**bodies, _ANALYSED: body}, extremity)
    verdict, tier = claim_verdict(
        cor.confidence, cor.independent_originators, cor.has_primary, extremity
    )
    reading = ClaimReading(
        claim=claim, extremity=extremity, confidence=cor.confidence,
        independent_originators=cor.independent_originators, has_primary=cor.has_primary,
        disputed=False, grounding=None, verdict=verdict, tier=tier, matched_cluster_id=None,
    )
    rated = source in reputation or any(r.source in reputation for r in matched_rows)
    return reading, rated


def _gather_candidates(
    queries: list[str],
    search: SearchFn,
    *,
    own_source: str,
    own_url: str,
    accept: Callable[[LiveCandidate], bool] | None,
    max_total: int,
    workers: int,
) -> tuple[list[LiveCandidate], int]:
    """Run the live searches in parallel and merge to a deduped, filtered candidate pool.
    Returns (pool capped at ``max_total``, considered-count before the cap)."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda q: _safe_search(search, q), queries))
    own_canon = canonical_source(own_source)
    seen: set[str] = set()
    pool: list[LiveCandidate] = []
    for cands in results:
        for c in cands:
            if not c.body or c.url == own_url or c.url in seen:
                continue
            # The pasted outlet can never corroborate itself (§5.5) — skip before fetch cost.
            if canonical_source(c.domain) == own_canon:
                continue
            if accept is not None and not accept(c):
                continue
            seen.add(c.url)
            pool.append(c)
    return pool[:max_total], len(pool)


def _safe_search(search: SearchFn, query: str) -> list[LiveCandidate]:
    try:
        return search(query)
    except Exception:  # noqa: BLE001 - one failed search must not sink the analysis
        return []


def analyse_article(
    url: str,
    *,
    reputation: Mapping[str, float],
    corpus_lookup: CorpusLookup | None = None,
    search: SearchFn | None = None,
    accept_candidate: Callable[[LiveCandidate], bool] | None = None,
    fetch: Callable[[str], FetchedPage | None] = fetch_page,
    extract: Callable[..., list[Claim]] = extract_claims,
    classify: Callable[..., list[Claim]] = classify_claims,
    extremity_of: Callable[[str], str] = rate_extremity,
    embed: Callable[[list[str]], list[list[float]]] = mistral_embed,
    language_of: Callable[[str], str] = _detect_language,
    same_fact_threshold: float = 0.82,
    live_max_searches: int = 10,
    live_max_candidates: int = 18,
    max_workers: int = 4,
    progress: ProgressFn | None = None,
) -> ArticleAnalysis:
    """Analyse one pasted URL end-to-end. ``corpus_lookup`` inherits existing corroboration;
    ``search`` corroborates novel claims live; either may be None (that leg is skipped).

    Raises ValueError when no article can be extracted from the URL — the endpoint maps that to
    a clear 422, never a fake score."""

    def emit(kind: str, data: dict[str, Any]) -> None:
        if progress is not None:
            progress(kind, data)

    page = fetch(url)
    if page is None or not page.body:
        raise ValueError("could not extract an article from this URL")
    source = _domain(url)
    language = language_of(page.body)
    emit("fetched", {"title": page.title, "source": source, "language": language,
                     "date": page.date})

    claims = classify(
        extract(page.body, source_metadata=source, language=language), article_text=page.body
    )
    fact_claims = [c for c in claims if c.kind != "projection"]
    projections = [c for c in claims if c.kind == "projection"]
    emit("extracted", {
        "facts": [
            {"text": c.text, "voice": c.voice, "speaker": c.speaker,
             "central": c.in_headline or c.is_synthesis}
            for c in fact_claims
        ],
        "projections": [{"text": c.text, "speaker": c.speaker} for c in projections],
    })

    matches: Sequence[CorpusFact | None]
    matches = corpus_lookup([c.text for c in fact_claims]) if corpus_lookup else [None] * len(fact_claims)

    readings: list[ClaimReading | None] = [None] * len(fact_claims)
    rated_flags: list[bool] = [False] * len(fact_claims)
    novel_idx = [i for i, m in enumerate(matches) if m is None]
    emit("matched", {"matched": len(fact_claims) - len(novel_idx), "novel": len(novel_idx)})

    def resolve(i: int, pair: tuple[ClaimReading, bool]) -> None:
        readings[i], rated_flags[i] = pair
        emit("claim", {"index": i, "total": len(fact_claims), "reading": readings[i]})

    # Corpus-matched claims resolve instantly.
    for i, m in enumerate(matches):
        if m is not None:
            resolve(i, _corpus_reading(fact_claims[i], page.body, source, m, reputation))

    # Novel claims: rate extremity (needed for both the lone read and search priority)…
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        extremities = dict(zip(novel_idx, ex.map(
            lambda i: extremity_of(fact_claims[i].text), novel_idx
        )))

    live: LiveMeta | None = None
    if search is None or not novel_idx:
        for i in novel_idx:
            resolve(i, _lone_reading(fact_claims[i], page.body, source, extremities[i], reputation))
        if search is not None:
            live = LiveMeta(0, 0, 0, 0)
    else:
        # …then go out to the web for the ones that matter most: central claims first, then by
        # extremity (an extraordinary claim needs the evidence most), then article order.
        ranked = sorted(novel_idx, key=lambda i: (
            not (fact_claims[i].in_headline or fact_claims[i].is_synthesis),
            -_EXTREMITY_RANK.get(extremities[i], 2),
            i,
        ))
        selected, skipped = ranked[:live_max_searches], ranked[live_max_searches:]
        for i in skipped:  # over the search cap — resolved honestly as lone, and REPORTED (meta)
            resolve(i, _lone_reading(fact_claims[i], page.body, source, extremities[i], reputation))
        emit("searching", {"claims": len(selected)})

        pool, considered = _gather_candidates(
            [fact_claims[i].text[:200] for i in selected], search,
            own_source=source, own_url=url, accept=accept_candidate,
            max_total=live_max_candidates, workers=max_workers,
        )
        # Extract each candidate's claims with the SAME extractor the feed uses.
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            cand_claims = list(ex.map(lambda c: _safe_extract(extract, c), pool))
        rows: list[ClaimRow] = []
        bodies: dict[str, str] = {}
        for cand, texts in zip(pool, cand_claims):
            if not texts:
                continue
            bodies[cand.url] = cand.body
            rows.extend(
                ClaimRow(id=f"live-{len(rows) + n}", text=t, article_id=cand.url, source=cand.domain)
                for n, t in enumerate(texts)
            )

        # One batched same-fact pass: every selected claim vs every candidate claim (§5.4 bar).
        by_claim: dict[int, list[ClaimRow]] = {i: [] for i in selected}
        if rows:
            sel_texts = [fact_claims[i].text for i in selected]
            vecs = np.asarray(embed([*sel_texts, *[r.text for r in rows]]), dtype=np.float64)
            sim = _unit(vecs[: len(sel_texts)]) @ _unit(vecs[len(sel_texts):]).T
            for pos, i in enumerate(selected):
                by_claim[i] = [rows[j] for j in np.nonzero(sim[pos] >= same_fact_threshold)[0]]
        for i in selected:
            resolve(i, _live_reading(
                fact_claims[i], page.body, source, extremities[i],
                by_claim[i], bodies, reputation,
            ))
        live = LiveMeta(
            searched_claims=len(selected), skipped_claims=len(skipped),
            candidates_considered=considered, candidates_used=len(bodies),
        )

    done = [r for r in readings if r is not None]
    proj_readings = [
        ClaimReading(
            claim=c, extremity="", confidence=0.0, independent_originators=0, has_primary=False,
            disputed=False, grounding=None, verdict="Forecast / opinion — not scored for truth",
            tier="none", matched_cluster_id=None,
        )
        for c in projections
    ]
    score = score_article([
        ArticleClaim(
            confidence=r.confidence,
            independent_originators=r.independent_originators,
            has_primary=r.has_primary,
            extremity=r.extremity,
            central=r.claim.in_headline or r.claim.is_synthesis,
            disputed=r.disputed,
            grounding=r.grounding,
            rated_originator=rated,
        )
        for r, rated in zip(done, rated_flags)
    ])
    emit("scored", {"score": score.score, "band": score.band, "label": score.label})

    return ArticleAnalysis(
        url=url, source=source, title=page.title, language=language, image=page.image,
        date=page.date, facts=done, projections=proj_readings, score=score,
        publisher_score=reputation.get(source), live=live,
    )


def _safe_extract(extract: Callable[..., list[Claim]], cand: LiveCandidate) -> list[str]:
    """A candidate article's claim texts; a failed extraction drops the candidate, never the run."""
    try:
        return [c.text for c in extract(cand.body, source_metadata=cand.domain)]
    except Exception:  # noqa: BLE001
        return []
