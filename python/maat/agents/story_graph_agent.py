"""Story-graph builder (#42/#43/#44, P4) — the missing consumer of `cluster.corroborated`.

Folds the live corroboration clusters into event-nodes joined by typed develops/spawns/merges
edges, with a many-to-many claim↔node map, and emits ONE `story.graph.rebuilt` event the kernel
projects into story_nodes / story_edges / story_node_clusters / claim_node_links — so the feed can
return THREADED stories instead of flat clusters.

Entity spine: deterministic proper-noun heuristic by default (no LLM, no cost). Set
MAAT_STORY_GRAPH_LLM=1 to use the DRAFT LLM extractor (maat.pipeline.story_graph_ner) instead.

Run: uv run python -m maat.agents.story_graph_agent  (scheduled after corroborate in the clock).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat.bus import connect
from maat.events import STORY_GRAPH_REBUILT, publish
from maat.pipeline.story_graph_build import build_graph, entity_spine_heuristic, graph_payload
from maat.providers.seam import mistral_embed

ROOT = Path(__file__).resolve().parents[3]


def _jload(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def _entity_fn():
    """The entity extractor: the DRAFT LLM NER when MAAT_STORY_GRAPH_LLM=1, else the heuristic."""
    if os.environ.get("MAAT_STORY_GRAPH_LLM") == "1":
        from maat.pipeline.story_graph_ner import llm_entity_spine  # DRAFT — review with cauri

        return llm_entity_spine
    return entity_spine_heuristic


async def main() -> None:
    load_dotenv(ROOT / ".env")
    tenant = os.environ.get("MAAT_TENANT_ID", "cauri")
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    crows = await pool.fetch("select id, fact, claim_ids from clusters where tenant_id = $1", tenant)
    claims = await pool.fetch("select id, text, article_id from claims")
    arts = await pool.fetch("select id, extract(epoch from ingested_at) ts from articles")
    await pool.close()

    clusters = [{"id": r["id"], "fact": r["fact"], "claim_ids": _jload(r["claim_ids"])} for r in crows]
    if not clusters:
        print("story-graph: no clusters to fold")
        return
    claim_text = {str(r["id"]): r["text"] for r in claims}
    claim_article = {str(r["id"]): str(r["article_id"]) for r in claims}
    art_ts = {str(r["id"]): float(r["ts"] or 0.0) for r in arts}

    embeddings = mistral_embed([c["fact"] or "" for c in clusters])  # batch — one embed call
    graph = build_graph(clusters, claim_text, claim_article, art_ts, embeddings, entity_fn=_entity_fn())

    nc = await connect()
    await publish(nc, STORY_GRAPH_REBUILT, f"story-graph:{tenant}", graph_payload(graph), tenant)
    await nc.flush()
    await nc.close()
    print(
        f"story-graph: {len(graph.nodes)} node(s), {len(graph.edges)} edge(s) "
        f"from {len(clusters)} cluster(s)"
    )


if __name__ == "__main__":
    asyncio.run(main())
