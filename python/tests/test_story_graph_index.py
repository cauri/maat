"""#285 — story-graph attachment uses an entity inverted index for sub-linear candidate lookup.

The attachment gate requires ≥1 shared entity, so candidates come from the entity index rather than
a scan of every node. These tests pin that the index returns EXACTLY the entity-sharing nodes (so a
disjoint node is never a candidate) and stays in sync as nodes are added — the behaviour the 97
existing story-graph tests already exercise end-to-end, now done sub-linearly.
"""

from maat.pipeline.story_graph import ClusterRow, EventNode, StoryGraph, fold_clusters


def _cluster(cid, ents, emb, ts):
    return ClusterRow(cluster_id=cid, entity_spine=ents, topic_embedding=emb, earliest_ts=ts, claim_ids=[f"{cid}-c"])


def _node(nid, ents, emb, ts):
    return EventNode(id=nid, headline=nid, entity_spine=ents, topic_embedding=emb,
                     first_seen=ts, last_updated=ts, cluster_count=1)


def test_candidate_nodes_returns_only_entity_sharing_nodes():
    shares = _node("shares", ["putin", "kyiv"], [1.0, 0.0], 0.0)
    disjoint = _node("disjoint", ["powell", "fed"], [1.0, 0.0], 0.0)
    g = StoryGraph(nodes={"shares": shares, "disjoint": disjoint})  # __post_init__ indexes both

    cluster = _cluster("c1", ["putin", "moscow"], [1.0, 0.0], 100.0)
    assert {n.id for n in g.candidate_nodes(cluster)} == {"shares"}  # disjoint node never considered

    # A cluster sharing nothing has no candidates at all (not a full-node scan that filters later).
    assert g.candidate_nodes(_cluster("c2", ["xi", "taiwan"], [1.0, 0.0], 100.0)) == []


def test_index_stays_in_sync_as_the_fold_adds_nodes():
    g = fold_clusters([
        _cluster("c1", ["putin", "kyiv"], [1.0, 0.0, 0.0], 0.0),
        _cluster("c2", ["powell", "fed"], [0.0, 1.0, 0.0], 1000.0),  # disjoint → second node
    ])
    assert len(g.nodes) == 2  # no false merge across disjoint entity spines
    # every entity points at exactly the node(s) carrying it
    for ent in ("putin", "kyiv", "powell", "fed"):
        ids = g._entity_index[ent]
        assert ids and all(ent in g.nodes[nid].entity_spine for nid in ids)


def test_index_seeded_from_existing_nodes_matches_a_fresh_build():
    nodes = {
        "n1": _node("n1", ["a", "b"], [1.0, 0.0], 0.0),
        "n2": _node("n2", ["b", "c"], [0.0, 1.0], 0.0),
    }
    g = StoryGraph(nodes=dict(nodes))  # seeded → __post_init__ must build the index
    # entity "b" is shared by both nodes; "a" only n1; "c" only n2
    assert {n.id for n in g.candidate_nodes(_cluster("x", ["b"], [1.0, 0.0], 0.0))} == {"n1", "n2"}
    assert {n.id for n in g.candidate_nodes(_cluster("y", ["a"], [1.0, 0.0], 0.0))} == {"n1"}
    assert {n.id for n in g.candidate_nodes(_cluster("z", ["c"], [1.0, 0.0], 0.0))} == {"n2"}
