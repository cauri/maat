"""Source-learning loop — acquisition preferences from source reputation (P3, §5 — Issue #35).

Given SourceReputation records (folded from the event log by reputation.py), produce a
ranked preference list and per-source acquisition weights to steer acquisition toward
sources that *prove* reliable — without collapsing into the loudest few or amplifying
Anglo-American slant.

Design principles (from issue #35 and cauri's notes):

1. REWARD signal: high independent-originator rate, good attribution quality, high confirmation
   rate. These are truth-over-time signals, not popularity signals.

2. ANTI-ECHO-CHAMBER guard: acquisition weight is capped so no single source dominates, and
   a diversity floor ensures sufficient geographic / source variety is maintained.  Sources
   that are reliable AND independent corroborators are preferred over prolific-but-derivative
   ones.

3. NO amplifying volume: a source with many appearances but poor independent_rate or low
   confirmation_rate must NOT rank highly.  The raw appearance count is ignored as a positive
   signal; it is only used to discount sources with too little evidence.

4. SOLO-EXTRAORDINARY penalty: a source that repeatedly stands alone on significant/
   extraordinary claims is red-flagged — it may be breaking news first, or fabricating.  Until
   context resolves the ambiguity, its weight is softened.

5. MIN-EVIDENCE threshold: a source with fewer than `min_appearances` total appearances gets
   a conservative weight (not zero — we don't want to silence new voices) but does not
   benefit from the full reliability signal.

Pure functions over plain values — no DB, no I/O. Feed from scripts or tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable

from maat.learning.reputation import SourceReputation

# ---------------------------------------------------------------------------
# Tuneable constants
# ---------------------------------------------------------------------------

# Minimum appearances before we trust the reliability signal.
_MIN_EVIDENCE = 5

# Maximum acquisition weight any single source may receive (diversity cap).
# A source cannot exceed this share regardless of how good its reputation is.
_MAX_WEIGHT = 0.30

# Floor weight assigned to any source that passes the diversity gate.  Even a
# source with very low reliability gets a small non-zero weight so new voices
# can accumulate evidence.
_FLOOR_WEIGHT = 0.02

# Number of sources in the minimum-diversity set.  The acquisition engine MUST
# surface articles from at least this many distinct sources over any rolling
# window (enforced at the caller; surfaced here as a constant for tests).
DIVERSITY_MINIMUM = 8

# Penalty factor applied to a source whose solo_extraordinary count is above
# this threshold relative to its independent_appearances.
_SOLO_PENALTY_THRESHOLD = 0.25  # > 25 % of independent appearances are solo-extraordinary
_SOLO_PENALTY_FACTOR = 0.60     # weight is multiplied by this if the flag fires

# Bonus factor for independent corroborators: a source that often appears as an
# independent voice (not a cascade node) on CONFIRMED facts is the behaviour we
# most want to reward.
_INDEPENDENT_CORROBORATOR_BONUS = 1.20

# Evidence discount: sources below the min-evidence threshold are treated as if
# they had a neutral reputation (0.50 confirmation rate, 0.50 independent rate).
_NEUTRAL_CONFIRMATION_RATE = 0.50
_NEUTRAL_INDEPENDENT_RATE = 0.50

# Confirmation rate used when a source has sufficient appearances but ZERO terminal outcomes
# (all facts still in flight).  These sources are genuinely unproven — they rank below sources
# that have at least some confirmed facts, so we use a rate below neutral.
_UNPROVEN_CONFIRMATION_RATE = 0.35


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourcePreference:
    """A single entry in the ranked source-preference list.

    Fields are intentionally not collapsed into a single score — each dimension
    is surfaced so the operator (and tests) can reason about WHY a source ranks
    here and what drove its weight.
    """

    source: str

    # Rank position (1-based, ascending preference — 1 = most preferred).
    rank: int

    # Normalised acquisition weight in [_FLOOR_WEIGHT, _MAX_WEIGHT].
    # Weights across ALL returned preferences sum to ≤ 1.0 (they are normalised
    # after capping so the engine can use them as a probability distribution).
    acquisition_weight: float

    # Signals that drove the weight (surfaced for auditability, not for the engine).
    confirmation_rate: float | None    # from SourceReputation; None = no terminal outcomes
    independent_rate: float            # from SourceReputation
    mean_attribution_weight: float     # from SourceReputation
    solo_extraordinary_flag: bool      # True = solo-penalty was applied
    low_evidence: bool                 # True = below min-evidence threshold

    # Whether this source is part of the mandatory diversity floor.
    in_diversity_floor: bool


@dataclass(frozen=True)
class SourcePreferences:
    """The full preference list + per-source weight map returned by `learn_preferences`.

    ranked: ordered list, most-preferred first.
    weights: {source: acquisition_weight} for O(1) lookup.
    diversity_floor: the set of sources the engine MUST draw from to avoid echo-chamber
        collapse (subset of ranked; lowest-weight sources the cap would otherwise squeeze).
    """

    ranked: list[SourcePreference]
    weights: dict[str, float]
    diversity_floor: frozenset[str]


# ---------------------------------------------------------------------------
# Internal scoring helpers
# ---------------------------------------------------------------------------


def _raw_score(rep: SourceReputation, *, min_evidence: int = _MIN_EVIDENCE) -> float:
    """Compute an un-normalised preference score for one source.

    Score = (confirmation_weight × confirmation_rate) + (independence_weight × independent_rate)
             + (attribution_weight × mean_attribution_weight)

    Where the weights sum to 1 and reflect the relative importance of each signal:
    - Outcome accuracy is the primary signal (proven reliable over time).
    - Independent-originator rate is the secondary signal (we want corroborators, not
      cascade nodes; this is also the anti-echo-chamber lever).
    - Attribution quality is a tertiary signal.

    Sources below min_evidence get neutral rates — they haven't proven themselves yet
    but shouldn't be silenced.
    """
    low_ev = rep.appearances < min_evidence

    if low_ev:
        cr = _NEUTRAL_CONFIRMATION_RATE
        ir = _NEUTRAL_INDEPENDENT_RATE
    elif rep.confirmation_rate is None:
        # Sufficient appearances but no terminal outcomes at all: the source's facts are all still
        # in flight.  That is not neutral — it's unproven.  Use a below-neutral rate so these
        # sources rank below any source that has at least some confirmed facts.
        cr = _UNPROVEN_CONFIRMATION_RATE
        ir = rep.independent_rate
    else:
        cr = rep.confirmation_rate
        ir = rep.independent_rate

    attr = rep.mean_attribution_weight  # already in [0, 1]

    # Weights: confirmation 50 %, independence 35 %, attribution 15 %.
    score = 0.50 * cr + 0.35 * ir + 0.15 * attr
    return score


def _solo_extraordinary_ratio(rep: SourceReputation) -> float:
    """Fraction of independent appearances that were solo on significant/extraordinary claims."""
    if rep.independent_appearances == 0:
        return 0.0
    return rep.solo_extraordinary / rep.independent_appearances


def _apply_bonuses_and_penalties(score: float, rep: SourceReputation) -> tuple[float, bool]:
    """Apply multiplicative adjustments; return (adjusted_score, solo_flag)."""
    solo_flag = False

    # Independent-corroborator bonus: confirmed facts with high independent_rate.
    # We want to amplify the signal for sources that break news AND get confirmed,
    # not just sources that re-report confirmed news from others.
    if (
        rep.confirmation_rate is not None
        and rep.confirmation_rate >= 0.70
        and rep.independent_rate >= 0.60
    ):
        score *= _INDEPENDENT_CORROBORATOR_BONUS

    # Solo-extraordinary penalty: a source with too many solo claims on extreme topics
    # is a red flag until those claims resolve.
    if _solo_extraordinary_ratio(rep) > _SOLO_PENALTY_THRESHOLD:
        score *= _SOLO_PENALTY_FACTOR
        solo_flag = True

    return score, solo_flag


def _normalise_weights(
    raw_weights: dict[str, float],
    *,
    floor: float = _FLOOR_WEIGHT,
    cap: float = _MAX_WEIGHT,
) -> dict[str, float]:
    """Normalise raw weights: apply floor, cap, then rescale to sum ≤ 1.0.

    Steps:
    1. Lift any weight below `floor` to `floor` — every source gets some share.
    2. Cap any weight above `cap` — no source dominates.
    3. Rescale so the sum is ≤ 1.0.  (We allow < 1.0 if no sources are present;
       the engine can treat the remainder as an "explore unknown sources" budget.)
    """
    if not raw_weights:
        return {}

    # Apply floor and cap.
    bounded = {s: max(floor, min(cap, w)) for s, w in raw_weights.items()}

    total = sum(bounded.values())
    if total == 0:
        n = len(bounded)
        return {s: 1.0 / n for s in bounded}

    # Rescale.
    scale = min(1.0, 1.0 / total) if total > 1.0 else 1.0
    return {s: round(w * scale, 4) for s, w in bounded.items()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def learn_preferences(
    reputations: Iterable[SourceReputation],
    *,
    min_evidence: int = _MIN_EVIDENCE,
    max_weight: float = _MAX_WEIGHT,
    floor_weight: float = _FLOOR_WEIGHT,
    diversity_minimum: int = DIVERSITY_MINIMUM,
) -> SourcePreferences:
    """Map SourceReputation records to a ranked source-preference list + acquisition weights.

    Returns a SourcePreferences with:
    - ranked: sources ordered most-preferred first;
    - weights: per-source acquisition weight, normalised, capped, floored;
    - diversity_floor: the minimum set of sources the engine MUST draw from.

    Anti-echo-chamber guarantee:
    - No single source weight exceeds `max_weight`.
    - At least `diversity_minimum` distinct sources appear in the output, regardless
      of how many sources there are (if there are fewer than that, all are included).
    - The diversity_floor set marks the minimum set; sources outside the floor are
      still in the output — the floor is the minimum, not the total.

    Idempotent and pure — same input always produces the same output.
    """
    reps = list(reputations)
    if not reps:
        return SourcePreferences(ranked=[], weights={}, diversity_floor=frozenset())

    # --- Score every source ---
    scores: dict[str, float] = {}
    solo_flags: dict[str, bool] = {}
    low_ev_flags: dict[str, bool] = {}

    for rep in reps:
        base = _raw_score(rep, min_evidence=min_evidence)
        adjusted, solo_flag = _apply_bonuses_and_penalties(base, rep)
        scores[rep.source] = adjusted
        solo_flags[rep.source] = solo_flag
        low_ev_flags[rep.source] = rep.appearances < min_evidence

    # --- Normalise weights ---
    normalised = _normalise_weights(scores, floor=floor_weight, cap=max_weight)

    # --- Sort by weight descending, then by raw score (breaks ties from the cap), then name ---
    # Using the raw pre-cap score as a tie-breaker ensures that when two sources are equalised
    # by the diversity cap, the one that genuinely earned a higher score ranks first.  The
    # source name is the final lexicographic tie-breaker so output is always deterministic.
    ordered = sorted(normalised.keys(), key=lambda s: (-normalised[s], -scores[s], s))

    # --- Determine diversity floor ---
    # The floor is the top-`diversity_minimum` sources by weight. But if there are
    # fewer total sources, include all of them.
    floor_count = min(diversity_minimum, len(ordered))
    floor_sources = frozenset(ordered[:floor_count])

    # --- Build ranked list ---
    rep_by_source = {r.source: r for r in reps}
    ranked: list[SourcePreference] = []
    for rank_idx, source in enumerate(ordered, start=1):
        rep = rep_by_source[source]
        ranked.append(
            SourcePreference(
                source=source,
                rank=rank_idx,
                acquisition_weight=normalised[source],
                confirmation_rate=rep.confirmation_rate,
                independent_rate=rep.independent_rate,
                mean_attribution_weight=rep.mean_attribution_weight,
                solo_extraordinary_flag=solo_flags[source],
                low_evidence=low_ev_flags[source],
                in_diversity_floor=source in floor_sources,
            )
        )

    return SourcePreferences(
        ranked=ranked,
        weights=normalised,
        diversity_floor=floor_sources,
    )


def preference_by_source(prefs: SourcePreferences) -> dict[str, SourcePreference]:
    """Index preference list by source name for O(1) lookup."""
    return {p.source: p for p in prefs.ranked}
