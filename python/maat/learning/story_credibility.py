"""Story credibility — one legible score per STORY, rolled up from everything we know (#264).

People read news as stories, not claims, so the story feed needs a single number. Maat's internal
model is deliberately multi-dimensional (never one magic number), so this is the PRODUCT-facing
roll-up: one score, derived transparently from the dimensions, with the breakdown one tap away.

Model (locked with cauri):
  * HEADLINE-ANCHORED — a story is about as credible as its central, best-corroborated FACT;
    strongly-corroborated supporting facts nudge it up, thin tangents don't drag it down.
  * REPUTATION-WEIGHTED ORIGINATORS — each independent originator counts toward corroboration in
    proportion to its source's truth-over-time reputation (#192). Folded INTO the corroboration
    (an effective originator count fed to the SAME confidence_read, §5.6), not a separate multiplier.
  * NEUTRAL, CAPPED COLD-START — an unrated source is neutral, never penalised (the first to break a
    true story is often unknown, §6.6); but a story carried ONLY by unproven sources is CAPPED below
    "strongly established" — corroboration among unproven outlets can't reach the top band.
Plus: projections (forecasts/opinions, §5.3) never feed the truth score; a disputed/refuted core
claim (#229) penalises and flags; primary grounding (#228) flows through confidence_read.

Pure — no DB, no I/O. Feed it cluster views + a {source: reputation} map (rated sources only).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from maat.pipeline.corroborate import confidence_read

# One independent-originator group counts this much, by its sources' reputation (#192). Cold-start
# (no rated sources) is NEUTRAL via _NEUTRAL_REP, never zero. Range [0.6, 1.0].
_NEUTRAL_REP = 0.5
_WEIGHT_FLOOR = 0.6


def _group_weight(rep: float) -> float:
    return _WEIGHT_FLOOR + (1.0 - _WEIGHT_FLOOR) * max(0.0, min(1.0, rep))


_COLD_START_CAP = 0.70       # ceiling for a story on only-unproven carriers (cauri: neutral, capped)
_DISPUTED_PENALTY = 0.30     # a refuted/contradicted core claim (#229)
_SUPPORT_BONUS = 0.03        # per strongly-corroborated supporting fact …
_SUPPORT_BONUS_CAP = 0.09    # … up to this much
_STRONG_SUPPORT = 0.70       # a supporting fact counts as "strong" at/above this confidence

# (min_score_inclusive, key, label) — first match wins, descending.
_BANDS = [
    (85, "established", "Strongly established"),
    (65, "corroborated", "Corroborated"),
    (45, "developing", "Developing"),
    (25, "thin", "Thinly sourced"),
    (0, "single", "Single source / unverified"),
]


@dataclass(frozen=True)
class FactView:
    """One FACT cluster in a story (projections are excluded by the caller)."""
    confidence: float                         # the cluster's own §5.6 read (for the support test)
    independent_originators: int
    has_primary: bool
    extremity: str = "notable"
    originator_sources: list[list[str]] = field(default_factory=list)  # source names per group
    grounding: str | None = None              # #228: "supported" | "not_addressed" | "contradicted"
    disputed: bool = False                    # #229: a stronger contradicting claim refuted this


@dataclass(frozen=True)
class StoryScore:
    score: int           # 0..100 — the headline number
    band: str            # band key (see _BANDS), or "disputed" / "forecast"
    label: str           # human label
    why: list[str]       # the transparent drivers behind the number
    capped: bool         # the cold-start ceiling bit
    forecast_only: bool  # the story is all forecast/opinion — not a truth score


def band_for(score: int) -> tuple[str, str]:
    for lo, key, label in _BANDS:
        if score >= lo:
            return key, label
    return _BANDS[-1][1], _BANDS[-1][2]


def _effective_originators(groups: list[list[str]], rep: Mapping[str, float]) -> tuple[float, int]:
    """Reputation-weighted originator count, and how many groups had a RATED source. A group with
    no rated source contributes a neutral weight (cold-start) and counts 0 toward `rated`."""
    total = 0.0
    rated = 0
    for grp in groups:
        scores = [rep[s] for s in grp if s in rep]
        if scores:
            rated += 1
            total += _group_weight(sum(scores) / len(scores))
        else:
            total += _group_weight(_NEUTRAL_REP)
    return total, rated


def score_story(facts: list[FactView], reputation: Mapping[str, float]) -> StoryScore:
    """Roll a story's FACT clusters into one 0..100 credibility score (see module docstring)."""
    if not facts:
        return StoryScore(0, "forecast", "Forecast / opinion", ["no checkable facts yet"], False, True)

    # Headline = the most-corroborated core fact; the story is anchored on it.
    headline = max(facts, key=lambda f: f.independent_originators)
    eff, rated = _effective_originators(headline.originator_sources, reputation)
    base = confidence_read(eff, headline.has_primary, headline.extremity, grounding=headline.grounding)

    why: list[str] = []
    n_ind = headline.independent_originators
    why.append(f"{n_ind} independent originator{'s' if n_ind != 1 else ''}"
               + (f", {rated} with a track record" if rated else ""))

    # Supporting facts can nudge UP (never drag a solid headline down).
    strong = sum(1 for f in facts if f is not headline and f.confidence >= _STRONG_SUPPORT)
    if strong:
        base = min(0.99, base + min(_SUPPORT_BONUS * strong, _SUPPORT_BONUS_CAP))
        why.append(f"{strong} corroborating fact{'s' if strong != 1 else ''} in the story")

    if headline.has_primary and headline.grounding == "supported":
        why.append("backed by a primary source")
    if headline.extremity in ("significant", "extraordinary"):
        why.append(f"{headline.extremity} claim — bar raised")

    # Cold-start cap: only-unproven carriers can't reach "strongly established" (neutral, capped).
    capped = False
    if all(_effective_originators(f.originator_sources, reputation)[1] == 0 for f in facts):
        if base > _COLD_START_CAP:
            base = _COLD_START_CAP
            capped = True
            why.append("carriers not yet proven — capped")

    # Dispute/refutation of a core claim (#229): penalise hard and override the band.
    disputed = any(f.disputed for f in facts) or headline.grounding == "contradicted"
    if disputed:
        base = max(0.0, base - _DISPUTED_PENALTY)
        score = round(base * 100)
        why.insert(0, "a core claim is disputed by a stronger contradicting claim")
        return StoryScore(score, "disputed", "Disputed", why, capped, False)

    score = round(max(0.0, min(1.0, base)) * 100)
    key, label = band_for(score)
    return StoryScore(score, key, label, why, capped, False)
