"""One credibility score per STORY — the product-facing roll-up (#264), assembled over the real
story graph (#42).

People read news as stories, not claims. This is the single layer that turns the #42 story graph
(``story_nodes`` / ``story_node_clusters``) plus the corroboration projections into a ranked list of
stories, each with ONE transparent credibility score and, on the detail view, the score's derivation
and its trajectory over time. The SAME layer backs the operator console (``/stories``,
``/story/{id}``) AND the served API (``/api/v2/stories``, ``/api/v2/story/{id}``) — and through the
API, the app — so every surface shows the identical paradigm and number.

A story's UNIT is a story-graph node (stable, content-addressed id). Clusters not yet threaded into a
node surface as their own one-cluster story (id ``cluster:<id>``) so coverage is always total and the
view self-heals as the next story-graph delta lands — never a console-only shortcut.

Pure assembly (``assemble_story`` / ``story_trajectory``) + thin async loaders. The score model lives
in ``maat.learning.story_credibility`` (headline-anchored, reputation-weighted, cold-start capped).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from maat.learning.reputation import fold_reputation, reputation_score
from maat.learning.story_credibility import FactView, StoryScore, score_story
from maat.learning.trajectory import load_trajectory
from maat.serving.buildcache import VersionCache, data_version


def _jload(v: Any) -> list:
    if isinstance(v, str):
        return json.loads(v) if v else []
    return list(v) if v else []


# ---------------------------------------------------------------------------
# Public data shapes (what the console renders and the API serialises)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorySource:
    """One INDEPENDENT originator group behind a fact. A group with >1 outlet is a wire reprint of a
    single report — counted once toward corroboration, never as separate confirmation."""

    names: list[str]
    reputation: float | None  # mean track-record of the group's RATED outlets; None = cold-start
    wire: bool


@dataclass(frozen=True)
class StoryFact:
    """One cluster within a story — a checkable fact, or (``is_projection``) a forecast/opinion that
    never feeds the truth score."""

    cluster_id: str
    fact: str
    fact_en: str | None       # English gloss (#240 pivots) when the fact is non-English
    confidence: float
    independent_originators: int
    has_primary: bool
    extremity: str
    grounding: str | None
    disputed: bool
    is_headline: bool         # the fact the score is anchored on
    is_projection: bool
    sources: list[StorySource]


@dataclass(frozen=True)
class TrajectoryPoint:
    """The story's credibility recomputed as of one calendar day, from the cluster snapshots (#39)."""

    day: str   # ISO date
    score: int
    band: str


@dataclass(frozen=True)
class StoryView:
    """A story: one headline, one score, its facts/forecasts, and (on the detail view) its
    trajectory. ``node_id`` is the story-graph node id, or ``cluster:<id>`` for an un-threaded one."""

    node_id: str
    headline: str
    headline_orig: str | None     # original-language headline when the display one is an English gloss
    score: StoryScore
    facts: list[StoryFact]        # checkable facts, headline first
    forecasts: list[StoryFact]    # projections, shown separately — never scored as truth
    source_count: int
    cluster_count: int
    first_seen: float
    last_updated: float
    trajectory: list[TrajectoryPoint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure assembly
# ---------------------------------------------------------------------------


def _cluster_kind(claim_ids: list, kind_by_claim: dict[str, str]) -> str:
    """A cluster's dominant claim kind — 'projection' (forecast/opinion) vs 'fact' (§5.3). Mirrors
    the feed: corroboration is a truth signal for facts, not for projections."""
    kinds = [kind_by_claim.get(str(c)) for c in claim_ids]
    kinds = [k for k in kinds if k]
    return "projection" if kinds and kinds.count("projection") > kinds.count("fact") else "fact"


def _fact_en(fact: str, claim_ids: list, text_by_claim: dict[str, str],
             pivots: dict[str, str]) -> str | None:
    """English gloss of a cluster's fact, reusing the #240 claim pivots (no render-time translation):
    match the fact back to its source claim by text, return that claim's stored English."""
    f = (fact or "").strip()
    for cid in claim_ids:
        if (text_by_claim.get(str(cid)) or "").strip() == f:
            en = (pivots.get(str(cid)) or "").strip()
            return en if en and en != f else None
    return None


def _sources_of(cl: Any, id_to_source: dict[str, str],
                reputation: dict[str, float]) -> list[StorySource]:
    out: list[StorySource] = []
    for grp in _jload(cl["originators"]):
        names = sorted({id_to_source.get(a, a) for a in grp})
        reps = [reputation[n] for n in names if n in reputation]
        out.append(StorySource(names=names, reputation=(sum(reps) / len(reps)) if reps else None,
                                wire=len(grp) > 1))
    return out


def _story_fact(cl: Any, *, is_headline: bool, kind_by_claim, text_by_claim, pivots,
                id_to_source, reputation, disputed_claims) -> StoryFact:
    claim_ids = _jload(cl["claim_ids"])
    disputed = any(str(x) in disputed_claims for x in claim_ids)
    return StoryFact(
        cluster_id=cl["id"],
        fact=cl["fact"] or "",
        fact_en=_fact_en(cl["fact"], claim_ids, text_by_claim, pivots),
        confidence=float(cl["confidence"] or 0.0),
        independent_originators=int(cl["independent_originators"] or 0),
        has_primary=bool(cl["has_primary"]),
        extremity=cl["extremity"] or "notable",
        grounding=cl["grounding"],
        disputed=disputed,
        is_headline=is_headline,
        is_projection=_cluster_kind(claim_ids, kind_by_claim) == "projection",
        sources=_sources_of(cl, id_to_source, reputation),
    )


def _fact_view(cl: Any, id_to_source, disputed_claims) -> FactView:
    claim_ids = _jload(cl["claim_ids"])
    groups = [[id_to_source.get(a, a) for a in grp] for grp in _jload(cl["originators"])]
    return FactView(
        confidence=float(cl["confidence"] or 0.0),
        independent_originators=int(cl["independent_originators"] or 0),
        has_primary=bool(cl["has_primary"]),
        extremity=cl["extremity"] or "notable",
        originator_sources=groups,
        grounding=cl["grounding"],
        disputed=any(str(x) in disputed_claims for x in claim_ids),
    )


def assemble_story(node_id: str, headline_hint: str | None, clusters: list[Any], *,
                   kind_by_claim, text_by_claim, pivots, id_to_source, reputation,
                   disputed_claims, first_seen: float = 0.0, last_updated: float = 0.0,
                   trajectory: list[TrajectoryPoint] | None = None) -> StoryView:
    """Roll a node's clusters into one StoryView. The score anchors on the best-corroborated FACT
    (most independent originators) — the same fact becomes the displayed headline, so the number and
    the headline describe the same thing. Projections are split out and never scored."""
    story_facts: list[StoryFact] = []
    fact_views: list[FactView] = []
    forecasts: list[StoryFact] = []
    for cl in clusters:
        claim_ids = _jload(cl["claim_ids"])
        sf = _story_fact(cl, is_headline=False, kind_by_claim=kind_by_claim,
                         text_by_claim=text_by_claim, pivots=pivots, id_to_source=id_to_source,
                         reputation=reputation, disputed_claims=disputed_claims)
        if _cluster_kind(claim_ids, kind_by_claim) == "projection":
            forecasts.append(sf)
        else:
            story_facts.append(sf)
            fact_views.append(_fact_view(cl, id_to_source, disputed_claims))

    score = score_story(fact_views, reputation)

    if fact_views:
        head = max(range(len(fact_views)), key=lambda i: fact_views[i].independent_originators)
        story_facts[head] = replace(story_facts[head], is_headline=True)
        story_facts = [story_facts[head]] + [f for i, f in enumerate(story_facts) if i != head]
        hf = story_facts[0]
        headline, headline_orig = (hf.fact_en or hf.fact), (hf.fact if hf.fact_en else None)
    else:
        headline = headline_hint or (forecasts[0].fact if forecasts else "")
        headline_orig = None

    source_count = len({x for cl in clusters for x in _jload(cl["sources"])})
    return StoryView(
        node_id=node_id, headline=headline, headline_orig=headline_orig, score=score,
        facts=story_facts, forecasts=forecasts, source_count=source_count,
        cluster_count=len(clusters), first_seen=first_seen, last_updated=last_updated,
        trajectory=trajectory or [],
    )


def story_trajectory(snapshots: list[dict], projection_cluster_ids: set[str],
                     reputation: dict[str, float], id_to_source: dict[str, str]) -> list[TrajectoryPoint]:
    """The story's credibility over time: for each calendar day a snapshot exists (#39), recompute
    the score from each fact cluster's state AS OF that day (latest snapshot ≤ day). Today's source
    reputations are applied throughout — an honest 'what would this read, given what we knew then'."""
    per_cluster: dict[str, list[dict]] = defaultdict(list)
    for s in snapshots:
        if s["cluster_id"] not in projection_cluster_ids:
            per_cluster[s["cluster_id"]].append(s)
    for snaps in per_cluster.values():
        snaps.sort(key=lambda s: s["snapshot_day"])

    points: list[TrajectoryPoint] = []
    for day in sorted({s["snapshot_day"] for s in snapshots}):
        fvs: list[FactView] = []
        for snaps in per_cluster.values():
            asof = None
            for s in snaps:
                if s["snapshot_day"] <= day:
                    asof = s
                else:
                    break
            if asof is None:
                continue
            groups = [[id_to_source.get(a, a) for a in grp] for grp in _jload(asof["originators"])]
            fvs.append(FactView(
                confidence=float(asof["confidence"] or 0.0),
                independent_originators=int(asof["independent_originators"] or 0),
                has_primary=bool(asof["has_primary"]), extremity=asof["extremity"] or "notable",
                originator_sources=groups, grounding=asof.get("grounding"),
                disputed=bool(asof.get("corrected")),
            ))
        if not fvs:
            continue
        s = score_story(fvs, reputation)
        points.append(TrajectoryPoint(day=str(day), score=s.score, band=s.band))
    return points


# ---------------------------------------------------------------------------
# Async loaders (the only I/O — pure assembly above takes it from here)
# ---------------------------------------------------------------------------


@dataclass
class _Common:
    clusters_by_id: dict[str, Any]
    kind_by_claim: dict[str, str]
    text_by_claim: dict[str, str]
    pivots: dict[str, str]
    id_to_source: dict[str, str]
    reputation: dict[str, float]
    disputed_claims: set[str]
    node_clusters: dict[str, list[str]]
    node_meta: dict[str, dict]


_COMMON_CACHE = VersionCache()


async def _load_common(pool: Any) -> _Common:
    # Cache the full snapshot/cluster load by data-version (#284): story-detail (and a feed cache
    # miss) otherwise re-fold the whole trajectory + reputation history per request.
    version = await data_version(pool)
    cached = _COMMON_CACHE.get("common", version)
    if cached is not None:
        return cached
    clusters = await pool.fetch("select * from clusters")
    claims = await pool.fetch("select id, kind, text, disputed from claims")
    arts = await pool.fetch("select id, source from articles")
    # Bounded read (#283): the latest English pivot per claim (one row per claim via `distinct on`),
    # not a full `claim.pivot` event-log scan. The pivot fold is last-write-wins per claim, so the
    # latest non-empty text_en per claim is byte-identical to folding the whole stream.
    pivot_rows = await pool.fetch(
        "select distinct on (data->>'claim_id') data from events "
        "where type = 'claim.pivot' and data->>'claim_id' <> '' and data->>'text_en' <> '' "
        "order by data->>'claim_id', id desc"
    )
    node_rows = await pool.fetch("select node_id, cluster_id from story_node_clusters")
    meta_rows = await pool.fetch(
        "select id, headline, first_seen, last_updated, cluster_count from story_nodes"
    )
    history = await load_trajectory(pool)

    clusters_by_id = {c["id"]: c for c in clusters}
    node_clusters: dict[str, list[str]] = defaultdict(list)
    for r in node_rows:
        if r["cluster_id"] in clusters_by_id:
            node_clusters[r["node_id"]].append(r["cluster_id"])

    pivots: dict[str, str] = {}
    for r in pivot_rows:
        d = json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] or {})
        if d.get("claim_id") and d.get("text_en"):
            pivots[d["claim_id"]] = d["text_en"]

    common = _Common(
        clusters_by_id=clusters_by_id,
        kind_by_claim={str(c["id"]): c["kind"] for c in claims},
        text_by_claim={str(c["id"]): c["text"] for c in claims},
        pivots=pivots,
        id_to_source={a["id"]: a["source"] for a in arts},
        # Reputation of RATED sources only (a resolved track record); cold-start = absent = neutral.
        reputation={r.source: reputation_score(r) for r in fold_reputation(history) if r.outcome_n > 0},
        disputed_claims={str(c["id"]) for c in claims if c["disputed"]},
        node_clusters=dict(node_clusters),
        node_meta={r["id"]: dict(r) for r in meta_rows},
    )
    _COMMON_CACHE.put("common", version, common)
    return common


def _assemble(c: _Common, node_id: str, cluster_ids: list[str], *,
              trajectory: list[TrajectoryPoint] | None = None) -> StoryView:
    meta = c.node_meta.get(node_id, {})
    return assemble_story(
        node_id, meta.get("headline"), [c.clusters_by_id[cid] for cid in cluster_ids],
        kind_by_claim=c.kind_by_claim, text_by_claim=c.text_by_claim, pivots=c.pivots,
        id_to_source=c.id_to_source, reputation=c.reputation, disputed_claims=c.disputed_claims,
        first_seen=float(meta.get("first_seen") or 0.0), last_updated=float(meta.get("last_updated") or 0.0),
        trajectory=trajectory,
    )


def _rank_key(v: StoryView) -> tuple:
    # Real stories first (forecast-only last), then by score, then most-recently updated.
    return (not v.score.forecast_only, v.score.score, v.last_updated)


_STORIES_CACHE = VersionCache()


async def load_story_views(pool: Any, *, limit: int | None = None) -> tuple[list[StoryView], int]:
    """Every story, ranked by credibility. Returns ``(views, total)`` — ``total`` is the full count
    before ``limit`` so callers can show "showing N of M". Threaded nodes plus a one-cluster story
    for any cluster not yet in the graph (total coverage).

    The credibility fold is global (so it can't be SQL-paged) but only changes when new events land,
    so the full sorted list is cached by data-version (#283); ``limit`` slices the cached result."""
    version = await data_version(pool)
    views = _STORIES_CACHE.get("views", version)
    if views is None:
        c = await _load_common(pool)
        views = []
        noded: set[str] = set()
        for node_id, cids in c.node_clusters.items():
            noded.update(cids)
            views.append(_assemble(c, node_id, cids))
        for cid in c.clusters_by_id:
            if cid not in noded:
                views.append(_assemble(c, f"cluster:{cid}", [cid]))
        views.sort(key=_rank_key, reverse=True)
        _STORIES_CACHE.put("views", version, views)
    return (views[:limit] if limit else views), len(views)


async def load_story_detail(pool: Any, node_id: str) -> StoryView | None:
    """One story with its full breakdown + credibility trajectory (from the cluster snapshots, #39).
    Accepts a story-graph node id or a ``cluster:<id>`` singleton id."""
    c = await _load_common(pool)
    if node_id.startswith("cluster:"):
        cid = node_id[len("cluster:"):]
        if cid not in c.clusters_by_id:
            return None
        cids = [cid]
    else:
        cids = c.node_clusters.get(node_id, [])
        if not cids:
            return None

    snap_rows = await pool.fetch(
        "select cluster_id, snapshot_day, independent_originators, has_primary, extremity, "
        "confidence, originators, grounding, corrected from cluster_snapshots "
        "where cluster_id = any($1::text[]) order by snapshot_day",
        cids,
    )
    proj_ids = {
        cid for cid in cids
        if _cluster_kind(_jload(c.clusters_by_id[cid]["claim_ids"]), c.kind_by_claim) == "projection"
    }
    traj = story_trajectory([dict(r) for r in snap_rows], proj_ids, c.reputation, c.id_to_source)
    return _assemble(c, node_id, cids, trajectory=traj)
