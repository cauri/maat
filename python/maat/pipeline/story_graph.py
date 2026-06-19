"""Story graph (P4 §#42/#43/#44) — event-nodes, typed edges, claim↔node mapping, novelty.

Decisions from docs/spikes/claim-node-attachment.md:
  - EventNode = named real-world occurrence with an entity spine + embedding centroid.
  - Attachment gate = entity Jaccard ≥ τ_entity AND temporal proximity ≤ window_s (AND-gate).
  - Semantic similarity (cosine) is a tiebreaker only when multiple nodes pass the gate.
  - Edge types are inferred lazily from node lifecycle signals; no LLM.
    · develops  — later cluster on same node, centroid similarity ≥ 0.72
    · spawns    — cluster attaches to two nodes whose centroids have diverged (cosine < 0.60)
    · merges    — two nodes converge on the same cluster (cosine ≥ 0.78); older node is source
  - Many-to-many: cluster → [node_id, …]; node → [cluster_id, …]; both append-only.
  - Novelty = per-user, cluster-level, 1.0 (novel) / 0.0 (seen), with 30-day decay window.

All functions are pure (rows/state in → graph out). No I/O, no migrations, no LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from maat import ids


# ---------------------------------------------------------------------------
# Public data shapes
# ---------------------------------------------------------------------------


@dataclass
class ClusterRow:
    """Minimal projection of a corroboration cluster needed by the story graph.

    The caller constructs this from DB rows or from a ``Corroboration`` object.
    No DB I/O here — pure data-in/graph-out.
    """

    cluster_id: str
    entity_spine: list[str]       # canonical entity ids (persons, orgs, places)
    topic_embedding: list[float]  # centroid of member claim embeddings
    earliest_ts: float            # Unix timestamp of the earliest claim in the cluster
    claim_ids: list[str] = field(default_factory=list)


@dataclass
class EventNode:
    """A persistent real-world occurrence in the story graph.

    The ``id`` is content-addressed from the entity spine + first-cluster id so it is
    reproducible from the same input (spike §1).

    ``topic_embedding`` is a running centroid over all attached clusters; callers should
    update it via ``update_centroid`` after each attachment.
    ``cluster_count`` is carried alongside the node so edge-inference has it cheaply.
    """

    id: str
    headline: str                 # shortest corroborated fact that names the event
    entity_spine: list[str]       # join key — canonical entity ids
    topic_embedding: list[float]  # running centroid of member cluster embeddings
    first_seen: float             # Unix timestamp; earliest claim in the seeding cluster
    last_updated: float           # Unix timestamp; most recent member cluster's earliest_ts
    cluster_count: int = 0        # number of clusters attached so far


@dataclass
class GraphEdge:
    """A typed directed edge between graph nodes or between a node and a cluster."""

    kind: str      # "develops" | "spawns" | "merges"
    from_id: str   # node_id (develops/spawns/merges)
    to_id: str     # cluster_id (develops) | node_id (spawns/merges)


@dataclass
class AttachmentResult:
    """What happened when a cluster was processed by the story graph fold."""

    cluster_id: str
    node_id: str           # the node the cluster attaches to (new or existing)
    is_new_node: bool
    new_node: EventNode | None = None   # populated only when is_new_node=True
    edges: list[GraphEdge] = field(default_factory=list)


class ClaimNodeLink(NamedTuple):
    """One entry in the claim↔node many-to-many join table (#43)."""

    claim_id: str
    node_id: str
    cluster_id: str   # the cluster that carried the claim to this node


@dataclass
class StoryGraph:
    """The complete graph produced by folding a sequence of clusters.

    Immutable in the sense that callers receive a new graph rather than mutating
    an existing one — use ``fold_clusters`` to build it.

    Attributes
    ----------
    nodes:
        Dict of node_id → EventNode.
    edges:
        All typed directed edges inferred during the fold.
    cluster_nodes:
        cluster_id → list of node_ids (a cluster can attach to multiple nodes).
    node_clusters:
        node_id → list of cluster_ids (a node accumulates clusters over its lifetime).
    claim_node_links:
        The fully expanded claim↔node join table (from ClusterRow.claim_ids).
    """

    nodes: dict[str, EventNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)
    cluster_nodes: dict[str, list[str]] = field(default_factory=dict)
    node_clusters: dict[str, list[str]] = field(default_factory=dict)
    claim_node_links: list[ClaimNodeLink] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Thresholds (from spike §2 / §3; surfaced as Config entries in P8)
# ---------------------------------------------------------------------------

_DEFAULT_ENTITY_TAU: float = 0.40    # Jaccard threshold for entity spine overlap
_DEFAULT_WINDOW_S: float = 72 * 3600  # 72-hour temporal window (seconds)

_DEVELOPS_COSINE_TAU: float = 0.72   # centroid similarity floor for a develops edge
_SPAWNS_DIVERGE_TAU: float = 0.60    # cosine BELOW this → stories have diverged → spawns
_MERGES_COSINE_TAU: float = 0.78     # cosine AT OR ABOVE this → nodes converging → merges

_DEFAULT_NOVELTY_DECAY_S: float = 30 * 24 * 3600  # 30-day decay window (seconds)


# ---------------------------------------------------------------------------
# Internal maths
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors; 0.0 on empty or mismatched length."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def entity_jaccard(a: list[str], b: list[str]) -> float:
    """Jaccard overlap between two entity spine lists."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _update_centroid(old: list[float], new: list[float], n: int) -> list[float]:
    """Online running centroid: incorporate the n-th vector (1-indexed)."""
    if not old:
        return list(new)
    return [(o * (n - 1) + v) / n for o, v in zip(old, new)]


def _node_id_for(cluster: ClusterRow) -> str:
    """Stable content-addressed id: entity spine (sorted) + cluster id (spike §1)."""
    return ids.node_id(cluster.entity_spine, cluster.cluster_id)


# ---------------------------------------------------------------------------
# Attachment gate
# ---------------------------------------------------------------------------


def passes_gate(
    cluster: ClusterRow,
    node: EventNode,
    *,
    entity_tau: float = _DEFAULT_ENTITY_TAU,
    window_s: float = _DEFAULT_WINDOW_S,
) -> bool:
    """Return True iff ``cluster`` satisfies the two-signal attachment gate for ``node``.

    Both signals are required (AND, not OR):
    1. Entity Jaccard >= entity_tau, with at least one entity in common.
    2. Cluster's earliest timestamp is within window_s of node.last_updated.
    """
    if not cluster.entity_spine or not node.entity_spine:
        return False
    j = entity_jaccard(cluster.entity_spine, node.entity_spine)
    if j < entity_tau:
        return False
    if not set(cluster.entity_spine) & set(node.entity_spine):
        return False
    dt = abs(cluster.earliest_ts - node.last_updated)
    return dt <= window_s


# ---------------------------------------------------------------------------
# Edge inference (pure functions of node state + incoming cluster signals)
# ---------------------------------------------------------------------------


def _infer_edges(
    cluster: ClusterRow,
    target_node: EventNode,
    all_nodes: list[EventNode],
    *,
    entity_tau: float = _DEFAULT_ENTITY_TAU,
    window_s: float = _DEFAULT_WINDOW_S,
) -> list[GraphEdge]:
    """Infer develops / spawns / merges edges triggered by a cluster attachment.

    ``target_node.cluster_count`` must reflect the count BEFORE this attachment.
    """
    edges: list[GraphEdge] = []

    # develops: a second-or-later cluster lands on this node with good centroid similarity
    if target_node.cluster_count >= 1:
        sim = _cosine(cluster.topic_embedding, target_node.topic_embedding)
        if sim >= _DEVELOPS_COSINE_TAU:
            edges.append(GraphEdge(
                kind="develops",
                from_id=target_node.id,
                to_id=cluster.cluster_id,
            ))

    for other in all_nodes:
        if other.id == target_node.id:
            continue

        other_passes = passes_gate(cluster, other, entity_tau=entity_tau, window_s=window_s)

        # spawns: cluster ALSO passes the gate for another node whose centroid has diverged
        if other_passes:
            diverged = _cosine(target_node.topic_embedding, other.topic_embedding) < _SPAWNS_DIVERGE_TAU
            if diverged:
                edges.append(GraphEdge(
                    kind="spawns",
                    from_id=target_node.id,
                    to_id=other.id,
                ))

        # merges: two nodes converge on the same cluster — older node is the edge source
        if other_passes:
            converged = _cosine(target_node.topic_embedding, other.topic_embedding) >= _MERGES_COSINE_TAU
            if converged:
                older = target_node if target_node.first_seen <= other.first_seen else other
                newer = other if older is target_node else target_node
                edges.append(GraphEdge(
                    kind="merges",
                    from_id=older.id,
                    to_id=newer.id,
                ))

    return edges


# ---------------------------------------------------------------------------
# Single-cluster attachment (building block of the fold)
# ---------------------------------------------------------------------------


def attach_cluster(
    cluster: ClusterRow,
    graph: StoryGraph,
    *,
    entity_tau: float = _DEFAULT_ENTITY_TAU,
    window_s: float = _DEFAULT_WINDOW_S,
) -> AttachmentResult:
    """Attach ``cluster`` to the best-matching node, or create a new one.

    Returns an ``AttachmentResult``; does NOT mutate ``graph`` — the caller applies the
    result (or use ``fold_clusters`` to drive the whole pipeline).
    """
    existing = list(graph.nodes.values())
    candidates = [n for n in existing if passes_gate(cluster, n, entity_tau=entity_tau, window_s=window_s)]

    if not candidates:
        # no match -> seed a new node
        new_id = _node_id_for(cluster)
        headline = cluster.claim_ids[0] if cluster.claim_ids else cluster.cluster_id
        new_node = EventNode(
            id=new_id,
            headline=headline,
            entity_spine=list(cluster.entity_spine),
            topic_embedding=list(cluster.topic_embedding),
            first_seen=cluster.earliest_ts,
            last_updated=cluster.earliest_ts,
            cluster_count=0,
        )
        return AttachmentResult(
            cluster_id=cluster.cluster_id,
            node_id=new_id,
            is_new_node=True,
            new_node=new_node,
        )

    # tiebreaker: highest cosine to the node's running centroid
    target = max(candidates, key=lambda n: _cosine(cluster.topic_embedding, n.topic_embedding))
    edges = _infer_edges(cluster, target, existing, entity_tau=entity_tau, window_s=window_s)
    return AttachmentResult(
        cluster_id=cluster.cluster_id,
        node_id=target.id,
        is_new_node=False,
        edges=edges,
    )


# ---------------------------------------------------------------------------
# Apply an attachment result back into the graph (mutable step of the fold)
# ---------------------------------------------------------------------------


def _apply(graph: StoryGraph, cluster: ClusterRow, result: AttachmentResult) -> None:
    """Mutate ``graph`` in-place to record the attachment described by ``result``."""
    node_id = result.node_id

    if result.is_new_node:
        assert result.new_node is not None
        node = result.new_node
        graph.nodes[node_id] = node
    else:
        node = graph.nodes[node_id]

    # update node centroid + metadata
    node.cluster_count += 1
    node.topic_embedding = _update_centroid(
        node.topic_embedding, cluster.topic_embedding, node.cluster_count
    )
    node.last_updated = max(node.last_updated, cluster.earliest_ts)

    # cluster->nodes mapping (many-to-many)
    graph.cluster_nodes.setdefault(cluster.cluster_id, [])
    if node_id not in graph.cluster_nodes[cluster.cluster_id]:
        graph.cluster_nodes[cluster.cluster_id].append(node_id)

    # node->clusters mapping (many-to-many)
    graph.node_clusters.setdefault(node_id, [])
    if cluster.cluster_id not in graph.node_clusters[node_id]:
        graph.node_clusters[node_id].append(cluster.cluster_id)

    # claim<->node links (#43)
    for claim_id in cluster.claim_ids:
        graph.claim_node_links.append(ClaimNodeLink(
            claim_id=claim_id,
            node_id=node_id,
            cluster_id=cluster.cluster_id,
        ))

    # edges
    graph.edges.extend(result.edges)


# ---------------------------------------------------------------------------
# Pure fold: list[ClusterRow] -> StoryGraph  (#42 + #43)
# ---------------------------------------------------------------------------


def fold_clusters(
    clusters: list[ClusterRow],
    *,
    entity_tau: float = _DEFAULT_ENTITY_TAU,
    window_s: float = _DEFAULT_WINDOW_S,
) -> StoryGraph:
    """Build a StoryGraph by folding a sequence of corroboration clusters.

    Pure fold: clusters are processed in the order given (callers should sort by
    ``earliest_ts`` when temporal ordering matters).  No I/O, no LLM calls.

    Returns a ``StoryGraph`` whose ``nodes``, ``edges``, ``cluster_nodes``,
    ``node_clusters``, and ``claim_node_links`` are all populated.
    """
    graph = StoryGraph()
    for cluster in clusters:
        result = attach_cluster(cluster, graph, entity_tau=entity_tau, window_s=window_s)
        _apply(graph, cluster, result)
    return graph


def fold_incremental(
    existing_nodes: list[EventNode],
    new_clusters: list[ClusterRow],
    *,
    entity_tau: float = _DEFAULT_ENTITY_TAU,
    window_s: float = _DEFAULT_WINDOW_S,
) -> tuple[StoryGraph, set[str], set[str]]:
    """Attach only the NEW clusters onto the EXISTING graph (the scalable steady state, #42).

    Re-folding the whole corpus each tick can't scale — the snapshot outgrows NATS's payload cap,
    re-embeds every fact, and is O(corpus). Instead we seed the graph with the nodes already built,
    fold only the new clusters onto them, and emit the difference.

    The graph is seeded with ``existing_nodes`` (so a new cluster can thread onto a story already in
    flight) but with EMPTY edge/mapping collections, so after the fold ``graph.edges`` /
    ``node_clusters`` / ``claim_node_links`` hold ONLY the new contributions — exactly the delta to
    persist. New clusters are folded in ``earliest_ts`` order so develops/spawns/merges edges point
    the right way. Pure: no I/O, no LLM.

    Returns ``(graph, touched_node_ids, created_node_ids)`` — every node a new cluster landed on
    (whose updated state must be upserted), and the subset that were freshly created.
    """
    graph = StoryGraph(nodes={n.id: n for n in existing_nodes})
    touched: set[str] = set()
    created: set[str] = set()
    for cluster in sorted(new_clusters, key=lambda r: r.earliest_ts):
        result = attach_cluster(cluster, graph, entity_tau=entity_tau, window_s=window_s)
        if result.is_new_node:
            created.add(result.node_id)
        _apply(graph, cluster, result)
        touched.add(result.node_id)
    return graph, touched, created


# ---------------------------------------------------------------------------
# Claim<->node query helpers (#43)
# ---------------------------------------------------------------------------


def nodes_for_claim(claim_id: str, graph: StoryGraph) -> list[str]:
    """Return all node_ids that the given claim is attached to."""
    return [link.node_id for link in graph.claim_node_links if link.claim_id == claim_id]


def claims_for_node(node_id: str, graph: StoryGraph) -> list[str]:
    """Return all claim_ids attached to the given node."""
    return [link.claim_id for link in graph.claim_node_links if link.node_id == node_id]


# ---------------------------------------------------------------------------
# Novelty (#44)
# ---------------------------------------------------------------------------


def cluster_novelty(
    cluster_id: str,
    user_id: str,
    seen_set: dict[tuple[str, str], float],
    now_ts: float,
    *,
    decay_s: float = _DEFAULT_NOVELTY_DECAY_S,
) -> float:
    """Return 1.0 (novel) or 0.0 (seen) for a (user, cluster) pair.

    ``seen_set`` maps (user_id, cluster_id) -> seen_at Unix timestamp.
    A cluster seen more than ``decay_s`` seconds ago is treated as novel again.
    This is a pure function: no DB, no LLM, no randomness.
    """
    seen_at = seen_set.get((user_id, cluster_id))
    if seen_at is None:
        return 1.0
    return 1.0 if (now_ts - seen_at) > decay_s else 0.0


def annotate_feed(
    cluster_ids: list[str],
    user_id: str,
    seen_set: dict[tuple[str, str], float],
    now_ts: float,
    *,
    decay_s: float = _DEFAULT_NOVELTY_DECAY_S,
) -> list[dict]:
    """Annotate a list of cluster ids with novelty scores for a given user.

    Returns a list of dicts with keys ``cluster_id`` and ``novelty`` (1.0 or 0.0).
    Ordered: novel clusters first, then seen (stable within each tier).
    Pure function: same inputs always produce the same output.
    """
    annotated = [
        {
            "cluster_id": cid,
            "novelty": cluster_novelty(cid, user_id, seen_set, now_ts, decay_s=decay_s),
        }
        for cid in cluster_ids
    ]
    annotated.sort(key=lambda x: -x["novelty"])
    return annotated
