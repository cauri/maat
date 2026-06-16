"""#42/#43 — building story-graph inputs from live cluster data. Pure: no DB, no LLM."""

from maat.pipeline.story_graph_build import cluster_row, entity_spine_heuristic


def test_entity_spine_extracts_proper_nouns_and_strips_leading_stopwords():
    spine = entity_spine_heuristic(
        "The European Central Bank raised rates as Christine Lagarde spoke in Frankfurt."
    )
    assert "european central bank" in spine
    assert "christine lagarde" in spine
    assert "frankfurt" in spine
    # leading "The" stripped, no bare stopwords as entities
    assert "the" not in spine
    assert "the european central bank" not in spine


def test_entity_spine_dedupes_repeated_entities():
    # the same proper noun, separated by lowercase connectors, collapses to one entity
    spine = entity_spine_heuristic("Reyes met with aides, and Reyes later resigned, and Reyes fled.")
    assert spine == ["reyes"]


def test_entity_spine_caps_at_max():
    spine = entity_spine_heuristic(
        "Reyes or Lopez or Chen or Park or Singh or Costa attended", max_entities=5
    )
    assert len(spine) == 5


def test_cluster_row_assembles_all_fields():
    row = cluster_row(
        "c1",
        "Minister Reyes resigns amid scandal",
        ["Reyes quit on Tuesday", "The minister stepped down"],
        ["cl1", "cl2"],
        [0.1, 0.2, 0.3],
        1000.0,
    )
    assert row.cluster_id == "c1"
    assert row.claim_ids == ["cl1", "cl2"]
    assert row.topic_embedding == [0.1, 0.2, 0.3]
    assert row.earliest_ts == 1000.0
    assert "reyes" in row.entity_spine  # entity spine drawn from fact + claims


def test_cluster_row_accepts_custom_entity_fn():
    row = cluster_row("c2", "anything", [], ["x"], [0.0], 0.0, entity_fn=lambda t: ["fixed"])
    assert row.entity_spine == ["fixed"]


def test_build_graph_threads_same_entity_clusters_into_one_node():
    # Two clusters about the same event (shared "Reyes", close in time, high topic similarity)
    # attach to ONE event-node — the threading the feed needs.
    from maat.pipeline.story_graph_build import build_graph, graph_payload

    clusters = [
        {"id": "c1", "fact": "Reyes resigns", "claim_ids": ["a1"]},
        {"id": "c2", "fact": "Reyes faces backlash over the exit", "claim_ids": ["a2"]},
    ]
    claim_text = {"a1": "Reyes quit", "a2": "Reyes under pressure"}
    claim_article = {"a1": "art1", "a2": "art2"}
    art_ts = {"art1": 1000.0, "art2": 2000.0}
    embeddings = [[1.0, 0.0], [0.99, 0.01]]  # high cosine → developing story
    graph = build_graph(clusters, claim_text, claim_article, art_ts, embeddings)
    assert len(graph.nodes) == 1
    node = next(iter(graph.nodes.values()))
    assert node.headline == "Reyes resigns"  # shortest corroborated fact names the event
    payload = graph_payload(graph)
    assert {nc["cluster_id"] for nc in payload["node_clusters"]} == {"c1", "c2"}
    assert {lk["claim_id"] for lk in payload["claim_node_links"]} == {"a1", "a2"}  # #43 bidirectional map


def test_build_graph_keeps_distinct_events_separate():
    from maat.pipeline.story_graph_build import build_graph

    clusters = [
        {"id": "c1", "fact": "Reyes resigns in Madrid", "claim_ids": ["a1"]},
        {"id": "c2", "fact": "Tokyo hosts a climate summit", "claim_ids": ["a2"]},
    ]
    claim_text = {"a1": "Reyes quit", "a2": "the summit opens"}
    claim_article = {"a1": "art1", "a2": "art2"}
    art_ts = {"art1": 1000.0, "art2": 2000.0}
    embeddings = [[1.0, 0.0], [0.0, 1.0]]  # orthogonal + disjoint entities → unrelated
    graph = build_graph(clusters, claim_text, claim_article, art_ts, embeddings)
    assert len(graph.nodes) == 2
