"""Article credibility — one legible score for a SINGLE analysed article (P14, #365, #366).

The story feed asks "is this corroborated fact true?" and anchors a story on its BEST-corroborated
fact (``learning.story_credibility``). The Analyse surface asks a different question — "can I trust
what THIS article is telling me?" — and an article is only as sound as its shakiest load-bearing
claim. So this roll-up is WEAKEST-CENTRAL-LINK, not headline-anchored, and a failed central claim is
DISQUALIFYING (locked with cauri, 2026-07-06).

Model (locked with cauri):
  * CENTRAL claims carry the score — a claim the article leads on (``in_headline``) or derives
    itself (``is_synthesis``). Peripheral facts nudge UP, never drag down; projections (§5.3) never
    feed the truth score at all (the caller passes facts only).
  * WEAKEST CENTRAL LINK anchors — the article is about as trustworthy as its shakiest central
    claim; one well-corroborated headline can't paper over a shaky co-central claim.
  * DISQUALIFYING central failures hard-floor to the bottom band, regardless of the rest (a bed of
    true context must not launder one bad load-bearing claim):
      - a central claim DISPUTED / contradicted by stronger reporting (#229), or
      - a central claim that is EXTRAORDINARY/significant yet rests on a SINGLE UNSUPPORTED source
        (the classic misinformation shape — the big claim IS the article, on thin sourcing).
  * BREAKING NEWS IS NOT PUNISHED — a merely-uncorroborated central claim of ROUTINE/ordinary
    extremity is not a failure, only not-yet-established; it lands in the normal low bands
    ("Single source / unverified"), never the disqualified floor. Extremity is the gate.
  * NEUTRAL, CAPPED COLD-START — unrated carriers are neutral, but an article carried only by
    unproven sourcing can't reach the top band (as in story_credibility).

Speaks the SAME number + bands as ``story_credibility`` — one paradigm across every Maat surface.
Pure — no DB, no I/O. DRAFT weights (mirrors story_credibility); tune on real articles (slice 6).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from maat.learning.story_credibility import StoryScore, band_for

_STRONG_SUPPORT = 0.70       # a supporting fact counts as "strong" at/above this confidence …
_SUPPORT_BONUS = 0.03        # … and nudges the score up this much each …
_SUPPORT_BONUS_CAP = 0.09    # … capped here (a solid supporting cast lifts, never rescues).
_COLD_START_CAP = 0.70       # only-unproven carriers can't reach "strongly established".
_PUB_CEILING_FLOOR = 0.5     # S4 #402: a PROVEN-WEAK publisher's article is bounded here — its own
#                              track record caps how far to trust it on uncorroborated say-so.
_DISQUALIFIED_CEILING = 20   # a disqualified article floors into the bottom band (cauri: disqualifying).
_BIG = ("significant", "extraordinary")


def _ceiling(any_rated_originator: bool, publisher_reputation: float | None) -> tuple[float, str]:
    """How high the article can score, and why (S4 #402). Corroboration by a proven OUTSIDE
    originator lifts the cold-start cap entirely; otherwise the PUBLISHER's own record bounds it —
    a proven-strong outlet up to the top, an unrated one at the cold-start cap, a proven-weak one
    below it (its record limits how far to trust its uncorroborated claims). This is a CEILING
    (bounds the top); the per-claim reputation weighting (S1) moves the base — different axes, so
    the two never double-count."""
    if any_rated_originator:
        return 1.0, ""
    if publisher_reputation is not None:
        rep = max(0.0, min(1.0, publisher_reputation))
        ceiling = round(_PUB_CEILING_FLOOR + (1.0 - _PUB_CEILING_FLOOR) * rep, 2)
        why = ("publisher has a strong track record" if ceiling >= _COLD_START_CAP
               else "publisher's own track record is weak — capped")
        return ceiling, why
    return _COLD_START_CAP, "carriers not yet proven — capped"


@dataclass(frozen=True)
class ArticleClaim:
    """One FACTUAL claim from the analysed article (the caller excludes projections).

    ``confidence`` is the claim's own §5.6 read — corpus-inherited (it clusters with an existing
    Maat fact) or scoped (``corroborate_fixed`` over live candidates); a lone claim reads as a
    single originator. ``rated_originator`` is True when at least one of its independent originators
    has a track record (drives the cold-start cap, so this module needs no reputation map).
    """

    confidence: float
    independent_originators: int
    has_primary: bool
    extremity: str = "notable"
    central: bool = False              # in_headline or is_synthesis — a load-bearing claim
    disputed: bool = False             # #229 — contradicted by a stronger claim
    grounding: str | None = None       # #228 — "supported" | "not_addressed" | "contradicted"
    rated_originator: bool = False
    why: list[str] = field(default_factory=list)


def _disqualifiers(central: list[ArticleClaim]) -> list[str]:
    """The disqualifying failures among the central claims (empty → none). Order-stable, deduped."""
    reasons: list[str] = []
    for c in central:
        if c.disputed or c.grounding == "contradicted":
            reasons.append("a central claim is contradicted by stronger reporting")
        elif c.extremity in _BIG and c.independent_originators <= 1 and not c.has_primary:
            reasons.append(f"a central {c.extremity} claim rests on a single unsupported source")
    return list(dict.fromkeys(reasons))


def score_article(
    claims: list[ArticleClaim], *, publisher_reputation: float | None = None
) -> StoryScore:
    """Roll an article's FACTUAL claims into one 0..100 credibility score (see module docstring).

    Projections are excluded by the caller; an article with no checkable fact is a forecast, not a
    truth score. ``publisher_reputation`` (S4 #402, the analysed outlet's own track record in [0,1],
    None = unrated) sets the article's ceiling when its claims aren't corroborated by a proven
    outside originator — a CEILING, distinct from the per-claim reputation weighting (S1) that moves
    the base, so the two never double-count."""
    facts = list(claims)
    if not facts:
        return StoryScore(0, "forecast", "No checkable factual claims",
                          ["nothing to verify yet"], False, True)

    # Central = what the article leads on / derives; fall back to its best-corroborated fact so the
    # score is always anchored on something even when nothing was flagged central.
    central = [c for c in facts if c.central] or [max(facts, key=lambda c: c.independent_originators)]

    # --- disqualifying central failures hard-floor the score (cauri: disqualifying) ---
    reasons = _disqualifiers(central)
    if reasons:
        # Keep a hair of signal from the weakest central claim so a wholly-unsupported piece still
        # reads below a merely-disputed-but-otherwise-sourced one — but always in the bottom band.
        weakest = min(central, key=lambda c: c.confidence)
        score = min(_DISQUALIFIED_CEILING, round(max(0.0, weakest.confidence) * 100))
        return StoryScore(score, "disqualified", "Fails verification", reasons, False, False)

    # --- weakest central link anchors the score ---
    weakest = min(central, key=lambda c: c.confidence)
    base = weakest.confidence
    why: list[str] = []
    n = weakest.independent_originators
    single = n <= 1 and not weakest.has_primary
    why.append(f"weakest central claim: {n} independent originator{'s' if n != 1 else ''}"
               + (" (single source)" if single else ""))

    # Supporting corroborated facts nudge UP (a bounded lift — never rescues a shaky central claim).
    strong = sum(1 for c in facts if c is not weakest and c.confidence >= _STRONG_SUPPORT)
    if strong:
        base = min(0.99, base + min(_SUPPORT_BONUS * strong, _SUPPORT_BONUS_CAP))
        why.append(f"{strong} corroborating fact{'s' if strong != 1 else ''} elsewhere in the article")

    if weakest.extremity in _BIG:
        why.append(f"{weakest.extremity} central claim — bar raised")

    # S4 #402 — the article's ceiling: corroboration by a proven OUTSIDE originator lifts it,
    # otherwise the publisher's own track record bounds it (see ``_ceiling``).
    ceiling, cap_why = _ceiling(any(c.rated_originator for c in facts), publisher_reputation)
    capped = False
    if base > ceiling:
        base, capped = ceiling, True
        if cap_why:
            why.append(cap_why)

    score = round(max(0.0, min(1.0, base)) * 100)
    key, label = band_for(score)
    return StoryScore(score, key, label, why, capped, False)
