"""Loader → assembly → render integration for the story feed (#264/#42), driven by a fake pool that
returns the real projection row shapes. Catches anything the pure tests can't: the SQL column reads,
the node↔cluster join, the singleton tail, and the console renderers end to end."""

import asyncio
import datetime as dt

from maat.serving.stories import load_story_detail, load_story_views
from maat.web.app import _stories_page, _story_detail_page


def _cl(cid, fact, claim_ids, indep, originators, *, conf=0.5, primary=False, grounding=None):
    return {"id": cid, "fact": fact, "claim_ids": claim_ids, "independent_originators": indep,
            "originators": originators, "sources": [f"s{i}" for i in range(max(1, indep))],
            "has_primary": primary, "confidence": conf, "extremity": "notable", "grounding": grounding}


_TABLES = {
    "clusters": [
        _cl("c1", "Quake hits the region.", ["k1"], 3, [["a1"], ["a2"], ["a3"]], conf=0.85,
            primary=True, grounding="supported"),
        _cl("c2", "Aid convoys arrive.", ["k2"], 1, [["a4"]], conf=0.4),
        _cl("c3", "Markets may fall next week.", ["k3"], 1, [["a5"]], conf=0.3),   # projection
        _cl("c4", "Local festival drew crowds.", ["k4"], 1, [["a6"]], conf=0.5),  # un-threaded
    ],
    "claims": [
        {"id": "k1", "kind": "fact", "text": "Quake hits the region.", "disputed": False},
        {"id": "k2", "kind": "fact", "text": "Aid convoys arrive.", "disputed": False},
        {"id": "k3", "kind": "projection", "text": "Markets may fall next week.", "disputed": False},
        {"id": "k4", "kind": "fact", "text": "Local festival drew crowds.", "disputed": False},
    ],
    "articles": [{"id": f"a{i}", "source": s} for i, s in enumerate(
        ["", "reuters.com", "apnews.com", "bbc.com", "local.test", "blog.test", "festival.test"])],
    "node_clusters": [
        {"node_id": "node:1", "cluster_id": "c1"},
        {"node_id": "node:1", "cluster_id": "c2"},
        {"node_id": "node:2", "cluster_id": "c3"},
    ],
    "nodes": [
        {"id": "node:1", "headline": "Quake hits the region.", "first_seen": 1.0,
         "last_updated": 100.0, "cluster_count": 2},
        {"id": "node:2", "headline": "Markets may fall next week.", "first_seen": 5.0,
         "last_updated": 50.0, "cluster_count": 1},
    ],
    "detail_snaps": [
        {"cluster_id": "c1", "snapshot_day": dt.date(2026, 6, 15), "independent_originators": 1,
         "has_primary": False, "extremity": "notable", "confidence": 0.4, "originators": [["a1"]],
         "grounding": None, "corrected": False},
        {"cluster_id": "c1", "snapshot_day": dt.date(2026, 6, 16), "independent_originators": 3,
         "has_primary": True, "extremity": "notable", "confidence": 0.85,
         "originators": [["a1"], ["a2"], ["a3"]], "grounding": "supported", "corrected": False},
    ],
}


class _FakePool:
    async def fetch(self, q, *args):
        q = " ".join(q.split())
        if "from clusters" in q:
            return _TABLES["clusters"]
        if "from claims" in q:
            return _TABLES["claims"]
        if "from articles" in q:
            return _TABLES["articles"]
        if "claim.pivot" in q:
            return []
        if "from story_node_clusters" in q:
            return _TABLES["node_clusters"]
        if "from story_nodes" in q:
            return _TABLES["nodes"]
        if "from cluster_snapshots" in q:
            return _TABLES["detail_snaps"] if "where cluster_id = any" in q else []
        return []  # events fallback (reputation history) → empty → all cold-start


def test_list_covers_nodes_plus_unthreaded_singletons_ranked():
    views, total = asyncio.run(load_story_views(_FakePool()))
    ids = [v.node_id for v in views]
    assert total == 3
    assert set(ids) == {"node:1", "node:2", "cluster:c4"}   # 2 nodes + the un-threaded cluster
    assert ids[0] == "node:1"                               # best-corroborated story ranks first
    node2 = next(v for v in views if v.node_id == "node:2")
    assert node2.score.forecast_only                        # projection-only story isn't a truth score

    html = _stories_page(views, total)
    assert 'href="/story/node:1"' in html and 'href="/story/cluster:c4"' in html


def test_detail_assembles_facts_forecast_split_and_trajectory():
    v = asyncio.run(load_story_detail(_FakePool(), "node:1"))
    assert v is not None
    assert [f.cluster_id for f in v.facts][0] == "c1" and v.facts[0].is_headline
    assert {f.cluster_id for f in v.facts} == {"c1", "c2"} and not v.forecasts
    assert [p.day for p in v.trajectory] == ["2026-06-15", "2026-06-16"]
    assert v.trajectory[1].score > v.trajectory[0].score   # credibility grew as corroboration arrived

    html = _story_detail_page(v)
    assert "Credibility over time" in html and "<polyline" in html
    assert "Quake hits the region." in html


def test_detail_of_unthreaded_cluster_resolves():
    v = asyncio.run(load_story_detail(_FakePool(), "cluster:c4"))
    assert v is not None and v.cluster_count == 1 and v.facts[0].cluster_id == "c4"


def test_detail_missing_story_returns_none():
    assert asyncio.run(load_story_detail(_FakePool(), "node:nope")) is None
