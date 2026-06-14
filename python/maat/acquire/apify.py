"""Apify fallback for acquisition (#33) — backup web search + content when GDELT is down.

Uses the apify/rag-web-browser actor (search + page extraction in one) via Apify's REST API.
Activated only when APIFY_API_KEY is set; GDELT stays the primary acquisition source. Handy
not just when GDELT rate-limits — its search also surfaces primary sources (e.g. the ECB's own
release) that a news-only stream misses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

_RUN_SYNC = "https://api.apify.com/v2/acts/apify~rag-web-browser/run-sync-get-dataset-items"


@dataclass(frozen=True)
class FetchedArticle:
    url: str
    title: str
    domain: str
    language: str
    body: str


def available() -> bool:
    return bool(os.environ.get("APIFY_API_KEY"))


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except ValueError:
        return ""


def parse_items(items: list, *, min_chars: int = 200) -> list[FetchedArticle]:
    out: list[FetchedArticle] = []
    for it in items or []:
        md = it.get("metadata") or {}
        sr = it.get("searchResult") or {}
        url = md.get("url") or sr.get("url")
        body = it.get("text") or it.get("markdown") or ""
        if not url or len(body) < min_chars:
            continue
        out.append(
            FetchedArticle(
                url=url,
                title=(md.get("title") or sr.get("title") or "").strip(),
                domain=_domain(url),
                language=md.get("languageCode") or "",
                body=body,
            )
        )
    return out


def search_and_fetch(query: str, *, max_results: int = 10, timeout: float = 180.0) -> list[FetchedArticle]:
    """Search the web + fetch article bodies via Apify (the GDELT fallback). [] without a key."""
    token = os.environ.get("APIFY_API_KEY")
    if not token:
        return []
    r = httpx.post(
        _RUN_SYNC,
        params={"token": token},
        json={"query": query, "maxResults": max_results},
        timeout=timeout,
    )
    r.raise_for_status()
    return parse_items(r.json())
