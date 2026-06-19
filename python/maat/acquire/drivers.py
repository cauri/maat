"""Per-source acquisition drivers (#290).

One ``run_*`` per acquisition channel — each does only its source-specific fetch + which fields
the article carries — over the shared :class:`maat.acquire.ingest.Ingestor`. ``acquire()`` wires
pool → state → ingestor → driver → flush, so ``scripts/acquire.py`` is a thin ``--source`` CLI and
the per-source scripts are one-line aliases.

Per-source policy (prefix, source-gate, body-clean, whether it reads the DB) lives in ``_DRIVERS``:
- gdelt   : manual query tool — DB-less (no dedup/deny), no gate, cleans bodies.
- rss     : pre-vetted feed list — DB dedup+deny, no gate, cleans bodies.
- newsdata: API bodies arrive clean — DB dedup+deny, source-gate, no clean.
- locales : per-language floor — DB dedup+deny, source-gate, cleans bodies.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from maat.acquire import apify, newsdata
from maat.acquire.ccnews import detect_lang
from maat.acquire.gdelt import search as gdelt_search
from maat.acquire.ingest import AcqState, Ingestor, load_state, load_topics, topics_query
from maat.acquire.locales import Locale, load_locales
from maat.acquire.rss import fetch_feed, load_feeds
from maat.bus import connect
from maat.db import get_pool

# A multipolar default — the languages an Anglophone-default stream under-covers (ISO-639-1).
_NEWSDATA_DEFAULT_LANGS = "zh,es,ru,ar,hi,pt,fr,de,ja,ko,id,tr"


# --- gdelt (the manual `make acquire QUERY=...` tool) --------------------------------------
async def run_gdelt(ing: Ingestor, *, root: Path, query: str = "", maxrecords: int | None = None, **_: Any) -> None:
    if not query:
        print('usage: acquire.py --source gdelt --query "<query>" [--max N]')
        return
    maxrec = maxrecords if maxrecords is not None else 12
    try:
        # Off the event loop: blocking httpx keeps the NATS client's flush/ping alive (see clock.py).
        arts = await asyncio.to_thread(gdelt_search, query, maxrecords=maxrec, timespan="7d")
    except Exception as e:  # noqa: BLE001 - GDELT down / still rate-limited after retries
        print(f"GDELT unavailable: {e}")
        arts = []
    print(f"GDELT: {len(arts)} articles for {query!r}")
    for a in arts:
        if await ing.ingest(url=a.url, title=a.title, source=a.domain, language=a.language,
                            body=None, image=None):
            print(f"  + [{a.country or '?'}/{a.language or '?'}] {a.domain}: {a.title[:52]}")
    # Fallback: GDELT (and trafilatura) yielded nothing usable — try Apify (search + body in one).
    if ing.new == 0 and apify.available():
        print("GDELT yielded nothing — falling back to Apify rag-web-browser")
        for fa in await asyncio.to_thread(apify.search_and_fetch, query, max_results=maxrec):
            if await ing.ingest(url=fa.url, title=fa.title, source=fa.domain, language=fa.language,
                                body=fa.body, image=fa.image):
                print(f"  + [apify/{fa.language or '?'}] {fa.domain}: {fa.title[:52]}")
    print(f"acquired {ing.new} articles")


# --- rss (the balanced multipolar feed set, #238) ------------------------------------------
async def run_rss(ing: Ingestor, *, root: Path, maxrecords: int | None = None, **_: Any) -> None:
    per_feed = maxrecords if maxrecords is not None else int(os.environ.get("MAAT_RSS_PER_FEED", "12"))
    feeds = load_feeds(root / "config" / "feeds.txt")
    for feed in feeds:
        try:
            items = await asyncio.to_thread(fetch_feed, feed, limit=per_feed)
        except Exception as e:  # noqa: BLE001 - one dead/blocked feed must not abort the run
            print(f"[rss] {feed.label} unavailable: {e}", flush=True)
            continue
        got = 0
        for it in items:
            if await ing.ingest(
                url=it.url, title=it.title, source=it.source, language=it.language,
                body=None, image=None,
                fields={"provider": "rss", "alignment": it.alignment, "country": it.country},
            ):
                got += 1
        print(f"[rss] {feed.label} (+{got})", flush=True)
    print(
        f"[rss] done: {ing.new} new, {ing.dropped} denied across {len(feeds)} feeds. "
        f"langs={dict(ing.by_lang.most_common())} countries={dict(ing.by_country.most_common())}",
        flush=True,
    )


# --- newsdata (a dedicated, reliable, multipolar channel) ----------------------------------
async def run_newsdata(ing: Ingestor, *, root: Path, maxrecords: int | None = None,
                       topics: list[str] | None = None, **_: Any) -> None:
    if not newsdata.available():
        print("[newsdata] MAAT_NEWSDATA_KEY not set — skipping (the channel is dormant)")
        return
    maxrec = maxrecords if maxrecords is not None else int(os.environ.get("MAAT_NEWSDATA_MAXRECORDS", "10"))
    langs = [c.strip() for c in os.environ.get("MAAT_NEWSDATA_LANGS", _NEWSDATA_DEFAULT_LANGS).split(",") if c.strip()]
    query = topics_query(load_topics(root, topics))
    # A broad pass (whatever's latest across languages) + one floor pass per target language.
    passes: list[tuple[str, str | None]] = [("broad", None)] + [(lng, lng) for lng in langs]
    for label, lang in passes:
        try:
            arts = await asyncio.to_thread(newsdata.search, query, language=lang, max_results=maxrec)
        except Exception as e:  # noqa: BLE001 — a paid provider must not abort the run
            print(f"[newsdata] {label} unavailable: {e}", flush=True)
            continue
        before = ing.new
        for a in arts:
            # API bodies arrive present-or-absent; "" (not None) means "skip", never "fetch".
            await ing.ingest(
                url=a.url, title=a.title, source=a.domain, language=a.language,
                body=a.body or "", image=a.image_url,
                fields={"provider": "newsdata", "country": a.country},
            )
        print(f"[newsdata] {label} (+{ing.new - before})", flush=True)
    print(
        f"[newsdata] done: {ing.new} new, {ing.dropped} dropped across {len(passes)} passes. "
        f"langs={dict(ing.by_lang.most_common())}",
        flush=True,
    )


# --- locales (per-language floor, #239: GDELT then Apify floor) -----------------------------
async def _locale_gdelt_phase(ing: Ingestor, locales: list[Locale], query: str, maxrecords: int) -> list[Locale]:
    """Best-effort GDELT pull per locale (paced). Returns the locales it could NOT fill — they go to
    the Apify floor. Bails out of GDELT entirely on the first 429 (the free API throttles by IP)."""
    needs_floor: list[Locale] = []
    gdelt_dead = False
    paced = 0
    for loc in locales:
        got = 0
        if not gdelt_dead:
            if paced:  # GDELT throttles ~1 query/5s
                await asyncio.sleep(5)
            paced += 1
            try:
                arts = await asyncio.to_thread(
                    gdelt_search, query, maxrecords=maxrecords, timespan="2d",
                    sourcelang=loc.language, sourcecountry=(loc.country or None),
                )
            except Exception as e:  # noqa: BLE001 - GDELT down / rate-limited
                print(f"[locale] {loc.label} gdelt unavailable: {e}", flush=True)
                arts = []
                if "429" in str(e):
                    gdelt_dead = True  # stop hammering a throttled API; the Apify floor takes over
            else:
                for a in arts:
                    if await ing.ingest(
                        url=a.url, title=a.title, source=a.domain, language=a.language,
                        body=None, image=None,
                        fields={"provider": "gdelt-locale", "locale": loc.label,
                                "country": a.country or loc.country},
                    ):
                        got += 1
            if not gdelt_dead:
                print(f"[locale] {loc.label} gdelt(+{got})", flush=True)
        if got == 0 and loc.terms:
            needs_floor.append(loc)
    return needs_floor


async def _locale_apify_floor(ing: Ingestor, locales: list[Locale], maxrecords: int) -> None:
    """Paid, reliable per-locale floor: Google each locale's native-language ``terms`` via Apify and
    ingest the in-language results. Runs locales concurrently (bounded) so the floor costs ~one slow
    request of wall-clock, not one-per-locale."""
    if not (apify.available() and locales):
        if locales:
            print(f"[locale] apify floor skipped (no APIFY_API_KEY) for {len(locales)} locale(s)", flush=True)
        return
    sem = asyncio.Semaphore(int(os.environ.get("MAAT_LOCALE_APIFY_CONC", "5")))

    async def floor(loc: Locale) -> None:
        async with sem:
            try:
                arts = await asyncio.to_thread(
                    apify.search_and_fetch, loc.terms, max_results=maxrecords, timeout=150.0
                )
            except Exception as e:  # noqa: BLE001
                print(f"[locale] {loc.label} apify unavailable: {e}", flush=True)
                return
            got = 0
            for fa in arts:
                # metadata languageCode is unreliable for the floor — detect from the body.
                if await ing.ingest(
                    url=fa.url, title=fa.title, source=fa.domain, language=fa.language,
                    body=fa.body, image=None,
                    fields={"provider": "apify-locale", "locale": loc.label, "country": loc.country},
                    detect_language=detect_lang,
                ):
                    got += 1
            print(f"[locale] {loc.label} apify(+{got})", flush=True)

    await asyncio.gather(*(floor(loc) for loc in locales))


async def run_locales(ing: Ingestor, *, root: Path, maxrecords: int | None = None,
                      topics: list[str] | None = None, **_: Any) -> None:
    maxrec = maxrecords if maxrecords is not None else int(os.environ.get("MAAT_LOCALE_MAXRECORDS", "12"))
    query = topics_query(load_topics(root, topics))
    locales = load_locales(root / "config" / "locales.txt")
    needs_floor = await _locale_gdelt_phase(ing, locales, query, maxrec)
    await _locale_apify_floor(ing, needs_floor, maxrec)
    print(
        f"[locale] done: {ing.new} new, {ing.dropped} dropped across {len(locales)} locales "
        f"({len(needs_floor)} via apify floor). langs={dict(ing.by_lang.most_common())}",
        flush=True,
    )


# (prefix, gate, clean, reads_db), driver
_DRIVERS: dict[str, tuple[tuple[str, bool, bool, bool], Any]] = {
    "gdelt": (("gd", False, True, False), run_gdelt),
    "rss": (("rss", False, True, True), run_rss),
    "newsdata": (("nd", True, False, True), run_newsdata),
    "locales": (("loc", True, True, True), run_locales),
}

SOURCES = tuple(_DRIVERS)


async def acquire(source: str, *, root: Path, **opts: Any) -> None:
    """Run one acquisition source end-to-end: build the shared ingestor, dispatch, flush, close."""
    try:
        (prefix, gate, clean, reads_db), driver = _DRIVERS[source]
    except KeyError:
        raise SystemExit(f"unknown --source {source!r}; choose from {', '.join(SOURCES)}")
    if reads_db:
        pool = await get_pool()
        try:
            state = await load_state(pool, with_gate=gate)
        finally:
            await pool.close()
    else:  # gdelt: manual tool, intentionally DB-less (no dedup/deny)
        state = AcqState(seen=set(), denied=set())
    nc = await connect()
    ing = Ingestor(nc, state, prefix=prefix, gate=gate, clean=clean)
    try:
        await driver(ing, root=root, **opts)
    finally:
        await nc.flush()
        await nc.close()
