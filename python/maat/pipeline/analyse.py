"""Analyse — Maat's veracity core pointed at ONE pasted article, on demand (P14: #365, #366).

The feed reads a corpus; this reads one URL a person pastes. Fetch → extract claims → classify
(facts vs projections) → read each fact against the corpus: a claim Maat has already corroborated
inherits that cluster's standing, FOLDED via ``corroborate_fixed`` so the pasted article itself
counts as an originator — and a wire reprint collapses rather than double-counting (§5.5). A novel
claim stands as a single originator, weighted by its own attribution quality (§5.2). Extremity is
FIRST-CLASS in the result: the page exposes that Maat holds a routine claim and an extraordinary
claim to different bars (cauri, 2026-07-06). "Corroborated" wording is RESERVED for claims with
actual outside confirmation — a lone-source claim says so plainly, whatever its score.

Pure orchestration over injected seams (fetch, extract, classify, extremity, embed, corpus,
reputation) — fully testable offline. The serving layer (slice 4) wires the real corpus, cache and
endpoint; live web corroboration for novel claims lands in slice 2 (#365). Analysed articles never
enter the canonical store.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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
from maat.providers.seam import mistral_embed

_BIG = ("significant", "extraordinary")
# article_id for the pasted article inside a scoped fold — never a real corpus id.
_ANALYSED = "analysed"


@dataclass(frozen=True)
class CorpusFact:
    """One existing Maat cluster, as a match target for the pasted article's claims.

    The serving layer assembles these from the projections (clusters + member claims + article
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

    def _unit(x: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(x, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return x / norms

    sim = _unit(q) @ _unit(k).T
    out: list[int | None] = []
    for row in sim:
        best = int(np.argmax(row))
        out.append(best if row[best] >= threshold else None)
    return out


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


def _read_claim(
    claim: Claim,
    body: str,
    source: str,
    match: CorpusFact | None,
    extremity_of: Callable[[str], str],
    reputation: Mapping[str, float],
) -> tuple[ClaimReading, bool]:
    """Read one factual claim → (reading, has_rated_originator). Matched claims fold the pasted
    article into the cluster (reprints collapse, an independent report counts); novel claims stand
    alone, weighted by the article's own attribution quality."""
    row = ClaimRow(id=claim.id, text=claim.text, article_id=_ANALYSED, source=source)
    if match is not None:
        cor = corroborate_fixed(
            [*match.member_claims, row],
            {**match.bodies, _ANALYSED: body},
            match.extremity,
            grounding=match.grounding,
        )
        extremity, grounding, disputed = match.extremity, match.grounding, match.disputed
        rated = source in reputation or any(
            s in reputation for grp in match.originator_sources for s in grp
        )
        matched_id = match.cluster_id
    else:
        extremity = extremity_of(claim.text)
        cor = corroborate_fixed([row], {_ANALYSED: body}, extremity)
        grounding, disputed, matched_id = None, False, None
        rated = source in reputation
    verdict, tier = claim_verdict(
        cor.confidence, cor.independent_originators, cor.has_primary, extremity,
        disputed=disputed, grounding=grounding,
    )
    reading = ClaimReading(
        claim=claim, extremity=extremity, confidence=cor.confidence,
        independent_originators=cor.independent_originators, has_primary=cor.has_primary,
        disputed=disputed, grounding=grounding, verdict=verdict, tier=tier,
        matched_cluster_id=matched_id,
    )
    return reading, rated


def analyse_article(
    url: str,
    *,
    corpus: Sequence[CorpusFact],
    reputation: Mapping[str, float],
    fetch: Callable[[str], FetchedPage | None] = fetch_page,
    extract: Callable[..., list[Claim]] = extract_claims,
    classify: Callable[..., list[Claim]] = classify_claims,
    extremity_of: Callable[[str], str] = rate_extremity,
    embed: Callable[[list[str]], list[list[float]]] = mistral_embed,
    corpus_embeddings: np.ndarray | None = None,
    language_of: Callable[[str], str] = _detect_language,
    same_fact_threshold: float = 0.82,
) -> ArticleAnalysis:
    """Analyse one pasted URL end-to-end (corpus-inherit; live corroboration is slice 2).

    Raises ValueError when no article can be extracted from the URL — the endpoint maps that to
    a clear 422, never a fake score."""
    page = fetch(url)
    if page is None or not page.body:
        raise ValueError("could not extract an article from this URL")
    source = _domain(url)
    language = language_of(page.body)
    claims = classify(
        extract(page.body, source_metadata=source, language=language), article_text=page.body
    )
    fact_claims = [c for c in claims if c.kind != "projection"]
    projections = [c for c in claims if c.kind == "projection"]

    matches = match_claims(
        [c.text for c in fact_claims], corpus,
        embed=embed, corpus_embeddings=corpus_embeddings, threshold=same_fact_threshold,
    )
    readings: list[ClaimReading] = []
    rated_flags: list[bool] = []
    for c, m in zip(fact_claims, matches):
        reading, rated = _read_claim(
            c, page.body, source, corpus[m] if m is not None else None, extremity_of, reputation
        )
        readings.append(reading)
        rated_flags.append(rated)

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
        for r, rated in zip(readings, rated_flags)
    ])

    return ArticleAnalysis(
        url=url, source=source, title=page.title, language=language, image=page.image,
        date=page.date, facts=readings, projections=proj_readings, score=score,
        publisher_score=reputation.get(source),
    )
