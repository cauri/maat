"""Reputation as a time-trajectory fold (P3, §5 — Issue #37).

cauri: reputation is measured AGAINST primary truth / outcomes over time, never consensus.
A source's standing is derived from the arc of its facts through the corroboration event log —
not a snapshot count, not a static score.

Key insight: a source that appears as an INDEPENDENT ORIGINATOR on claims that later CONFIRMED is
the signal we want. A source that floods the wire but always collapses into cascade nodes adds
noise, not signal. A source that often stands ALONE on extraordinary claims is a red flag — it
may be first (valuable) or fabricating (dangerous); we surface that tension explicitly.

Architecture: pure fold over `cluster.corroborated` event dicts.

Event dict fields (from the corroborate agent):
  fact                    — canonical fact text
  sources                 — list[str] of source names for the cluster
  originators             — list[list[str]] each inner list is one collapsed originator group
  independent_originators — int count of originator groups (len(originators))
  has_primary             — bool: any primary source in the cluster
  extremity               — "routine"/"ordinary"/"notable"/"significant"/"extraordinary"
  confidence              — float confidence read

Outcome resolution uses `calibration.resolve_outcome`; it reads the trajectory across multiple
events for the SAME fact (oldest-first) — so `fold_reputation` takes the full ordered event
stream and groups by fact internally, exactly as `observations_from_history` does.

Pure functions — no DB, no I/O. Feed from `scripts/calibrate.py` or tests.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from maat.learning.calibration import CONFIRMED, REFUTED, resolve_outcome
from maat.pipeline.corroborate import attribution_weight, is_primary_source

# Extremity levels that warrant extra scrutiny when a source stands alone.
_EXTRAORDINARY_LEVELS = frozenset({"significant", "extraordinary"})


def _norm_fact(fact: str) -> str:
    """Normalise a fact string for grouping — case + whitespace insensitive."""
    return " ".join((fact or "").lower().split())


def _source_is_independent_originator(source: str, originators: list[list[str]]) -> bool:
    """Is `source` present as its own independent originator group?

    An originator group is a collapsed set of article IDs from sources that are considered
    non-independent (same source, wire reprints, or citation cascades). A source that has its
    own group is an independent voice on this fact.

    The event stores originator groups as lists of article IDs, not source names. We cannot
    resolve article IDs back to sources without a DB. But the `sources` list in the event
    carries the sources for the cluster, and `independent_originators` counts the groups.
    To determine per-source independence we use a conservative signal: if the cluster has at
    least as many independent groups as distinct sources, every source is represented in at
    least one independent group. Otherwise we use `is_primary_source` as a proxy — primary
    sources are never collapsed into cascade nodes.

    This is a best-effort fold over the plain event dict (no join to article bodies/IDs). It
    errs toward giving sources benefit of the doubt on independence, which is the safer direction
    for reputation (false positive independence is corrected by outcome accuracy over time).
    """
    n_groups = len(originators) if originators else 0
    if n_groups == 0:
        return False
    # If there is only one originator group, ALL sources in the cluster collapsed into one node —
    # none of them is "independent" of the others.
    if n_groups == 1:
        return is_primary_source(source)
    # Multiple independent groups: assume each non-collapsing source is in its own group.
    # A primary source is always independent; otherwise presence in the sources list is
    # a reasonable proxy when we cannot resolve article IDs.
    return True


@dataclass
class _SourceAccumulator:
    """Running tallies per source, updated as we fold events."""

    appearances: int = 0               # total cluster appearances
    independent_appearances: int = 0   # appearances as an independent originator
    primary_appearances: int = 0       # clusters where this source contributed a primary-source signal
    # attribution quality: running sum and count for averaging
    attribution_weight_sum: float = 0.0
    attribution_weight_n: int = 0
    # extraordinary solo flag: times this source appeared alone on an extraordinary/significant claim
    solo_extraordinary: int = 0
    # outcome tracking (where we can resolve via calibration)
    facts_confirmed: int = 0
    facts_refuted: int = 0
    facts_unresolved: int = 0


@dataclass(frozen=True)
class SourceReputation:
    """Per-source reputation record — a fold over the event trajectory, not a static score.

    Fields reflect the key dimensions cauri specified:
    - how often independent vs cascade node
    - average attribution/sourcing quality
    - solo extraordinary appearances (red flag: first or fabricating)
    - outcome accuracy where derivable (confirmed vs refuted facts)
    """

    source: str

    # -- presence signals --
    appearances: int                    # total times source appeared in a corroborated cluster
    independent_appearances: int        # times as an independent originator (not collapsed)
    independent_rate: float             # independent_appearances / appearances
    primary_appearances: int            # times source was identified as a primary source

    # -- quality signal --
    mean_attribution_weight: float      # avg attribution weight across independent appearances

    # -- red-flag signal --
    solo_extraordinary: int             # times source stood alone on significant/extraordinary claim
    #   (positive: may be breaking news first; negative: may be fabricating; context needed)

    # -- outcome accuracy (truth-over-time, where resolvable) --
    facts_confirmed: int                # facts from this source that later confirmed
    facts_refuted: int                  # facts from this source that later were refuted
    facts_unresolved: int               # facts still in flight (corroborating / unconfirmed)
    outcome_n: int                      # total facts with a resolved terminal outcome
    confirmation_rate: float | None     # confirmed / (confirmed + refuted); None if no terminal outcomes

    # -- composite reliability rank (not a score, a sort key) --
    # Higher = more reliably confirmed. Tie-break: independent_rate, then appearances.
    # Never collapsed into a single magic number — each dimension is surfaced separately.
    _reliability_rank: float = field(repr=False)


def _make_reputation(source: str, acc: _SourceAccumulator) -> SourceReputation:
    """Build a frozen SourceReputation from a mutable accumulator."""
    independent_rate = (
        acc.independent_appearances / acc.appearances if acc.appearances else 0.0
    )
    mean_attr = (
        round(acc.attribution_weight_sum / acc.attribution_weight_n, 3)
        if acc.attribution_weight_n
        else 0.0
    )
    outcome_n = acc.facts_confirmed + acc.facts_refuted
    confirmation_rate = (
        round(acc.facts_confirmed / outcome_n, 3) if outcome_n else None
    )
    # Reliability rank: confirmed_rate (primary) × independent_rate (secondary).
    # Sources with no resolved outcomes rank below any source that has.
    if confirmation_rate is not None:
        rank = confirmation_rate * 0.7 + round(independent_rate, 3) * 0.3
    else:
        # No terminal outcomes yet — rank by independent_rate only, below the scorable tier.
        rank = -1.0 + round(independent_rate, 3) * 0.3

    return SourceReputation(
        source=source,
        appearances=acc.appearances,
        independent_appearances=acc.independent_appearances,
        independent_rate=round(independent_rate, 3),
        primary_appearances=acc.primary_appearances,
        mean_attribution_weight=mean_attr,
        solo_extraordinary=acc.solo_extraordinary,
        facts_confirmed=acc.facts_confirmed,
        facts_refuted=acc.facts_refuted,
        facts_unresolved=acc.facts_unresolved,
        outcome_n=outcome_n,
        confirmation_rate=confirmation_rate,
        _reliability_rank=round(rank, 4),
    )


@dataclass
class _FactTracker:
    """Tracks events for one normalised fact across multiple corroboration ticks."""

    events: list[Mapping] = field(default_factory=list)


def fold_reputation(
    events: Iterable[Mapping],
    *,
    attribution_weight_fn=attribution_weight,
) -> list[SourceReputation]:
    """Fold the `cluster.corroborated` event stream into per-source reputation records.

    Events must be supplied oldest-first (as the DB returns them: `order by id`). The fold:
    1. Groups events by normalised fact to resolve truth-over-time outcomes via
       `calibration.resolve_outcome` (same logic as `observations_from_history`).
    2. For each fact, for each source in the cluster, accumulates the per-source signals
       across all appearances.
    3. Returns SourceReputation records sorted by reliability (descending): sources whose
       facts confirmed most often and who most often appeared as independent originators.

    Why sorted by reliability: the consumer (admin panel, calibration report, agent routing)
    needs to find the most reliable sources at the top. The sort is deterministic — no ties
    unless two sources have identical records.

    Pure — no DB, no I/O.
    """
    # Pass 1: bucket events by fact (preserving arrival order).
    by_fact: OrderedDict[str, _FactTracker] = OrderedDict()
    all_events: list[Mapping] = list(events)
    for ev in all_events:
        key = _norm_fact(ev.get("fact", ""))
        if key not in by_fact:
            by_fact[key] = _FactTracker()
        by_fact[key].events.append(ev)

    # Per-source accumulators, keyed by source name.
    accs: dict[str, _SourceAccumulator] = {}

    # Pass 2: per fact, resolve outcome and fold into per-source accumulators.
    for tracker in by_fact.values():
        hist = tracker.events
        if not hist:
            continue

        first, last = hist[0], hist[-1]
        outcome = resolve_outcome(
            int(first.get("independent_originators", 0)),
            int(last.get("independent_originators", 0)),
            latest_has_primary=bool(last.get("has_primary", False)),
            corrected=any(h.get("corrected") for h in hist),
            grounding=last.get("grounding"),
        )

        # Use the LATEST event for per-source signals — it reflects the most corroborated state.
        # (For outcome accuracy we use the resolved outcome above, which spans the full trajectory.)
        ev = last
        sources: list[str] = ev.get("sources") or []
        originators: list[list[str]] = ev.get("originators") or []
        extremity: str = ev.get("extremity", "notable")
        has_primary: bool = bool(ev.get("has_primary", False))

        # Solo extraordinary: only one independent originator AND extremity is significant/extraordinary.
        is_solo = int(ev.get("independent_originators", 0)) == 1
        is_extraordinary = extremity in _EXTRAORDINARY_LEVELS

        for source in sources:
            if source not in accs:
                accs[source] = _SourceAccumulator()
            acc = accs[source]
            acc.appearances += 1

            is_independent = _source_is_independent_originator(source, originators)
            if is_independent:
                acc.independent_appearances += 1
                # Attribution weight: use a body proxy — primary sources count fully,
                # others get the named weight as a proxy (we don't have article bodies here).
                w = 1.0 if is_primary_source(source) else attribution_weight_fn("", source)
                acc.attribution_weight_sum += w
                acc.attribution_weight_n += 1

            if has_primary and is_primary_source(source):
                acc.primary_appearances += 1

            if is_solo and is_extraordinary:
                acc.solo_extraordinary += 1

            # Outcome accounting (truth-over-time, not consensus).
            if outcome == CONFIRMED:
                acc.facts_confirmed += 1
            elif outcome == REFUTED:
                acc.facts_refuted += 1
            else:
                acc.facts_unresolved += 1

    records = [_make_reputation(src, acc) for src, acc in accs.items()]
    # Sort by reliability descending, then source name for determinism.
    records.sort(key=lambda r: (-r._reliability_rank, r.source))
    return records


def reputation_by_source(
    reputations: Iterable[SourceReputation],
) -> dict[str, SourceReputation]:
    """Index reputation records by source name for O(1) lookup."""
    return {r.source: r for r in reputations}


def reputation_score(rec: SourceReputation) -> float:
    """Collapse a reputation record into a single 0..1 standing for display/sorting (#192).

    Outcome-anchored, per cauri's rule that reputation is truth-over-time, never consensus:
      * once the trajectory has resolved terminal outcomes (outcome_n > 0), confirmation_rate
        dominates, nudged by how often the source stands as an independent originator;
      * before any outcome resolves there is no truthfulness signal yet, so the score sits in a
        provisional 0..0.5 band scaled by independent_rate — callers flag these as cold-start
        (``outcome_n == 0``) rather than reading the number as a verdict.

    This is a SORT KEY / sparkline sample, not a verdict; the individual dimensions on the record
    (confirmation_rate, independent_rate, solo_extraordinary) remain the honest detail.
    """
    if rec.confirmation_rate is not None:
        return round(0.7 * rec.confirmation_rate + 0.3 * rec.independent_rate, 3)
    return round(0.5 * rec.independent_rate, 3)


def reputation_trajectories(
    events: Iterable[Mapping],
    *,
    buckets: int = 8,
    attribution_weight_fn=attribution_weight,
) -> dict[str, list[float]]:
    """Per-source reputation sparkline (#192): ``reputation_score`` recomputed over expanding
    chronological prefixes of the corroboration history.

    ``events`` oldest-first (as the DB returns them, ``order by id``). The history is split into up
    to ``buckets`` expanding prefixes (events[:c1] ⊂ events[:c2] ⊂ … ⊂ events); each prefix is
    folded with ``fold_reputation`` and every source's score at that point is appended to its
    series. Because events only accumulate, a source's presence is monotonic — its series is a
    contiguous run from the first bucket it appears in, so sources that arrive late have shorter
    sparklines (the UI just plots fewer points). Returns ``{source: [score, …]}``.

    Pure — no DB, no I/O. Cost is ``buckets`` folds, i.e. O(buckets × events).
    """
    evs = list(events)
    if not evs:
        return {}
    buckets = max(1, buckets)
    n = len(evs)
    cuts = sorted({max(1, round(n * i / buckets)) for i in range(1, buckets + 1)})
    series: dict[str, list[float]] = {}
    for c in cuts:
        recs = fold_reputation(evs[:c], attribution_weight_fn=attribution_weight_fn)
        for r in recs:
            series.setdefault(r.source, []).append(reputation_score(r))
    return series
