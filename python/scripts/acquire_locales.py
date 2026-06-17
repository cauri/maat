"""Per-locale acquisition driver (#239) — a deliberate language/region floor.

For each configured locale we try to pull news IN THAT LANGUAGE, so the corpus actively carries
Arabic, Chinese, Russian, Hindi, … coverage in its own language rather than only what surfaces
through English. Two engines, in order:

  1. GDELT filtered to the locale's ``sourcelang`` (+ optional ``sourcecountry``) — free, paced.
  2. Apify floor (paid, reliable): when GDELT is rate-limited (429s pervasively) or returns
     nothing for a locale, Google the locale's native-language ``terms`` via apify/rag-web-browser
     and ingest the in-language results. This is what keeps the floor real — GDELT's free API
     throttles hard under the clock's load, so on the box it is effectively always the Apify pass.

The Apify floor runs the per-locale calls CONCURRENTLY (bounded) so the whole floor finishes in
~one slow request, not one-per-locale serially. Articles are tagged ``provider: gdelt-locale`` or
``provider: apify-locale`` with their detected language + locale label.

Run:  uv run python scripts/acquire_locales.py        (or `make acquire-locales`)
Env:  MAAT_LOCALE_MAXRECORDS (default 12) — records pulled per locale per engine.
      MAAT_LOCALE_APIFY_CONC (default 5)  — max concurrent Apify locale calls.
      Topics come from MAAT_TOPICS / config/topics.txt (GDELT only); the Apify floor uses each
      locale's native ``terms``. Locales from config/locales.txt (else the multipolar default).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat import prompts
from maat.acquire import apify
from maat.acquire.ccnews import detect_lang
from maat.acquire.fetch import fetch_article
from maat.acquire.gdelt import search
from maat.acquire.locales import Locale, load_locales
from maat.acquire.source_gate import accept_source
from maat.bus import connect
from maat.events import publish
from maat.serving.source_flags import denied_sources

ROOT = Path(__file__).resolve().parents[2]


def _aid(url: str) -> str:
    return "loc-" + hashlib.sha1(url.encode()).hexdigest()[:18]


def _topics() -> list[str]:
    if len(sys.argv) > 1:
        return sys.argv[1:]
    env = os.environ.get("MAAT_TOPICS")
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    f = ROOT / "config" / "topics.txt"
    if f.exists():
        return [ln.strip() for ln in f.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return ["news"]


def _query(topics: list[str]) -> str:
    # OR-of-phrases so one GDELT call per locale covers all tracked topics (GDELT matches across
    # languages, so English topic phrases still pull that-language articles via the sourcelang filter).
    return " OR ".join(f'"{t}"' for t in topics) if topics else "news"


class _Ctx:
    """Shared acquisition state threaded through both engines (loop-thread mutation only)."""

    def __init__(self, nc, seen, denied, gate_prompt, known_good):
        self.nc = nc
        self.seen: set[str] = seen
        self.denied = denied
        self.gate_prompt = gate_prompt
        self.known_good = known_good
        self.gate_cache: dict = {}
        self.by_lang: Counter[str] = Counter()
        self.new = 0
        self.dropped = 0

    async def ingest(self, *, url, title, source, language, body, image, provider, loc: Locale,
                     country: str | None) -> bool:
        """Dedup → deny-list → source-gate → publish one candidate. Returns True if ingested.

        ``body`` may be None for GDELT candidates (fetched here); Apify candidates arrive with a
        body already. We claim ``url`` in ``seen`` up front so concurrent locale passes never
        double-fetch the same article."""
        if not url or url in self.seen:
            return False
        self.seen.add(url)
        if source in self.denied:
            self.dropped += 1
            return False
        verdict = await asyncio.to_thread(
            accept_source, source, title, prompt=self.gate_prompt,
            known_good=self.known_good, cache=self.gate_cache,
        )
        if not verdict.accept:
            self.dropped += 1
            return False
        if body is None:  # GDELT path: fetch the body now
            body, image = await asyncio.to_thread(fetch_article, url)
        if not body:
            return False
        if provider == "apify-locale":  # metadata languageCode is unreliable — detect from the body
            language = detect_lang(body) or language
        await publish(
            self.nc, "article.ingested", _aid(url),
            {
                "title": title, "source": source, "language": language,
                "body": body, "url": url, "image_url": image,
                "provider": provider, "locale": loc.label, "country": country or loc.country,
            },
        )
        self.new += 1
        self.by_lang[language or "?"] += 1
        return True


async def _gdelt_phase(ctx: _Ctx, locales: list[Locale], query: str, maxrecords: int) -> list[Locale]:
    """Best-effort GDELT pull per locale (paced). Returns the locales it could NOT fill — they go
    to the Apify floor. Bails out of GDELT entirely on the first 429: the free API throttles by IP,
    so once one locale is rate-limited the rest will be too, and pacing through 12 dead calls just
    wastes a minute per tick."""
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
                    search, query, maxrecords=maxrecords, timespan="2d",
                    sourcelang=loc.language, sourcecountry=(loc.country or None),
                )
            except Exception as e:  # noqa: BLE001 - GDELT down / rate-limited
                print(f"[locale] {loc.label} gdelt unavailable: {e}", flush=True)
                arts = []
                if "429" in str(e):
                    gdelt_dead = True  # stop hammering a throttled API; the Apify floor takes over
            else:
                for a in arts:
                    if await ctx.ingest(
                        url=a.url, title=a.title, source=a.domain, language=a.language,
                        body=None, image=None, provider="gdelt-locale", loc=loc, country=a.country,
                    ):
                        got += 1
            if not gdelt_dead:
                print(f"[locale] {loc.label} gdelt(+{got})", flush=True)
        if got == 0 and loc.terms:
            needs_floor.append(loc)
    return needs_floor


async def _apify_floor(ctx: _Ctx, locales: list[Locale], maxrecords: int) -> None:
    """Paid, reliable per-locale floor: Google each locale's native-language ``terms`` via Apify
    and ingest the in-language results. Runs locales concurrently (bounded) so the floor costs ~one
    slow request of wall-clock, not one-per-locale."""
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
                if await ctx.ingest(
                    url=fa.url, title=fa.title, source=fa.domain, language=fa.language,
                    body=fa.body, image=None, provider="apify-locale", loc=loc, country=None,
                ):
                    got += 1
            print(f"[locale] {loc.label} apify(+{got})", flush=True)

    await asyncio.gather(*(floor(loc) for loc in locales))


async def main() -> None:
    load_dotenv(ROOT / ".env")
    maxrecords = int(os.environ.get("MAAT_LOCALE_MAXRECORDS", "12"))
    query = _query(_topics())

    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    seen = {r["url"] for r in await pool.fetch("select url from articles where url is not null")}
    gate_prompt = await prompts.active_text(pool, "source_gate", prompts.seed_default("source_gate"))
    known_good = frozenset(
        (r["source"] or "").lower().removeprefix("www.")
        for r in await pool.fetch("select distinct source from articles where source is not null")
    )
    denied = denied_sources(
        (json.loads(r["data"]) if isinstance(r["data"], str) else r["data"])
        for r in await pool.fetch(
            "select data from events where type = 'admin.source.flagged' order by id"
        )
    )
    await pool.close()

    locales = load_locales(ROOT / "config" / "locales.txt")
    nc = await connect()
    ctx = _Ctx(nc, seen, denied, gate_prompt, known_good)

    needs_floor = await _gdelt_phase(ctx, locales, query, maxrecords)
    await _apify_floor(ctx, needs_floor, maxrecords)

    await nc.flush()
    await nc.close()
    print(
        f"[locale] done: {ctx.new} new, {ctx.dropped} dropped across {len(locales)} locales "
        f"({len(needs_floor)} via apify floor). langs={dict(ctx.by_lang.most_common())}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
