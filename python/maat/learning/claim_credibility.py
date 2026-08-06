"""Claim credibility — one legible score for reader-typed claim(s) (P16 #449, #450).

Article mode asks "can I trust what this article tells me?"; claim mode asks "does this claim
hold up?". Every normalised claim is CENTRAL by construction — the reader asked about exactly it —
so the roll-up delegates to ``article_credibility``'s weakest-central-link + disqualifier
machinery with NO publisher axis: there is no carrying outlet (the reader is asking, not
publishing), so nothing lifts or caps the score but the evidence itself — corroboration by a
proven originator lifts the cold-start ceiling exactly as in article mode (``rated_originator``),
and ``publisher_reputation=None`` keeps the only-unproven-carriers cap.

The NUMBER and BANDS are the same paradigm as every Maat surface; only the WORDS change, because
an article and a claim fail differently: an article "Fails verification"; a claim is "Refuted"
(contradicted by the primary source or independent reporting) or has "No credible support" (an
extraordinary claim resting on nothing). A zero-evidence claim reads "No independent support
found" — never "Single source", which would imply somebody published it. Pure — no DB, no I/O.
"""

from __future__ import annotations

import re

from maat.learning.article_credibility import ArticleClaim, score_article
from maat.learning.story_credibility import StoryScore

# Disqualifier rewording: article_credibility phrases its reasons for an article ("a central
# claim is …"); a claim-mode reader asked about THE claim itself. Patterns match the exact
# reason shapes ``_disqualifiers`` emits — a new disqualifier there surfaces here unreworded
# (honest, just article-phrased) rather than silently dropped. (singular, plural) per pattern.
_REWORD = [
    (re.compile(r"^a central claim is contradicted by the primary source$"),
     ("contradicted by the primary source", "one claim is contradicted by the primary source")),
    (re.compile(r"^a central claim is contradicted by stronger reporting$"),
     ("contradicted by independent reporting",
      "one claim is contradicted by independent reporting")),
    (re.compile(r"^a central (extraordinary) claim rests on a single unsupported source$"),
     (r"an \1 claim with no independent support", r"an \1 claim with no independent support")),
    (re.compile(r"^a central (\w+) claim rests on a single unsupported source$"),
     (r"a \1 claim with no independent support", r"a \1 claim with no independent support")),
]


def _reworded(reasons: list[str], plural: bool) -> list[str]:
    out: list[str] = []
    for reason in reasons:
        for rx, (one, many) in _REWORD:
            if rx.match(reason):
                out.append(rx.sub(many if plural else one, reason))
                break
        else:
            out.append(reason)
    return out


def _claim_why(why: list[str], plural: bool) -> list[str]:
    """The normal-band drivers, reworded from article language to claim language."""
    subject = "weakest claim" if plural else "the claim"
    out: list[str] = []
    for w in why:
        w = w.replace("weakest central claim", subject)
        w = w.replace("central claim — bar raised", "claim — bar raised")
        w = w.replace("elsewhere in the article", "among the claims")
        # Zero originators is NOT "a single source" — nobody published it at all.
        w = w.replace("0 independent originators (single source)", "0 independent originators")
        out.append(w)
    return out


def score_claims(claims: list[ArticleClaim]) -> StoryScore:
    """Roll reader-typed claim(s) into one 0..100 credibility read (see module docstring).

    The caller passes every checkable claim with ``central=True`` (decomposed claims are all
    load-bearing: the overall anchors on the weakest, as an article does on its weakest central
    claim) and excludes projections/opinions — no checkable claim at all → the forecast band.
    """
    plural = len(claims) > 1
    base = score_article(claims, publisher_reputation=None)

    if base.forecast_only:
        return StoryScore(
            0, "forecast", "Nothing to check — opinion or forecast",
            ["no checkable factual claim"], False, True,
        )

    if base.band == "disqualified":
        contradicted = any("contradicted" in r for r in base.why)
        label = "Refuted" if contradicted else "No credible support"
        return StoryScore(
            base.score, "disqualified", label, _reworded(list(base.why), plural),
            base.capped, False,
        )

    label = base.label
    why = _claim_why(list(base.why), plural)
    central = [c for c in claims if c.central] or claims
    if base.band == "single" and all(c.independent_originators == 0 for c in central):
        # Nobody published this at all — "Single source" would invent one.
        label = "No independent support found"
        if all(c.fresh_absence for c in central):
            # #454 — hours old: absence is expected, not damning. Say so.
            label = "Too early to tell"
            why.append("first seen only hours ago — too early for independent reporting")
    return StoryScore(base.score, base.band, label, why, base.capped, False)
