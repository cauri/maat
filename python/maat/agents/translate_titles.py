"""Title translation step (feed display, #54) — English gloss for non-English article titles.

cauri: on the feed, show every non-English title with its English translation next to the original.
This step translates each non-English title ONCE (Mistral; display-only, never scored — §4) and
caches it as an ``article.title_en`` event; the feed reads the cache and shows original + English.
Bounded to untranslated non-English titles; a no-op without a Mistral key (translate_text degrades
to identity, so we simply don't emit and re-try on a later tick).

Run: uv run python -m maat.agents.translate_titles  (in the clock loop, after acquire).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat.bus import connect
from maat.events import ARTICLE_TITLE_EN, publish
from maat.translate import translate_text

ROOT = Path(__file__).resolve().parents[3]


def _is_english(lang: str) -> bool:
    return (lang or "").strip().lower()[:2] in ("", "en")


async def main() -> None:
    if os.environ.get("MAAT_TRANSLATE_TITLES", "1") != "1":
        print("translate-titles: MAAT_TRANSLATE_TITLES != 1 — disabled")
        return
    load_dotenv(ROOT / ".env")
    tenant = os.environ.get("MAAT_TENANT_ID", "cauri")
    pool = await get_pool()
    arts = await pool.fetch(
        "select id, title, language from articles where tenant_id = $1 and title is not null", tenant
    )
    done = {
        r["aid"]
        for r in await pool.fetch(
            "select distinct stream_id aid from events where type = $1", ARTICLE_TITLE_EN
        )
    }
    await pool.close()

    todo = [r for r in arts if not _is_english(r["language"]) and r["id"] not in done]
    if not todo:
        print("translate-titles: no untranslated non-English titles")
        return

    nc = await connect()
    translated = 0
    for r in todo:
        en, engine = translate_text(r["title"], "en", source=(r["language"] or None))
        if engine != "mistral":
            continue  # no key / provider error — don't mark done; re-try on a later tick
        await publish(
            nc, ARTICLE_TITLE_EN, r["id"],
            {"article_id": r["id"], "title_en": en.strip(), "lang": r["language"] or ""}, tenant,
        )
        translated += 1
    await nc.flush()
    await nc.close()
    print(f"translate-titles: translated {translated}/{len(todo)} non-English title(s)")


if __name__ == "__main__":
    asyncio.run(main())
