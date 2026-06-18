"""Per-source history acquisition (#241 backfilling stage) — pull an outlet's PAST articles so a
newly-registered source can earn a real reputation instead of waiting to accrue one live.

Channel-pluggable, tried in order until ``depth`` is met (each is best-effort; a dead channel just
falls through):

  1. GDELT  — ``domain:<source>`` over the last N months (free, purpose-built for per-domain history).
  2. Apify  — ``site:<source>`` web search (paid; reliable where GDELT 429s or thins out).
  3. NewsData.io — ``domain=<source>`` (paid; only if MAAT_NEWSDATA_KEY is set).

Returns de-duplicated articles WITH bodies (GDELT gives metadata, so its URLs are fetched here;
Apify / NewsData already carry the body). Pure-ish: network only, no DB / bus.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from maat.acquire import apify, newsdata
from maat.acquire.fetch import fetch_article
from maat.acquire.gdelt import search_window


@dataclass(frozen=True)
class HistoryArticle:
    url: str
    title: str
    domain: str
    language: str
    body: str
    image_url: str
    channel: str  # which acquisition channel surfaced it (gdelt / apify / newsdata)


def _norm(domain: str) -> str:
    return (domain or "").strip().lower().removeprefix("www.")


async def _gdelt_history(source: str, *, depth: int, months: int, fetch_conc: int) -> list[HistoryArticle]:
    now = datetime.now(timezone.utc)
    try:
        metas = await asyncio.to_thread(
            search_window,
            f"domain:{source}",
            start=now - timedelta(days=30 * months),
            end=now,
            maxrecords=min(depth * 2, 250),
        )
    except Exception as e:  # noqa: BLE001 — GDELT down / rate-limited: fall through to the next channel
        print(f"[history] {source} gdelt unavailable: {e}", flush=True)
        return []
    # GDELT yields metadata; fetch bodies concurrently (bounded) — this is the slow part.
    sem = asyncio.Semaphore(fetch_conc)
    seen: set[str] = set()
    metas = [m for m in metas if m.url and not (m.url in seen or seen.add(m.url))]

    async def fetch(m) -> HistoryArticle | None:
        async with sem:
            body, image = await asyncio.to_thread(fetch_article, m.url)
        if not body:
            return None
        return HistoryArticle(
            url=m.url, title=m.title, domain=_norm(m.domain or source), language=m.language or "",
            body=body, image_url=image or "", channel="gdelt",
        )
    out = await asyncio.gather(*(fetch(m) for m in metas[: depth * 2]))
    return [a for a in out if a][:depth]


async def _apify_history(source: str, *, depth: int) -> list[HistoryArticle]:
    if not apify.available():
        return []
    try:
        items = await asyncio.to_thread(apify.search_and_fetch, f"site:{source}", max_results=depth)
    except Exception as e:  # noqa: BLE001
        print(f"[history] {source} apify unavailable: {e}", flush=True)
        return []
    return [
        HistoryArticle(url=a.url, title=a.title, domain=_norm(a.domain or source),
                       language=a.language or "", body=a.body, image_url="", channel="apify")
        for a in items
    ]


async def _newsdata_history(source: str, *, depth: int) -> list[HistoryArticle]:
    if not newsdata.available():
        return []
    try:
        arts = await asyncio.to_thread(
            newsdata.search, "", domain=_norm(source), max_results=depth, pages=2
        )
    except Exception as e:  # noqa: BLE001
        print(f"[history] {source} newsdata unavailable: {e}", flush=True)
        return []
    return [
        HistoryArticle(url=a.url, title=a.title, domain=_norm(a.domain or source),
                       language=a.language or "", body=a.body, image_url=a.image_url, channel="newsdata")
        for a in arts
    ]


async def fetch_source_history(
    source: str, *, depth: int = 100, months: int | None = None, fetch_conc: int = 8
) -> list[HistoryArticle]:
    """An outlet's recent published history, up to ``depth`` articles with bodies. Tries GDELT, then
    tops up from Apify, then NewsData, de-duplicating by URL — so a thin channel is supplemented, not
    relied upon alone."""
    months = months or int(os.environ.get("MAAT_BACKFILL_MONTHS", "6"))
    source = _norm(source)
    out: list[HistoryArticle] = []
    seen: set[str] = set()

    def add(arts: list[HistoryArticle]) -> None:
        for a in arts:
            if a.url and a.url not in seen and len(out) < depth:
                seen.add(a.url)
                out.append(a)

    add(await _gdelt_history(source, depth=depth, months=months, fetch_conc=fetch_conc))
    if len(out) < depth:
        add(await _apify_history(source, depth=depth - len(out)))
    if len(out) < depth:
        add(await _newsdata_history(source, depth=depth - len(out)))
    return out
