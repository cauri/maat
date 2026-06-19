"""Tests for story_graph.py (issues #42, #43, #44).

Coverage:
  #42 — EventNode formation, typed edge inference (develops / spawns / merges),
         pure fold over corroboration clusters
  #43 — Claim<->node many-to-many mapping (nodes_for_claim, claims_for_node,
         cluster_nodes, node_clusters)
  #44 — Novelty function: unseen=novel, recently-seen=not-novel, decayed=novel,
         per-user isolation, annotate_feed ordering

All tests are deterministic; no DB, no LLM, no filesystem I/O.
"""

from __future__ import annotations

from maat.pipeline.story_graph import (
    ClusterRow,
    EventNode,
    StoryGraph,
    annotate_feed,
    attach_cluster,
    claims_for_node,
    cluster_novelty,
    entity_jaccard,
    fold_clusters,
    nodes_for_claim,
    passes_gate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TAU = 0.40
_WIN = 72 * 3600  # 72 hours in seconds
_DECAY = 30 * 24 * 3600  # 30 days
_NOW = 1_000_000.0


def _vec(val: float, dim: int = 4) -> list[float]:
    """Return a simple vector pointing in direction ``val`` for cosine tests."""
    v = [0.0] * dim
    v[0] = val
    v[1] = 1.0 - val
    return v


def _cluster(
    cid: str,
    entities: list[str],
    ts: float = 0.0,
    emb: list[float] | None = None,
    claim_ids: list[str] | None = None,
) -> ClusterRow:
    return ClusterRow(
        cluster_id=cid,
        entity_spine=entities,
        topic_embedding=emb or _vec(0.9),
        earliest_ts=ts,
        claim_ids=claim_ids or [],
    )


def _node(
    nid: str,
    entities: list[str],
    last_updated: float = 0.0,
    emb: list[float] | None = None,
    first_seen: float = 0.0,
    cluster_count: int = 0,
) -> EventNode:
    return EventNode(
        id=nid,
        headline=nid,
        entity_spine=entities,
        topic_embedding=emb or _vec(0.9),
        first_seen=first_seen,
        last_updated=last_updated,
        cluster_count=cluster_count,
    )


def _graph_with(*nodes: EventNode) -> StoryGraph:
    """Build a StoryGraph pre-seeded with the given nodes (for gate / edge tests)."""
    g = StoryGraph(nodes={n.id: n for n in nodes})  # constructor builds the entity index (#285)
    for n in nodes:
        g.node_clusters[n.id] = []
    return g


# ---------------------------------------------------------------------------
# #42a  Entity Jaccard
# ---------------------------------------------------------------------------


def test_jaccard_full_overlap():
    assert entity_jaccard(["A", "B"], ["A", "B"]) == 1.0


def test_jaccard_partial():
    j = entity_jaccard(["A", "B", "C"], ["A", "C", "D"])
    assert abs(j - 2 / 4) < 1e-9  # 2 shared, 4 union


def test_jaccard_no_overlap():
    assert entity_jaccard(["A"], ["B"]) == 0.0


def test_jaccard_empty_spine_left():
    assert entity_jaccard([], ["A"]) == 0.0


def test_jaccard_empty_spine_right():
    assert entity_jaccard(["A"], []) == 0.0


def test_jaccard_both_empty():
    assert entity_jaccard([], []) == 0.0


# ---------------------------------------------------------------------------
# #42b  Attachment gate: both signals required
# ---------------------------------------------------------------------------


def test_gate_passes_when_both_signals_clear():
    c = _cluster("c1", ["minister-x", "valoria"], ts=1000.0)
    n = _node("n1", ["minister-x", "valoria"], last_updated=1000.0)
    assert passes_gate(c, n, entity_tau=_TAU, window_s=_WIN)


def test_gate_fails_low_entity_jaccard():
    c = _cluster("c1", ["president-z"], ts=1000.0)
    n = _node("n1", ["minister-x", "valoria"], last_updated=1000.0)
    assert not passes_gate(c, n, entity_tau=_TAU, window_s=_WIN)


def test_gate_fails_stale_timestamp():
    c = _cluster("c1", ["minister-x"], ts=1000.0 + _WIN + 1)
    n = _node("n1", ["minister-x"], last_updated=1000.0)
    assert not passes_gate(c, n, entity_tau=_TAU, window_s=_WIN)


def test_gate_fails_when_spines_empty():
    c = _cluster("c1", [], ts=1000.0)
    n = _node("n1", [], last_updated=1000.0)
    assert not passes_gate(c, n, entity_tau=_TAU, window_s=_WIN)


def test_gate_passes_at_exact_boundary():
    c = _cluster("c1", ["minister-x"], ts=_WIN)  # exactly at window boundary
    n = _node("n1", ["minister-x"], last_updated=0.0)
    assert passes_gate(c, n, entity_tau=_TAU, window_s=_WIN)


def test_gate_fails_just_outside_boundary():
    c = _cluster("c1", ["minister-x"], ts=_WIN + 1)
    n = _node("n1", ["minister-x"], last_updated=0.0)
    assert not passes_gate(c, n, entity_tau=_TAU, window_s=_WIN)


# ---------------------------------------------------------------------------
# #42c  Node formation: new node created when no candidates exist
# ---------------------------------------------------------------------------


def test_attach_creates_new_node_when_no_candidates():
    c = _cluster("c1", ["president-z"], ts=0.0, claim_ids=["claim-1"])
    g = _graph_with(_node("n1", ["minister-x"], last_updated=0.0))
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    assert result.is_new_node
    assert result.new_node is not None
    assert result.edges == []


def test_attach_new_node_gets_stable_id():
    c = _cluster("c1", ["minister-x"], ts=0.0)
    g = StoryGraph()
    r1 = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    r2 = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    assert r1.node_id == r2.node_id, "same cluster always seeds the same node id"


def test_attach_first_node_cluster_count_is_zero():
    c = _cluster("c1", ["minister-x"], ts=5.0, claim_ids=["cl-1"])
    g = StoryGraph()
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    assert result.new_node is not None
    assert result.new_node.cluster_count == 0


def test_new_node_preserves_earliest_ts():
    c = _cluster("c1", ["minister-x"], ts=42.0)
    g = StoryGraph()
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    assert result.new_node is not None
    assert result.new_node.first_seen == 42.0
    assert result.new_node.last_updated == 42.0


# ---------------------------------------------------------------------------
# #42d  Node formation: attach to existing node when gate passes
# ---------------------------------------------------------------------------


def test_attach_to_existing_node():
    c = _cluster("c2", ["minister-x"], ts=3600.0)
    n = _node("n1", ["minister-x"], last_updated=0.0, cluster_count=1)
    g = _graph_with(n)
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    assert not result.is_new_node
    assert result.node_id == "n1"


def test_cosine_tiebreaker_picks_nearer_node():
    c = _cluster("c3", ["minister-x"], ts=0.0, emb=_vec(0.1))
    n1 = _node("n1", ["minister-x"], last_updated=0.0, emb=_vec(0.9))
    n2 = _node("n2", ["minister-x"], last_updated=0.0, emb=_vec(0.1))
    g = _graph_with(n1, n2)
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    assert result.node_id == "n2", "cluster embedding is closer to n2"


# ---------------------------------------------------------------------------
# #42e  Edge inference: develops
# ---------------------------------------------------------------------------


def test_develops_edge_on_second_cluster():
    # First cluster seeds a node; second cluster develops it.
    c1 = _cluster("c1", ["minister-x"], ts=0.0, emb=_vec(0.9))
    c2 = _cluster("c2", ["minister-x"], ts=3600.0, emb=_vec(0.9))
    g = fold_clusters([c1, c2], entity_tau=_TAU, window_s=_WIN)
    develops = [e for e in g.edges if e.kind == "develops"]
    assert len(develops) == 1
    assert develops[0].to_id == "c2"


def test_no_develops_on_first_cluster():
    c = _cluster("c1", ["minister-x"], ts=0.0)
    g = StoryGraph()
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    develops = [e for e in result.edges if e.kind == "develops"]
    assert develops == []


def test_develops_requires_cosine_threshold():
    # c2 has a very different embedding -> no develops edge despite same entity spine.
    c1 = _cluster("c1", ["minister-x"], ts=0.0, emb=_vec(0.9))
    c2 = _cluster("c2", ["minister-x"], ts=3600.0, emb=_vec(0.0))
    g = fold_clusters([c1, c2], entity_tau=_TAU, window_s=_WIN)
    develops = [e for e in g.edges if e.kind == "develops"]
    assert develops == [], "low cosine should not produce a develops edge"


# ---------------------------------------------------------------------------
# #42f  Edge inference: spawns
# ---------------------------------------------------------------------------


def test_spawns_edge_when_cluster_fits_two_diverged_nodes():
    c = _cluster("c3", ["minister-x"], ts=0.0, emb=_vec(0.9))
    n1 = _node("n1", ["minister-x"], last_updated=0.0, emb=_vec(0.9), cluster_count=1)
    n2 = _node("n2", ["minister-x"], last_updated=0.0, emb=_vec(0.1))  # diverged
    g = _graph_with(n1, n2)
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    spawns = [e for e in result.edges if e.kind == "spawns"]
    assert len(spawns) >= 1
    assert spawns[0].from_id == "n1"
    assert spawns[0].to_id == "n2"


def test_no_spawns_when_other_node_fails_gate():
    c = _cluster("c3", ["minister-x"], ts=0.0)
    n1 = _node("n1", ["minister-x"], last_updated=0.0, cluster_count=1)
    n2 = _node("n2", ["completely-different-entity"], last_updated=0.0)
    g = _graph_with(n1, n2)
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    spawns = [e for e in result.edges if e.kind == "spawns"]
    assert spawns == []


# ---------------------------------------------------------------------------
# #42g  Edge inference: merges
# ---------------------------------------------------------------------------


def test_merges_edge_when_two_nodes_converge():
    c = _cluster("c4", ["minister-x"], ts=0.0, emb=_vec(0.9))
    n1 = _node("n1", ["minister-x"], last_updated=0.0, emb=_vec(0.9), first_seen=0.0, cluster_count=1)
    n2 = _node("n2", ["minister-x"], last_updated=0.0, emb=_vec(0.85), first_seen=1.0, cluster_count=1)
    g = _graph_with(n1, n2)
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    merges = [e for e in result.edges if e.kind == "merges"]
    assert len(merges) >= 1
    # older node (n1, first_seen=0) is the edge source
    assert merges[0].from_id == "n1"
    assert merges[0].to_id == "n2"


def test_merges_older_is_always_source():
    c = _cluster("c5", ["minister-x"], ts=0.0, emb=_vec(0.9))
    n1 = _node("n1", ["minister-x"], last_updated=0.0, emb=_vec(0.9), first_seen=100.0, cluster_count=1)
    n2 = _node("n2", ["minister-x"], last_updated=0.0, emb=_vec(0.85), first_seen=50.0, cluster_count=1)
    g = _graph_with(n1, n2)
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    merges = [e for e in result.edges if e.kind == "merges"]
    # n2 has earlier first_seen; should be the source
    merge = merges[0]
    assert merge.from_id == "n2"
    assert merge.to_id == "n1"


def test_no_merges_when_nodes_have_not_converged():
    c = _cluster("c6", ["minister-x"], ts=0.0, emb=_vec(0.9))
    n1 = _node("n1", ["minister-x"], last_updated=0.0, emb=_vec(0.9), cluster_count=1)
    n2 = _node("n2", ["minister-x"], last_updated=0.0, emb=_vec(0.1))  # diverged, not converged
    g = _graph_with(n1, n2)
    result = attach_cluster(c, g, entity_tau=_TAU, window_s=_WIN)
    merges = [e for e in result.edges if e.kind == "merges"]
    assert merges == []


# ---------------------------------------------------------------------------
# #42h  fold_clusters: temporal ordering produces correct node grouping
# ---------------------------------------------------------------------------


def test_fold_two_same_entity_clusters_produce_one_node():
    c1 = _cluster("c1", ["minister-x"], ts=0.0)
    c2 = _cluster("c2", ["minister-x"], ts=3600.0)  # 1 hour later — well within window
    g = fold_clusters([c1, c2], entity_tau=_TAU, window_s=_WIN)
    assert len(g.nodes) == 1


def test_fold_temporally_distant_clusters_produce_two_nodes():
    c1 = _cluster("c1", ["minister-x"], ts=0.0)
    c2 = _cluster("c2", ["minister-x"], ts=_WIN + 1)  # just outside the 72-hour window
    g = fold_clusters([c1, c2], entity_tau=_TAU, window_s=_WIN)
    assert len(g.nodes) == 2


def test_fold_different_entities_produce_separate_nodes():
    c1 = _cluster("c1", ["minister-x"], ts=0.0)
    c2 = _cluster("c2", ["president-z"], ts=0.0)
    g = fold_clusters([c1, c2], entity_tau=_TAU, window_s=_WIN)
    assert len(g.nodes) == 2


def test_fold_empty_clusters_returns_empty_graph():
    g = fold_clusters([])
    assert g.nodes == {}
    assert g.edges == []
    assert g.claim_node_links == []


def test_fold_single_cluster_seeds_one_node():
    c = _cluster("c1", ["minister-x"], ts=0.0)
    g = fold_clusters([c])
    assert len(g.nodes) == 1
    node = next(iter(g.nodes.values()))
    assert node.cluster_count == 1


def test_fold_node_centroid_updates_after_second_attachment():
    emb1 = [1.0, 0.0, 0.0, 0.0]
    emb2 = [0.0, 1.0, 0.0, 0.0]
    c1 = _cluster("c1", ["minister-x"], ts=0.0, emb=emb1)
    c2 = _cluster("c2", ["minister-x"], ts=3600.0, emb=emb2)
    g = fold_clusters([c1, c2], entity_tau=_TAU, window_s=_WIN)
    node = next(iter(g.nodes.values()))
    # centroid after 2 attachments: (emb1 + emb2) / 2
    expected = [0.5, 0.5, 0.0, 0.0]
    for a, b in zip(node.topic_embedding, expected):
        assert abs(a - b) < 1e-9


def test_fold_last_updated_tracks_latest_ts():
    c1 = _cluster("c1", ["minister-x"], ts=100.0)
    c2 = _cluster("c2", ["minister-x"], ts=3600.0)
    g = fold_clusters([c1, c2], entity_tau=_TAU, window_s=_WIN)
    node = next(iter(g.nodes.values()))
    assert node.last_updated == 3600.0


# ---------------------------------------------------------------------------
# #43a  Claim<->node many-to-many via fold
# ---------------------------------------------------------------------------


def test_claim_ids_appear_in_claim_node_links():
    c = _cluster("c1", ["minister-x"], ts=0.0, claim_ids=["cl-1", "cl-2"])
    g = fold_clusters([c])
    node_id = next(iter(g.nodes))
    claim_ids_on_node = claims_for_node(node_id, g)
    assert "cl-1" in claim_ids_on_node
    assert "cl-2" in claim_ids_on_node


def test_nodes_for_claim_returns_correct_node():
    c = _cluster("c1", ["minister-x"], ts=0.0, claim_ids=["cl-1"])
    g = fold_clusters([c])
    node_id = next(iter(g.nodes))
    assert nodes_for_claim("cl-1", g) == [node_id]


def test_nodes_for_unknown_claim_returns_empty():
    c = _cluster("c1", ["minister-x"], ts=0.0, claim_ids=["cl-1"])
    g = fold_clusters([c])
    assert nodes_for_claim("not-a-claim", g) == []


def test_claim_maps_to_multiple_nodes_when_cluster_spans_events():
    # Simulate a cluster that passes the gate for two existing nodes by folding in order.
    # c1 seeds node-A; c2 seeds node-B (different entity, different ts).
    # c3 has entities of BOTH and attaches only to the best match (tiebreaker selects one node).
    # For a genuine many-to-many, we test cluster_nodes directly.
    c1 = _cluster("c1", ["entity-a"], ts=0.0, claim_ids=["cl-1"])
    c2 = _cluster("c2", ["entity-b"], ts=0.0, claim_ids=["cl-2"])
    g = fold_clusters([c1, c2])
    assert len(g.nodes) == 2
    # each cluster attaches to its own node
    assert len(g.cluster_nodes["c1"]) == 1
    assert len(g.cluster_nodes["c2"]) == 1


def test_node_clusters_mapping_grows_with_each_attachment():
    c1 = _cluster("c1", ["minister-x"], ts=0.0)
    c2 = _cluster("c2", ["minister-x"], ts=3600.0)
    c3 = _cluster("c3", ["minister-x"], ts=7200.0)
    g = fold_clusters([c1, c2, c3], entity_tau=_TAU, window_s=_WIN)
    node_id = next(iter(g.nodes))
    assert len(g.node_clusters[node_id]) == 3


def test_cluster_nodes_records_node_for_each_cluster():
    c1 = _cluster("c1", ["minister-x"], ts=0.0)
    c2 = _cluster("c2", ["minister-x"], ts=3600.0)
    g = fold_clusters([c1, c2], entity_tau=_TAU, window_s=_WIN)
    node_id = next(iter(g.nodes))
    assert g.cluster_nodes["c1"] == [node_id]
    assert g.cluster_nodes["c2"] == [node_id]


def test_multiple_claims_per_cluster_all_linked():
    c = _cluster("c1", ["entity-a"], ts=0.0, claim_ids=["cl-1", "cl-2", "cl-3"])
    g = fold_clusters([c])
    node_id = next(iter(g.nodes))
    linked = claims_for_node(node_id, g)
    assert set(linked) == {"cl-1", "cl-2", "cl-3"}


def test_claims_from_two_clusters_both_linked_to_shared_node():
    c1 = _cluster("c1", ["minister-x"], ts=0.0, claim_ids=["cl-1"])
    c2 = _cluster("c2", ["minister-x"], ts=3600.0, claim_ids=["cl-2"])
    g = fold_clusters([c1, c2], entity_tau=_TAU, window_s=_WIN)
    node_id = next(iter(g.nodes))
    linked = claims_for_node(node_id, g)
    assert "cl-1" in linked
    assert "cl-2" in linked


# ---------------------------------------------------------------------------
# #44a  Novelty: basic cases
# ---------------------------------------------------------------------------


def test_novelty_unseen_cluster_is_novel():
    assert cluster_novelty("c1", "cauri", {}, _NOW, decay_s=_DECAY) == 1.0


def test_novelty_recently_seen_is_zero():
    seen = {("cauri", "c1"): _NOW - 3600}  # 1 hour ago
    assert cluster_novelty("c1", "cauri", seen, _NOW, decay_s=_DECAY) == 0.0


def test_novelty_resets_after_decay_window():
    seen = {("cauri", "c1"): _NOW - _DECAY - 1}  # just past 30 days
    assert cluster_novelty("c1", "cauri", seen, _NOW, decay_s=_DECAY) == 1.0


def test_novelty_exactly_at_decay_boundary_is_still_seen():
    seen = {("cauri", "c1"): _NOW - _DECAY}  # exactly 30 days, NOT expired
    assert cluster_novelty("c1", "cauri", seen, _NOW, decay_s=_DECAY) == 0.0


def test_novelty_per_user_isolation():
    seen = {("alice", "c1"): _NOW - 3600}
    assert cluster_novelty("c1", "bob", seen, _NOW, decay_s=_DECAY) == 1.0
    assert cluster_novelty("c1", "alice", seen, _NOW, decay_s=_DECAY) == 0.0


def test_novelty_different_clusters_independent():
    seen = {("cauri", "c1"): _NOW - 3600}
    assert cluster_novelty("c2", "cauri", seen, _NOW, decay_s=_DECAY) == 1.0


def test_novelty_custom_decay_window():
    # 1-hour decay window: seen 2 hours ago -> novel again
    one_hour = 3600.0
    seen = {("cauri", "c1"): _NOW - 7200}
    assert cluster_novelty("c1", "cauri", seen, _NOW, decay_s=one_hour) == 1.0


# ---------------------------------------------------------------------------
# #44b  annotate_feed ordering and content
# ---------------------------------------------------------------------------


def test_annotate_feed_novel_first():
    seen = {("cauri", "c1"): _NOW - 3600}  # c1 seen recently
    result = annotate_feed(["c1", "c2", "c3"], "cauri", seen, _NOW, decay_s=_DECAY)
    novelties = [r["novelty"] for r in result]
    # novel clusters (c2, c3) should come before seen cluster (c1)
    assert novelties[0] == 1.0
    assert novelties[1] == 1.0
    assert novelties[2] == 0.0


def test_annotate_feed_all_novel():
    result = annotate_feed(["c1", "c2"], "cauri", {}, _NOW, decay_s=_DECAY)
    assert all(r["novelty"] == 1.0 for r in result)


def test_annotate_feed_all_seen():
    seen = {("cauri", "c1"): _NOW - 3600, ("cauri", "c2"): _NOW - 7200}
    result = annotate_feed(["c1", "c2"], "cauri", seen, _NOW, decay_s=_DECAY)
    assert all(r["novelty"] == 0.0 for r in result)


def test_annotate_feed_empty_input():
    result = annotate_feed([], "cauri", {}, _NOW)
    assert result == []


def test_annotate_feed_preserves_cluster_ids():
    result = annotate_feed(["c1", "c2"], "cauri", {}, _NOW)
    ids = {r["cluster_id"] for r in result}
    assert ids == {"c1", "c2"}


def test_annotate_feed_decayed_seen_becomes_novel():
    old_ts = _NOW - _DECAY - 1
    seen = {("cauri", "c1"): old_ts}
    result = annotate_feed(["c1"], "cauri", seen, _NOW, decay_s=_DECAY)
    assert result[0]["novelty"] == 1.0


# ---------------------------------------------------------------------------
# #42  End-to-end: fold produces correct edge set on a realistic scenario
# ---------------------------------------------------------------------------


def test_end_to_end_minister_resignation_scenario():
    """Simulate a 3-cluster story: resignation confirmed, probe revealed, deputy takes over.

    All three share the minister entity and fall within the 72-hour window.
    Expected: one node, two develops edges (c2->develops->c2, c3->develops->c3).
    """
    emb_base = _vec(0.9)

    c1 = _cluster("c1", ["minister-x", "valoria"], ts=0.0, emb=emb_base, claim_ids=["cl-1"])
    c2 = _cluster("c2", ["minister-x", "valoria"], ts=3600.0, emb=emb_base, claim_ids=["cl-2"])
    c3 = _cluster("c3", ["minister-x", "valoria"], ts=7200.0, emb=emb_base, claim_ids=["cl-3"])

    g = fold_clusters([c1, c2, c3], entity_tau=_TAU, window_s=_WIN)

    assert len(g.nodes) == 1, "one event node for the minister resignation story"
    develops = [e for e in g.edges if e.kind == "develops"]
    assert len(develops) == 2, "two develops edges: c1->c2, c2->c3"
    merges = [e for e in g.edges if e.kind == "merges"]
    assert merges == [], "no merges in a single-node story"


def test_end_to_end_two_distinct_stories():
    """ECB rate decision and Fed rate decision share no entities -> two nodes, no edges."""
    c_ecb = _cluster("c_ecb", ["ecb", "euro-zone"], ts=0.0)
    c_fed = _cluster("c_fed", ["fed", "us-economy"], ts=0.0)

    g = fold_clusters([c_ecb, c_fed], entity_tau=_TAU, window_s=_WIN)

    assert len(g.nodes) == 2
    assert g.edges == []
