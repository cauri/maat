"""Tests for the story-graph spike prototype (issue #45).

Covers the four decisions from docs/spikes/claim-node-attachment.md:
  1. Attachment gate = entity Jaccard + temporal window (AND-gate; cosine is tiebreaker)
  2. Edge inference: develops / spawns / merges
  3. Novelty is cluster-level; decay window resets after 30 days
  4. Wire-dedup for novelty falls out of corroboration (not tested here — tested in test_pipeline.py)
"""

from maat.pipeline.story_graph_proto import (
    ClusterSignal,
    EventNode,
    attach_cluster,
    cluster_novelty,
    entity_jaccard,
    passes_gate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vec(val: float, dim: int = 4) -> list[float]:
    """Return a unit-ish vector pointing mostly in one direction, for cosine tests."""
    v = [0.0] * dim
    v[0] = val
    v[1] = (1.0 - val)
    return v


def _cluster(
    cid: str,
    entities: list[str],
    ts: float = 0.0,
    emb: list[float] | None = None,
) -> ClusterSignal:
    return ClusterSignal(
        cluster_id=cid,
        entity_spine=entities,
        topic_embedding=emb or _vec(0.9),
        earliest_ts=ts,
    )


def _node(
    nid: str,
    entities: list[str],
    last_updated: float = 0.0,
    emb: list[float] | None = None,
    first_seen: float = 0.0,
) -> EventNode:
    return EventNode(
        node_id=nid,
        entity_spine=entities,
        topic_embedding=emb or _vec(0.9),
        first_seen=first_seen,
        last_updated=last_updated,
    )


# ---------------------------------------------------------------------------
# 1. Entity Jaccard
# ---------------------------------------------------------------------------


def test_entity_jaccard_full_overlap():
    assert entity_jaccard(["A", "B"], ["A", "B"]) == 1.0


def test_entity_jaccard_partial():
    j = entity_jaccard(["A", "B", "C"], ["A", "C", "D"])
    assert abs(j - 2 / 4) < 1e-9  # 2 shared, 4 union


def test_entity_jaccard_no_overlap():
    assert entity_jaccard(["A"], ["B"]) == 0.0


def test_entity_jaccard_empty():
    assert entity_jaccard([], ["A"]) == 0.0
    assert entity_jaccard(["A"], []) == 0.0


# ---------------------------------------------------------------------------
# 2. Attachment gate: both signals required
# ---------------------------------------------------------------------------

_TAU = 0.40
_WIN = 72 * 3600  # 72h


def test_gate_passes_when_both_signals_clear():
    c = _cluster("c1", ["minister-x", "country-valoria"], ts=1000.0)
    n = _node("n1", ["minister-x", "country-valoria"], last_updated=1000.0)
    assert passes_gate(c, n, entity_tau=_TAU, window_s=_WIN)


def test_gate_fails_on_low_entity_jaccard():
    c = _cluster("c1", ["president-z"], ts=1000.0)
    n = _node("n1", ["minister-x", "country-valoria"], last_updated=1000.0)
    # Jaccard = 0 / 3 = 0 < 0.40
    assert not passes_gate(c, n, entity_tau=_TAU, window_s=_WIN)


def test_gate_fails_on_stale_timestamp():
    c = _cluster("c1", ["minister-x"], ts=1000.0 + _WIN + 1)
    n = _node("n1", ["minister-x"], last_updated=1000.0)
    assert not passes_gate(c, n, entity_tau=_TAU, window_s=_WIN)


def test_gate_fails_when_no_shared_entity_despite_high_jaccard():
    # If both spines are empty, Jaccard is undefined — gate should reject
    c = _cluster("c1", [], ts=1000.0)
    n = _node("n1", [], last_updated=1000.0)
    assert not passes_gate(c, n, entity_tau=_TAU, window_s=_WIN)


# ---------------------------------------------------------------------------
# 3. attach_cluster: new node when no match
# ---------------------------------------------------------------------------


def test_attach_creates_new_node_when_no_candidates():
    c = _cluster("c1", ["president-z"], ts=0.0)
    n = _node("n1", ["minister-x"], last_updated=0.0)
    result = attach_cluster(c, [n], {}, entity_tau=_TAU, window_s=_WIN)
    assert result.is_new_node
    assert result.edges == []


def test_attach_attaches_to_matching_node():
    c = _cluster("c2", ["minister-x"], ts=3600.0)
    n = _node("n1", ["minister-x"], last_updated=0.0)
    result = attach_cluster(c, [n], {"n1": 1}, entity_tau=_TAU, window_s=_WIN)
    assert not result.is_new_node
    assert result.node_id == "n1"


def test_attach_uses_cosine_tiebreaker_on_multiple_candidates():
    # Two nodes both pass the gate; cluster embedding closer to n2.
    c = _cluster("c3", ["minister-x"], ts=0.0, emb=_vec(0.1))
    n1 = _node("n1", ["minister-x"], last_updated=0.0, emb=_vec(0.9))
    n2 = _node("n2", ["minister-x"], last_updated=0.0, emb=_vec(0.1))
    result = attach_cluster(c, [n1, n2], {"n1": 1, "n2": 1}, entity_tau=_TAU, window_s=_WIN)
    assert result.node_id == "n2"


# ---------------------------------------------------------------------------
# 4. Edge inference
# ---------------------------------------------------------------------------


def test_develops_edge_on_second_cluster():
    # A second cluster lands on the same node with high cosine similarity.
    c = _cluster("c2", ["minister-x"], ts=3600.0, emb=_vec(0.9))
    n = _node("n1", ["minister-x"], last_updated=0.0, emb=_vec(0.9))
    result = attach_cluster(c, [n], {"n1": 1}, entity_tau=_TAU, window_s=_WIN)
    assert any(e.kind == "develops" for e in result.edges)


def test_no_develops_on_first_cluster():
    # First cluster on a node: no develops edge.
    c = _cluster("c1", ["minister-x"], ts=0.0)
    n = _node("n1", ["minister-x"], last_updated=0.0)
    result = attach_cluster(c, [n], {"n1": 0}, entity_tau=_TAU, window_s=_WIN)
    assert not any(e.kind == "develops" for e in result.edges)


def test_spawns_edge_when_cluster_also_fits_diverged_other_node():
    # Cluster passes gate for n1 (primary) and n2 (other); n1 and n2 centroids have diverged.
    c = _cluster("c3", ["minister-x"], ts=0.0, emb=_vec(0.9))
    n1 = _node("n1", ["minister-x"], last_updated=0.0, emb=_vec(0.9))
    n2 = _node("n2", ["minister-x"], last_updated=0.0, emb=_vec(0.1))  # diverged from n1
    result = attach_cluster(c, [n1, n2], {"n1": 1, "n2": 0}, entity_tau=_TAU, window_s=_WIN)
    spawns = [e for e in result.edges if e.kind == "spawns"]
    assert len(spawns) >= 1
    assert spawns[0].from_id == "n1"
    assert spawns[0].to_id == "n2"


def test_merges_edge_when_two_nodes_converge():
    # Cluster passes gate for n1; n2 also passes gate; n1 and n2 centroids are converged.
    c = _cluster("c4", ["minister-x"], ts=0.0, emb=_vec(0.9))
    n1 = _node("n1", ["minister-x"], last_updated=0.0, emb=_vec(0.9), first_seen=0.0)
    n2 = _node("n2", ["minister-x"], last_updated=0.0, emb=_vec(0.85), first_seen=1.0)
    result = attach_cluster(c, [n1, n2], {"n1": 1, "n2": 1}, entity_tau=_TAU, window_s=_WIN)
    merges = [e for e in result.edges if e.kind == "merges"]
    assert len(merges) >= 1
    # older node (n1, first_seen=0) is the source
    assert merges[0].from_id == "n1"


# ---------------------------------------------------------------------------
# 5. Novelty signal
# ---------------------------------------------------------------------------

_NOW = 1_000_000.0
_DECAY = 30 * 24 * 3600  # 30 days


def test_novelty_is_1_when_cluster_unseen():
    assert cluster_novelty("c1", "cauri", {}, _NOW, decay_s=_DECAY) == 1.0


def test_novelty_is_0_when_recently_seen():
    seen = {("cauri", "c1"): _NOW - 3600}  # seen 1 hour ago
    assert cluster_novelty("c1", "cauri", seen, _NOW, decay_s=_DECAY) == 0.0


def test_novelty_resets_after_decay_window():
    seen = {("cauri", "c1"): _NOW - _DECAY - 1}  # seen just over 30 days ago
    assert cluster_novelty("c1", "cauri", seen, _NOW, decay_s=_DECAY) == 1.0


def test_novelty_is_per_user():
    seen = {("alice", "c1"): _NOW - 3600}
    assert cluster_novelty("c1", "bob", seen, _NOW, decay_s=_DECAY) == 1.0
    assert cluster_novelty("c1", "alice", seen, _NOW, decay_s=_DECAY) == 0.0
