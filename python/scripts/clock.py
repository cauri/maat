"""Ingestion clock (#34) — one incremental tick: acquire NEW articles for the tracked topics.

Skips URLs already ingested (incremental deltas), so it's safe to run on a schedule (cron /
systemd timer). Deliberately a single pass — cadence, and therefore cost, are the operator's
choice, not a hardcoded daemon. The full pipeline (kernel + agents) must be running wherever
the tick runs, so the freshly-ingested articles get extracted, classified, and corroborated.

Topics: CLI args, else MAAT_TOPICS (comma-separated), else config/topics.txt (one per line).
Run: uv run python scripts/clock.py ["topic" ...]   (or `make tick`)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat import prompts
from maat.acquire import apify
from maat.acquire.fetch import fetch_article
from maat.acquire.gdelt import search
from maat.acquire.source_gate import accept_source
from maat.acquire.steer import (
    PER_QUERY_FETCH_BUDGET,
    deepening_plan,
    rank_for_fetch,
    steer_summary,
)
from maat.bus import connect
from maat.clocks import is_paused
from maat.events import publish
from maat.learning.reputation import fold_reputation
from maat.learning.source_learning import learn_preferences
from maat.serving.source_flags import denied_sources
from maat.serving.topics import news_queries

ROOT = Path(__file__).resolve().parents[2]


def _aid(url: str) -> str:
    return "gd-" + hashlib.sha1(url.encode()).hexdigest()[:18]


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
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    clk = await pool.fetch(
        "select data from events where type = 'admin.clock.set' order by id desc limit 20"
    )
    if is_paused([json.loads(r["data"]) if isinstance(r["data"], str) else r["data"] for r in clk]):
        await pool.close()
        print("ingestion clock paused (admin.clock.set) — skipping tick")
        return
    seen = {r["url"] for r in await pool.fetch("select url from articles where url is not null")}
    # Resolve the operator's active prompts (P8) before the pool closes.
    queries_prompt = await prompts.active_text(
        pool, "acquire_queries", prompts.seed_default("acquire_queries")
    )
    gate_prompt = await prompts.active_text(
        pool, "source_gate", prompts.seed_default("source_gate")
    )
    # Domains already in the corpus were previously accepted (news) — the gate trusts them and
    # skips re-classifying, so we only spend an LLM call on genuinely new domains.
    known_good = frozenset(
        (r["source"] or "").lower().removeprefix("www.")
        for r in await pool.fetch("select distinct source from articles where source is not null")
    )
    # Operator allow/deny enforcement (#187): never acquire from a denied source.
    denied = denied_sources(
        (json.loads(r["data"]) if isinstance(r["data"], str) else r["data"])
        for r in await pool.fetch(
            "select data from events where type = 'admin.source.flagged' order by id"
        )
    )
    # Source-learning actuation (#35): fold the corroboration history into per-source reputation,
    # then into capped, diversity-floored acquisition weights. The tick CONSULTS these below to
    # steer fetch toward sources that prove reliable over time — re-rank within a per-query budget
    # + a bounded deepen-top-sources pass. Resilient: missing/empty events → no weights → the
    # cold-start pass-through (acquisition behaves exactly as before steering).
    try:
        corro = await pool.fetch(
            "select data from events where type = 'cluster.corroborated' order by id"
        )
        history = [
            json.loads(r["data"]) if isinstance(r["data"], str) else r["data"] for r in corro
        ]
    except Exception:
        history = []
    prefs = learn_preferences(fold_reputation(history))
    steer_active = bool(prefs.weights)
    await pool.close()

    nc = await connect()
    new = 0
    dropped = 0  # candidates the source gate rejected (not a credible publisher)
    gate_cache: dict = {}  # per-tick domain → verdict, so each new domain is classified once
    use_apify = apify.available() and os.environ.get("MAAT_PRIMARY_PASS", "1") != "0"
    paced = 0  # global GDELT-call counter — GDELT throttles ~1 query/5s, so space ALL calls out
    reranked = 0  # main-loop queries the per-query budget actually narrowed (#35 observability)
    rep_queries: list[str] = []  # one query per topic — seeds the deepen-top-sources pass (#35)

    async def ingest_gdelt(a) -> bool:
        """Gate → fetch body → publish one GDELT candidate. Returns True iff a NEW article was
        published; idempotent on ``seen``. Updates the tick's new/dropped counters. Shared by the
        main acquisition loop and the #35 deepen pass so both apply the identical gate + dedup."""
        nonlocal new, dropped
        if a.url in seen:
            return False
        if a.domain in denied:  # operator-denied source (#187) — never acquire it
            seen.add(a.url)
            dropped += 1
            return False
        # Source gate: only credible publishers become articles (#sources). Judge the
        # domain+headline BEFORE fetching the body, so we don't even pull non-news.
        verdict = await asyncio.to_thread(
            accept_source, a.domain, a.title,
            prompt=gate_prompt, known_good=known_good, cache=gate_cache,
        )
        if not verdict.accept:
            seen.add(a.url)
            dropped += 1
            return False
        body, image_url = await asyncio.to_thread(fetch_article, a.url)
        if not body:
            return False
        await publish(nc, "article.ingested", _aid(a.url),
                      {"title": a.title, "source": a.domain, "language": a.language,
                       "body": body, "url": a.url, "image_url": image_url})
        seen.add(a.url)
        new += 1
        return True

    for topic in topics:
        # Turn the interest into recent-NEWS queries (LLM) — searching the literal interest pulls
        # SEO/blog/listicle junk, not news. Blocking (one LLM call); run off the event loop.
        queries = await asyncio.to_thread(news_queries, topic, prompt=queries_prompt)
        print(f"[{topic}] → {queries}", flush=True)
        if queries:
            rep_queries.append(queries[0])  # representative query for this topic's deepen pass
        got = 0
        for q in queries:
            if paced:  # space successive GDELT calls to dodge its 429 back-off (a blocking sleep)
                await asyncio.sleep(5)
            paced += 1
            try:
                # search()/fetch_article() block (httpx + trafilatura). Run them OFF the event loop
                # — otherwise a multi-second fetch starves the NATS client's flush/ping tasks, the
                # connection drops, and published articles are silently lost (the 83→7 bug).
                arts = await asyncio.to_thread(search, q, maxrecords=15, timespan="1d")
            except Exception as e:  # GDELT down / rate-limited
                print(f"  [{q}] GDELT unavailable: {e}", flush=True)
                arts = []
            # #35 actuation — re-rank within a per-query fetch budget: order the unseen candidates by
            # learned source weight (rank_for_fetch keeps diversity structural — every source present
            # gets a slot before reward priority fills the rest), then fetch bodies for at most
            # `budget` of them. Cold start (no learned weights) → budget None → original order, no cap.
            fresh = [a for a in arts if a.url not in seen]
            ordered = rank_for_fetch(fresh, prefs)
            budget = PER_QUERY_FETCH_BUDGET if steer_active else None
            published_q = 0
            for a in ordered:  # GDELT gives metadata; ingest_gdelt fetches body + lead image (#1)
                if budget is not None and published_q >= budget:
                    reranked += 1  # the budget bit — there were more credible candidates we deferred
                    break
                if await ingest_gdelt(a):
                    published_q += 1
                    got += 1
            # Apify pass per query: its web search surfaces primary/authoritative sources GDELT
            # misses (#108, e.g. the issuer's own release). MAAT_PRIMARY_PASS=0 opts out (credits).
            if use_apify:
                try:
                    items = await asyncio.to_thread(apify.search_and_fetch, q, max_results=5)
                except Exception as e:  # Apify down / out of credit (402) — a paid provider must
                    # NOT abort the tick. Disable it for the rest of this tick (next tick retries,
                    # in case credit is topped up) and keep going on the free GDELT stream.
                    print(f"  [{q}] Apify unavailable, skipping primary pass this tick: {e}", flush=True)
                    use_apify = False
                    items = []
                for fa in items:
                    if fa.url in seen:
                        continue
                    if fa.domain in denied:  # operator-denied source (#187)
                        seen.add(fa.url)
                        dropped += 1
                        continue
                    verdict = await asyncio.to_thread(
                        accept_source, fa.domain, fa.title,
                        prompt=gate_prompt, known_good=known_good, cache=gate_cache,
                    )
                    if not verdict.accept:
                        seen.add(fa.url)
                        dropped += 1
                        continue
                    await publish(nc, "article.ingested", _aid(fa.url),
                                  {"title": fa.title, "source": fa.domain, "language": fa.language,
                                   "body": fa.body, "url": fa.url, "image_url": fa.image})
                    seen.add(fa.url)
                    new += 1
                    got += 1
        print(f"[{topic}] +{got} new", flush=True)

    # #35 deepen pass: give the top proven-reliable sources MORE coverage by re-querying the tracked
    # topics scoped (domain:) to their domains. Bounded (≤ a handful of extra GDELT calls/tick) and
    # paced like the main loop. Skipped on cold start / when no source has earned deepening yet.
    deepened = 0
    plan = deepening_plan(prefs, rep_queries) if steer_active else []
    for src, dq in plan:
        if paced:  # keep pacing GDELT's ~1 query/5s throttle across the deepen calls too
            await asyncio.sleep(5)
        paced += 1
        try:
            arts = await asyncio.to_thread(search, dq, maxrecords=15, timespan="1d")
        except Exception as e:  # GDELT down / rate-limited — skip this deepen query, keep going
            print(f"  [deepen {src}] GDELT unavailable: {e}", flush=True)
            arts = []
        fresh = [a for a in arts if a.url not in seen]
        for a in rank_for_fetch(fresh, prefs, budget=PER_QUERY_FETCH_BUDGET):
            if await ingest_gdelt(a):
                deepened += 1
    if plan:
        print(f"deepen: +{deepened} from {sorted({s for s, _ in plan})}", flush=True)

    # Record what the steer did this tick — lands in the append-only events log (the kernel records
    # every type before projecting), so the actuation is observable/auditable (#35 verification).
    if steer_active:
        await publish(
            nc, "acquire.steer", "acquire-steer",
            steer_summary(
                prefs,
                per_query_budget=PER_QUERY_FETCH_BUDGET,
                deepen_plan=plan,
                deepened_articles=deepened,
                reranked_queries=reranked,
            ),
        )

    await nc.flush()
    await nc.close()
    print(
        f"tick: {new} new articles across {len(topics)} topics "
        f"({dropped} dropped by the source gate"
        + (f"; #35 steer: {reranked} queries narrowed, +{deepened} deepened" if steer_active else "")
        + ")",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
