"""Shared acquisition orchestration (#290).

Every acquisition source repeats the same shape around its per-source fetch: load the
already-seen urls + operator deny-list, then for each candidate dedup → deny-filter →
(optionally) LLM source-gate → fetch the body → clean → ``publish(article.ingested)`` →
count. That shape lived copy-pasted across scripts/acquire*.py; it now lives here once.

Per-source specifics (which engine fetches, which extra fields each carries, whether the
source-gate applies) stay in :mod:`maat.acquire.drivers` and :mod:`maat.acquire.<source>`.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maat import ids, prompts
from maat.acquire.clean import clean_article
from maat.acquire.fetch import fetch_article
from maat.acquire.source_gate import accept_source
from maat.events import publish
from maat.serving.source_flags import denied_sources

ADMIN_SOURCE_FLAGGED = "admin.source.flagged"


@dataclass
class AcqState:
    """Pre-loaded acquisition state shared by every candidate in a run (read once, up front)."""

    seen: set[str]
    denied: set[str]
    gate_prompt: str = ""
    known_good: frozenset[str] = field(default_factory=frozenset)


async def load_state(pool: Any, *, with_gate: bool) -> AcqState:
    """Read the seen-url set + operator deny-list (and, when gating, the source-gate prompt +
    the known-good source set) in one pass, so the per-item loop touches no DB."""
    seen = {r["url"] for r in await pool.fetch("select url from articles where url is not null")}
    denied = denied_sources(
        (json.loads(r["data"]) if isinstance(r["data"], str) else r["data"])
        for r in await pool.fetch(
            "select data from events where type = $1 order by id", ADMIN_SOURCE_FLAGGED
        )
    )
    gate_prompt = ""
    known_good: frozenset[str] = frozenset()
    if with_gate:
        gate_prompt = await prompts.active_text(pool, "source_gate", prompts.seed_default("source_gate"))
        known_good = frozenset(
            (r["source"] or "").lower().removeprefix("www.")
            for r in await pool.fetch("select distinct source from articles where source is not null")
        )
    return AcqState(seen=seen, denied=denied, gate_prompt=gate_prompt, known_good=known_good)


class Ingestor:
    """Dedup → deny → (gate) → fetch → clean → publish(article.ingested), counting as it goes.

    One per run; the per-source driver calls :meth:`ingest` for each candidate. ``prefix`` is the
    article-id channel prefix (rss/gd/cc/nd/loc). ``gate`` turns on the LLM source-gate (off for
    pre-vetted channels like the RSS feed list). ``clean`` strips scraped boilerplate from the
    title/body (off for NewsData, whose API bodies arrive clean). State mutation is loop-thread only.
    """

    def __init__(self, nc: Any, state: AcqState, *, prefix: str, gate: bool, clean: bool = True) -> None:
        self.nc = nc
        self.seen = state.seen
        self.denied = state.denied
        self.gate = gate
        self.clean = clean
        self.gate_prompt = state.gate_prompt
        self.known_good = state.known_good
        self.gate_cache: dict[str, Any] = {}
        self.new = 0
        self.dropped = 0
        self.by_lang: Counter[str] = Counter()
        self.by_country: Counter[str] = Counter()
        self._prefix = prefix

    async def ingest(
        self,
        *,
        url: str,
        title: str,
        source: str,
        language: str | None,
        body: str | None,
        image: str | None,
        fields: dict[str, Any] | None = None,
        detect_language: Callable[[str], str | None] | None = None,
    ) -> bool:
        """Ingest one candidate; return True iff an ``article.ingested`` was published.

        ``body`` may be None — the body is fetched here (the GDELT path). ``url`` is claimed in
        ``seen`` up front so concurrent passes never double-fetch. ``detect_language`` overrides
        the language from the body when the source's metadata is unreliable (the Apify floor).
        ``fields`` carries the per-source extras (provider/alignment/country/locale).
        """
        if not url or url in self.seen:
            return False
        self.seen.add(url)
        if source in self.denied:  # operator-denied source (#187)
            self.dropped += 1
            return False
        if self.gate:
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
        if detect_language is not None:
            language = detect_language(body) or language
        if self.clean:
            title, body = clean_article(title, body, source)  # strip scraped boilerplate (#33)
        data: dict[str, Any] = {
            "title": title, "source": source, "language": language,
            "body": body, "url": url, "image_url": image,
        }
        if fields:
            data.update(fields)
        await publish(self.nc, "article.ingested", ids.article_id(url, self._prefix), data)
        self.new += 1
        self.by_lang[language or "?"] += 1
        if data.get("country"):
            self.by_country[str(data["country"])] += 1
        return True


def load_topics(root: Path, override: list[str] | None = None) -> list[str]:
    """Tracked topics: explicit ``override`` → ``MAAT_TOPICS`` env → ``config/topics.txt`` → ["news"]."""
    if override:
        return override
    env = os.environ.get("MAAT_TOPICS")
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    f = root / "config" / "topics.txt"
    if f.exists():
        return [ln.strip() for ln in f.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return ["news"]


def topics_query(topics: list[str]) -> str:
    """OR-of-quoted-phrases query string (GDELT/NewsData match space-separated terms as OR)."""
    return " OR ".join(f'"{t}"' for t in topics) if topics else "news"
