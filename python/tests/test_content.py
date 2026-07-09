"""Extraction ladder (acquire/content.py): rungs 2-3 are pure over HTML — tested offline with
fixture pages; the ladder's rung ordering and the inert Zyte rung are tested via monkeypatched
seams. No test touches the network."""

import json

import pytest

from maat.acquire import content
from maat.acquire.content import (
    FetchedPage,
    fetch_page,
    page_from_downloaded,
    page_from_html,
    page_from_jsonld,
    zyte_page,
)

PARA = (
    "The appeals court in Paris handed down a reduced sentence on Tuesday, upholding the "
    "conviction while shortening the ban from public office to fifteen months. "
)
BODY = (PARA * 8).strip()  # comfortably past min_chars for every rung


def html_page(*, jsonld: object = None, article_text: str = "", title: str = "Fixture Page") -> str:
    """A minimal but realistic page: optional JSON-LD block + optional visible article."""
    script = (
        f'<script type="application/ld+json">{json.dumps(jsonld)}</script>' if jsonld else ""
    )
    paragraphs = "".join(f"<p>{p.strip()}.</p>" for p in article_text.split(".") if p.strip())
    return f"""<!DOCTYPE html>
<html><head>
<title>{title}</title>
<link rel="canonical" href="https://news.example/story"/>
<meta property="og:image" content="https://news.example/og.jpg"/>
{script}
</head><body>
<nav><a href="/">Home</a><a href="/politics">Politics</a><a href="/sport">Sport</a></nav>
<main><article><h1>{title}</h1>{paragraphs}</article></main>
<footer>Contact us. Terms of use. Subscribe to our newsletter for daily updates.</footer>
</body></html>"""


# ── rung 2: JSON-LD ──────────────────────────────────────────────────────────────────────────────


def test_jsonld_news_article_body_and_metadata():
    node = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "Le Pen sentence reduced on appeal",
        "articleBody": BODY,
        "datePublished": "2026-07-07T13:05:00Z",
        "mainEntityOfPage": {"@id": "https://news.example/canonical-story"},
        "image": {"@type": "ImageObject", "url": "https://news.example/lead.jpg"},
    }
    page = page_from_jsonld(html_page(jsonld=node), "https://news.example/story")
    assert page is not None
    assert page.body == BODY
    assert page.title == "Le Pen sentence reduced on appeal"
    assert page.date == "2026-07-07T13:05:00Z"
    assert page.canonical == "https://news.example/canonical-story"
    assert page.image == "https://news.example/lead.jpg"


def test_jsonld_graph_wrapper_and_list_type():
    node = {"@graph": [
        {"@type": "Organization", "name": "News Example"},
        {"@type": ["Article", "NewsArticle"], "articleBody": BODY, "headline": "Graphed"},
    ]}
    page = page_from_jsonld(html_page(jsonld=node), "https://news.example/story")
    assert page is not None and page.title == "Graphed" and page.body == BODY


def test_jsonld_liveblog_segments_updates_in_order():
    node = {
        "@type": "LiveBlogPosting",
        "headline": "Le Pen verdict — live",
        "datePublished": "2026-07-07T08:00:00Z",
        "liveBlogUpdate": [
            {"@type": "BlogPosting", "headline": "Court convenes",
             "datePublished": "2026-07-07T08:01:00Z", "articleBody": PARA},
            {"@type": "BlogPosting", "headline": "Sentence read out",
             "datePublished": "2026-07-07T09:30:00Z", "articleBody": PARA * 2},
        ],
    }
    page = page_from_jsonld(html_page(jsonld=node), "https://news.example/live")
    assert page is not None
    assert page.title == "Le Pen verdict — live"
    first = page.body.index("Court convenes")
    second = page.body.index("Sentence read out")
    assert first < second  # page order preserved
    assert "2026-07-07T09:30:00Z" in page.body  # updates keep their timestamps


def test_jsonld_broken_block_does_not_hide_valid_one():
    html = html_page(jsonld={"@type": "NewsArticle", "articleBody": BODY}).replace(
        "<head>",
        '<head><script type="application/ld+json">{not json…</script>',
    )
    page = page_from_jsonld(html, "https://news.example/story")
    assert page is not None and page.body == BODY


def test_jsonld_thin_or_absent_body_returns_none():
    node = {"@type": "NewsArticle", "headline": "Teaser only", "articleBody": "Too short."}
    assert page_from_jsonld(html_page(jsonld=node), "https://x.example/s") is None
    assert page_from_jsonld(html_page(), "https://x.example/s") is None


# ── rung 3: generic ──────────────────────────────────────────────────────────────────────────────


def test_generic_extracts_article_and_metadata():
    page = page_from_html(html_page(article_text=BODY), "https://news.example/story")
    assert page is not None
    assert "appeals court in Paris" in page.body
    assert "Subscribe to our newsletter" not in page.body  # boilerplate stripped
    assert page.canonical == "https://news.example/story"


def test_generic_falls_back_to_readability_when_trafilatura_thin(monkeypatch):
    monkeypatch.setattr(content.trafilatura, "extract", lambda *a, **k: None)
    page = page_from_html(html_page(article_text=BODY), "https://news.example/story")
    assert page is not None
    assert "appeals court in Paris" in page.body


# ── rungs 2+3 composed ───────────────────────────────────────────────────────────────────────────


def test_downloaded_prefers_structured_and_backfills_metadata():
    node = {"@type": "NewsArticle", "headline": "Structured wins", "articleBody": BODY}
    page = page_from_downloaded(
        html_page(jsonld=node, article_text=BODY), "https://news.example/story"
    )
    assert page is not None
    assert page.title == "Structured wins"
    assert page.body == BODY
    assert page.image == "https://news.example/og.jpg"  # backfilled from the generic read
    assert page.canonical == "https://news.example/story"


def test_downloaded_teaser_jsonld_loses_to_fuller_generic_body():
    teaser = PARA  # rich enough to pass min_chars, but a fraction of the real article
    node = {"@type": "NewsArticle", "headline": "Teaser", "articleBody": teaser}
    page = page_from_downloaded(
        html_page(jsonld=node, article_text=BODY * 2), "https://news.example/story"
    )
    assert page is not None
    assert len(page.body) > 2 * len(teaser)  # the generic read won
    assert page.title  # metadata still merged


# ── the ladder ───────────────────────────────────────────────────────────────────────────────────


def test_ladder_serves_direct_rung(monkeypatch):
    monkeypatch.setattr(content, "_fetch_html", lambda url: html_page(article_text=BODY))
    monkeypatch.setattr(
        content, "_fetch_via_apify",
        lambda url, min_chars: pytest.fail("apify rung must not run when direct serves"),
    )
    page = fetch_page("https://news.example/story")
    assert page is not None and "appeals court" in page.body


def test_ladder_falls_through_to_apify(monkeypatch):
    monkeypatch.setattr(content, "_fetch_html", lambda url: None)
    monkeypatch.setattr(
        content, "_fetch_via_apify",
        lambda url, min_chars: FetchedPage(body=BODY, title="via apify"),
    )
    page = fetch_page("https://walled.example/story")
    assert page is not None and page.title == "via apify"


def test_ladder_zyte_inert_without_key_and_last(monkeypatch):
    monkeypatch.delenv("MAAT_ZYTE_API_KEY", raising=False)
    monkeypatch.delenv("ZYTE_API_KEY", raising=False)
    monkeypatch.setattr(content, "_fetch_html", lambda url: None)
    monkeypatch.setattr(content, "_fetch_via_apify", lambda url, min_chars: None)
    calls = []
    monkeypatch.setattr(
        content, "httpx", None, raising=False
    )  # any HTTP attempt without a key would explode — proving inertness
    monkeypatch.setattr(content, "_zyte_key", lambda: calls.append("checked") or None)
    assert fetch_page("https://walled.example/story") is None
    assert calls  # the rung was consulted, keyless, and stayed inert


def test_zyte_page_prefers_their_article_extraction():
    data = {
        "article": {
            "headline": "Zyte extracted",
            "articleBody": BODY,
            "datePublished": "2026-07-07T10:00:00Z",
            "mainImage": {"url": "https://cdn.example/img.jpg"},
            "canonicalUrl": "https://news.example/canonical",
        },
        "browserHtml": "<html><body>ignored</body></html>",
    }
    page = zyte_page(data, "https://news.example/story")
    assert page is not None
    assert page.body == BODY and page.title == "Zyte extracted"
    assert page.canonical == "https://news.example/canonical"


def test_zyte_page_falls_back_to_browser_html():
    data = {"article": {}, "browserHtml": html_page(article_text=BODY)}
    page = zyte_page(data, "https://news.example/story")
    assert page is not None and "appeals court" in page.body


# ── the public seam ──────────────────────────────────────────────────────────────────────────────


def test_fetch_module_reexports_the_ladder():
    from maat.acquire import fetch

    assert fetch.fetch_page is content.fetch_page
    assert fetch.FetchedPage is content.FetchedPage
    assert fetch.fetch_article is content.fetch_article
    assert fetch.fetch_body is content.fetch_body
