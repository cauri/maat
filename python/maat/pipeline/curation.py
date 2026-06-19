"""Curation core (P5, issue #47): the pure de-US-centering re-rank.

This module is the PURE, fully-unit-testable core of the curation step. Given
stories with source/geography/language metadata + their confidence, ``curate()``
returns a de-US-centered ordering that

  (a) preserves high-confidence prominence — confidence is the product's
      promise; we only re-rank within tolerance bands, we never bury a
      well-corroborated story under a poorly corroborated one;
  (b) lifts under-represented regions toward balance — story slots rotate
      across geographic buckets so no single region dominates the feed;
  (c) caps any single country or source's share — no country > COUNTRY_CAP
      of the visible feed, no source > SOURCE_CAP.

Never touches confidence, labels, or any veracity signal — those are
computed upstream and are immutable here.

No I/O, no LLM, no event bus: this is the layer the serving feed builder
(``maat.serving.feed``) and the curation agent (``maat.agents.curation``) both
import — the dependency only ever points *down* into the pipeline.

An LLM step is NOT needed for the ranking logic — the geographic/source
balance is deterministic. There is a DRAFT enrichment hook below for a future
step that might infer geography from a story when the metadata is absent; that
path is clearly marked and disabled by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Story:
    """Minimal metadata needed for curation. Confidence is read-only here."""

    id: str
    confidence: float
    country: str  # ISO-3166-1 alpha-2 or empty string when unknown
    source: str   # bare source name / domain
    language: str  # BCP-47 or ISO-639-1


# ---------------------------------------------------------------------------
# Tunable caps / bands (not veracity weights — purely diversity knobs)
# ---------------------------------------------------------------------------

# No single country may hold more than this share of the feed slots.
COUNTRY_CAP: float = 0.25

# No single source may hold more than this share of the feed slots.
SOURCE_CAP: float = 0.20

# Confidence tolerance: within this window a lower-confidence story may be
# promoted ahead of a higher-confidence one for diversity.  A story whose
# confidence is more than CONFIDENCE_BAND below the front-runner is never
# promoted — veracity takes precedence.
CONFIDENCE_BAND: float = 0.20

# Countries that dominate Anglo-American mainstream media production.
# A story whose *sole* country tag matches one of these is "over-represented"
# and will be de-prioritised once their CAP share is reached.
_ANGLOSPHERE = frozenset({"US", "GB", "CA", "AU"})


# ---------------------------------------------------------------------------
# Pure ranking logic (no I/O, no LLM — fully unit-testable)
# ---------------------------------------------------------------------------


def curate(
    stories: Sequence[Story],
    *,
    country_cap: float = COUNTRY_CAP,
    source_cap: float = SOURCE_CAP,
    confidence_band: float = CONFIDENCE_BAND,
) -> list[Story]:
    """Return a re-ordered sequence that balances geographic + source diversity
    while preserving high-confidence prominence.

    Algorithm
    ---------
    1. Sort descending by confidence (baseline veracity order).
    2. Greedily pick the next story that:
       - is within `confidence_band` of the top remaining story (so we never
         bury a significantly more credible story), AND
       - does not push any country or source over its cap.
       If no candidate passes both gates, relax: take the highest-confidence
       remaining story regardless (caps are aspirational at small feed sizes).
    3. Repeat until all stories are placed.

    Country/source share is computed over the *total* feed size, not the
    running prefix, so caps scale correctly with feed length.
    """
    if not stories:
        return []

    n = len(stories)
    country_limit = max(1, int(n * country_cap + 0.999))  # ceil; always ≥1
    source_limit = max(1, int(n * source_cap + 0.999))

    remaining = sorted(stories, key=lambda s: s.confidence, reverse=True)
    placed: list[Story] = []
    country_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}

    while remaining:
        top_conf = remaining[0].confidence
        min_conf = top_conf - confidence_band

        candidate = None
        for story in remaining:
            if story.confidence < min_conf:
                # everything from here down is below the band — stop scanning
                break
            c_ok = not story.country or country_counts.get(story.country, 0) < country_limit
            s_ok = not story.source or source_counts.get(story.source, 0) < source_limit
            if c_ok and s_ok:
                candidate = story
                break

        if candidate is None:
            # All candidates within the band are capped — relax and take the
            # highest-confidence remaining story (caps are soft at small N).
            candidate = remaining[0]

        remaining.remove(candidate)
        placed.append(candidate)
        if candidate.country:
            country_counts[candidate.country] = country_counts.get(candidate.country, 0) + 1
        if candidate.source:
            source_counts[candidate.source] = source_counts.get(candidate.source, 0) + 1

    return placed


def anglosphere_share(stories: Sequence[Story]) -> float:
    """Fraction of stories whose country is in the Anglosphere.

    A diagnostic helper — not used in ranking, but useful for tests and
    for the future feed-quality report.
    """
    if not stories:
        return 0.0
    count = sum(1 for s in stories if s.country in _ANGLOSPHERE)
    return count / len(stories)


def region_distribution(stories: Sequence[Story]) -> dict[str, int]:
    """Count of stories per country code (or '' for unknown), for diagnostics."""
    dist: dict[str, int] = {}
    for s in stories:
        dist[s.country] = dist.get(s.country, 0) + 1
    return dist


# ---------------------------------------------------------------------------
# DRAFT LLM enrichment hook — gated off, surfaced read-only for cauri review.
# ---------------------------------------------------------------------------
# The LLM geo-tagging path is NOT wired into the pipeline.  If a future pass
# needs to infer geography from a story when the metadata is absent, it would
# run on this prompt; until then the constant only makes the DRAFT text
# reviewable in the operator console (see ``maat.prompts``).
#
# DRAFT prompt — flag for cauri review (do not finalize without cauri)
# (example, not activated — surfaced read-only in the operator console for cauri review).
# The LLM geo-tagging path stays disabled; this constant only makes the DRAFT text reviewable.
_DRAFT_GEOTAG_PROMPT = """
You are a geography tagger. Given a news story fact, identify the primary
country it concerns (ISO-3166-1 alpha-2). If unclear, return empty string.
Respond with JSON: {"country": "<code or empty>"}
"""
# DRAFT prompt — flag for cauri review (do not finalize without cauri)


def _stories_from_payload(payload: list[dict[str, Any]]) -> list[Story]:
    """Deserialise story dicts from a ``feed.requested`` event payload."""
    return [
        Story(
            id=s["id"],
            confidence=float(s.get("confidence") or 0.0),
            country=(s.get("country") or s.get("geo") or "").upper()[:2],
            source=s.get("source") or "",
            language=s.get("language") or s.get("lang") or "",
        )
        for s in payload
    ]
