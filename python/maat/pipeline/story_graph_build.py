"""Build story-graph inputs from live corroboration data (#42/#43/#44, P4).

`story_graph.py` is a pure fold over `ClusterRow`s (entity spine + topic-embedding centroid +
timestamp). This module turns the live `clusters`/`claims`/`articles` projections into those
ClusterRows so the story-graph builder agent can thread them into event-nodes + typed edges.

Entity extraction is deterministic by default (a proper-noun heuristic — no I/O, no LLM, fully
testable). An LLM NER can be plugged in via ``entity_fn`` for higher quality; the agent passes a
DRAFT-prompted extractor when an API key is present, else this heuristic.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from maat.pipeline.story_graph import ClusterRow, StoryGraph, fold_clusters

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
