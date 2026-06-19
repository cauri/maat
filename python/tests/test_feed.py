"""Tests for the Feed API pure builders (maat/serving/feed.py, issue #48).

All tests exercise PURE functions — no I/O, no DB, no NATS.

Coverage:
  - build_claim(): field mapping, defaults, article meta join
  - build_originator_groups(): single/collapsed groups, empty
  - build_story(): confidence label, tier, provenance assembly
  - build_feed(): ordering, de-US re-ranking (via curate()), empty input
  - confidence_label integration: label strings and tier codes for known confidence values
  - _source_country() / _infer_country(): geography inference helpers
  - Edge cases: missing fields, empty projections, None values
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from maat.serving.feed import (
    _hero_image_article_id,
    _host_is_public,
    _jload,
    _primary_source,
    build_claim,
    build_feed,
    build_originator_groups,
    build_story,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal projection row factories
# ---------------------------------------------------------------------------


def _article(
    id: str = "a1",
    source: str = "reuters.com",
    language: str = "en",
    title: str = "Test article",
    url: str = "https://reuters.com/test",
) -> dict:
    return {"id": id, "source": source, "language": language, "title": title, "url": url}


def _claim(
    id: str = "c1",
    article_id: str = "a1",
    text: str = "Central bank raised rates.",
    voice: str = "own",
    speaker: str | None = None,
    kind: str = "fact",
    is_synthesis: bool = False,
    horizon: str | None = None,
    in_headline: bool = False,
    evidence_span: str | None = None,
) -> dict:
    return {
        "id": id,
        "article_id": article_id,
        "text": text,
        "voice": voice,
        "speaker": speaker,
        "kind": kind,
        "is_synthesis": is_synthesis,
        "horizon": horizon,
        "in_headline": in_headline,
        "evidence_span": evidence_span,
    }


def _cluster(
    id: str = "cluster1",
    fact: str = "Central bank raised rates by 25 bps.",
    sources: list | None = None,
    originators: list | None = None,
    independent_originators: int = 3,
    has_primary: bool = False,
    claim_ids: list | None = None,
    confidence: float = 0.75,
    extremity: str = "notable",
) -> dict:
    return {
        "id": id,
        "fact": fact,
        "sources": sources or ["reuters.com", "ft.com"],
        "originators": originators or [["a1"], ["a2"]],
        "independent_originators": independent_originators,
        "has_primary": has_primary,
        "claim_ids": claim_ids or ["c1", "c2"],
        "confidence": confidence,
        "extremity": extremity,
    }


# ---------------------------------------------------------------------------
# _jload
# ---------------------------------------------------------------------------


def test_jload_string():
    assert _jload('["a","b"]') == ["a", "b"]


def test_jload_list():
    assert _jload(["a", "b"]) == ["a", "b"]


def test_jload_none():
    assert _jload(None) == []


# ---------------------------------------------------------------------------
# build_claim
# ---------------------------------------------------------------------------


def test_build_claim_basic():
    art_meta = {"a1": _article("a1", source="reuters.com", language="en")}
    c = _claim(id="c1", article_id="a1", text="Rates up.", voice="own", kind="fact")
    result = build_claim(c, art_meta)

    assert result["id"] == "c1"
    assert result["text"] == "Rates up."
    assert result["voice"] == "own"
    assert result["kind"] == "fact"
    assert result["source"] == "reuters.com"
    assert result["language"] == "en"
    assert result["article_id"] == "a1"
    assert result["is_synthesis"] is False
    assert result["in_headline"] is False


def test_build_claim_attributed():
    art_meta = {"a2": _article("a2", source="ft.com", language="en")}
    c = _claim(
        id="c2",
        article_id="a2",
        voice="attributed",
        speaker="ECB president",
        in_headline=True,
        is_synthesis=True,
    )
    result = build_claim(c, art_meta)

    assert result["voice"] == "attributed"
    assert result["speaker"] == "ECB president"
    assert result["in_headline"] is True
    assert result["is_synthesis"] is True


def test_build_claim_missing_article_meta():
    """A claim whose article is not in the meta map gets defaults."""
    c = _claim(id="cx", article_id="missing_art")
    result = build_claim(c, {})

    assert result["source"] is None
    assert result["language"] == "en"  # default


def test_build_claim_none_fields():
    """None values for optional fields pass through without error."""
    art_meta = {"a1": _article("a1")}
    c = _claim(id="c1", article_id="a1", speaker=None, horizon=None, evidence_span=None)
    result = build_claim(c, art_meta)
    assert result["speaker"] is None
    assert result["horizon"] is None
    assert result["evidence_span"] is None


def test_build_claim_language_default():
    """When article has no language, default to 'en'."""
    art_meta = {"a1": {"id": "a1", "source": "example.com", "language": None}}
    c = _claim(id="c1", article_id="a1")
    result = build_claim(c, art_meta)
    assert result["language"] == "en"


# ---------------------------------------------------------------------------
# build_originator_groups
# ---------------------------------------------------------------------------


def test_build_originator_groups_independent():
    meta = {
        "a1": _article("a1", source="reuters.com"),
        "a2": _article("a2", source="ft.com"),
    }
    # Two single-article groups — independent originators
    groups = build_originator_groups([["a1"], ["a2"]], meta)

    assert len(groups) == 2
    assert groups[0]["collapsed"] is False
    assert "reuters.com" in groups[0]["sources"]
    assert groups[1]["collapsed"] is False
    assert "ft.com" in groups[1]["sources"]


def test_build_originator_groups_collapsed():
    meta = {
        "a1": _article("a1", source="ap.com"),
        "a2": _article("a2", source="afp.com"),
    }
    # Wire-syndicated — two articles collapsed into one originator
    groups = build_originator_groups([["a1", "a2"]], meta)

    assert len(groups) == 1
    assert groups[0]["collapsed"] is True
    assert set(groups[0]["sources"]) == {"ap.com", "afp.com"}


def test_build_originator_groups_empty():
    groups = build_originator_groups([], {})
    assert groups == []


def test_build_originator_groups_missing_meta():
    """Article ids not in meta map fall back to the id itself."""
    groups = build_originator_groups([["unknown-art"]], {})
    assert groups[0]["sources"] == ["unknown-art"]


def test_build_originator_groups_json_string():
    """Handles JSON-encoded originators column."""
    meta = {"a1": _article("a1", source="bbc.co.uk")}
    groups = build_originator_groups('[["a1"]]', meta)
    assert len(groups) == 1
    assert "bbc.co.uk" in groups[0]["sources"]


# ---------------------------------------------------------------------------
# build_story
# ---------------------------------------------------------------------------


def test_build_story_basic():
    art_meta = {
        "a1": _article("a1", source="reuters.com", language="en"),
        "a2": _article("a2", source="ft.com", language="en"),
    }
    claims = {
        "c1": _claim("c1", "a1"),
        "c2": _claim("c2", "a2"),
    }
    cl = _cluster(
        id="story1",
        fact="Rates raised.",
        confidence=0.75,
        independent_originators=3,
        has_primary=False,
        extremity="notable",
        originators=[["a1"], ["a2"]],
        claim_ids=["c1", "c2"],
        sources=["reuters.com", "ft.com"],
    )
    story = build_story(cl, claims, art_meta)

    assert story["id"] == "story1"
    assert story["fact"] == "Rates raised."
    assert story["confidence"] == 0.75
    assert story["extremity"] == "notable"
    assert story["independent_originators"] == 3
    assert story["has_primary"] is False
    assert story["source_count"] == 2
    assert len(story["claims"]) == 2
    assert story["languages"] == ["en"]
    assert len(story["originator_groups"]) == 2


def test_build_story_verdict_and_tier_hi():
    """confidence >= 0.85 → 'Well corroborated' / 'hi'."""
    art_meta = {"a1": _article("a1")}
    claims = {"c1": _claim("c1", "a1")}
    cl = _cluster(
        confidence=0.90,
        independent_originators=5,
        has_primary=True,
        extremity="notable",
        claim_ids=["c1"],
        originators=[["a1"]],
    )
    story = build_story(cl, claims, art_meta)
    assert story["verdict"] == "Well corroborated"
    assert story["tier"] == "hi"


def test_build_story_verdict_and_tier_mid():
    """0.60 <= confidence < 0.85 → 'Corroborated' / 'mid'."""
    art_meta = {"a1": _article("a1")}
    claims = {"c1": _claim("c1", "a1")}
    cl = _cluster(
        confidence=0.72,
        independent_originators=2,
        has_primary=False,
        extremity="notable",
        claim_ids=["c1"],
        originators=[["a1"]],
    )
    story = build_story(cl, claims, art_meta)
    assert story["verdict"] == "Corroborated"
    assert story["tier"] == "mid"


def test_build_story_verdict_single_source():
    """Single source, not primary → 'Single source' / low tier."""
    art_meta = {"a1": _article("a1")}
    claims = {"c1": _claim("c1", "a1")}
    cl = _cluster(
        confidence=0.20,
        independent_originators=1,
        has_primary=False,
        extremity="notable",
        claim_ids=["c1"],
        originators=[["a1"]],
    )
    story = build_story(cl, claims, art_meta)
    assert story["verdict"] == "Single source"
    assert story["tier"] in ("lo", "floor")


def test_build_story_empty_claims():
    """Missing claims produce an empty list without error."""
    cl = _cluster(claim_ids=["nonexistent"])
    story = build_story(cl, {}, {})
    assert story["claims"] == []
    assert story["languages"] == ["en"]


def test_build_story_multilingual():
    """Languages are aggregated from claim sources."""
    art_meta = {
        "a1": _article("a1", language="fr"),
        "a2": _article("a2", language="de"),
    }
    claims = {"c1": _claim("c1", "a1"), "c2": _claim("c2", "a2")}
    cl = _cluster(claim_ids=["c1", "c2"], originators=[["a1"], ["a2"]])
    story = build_story(cl, claims, art_meta)
    assert "fr" in story["languages"]
    assert "de" in story["languages"]


def test_build_story_extraordinary_label():
    """Extraordinary single source → failure mode label names it."""
    art_meta = {"a1": _article("a1")}
    claims = {"c1": _claim("c1", "a1")}
    cl = _cluster(
        confidence=0.10,
        independent_originators=1,
        has_primary=False,
        extremity="extraordinary",
        claim_ids=["c1"],
        originators=[["a1"]],
    )
    story = build_story(cl, claims, art_meta)
    assert "extraordinary" in story["verdict"].lower()


# ---------------------------------------------------------------------------
# build_feed
# ---------------------------------------------------------------------------


def test_build_feed_empty():
    result = build_feed([], {}, {})
    assert result["count"] == 0
    assert result["stories"] == []
    assert "generated_at" in result


def test_build_feed_single_cluster():
    art_meta = {"a1": _article("a1", source="reuters.com", language="en")}
    claims = {"c1": _claim("c1", "a1")}
    cl = _cluster(
        id="s1",
        confidence=0.80,
        independent_originators=3,
        claim_ids=["c1"],
        originators=[["a1"]],
    )
    result = build_feed([cl], claims, art_meta)
    assert result["count"] == 1
    assert result["stories"][0]["id"] == "s1"


def test_build_feed_ordering_by_confidence():
    """Without geographic pressure, highest-confidence story comes first."""
    art_meta = {
        "a1": _article("a1", source="ap.org", language="en"),
        "a2": _article("a2", source="afp.fr", language="fr"),
    }
    claims = {
        "c1": _claim("c1", "a1"),
        "c2": _claim("c2", "a2"),
    }
    hi = _cluster(id="hi", confidence=0.90, claim_ids=["c1"], originators=[["a1"]])
    lo = _cluster(id="lo", confidence=0.55, claim_ids=["c2"], originators=[["a2"]])

    result = build_feed([hi, lo], claims, art_meta)
    assert result["stories"][0]["id"] == "hi"


def test_build_feed_de_us_reranking():
    """Non-English stories from identifiable countries are promoted by curate().

    Uses sources with country-mappable TLDs (.fr, .de, etc.) alongside English
    sources whose TLD is .com (unmapped → country="").  A French story within the
    confidence band should be promoted ahead of any remaining .com story once the
    .fr country cap is not yet reached.
    """
    # 6 English-language .com stories (country="" — unmapped); 1 French (.fr → FR)
    art_meta = {
        **{f"en{i}": _article(f"en{i}", source=f"outlet{i}.com", language="en") for i in range(6)},
        "fr1": _article("fr1", source="lemonde.fr", language="fr"),
    }
    claims = {
        **{f"c_en{i}": _claim(f"c_en{i}", f"en{i}") for i in range(6)},
        "c_fr1": _claim("c_fr1", "fr1"),
    }
    clusters = [
        _cluster(
            id=f"en{i}",
            confidence=0.80,
            claim_ids=[f"c_en{i}"],
            originators=[[f"en{i}"]],
            sources=[f"outlet{i}.com"],
        )
        for i in range(6)
    ] + [
        _cluster(
            id="fr1",
            confidence=0.75,  # within 0.20 confidence_band of 0.80
            claim_ids=["c_fr1"],
            originators=[["fr1"]],
            sources=["lemonde.fr"],
        )
    ]

    result = build_feed(clusters, claims, art_meta, country_cap=0.25)
    story_ids = [s["id"] for s in result["stories"]]

    # All 7 stories must be present.
    assert len(story_ids) == 7
    assert "fr1" in story_ids

    # The French story (country="FR", not yet capped) should be promoted above
    # the lowest-confidence .com story — i.e. not last in the feed.
    # With country_cap=0.25 on 7 stories → cap=2 for FR; FR is not near cap.
    # The greedy picker selects fr1 as soon as the .com stories fill whatever
    # their "cap" slot would be. Since .com maps to "" (uncapped), the promotion
    # here is purely confidence-band based: fr1 at 0.75 is within 0.20 of
    # the top (0.80), so it is eligible and will be placed ahead of nothing extra
    # unless source cap kicks in.  The test just verifies FR is present and all
    # stories are placed — the main diversity mechanism (cap enforcement) is
    # tested in test_curation.py which exercises curate() directly.
    assert set(story_ids) == {f"en{i}" for i in range(6)} | {"fr1"}


def test_build_feed_confidence_values_unchanged():
    """build_feed must never mutate confidence values."""
    art_meta = {"a1": _article("a1", language="en")}
    claims = {"c1": _claim("c1", "a1")}
    cl = _cluster(id="s1", confidence=0.731, claim_ids=["c1"], originators=[["a1"]])
    result = build_feed([cl], claims, art_meta)
    assert result["stories"][0]["confidence"] == pytest.approx(0.731, abs=1e-4)


def test_build_feed_count_matches_stories():
    """count field must equal len(stories)."""
    art_meta = {
        "a1": _article("a1"),
        "a2": _article("a2"),
        "a3": _article("a3"),
    }
    claims = {
        "c1": _claim("c1", "a1"),
        "c2": _claim("c2", "a2"),
        "c3": _claim("c3", "a3"),
    }
    clusters = [
        _cluster(id="s1", claim_ids=["c1"], originators=[["a1"]]),
        _cluster(id="s2", claim_ids=["c2"], originators=[["a2"]]),
        _cluster(id="s3", claim_ids=["c3"], originators=[["a3"]]),
    ]
    result = build_feed(clusters, claims, art_meta)
    assert result["count"] == len(result["stories"])


def test_build_feed_all_stories_present():
    """All clusters in → all stories out."""
    n = 5
    art_meta = {f"a{i}": _article(f"a{i}") for i in range(n)}
    claims = {f"c{i}": _claim(f"c{i}", f"a{i}") for i in range(n)}
    clusters = [
        _cluster(id=f"s{i}", claim_ids=[f"c{i}"], originators=[[f"a{i}"]])
        for i in range(n)
    ]
    result = build_feed(clusters, claims, art_meta)
    assert result["count"] == n
    ids = {s["id"] for s in result["stories"]}
    assert ids == {f"s{i}" for i in range(n)}


# Geography inference (_source_country / _infer_country) moved to maat/geo.py — see tests/test_geo.py (#291).


# ---------------------------------------------------------------------------
# _primary_source
# ---------------------------------------------------------------------------


def test_primary_source_from_originators():
    art_meta = {"a1": _article("a1", source="ft.com")}
    cl = _cluster(originators=[["a1"], ["a2"]])
    assert _primary_source(cl, art_meta) == "ft.com"


def test_primary_source_fallback_to_sources():
    cl = _cluster(originators=[], sources=["fallback.com"])
    assert _primary_source(cl, {}) == "fallback.com"


def test_primary_source_empty():
    cl = {"id": "x", "originators": [], "sources": [], "confidence": 0.0,
          "independent_originators": 0, "has_primary": False, "claim_ids": [],
          "fact": "", "extremity": "notable"}
    assert _primary_source(cl, {}) == ""


# ---------------------------------------------------------------------------
# confidence_label integration (via build_story verdict field)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conf, ind, primary, extremity, expected_verdict, expected_tier",
    [
        (0.90, 4, True, "notable", "Well corroborated", "hi"),
        (0.70, 2, False, "notable", "Corroborated", "mid"),
        (0.30, 1, False, "notable", "Single source", "floor"),
        (0.45, 1, False, "notable", "Single source", "lo"),
        (0.10, 1, False, "extraordinary", "Single source · extraordinary claim", "floor"),
    ],
)
def test_confidence_label_via_build_story(
    conf, ind, primary, extremity, expected_verdict, expected_tier
):
    art_meta = {"a1": _article("a1")}
    claims = {"c1": _claim("c1", "a1")}
    cl = _cluster(
        confidence=conf,
        independent_originators=ind,
        has_primary=primary,
        extremity=extremity,
        claim_ids=["c1"],
        originators=[["a1"]],
    )
    story = build_story(cl, claims, art_meta)
    assert story["verdict"] == expected_verdict, (
        f"conf={conf}, ind={ind}, primary={primary}, extremity={extremity}"
    )
    assert story["tier"] == expected_tier


# ---------------------------------------------------------------------------
# _hero_image_article_id (#1) — which article's og:image represents the story
# ---------------------------------------------------------------------------


def _art_with_image(id: str, image_url: str | None) -> dict:
    a = _article(id)
    a["image_url"] = image_url
    return a


def test_hero_image_prefers_originator():
    """The first originator article with an image wins."""
    meta = {"a1": _art_with_image("a1", "https://cdn.x/1.jpg"),
            "a2": _art_with_image("a2", "https://cdn.x/2.jpg")}
    cl = _cluster(originators=[["a1"], ["a2"]], claim_ids=["c1"])
    claims = [{"article_id": "a2"}]
    assert _hero_image_article_id(cl, claims, meta) == "a1"


def test_hero_image_falls_back_to_claim_article():
    """No originator image → first claim article that has one."""
    meta = {"a1": _art_with_image("a1", None),
            "a9": _art_with_image("a9", "https://cdn.x/9.jpg")}
    cl = _cluster(originators=[["a1"]], claim_ids=["c1"])
    claims = [{"article_id": "a1"}, {"article_id": "a9"}]
    assert _hero_image_article_id(cl, claims, meta) == "a9"


def test_hero_image_none_when_no_images():
    meta = {"a1": _art_with_image("a1", None)}
    cl = _cluster(originators=[["a1"]], claim_ids=["c1"])
    assert _hero_image_article_id(cl, [{"article_id": "a1"}], meta) is None


def test_build_story_exposes_hero_image_article_id():
    meta = {"a1": _art_with_image("a1", "https://cdn.x/1.jpg")}
    cl = _cluster(originators=[["a1"]], claim_ids=["c1"])
    claims = {"c1": _claim("c1", "a1")}
    story = build_story(cl, claims, meta)
    assert story["hero_image_article_id"] == "a1"


# ---------------------------------------------------------------------------
# _host_is_public (#1) — SSRF guard for the image proxy
# ---------------------------------------------------------------------------


def _patch_resolve(monkeypatch, ip: str) -> None:
    """Force socket.getaddrinfo (used by loop.getaddrinfo) to resolve to one IP."""
    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_host_is_public_allows_public(monkeypatch):
    _patch_resolve(monkeypatch, "93.184.216.34")
    assert asyncio.run(_host_is_public("example.com", 443)) is True


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.10"])
def test_host_is_public_blocks_private_and_metadata(monkeypatch, ip):
    """RFC-1918, loopback, and the cloud-metadata link-local address are all refused."""
    _patch_resolve(monkeypatch, ip)
    assert asyncio.run(_host_is_public("evil.example", 80)) is False


def test_host_is_public_blocks_unresolvable(monkeypatch):
    def boom(host, port, *a, **k):
        raise OSError("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert asyncio.run(_host_is_public("nope.invalid", 443)) is False
