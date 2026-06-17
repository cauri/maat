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


def test_build_deeper_expands_provenance():
    # #56 — Tier-3 server-computed provenance: cross-language spread, primary list, originators.
    from maat.serving.feed import build_deeper

    cluster = {"originators": [["a1"], ["a2", "a3"]]}
    claims = [
        {"id": "c1", "source": "reuters.com", "language": "en"},
        {"id": "c2", "source": "lemonde.fr", "language": "fr"},
        {"id": "c3", "source": "ecb.europa.eu", "language": "en"},
    ]
    article_meta = {
        "a1": {"source": "reuters.com"},
        "a2": {"source": "lemonde.fr"},
        "a3": {"source": "lemonde.fr"},
    }
    d = build_deeper(cluster, claims, article_meta)
    assert {lang["language"] for lang in d["languages"]} == {"en", "fr"}  # cross-language spread
    assert "ecb.europa.eu" in d["primary_sources"]  # primary source surfaced
    assert len(d["originators"]) == 2  # per-originator breakdown
    assert d["source_count"] == 3


def test_annotate_accuracy_tags_stories_by_normalised_fact():
    # #38 — accuracy-axis lifecycle state attached per story, matched on the normalised fact.
    from maat.serving.feed import _annotate_accuracy

    payload = {"stories": [
        {"id": "c1", "fact": "Reyes  Resigns"},  # extra space + mixed case
        {"id": "c2", "fact": "Tokyo summit opens"},
    ]}
    out = _annotate_accuracy(payload, {"reyes resigns": "resolved"})
    assert out["stories"][0]["accuracy_state"] == "resolved"
    assert "accuracy_state" not in out["stories"][1]  # no history → unannotated


def test_annotate_accuracy_extracts_enum_value():
    from maat.learning.accuracy import LifecycleState
    from maat.serving.feed import _annotate_accuracy

    out = _annotate_accuracy({"stories": [{"id": "c1", "fact": "x"}]}, {"x": LifecycleState.RESOLVED})
    assert out["stories"][0]["accuracy_state"] == LifecycleState.RESOLVED.value


def test_preferences_payload_serialises_ranked():
    # #35 — learned acquisition preferences serialised for /api/v2/source-preferences.
    from types import SimpleNamespace

    from maat.serving.feed import _preferences_payload

    prefs = SimpleNamespace(
        ranked=[SimpleNamespace(
            source="reuters.com", rank=1, acquisition_weight=0.3, confirmation_rate=0.9,
            independent_rate=0.8, in_diversity_floor=False, low_evidence=False,
        )],
        diversity_floor=frozenset({"azertag.az"}),
    )
    out = _preferences_payload(prefs)
    assert out["ranked"][0]["source"] == "reuters.com"
    assert out["ranked"][0]["acquisition_weight"] == 0.3
    assert out["diversity_floor"] == ["azertag.az"]


def test_source_preference_dataclass_has_serialised_fields():
    # guards the serialiser above against drift in the real dataclass field names
    import dataclasses

    from maat.learning.source_learning import SourcePreference

    names = {f.name for f in dataclasses.fields(SourcePreference)}
    assert {"source", "rank", "acquisition_weight", "in_diversity_floor", "low_evidence"} <= names


def test_reputation_map_serialises_per_source():
    # #199 regression: _reputation_map must read the reputation_score FOLD over REAL SourceReputation
    # records — NOT a `.reputation` attribute (there isn't one). The old mock used SimpleNamespace(
    # reputation=...), so the unit test was green while `?reputation=1` 500'd in prod on the real
    # fold output. This passes genuine fold_reputation records through, the shape the endpoint uses.
    from maat.learning.reputation import fold_reputation, reputation_score
    from maat.serving.feed import _reputation_map

    history = [
        {
            "fact": "central bank holds rates",
            "sources": ["reuters.com", "blog.example"],
            "originators": [["a1"], ["b1"]],
            "independent_originators": 2,
            "has_primary": False,
            "extremity": "notable",
            "confidence": 0.8,
        }
    ]
    reps = fold_reputation(history)
    assert reps, "fixture should fold to at least one SourceReputation"
    out = _reputation_map(reps)
    # Real records flow through without AttributeError and collapse via the reputation_score fold.
    assert out == {r.source: reputation_score(r) for r in reps}
    assert all(isinstance(v, float) and 0.0 <= v <= 1.0 for v in out.values())


def test_filter_denied_drops_fully_denied_stories():
    # #187 — operator source-deny enforcement on the served feed.
    from maat.serving.feed import _filter_denied

    payload = {"count": 3, "stories": [
        {"id": "c1", "originator_groups": [{"sources": ["spam.example"]}, {"sources": ["gamed.example"]}]},
        {"id": "c2", "originator_groups": [{"sources": ["reuters.com"]}, {"sources": ["spam.example"]}]},
        {"id": "c3", "originator_groups": [{"sources": ["bbc.com"]}]},
    ]}
    out = _filter_denied(payload, {"spam.example", "gamed.example"})
    assert [s["id"] for s in out["stories"]] == ["c2", "c3"]  # fully-denied c1 dropped; mixed c2 kept
    assert out["count"] == 2
    assert _filter_denied(payload, set()) is payload  # no denials → untouched
