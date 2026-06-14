"""GDELT DOC 2.0 acquisition (§8, #33) — broad, global, multilingual article discovery.

GDELT monitors news in ~100 languages worldwide with no API key. We query it for articles on
a topic and fetch the bodies ourselves (`fetch.py`). This is the de-slanted acquisition seam:
source-agnostic by construction, with optional sourcelang / sourcecountry filters to widen
coverage on purpose (one query for "central bank interest rate" already returns Macedonian,
Chinese, Hindi, Norwegian and Indian-English outlets). The learning loop that narrows toward
rewarding sources is #35.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
_UA = "maat-acquire/0.1 (veracity research)"


@dataclass(frozen=True)
class GdeltArticle:
    url: str
    title: str
    domain: str
    language: str
    country: str
    seendate: str


def build_params(
    query: str,
    *,
    maxrecords: int = 20,
    timespan: str = "3d",
    sourcelang: str | None = None,
    sourcecountry: str | None = None,
) -> dict[str, str]:
    q = query.strip()
    if sourcelang:
        q += f" sourcelang:{sourcelang}"
    if sourcecountry:
        q += f" sourcecountry:{sourcecountry}"
    return {
        "query": q,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(maxrecords),
        "timespan": timespan,
        "sort": "hybridrel",
    }


def parse_articles(data: dict) -> list[GdeltArticle]:
    out: list[GdeltArticle] = []
    for a in data.get("articles", []) or []:
        url = a.get("url")
        if not url:
            continue
        out.append(
            GdeltArticle(
                url=url,
                title=(a.get("title") or "").strip(),
                domain=a.get("domain") or "",
                language=a.get("language") or "",
                country=a.get("sourcecountry") or "",
                seendate=a.get("seendate") or "",
            )
        )
    return out


def search(
    query: str,
    *,
    maxrecords: int = 20,
    timespan: str = "3d",
    sourcelang: str | None = None,
    sourcecountry: str | None = None,
    timeout: float = 30.0,
    retries: int = 4,
) -> list[GdeltArticle]:
    """Query GDELT DOC for recent articles matching `query` (broad global news search).

    GDELT throttles to roughly one query every 5s and answers bursts with 429; back off and
    retry rather than failing the acquisition run.
    """
    params = build_params(
        query,
        maxrecords=maxrecords,
        timespan=timespan,
        sourcelang=sourcelang,
        sourcecountry=sourcecountry,
    )
    headers = {"User-Agent": _UA}
    last: httpx.Response | None = None
    for attempt in range(retries):
        last = httpx.get(DOC_API, params=params, headers=headers, timeout=timeout)
        if last.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        last.raise_for_status()
        # GDELT returns plaintext errors (e.g. a too-broad query) instead of JSON — guard.
        if "json" not in last.headers.get("content-type", ""):
            return []
        return parse_articles(last.json())
    if last is not None:
        last.raise_for_status()
    return []
