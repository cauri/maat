"""Incremental story-graph deltas (#42 at scale): fold only NEW clusters onto the existing graph
and emit the difference, chunked under NATS's payload cap. Pure — no DB, no bus."""

import json

from maat.pipeline.story_graph import ClusterRow, EventNode, StoryGraph, fold_incremental
from maat.pipeline.story_graph_build import (
    build_graph_incremental,
    chunk_delta,
    delta_payload,
)

_WINDOW_S = 72 * 3600


def _row(cid, spine, emb, ts, claim_ids):
    return ClusterRow(cluster_id=cid, entity_spine=spine, topic_embedding=emb,
                      earliest_ts=ts, claim_ids=claim_ids)


def _node(nid, spine, emb, ts, *, count=1):
    return EventNode(id=nid, headline="h", entity_spine=spine, topic_embedding=emb,
                     first_seen=ts, last_updated=ts, cluster_count=count)


def test_new_cluster_threads_onto_existing_node():
    existing = _node("node:x", ["ecb", "lagarde"], [1.0, 0.0], 100.0)
    new = _row("c2", ["ecb", "lagarde"], [1.0, 0.0], 100.0 + 3600, ["cl2"])  # same spine, in window
    graph, touched, created = fold_incremental([existing], [new])
    assert touched == {"node:x"} and not created           # joined the existing node, nothing new
    assert graph.node_clusters["node:x"] == ["c2"]          # delta carries ONLY the new mapping
    assert graph.nodes["node:x"].cluster_count == 2         # node state advanced
    assert any(lk.cluster_id == "c2" for lk in graph.claim_node_links)


def test_novel_cluster_seeds_a_new_node():
    new = _row("c1", ["mars", "nasa"], [0.0, 1.0], 100.0, ["cl1"])
    graph, touched, created = fold_incremental([], [new])
    assert len(created) == 1 and touched == created
    nid = next(iter(created))
    assert graph.node_clusters[nid] == ["c1"]


def test_out_of_window_cluster_does_not_thread():
    existing = _node("node:x", ["ecb"], [1.0], 100.0)
    new = _row("c2", ["ecb"], [1.0], 100.0 + _WINDOW_S * 2, ["cl2"])  # same entity, far in the future
    _, touched, created = fold_incremental([existing], [new])
    assert created and "node:x" not in touched             # gate's time window blocks the join


def test_existing_collections_start_empty_so_delta_is_only_new():
    # An existing node already has clusters in the DB; the delta must NOT re-emit them.
    existing = _node("node:x", ["ecb"], [1.0], 100.0, count=3)
    new = _row("c4", ["ecb"], [1.0], 100.0 + 60, ["cl4"])
    graph, _, _ = fold_incremental([existing], [new])
    assert graph.node_clusters["node:x"] == ["c4"]         # only the one new cluster, not all 4
    assert len(graph.claim_node_links) == 1


def test_delta_payload_centroid_only_for_active_nodes():
    active = EventNode(id="a", headline="A", entity_spine=["x"], topic_embedding=[0.1, 0.2, 0.3],
                       first_seen=0.0, last_updated=1000.0, cluster_count=1)
    settled = EventNode(id="b", headline="B", entity_spine=["y"], topic_embedding=[0.4, 0.5, 0.6],
                        first_seen=0.0, last_updated=10.0, cluster_count=1)
    g = StoryGraph(nodes={"a": active, "b": settled})
    g.node_clusters = {"a": ["ca"], "b": ["cb"]}
    payload = delta_payload(g, {"a", "b"}, active_since=500.0)
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert "topic_embedding" in by_id["a"]                  # recent → carries its centroid forward
    assert "topic_embedding" not in by_id["b"]              # settled → drops the ~1k-float vector
    assert {"node_id": "a", "cluster_id": "ca"} in payload["node_clusters"]


def test_chunk_delta_splits_stays_under_cap_and_reassembles():
    nodes = [
        {"id": f"n{i}", "headline": "x" * 40, "entity_spine": ["a", "b"],
         "first_seen": 0, "last_updated": 0, "cluster_count": 1}
        for i in range(400)
    ]
    ncs = [{"node_id": f"n{i}", "cluster_id": f"c{i}"} for i in range(400)]
    payload = {"nodes": nodes, "edges": [], "node_clusters": ncs, "claim_node_links": []}
    chunks = chunk_delta(payload, reset=True, max_bytes=5000)

    assert len(chunks) > 1                                  # genuinely split
    assert chunks[0].get("reset") is True                   # truncate rides only on the first chunk
    assert all("reset" not in c for c in chunks[1:])
    for c in chunks:
        assert len(json.dumps(c)) <= 5000 + 500             # each chunk safely under the cap

    keys = ("nodes", "edges", "node_clusters", "claim_node_links")
    merged = {k: [x for c in chunks for x in c.get(k, [])] for k in keys}
    assert merged["nodes"] == nodes and merged["node_clusters"] == ncs  # nothing lost or duplicated


def test_chunk_delta_reset_with_no_rows_still_truncates():
    chunks = chunk_delta({"nodes": [], "edges": [], "node_clusters": [], "claim_node_links": []},
                         reset=True)
    assert len(chunks) == 1 and chunks[0]["reset"] is True


def test_build_graph_incremental_headlines_created_node_with_shortest_fact():
    new = [
        {"id": "c1", "fact": "A longer sentence naming the event in full.", "claim_ids": ["cl1", "cl2"]},
    ]
    claim_text = {"cl1": "A longer sentence naming the event in full.", "cl2": "Short."}
    claim_article = {"cl1": "a1", "cl2": "a1"}
    art_ts = {"a1": 100.0}
    graph, _, created = build_graph_incremental(
        [], new, claim_text, claim_article, art_ts, [[0.0, 1.0]]
    )
    nid = next(iter(created))
    # The node headline is the shortest FACT among its clusters (here only the cluster's own fact).
    assert graph.nodes[nid].headline == "A longer sentence naming the event in full."
