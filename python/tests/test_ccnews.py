"""Tests for the CC-News (Common Crawl) source (#237) — pure helpers + offline WARC parsing.

No network: we build a tiny WARC in memory with warcio and parse it back, so iter_warc is
exercised end-to-end (record filtering + trafilatura extraction) deterministically.
"""

from __future__ import annotations

import io

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from maat.acquire import ccnews


def test_country_of_from_tld():
    assert ccnews.country_of("lemonde.fr") == "FR"
    assert ccnews.country_of("bbc.co.uk") == "GB"        # two-label TLD beats one-label
    assert ccnews.country_of("finance.sina.com.cn") == "CN"
    assert ccnews.country_of("nytimes.com") == ""        # .com -> unknown country
    assert ccnews.country_of("") == ""


def test_normalise_lang_cld2_to_two_letter():
    assert ccnews.normalise_lang("fra") == "fr"
    assert ccnews.normalise_lang("eng,deu") == "en"      # first (highest-confidence) wins
    assert ccnews.normalise_lang("zho") == "zh"
    assert ccnews.normalise_lang("xyz") == "xy"          # unknown -> first two chars
    assert ccnews.normalise_lang("") == ""


_ARTICLE_HTML = (
    b"<html><head><title>Ministry confirms the policy</title>"
    b'<meta property="og:image" content="https://lemonde.fr/lead.jpg"></head>'
    b"<body><article><h1>Ministry confirms the policy</h1><p>"
    b"The ministry confirmed on Tuesday that the new policy will take effect next month, "
    b"following months of negotiation between the parties involved and a final review by the "
    b"oversight committee, officials said in a detailed statement to reporters.</p></article></body></html>"
)


def _warc_bytes(records: list[tuple[str, bytes, str, str]]) -> io.BytesIO:
    """records = [(url, html_bytes, lang_header, content_type)] -> an in-memory WARC stream."""
    buf = io.BytesIO()
    writer = WARCWriter(buf, gzip=False)
    for url, html, lang, ctype in records:
        http_headers = StatusAndHeaders("200 OK", [("Content-Type", ctype)], protocol="HTTP/1.0")
        warc_headers = {"WARC-Identified-Content-Language": lang} if lang else None
        rec = writer.create_warc_record(
            url, "response", payload=io.BytesIO(html),
            http_headers=http_headers, warc_headers_dict=warc_headers or {},
        )
        writer.write_record(rec)
    buf.seek(0)
    return buf


def test_iter_warc_parses_html_response():
    stream = _warc_bytes([("https://www.lemonde.fr/article-1", _ARTICLE_HTML, "fra", "text/html; charset=utf-8")])
    arts = list(ccnews.iter_warc(stream, min_chars=50))
    assert len(arts) == 1
    a = arts[0]
    assert a.url == "https://www.lemonde.fr/article-1"
    assert a.source == "lemonde.fr"            # www. stripped
    assert a.language == "fr"                   # from the CLD2 header
    assert a.country == "FR"                    # from the .fr TLD
    assert "new policy will take effect" in a.body
    assert a.title == "Ministry confirms the policy"


def test_iter_warc_skips_non_html_thin_and_non_response():
    records = [
        ("https://x.test/non-html", b"\x00\x01binary", "eng", "image/jpeg"),       # not HTML
        ("https://x.test/thin", b"<html><body><p>too short</p></body></html>", "eng", "text/html"),  # thin
        ("https://lemonde.fr/ok", _ARTICLE_HTML, "fra", "text/html"),              # the only keeper
    ]
    arts = list(ccnews.iter_warc(_warc_bytes(records), min_chars=50))
    assert [a.source for a in arts] == ["lemonde.fr"]


def test_detect_lang_offline():
    assert ccnews.detect_lang("The ministry confirmed the new policy will take effect next month.") == "en"
    assert ccnews.detect_lang("") == ""               # nothing to judge
    assert ccnews.detect_lang("hi") == ""             # too short


def test_iter_warc_detects_language_when_header_absent():
    # CC-NEWS records frequently lack WARC-Identified-Content-Language (verified live) → the body
    # is language-detected so the de-slant still has a real stratum instead of "unknown".
    stream = _warc_bytes([("https://lemonde.fr/x", _ARTICLE_HTML, "", "text/html")])  # no lang header
    arts = list(ccnews.iter_warc(stream, min_chars=50))
    assert arts and arts[0].language == "en"


def test_iter_warc_respects_limit():
    records = [
        (f"https://lemonde.fr/a{i}", _ARTICLE_HTML, "fra", "text/html") for i in range(3)
    ]
    assert len(list(ccnews.iter_warc(_warc_bytes(records), limit=2, min_chars=50))) == 2
