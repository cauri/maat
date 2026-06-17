"""Acquisition steering (#35) — fold learned source weights into what actually gets ingested.

The source-learning loop (`learning/source_learning.py`) ranks sources by *proven* reliability —
truth-over-time signals (confirmation rate, independent-originator rate, attribution quality), never
volume or geography — into capped, diversity-floored acquisition weights. That half is wired into a
live read (`GET /api/v2/source-preferences`). THIS module is the load-bearing other half: the
*actuation* that lets the live ingestion clock (`scripts/clock.py`) bias what it fetches toward
rewarding sources WITHOUT collapsing diversity or re-centering on Anglo-American outlets.

Two bounded mechanisms (cauri's call: "both", strongest steer, still bounded by floor + cap):

1. RE-RANK within a per-query fetch budget (`rank_for_fetch`). When a query yields more credible
   candidates than we will fetch bodies for, choose which ones by learned weight — but in three
   phases so diversity is structural, not incidental:
     - phase 1 (diversity floor): one candidate per distinct source, in weight order, so every
       source present is represented before any source gets a second slot. New/unknown sources sit
       at the floor weight, so a fresh topic where nothing has a reputation yet is untouched — new
       voices are never silenced.
     - phase 2 (reward priority): fill the remaining budget by descending weight, capped per source
       (`_MAX_WEIGHT` share) so no single source dominates a query — the anti-echo-chamber lever.
     - phase 3 (don't waste budget): if the cap left budget idle only because few sources exist,
       relax it — refusing an available article buys no diversity when there is nothing more
       diverse to fetch.

2. DEEPEN top sources (`deepening_plan`). A bounded extra pass that re-queries the tracked topics
   scoped (`domain:`) to the top proven-reliable sources, so reward earns MORE coverage rather than
   merely winning ties. Low-evidence and solo-extraordinary-flagged sources are excluded — we do not
   amplify the unproven or the red-flagged.

Pure functions over plain values — no DB, no I/O, no LLM. The clock wires the I/O around them and
emits an `acquire.steer` event per tick so the effect is observable in the log.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from math import ceil
from typing import Any

from maat.learning.source_learning import _FLOOR_WEIGHT, _MAX_WEIGHT, SourcePreferences

# ---------------------------------------------------------------------------
# Tuneable constants (operator controls cadence/cost via the clock; these bound a single tick).
# ---------------------------------------------------------------------------

# Bodies to fetch per query once steering is active. Below GDELT's maxrecords (15): the point of
# the re-rank is to spend that budget on the most-rewarding credible sources, not on all 15.
PER_QUERY_FETCH_BUDGET = 8

# How many top sources the deepening pass re-queries, and the tick-wide cap on extra GDELT calls
# (GDELT throttles ~1 query/5s, so this directly bounds added tick time).
_DEEPEN_TOP_N = 3
_DEEPEN_MAX_QUERIES = 6


def _domain(candidate: Any) -> str:
    """Source key for a candidate. Acquisition candidates (GdeltArticle / Apify items) expose
    ``.domain``; fall back to ``.source`` for anything that carries that instead."""
    return getattr(candidate, "domain", "") or getattr(candidate, "source", "") or ""


# ---------------------------------------------------------------------------
# Re-rank within budget
# ---------------------------------------------------------------------------


def rank_for_fetch(
    candidates: Iterable[Any],
    prefs: SourcePreferences,
    *,
    budget: int | None = None,
    source_of: Callable[[Any], str] = _domain,
) -> list[Any]:
    """Order (and optionally truncate to ``budget``) the candidates to fetch, biasing toward
    learned-reliable sources while guaranteeing source diversity.

    ``candidates``: acquisition candidates exposing a source via ``source_of`` (default ``.domain``).
    ``prefs``: the learned :class:`SourcePreferences`. ``budget``: max candidates to return; ``None``
    means "order, don't truncate".

    Cold start (no learned weights yet) is an explicit pass-through: original order is preserved so a
    reputation-free system behaves exactly as it did before steering. Deterministic and pure.
    """
    cands = list(candidates)
    limit = len(cands) if budget is None else max(0, budget)
    if not prefs.weights:  # no learned signal — preserve original order (and budget, if given)
        return cands[:limit]
    if limit == 0 or not cands:
        return []

    # Group candidates by source, preserving arrival order within each source.
    groups: OrderedDict[str, list[Any]] = OrderedDict()
    for c in cands:
        groups.setdefault(source_of(c), []).append(c)

    def weight(src: str) -> float:
        # Unknown sources are treated at the floor weight — present, but lowest preference.
        return prefs.weights.get(src, _FLOOR_WEIGHT)

    # Sources in descending learned weight, name as the deterministic tie-break.
    sources = sorted(groups, key=lambda s: (-weight(s), s))
    per_source_cap = max(1, ceil(_MAX_WEIGHT * limit))
    taken: dict[str, int] = dict.fromkeys(sources, 0)
    out: list[Any] = []

    def _emit(src: str) -> None:
        out.append(groups[src].pop(0))
        taken[src] += 1

    # Phase 1 — diversity floor: one per source in weight order. When sources outnumber the budget,
    # the scarce slots go to the highest-weighted sources (reward wins, breadth still maximised).
    for s in sources:
        if len(out) >= limit:
            break
        _emit(s)

    # Phase 2 — reward priority, capped: fill the rest by descending weight, but never let one source
    # exceed its share of the budget (the anti-echo-chamber cap).
    progressed = True
    while len(out) < limit and progressed:
        progressed = False
        for s in sources:  # already weight-ordered → highest weight fills first
            if len(out) >= limit:
                break
            if groups[s] and taken[s] < per_source_cap:
                _emit(s)
                progressed = True

    # Phase 3 — don't waste budget: if the cap left slots idle only because few sources exist, relax
    # it. Refusing an available article gains no diversity when nothing more diverse remains.
    progressed = True
    while len(out) < limit and progressed:
        progressed = False
        for s in sources:
            if len(out) >= limit:
                break
            if groups[s]:
                _emit(s)
                progressed = True

    return out


# ---------------------------------------------------------------------------
# Deepen top sources
# ---------------------------------------------------------------------------


def deepening_plan(
    prefs: SourcePreferences,
    topic_queries: Sequence[str],
    *,
    top_n: int = _DEEPEN_TOP_N,
    max_queries: int = _DEEPEN_MAX_QUERIES,
) -> list[tuple[str, str]]:
    """Plan the bounded "deepen top sources" pass: extra GDELT queries that re-search the tracked
    topics scoped to the top proven-reliable sources.

    Returns ``[(source, gdelt_query)]`` with ``len <= max_queries``; each query is a topic query with
    a ``domain:<source>`` filter appended. Top sources are round-robined across topics so every
    deepened source gets coverage; identical (source, topic) pairs are de-duplicated (re-running the
    same query yields nothing new). Low-evidence and solo-extraordinary-flagged sources are excluded.

    Pure — the clock runs the returned queries.
    """
    queries = [q for q in topic_queries if q and q.strip()]
    if not queries or max_queries <= 0 or top_n <= 0:
        return []

    top = [
        p.source
        for p in prefs.ranked
        if not p.low_evidence and not p.solo_extraordinary_flag
    ][:top_n]
    if not top:
        return []

    plan: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    rounds = ceil(max_queries / len(top))
    for r in range(rounds):
        tq = queries[r % len(queries)]
        for src in top:  # each round pairs every top source with the round's topic
            key = (src, tq)
            if key in seen:
                continue
            seen.add(key)
            plan.append((src, f"{tq} domain:{src}"))
            if len(plan) >= max_queries:
                return plan
    return plan


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def steer_summary(
    prefs: SourcePreferences,
    *,
    per_query_budget: int,
    deepen_plan: Sequence[tuple[str, str]],
    deepened_articles: int,
    reranked_queries: int,
) -> dict[str, Any]:
    """Build the ``acquire.steer`` event payload — what the steer did this tick, for the log/console.

    Lands in the append-only ``events`` table (the kernel records every type before projecting), so
    the actuation is auditable: which sources were favoured, how many deepen queries ran, how much
    extra coverage they yielded. Pure data, not an LLM prompt.
    """
    return {
        "active": bool(prefs.weights),
        "per_query_budget": per_query_budget,
        "reranked_queries": reranked_queries,
        "diversity_floor": sorted(prefs.diversity_floor),
        "deepen_sources": sorted({s for s, _ in deepen_plan}),
        "deepen_queries": len(deepen_plan),
        "deepened_articles": deepened_articles,
        "top_sources": [
            {
                "source": p.source,
                "rank": p.rank,
                "acquisition_weight": p.acquisition_weight,
            }
            for p in prefs.ranked[:10]
        ],
        "note": (
            "Acquisition steered toward learned-reliable sources (#35): re-rank within a per-query "
            "fetch budget + deepen top sources, bounded by the diversity floor and per-source cap. "
            "Reward = truth-over-time, never volume or geography."
        ),
    }
