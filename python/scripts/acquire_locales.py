"""Per-locale acquisition driver (#239) — a deliberate language/region floor via GDELT.

For each configured locale, query GDELT filtered to that language (+ optional country) so the
corpus actively carries Arabic, Chinese, Russian, Hindi, … coverage in its own language rather
than only what surfaces through English. One paced GDELT call per locale (it throttles ~1/5s),
gated to credible publishers, ingested tagged ``provider: gdelt-locale``.

Run:  uv run python scripts/acquire_locales.py        (or `make acquire-locales`)
Env:  MAAT_LOCALE_MAXRECORDS (default 12)  — records pulled per locale.
      Topics come from MAAT_TOPICS / config/topics.txt (same as the clock); locales from
      config/locales.txt (else the built-in multipolar default set).
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
from maat.acquire.fetch import fetch_article
from maat.acquire.gdelt import search
from maat.acquire.locales import load_locales
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
    new = dropped = 0
    gate_cache: dict = {}
    by_lang: Counter[str] = Counter()
    paced = 0
    for loc in locales:
        if paced:  # GDELT throttles ~1 query/5s — space ALL calls (blocking sleep off the loop)
            await asyncio.sleep(5)
        paced += 1
        try:
            arts = await asyncio.to_thread(
                search, query, maxrecords=maxrecords, timespan="2d",
                sourcelang=loc.language, sourcecountry=(loc.country or None),
            )
        except Exception as e:  # noqa: BLE001 - GDELT down / rate-limited: skip this locale
            print(f"[locale] {loc.label} GDELT unavailable: {e}", flush=True)
            continue
        got = 0
        for a in arts:
            if a.url in seen:
                continue
            if a.domain in denied:
                seen.add(a.url)
                dropped += 1
                continue
            verdict = await asyncio.to_thread(
                accept_source, a.domain, a.title, prompt=gate_prompt, known_good=known_good, cache=gate_cache
            )
            if not verdict.accept:
                seen.add(a.url)
                dropped += 1
                continue
            body, image = await asyncio.to_thread(fetch_article, a.url)
            if not body:
                continue
            await publish(
                nc, "article.ingested", _aid(a.url),
                {
                    "title": a.title, "source": a.domain, "language": a.language,
                    "body": body, "url": a.url, "image_url": image,
                    "provider": "gdelt-locale", "locale": loc.label, "country": a.country,
                },
            )
            seen.add(a.url)
            new += 1
            got += 1
            by_lang[a.language or "?"] += 1
        print(f"[locale] {loc.label} (+{got})", flush=True)
    await nc.flush()
    await nc.close()
    print(
        f"[locale] done: {new} new, {dropped} dropped across {len(locales)} locales. "
        f"langs={dict(by_lang.most_common())}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
