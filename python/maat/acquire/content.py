"""URL → clean article content: the extraction ladder (P14 follow-up to the Le Pen debugging).

One module owns the whole journey from URL to boilerplate-free article text, as a LADDER —
cheap and deterministic first, expensive and hostile-capable last, every rung telemetried:

  1. FETCH  — curl_cffi with a real-browser TLS/HTTP2 fingerprint (a plain httpx/urllib GET is
     flagged at the TLS handshake by modern bot walls before headers are even read).
  2. JSON-LD — publishers' own schema.org markup is the highest-precision source when present:
     ``NewsArticle.articleBody`` is the canonical text with zero boilerplate, and
     ``LiveBlogPosting.liveBlogUpdate`` turns a 30k-char live-page scroll (where every generic
     extractor collapses — Bevendorff et al. 2023) into clean, timestamped update segments.
  3. GENERIC — trafilatura (best mean F1/precision across the published benchmarks) with
     readability-lxml as the robustness fallback (best median), per the SIGIR 2023 reproduction
     study: no single extractor wins everywhere, an agreement pair beats either alone.
  4. APIFY  — the rag-web-browser actor re-fetches pages that block rung 1 (it got PBS bodies
     the naive fetcher could not) — flaky on the hardest walls, so it is a retry, not the answer.
  5. ZYTE   — managed unblocker terminal rung (browser rendering + residential egress + their
     ML article extraction as a cross-check). INERT until ``MAAT_ZYTE_API_KEY`` is set — wired,
     telemetried, and activates without a deploy, same pattern as pipeline/nli.py.

Rungs 2–3 are pure functions over HTML (offline-testable); rungs 1/4/5 are the network seams.
``fetch_page`` keeps the exact signature/semantics the pipeline already depends on — callers in
pipeline/analyse.py, serving/analyse.py, acquire/ingest.py and acquire/history.py are unchanged.

Evidence base: docs/extraction-layer-research.md (adversarially verified benchmark findings).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from urllib.parse import urljoin, urlparse

import trafilatura
from trafilatura.metadata import extract_metadata

log = logging.getLogger("maat.acquire.content")
# readability-lxml narrates its retries ("ruthless removal did not work") at INFO — ops noise.
logging.getLogger("readability").setLevel(logging.WARNING)

_MAX_HTML_BYTES = 5_000_000          # bound memory on pathological pages
_FETCH_TIMEOUT = 20.0
_ZYTE_TIMEOUT = 60.0
_ZYTE_URL = "https://api.zyte.com/v1/extract"

# schema.org types whose articleBody is the publisher's own canonical article text. @type is
# matched case-sensitively per the spec; both bare strings and lists are accepted.
_ARTICLE_TYPES = frozenset({
    "NewsArticle", "ReportageNewsArticle", "AnalysisNewsArticle", "BackgroundNewsArticle",
    "OpinionNewsArticle", "ReviewNewsArticle", "Article", "BlogPosting",
})
_LIVEBLOG_TYPE = "LiveBlogPosting"


@dataclass(frozen=True)
class FetchedPage:
    """One fetched article page — main text plus best-effort display metadata."""

    body: str
    title: str | None = None
    image: str | None = None  # lead image (og:image) — display-only, never a veracity signal
    date: str | None = None   # publication date (ISO) when the page states one
    canonical: str | None = None  # the page's declared canonical URL (<link rel=canonical>/og:url)


# ── rung 1: impersonated fetch ───────────────────────────────────────────────────────────────────


def _fetch_html(url: str) -> str | None:
    """Raw page HTML via a Chrome-impersonated client, or None (non-200, non-HTML, no body).

    curl_cffi mimics a real browser's TLS + HTTP/2 fingerprint — the cheapest single upgrade
    against bot walls (the detection layer that most commonly defeats plain Python clients).
    Falls back to trafilatura's fetcher if curl_cffi is unavailable in this environment.
    """
    try:
        from curl_cffi import requests as curl
    except ImportError:  # pragma: no cover - dependency ships in pyproject; belt-and-braces
        log.warning("curl_cffi unavailable — falling back to trafilatura fetch for %s", url)
        return trafilatura.fetch_url(url) or None
    try:
        r = curl.get(
            url,
            impersonate="chrome",
            timeout=_FETCH_TIMEOUT,
            allow_redirects=True,
            headers={"Accept-Language": "en;q=0.9, *;q=0.5"},
        )
    except Exception as e:  # noqa: BLE001 - a dead host must not sink the ladder
        log.info("fetch rung=curl url=%s failed: %s", url, type(e).__name__)
        return None
    if r.status_code != 200:
        log.info("fetch rung=curl url=%s status=%s", url, r.status_code)
        return None
    ctype = (r.headers.get("content-type") or "").lower()
    if ctype and "html" not in ctype and "xml" not in ctype:
        return None
    text = r.text or ""
    return text[:_MAX_HTML_BYTES] or None


# ── rung 2: JSON-LD (pure) ───────────────────────────────────────────────────────────────────────


def _jsonld_blocks(html: str) -> list[dict]:
    """Every JSON-LD object on the page, flattened: pages embed MULTIPLE blocks, wrap them in
    @graph, and mistype them — so collect all dicts and let the caller filter (GDELT's lesson)."""
    try:
        from lxml import html as lhtml
        tree = lhtml.fromstring(html.encode("utf-8", errors="ignore"))
        scripts = tree.xpath('//script[@type="application/ld+json"]/text()')
    except Exception:  # noqa: BLE001 - malformed HTML never sinks the ladder
        return []
    out: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            out.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for raw in scripts:
        try:
            walk(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue  # one broken block never hides the others
    return out


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()


def _str(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _image_url(v: object) -> str | None:
    """schema.org image is a string, an ImageObject, or a list of either."""
    if isinstance(v, list) and v:
        return _image_url(v[0])
    if isinstance(v, dict):
        return _str(v.get("url")) or _str(v.get("contentUrl"))
    return _str(v)


def _canonical_of(node: dict) -> str | None:
    main = node.get("mainEntityOfPage")
    if isinstance(main, dict):
        main = main.get("@id") or main.get("url")
    return _str(main) or _str(node.get("url"))


def _liveblog_body(node: dict) -> str:
    """A LiveBlogPosting as clean text: one titled, dated segment per update, page order kept —
    the structure every generic extractor loses on live pages."""
    updates = node.get("liveBlogUpdate")
    if isinstance(updates, dict):
        updates = [updates]
    if not isinstance(updates, list):
        return ""
    parts: list[str] = []
    for u in updates:
        if not isinstance(u, dict):
            continue
        headline, when = _str(u.get("headline")), _str(u.get("datePublished"))
        body = _str(u.get("articleBody")) or _str(u.get("text"))
        if not (headline or body):
            continue
        head = " — ".join(x for x in (headline, when) if x)
        parts.append("\n".join(x for x in (head, body) if x))
    return "\n\n".join(parts)


def page_from_jsonld(html: str, url: str, *, min_chars: int = 200) -> FetchedPage | None:
    """The publisher's own structured article, when the page carries one rich enough to use.

    Returns None when no article-typed block has a usable body — richness varies wildly across
    publishers (many emit only headline + URL), so JSON-LD is a rung, never the whole answer.
    """
    best: FetchedPage | None = None
    for node in _jsonld_blocks(html):
        types = _types(node)
        if _LIVEBLOG_TYPE in types:
            body = _liveblog_body(node)
            kind_liveblog = True
        elif types & _ARTICLE_TYPES:
            body = _str(node.get("articleBody")) or ""
            kind_liveblog = False
        else:
            continue
        if len(body) < min_chars:
            continue
        page = FetchedPage(
            body=body,
            title=_str(node.get("headline")) or _str(node.get("name")),
            image=_image_url(node.get("image")),
            date=_str(node.get("datePublished")) or _str(node.get("dateModified")),
            canonical=_resolve(url, _canonical_of(node)),
        )
        # A live blog's segmentation beats anything else on the page; otherwise keep the longest.
        if kind_liveblog:
            return page
        if best is None or len(page.body) > len(best.body):
            best = page
    return best


def _resolve(base: str, maybe_relative: str | None) -> str | None:
    if not maybe_relative:
        return None
    try:
        resolved = urljoin(base, maybe_relative)
        return resolved if urlparse(resolved).scheme in ("http", "https") else None
    except ValueError:
        return None


# ── rung 3: generic extraction (pure) ────────────────────────────────────────────────────────────


def _readability_text(html: str) -> str:
    """readability-lxml main content as plain text — the robustness fallback (best median F1,
    lowest spread in the SIGIR 2023 reproduction study)."""
    try:
        from lxml import html as lhtml
        from readability import Document

        summary = Document(html).summary(html_partial=True)
        text = lhtml.fromstring(summary).text_content()
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())
    except Exception:  # noqa: BLE001 - fallback of a fallback; None-equivalent on any failure
        return ""


def page_from_html(html: str, url: str, *, min_chars: int = 200) -> FetchedPage | None:
    """Generic main-content extraction: trafilatura first (best mean precision — least
    boilerplate for the LLM downstream), readability when trafilatura comes up thin or
    visibly under-recalls (its known failure mode). Metadata via trafilatura either way."""
    text = trafilatura.extract(
        html, include_comments=False, include_tables=False, favor_precision=True
    ) or ""
    alt = _readability_text(html)
    # Under-recall guard: readability finding substantially more real text than trafilatura
    # signals trafilatura dropped the body (genre-dependent failure), not that the page is thin.
    if len(alt) >= max(min_chars, 2 * len(text)):
        text = alt
    if len(text) < min_chars:
        return None
    title = image = date = canonical = None
    try:
        md = extract_metadata(html)
        if md:
            title = getattr(md, "title", None) or None
            image = getattr(md, "image", None) or None
            date = getattr(md, "date", None) or None
            # trafilatura fills `url` from <link rel="canonical"> / og:url when the page states one.
            canonical = getattr(md, "url", None) or None
    except Exception:  # noqa: BLE001 - metadata is best-effort enrichment, never fatal
        pass
    return FetchedPage(body=text, title=title, image=image, date=date, canonical=canonical)


def _merge(primary: FetchedPage, backfill: FetchedPage | None) -> FetchedPage:
    """Primary page with metadata gaps filled from the other extraction's read of the same HTML
    (JSON-LD often omits og:image; trafilatura often has it)."""
    if backfill is None:
        return primary
    return replace(
        primary,
        title=primary.title or backfill.title,
        image=primary.image or backfill.image,
        date=primary.date or backfill.date,
        canonical=primary.canonical or backfill.canonical,
    )


def page_from_downloaded(html: str, url: str, *, min_chars: int = 200) -> FetchedPage | None:
    """Rungs 2+3 over HTML already in hand: JSON-LD body when rich enough, else generic —
    each backfilling the other's metadata."""
    structured = page_from_jsonld(html, url, min_chars=min_chars)
    generic = page_from_html(html, url, min_chars=min_chars)
    if structured is not None:
        # Truncation guard: some publishers put only a teaser in articleBody. Prefer the
        # generic read when it recovered substantially more of the article.
        if generic is not None and len(generic.body) > 2 * len(structured.body):
            return _merge(generic, structured)
        return _merge(structured, generic)
    return generic


# ── rung 4: apify re-fetch ───────────────────────────────────────────────────────────────────────


def _fetch_via_apify(url: str, *, min_chars: int) -> FetchedPage | None:
    """Second fetch attempt through the rag-web-browser actor (its fetching beat the naive
    client on PBS in testing). Returns extracted text directly — the actor strips boilerplate."""
    from maat.acquire import apify

    if not apify.available():
        return None
    try:
        arts = apify.search_and_fetch(url, max_results=1, timeout=90.0)
    except Exception as e:  # noqa: BLE001
        log.info("fetch rung=apify url=%s failed: %s", url, type(e).__name__)
        return None
    for a in arts:
        if len(a.body) >= min_chars:
            return FetchedPage(body=a.body, title=a.title or None, image=a.image)
    return None


# ── rung 5: zyte terminal rung (inert without a key) ─────────────────────────────────────────────


def _zyte_key() -> str | None:
    return os.environ.get("MAAT_ZYTE_API_KEY") or os.environ.get("ZYTE_API_KEY") or None


def zyte_page(data: dict, url: str, *, min_chars: int = 200) -> FetchedPage | None:
    """A FetchedPage from a Zyte /v1/extract response: their ML article extraction when it
    carries the body, else our own rungs 2+3 over the browser-rendered HTML. Pure — testable
    from a fixture dict."""
    art = data.get("article") or {}
    body = art.get("articleBody") or ""
    if isinstance(body, str) and len(body) >= min_chars:
        return FetchedPage(
            body=body,
            title=_str(art.get("headline")),
            image=_image_url(art.get("mainImage")),
            date=_str(art.get("datePublished")) or _str(art.get("datePublishedRaw")),
            canonical=_str(art.get("canonicalUrl")) or _str(art.get("url")),
        )
    html = data.get("browserHtml") or ""
    if isinstance(html, str) and html:
        return page_from_downloaded(html[:_MAX_HTML_BYTES], url, min_chars=min_chars)
    return None


def _fetch_via_zyte(url: str, *, min_chars: int) -> FetchedPage | None:
    key = _zyte_key()
    if not key:
        return None
    import httpx

    try:
        r = httpx.post(
            _ZYTE_URL,
            auth=(key, ""),
            json={"url": url, "browserHtml": True, "article": True},
            timeout=_ZYTE_TIMEOUT,
        )
        r.raise_for_status()
        return zyte_page(r.json(), url, min_chars=min_chars)
    except Exception as e:  # noqa: BLE001
        log.warning("fetch rung=zyte url=%s failed: %s", url, type(e).__name__)
        return None


# ── the ladder ───────────────────────────────────────────────────────────────────────────────────


def fetch_page(url: str, *, min_chars: int = 200) -> FetchedPage | None:
    """Download `url` → FetchedPage, or None if every rung fails or the text is too thin.

    Cheap-deterministic first, hostile-capable last; the serving rung is logged per URL so
    fetch failures are visible in ops instead of silently starving corroboration (the Le Pen
    failure mode)."""
    t0 = time.monotonic()

    def served(rung: str, page: FetchedPage) -> FetchedPage:
        log.info(
            "fetch url=%s rung=%s body=%d secs=%.1f",
            url, rung, len(page.body), time.monotonic() - t0,
        )
        return page

    html = _fetch_html(url)
    if html:
        page = page_from_downloaded(html, url, min_chars=min_chars)
        if page is not None:
            return served("direct", page)
        log.info("fetch url=%s rung=direct fetched html=%d but no article body", url, len(html))

    page = _fetch_via_apify(url, min_chars=min_chars)
    if page is not None:
        return served("apify", page)

    page = _fetch_via_zyte(url, min_chars=min_chars)
    if page is not None:
        return served("zyte", page)

    log.info("fetch url=%s FAILED all rungs (zyte %s) secs=%.1f",
             url, "on" if _zyte_key() else "off", time.monotonic() - t0)
    return None


def fetch_article(url: str, *, min_chars: int = 200) -> tuple[str | None, str | None]:
    """Download `url`; return ``(body, image_url)``.

    body is the boilerplate-stripped main text, or None if the download fails or is too thin.
    image_url is the article's lead image (og:image / twitter:image) when present, else None.
    """
    page = fetch_page(url, min_chars=min_chars)
    return (page.body, page.image) if page else (None, None)


def fetch_body(url: str, *, min_chars: int = 200) -> str | None:
    """Back-compat: the main-text body only (callers that don't need the image)."""
    return fetch_article(url, min_chars=min_chars)[0]
