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

# #417 — commit in batches so a step timeout can never discard the run's work, and cap the work per
# tick (one LLM translate per title). The `done` set above dedupes, so a capped tick still makes permanent progress.
_BATCH = int(os.environ.get("MAAT_TRANSLATE_TITLES_BATCH", "25"))
_MAX_PER_TICK = int(os.environ.get("MAAT_TRANSLATE_TITLES_MAX_PER_TICK", "200"))


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

    # #417 — same defect as ownership/geotag: `translate_text` is a BLOCKING LLM call made in the
    # loop while holding a NATS connection, starving the keepalive → server dropped the link →
    # flush() raised FlushTimeoutError → every translation lost. ZERO events in this agent's life.
    # Translate OFF the loop, commit in BATCHES (a step timeout must not discard the whole run —
    # `done` dedupes, so each tick makes permanent progress), and cap the work per tick.
    todo = todo[:_MAX_PER_TICK]
    translated = 0
    for start in range(0, len(todo), _BATCH):
        rows: list[tuple[str, str, str]] = []
        for r in todo[start : start + _BATCH]:
            en, engine = await asyncio.to_thread(
                translate_text, r["title"], "en", (r["language"] or None)
            )
            if engine != "mistral":
                continue  # no key / provider error — don't mark done; re-try on a later tick
            rows.append((r["id"], en.strip(), r["language"] or ""))
        if not rows:
            continue
        nc = await connect()
        try:
            for aid, en, lang in rows:
                await publish(
                    nc, ARTICLE_TITLE_EN, aid,
                    {"article_id": aid, "title_en": en, "lang": lang}, tenant,
                )
            await nc.flush()
        finally:
            await nc.close()
        translated += len(rows)
        print(f"translate-titles: committed {translated}/{len(todo)}", flush=True)
    print(f"translate-titles: translated {translated}/{len(todo)} non-English title(s)")


if __name__ == "__main__":
    asyncio.run(main())
