"""#50 — the Feed API's optional natural-language topic filter (parse_interest + story_matches
wired into /api/v2/feed). Pure: no DB, no HTTP."""

from maat.serving.feed import _filter_by_topics

_PAYLOAD = {
    "generated_at": "2026-06-16T00:00:00Z",
    "count": 3,
    "stories": [
        {"id": "flood", "fact": "Heavy flooding hits coastal towns",
         "claims": [{"text": "Authorities ordered evacuations as the storm intensified."}]},
        {"id": "ai", "fact": "New frontier AI model released",
         "claims": [{"text": "Benchmark scores were published alongside a safety card."}]},
        {"id": "ghana", "fact": "Ghana parliament debates the budget",
         "claims": [{"text": "West African politics dominated the session in Accra."}]},
    ],
}


def test_no_topics_returns_full_feed_unchanged():
    out = _filter_by_topics(_PAYLOAD, "")
    assert out is _PAYLOAD  # untouched object → existing clients unaffected
    assert _filter_by_topics(_PAYLOAD, "   ")["count"] == 3


def test_single_topic_filters_to_matching_stories():
    out = _filter_by_topics(_PAYLOAD, "flooding")
    assert [s["id"] for s in out["stories"]] == ["flood"]
    assert out["count"] == 1


def test_multiple_topics_union():
    out = _filter_by_topics(_PAYLOAD, "flooding, West African politics")
    assert {s["id"] for s in out["stories"]} == {"flood", "ghana"}
    assert out["count"] == 2


def test_topic_matches_claim_text_not_just_fact():
    # "evacuations" appears only in the claim body, not the fact headline.
    out = _filter_by_topics(_PAYLOAD, "evacuations")
    assert [s["id"] for s in out["stories"]] == ["flood"]


def test_thread_payload_groups_clusters_into_threads():
    # #42/#44 — the feed tags each story with its event-node and surfaces multi-cluster threads.
    from maat.serving.feed import _thread_payload

    payload = {"count": 3, "stories": [
        {"id": "c1", "fact": "Reyes resigns"},
        {"id": "c2", "fact": "Reyes faces backlash"},
        {"id": "c3", "fact": "Tokyo hosts a summit"},
    ]}
    cluster_node = {"c1": "n1", "c2": "n1", "c3": "n2"}
    node_meta = {"n1": {"headline": "Reyes resigns"}, "n2": {"headline": "Tokyo hosts a summit"}}
    node_edges = {"n1": [{"kind": "develops", "to": "c2"}]}
    out = _thread_payload(payload, cluster_node, node_meta, node_edges)
    assert out["stories"][0]["node_id"] == "n1"
    assert out["stories"][0]["node_headline"] == "Reyes resigns"
    assert len(out["threads"]) == 1  # only the 2-cluster event-node is a thread
    t = out["threads"][0]
    assert t["node_id"] == "n1"
    assert set(t["cluster_ids"]) == {"c1", "c2"}
    assert t["edges"] == [{"kind": "develops", "to": "c2"}]


def test_thread_payload_without_graph_is_flat():
    from maat.serving.feed import _thread_payload

    payload = {"count": 1, "stories": [{"id": "c1", "fact": "x"}]}
    out = _thread_payload(payload, {}, {}, {})
    assert out["threads"] == []
    assert "node_id" not in out["stories"][0]
