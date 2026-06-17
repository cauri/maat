"""Tests for the RSS source (#238) — feed parsing + the balanced feed-list loader (no network)."""

from __future__ import annotations

from maat.acquire import rss
from maat.acquire.rss import Feed

_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Le Monde</title><language>fr</language>
  <item><title>Le titre un</title><link>https://www.lemonde.fr/a/1</link></item>
  <item><title>Deux</title><link>https://lemonde.fr/a/2</link></item>
  <item><title>No link</title></item>
</channel></rss>"""


def test_parse_feed_extracts_items_and_tags():
    feed = Feed("https://www.lemonde.fr/rss/une.xml", "FR", "fr", "independent", "Le Monde")
    items = rss.parse_feed(_RSS, feed)
    assert len(items) == 2                         # the link-less item is skipped
    a = items[0]
    assert a.url == "https://www.lemonde.fr/a/1"
    assert a.source == "lemonde.fr"                # www. stripped
    assert a.title == "Le titre un"
    assert a.language == "fr"                      # from the feed's <language>
    assert a.country == "FR" and a.alignment == "independent"  # carried from the Feed tag


def test_parse_feed_respects_limit():
    feed = Feed("https://x/rss", "FR", "fr", "independent", "X")
    assert len(rss.parse_feed(_RSS, feed, limit=1)) == 1


def test_default_feeds_are_balanced_and_flagged():
    feeds = rss.load_feeds(None)
    assert len(feeds) >= 20
    langs = {f.language for f in feeds}
    countries = {f.country for f in feeds}
    aligns = {f.alignment for f in feeds}
    # genuinely multilingual + multipolar, not Anglophone-only
    assert {"en", "fr", "es", "de", "ru", "ja", "pt"} <= langs
    assert len(countries) >= 12
    # English is present but must not be the overwhelming majority
    assert sum(1 for f in feeds if f.language == "en") < len(feeds) * 0.7
    # state-aligned outlets exist and are explicitly flagged (so they're never independent corroboration)
    assert "state" in aligns and "independent" in aligns
    assert any(f.alignment == "state" for f in feeds) and any(f.alignment == "independent" for f in feeds)


def test_load_feeds_reads_operator_file(tmp_path):
    cfg = tmp_path / "feeds.txt"
    cfg.write_text(
        "# my feeds\n"
        "https://a.example/rss | US | en | independent | A News\n"
        "\n"
        "https://b.example/rss | DE | de | public | B Funk\n"
    )
    feeds = rss.load_feeds(cfg)
    assert [f.label for f in feeds] == ["A News", "B Funk"]
    assert feeds[1].country == "DE" and feeds[1].alignment == "public"
