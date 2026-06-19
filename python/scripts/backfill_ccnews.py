"""CC-News backfill (#237, P3) — replay Common Crawl's news archive to train reputation, de-slanted.

CC-NEWS is a large multilingual archive; replayed naively it would amplify the English-language
majors, so this driver reuses the SAME archive-bias correction as the GDELT backfill (#40):
measure the (language, country) skew, then ``cap_per_stratum`` before ingest. Articles flow
through the normal extract -> classify -> corroborate -> reputation path, tagged
``backfill: true, provider: cc-news`` with the WARC date, so their reputation contribution is
overwritten by live evidence over time (§6.5). Unlike GDELT, CC-NEWS records carry the body +
language already, so there is no separate fetch.

Run:  uv run python scripts/backfill_ccnews.py        (or `make ccnews-backfill`)
Env:  MAAT_CCNEWS_MONTHS (default 1)        — how many prior months to walk back
      MAAT_CCNEWS_WARCS (default 2)         — WARC files sampled per month
      MAAT_CCNEWS_PER_WARC (default 200)    — articles parsed per WARC (streamed, stops early)
      MAAT_CCNEWS_PER_STRATUM (default 8)   — cap per (language, country) cell after pooling
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat import prompts
from maat.acquire import ccnews
from maat.acquire.source_gate import accept_source
from maat.bus import connect
from maat.events import publish
from maat.learning.backfill import bias_summary, cap_per_stratum
from maat.serving.source_flags import denied_sources

ROOT = Path(__file__).resolve().parents[2]


def _aid(url: str) -> str:
    return "cc-" + hashlib.sha1(url.encode()).hexdigest()[:18]


def _months_back(now: datetime, n: int) -> list[tuple[int, int]]:
    """The n full months before ``now`` (current month is usually incomplete, so start at -1)."""
    y, m, out = now.year, now.month, []
    for _ in range(max(1, n)):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        out.append((y, m))
    return out


async def main() -> None:
    load_dotenv(ROOT / ".env")
    months = int(os.environ.get("MAAT_CCNEWS_MONTHS", "1"))
    warcs = int(os.environ.get("MAAT_CCNEWS_WARCS", "2"))
    per_warc = int(os.environ.get("MAAT_CCNEWS_PER_WARC", "200"))
    per_stratum = int(os.environ.get("MAAT_CCNEWS_PER_STRATUM", "8"))

    pool = await get_pool()
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

    # 1) Gather candidates across the sampled WARCs (body + language already present).
    cand: list[dict] = []
    for year, month in _months_back(datetime.now(timezone.utc), months):
        try:
            paths = await asyncio.to_thread(ccnews.warc_paths, year, month, limit=warcs)
        except Exception as e:  # noqa: BLE001 - month missing / network: skip it
            print(f"[ccnews] {year}-{month:02d} index unavailable: {e}", flush=True)
            continue
        for path in paths:
            try:
                arts = await asyncio.to_thread(ccnews.fetch_warc, path, limit=per_warc)
            except Exception as e:  # noqa: BLE001 - one WARC failing must not abort the run
                print(f"[ccnews] WARC unavailable ({path}): {e}", flush=True)
                continue
            for a in arts:
                if a.url in seen:
                    continue
                seen.add(a.url)  # dedup within the run too
                cand.append({"language": a.language, "country": a.country, "art": a})
            print(f"[ccnews] {year}-{month:02d} {path.split('/')[-1]}: pooled {len(cand)} so far", flush=True)

    if not cand:
        print("[ccnews] no candidates gathered", flush=True)
        return

    # 2) Archive-bias correction (§6.5): report the skew, then cap each (lang, country) stratum.
    report = bias_summary(cand)
    print(
        f"[ccnews] bias: {report.n_articles} articles, {report.n_strata} strata, "
        f"top={report.most_overrepresented} ({report.most_overrepresented_fraction:.1%}), "
        f"ESS={report.effective_sample_size}",
        flush=True,
    )
    kept = cap_per_stratum(cand, cap=per_stratum)
    print(f"[ccnews] de-slanted {len(cand)} -> {len(kept)} (cap {per_stratum}/stratum)", flush=True)

    # 3) Gate (credible publisher only) + publish, tagged backfill/provider with the WARC date.
    nc = await connect()
    new = dropped = 0
    gate_cache: dict = {}
    for c in kept:
        a: ccnews.CCNewsArticle = c["art"]
        if a.source in denied:
            dropped += 1
            continue
        verdict = await asyncio.to_thread(
            accept_source, a.source, a.title, prompt=gate_prompt, known_good=known_good, cache=gate_cache
        )
        if not verdict.accept:
            dropped += 1
            continue
        await publish(
            nc, "article.ingested", _aid(a.url),
            {
                "title": a.title, "source": a.source, "language": a.language,
                "body": a.body, "url": a.url, "image_url": a.image,
                "backfill": True, "provider": "cc-news", "seendate": a.seendate,
            },
        )
        new += 1
    await nc.flush()
    await nc.close()
    print(f"[ccnews] backfill done: {new} ingested, {dropped} dropped by the gate/denylist", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
