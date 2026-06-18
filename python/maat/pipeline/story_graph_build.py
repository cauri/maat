"""Build story-graph inputs from live corroboration data (#42/#43/#44, P4).

`story_graph.py` is a pure fold over `ClusterRow`s (entity spine + topic-embedding centroid +
timestamp). This module turns the live `clusters`/`claims`/`articles` projections into those
ClusterRows so the story-graph builder agent can thread them into event-nodes + typed edges.

Entity extraction is deterministic by default (a proper-noun heuristic — no I/O, no LLM, fully
testable). An LLM NER can be plugged in via ``entity_fn`` for higher quality; the agent passes a
DRAFT-prompted extractor when an API key is present, else this heuristic.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable

from maat.pipeline.story_graph import (
    ClusterRow,
    EventNode,
    StoryGraph,
    fold_clusters,
    fold_incremental,
)

# A leading sentence-start word we never want as the head of an entity ("The ECB" -> "ECB").
_LEADING_STOP = {
    "the", "a", "an", "this", "that", "these", "those", "it", "he", "she", "they", "we",
    "in", "on", "at", "of", "and", "but", "for", "to", "from", "with", "as", "by", "its",
    "his", "her", "their", "our", "mr", "ms", "mrs", "dr",
}
# Capitalised non-entities that pollute the spine (weekdays, months). Dropped so a stray date
# doesn't dilute the entity Jaccard below the attachment gate.
_NOISE = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}
# Capitalised token runs: "European Central Bank", "Christine Lagarde", "U.S.", "AT&T".
_PROPER = re.compile(r"\b([A-Z][\w.&'’-]*(?:\s+[A-Z][\w.&'’-]*)*)\b")


def entity_spine_heuristic(text: str, *, max_entities: int = 12) -> list[str]:
    """Canonical-ish entity ids from a text: capitalised proper-noun runs, normalised.

    Deterministic, no I/O, no LLM. Strips leading stop-words ("The ECB" -> "ecb"), lowercases,
    de-duplicates while preserving order, and caps the spine so a long story doesn't blow up the
    Jaccard. Good enough to seed story-graph attachment; swap in an LLM NER via ``entity_fn`` for
    precision.
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _PROPER.finditer(text):
        words = m.group(1).split()
        while words and words[0].lower() in _LEADING_STOP:
            words = words[1:]
        if not words:
            continue
        norm = " ".join(words).lower()
        if len(norm) < 3 or norm in seen or norm in _NOISE:
            continue
        seen.add(norm)
        out.append(norm)
        if len(out) >= max_entities:
            break
    return out


def cluster_row(
    cluster_id: str,
    fact: str,
    claim_texts: Iterable[str],
    claim_ids: Iterable[str],
    embedding: Iterable[float],
    earliest_ts: float,
    *,
    entity_fn: Callable[[str], list[str]] = entity_spine_heuristic,
) -> ClusterRow:
    """Assemble one ``ClusterRow`` for the story-graph fold from live cluster data.

    ``embedding`` is the cluster's topic-embedding centroid (the agent computes it via the
    provider seam). The entity spine is extracted from the fact + member claim texts.
    """
    spine_text = " ".join([fact, *list(claim_texts)])
    return ClusterRow(
        cluster_id=cluster_id,
        entity_spine=entity_fn(spine_text),
        topic_embedding=[float(x) for x in embedding],
        earliest_ts=float(earliest_ts),
        claim_ids=[str(c) for c in claim_ids],
    )


def build_graph(
    clusters: list[dict],
    claim_text: dict[str, str],
    claim_article: dict[str, str],
    art_ts: dict[str, float],
    embeddings: Iterable[Iterable[float]],
    *,
    entity_fn: Callable[[str], list[str]] = entity_spine_heuristic,
) -> StoryGraph:
    """Pure: live cluster rows + claim/article lookups + per-cluster embeddings → a folded
    StoryGraph (event-nodes + typed edges + claim↔node links). Each ``clusters`` dict carries
    ``id``, ``fact`` and ``claim_ids`` (a list); ``embeddings`` is parallel to ``clusters``.
    Rows are folded in chronological order so develops/spawns/merges edges point the right way.
    """
    rows: list[ClusterRow] = []
    for c, emb in zip(clusters, embeddings):
        cl_ids = [str(x) for x in (c.get("claim_ids") or [])]
        texts = [claim_text.get(cid, "") for cid in cl_ids]
        ts = [art_ts.get(claim_article.get(cid, ""), 0.0) for cid in cl_ids]
        earliest = min([t for t in ts if t] or [0.0])
        rows.append(
            cluster_row(c["id"], c.get("fact", ""), texts, cl_ids, emb, earliest, entity_fn=entity_fn)
        )
    rows.sort(key=lambda r: r.earliest_ts)
    graph = fold_clusters(rows)
    # The fold keys nodes on the entity spine; ClusterRow carries no fact, so give each node a
    # human headline here = the shortest corroborated fact among its clusters (it names the event).
    fact_by = {c["id"]: (c.get("fact") or "") for c in clusters}
    for nid, node in graph.nodes.items():
        facts = [fact_by[cid] for cid in graph.node_clusters.get(nid, []) if fact_by.get(cid)]
        if facts:
            node.headline = min(facts, key=len)
    return graph


def graph_payload(graph: StoryGraph) -> dict:
    """Serialise a StoryGraph into the ``story.graph.rebuilt`` event payload the kernel projects."""
    return {
        "nodes": [
            {
                "id": n.id,
                "headline": n.headline,
                "entity_spine": n.entity_spine,
                "first_seen": n.first_seen,
                "last_updated": n.last_updated,
                "cluster_count": n.cluster_count,
            }
            for n in graph.nodes.values()
        ],
        "edges": [{"kind": e.kind, "from_id": e.from_id, "to_id": e.to_id} for e in graph.edges],
        "node_clusters": [
            {"node_id": nid, "cluster_id": cid}
            for nid, cids in graph.node_clusters.items()
            for cid in cids
        ],
        "claim_node_links": [
            {"claim_id": lk.claim_id, "node_id": lk.node_id, "cluster_id": lk.cluster_id}
            for lk in graph.claim_node_links
        ],
    }


# ---------------------------------------------------------------------------
# Incremental delta build + emission (#42 at scale — see story_graph.fold_incremental)
# ---------------------------------------------------------------------------

# Well under NATS's 1 MB max_payload; the rest of the budget covers the event envelope + JSON keys.
_DELTA_MAX_BYTES = 700_000


def build_graph_incremental(
    existing_nodes: list[EventNode],
    new_clusters: list[dict],
    claim_text: dict[str, str],
    claim_article: dict[str, str],
    art_ts: dict[str, float],
    embeddings: Iterable[Iterable[float]],
    *,
    entity_fn: Callable[[str], list[str]] = entity_spine_heuristic,
) -> tuple[StoryGraph, set[str], set[str]]:
    """Fold only the NEW clusters onto the already-built graph (the per-tick steady state).

    ``existing_nodes`` are rehydrated from ``story_nodes`` (entity spine + persisted centroid);
    ``new_clusters`` / ``embeddings`` are parallel and cover ONLY the clusters not yet threaded.
    Returns ``(graph, touched, created)`` — the graph holds only the new edges/mappings, ``touched``
    is every node a new cluster landed on, ``created`` the freshly-minted subset. Pure.
    """
    rows: list[ClusterRow] = []
    for c, emb in zip(new_clusters, embeddings):
        cl_ids = [str(x) for x in (c.get("claim_ids") or [])]
        texts = [claim_text.get(cid, "") for cid in cl_ids]
        ts = [art_ts.get(claim_article.get(cid, ""), 0.0) for cid in cl_ids]
        earliest = min([t for t in ts if t] or [0.0])
        rows.append(
            cluster_row(c["id"], c.get("fact", ""), texts, cl_ids, emb, earliest, entity_fn=entity_fn)
        )
    graph, touched, created = fold_incremental(existing_nodes, rows)
    # Headline a NEWLY created node with the shortest fact among its clusters (it names the event);
    # an existing node keeps the headline it was built with (we don't carry its old facts here).
    fact_by = {c["id"]: (c.get("fact") or "") for c in new_clusters}
    for nid in created:
        facts = [fact_by[cid] for cid in graph.node_clusters.get(nid, []) if fact_by.get(cid)]
        if facts:
            graph.nodes[nid].headline = min(facts, key=len)
    return graph, touched, created


def _node_dict(node: EventNode, *, include_centroid: bool) -> dict:
    d = {
        "id": node.id,
        "headline": node.headline,
        "entity_spine": node.entity_spine,
        "first_seen": node.first_seen,
        "last_updated": node.last_updated,
        "cluster_count": node.cluster_count,
    }
    # The centroid is a ~1k-float vector; only carry it while the node is still ACTIVE (could accrue
    # another cluster). Settled nodes leave it null — they'll never be an attachment candidate again.
    if include_centroid and node.topic_embedding:
        d["topic_embedding"] = list(node.topic_embedding)
    return d


def delta_payload(
    graph: StoryGraph, touched: set[str], *, active_since: float = float("-inf")
) -> dict:
    """Serialise the incremental delta: the touched nodes (centroid attached only while still within
    the attach window, ``last_updated >= active_since``) + the new edges / node↔cluster /
    claim↔node rows. The kernel applies these with insert/upsert — no full replace."""
    nodes = [
        _node_dict(graph.nodes[nid], include_centroid=graph.nodes[nid].last_updated >= active_since)
        for nid in touched
    ]
    return {
        "nodes": nodes,
        "edges": [{"kind": e.kind, "from_id": e.from_id, "to_id": e.to_id} for e in graph.edges],
        "node_clusters": [
            {"node_id": nid, "cluster_id": cid}
            for nid in touched
            for cid in graph.node_clusters.get(nid, [])
        ],
        "claim_node_links": [
            {"claim_id": lk.claim_id, "node_id": lk.node_id, "cluster_id": lk.cluster_id}
            for lk in graph.claim_node_links
        ],
    }


def chunk_delta(
    payload: dict, *, reset: bool = False, max_bytes: int = _DELTA_MAX_BYTES
) -> list[dict]:
    """Split a delta into ``story.graph.delta`` chunks each safely under the bus payload cap.

    Items from all four lists are greedily packed across chunks (order is irrelevant — the kernel
    inserts each row independently and idempotently). ``reset`` (truncate-first) rides ONLY on
    chunk 0, so a multi-chunk full rebuild clears once and the remaining chunks append. A reset with
    no rows still yields one (empty) chunk so the truncate happens.
    """
    keys = ("nodes", "edges", "node_clusters", "claim_node_links")
    stream = [(k, item) for k in keys for item in payload.get(k, [])]
    chunks: list[dict] = []
    cur: dict = {k: [] for k in keys}
    size = 0
    for k, item in stream:
        item_bytes = len(json.dumps(item)) + 8
        if size and size + item_bytes > max_bytes:
            chunks.append(cur)
            cur = {k2: [] for k2 in keys}
            size = 0
        cur[k].append(item)
        size += item_bytes
    if size or not chunks:
        chunks.append(cur)
    if reset:
        chunks[0]["reset"] = True
    return chunks
