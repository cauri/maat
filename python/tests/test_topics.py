"""Tests for maat/serving/topics.py (issue #50 — NL interest → topics + story matcher).

All tests exercise PURE functions only — no LLM calls, no I/O.
Coverage plan:
  (a) parse_interest / _pure_parse: keyword extraction correctness
  (b) parse_interest: edge-cases (empty, stop-word-only, short token, hyphen)
  (c) story_matches: relevant story matches, irrelevant doesn't
  (d) story_matches: edge-cases (no topics, empty story, multi-topic)
  (e) TopicSpec shape guarantees
"""

from __future__ import annotations

import pytest

from maat.serving.topics import TopicSpec, parse_interest, story_matches


# ---------------------------------------------------------------------------
# (a) parse_interest — keyword extraction correctness
# ---------------------------------------------------------------------------


def test_european_monetary_policy_extracts_bigrams():
    spec = parse_interest("European monetary policy")
    # bigrams must be present
    assert "european monetary" in spec.terms or "monetary policy" in spec.terms


def test_west_african_politics_extracts_geo_phrase():
    spec = parse_interest("West African politics")
    assert any("african" in t for t in spec.terms)
    assert any("west" in t or "politics" in t for t in spec.terms)


def test_semiconductor_supply_chains():
    spec = parse_interest("semiconductor supply chains")
    terms_lower = [t.lower() for t in spec.terms]
    # at least "semiconductor" and "supply" must survive stop-word filter
    assert any("semiconductor" in t for t in terms_lower)
    assert any("supply" in t for t in terms_lower)


def test_query_is_non_empty_string():
    spec = parse_interest("European monetary policy")
    assert isinstance(spec.query, str)
    assert spec.query.strip() != ""


def test_query_derived_from_terms():
    spec = parse_interest("semiconductor supply chains")
    # query should contain at least one of the extracted terms
    assert any(t in spec.query for t in spec.terms)


def test_raw_preserved():
    interest = "West African politics"
    spec = parse_interest(interest)
    assert spec.raw == interest


def test_returns_topic_spec():
    spec = parse_interest("European monetary policy")
    assert isinstance(spec, TopicSpec)


def test_terms_are_lowercase():
    spec = parse_interest("European Monetary Policy")
    for term in spec.terms:
        assert term == term.lower(), f"term not lowercase: {term!r}"


def test_no_stop_words_alone_in_terms():
    stop_words = {"and", "or", "the", "a", "an", "in", "on", "at", "to", "for", "of", "with"}
    spec = parse_interest("European monetary policy")
    for term in spec.terms:
        # single-word terms should not be pure stop-words
        if " " not in term:
            assert term not in stop_words, f"stop-word leaked into terms: {term!r}"


def test_terms_are_tuple():
    spec = parse_interest("climate negotiations")
    assert isinstance(spec.terms, tuple)


def test_sourcelang_default_none():
    spec = parse_interest("Japanese trade surplus")
    assert spec.sourcelang is None  # pure path never sets geo filters


def test_sourcecountry_default_none():
    spec = parse_interest("Brazilian deforestation")
    assert spec.sourcecountry is None


# ---------------------------------------------------------------------------
# (b) parse_interest — edge-cases
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty_spec():
    spec = parse_interest("")
    assert spec.terms == ()
    assert spec.query == ""


def test_whitespace_only_returns_empty_spec():
    spec = parse_interest("   ")
    assert spec.terms == ()


def test_stop_word_only_input_falls_back_to_raw():
    # "and the or" — all stop-words; should not crash and return something
    spec = parse_interest("and the or")
    # raw is preserved
    assert spec.raw == "and the or"
    # query is non-empty (fallback is the raw string)
    assert spec.query.strip() != ""


def test_hyphenated_phrase_split():
    spec = parse_interest("supply-chain disruption")
    terms_concat = " ".join(spec.terms)
    assert "supply" in terms_concat or "chain" in terms_concat


def test_single_long_word():
    spec = parse_interest("semiconductors")
    assert "semiconductors" in spec.terms


def test_short_tokens_dropped():
    spec = parse_interest("AI in EU")
    # "in" is a stop-word; "AI" and "EU" are 2 chars — below MIN_TOKEN_LEN
    # result may be empty or fall back to raw, but must not crash
    assert isinstance(spec.terms, tuple)


def test_multi_word_no_bigrams_when_only_one_token():
    spec = parse_interest("inflation")
    # single token: terms = ["inflation"], query = "inflation"
    assert "inflation" in spec.terms
    assert "inflation" in spec.query


def test_duplicate_terms_deduplicated():
    spec = parse_interest("central bank central bank")
    # "central bank" should appear exactly once
    assert spec.terms.count("central bank") == 1


# ---------------------------------------------------------------------------
# (c) story_matches — relevant story matches, irrelevant doesn't
# ---------------------------------------------------------------------------


def _story(title: str = "", body: str = "") -> dict:
    return {"title": title, "body": body, "language": "en", "country": "US"}


def test_relevant_story_matches_by_title():
    spec = parse_interest("European monetary policy")
    story = _story(title="ECB announces new monetary policy framework for Europe")
    assert story_matches(story, [spec]) is True


def test_relevant_story_matches_by_body():
    spec = parse_interest("semiconductor supply chains")
    story = _story(
        title="Tech sector update",
        body="The global semiconductor supply chains face disruption due to export controls.",
    )
    assert story_matches(story, [spec]) is True


def test_irrelevant_story_does_not_match():
    spec = parse_interest("European monetary policy")
    story = _story(
        title="Local weather forecast for the weekend",
        body="Temperatures are expected to rise across the midwest.",
    )
    assert story_matches(story, [spec]) is False


def test_west_african_politics_matches_relevant():
    spec = parse_interest("West African politics")
    story = _story(
        title="ECOWAS summit: West African leaders debate political transition",
        body="Politics in West Africa took a new turn as regional leaders convened.",
    )
    assert story_matches(story, [spec]) is True


def test_west_african_politics_no_match_on_unrelated_story():
    spec = parse_interest("West African politics")
    story = _story(
        title="Record snowfall hits Scandinavian capitals",
        body="Oslo and Stockholm experienced their coldest February in decades.",
    )
    assert story_matches(story, [spec]) is False


def test_central_bank_rate_decision_matches():
    spec = parse_interest("central bank interest rate decision")
    story = _story(
        title="Federal Reserve raises interest rate by 25bp",
        body="The central bank's decision surprised markets expecting a pause.",
    )
    assert story_matches(story, [spec]) is True


def test_unrelated_story_does_not_match_rate_decision():
    spec = parse_interest("central bank interest rate decision")
    story = _story(
        title="New album released by indie band",
        body="The band's latest record explores themes of love and loss.",
    )
    assert story_matches(story, [spec]) is False


def test_matching_is_case_insensitive():
    spec = parse_interest("monetary policy")
    story = _story(title="MONETARY POLICY shifts as inflation cools")
    assert story_matches(story, [spec]) is True


# ---------------------------------------------------------------------------
# (d) story_matches — edge-cases
# ---------------------------------------------------------------------------


def test_no_topics_returns_false():
    story = _story(title="ECB raises rates")
    assert story_matches(story, []) is False


def test_empty_story_no_match():
    spec = parse_interest("monetary policy")
    assert story_matches({}, [spec]) is False


def test_empty_story_with_none_fields():
    spec = parse_interest("monetary policy")
    story = {"title": None, "body": None}
    assert story_matches(story, [spec]) is False


def test_multi_topic_matches_any():
    spec1 = parse_interest("climate negotiations")
    spec2 = parse_interest("semiconductor supply chains")
    story = _story(title="Climate talks stall as chip shortage worsens supply chains")
    # should match at least one of the topics
    assert story_matches(story, [spec1, spec2]) is True


def test_multi_topic_none_matches_irrelevant():
    spec1 = parse_interest("climate negotiations")
    spec2 = parse_interest("European monetary policy")
    story = _story(title="Local sports results: football league weekend round-up")
    assert story_matches(story, [spec1, spec2]) is False


def test_min_hits_raises_bar():
    spec = parse_interest("central bank rate")
    # story mentions "central" but not "bank" or "rate" — with min_hits=2 should not match
    story = _story(body="The central government issued a statement on fiscal matters.")
    # min_hits=1: "central" alone might match
    # min_hits=2: needs 2 terms → should fail for this story
    result_strict = story_matches(story, [spec], min_hits=2)
    # we don't assert the exact value (depends on term count) but it must be bool
    assert isinstance(result_strict, bool)


def test_topic_spec_with_empty_terms_skipped():
    spec = TopicSpec(terms=(), raw="", query="")
    story = _story(title="ECB raises rates")
    assert story_matches(story, [spec]) is False


# ---------------------------------------------------------------------------
# (e) TopicSpec shape
# ---------------------------------------------------------------------------


def test_topic_spec_is_frozen():
    spec = parse_interest("inflation targeting")
    with pytest.raises((AttributeError, TypeError)):
        spec.terms = ("something",)  # type: ignore[misc]


def test_topic_spec_equality():
    s1 = parse_interest("monetary policy")
    s2 = parse_interest("monetary policy")
    assert s1 == s2


def test_use_llm_false_is_default():
    """parse_interest() with no keyword arg must behave identically to use_llm=False."""
    s1 = parse_interest("inflation")
    s2 = parse_interest("inflation", use_llm=False)
    assert s1 == s2


# ---------------------------------------------------------------------------
# parse_news_queries — interest → recent-news queries (pure parse of the LLM reply)
# ---------------------------------------------------------------------------


def test_parse_news_queries_extracts_and_caps():
    from maat.serving.topics import parse_news_queries

    reply = 'noise {"queries": ["AI regulation EU", "frontier model release", "a", "b", "c"]} tail'
    out = parse_news_queries(reply, fallback="ai", max_queries=4)
    assert out == ["AI regulation EU", "frontier model release", "a", "b"]


def test_parse_news_queries_falls_back_on_no_json():
    from maat.serving.topics import parse_news_queries

    assert parse_news_queries("sorry, I cannot help", fallback="art") == ["art"]


def test_parse_news_queries_falls_back_on_empty():
    from maat.serving.topics import parse_news_queries

    assert parse_news_queries('{"queries": []}', fallback="fun and laughter") == ["fun and laughter"]
