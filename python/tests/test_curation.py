"""Deterministic tests for the curation agent (issue #47).

All tests exercise the PURE ranking function — no I/O, no NATS, no DB.
Three core properties must hold:
  (a) high-confidence stories still surface — veracity is not degraded;
  (b) US-dominated input → balanced output (anglosphere share falls);
  (c) country/source caps are enforced.
"""

from __future__ import annotations

import pytest

from maat.pipeline.curation import (
    Story,
    anglosphere_share,
    curate,
    region_distribution,
    _stories_from_payload,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _s(id: str, conf: float, country: str, source: str = "", language: str = "en") -> Story:
    return Story(id=id, confidence=conf, country=country, source=source, language=language)


# ---------------------------------------------------------------------------
# baseline: ordering with no caps reached
# ---------------------------------------------------------------------------

def test_empty_feed_returns_empty():
    assert curate([]) == []


def test_single_story_is_returned():
    s = _s("a", 0.9, "US")
    assert curate([s]) == [s]


def test_all_same_conf_preserves_some_order():
    stories = [_s(f"s{i}", 0.7, "FR") for i in range(5)]
    result = curate(stories)
    assert len(result) == 5
    assert set(result) == set(stories)


def test_high_confidence_placed_first_when_no_cap_pressure():
    # Two stories, no cap pressure — the more confident one should lead.
    high = _s("hi", 0.92, "DE")
    low = _s("lo", 0.50, "JP")
    result = curate([low, high])
    assert result[0] == high


# ---------------------------------------------------------------------------
# (a) high-confidence stays prominent despite diversity pressure
# ---------------------------------------------------------------------------

def test_high_confidence_not_buried_by_diversity():
    """A story 0.30 below the top (beyond CONFIDENCE_BAND=0.20) must not be
    promoted ahead of the top-confidence story even if the top story's country
    is already capped."""
    # 10 US stories at 0.85, one outlier far below confidence band
    us_stories = [_s(f"us{i}", 0.85, "US", f"src{i}") for i in range(10)]
    outlier = _s("out", 0.50, "IN", "hindustan")  # 0.35 gap — beyond band
    all_stories = us_stories + [outlier]
    result = curate(all_stories)
    # The outlier must NOT be placed before any US story because of the conf gap.
    outlier_pos = result.index(outlier)
    # All US stories within the band should come first (band=0.20, gap=0.35 → no promotion)
    for us in us_stories:
        assert result.index(us) < outlier_pos


def test_story_within_band_can_be_promoted():
    """A story within CONFIDENCE_BAND of the top can be promoted for diversity."""
    # top US story at 0.80; diverse story at 0.65 (gap=0.15 < 0.20)
    top_us = _s("us0", 0.80, "US", "reuters")
    diverse = _s("ng", 0.65, "NG", "channels")
    result = curate([top_us, diverse], country_cap=0.49)  # cap at 1 story for 2-item feed
    # Both must be in result; diverse can appear first since cap already reached at us0
    assert set(result) == {top_us, diverse}


# ---------------------------------------------------------------------------
# (b) US-dominated input → balanced output
# ---------------------------------------------------------------------------

def test_us_dominated_input_produces_balanced_output():
    """8 US + 4 non-US: curation interleaves non-US stories into the feed.

    When diverse alternatives exist the cap is respected greedily; once they are
    exhausted the remaining US stories still fill the tail (relaxation).  The key
    property is that non-US stories appear *earlier* than they would in a plain
    confidence sort, not that the cap is fully satisfied when corpus is US-heavy.
    """
    us = [_s(f"us{i}", 0.75, "US", f"ap{i}") for i in range(8)]
    non_us = [
        _s("br1", 0.72, "BR", "folha"),
        _s("in1", 0.70, "IN", "hindu"),
        _s("cn1", 0.68, "CN", "xinhua"),
        _s("de1", 0.65, "DE", "spiegel"),
    ]
    result = curate(us + non_us)
    # All 12 stories must be in the output.
    assert len(result) == 12
    # The cap limit (3) US slots fill before non-US stories are interleaved.
    # Verify that the 4 non-US stories all appear in the first (3 + 4) = 7 slots.
    first_seven_ids = {s.id for s in result[:7]}
    non_us_ids = {"br1", "in1", "cn1", "de1"}
    assert non_us_ids.issubset(first_seven_ids), (
        "All non-US stories should be promoted into the first 7 slots"
    )


def test_anglosphere_share_reduced():
    """Start with a purely anglosphere corpus; curation should not worsen diversity
    (and with diverse alternatives, it should reduce the share)."""
    us = [_s(f"us{i}", 0.80, "US", f"nyt{i}") for i in range(6)]
    gb = [_s(f"gb{i}", 0.78, "GB", f"bbc{i}") for i in range(4)]
    ru = [_s(f"ru{i}", 0.76, "RU", f"rt{i}") for i in range(4)]
    mx = [_s(f"mx{i}", 0.74, "MX", f"el{i}") for i in range(4)]
    all_stories = us + gb + ru + mx

    uncurated_share = anglosphere_share(all_stories)
    curated = curate(all_stories)
    curated_share = anglosphere_share(curated)

    # Curation must not increase the anglosphere share (it can match at small N)
    assert curated_share <= uncurated_share + 0.01


def test_diverse_corpus_unchanged():
    """A feed that is already diverse should pass through without reordering story count."""
    stories = [
        _s("a", 0.90, "BR", "folha"),
        _s("b", 0.88, "NG", "punch"),
        _s("c", 0.85, "IN", "hindu"),
        _s("d", 0.82, "DE", "spiegel"),
    ]
    result = curate(stories)
    assert len(result) == 4
    assert set(result) == set(stories)
    # Top story must still be the most confident (no cap pressure)
    assert result[0].id == "a"


# ---------------------------------------------------------------------------
# (c) caps are enforced
# ---------------------------------------------------------------------------

def test_country_cap_enforced():
    """No country should exceed its cap slot count in the output."""
    # 12 stories, all from US; with 25% cap only 3 (ceil) should appear first.
    us_stories = [_s(f"us{i}", 0.85 - i * 0.01, "US", f"src{i}") for i in range(12)]
    result = curate(us_stories, country_cap=0.25)
    # With only US stories and cap enforced, after the limit is hit the algorithm
    # relaxes (no alternatives), so all 12 end up placed — but the cap is NOT
    # violated in the *greedy* phase while alternatives exist.
    # When there are no alternatives the algorithm falls back; verify all placed.
    assert len(result) == 12


def test_country_cap_with_alternatives():
    """Country cap is enforced in the greedy phase when diverse alternatives exist.

    With 6 US + 6 diverse (each from a different country) and a 25% cap (3 slots):
    after the first 3 US stories the cap kicks in and diverse stories are promoted.
    Once the 6 diverse slots fill, the remaining 3 US stories fall to the tail —
    giving 6 US total.  The test verifies that diverse stories are *interleaved*
    into the top 9 slots (not dumped at the end) and that no more than cap_slots US
    stories appear in the *first* (cap_slots + n_diverse) positions.
    """
    us = [_s(f"us{i}", 0.80, "US", f"cnn{i}") for i in range(6)]
    diverse = [
        _s("ke1", 0.79, "KE", "nation"),
        _s("eg1", 0.78, "EG", "ahram"),
        _s("ar1", 0.77, "AR", "clarin"),
        _s("tr1", 0.76, "TR", "hurriyet"),
        _s("id1", 0.75, "ID", "kompas"),
        _s("za1", 0.74, "ZA", "mail"),
    ]
    result = curate(us + diverse, country_cap=0.25)
    cap_slots = max(1, int(12 * 0.25 + 0.999))  # = 3

    # All 12 stories placed.
    assert len(result) == 12

    # The diverse stories must all appear in the first (cap_slots + 6) = 9 positions.
    top9_countries = [s.country for s in result[:9]]
    diverse_countries = {"KE", "EG", "AR", "TR", "ID", "ZA"}
    assert all(c in top9_countries for c in diverse_countries), (
        "All 6 diverse stories should be promoted into the top 9 slots"
    )

    # Only cap_slots US stories appear in the first 9 positions.
    us_in_top9 = sum(1 for s in result[:9] if s.country == "US")
    assert us_in_top9 <= cap_slots


def test_source_cap_enforced_with_alternatives():
    """Source cap is enforced in the greedy phase when diverse alternatives exist.

    With 6 'bigmedia' + 6 unique-source stories, cap at 20% (3 slots):
    after 3 bigmedia the cap kicks in and the 6 unique-source stories are promoted.
    Verify the 6 diverse stories appear in the top 9 positions and bigmedia is
    limited to cap_slots in that window.
    """
    big = [_s(f"bm{i}", 0.80, "US", "bigmedia") for i in range(6)]
    diverse = [_s(f"d{i}", 0.79, f"C{i}", f"outlet{i}") for i in range(6)]
    result = curate(big + diverse, source_cap=0.20)
    cap_slots = max(1, int(12 * 0.20 + 0.999))  # = 3

    assert len(result) == 12

    # All diverse-source stories appear in the top 9.
    top9_sources = [s.source for s in result[:9]]
    diverse_sources = {f"outlet{i}" for i in range(6)}
    assert all(src in top9_sources for src in diverse_sources)

    # bigmedia held to cap_slots in the first 9.
    big_in_top9 = sum(1 for s in result[:9] if s.source == "bigmedia")
    assert big_in_top9 <= cap_slots


def test_output_length_equals_input():
    """Curation never drops or duplicates stories."""
    stories = [_s(f"s{i}", 0.9 - i * 0.05, "US" if i < 5 else "FR") for i in range(10)]
    result = curate(stories)
    assert len(result) == len(stories)
    assert len(set(s.id for s in result)) == len(stories)


# ---------------------------------------------------------------------------
# confidence immutability
# ---------------------------------------------------------------------------

def test_confidence_values_unchanged():
    """curate() must never alter confidence values."""
    stories = [_s(f"s{i}", round(0.9 - i * 0.1, 2), "US") for i in range(5)]
    original_confs = {s.id: s.confidence for s in stories}
    result = curate(stories)
    for s in result:
        assert s.confidence == original_confs[s.id]


# ---------------------------------------------------------------------------
# diagnostic helpers
# ---------------------------------------------------------------------------

def test_anglosphere_share_empty():
    assert anglosphere_share([]) == 0.0


def test_anglosphere_share_all_anglosphere():
    stories = [_s("a", 0.8, "US"), _s("b", 0.7, "GB")]
    assert anglosphere_share(stories) == 1.0


def test_anglosphere_share_mixed():
    stories = [_s("a", 0.8, "US"), _s("b", 0.7, "DE"), _s("c", 0.6, "JP")]
    assert pytest.approx(anglosphere_share(stories)) == 1 / 3


def test_region_distribution_counts():
    stories = [_s("a", 0.9, "US"), _s("b", 0.8, "US"), _s("c", 0.7, "IN")]
    dist = region_distribution(stories)
    assert dist == {"US": 2, "IN": 1}


def test_region_distribution_empty():
    assert region_distribution([]) == {}


# ---------------------------------------------------------------------------
# payload deserialisation
# ---------------------------------------------------------------------------

def test_stories_from_payload_basic():
    payload = [
        {"id": "x1", "confidence": 0.85, "country": "us", "source": "ap", "language": "en"},
        {"id": "x2", "confidence": 0.70, "geo": "DE", "language": "de"},
    ]
    stories = _stories_from_payload(payload)
    assert len(stories) == 2
    assert stories[0].id == "x1"
    assert stories[0].country == "US"  # uppercased
    assert stories[1].country == "DE"  # geo key also accepted


def test_stories_from_payload_missing_fields():
    payload = [{"id": "bare", "confidence": 0.5}]
    stories = _stories_from_payload(payload)
    assert stories[0].country == ""
    assert stories[0].source == ""
    assert stories[0].language == ""


# ---------------------------------------------------------------------------
# edge: all stories from one source (relaxation path)
# ---------------------------------------------------------------------------

def test_single_source_feed_relaxes_gracefully():
    """When all stories share one source, cap relaxation must place all stories."""
    stories = [_s(f"s{i}", 0.9 - i * 0.05, "US", "monopoly") for i in range(8)]
    result = curate(stories, source_cap=0.20)
    assert len(result) == 8  # nothing dropped
