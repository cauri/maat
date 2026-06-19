"""Historical backfill (#40, §6.5) — replay prior news to bootstrap reputation, archive-bias-corrected.

History is the one regime where the eventual-primary-truth signal already exists: resolved claims
arrive with the answer key, so backfilled corroboration scores against the same anti-consensus
anchor, legitimately (BRIEF §6.5). But archives over-represent large English-language majors, so a
naive replay would amplify the exact US/Anglo slant the product exists to escape. This driver
therefore DE-SLANTS each window's candidates before ingesting them:

  1. walk back over `MAAT_BACKFILL_WEEKS` weekly windows (GDELT DOC startdatetime/enddatetime);
  2. per window, gather candidates, drop already-seen + operator-denied sources;
  3. MEASURE the (language, country) skew (learning.backfill.bias_summary) and CAP each stratum
     (cap_per_stratum) so no cell — the English-language majors above all — dominates the replay;
  4. gate to credible publishers, fetch bodies, ingest as `article.ingested` tagged `backfill: true`
     with the article's GDELT `seendate`, so the corpus records the provenance.

The backfilled articles flow through the normal extract → classify → corroborate pipeline; their
contribution to reputation is then naturally OVERWRITTEN by live evidence over time (§6.5's decaying,
capped prior). Cost scales with weeks × topics — run it deliberately, not on the live clock.

Run: uv run python scripts/backfill.py ["topic" ...]   (or `make backfill`)
Env: MAAT_BACKFILL_WEEKS (default 8), MAAT_BACKFILL_PER_STRATUM (default 5),
     MAAT_BACKFILL_MAXRECORDS (default 50); topics like clock.py (args / MAAT_TOPICS / topics.txt).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from maat import ids
from maat.db import get_pool
from maat import prompts
from maat.acquire.clean import clean_article
from maat.acquire.fetch import fetch_article
from maat.acquire.gdelt import search_window, week_windows
from maat.acquire.source_gate import accept_source
from maat.bus import connect
from maat.events import publish
from maat.learning.backfill import bias_summary, cap_per_stratum
from maat.serving.source_flags import denied_sources
from maat.serving.topics import news_queries

ROOT = Path(__file__).resolve().parents[2]


def _aid(url: str) -> str:
    return ids.article_id(url, "gd")


def _topics() -> list[str]:
    if len(sys.argv) > 1:
        return sys.argv[1:]
    env = os.environ.get("MAAT_TOPICS")
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    f = ROOT / "config" / "topics.txt"
    if f.exists():
        return [ln.strip() for ln in f.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return []


async def main() -> None:
    load_dotenv(ROOT / ".env")
    topics = _topics()
    if not topics:
        print("no topics — pass args, set MAAT_TOPICS, or fill config/topics.txt")
        return
    weeks = int(os.environ.get("MAAT_BACKFILL_WEEKS", "8"))
    per_stratum = int(os.environ.get("MAAT_BACKFILL_PER_STRATUM", "5"))
    maxrecords = int(os.environ.get("MAAT_BACKFILL_MAXRECORDS", "50"))

    pool = await get_pool()
    seen = {r["url"] for r in await pool.fetch("select url from articles where url is not null")}
    queries_prompt = await prompts.active_text(
        pool, "acquire_queries", prompts.seed_default("acquire_queries")
    )
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

    windows = week_windows(datetime.now(timezone.utc), weeks)
    # One news-query expansion per topic (LLM), reused across every window — keeps backfill cheap.
    topic_queries = {
        topic: await asyncio.to_thread(news_queries, topic, prompt=queries_prompt) for topic in topics
    }

    nc = await connect()
    new = 0
    dropped = 0
    capped = 0
    gate_cache: dict = {}
    paced = 0
    for topic in topics:
        queries = topic_queries[topic]
        for start, end in windows:
            # Pool this window's candidates across the topic's queries (GDELT metadata only — no
            # body fetch yet), so the bias correction sees the whole window before we spend on bodies.
            cand: list[dict] = []
            for q in queries:
                if paced:  # GDELT throttles ~1 query/5s — space ALL calls (a blocking sleep off-loop)
                    await asyncio.sleep(5)
                paced += 1
                try:
                    arts = await asyncio.to_thread(
                        search_window, q, start=start, end=end, maxrecords=maxrecords
                    )
                except Exception as e:  # noqa: BLE001 — GDELT down / rate-limited: skip this query
                    print(f"  [{topic}] {start:%Y-%m-%d} GDELT unavailable: {e}", flush=True)
                    arts = []
                for a in arts:
                    if a.url in seen or a.domain in denied:
                        if a.domain in denied:
                            dropped += 1
                        seen.add(a.url)
                        continue
                    seen.add(a.url)  # dedup within the run too
                    cand.append({"language": a.language, "country": a.country, "art": a})

            if not cand:
                continue
            # Archive-bias correction (§6.5): report the skew, then cap each (lang, country) stratum
            # so the English-language majors can't dominate this window's replay.
            before = len(cand)
            report = bias_summary(cand)
            kept = cap_per_stratum(cand, cap=per_stratum)
            capped += before - len(kept)
            print(
                f"[{topic}] {start:%Y-%m-%d}..{end:%Y-%m-%d}: {before} candidates, "
                f"top stratum {report.most_overrepresented} {report.most_overrepresented_fraction:.0%} "
                f"→ kept {len(kept)} after cap (per-stratum {per_stratum})",
                flush=True,
            )

            for d in kept:
                a = d["art"]
                verdict = await asyncio.to_thread(
                    accept_source, a.domain, a.title,
                    prompt=gate_prompt, known_good=known_good, cache=gate_cache,
                )
                if not verdict.accept:
                    dropped += 1
                    continue
                body, image_url = await asyncio.to_thread(fetch_article, a.url)
                if not body:
                    continue
                ct, cb = clean_article(a.title, body, a.domain)  # strip scraped boilerplate (#33)
                await publish(
                    nc, "article.ingested", _aid(a.url),
                    {"title": ct, "source": a.domain, "language": a.language, "body": cb,
                     "url": a.url, "image_url": image_url, "backfill": True, "seendate": a.seendate},
                )
                new += 1

    await nc.flush()
    await nc.close()
    print(
        f"backfill: {new} historical articles across {len(topics)} topics × {weeks} weeks "
        f"({capped} dropped by archive-bias cap, {dropped} by deny/gate)",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
