"""Google Fact Check Tools acquisition (P16 #451) — professional fact-checks as evidence.

Rumour-shaped claims are exactly what fact-checkers (AFP, Snopes, Reuters Fact Check, PolitiFact,
Full Fact, …) cover, and their published ClaimReview records carry three things the claim-mode
braid wants: a same-fact match target (the claim as THEY state it), a rating (their verdict), and
provenance seeds (who first made the claim, and when — feeds the origin trace, #452).

This module is the thin connector: query the Fact Check Tools ``claims:search`` API by claim
text, parse the ClaimReview shape, and normalise the free-text ``textualRating`` ("False",
"Pants on Fire!", "Mostly true", …) into a closed polarity set. The PIPELINE decides what a
fact-check is worth — the same-fact gate is NLI (their claim text vs ours), never this module's
say-so, and a rating only moves a verdict after that gate passes (#451).

No API key → the leg is OFF (callers get a None factory), recorded in the run's coverage meta —
never a silent degradation. Requires only the free Fact Check Tools key (MAAT_FACTCHECK_KEY).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("maat.acquire.factcheck")

API = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
_UA = "maat-acquire/0.1 (veracity research)"
_TIMEOUT = 12.0

# ``textualRating`` is free text in many languages; these are the high-precision English cores
# fact-checkers actually use (ClaimReview's own guidance encourages a small vocabulary). A rating
# that matches neither family is "unclear" and NEVER moves a verdict — precision over recall.
_FALSE_MARKERS = (
    "false", "fake", "pants on fire", "incorrect", "wrong", "fabricated", "hoax", "scam",
    "no evidence", "unfounded", "unsupported", "not true", "untrue", "misleading", "distorts",
    "debunked", "faux", "falso", "falsch",
)
_TRUE_MARKERS = ("true", "correct", "accurate", "verified", "vrai", "verdadero", "wahr")
_MIXED_MARKERS = ("mixed", "mixture", "partly", "partially", "half", "mostly false",
                  "mostly true", "needs context", "missing context", "unproven", "unverified")


def rating_polarity(textual_rating: str) -> str:
    """One of "false" | "true" | "mixed" | "unclear" for a free-text ClaimReview rating.

    Mixed markers are checked FIRST — "mostly true" contains "true", and a mixed verdict must
    never read as a clean confirmation (or a clean refutation)."""
    r = (textual_rating or "").casefold().strip()
    if not r:
        return "unclear"
    if any(m in r for m in _MIXED_MARKERS):
        return "mixed"
    if any(m in r for m in _FALSE_MARKERS):
        return "false"
    if any(m in r for m in _TRUE_MARKERS):
        return "true"
    return "unclear"


@dataclass(frozen=True)
class FactCheck:
    """One published fact-check of one claim (flattened ClaimReview)."""

    claim_text: str        # the claim as the fact-checker states it — the same-fact match target
    claimant: str          # who made the claim, per the fact-checker ("" unknown) — origin seed
    claim_date: str        # when it was made (ISO, "" unknown) — origin seed (#452)
    publisher: str         # fact-checker name ("AFP Fact Check")
    site: str              # fact-checker site ("factcheck.afp.com")
    review_url: str
    review_title: str
    review_date: str
    rating: str            # raw textualRating
    polarity: str          # rating_polarity(rating)


def parse_claims(data: dict) -> list[FactCheck]:
    """Flatten a ``claims:search`` response — one FactCheck per (claim, review) pair, skipping
    entries without the fields the braid needs (claim text + a review URL)."""
    out: list[FactCheck] = []
    for claim in data.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        text = (claim.get("text") or "").strip()
        if not text:
            continue
        for review in claim.get("claimReview") or []:
            if not isinstance(review, dict):
                continue
            url = (review.get("url") or "").strip()
            if not url:
                continue
            pub = review.get("publisher") or {}
            rating = (review.get("textualRating") or "").strip()
            out.append(FactCheck(
                claim_text=text,
                claimant=(claim.get("claimant") or "").strip(),
                claim_date=(claim.get("claimDate") or "").strip(),
                publisher=(pub.get("name") or "").strip(),
                site=(pub.get("site") or "").strip() or _site_of(url),
                review_url=url,
                review_title=(review.get("title") or "").strip(),
                review_date=(review.get("reviewDate") or "").strip(),
                rating=rating,
                polarity=rating_polarity(rating),
            ))
    return out


def _site_of(url: str) -> str:
    try:
        return httpx.URL(url).host or ""
    except Exception:  # noqa: BLE001 - a bad review URL just loses its site label
        return ""


def search_fact_checks(
    query: str, *, key: str, language: str | None = None,
    max_results: int = 10, timeout: float = _TIMEOUT,
) -> list[FactCheck]:
    """Published fact-checks matching ``query`` (best-effort — an API failure returns [], logged;
    one dead leg never sinks an analysis). ``language`` narrows to a BCP-47 code when the claim's
    language is known; the API still returns cross-language matches on its own translations."""
    params: dict[str, str] = {"query": query.strip()[:256], "key": key,
                              "pageSize": str(max_results)}
    if language and language != "unknown":
        params["languageCode"] = language
    t0 = time.monotonic()
    try:
        r = httpx.get(API, params=params, headers={"User-Agent": _UA}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("fact-check search failed after %.1fs: %s",
                    time.monotonic() - t0, type(e).__name__)
        return []
    got = parse_claims(data if isinstance(data, dict) else {})
    log.info("fact-check search %r -> %d reviews in %.1fs",
             query[:60], len(got), time.monotonic() - t0)
    return got
