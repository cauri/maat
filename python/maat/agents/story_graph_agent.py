"""Story-graph builder (#42/#43/#44, P4) — the consumer of `cluster.corroborated`.

Threads the live corroboration clusters into event-nodes joined by typed develops/spawns/merges
edges, with a many-to-many claim↔node map, projected into story_nodes / story_edges /
story_node_clusters / claim_node_links — so the feed can return THREADED stories instead of flat
clusters.

INCREMENTAL by default (#42 at scale): each tick we fold only the NEW clusters (those not yet
threaded) onto the existing graph, embed only those, and emit the difference as `story.graph.delta`
events chunked under NATS's 1 MB payload cap. The old whole-graph `story.graph.rebuilt` snapshot
outgrew that cap once the corpus passed ~1k clusters and silently stopped landing — re-folding,
re-embedding, and re-shipping the entire corpus every tick never scaled. MAAT_STORY_GRAPH_RESET=1
forces a clean full re-fold (still streamed in chunks, with a truncate on the first one).

Entity spine: deterministic proper-noun heuristic by default (no LLM, no cost). Set
MAAT_STORY_GRAPH_LLM=1 to use the DRAFT LLM extractor (maat.pipeline.story_graph_ner) instead.

Run: uv run python -m maat.agents.story_graph_agent  (scheduled after corroborate in the clock).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat.bus import connect
from maat.events import STORY_GRAPH_DELTA, publish
from maat.pipeline.story_graph import _DEFAULT_WINDOW_S, EventNode  # window mirrors the attach gate
from maat.pipeline.story_graph_build import (
    build_graph_incremental,
    chunk_delta,
    delta_payload,
    entity_spine_heuristic,
)
from maat.providers.seam import mistral_embed

ROOT = Path(__file__).resolve().parents[3]


def _jload(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


# The DRAFT LLM NER (one Sonnet call per cluster) is gated behind MAAT_STORY_GRAPH_LLM=1. Run those
# calls CONCURRENTLY with a per-call timeout + heuristic fallback (#42): 245 sequential calls is
# ~7 min and never completed within a tick (the rebuild silently never emitted — 0 story.graph.rebuilt
# events ever), so bounded concurrency brings it under a minute and the timeout guarantees one slow or
# hung call can't stall the whole rebuild.
_NER_CONCURRENCY = 8
_NER_TIMEOUT_S = 25.0


async def _llm_entity_spines(
    texts: list[str], *, concurrency: int = _NER_CONCURRENCY, timeout: float = _NER_TIMEOUT_S
) -> dict[str, list[str]]:
    """Extract the LLM entity spine for each text concurrently (bounded), each call timeout-guarded
    with a deterministic heuristic fallback, so the DRAFT LLM path completes fast and can never stall
    the story-graph rebuild. Returns ``{text: entity_spine}``; only the provider seam does I/O."""
    from maat.pipeline.story_graph_ner import llm_entity_spine  # DRAFT — review with cauri

    sem = asyncio.Semaphore(concurrency)
    uniq = list(dict.fromkeys(texts))  # dedupe identical spine texts; preserve order

    async def one(text: str) -> list[str]:
        async with sem:
            try:
                return await asyncio.wait_for(asyncio.to_thread(llm_entity_spine, text), timeout)
            except Exception:  # noqa: BLE001 — timeout or provider error → deterministic fallback
                return entity_spine_heuristic(text)

    spines = await asyncio.gather(*(one(t) for t in uniq))
    return dict(zip(uniq, spines))


def _rehydrate_node(r) -> EventNode:
    """Rebuild an EventNode from a story_nodes row (jsonb columns arrive as text — parse them)."""
    return EventNode(
        id=r["id"],
        headline=r["headline"] or "",
        entity_spine=_jload(r["entity_spine"]),
        topic_embedding=[float(x) for x in _jload(r["topic_embedding"])],
        first_seen=float(r["first_seen"] or 0.0),
        last_updated=float(r["last_updated"] or 0.0),
        cluster_count=int(r["cluster_count"] or 0),
    )


async def main() -> None:
    load_dotenv(ROOT / ".env")
    tenant = os.environ.get("MAAT_TENANT_ID", "cauri")
    reset = os.environ.get("MAAT_STORY_GRAPH_RESET") == "1"
    window_s = _DEFAULT_WINDOW_S
    pool = await get_pool()
    crows = await pool.fetch("select id, fact, claim_ids from clusters where tenant_id = $1", tenant)
    claims = await pool.fetch("select id, text, article_id from claims")
    arts = await pool.fetch("select id, extract(epoch from ingested_at) ts from articles")

    clusters_all = [{"id": r["id"], "fact": r["fact"], "claim_ids": _jload(r["claim_ids"])} for r in crows]
    if not clusters_all:
        await pool.close()
        print("story-graph: no clusters to fold")
        return
    claim_text = {str(r["id"]): r["text"] for r in claims}
    claim_article = {str(r["id"]): str(r["article_id"]) for r in claims}
    art_ts = {str(r["id"]): float(r["ts"] or 0.0) for r in arts}

    def cl_earliest(c: dict) -> float:
        ts = [art_ts.get(claim_article.get(str(cid), ""), 0.0) for cid in c["claim_ids"]]
        return min([t for t in ts if t] or [0.0])

    # INCREMENTAL: fold only the clusters not yet threaded, onto the nodes that could still accept
    # them (last_updated within the attach window of the oldest new cluster). RESET: re-fold the lot.
    if reset:
        target = clusters_all
        existing_nodes: list[EventNode] = []
    else:
        noded = {
            r["cluster_id"]
            for r in await pool.fetch(
                "select cluster_id from story_node_clusters where tenant_id = $1", tenant
            )
        }
        target = [c for c in clusters_all if c["id"] not in noded]
        if not target:
            await pool.close()
            print("story-graph: up to date — no new clusters")
            return
        horizon = min(cl_earliest(c) for c in target) - window_s
        node_rows = await pool.fetch(
            "select id, headline, entity_spine, topic_embedding, first_seen, last_updated, "
            "cluster_count from story_nodes where tenant_id = $1 and last_updated >= $2",
            tenant,
            horizon,
        )
        existing_nodes = [_rehydrate_node(r) for r in node_rows]
    await pool.close()

    embeddings = mistral_embed([c["fact"] or "" for c in target])  # only the NEW clusters

    # Entity spine: the proper-noun heuristic by default; the DRAFT LLM NER when enabled. When on,
    # precompute each cluster's spine CONCURRENTLY (bounded + timeout-guarded) so it finishes fast —
    # the sequential per-cluster LLM path never completed within a tick (#42).
    if os.environ.get("MAAT_STORY_GRAPH_LLM") == "1":
        spine_texts = [
            " ".join([c.get("fact", ""), *[claim_text.get(str(cid), "") for cid in c.get("claim_ids") or []]])
            for c in target
        ]
        spine_map = await _llm_entity_spines(spine_texts)

        def entity_fn(text: str) -> list[str]:
            return spine_map[text] if text in spine_map else entity_spine_heuristic(text)
    else:
        entity_fn = entity_spine_heuristic

    graph, touched, created = build_graph_incremental(
        existing_nodes, target, claim_text, claim_article, art_ts, embeddings, entity_fn=entity_fn
    )

    # The centroid (~1k floats) rides only for nodes still inside the attach window — settled nodes
    # leave it null and never re-enter the candidate set.
    latest_ts = max((graph.nodes[nid].last_updated for nid in touched), default=0.0)
    payload = delta_payload(graph, touched, active_since=latest_ts - window_s)
    chunks = chunk_delta(payload, reset=reset)

    nc = await connect()
    for ch in chunks:
        await publish(nc, STORY_GRAPH_DELTA, f"story-graph:{tenant}", ch, tenant)
    await nc.flush()
    await nc.close()
    print(
        f"story-graph: +{len(created)} new node(s), {len(touched)} touched, {len(graph.edges)} "
        f"edge(s) from {len(target)} cluster(s) in {len(chunks)} chunk(s)" + (" [reset]" if reset else "")
    )


if __name__ == "__main__":
    asyncio.run(main())
