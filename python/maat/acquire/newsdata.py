"""NewsData.io acquisition — a paid, structured news API for deliberate multipolar breadth.

NewsData.io covers 80+ languages with per-country / per-language filtering and returns the article
text directly (no separate fetch), so it's a reliable dedicated channel where the free GDELT stream
429s. Activated ONLY when ``MAAT_NEWSDATA_KEY`` is set (dormant otherwise, exactly like the Apify
fallback) — the operator drops the key into the box ``.env``; this module never sees it otherwise.

REST: GET https://newsdata.io/api/1/latest?apikey=…&q=…&language=…&country=…[&page=token]
Response: {status, totalResults, results: [{title, link, description, content, source_id,
language, country: [..], image_url, pubDate, …}], nextPage}. Free tier truncates ``content``;
a paid key returns the full body (so corroboration has real text to cluster on).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

_LATEST_URL = "https://newsdata.io/api/1/latest"


@dataclass(frozen=True)
class NewsDataArticle:
    url: str
    title: str
    domain: str
    language: str
    country: str
    body: str
    image_url: str


def available() -> bool:
    return bool(os.environ.get("MAAT_NEWSDATA_KEY"))


def _domain(url: str, source_id: str | None) -> str:
    try:
        d = urlparse(url).netloc.removeprefix("www.")
    except ValueError:
        d = ""
    return d or (source_id or "")


def _first(value) -> str:
    """NewsData returns country/language sometimes as a list, sometimes a scalar — normalise."""
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def parse_results(results: list | None, *, min_chars: int = 200) -> list[NewsDataArticle]:
    """Pure: NewsData ``results`` → article rows, dropping anything without a URL or real body.
    Prefers the full ``content`` (paid tier) and falls back to ``description``."""
    out: list[NewsDataArticle] = []
    for r in results or []:
        url = (r.get("link") or "").strip()
        body = (r.get("content") or r.get("description") or "").strip()
        if not url or len(body) < min_chars:
            continue
        out.append(
            NewsDataArticle(
                url=url,
                title=(r.get("title") or "").strip(),
                domain=_domain(url, r.get("source_id")),
                language=_first(r.get("language")),
                country=_first(r.get("country")),
                body=body,
                image_url=r.get("image_url") or "",
            )
        )
    return out


def search(
    query: str,
    *,
    language: str | None = None,
    country: str | None = None,
    domain: str | None = None,
    max_results: int = 10,
    pages: int = 1,
    timeout: float = 30.0,
) -> list[NewsDataArticle]:
    """Latest NewsData articles for a query (optionally scoped to a language / country / domain).
    ``[]`` without a key. ``pages`` walks NewsData's ``nextPage`` token up to ``max_results``.
    ``domain`` (e.g. "bbc.com") scopes to one outlet — used by the per-source history backfill."""
    key = os.environ.get("MAAT_NEWSDATA_KEY")
    if not key:
        return []
    out: list[NewsDataArticle] = []
    page: str | None = None
    left = pages
    while left > 0 and len(out) < max_results:
        params: dict[str, str] = {}
        if query:  # q is optional when a domain/country filter is given; NewsData rejects an empty q
            params["q"] = query
        if language:
            params["language"] = language
        if country:
            params["country"] = country
        if domain:  # filter by the outlet's domain URL (e.g. "bbc.com"); `domain` wants NewsData's IDs
            params["domainurl"] = domain
        if page:
            params["page"] = page
        # Auth via header, NOT the apikey query param — so the key never lands in a URL, error
        # message, or access log (a 422 once echoed the full URL incl. the key).
        r = httpx.get(_LATEST_URL, params=params, headers={"X-ACCESS-KEY": key}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        out.extend(parse_results(data.get("results")))
        page = data.get("nextPage")
        left -= 1
        if not page:
            break
    return out[:max_results]
