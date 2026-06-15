"""Tests for the source gate's pure logic (prefilter / parse / orchestration — no LLM call)."""

from __future__ import annotations

from maat.acquire import source_gate as sg


def test_prefilter_drops_encyclopedia_and_social_but_not_youtube():
    assert sg.prefiltered_reject("en.wikipedia.org") is True
    assert sg.prefiltered_reject("www.reddit.com") is True
    assert sg.prefiltered_reject("facebook.com") is True
    # News outlets publish on YouTube, so it is NOT prefiltered — the classifier judges the channel.
    assert sg.prefiltered_reject("youtube.com") is False
    assert sg.prefiltered_reject("reuters.com") is False


def test_parse_verdict():
    v = sg.parse_verdict('noise {"accept": true, "kind": "news", "outlet": "Reuters"} tail')
    assert v is not None and v.accept and v.kind == "news" and v.outlet == "Reuters"
    assert sg.parse_verdict("no json here") is None


def test_accept_source_prefilter_known_good_and_cache_skip_the_llm():
    cache: dict = {}
    # prefiltered → reject without an LLM call
    assert sg.accept_source("en.wikipedia.org", "Inflation", cache=cache).accept is False
    # already in the corpus (known-good) → accept without an LLM call
    assert sg.accept_source("reuters.com", "x", known_good=frozenset({"reuters.com"}), cache=cache).accept
    # second look at a cached domain returns the cached verdict (no reclassification)
    assert sg.accept_source("reuters.com", "different headline", cache=cache).accept
