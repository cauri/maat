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

import logging
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
from maat.pipeline.corroborate import (
    SAME_FACT_THRESHOLD,
    ClaimRow,
    _named_speaker,
    attribution_weight,
    claim_attribution_weight,
    confidence_label,
    corroborate_fixed,
    group_by_similarity,
)
from maat.pipeline.extract import extract_claims
from maat.pipeline.extremity import rate_extremity
from maat.pipeline.identity import canonical_source
from maat.providers.seam import mistral_embed

log = logging.getLogger("maat.pipeline.analyse")

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
    # Web-search-path observability (0 on the Apify-only path). Not public — an ops signal for the
    # audit event (#382): how corroboration was actually reached, and how often it fell back.
    web_corroborated: int = 0    # claims with >=1 verbatim-verified, NLI-entailed outside source
    web_contradicted: int = 0    # claims a verified outside source CONTRADICTS (feeds disputed)
    web_neutral: int = 0         # claims web-search FOUND sources for, but NLI entailed none (#389
                                 # recall-drift signal — distinguishes "no coverage" from "rejected")
    apify_fallbacks: int = 0     # claims where web-search found nothing → fell back to Apify
    nli_available: bool = True   # False → NLI model down; web-search corroboration degraded
    deep_searched: int = 0       # S3 #401: claims that came back uncorroborated and got a DEEPER
                                 # second search before being concluded single-source
    deep_rescued: int = 0        # …of those, how many the deeper search actually corroborated —
                                 # the "was one shallow pass enough?" signal (low → recall is fine)


@dataclass(frozen=True)
class Citation:
    """One source the web-search pass offers for a claim: a URL and the verbatim passage it says
    asserts the claim. The passage is UNTRUSTED — the pipeline re-fetches the page through the
    extraction ladder and checks the quote is really there (``verify_quote``) before an NLI model
    judges whether it actually entails the claim. The model proposes; deterministic code + NLI
    decide — which is what stops a topically-similar-but-unrelated passage from reading as
    agreement (the false-corroboration failure this rebuild fixes)."""

    url: str
    domain: str
    quote: str


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
    checked: bool = True            # False → novel claim we did NOT search (over the live cap):
                                    # carried with NO score, excluded from the overall, and offered
                                    # to the reader for an on-demand force-check (#397). Never
                                    # conflate "checked, found alone" with "never checked".


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
    merged_claims: int = 0           # near-duplicate extractions consolidated into one claim (#411)


# Seam types. ``CorpusLookup`` returns, per claim text, a HYDRATED CorpusFact or None — the
# serving layer implements it as cached-embedding match + one bounded DB fetch for the matches.
# ``GateFn`` inspects (publisher_domain, page_title) and returns a user-facing rejection message
# when the page is not a news article, else None.
CorpusLookup = Callable[[Sequence[str]], Sequence[CorpusFact | None]]
SearchFn = Callable[[str], list[LiveCandidate]]
GateFn = Callable[[str, str], str | None]
ProgressFn = Callable[[str, dict[str, Any]], None]
# ``WebSearchFn`` is the PRIMARY corroboration seam (P14 #381): one web-search pass over ALL the
# selected novel claims at once, returning, per claim (aligned with the input order), the outside
# sources it found — each a Citation (url + verbatim quote). The serving layer implements it with
# the Anthropic web_search tool; ``own_domain`` is passed so the provider can block the analysed
# publisher's own pages. ``NliFn`` is the entailment judge (premise, hypothesis) → (label, prob) —
# an NLI MODEL, not an LLM (cauri's call, #229/#381); None when the model is unavailable.
# ``deep`` (S3 #401, optional kwarg): a larger-budget second pass for claims a first pass left
# uncorroborated — same prompt, more searches, so recall improves without an in-app prompt change.
WebSearchFn = Callable[..., Sequence[Sequence[Citation]]]


@dataclass(frozen=True)
class ScoringKnobs:
    """Operator-promoted scoring overrides (#412), threaded as ONE object through the analyse
    readings into ``corroborate_fixed`` / ``score_article`` / the weight functions. Every field
    ``None`` → the code constant, so a default ``ScoringKnobs()`` (or ``knobs=None``) is exactly the
    unconfigured pipeline. The serving layer builds this from the promoted ``admin.config.promoted``
    events (``maat.config.analyse_overrides``) — the same sign-off-gated flow the feed's corroborate
    agent honours, so a promoted knob now applies to BOTH surfaces."""

    decay: dict[str, float] | None = None            # extremity decay curve (§5.6)
    primary_lift: float | None = None                # primary-source bonus (§5.7)
    cap: float | None = None                         # maximum confidence (§5.7)
    duplicate_source_threshold: float | None = None  # originator-collapse lexical bar (§5.5)
    rep_unrated: float | None = None                 # S1: unrated-outlet reputation weight
    rep_floor: float | None = None                   # S1: proven-weak outlet floor
    w_own: float | None = None                       # S2: own-voice claim attribution weight
    entail_floor: float | None = None                # S5: barely-entailing citation floor
    publisher_floor: float | None = None             # S4: proven-weak publisher ceiling floor

    def fixed_kwargs(self) -> dict[str, Any]:
        """The ``corroborate_fixed`` kwargs this carries (None means 'code default' there too)."""
        out: dict[str, Any] = {
            "rep_unrated": self.rep_unrated, "rep_floor": self.rep_floor,
            "decay": self.decay, "primary_lift": self.primary_lift, "cap": self.cap,
        }
        if self.duplicate_source_threshold is not None:
            out["duplicate_source_threshold"] = self.duplicate_source_threshold
        return out
NliFn = Callable[[str, str], tuple[str, float] | None]


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


def claim_position(evidence_span: str, body: str) -> float:
    """Where a claim's evidence sits in the article, as a 0–1 fraction of the way down — so the
    Analyse page can highlight it at the right spot in a MINIATURE (skeleton) rendering without any
    article prose leaving the server. Whitespace/typography-normalised (same fold as the span
    guard); 0.0 when not locatable."""
    if not evidence_span or not body:
        return 0.0
    hay = _norm(body)
    if not hay:
        return 0.0
    i = hay.find(_norm(evidence_span))
    return round(max(0.0, min(1.0, i / len(hay))), 4) if i >= 0 else 0.0


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
    threshold: float = SAME_FACT_THRESHOLD,
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
    reputation: Mapping[str, float], ownership: dict[str, str] | None = None,
    knobs: ScoringKnobs | None = None,
) -> tuple[ClaimReading, bool]:
    """Fold the pasted article into an existing cluster's read (reprints collapse, an independent
    report counts) and inherit the cluster's extremity/grounding/disputed standing."""
    kn = knobs or ScoringKnobs()
    row = ClaimRow(id=claim.id, text=claim.text, article_id=_ANALYSED, source=source)
    cor = corroborate_fixed(
        [*match.member_claims, row],
        {**match.bodies, _ANALYSED: body},
        match.extremity,
        grounding=match.grounding,
        ownership=ownership,
        reputation=dict(reputation),  # S1 #399: established originators corroborate more
        attribution={_ANALYSED: claim_attribution_weight(
            claim.voice, claim.speaker, body, source, w_own=kn.w_own)},  # S2 #400
        **kn.fixed_kwargs(),
    )
    # OUTSIDE-corroboration rating only (S4 #402): the pasted publisher's OWN rating is now the
    # `publisher_reputation` ceiling, not this flag — this flag means "a proven OTHER originator
    # corroborates", which lifts the cold-start cap.
    rated = any(
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
    claim: Claim, body: str, source: str, extremity: str, reputation: Mapping[str, float],
    knobs: ScoringKnobs | None = None,
) -> tuple[ClaimReading, bool]:
    """A claim with no outside evidence: a single originator, weighted by its own attribution
    quality (§5.2) — honest, never inflated."""
    kn = knobs or ScoringKnobs()
    row = ClaimRow(id=claim.id, text=claim.text, article_id=_ANALYSED, source=source)
    cor = corroborate_fixed(
        [row], {_ANALYSED: body}, extremity, reputation=dict(reputation),
        attribution={_ANALYSED: claim_attribution_weight(
            claim.voice, claim.speaker, body, source, w_own=kn.w_own)},
        **kn.fixed_kwargs(),
    )
    verdict, tier = claim_verdict(
        cor.confidence, cor.independent_originators, cor.has_primary, extremity
    )
    reading = ClaimReading(
        claim=claim, extremity=extremity, confidence=cor.confidence,
        independent_originators=cor.independent_originators, has_primary=cor.has_primary,
        disputed=False, grounding=None, verdict=verdict, tier=tier, matched_cluster_id=None,
    )
    return reading, False  # lone → no OUTSIDE corroboration; publisher rating is the S4 ceiling now


def _unchecked_reading(claim: Claim, extremity: str) -> tuple[ClaimReading, bool]:
    """A novel claim we did NOT search — over the live cap (#397). It is NOT lone (we never looked),
    so it carries no confidence, no score, and is excluded from the overall read; the reader can
    force a check on it. Distinct from ``_lone_reading`` ("searched, genuinely single-source"),
    which the identical old wording ("Only this source so far") dishonestly conflated it with."""
    reading = ClaimReading(
        claim=claim, extremity=extremity, confidence=0.0, independent_originators=0,
        has_primary=False, disputed=False, grounding=None,
        verdict="Not checked yet", tier="unchecked", matched_cluster_id=None, checked=False,
    )
    return reading, False


def _live_reading(
    claim: Claim, body: str, source: str, extremity: str,
    matched_rows: list[ClaimRow], bodies: dict[str, str],
    reputation: Mapping[str, float],
    *, disputed: bool = False, ownership: dict[str, str] | None = None,
    cite_weights: dict[str, float] | None = None,
    knobs: ScoringKnobs | None = None,
) -> tuple[ClaimReading, bool]:
    """Fold live-found corroborating claims with the pasted article's own assertion — the same
    §5.5 collapse and §5.6 read the feed uses, over evidence found minutes ago.

    ``disputed`` (P14 #381): a verified outside source CONTRADICTS the claim (NLI) and none
    corroborate it — carried through to ``corroborate_fixed`` as ``grounding="contradicted"`` (the
    read multiplies down) and to the verdict ("Disputed — contradicted by stronger reporting").

    ``ownership`` (#41/#254): co-owned outlets found live collapse to ONE independent originator —
    the same anti-laundering rollup the feed applies, so web search surfacing several sister
    outlets of one group cannot inflate the count."""
    kn = knobs or ScoringKnobs()
    grounding = "contradicted" if disputed else None
    own = ClaimRow(id=claim.id, text=claim.text, article_id=_ANALYSED, source=source)
    cor = corroborate_fixed(
        [own, *matched_rows], {**bodies, _ANALYSED: body}, extremity, grounding=grounding,
        ownership=ownership, reputation=dict(reputation),  # S1 #399
        attribution={
            # S5 #403: each cited source graded by attribution × entailment strength…
            **(cite_weights or {}),
            # …and the pasted article's own claim by its voice/speaker (S2 #400).
            _ANALYSED: claim_attribution_weight(
                claim.voice, claim.speaker, body, source, w_own=kn.w_own),
        },
        **kn.fixed_kwargs(),
    )
    verdict, tier = claim_verdict(
        cor.confidence, cor.independent_originators, cor.has_primary, extremity,
        disputed=disputed, grounding=grounding,
    )
    reading = ClaimReading(
        claim=claim, extremity=extremity, confidence=cor.confidence,
        independent_originators=cor.independent_originators, has_primary=cor.has_primary,
        disputed=disputed, grounding=grounding, verdict=verdict, tier=tier, matched_cluster_id=None,
    )
    rated = any(  # OUTSIDE corroboration only (S4 #402) — publisher rating is the ceiling
        _rep(reputation, r.source) is not None for r in matched_rows
    )
    return reading, rated


# ── web-search corroboration (P14 #381): verbatim-verify a cited quote, then NLI-judge it ─────────


def verify_quote(quote: str, body: str) -> bool:
    """The cited passage appears on the page verbatim (whitespace/typography-normalised — the SAME
    fold as the pasted-article span guard)."""
    if not quote or not body:
        return False
    return _norm(quote) in _norm(body)


def _content_tokens(text: str) -> set[str]:
    return {w for w in _norm(text).split() if len(w) >= 4}


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _page_sentences(body: str) -> list[str]:
    """Body split into candidate sentences (6–60 words) — the pool the second-chance NLI ranks."""
    return [s.strip() for s in _SENTENCE_SPLIT.split(body)
            if 6 <= len(s.split()) <= 60]


def best_entailing_sentence(
    body: str, claim_text: str, nli: NliFn | None,
    *, min_entail: float = 0.5, top_k: int = 6,
) -> str | None:
    """A sentence from the page that ENTAILS the claim, or None (#389 recall).

    Second chance when the search model quoted a sentence NLI didn't accept but the PAGE still
    asserts the fact in different words: rank the page's own sentences by content-token overlap
    with the claim and NLI the top few (bidirectionally, same gate as ``judge_entailment``). Only
    an entailment survives, so this recovers reframed corroboration ("found guilty of embezzling
    EU funds" ⇒ "convicted of misusing EU funds") WITHOUT loosening the gate — validated to leave
    the false positives (Australian ankle-monitor pages) rejected."""
    if nli is None or not body:
        return None
    qt = _content_tokens(claim_text)
    if not qt:
        return None
    ranked = sorted(
        _page_sentences(body),
        key=lambda s: len(_content_tokens(s) & qt) / (len(_content_tokens(s)) or 1),
        reverse=True,
    )
    for sentence in ranked[:top_k]:
        if judge_entailment(nli, sentence, claim_text, min_entail=min_entail) == "entails":
            return sentence
    return None


def quote_grounded(quote: str, body: str, *, min_overlap: float = 0.6) -> bool:
    """Is the cited quote grounded in the page we fetched? Verbatim, OR most of its content words
    are present (token overlap ≥ ``min_overlap``).

    RELAXED on purpose (#381): the search model quotes the page as ANTHROPIC fetched it, but our
    re-fetch is often a different rendering (a bot-wall variant, a live-updated article, a consent
    trim) — real coverage of the Le Pen sentencing was being dropped because the exact sentence
    differed by a word. This is a best-effort anti-fabrication check, not the primary gate (NLI is):
    it catches a quote whose content is wholly absent from the page (mis-attributed / fabricated)
    while tolerating rendering drift. A page we CANNOT fetch is judged on NLI alone, never dropped
    for being unfetchable."""
    if not quote or not body:
        return False
    if verify_quote(quote, body):
        return True
    qt = _content_tokens(quote)
    if not qt:
        return False
    return len(qt & _content_tokens(body)) / len(qt) >= min_overlap


# NLI id2label of the pinned cross-encoder (pipeline/nli.py): contradiction / entailment / neutral.
_NLI_ENTAIL = "entailment"
_NLI_CONTRADICT = "contradiction"


def judge_entailment_scored(
    nli: NliFn | None, quote: str, claim_text: str,
    *, min_entail: float = 0.5, min_contradict: float = 0.6,
) -> tuple[str, float]:
    """An NLI MODEL's ruling on whether the cited passage supports the claim, WITH its strength:
    ('entails' | 'contradicts' | 'neutral' | 'unknown', entailment probability).

    Only 'entails' counts as corroboration — this is the gate that stops a topically-similar but
    unrelated passage (an Australian ankle-monitor ruling vs a French politician's sentence) from
    being read as agreement, which cosine similarity could not tell apart. cauri's call: an NLI
    model, not an LLM judge (#229/#381).

    Judged BIDIRECTIONALLY: two independent reports of one fact often frame it differently — one
    over-specifies ("handed a three-year sentence") where the other reframes ("term reduced to
    three years") — so neither strictly entails the other, though both assert the same fact. We
    accept entailment in EITHER direction (validated on real Le Pen coverage: bidirectional lifts
    recall from 4/6 to 6/7 including cross-language French support, while every unrelated /
    contradicting pair is still rejected). Contradiction is judged passage→claim only (the natural
    'does the evidence dispute the claim'). 'unknown' when the model is unavailable — the caller
    then does NOT count the source (it never trusts the search model's own claim→source mapping).

    The strength (S5 #403) is the best entailment probability across the two directions — 0.0
    whenever the verdict is not 'entails' — so a source that asserts the fact head-on can count
    more than one that barely clears the gate (``entailment_weight`` maps it to a fold weight)."""
    if nli is None:
        return "unknown", 0.0
    forward = nli(quote, claim_text)
    reverse = nli(claim_text, quote)
    if forward is None and reverse is None:
        return "unknown", 0.0
    strength = max(
        (res[1] for res in (forward, reverse)
         if res is not None and res[0] == _NLI_ENTAIL and res[1] >= min_entail),
        default=0.0,
    )
    if strength > 0.0:
        return "entails", strength
    if forward is not None and forward[0] == _NLI_CONTRADICT and forward[1] >= min_contradict:
        return "contradicts", 0.0
    return "neutral", 0.0


def judge_entailment(
    nli: NliFn | None, quote: str, claim_text: str,
    *, min_entail: float = 0.5, min_contradict: float = 0.6,
) -> str:
    """The verdict alone — see ``judge_entailment_scored`` (the gate is identical)."""
    return judge_entailment_scored(
        nli, quote, claim_text, min_entail=min_entail, min_contradict=min_contradict
    )[0]


# S5 #403 — graded corroboration strength: within the accepted (the gate already dropped junk), a
# source that asserts the fact head-on counts more than one that barely clears the entailment bar.
# A citation's fold weight scales linearly from the floor (at the gate threshold) to 1.0 (at
# certainty). Gentle by design — grading, not a second gate. DRAFT knob.
_ENTAIL_FLOOR = 0.7


def entailment_weight(strength: float, min_entail: float = 0.5, *, floor: float | None = None) -> float:
    """Map an accepted citation's entailment strength to its fold weight in [_ENTAIL_FLOOR, 1.0].
    ``floor`` overrides the constant — the operator-promoted knob seam (#412)."""
    lo = _ENTAIL_FLOOR if floor is None else floor
    if strength <= min_entail:
        return lo
    span = 1.0 - min_entail
    frac = (min(1.0, strength) - min_entail) / span if span > 0 else 1.0
    return round(lo + (1.0 - lo) * frac, 2)


class _CiteFetch:
    """Fetch each cited URL at most once across all claims (many claims cite the same page) and
    cache the ladder body. A ``None`` entry caches a failed fetch so it is not retried."""

    def __init__(self, fetch: Callable[[str], FetchedPage | None]) -> None:
        self._fetch = fetch
        self._lock = threading.Lock()
        self._bodies: dict[str, str | None] = {}

    def body(self, url: str) -> str | None:
        with self._lock:
            if url in self._bodies:
                return self._bodies[url]
        try:
            page = self._fetch(url)
            got = page.body if page and page.body else None
        except Exception as e:  # noqa: BLE001 - a dead cited URL drops that source, never the run
            log.warning("cite-fetch url=%s failed: %s", url, type(e).__name__)
            got = None
        if got is None:
            log.info("cite-fetch url=%s no body — citation judged on NLI alone", url)
        with self._lock:
            self._bodies.setdefault(url, got)
            return self._bodies[url]


def _websearch_rows(
    i: int, claim: Claim, citations: Sequence[Citation], *,
    url: str, own_canon: str, accept_candidate: Callable[[LiveCandidate], bool] | None,
    cite_fetch: _CiteFetch, nli: NliFn | None, nli_min: float, contradict_min: float,
    entail_floor: float | None = None,
) -> tuple[list[ClaimRow], dict[str, str], bool, dict[str, float]]:
    """One claim's web-search corroboration. For each offered citation: exclude the pasted outlet /
    denied domains, then NLI-JUDGE the quote against the claim (the hard gate — entailment counts,
    contradiction disputes, everything else drops). An NLI-entailed source is then re-fetched
    best-effort (FAST rungs only) and dropped ONLY if we got a body the quote is wholly absent from
    (``quote_grounded``); a page we cannot fetch is kept on the NLI judgement — real corroboration
    is never lost just because a publisher is hard to fetch. Returns (rows, their bodies,
    contradicted?, per-URL fold weights). The fold body is the fetched page when available, else the
    quote itself.

    The weights (S5 #403) grade each accepted source by HOW STRONGLY it asserts the fact: its
    sourcing quality (the same §5.2 attribution scan the fold would run) × its entailment strength
    (``entailment_weight``) — so a head-on assertion counts more than one that barely clears the
    gate. Fed to ``corroborate_fixed`` as ``attribution`` overrides for exactly these URLs."""
    rows: list[ClaimRow] = []
    bodies: dict[str, str] = {}
    weights: dict[str, float] = {}
    contradicted = False
    seen: set[str] = set()
    for n, cit in enumerate(citations):
        if not cit.url or cit.url == url or cit.url in seen:
            continue
        if canonical_source(cit.domain) == own_canon:
            continue  # the pasted outlet is not independent of itself
        if accept_candidate is not None and not accept_candidate(
            LiveCandidate(url=cit.url, domain=cit.domain, title="", body="")
        ):
            continue  # denied source / non-news (prefiltered_reject drops wikis, social, …)
        seen.add(cit.url)
        verdict, strength = judge_entailment_scored(
            nli, cit.quote, claim.text, min_entail=nli_min, min_contradict=contradict_min
        )
        if verdict == "contradicts":
            contradicted = True
            continue
        body = cite_fetch.body(cit.url)  # best-effort; None when the publisher is walled
        quote = cit.quote
        if verdict == "entails":
            if body is not None and not quote_grounded(quote, body):
                continue  # fetched, but the quote's content isn't on the page → mis-attributed
        elif body is not None:
            # Neutral model quote, but we have the page (#389): the page may assert the fact in
            # its own words — give the source a second chance from its own sentences.
            sentence = best_entailing_sentence(body, claim.text, nli, min_entail=nli_min)
            if sentence is None:
                continue
            quote = sentence
            _v, strength = judge_entailment_scored(  # the accepted sentence's own strength (S5)
                nli, sentence, claim.text, min_entail=nli_min, min_contradict=contradict_min
            )
        else:
            continue  # neutral / unknown and unfetchable — never trust the model's own mapping
        rows.append(ClaimRow(id=f"cite-{i}-{n}", text=quote, article_id=cit.url, source=cit.domain))
        bodies[cit.url] = body or quote
        weights[cit.url] = round(
            attribution_weight(bodies[cit.url], cit.domain)
            * entailment_weight(strength, nli_min, floor=entail_floor),
            2,
        )
    return rows, bodies, contradicted, weights


def _safe_search(search: SearchFn, query: str) -> list[LiveCandidate]:
    try:
        return search(query)
    except Exception as e:  # noqa: BLE001 - one failed search must not sink the analysis
        log.warning("live-search query=%r failed: %s", query[:60], type(e).__name__)
        return []


def _safe_extract(extract: Callable[..., list[Claim]], cand: LiveCandidate) -> list[str]:
    """A candidate article's claim texts; a failed extraction drops the candidate, never the run."""
    try:
        return [c.text for c in extract(cand.body, source_metadata=cand.domain)]
    except Exception as e:  # noqa: BLE001
        log.warning("candidate-extract url=%s body=%d failed: %s",
                    cand.url, len(cand.body), type(e).__name__)
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


def _apify_rows(
    i: int, claim: Claim, *,
    search: SearchFn, extract: Callable[..., list[Claim]],
    embed: Callable[[list[str]], list[list[float]]], shared: _LiveShared,
    accept_candidate: Callable[[LiveCandidate], bool] | None, own_canon: str, url: str,
    same_fact_threshold: float,
) -> tuple[list[ClaimRow], dict[str, str]]:
    """The Apify corroboration path for ONE claim — search → read candidates (shared per-URL
    extraction) → same-fact match at the §5.4 bar. The fallback when web-search finds nothing (and
    the sole path when no web_search seam is wired — behaviour is byte-identical to before #381)."""
    found = _safe_search(search, claim.text[:200])
    usable = [
        c for c in found
        if c.body and c.url != url and canonical_source(c.domain) != own_canon
        and (accept_candidate is None or accept_candidate(c))
    ]
    rows: list[ClaimRow] = []
    for cand in shared.take(usable):
        texts = shared.extract_once(cand, extract)
        if not texts:
            continue
        rows.extend(
            ClaimRow(id=f"live-{i}-{n}", text=t, article_id=cand.url, source=cand.domain)
            for n, t in enumerate(texts)
        )
    matched_rows: list[ClaimRow] = []
    if rows:  # one small same-fact pass for THIS claim (§5.4 bar)
        vecs = np.asarray(embed([claim.text, *[r.text for r in rows]]), dtype=np.float64)
        sim = (_unit(vecs[:1]) @ _unit(vecs[1:]).T)[0]
        matched_rows = [rows[j] for j in np.nonzero(sim >= same_fact_threshold)[0]]
    bodies = {u: shared.bodies[u] for u in {r.article_id for r in matched_rows}}
    return matched_rows, bodies


def consolidate_claims(
    claims: list[Claim],
    embed: Callable[[list[str]], list[list[float]]],
    threshold: float = SAME_FACT_THRESHOLD,
) -> tuple[list[Claim], int]:
    """Merge near-duplicate extracted claims into ONE claim each (#411) — one fact, one claim.

    The extractor can assert the same fact twice in different words ("Senator Lindsey Graham dies
    at 71" / "Lindsey Graham died on July 11 at the age of 71"). Left separate, each copy gets its
    OWN extremity rating (LLM variance rated those significant vs notable) and its OWN search
    budget (both landed in one batch, which piled its citations onto one variant) — the same fact
    scored 45 and 95 in one analysis. Consolidating BEFORE rating and search kills both: one claim
    → one rating → one search → one score.

    Same-fact is the system's ONE definition (§5.4): embedding cosine at the ``threshold`` bar,
    average-linkage — the exact machinery and bar the corpus match uses. Merge policy: the
    representative is the best-evidenced member (a NAMED attributed voice first — it carries the
    strongest §5.2 weight — then the most detailed text); centrality survives if ANY member was
    central. Returns (consolidated claims in article order, how many were folded away). A failed
    embed skips consolidation — never sinks the analysis."""
    if len(claims) <= 1:
        return claims, 0
    try:
        vecs = np.asarray(embed([c.text for c in claims]), dtype=np.float64)
        # Validate the embed CONTRACT before clustering (review #2): a wrong-length or non-finite
        # return is not an exception, but it would make group_by_similarity index off the matrix
        # rows (silently dropping trailing claims / IndexError in the merge loop) or make every NaN
        # comparison force an all-merge (collapsing distinct facts). Any violation → skip, don't
        # sink or corrupt — the documented guarantee.
        if vecs.ndim != 2 or vecs.shape[0] != len(claims) or not np.isfinite(vecs).all():
            raise ValueError(f"embed returned {vecs.shape} for {len(claims)} claims")
        groups = group_by_similarity([c.text for c in claims], threshold, embeddings=vecs)
    except Exception as e:  # noqa: BLE001 - consolidation is an optimisation, never fatal
        log.warning("claim consolidation skipped (%s)", type(e).__name__)
        return claims, 0
    picked: list[tuple[int, Claim]] = []  # (first member's article position, merged claim)
    merged = 0
    for g in groups:
        members = [claims[i] for i in sorted(g)]
        if len(members) == 1:
            picked.append((min(g), members[0]))
            continue
        merged += len(members) - 1
        rep = max(members, key=lambda c: (
            c.voice == "attributed" and _named_speaker(c.speaker), len(c.text),
        ))
        picked.append((min(g), rep.model_copy(update={
            "in_headline": any(c.in_headline for c in members),
            "is_synthesis": any(c.is_synthesis for c in members),
        })))
    picked.sort(key=lambda p: p[0])
    return [c for _pos, c in picked], merged


def analyse_article(
    url: str,
    *,
    reputation: Mapping[str, float],
    ownership: dict[str, str] | None = None,
    corpus_lookup: CorpusLookup | None = None,
    web_search: WebSearchFn | None = None,
    nli: NliFn | None = None,
    search: SearchFn | None = None,
    accept_candidate: Callable[[LiveCandidate], bool] | None = None,
    gate: GateFn | None = None,
    fetch: Callable[[str], FetchedPage | None] = fetch_page,
    extract: Callable[..., list[Claim]] = extract_claims,
    classify: Callable[..., list[Claim]] = classify_claims,
    extremity_of: Callable[[str], str] = rate_extremity,
    embed: Callable[[list[str]], list[list[float]]] = mistral_embed,
    language_of: Callable[[str], str] = _detect_language,
    same_fact_threshold: float = SAME_FACT_THRESHOLD,
    nli_entail_min: float = 0.5,
    nli_contradict_min: float = 0.6,
    live_max_searches: int = 10,
    live_max_candidates: int = 18,
    body_max_chars: int = 60_000,
    max_workers: int = 4,
    knobs: ScoringKnobs | None = None,
    progress: ProgressFn | None = None,
) -> ArticleAnalysis:
    """Analyse one pasted URL end-to-end. ``corpus_lookup`` inherits existing corroboration.

    Novel claims corroborate LIVE (per claim, in parallel — chips resolve as their evidence lands):
      * ``web_search`` (PRIMARY, #381) — one pass over all selected novel claims returns cited
        quotes; each is re-fetched (``fetch`` ladder), verbatim-verified, and NLI-judged (``nli``);
        only entailed sources count, a verified contradiction disputes.
      * ``search`` (Apify, FALLBACK) — the per-claim search→extract→cosine path, used when
        web-search finds nothing for a claim (and the sole path when ``web_search`` is None, which
        is byte-identical to the pre-#381 behaviour).

    ``gate`` rejects non-news pages. Any seam may be None (that leg is skipped). Raises AnalyseError
    (user-facing message) when the URL yields no article or fails the gate — never a fake score."""

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
    # One fact, one claim (#411): BEFORE the skeleton is emitted and before any rating/search, so
    # a duplicated fact can never earn two extremity ratings or split one search budget in two.
    fact_claims, merged = consolidate_claims(fact_claims, embed, same_fact_threshold)
    emit("extracted", {
        "facts": [
            {"text": c.text, "voice": c.voice, "speaker": c.speaker,
             "central": c.in_headline or c.is_synthesis,
             "position": claim_position(c.evidence_span, body)}
            for c in fact_claims
        ],
        "projections": [{"text": c.text, "speaker": c.speaker} for c in projections],
        "dropped": dropped,
        "merged": merged,
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
            resolve(i, _corpus_reading(fact_claims[i], body, source, m, reputation, ownership, knobs))

    # Novel claims: rate extremity (needed for both the lone read and search priority)…
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        extremities = dict(zip(novel_idx, ex.map(
            lambda i: extremity_of(fact_claims[i].text), novel_idx
        )))

    live: LiveMeta | None = None
    have_live = web_search is not None or search is not None
    if not have_live or not novel_idx:
        for i in novel_idx:
            resolve(i, _lone_reading(fact_claims[i], body, source, extremities[i], reputation, knobs))
        if have_live:
            live = LiveMeta(0, 0, 0, 0, nli_available=nli is not None)
    else:
        # …then go out to the web for the ones that matter most: central claims first, then by
        # extremity (an extraordinary claim needs the evidence most), then article order.
        ranked = sorted(novel_idx, key=lambda i: (
            not (fact_claims[i].in_headline or fact_claims[i].is_synthesis),
            -_EXTREMITY_RANK.get(extremities[i], 2),
            i,
        ))
        selected, skipped = ranked[:live_max_searches], ranked[live_max_searches:]
        for i in skipped:  # over the search cap — honestly "Not checked" (never searched), scored
            resolve(i, _unchecked_reading(fact_claims[i], extremities[i]))  # by neither us nor the overall (#397)
        emit("searching", {"claims": len(selected)})

        own_canon = canonical_source(source)
        shared = _LiveShared(live_max_candidates)   # Apify path / fallback
        cite_fetch = _CiteFetch(fetch)              # web-search quote verification (fetch once/URL)

        # ONE web-search pass over all selected claims (#381): per-claim cited quotes, aligned with
        # `selected`. A failed pass leaves every claim to the Apify fallback below.
        citations: dict[int, Sequence[Citation]] = {}
        if web_search is not None:
            try:
                results = web_search([fact_claims[i].text for i in selected], source)
            except Exception as e:  # noqa: BLE001 - a failed web-search never sinks the analysis
                log.warning("web-search pass failed (%s) — all %d claims fall back to Apify",
                            type(e).__name__, len(selected))
                results = []
            for k, i in enumerate(selected):
                if k < len(results):
                    citations[i] = results[k]

        tally = {"web": 0, "contra": 0, "fallback": 0, "neutral": 0}
        # Which claims each ops counter has ALREADY counted, so the deep pass adjusts precisely
        # rather than blind-incrementing (a pass-1 web claim can still be independent_originators<=1
        # when its only outside source collapsed with the pasted outlet, and would double-count on
        # rescue; review #4). ops-meta only — never the score.
        web_counted: set[int] = set()
        neutral_counted: set[int] = set()
        tally_lock = threading.Lock()

        def live_one(i: int) -> None:
            """One claim's whole live journey — web-search verify+judge (or Apify fallback) → fold
            → resolve — so its chip lights up the moment its own evidence is weighed."""
            claim = fact_claims[i]
            emit("checking", {"index": i})
            matched_rows: list[ClaimRow] = []
            bodies: dict[str, str] = {}
            weights: dict[str, float] = {}
            contradicted = False
            cits = citations.get(i)
            if cits:
                matched_rows, bodies, contradicted, weights = _websearch_rows(
                    i, claim, cits, url=url, own_canon=own_canon,
                    accept_candidate=accept_candidate, cite_fetch=cite_fetch, nli=nli,
                    nli_min=nli_entail_min, contradict_min=nli_contradict_min,
                    entail_floor=(knobs.entail_floor if knobs else None),
                )
            if matched_rows:
                with tally_lock:
                    tally["web"] += 1
                    web_counted.add(i)
            else:
                if cits:  # web-search returned sources but NLI entailed none (#389 recall signal)
                    with tally_lock:
                        tally["neutral"] += 1
                        neutral_counted.add(i)
                if search is not None:  # web-search found nothing usable → Apify fallback
                    matched_rows, bodies = _apify_rows(
                        i, claim, search=search, extract=extract, embed=embed, shared=shared,
                        accept_candidate=accept_candidate, own_canon=own_canon, url=url,
                        same_fact_threshold=same_fact_threshold,
                    )
                    if matched_rows and web_search is not None:
                        with tally_lock:
                            tally["fallback"] += 1
            # Conservative dispute: a verified outside source contradicts AND none corroborate.
            dispute = contradicted and not matched_rows
            if dispute:
                with tally_lock:
                    tally["contra"] += 1
            resolve(i, _live_reading(
                claim, body, source, extremities[i], matched_rows, bodies, reputation,
                disputed=dispute, ownership=ownership, cite_weights=weights, knobs=knobs,
            ))

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(live_one, selected))

        # S3 #401 — coverage-aware corroboration: the originator COUNT is a floor gated by search
        # recall, so a claim that came back with NO outside corroboration gets a DEEPER second search
        # (fresh pass, larger budget) before we conclude it is genuinely single-source. This is the
        # honest answer to "we're not exhaustive": at the low end — where each originator swings the
        # score most and a shallow miss reads as 'lone' — we look harder rather than stop at one pass.
        # Bounded: only the still-lone claims, one extra pass. ``deep_rescued`` records whether the
        # deeper look actually changes anything (the "was one pass enough?" signal).
        deep_searched = deep_rescued = 0
        still_lone = [
            i for i in selected
            if readings[i] is not None and readings[i].independent_originators <= 1
            and not readings[i].disputed
        ]
        if web_search is not None and still_lone:
            deep_searched = len(still_lone)
            emit("searching", {"claims": deep_searched, "deep": True})
            try:
                deeper = web_search([fact_claims[i].text for i in still_lone], source, deep=True)
            except Exception as e:  # noqa: BLE001 - a failed deep pass just leaves the pass-1 read
                log.warning("deep web-search pass failed (%s)", type(e).__name__)
                deeper = []

            def deepen(pair: tuple[int, int]) -> None:
                nonlocal deep_rescued
                k, i = pair
                extra = deeper[k] if k < len(deeper) else ()
                if not extra:
                    return
                merged = [*(citations.get(i) or ()), *extra]
                rows, bodies2, _contra, weights2 = _websearch_rows(
                    i, fact_claims[i], merged, url=url, own_canon=own_canon,
                    accept_candidate=accept_candidate, cite_fetch=cite_fetch, nli=nli,
                    nli_min=nli_entail_min, contradict_min=nli_contradict_min,
                    entail_floor=(knobs.entail_floor if knobs else None),
                )
                if not rows:
                    return
                reading, rated = _live_reading(
                    fact_claims[i], body, source, extremities[i], rows, bodies2, reputation,
                    disputed=False, ownership=ownership, cite_weights=weights2, knobs=knobs,
                )
                if reading.independent_originators > readings[i].independent_originators:
                    with tally_lock:
                        deep_rescued += 1
                        if i not in web_counted:      # count this claim as web-corroborated once
                            tally["web"] += 1
                            web_counted.add(i)
                        if i in neutral_counted:       # …and it's no longer merely neutral
                            tally["neutral"] = max(0, tally["neutral"] - 1)
                            neutral_counted.discard(i)
                    resolve(i, (reading, rated))

            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                list(ex.map(deepen, list(enumerate(still_lone))))

        live = LiveMeta(
            searched_claims=len(selected), skipped_claims=len(skipped),
            candidates_considered=shared.considered, candidates_used=len(shared.bodies),
            web_corroborated=tally["web"], web_contradicted=tally["contra"],
            web_neutral=tally["neutral"], apify_fallbacks=tally["fallback"],
            nli_available=nli is not None, deep_searched=deep_searched, deep_rescued=deep_rescued,
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
    # Unchecked claims (#397) carry no evidence either way — they NEVER move the overall. Score over
    # the checked facts only; fall back to all facts if (a misconfigured cap of 0) left none checked,
    # so a real article is never mislabelled a "forecast".
    scored = [(r, rated) for r, rated in zip(done, rated_flags) if r.checked] or list(
        zip(done, rated_flags)
    )
    score = score_article(
        [
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
            for r, rated in scored
        ],
        publisher_reputation=_rep(reputation, source),  # S4 #402: the outlet's own record → ceiling
        publisher_floor=(knobs.publisher_floor if knobs else None),
    )
    emit("scored", {"score": score.score, "band": score.band, "label": score.label})

    return ArticleAnalysis(
        url=url, source=source, title=page.title, language=language, image=page.image,
        date=page.date, facts=done, projections=proj_readings, score=score,
        publisher_score=_rep(reputation, source), live=live, dropped_claims=dropped,
        merged_claims=merged,
    )


def check_one_claim(
    claim: Claim,
    *,
    body: str,
    source: str,
    url: str,
    reputation: Mapping[str, float],
    ownership: dict[str, str] | None = None,
    web_search: WebSearchFn | None = None,
    nli: NliFn | None = None,
    search: SearchFn | None = None,
    accept_candidate: Callable[[LiveCandidate], bool] | None = None,
    fetch: Callable[[str], FetchedPage | None] = fetch_page,
    extract: Callable[..., list[Claim]] = extract_claims,
    extremity_of: Callable[[str], str] = rate_extremity,
    embed: Callable[[list[str]], list[list[float]]] = mistral_embed,
    same_fact_threshold: float = SAME_FACT_THRESHOLD,
    nli_entail_min: float = 0.5,
    nli_contradict_min: float = 0.6,
    live_max_candidates: int = 18,
    knobs: ScoringKnobs | None = None,
) -> tuple[ClaimReading, bool]:
    """The reader's on-demand force-check of ONE claim (#397): the SAME web-search → verify → NLI →
    fold journey a batch claim takes inside ``analyse_article``, run standalone for a single claim
    whose search was skipped over the cap. Returns (reading, rated); the reading is ``checked=True``
    (a real check happened) whatever it finds — "Only this source so far" now means we looked.

    Reuses the batch helpers verbatim (``_websearch_rows``/``_apify_rows``/``_live_reading``) so the
    forced path and the batch path can never drift apart in how they judge or fold evidence —
    including the promoted scoring ``knobs`` (#412)."""
    own_canon = canonical_source(source)
    extremity = extremity_of(claim.text)
    cite_fetch = _CiteFetch(fetch)
    matched_rows: list[ClaimRow] = []
    bodies: dict[str, str] = {}
    weights: dict[str, float] = {}
    contradicted = False

    if web_search is not None:
        try:
            results = web_search([claim.text], source)
        except Exception as e:  # noqa: BLE001 - a failed pass falls back to Apify below
            log.warning("force-check web-search failed: %s", type(e).__name__)
            results = []
        cits = results[0] if results else []
        if cits:
            matched_rows, bodies, contradicted, weights = _websearch_rows(
                0, claim, cits, url=url, own_canon=own_canon,
                accept_candidate=accept_candidate, cite_fetch=cite_fetch, nli=nli,
                nli_min=nli_entail_min, contradict_min=nli_contradict_min,
                entail_floor=(knobs.entail_floor if knobs else None),
            )
    if not matched_rows and search is not None:  # web-search found nothing usable → Apify fallback
        matched_rows, bodies = _apify_rows(
            0, claim, search=search, extract=extract, embed=embed,
            shared=_LiveShared(live_max_candidates), accept_candidate=accept_candidate,
            own_canon=own_canon, url=url, same_fact_threshold=same_fact_threshold,
        )
    dispute = contradicted and not matched_rows
    return _live_reading(
        claim, body, source, extremity, matched_rows, bodies, reputation,
        disputed=dispute, ownership=ownership, cite_weights=weights, knobs=knobs,
    )
