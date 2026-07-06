"""Analyse — Maat's veracity core pointed at ONE pasted article, on demand (P14: #365, #366).

The feed reads a corpus; this reads one URL a person pastes. Fetch → gate (news, not junk) →
sanitise → extract claims → verify each claim quotes the page → classify (facts vs projections)
→ read each fact:

  * CORPUS-INHERIT — a claim Maat has already corroborated inherits that cluster's standing,
    FOLDED via ``corroborate_fixed`` so the pasted article itself counts as an originator — and a
    wire reprint collapses rather than double-counting (§5.5).
  * LIVE CORROBORATION — a novel claim goes out to the open web (the ``search`` seam), each claim
    independently and in parallel so its chip resolves the moment ITS evidence is in: candidates
    fetched, their claims extracted with the SAME extractor, same-fact matched at the SAME §5.4
    bar, folded with the SAME collapse. A claim nobody else asserts stands as a single
    originator, weighted by its own attribution quality (§5.2).

Extremity is FIRST-CLASS in the result: the page exposes that Maat holds a routine claim and an
extraordinary claim to different bars (cauri, 2026-07-06). "Corroborated" wording is RESERVED for
claims with actual outside confirmation — a lone-source claim says so plainly, whatever its score.

Adversarial input is part of the threat model (a pasted page is attacker-controlled):
``sanitise_body`` strips invisible/bidi characters that hide instructions and caps length; the
EVIDENCE-SPAN CHECK drops any "claim" that does not quote the page verbatim — a prompt-injected
instruction can at worst emit text that is not on the page, and such text never survives. Model
outputs are parsed structurally (fixed schemas / label sets) everywhere. Scores cannot be gamed
upward by page content alone: lone-source claims cap low and only-unproven carriers cap at 0.70.

Pure orchestration over injected seams — fully testable offline. The serving layer wires the real
corpus, gate, search, cache and endpoints. Analysed articles never enter the canonical store.
"""

from __future__ import annotations

import re
import threading
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


class AnalyseError(ValueError):
    """A user-facing analysis failure — its message is safe to show the reader verbatim.
    Anything else that escapes the engine is internal and must be masked at the wire."""


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
    publisher_score: float | None    # reputation lookup (canonical-aware); None = "not yet rated"
    live: LiveMeta | None = None     # live-corroboration coverage, when the pass ran
    dropped_claims: int = 0          # "claims" that failed the quote-the-page check (injection guard)


# Seam types. ``CorpusLookup`` returns, per claim text, a HYDRATED CorpusFact or None — the
# serving layer implements it as cached-embedding match + one bounded DB fetch for the matches.
# ``GateFn`` inspects (publisher_domain, page_title) and returns a user-facing rejection message
# when the page is not a news article, else None.
CorpusLookup = Callable[[Sequence[str]], Sequence[CorpusFact | None]]
SearchFn = Callable[[str], list[LiveCandidate]]
GateFn = Callable[[str, str], str | None]
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


def _rep(reputation: Mapping[str, float], name: str) -> float | None:
    """Reputation lookup that survives source-string variants: raw, then canonical (§6.7) —
    so bbc.co.uk finds a track record stored under bbc.com / "BBC News"."""
    hit = reputation.get(name)
    if hit is not None:
        return hit
    return reputation.get(canonical_source(name))


# ── adversarial-input hygiene ────────────────────────────────────────────────────────────────────

# Invisible / direction-override characters used to smuggle instructions past a human reviewer:
# zero-widths, bidi overrides + isolates, word-joiners, BOM. Plus C0/C1 controls (keep \\t \\n).
_INVISIBLE = re.compile(
    "[\u0000-\u0008\u000b-\u001f\u007f-\u009f"  # C0 (except tab/newline) + DEL + C1
    "\u200b-\u200f"                              # zero-widths + LRM/RLM
    "\u202a-\u202e"                              # bidi embeddings/overrides
    "\u2060-\u2064"                              # word joiner + invisible operators
    "\u2066-\u2069"                              # bidi isolates
    "\ufeff]"                                     # BOM / zero-width no-break space
)


def sanitise_body(text: str, *, max_chars: int = 60_000) -> str:
    """Article text hygiene before it reaches any prompt: strip invisible/bidi/control characters
    (instruction-smuggling vectors), collapse blank-line runs, and cap the length (also bounds
    token spend). The page is DATA — this keeps it legible data."""
    text = _INVISIBLE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        cut = text.rfind(" ", 0, max_chars)
        text = text[: cut if cut > max_chars // 2 else max_chars]
    return text.strip()


_WS = re.compile(r"\s+")
# The model normalises punctuation GLYPHS when quoting — and swaps quote KINDS outright (BBC's
# “double curly” came back as 'single straight'; 13/39 real spans false-dropped on exactly this).
# Quotes carry no factual content, so DELETE every quote mark from both sides; fold dash/ellipsis
# variants. The guarantee is unchanged: the span's WORDS must still appear on the page verbatim.
_TYPO = str.maketrans({
    "'": None, '"': None, "`": None, "´": None,
    "‘": None, "’": None, "‚": None, "‛": None,
    "“": None, "”": None, "„": None, "«": None, "»": None,
    "‹": None, "›": None,
    "–": "-", "—": "-", "−": "-",
    "…": "...", "­": None,
})


def _norm(text: str) -> str:
    return _WS.sub(" ", text.translate(_TYPO)).strip().lower()


def verify_spans(claims: list[Claim], body: str) -> tuple[list[Claim], int]:
    """Keep only claims whose ``evidence_span`` actually quotes the page (whitespace-normalised).

    The extractor is REQUIRED to quote verbatim, so this is a structural injection guard: a
    prompt-injected "claim" that isn't on the page cannot survive, whatever the model was talked
    into. Returns (kept, dropped_count)."""
    hay = _norm(body)
    kept = [c for c in claims if c.evidence_span and _norm(c.evidence_span) in hay]
    return kept, len(claims) - len(kept)


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
    rated = _rep(reputation, source) is not None or any(
        _rep(reputation, s) is not None for grp in match.originator_sources for s in grp
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
    return reading, _rep(reputation, source) is not None


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
    rated = _rep(reputation, source) is not None or any(
        _rep(reputation, r.source) is not None for r in matched_rows
    )
    return reading, rated


def _safe_search(search: SearchFn, query: str) -> list[LiveCandidate]:
    try:
        return search(query)
    except Exception:  # noqa: BLE001 - one failed search must not sink the analysis
        return []


def _safe_extract(extract: Callable[..., list[Claim]], cand: LiveCandidate) -> list[str]:
    """A candidate article's claim texts; a failed extraction drops the candidate, never the run."""
    try:
        return [c.text for c in extract(cand.body, source_metadata=cand.domain)]
    except Exception:  # noqa: BLE001
        return []


class _LiveShared:
    """State shared by the per-claim live workers: URL dedupe, a per-URL extraction cache (one
    LLM read per candidate however many claims it serves), and the honest global caps."""

    def __init__(self, max_candidates: int) -> None:
        self.lock = threading.Lock()
        self.seen: set[str] = set()
        self.texts: dict[str, list[str]] = {}   # url -> extracted claim texts
        self.bodies: dict[str, str] = {}
        self.budget = max_candidates
        self.considered = 0

    def take(self, cands: list[LiveCandidate]) -> list[LiveCandidate]:
        out: list[LiveCandidate] = []
        with self.lock:
            for c in cands:
                if c.url in self.seen:
                    if c.url in self.bodies:  # already fetched for another claim — reuse free
                        out.append(c)
                    continue
                self.considered += 1
                if self.budget <= 0:
                    continue
                self.budget -= 1
                self.seen.add(c.url)
                out.append(c)
        return out

    def extract_once(self, cand: LiveCandidate, extract: Callable[..., list[Claim]]) -> list[str]:
        with self.lock:
            if cand.url in self.texts:
                return self.texts[cand.url]
        texts = _safe_extract(extract, cand)  # LLM call outside the lock
        with self.lock:
            self.texts.setdefault(cand.url, texts)
            if texts:
                self.bodies.setdefault(cand.url, cand.body)
            return self.texts[cand.url]


def analyse_article(
    url: str,
    *,
    reputation: Mapping[str, float],
    corpus_lookup: CorpusLookup | None = None,
    search: SearchFn | None = None,
    accept_candidate: Callable[[LiveCandidate], bool] | None = None,
    gate: GateFn | None = None,
    fetch: Callable[[str], FetchedPage | None] = fetch_page,
    extract: Callable[..., list[Claim]] = extract_claims,
    classify: Callable[..., list[Claim]] = classify_claims,
    extremity_of: Callable[[str], str] = rate_extremity,
    embed: Callable[[list[str]], list[list[float]]] = mistral_embed,
    language_of: Callable[[str], str] = _detect_language,
    same_fact_threshold: float = 0.82,
    live_max_searches: int = 10,
    live_max_candidates: int = 18,
    body_max_chars: int = 60_000,
    max_workers: int = 4,
    progress: ProgressFn | None = None,
) -> ArticleAnalysis:
    """Analyse one pasted URL end-to-end. ``corpus_lookup`` inherits existing corroboration;
    ``search`` corroborates novel claims live (per claim, in parallel — chips resolve as their
    evidence lands); ``gate`` rejects non-news pages. Any seam may be None (that leg is skipped).

    Raises AnalyseError (user-facing message) when the URL yields no article or fails the gate —
    never a fake score."""

    def emit(kind: str, data: dict[str, Any]) -> None:
        if progress is not None:
            progress(kind, data)

    page = fetch(url)
    if page is None or not page.body:
        raise AnalyseError("could not extract an article from this URL")
    source = _domain(url)
    body = sanitise_body(page.body, max_chars=body_max_chars)
    language = language_of(body)
    emit("fetched", {"title": page.title, "source": source, "language": language,
                     "date": page.date})

    if gate is not None:
        rejection = gate(source, page.title or "")
        if rejection:
            raise AnalyseError(rejection)

    extracted = extract(body, source_metadata=source, language=language)
    extracted, dropped = verify_spans(extracted, body)  # injection guard: must quote the page
    claims = classify(extracted, article_text=body)
    fact_claims = [c for c in claims if c.kind != "projection"]
    projections = [c for c in claims if c.kind == "projection"]
    emit("extracted", {
        "facts": [
            {"text": c.text, "voice": c.voice, "speaker": c.speaker,
             "central": c.in_headline or c.is_synthesis}
            for c in fact_claims
        ],
        "projections": [{"text": c.text, "speaker": c.speaker} for c in projections],
        "dropped": dropped,
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
            resolve(i, _corpus_reading(fact_claims[i], body, source, m, reputation))

    # Novel claims: rate extremity (needed for both the lone read and search priority)…
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        extremities = dict(zip(novel_idx, ex.map(
            lambda i: extremity_of(fact_claims[i].text), novel_idx
        )))

    live: LiveMeta | None = None
    if search is None or not novel_idx:
        for i in novel_idx:
            resolve(i, _lone_reading(fact_claims[i], body, source, extremities[i], reputation))
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
            resolve(i, _lone_reading(fact_claims[i], body, source, extremities[i], reputation))
        emit("searching", {"claims": len(selected)})

        own_canon = canonical_source(source)
        shared = _LiveShared(live_max_candidates)

        def live_one(i: int) -> None:
            """One claim's whole live journey — search → read candidates → fold → resolve —
            so its chip lights up the moment its own evidence is weighed."""
            claim = fact_claims[i]
            emit("checking", {"index": i})
            found = _safe_search(search, claim.text[:200])
            usable = [
                c for c in found
                if c.body and c.url != url and canonical_source(c.domain) != own_canon
                and (accept_candidate is None or accept_candidate(c))
            ]
            rows: list[ClaimRow] = []
            bodies: dict[str, str] = {}
            for cand in shared.take(usable):
                texts = shared.extract_once(cand, extract)
                if not texts:
                    continue
                bodies[cand.url] = cand.body
                rows.extend(
                    ClaimRow(id=f"live-{i}-{n}", text=t, article_id=cand.url, source=cand.domain)
                    for n, t in enumerate(texts)
                )
            matched_rows: list[ClaimRow] = []
            if rows:  # one small same-fact pass for THIS claim (§5.4 bar)
                vecs = np.asarray(embed([claim.text, *[r.text for r in rows]]), dtype=np.float64)
                sim = (_unit(vecs[:1]) @ _unit(vecs[1:]).T)[0]
                matched_rows = [rows[j] for j in np.nonzero(sim >= same_fact_threshold)[0]]
            resolve(i, _live_reading(
                claim, body, source, extremities[i], matched_rows,
                {u: shared.bodies[u] for u in {r.article_id for r in matched_rows}},
                reputation,
            ))

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(live_one, selected))
        live = LiveMeta(
            searched_claims=len(selected), skipped_claims=len(skipped),
            candidates_considered=shared.considered,
            candidates_used=len(shared.bodies),
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
        publisher_score=_rep(reputation, source), live=live, dropped_claims=dropped,
    )
