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
