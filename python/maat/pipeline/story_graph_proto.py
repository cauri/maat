"""Story-graph spike prototype (issue #45).

Pure-function reference implementation of the attachment gate and edge-inference signals
decided in docs/spikes/claim-node-attachment.md.  Nothing in the production pipeline imports
this module yet — it exists so #42/#43 have a concrete, tested starting point.

No I/O, no migrations, no LLM calls.  All functions are deterministic and trivially testable.

DECISIONS (see the spike doc for full rationale):
  - Attachment gate = entity Jaccard >= tau_entity AND temporal proximity <= window_seconds.
  - Semantic similarity (embedding cosine) is a tiebreaker when multiple nodes pass the gate.
  - Edge inference is lazy from lifecycle signals; no LLM.
  - Novelty is per-user, cluster-level, with a decay window.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data shapes (stubs — #42 will replace with real dataclasses / DB rows)
# ---------------------------------------------------------------------------


@dataclass
class ClusterSignal:
    """What the attachment gate needs to know about one corroboration cluster."""

    cluster_id: str
    entity_spine: list[str]      # canonical entity ids (persons, orgs, places)
    topic_embedding: list[float]  # centroid of member claim embeddings
    earliest_ts: float            # Unix timestamp of the earliest claim in the cluster


@dataclass
class EventNode:
    """A persistent real-world occurrence in the story graph."""

    node_id: str
    entity_spine: list[str]
    topic_embedding: list[float]
    first_seen: float             # Unix timestamp
    last_updated: float           # Unix timestamp — rolling, updated on each attachment


@dataclass
class GraphEdge:
    kind: str   # "develops" | "spawns" | "merges"
    from_id: str
    to_id: str


@dataclass
class AttachmentResult:
    node_id: str          # the node the cluster attaches to (new or existing)
    is_new_node: bool
    edges: list[GraphEdge] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cosine similarity (mirrors corroborate._cosine — kept local to stay standalone)
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------------------
# Entity Jaccard
# ---------------------------------------------------------------------------


def entity_jaccard(a: list[str], b: list[str]) -> float:
    """Jaccard overlap between two entity spine lists."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ---------------------------------------------------------------------------
# Attachment gate
# ---------------------------------------------------------------------------

_DEFAULT_ENTITY_TAU = 0.40   # §2 recommendation; surface as Config entry in P8
_DEFAULT_WINDOW_S = 72 * 3600  # 72 hours in seconds; surface as Config entry in P8
_DEVELOPS_COSINE_TAU = 0.72
_SPAWNS_DIVERGE_TAU = 0.60   # cosine BELOW this => stories have diverged
_MERGES_COSINE_TAU = 0.78


def passes_gate(
    cluster: ClusterSignal,
    node: EventNode,
    *,
    entity_tau: float = _DEFAULT_ENTITY_TAU,
    window_s: float = _DEFAULT_WINDOW_S,
) -> bool:
    """Return True iff `cluster` satisfies the two-signal attachment gate for `node`.

    Both signals are required (AND, not OR):
      1. Entity Jaccard >= entity_tau, with at least one named entity in common.
      2. Cluster's earliest timestamp is within window_s of node.last_updated.
    """
    if not cluster.entity_spine or not node.entity_spine:
        return False
    j = entity_jaccard(cluster.entity_spine, node.entity_spine)
    if j < entity_tau:
        return False
    # at least one common named entity (both spines share at least one element)
    if not set(cluster.entity_spine) & set(node.entity_spine):
        return False
    dt = abs(cluster.earliest_ts - node.last_updated)
    return dt <= window_s


def _running_centroid(old: list[float], new: list[float], n: int) -> list[float]:
    """Online update: centroid of (n-1) old vectors + 1 new vector."""
    if not old:
        return list(new)
    return [(o * (n - 1) + v) / n for o, v in zip(old, new)]


def _infer_edges(
    cluster: ClusterSignal,
    target_node: EventNode,
    all_nodes: list[EventNode],
    cluster_count_on_target: int,
) -> list[GraphEdge]:
    """Infer develops / spawns / merges edges triggered by a cluster attachment.

    Pure function: returns the edges implied by this attachment; caller persists them.

    `cluster_count_on_target` is the count of clusters already on `target_node` BEFORE
    this attachment (used to decide whether a `develops` edge is warranted).
    """
    edges: list[GraphEdge] = []

    # develops: a second-or-later cluster lands on this node with good centroid similarity
    if cluster_count_on_target >= 1:
        sim = _cosine(cluster.topic_embedding, target_node.topic_embedding)
        if sim >= _DEVELOPS_COSINE_TAU:
            # edge from the node (standing in for the previous cluster set) to this cluster
            edges.append(GraphEdge(kind="develops", from_id=target_node.node_id, to_id=cluster.cluster_id))

    # spawns: this cluster ALSO passes the gate for another existing node whose centroid has diverged
    for other in all_nodes:
        if other.node_id == target_node.node_id:
            continue
        diverged = _cosine(target_node.topic_embedding, other.topic_embedding) < _SPAWNS_DIVERGE_TAU
        if diverged and passes_gate(cluster, other):
            edges.append(GraphEdge(kind="spawns", from_id=target_node.node_id, to_id=other.node_id))

    # merges: two nodes (target and another) now pass the gate for the same cluster AND converge
    for other in all_nodes:
        if other.node_id == target_node.node_id:
            continue
        converged = _cosine(target_node.topic_embedding, other.topic_embedding) >= _MERGES_COSINE_TAU
        other_passes = passes_gate(cluster, other)
        if converged and other_passes:
            # older node is the source of the merges edge
            older = target_node if target_node.first_seen <= other.first_seen else other
            newer = other if older is target_node else target_node
            edges.append(GraphEdge(kind="merges", from_id=older.node_id, to_id=newer.node_id))

    return edges


def attach_cluster(
    cluster: ClusterSignal,
    existing_nodes: list[EventNode],
    cluster_counts: dict[str, int],
    *,
    entity_tau: float = _DEFAULT_ENTITY_TAU,
    window_s: float = _DEFAULT_WINDOW_S,
) -> AttachmentResult:
    """Attach `cluster` to the best-matching existing node, or create a new one.

    `cluster_counts` maps node_id -> number of clusters already attached to that node.

    Returns an AttachmentResult with the target node_id, whether it is new, and any
    inferred edges.  The caller is responsible for persisting the attachment event and
    updating node state (topic_embedding, last_updated).
    """
    candidates = [n for n in existing_nodes if passes_gate(cluster, n, entity_tau=entity_tau, window_s=window_s)]

    if not candidates:
        # no match — seed a new node
        new_node_id = f"node:{cluster.cluster_id}"
        return AttachmentResult(node_id=new_node_id, is_new_node=True)

    # tiebreaker: highest embedding cosine to node centroid
    target = max(candidates, key=lambda n: _cosine(cluster.topic_embedding, n.topic_embedding))
    count = cluster_counts.get(target.node_id, 0)
    edges = _infer_edges(cluster, target, existing_nodes, count)
    return AttachmentResult(node_id=target.node_id, is_new_node=False, edges=edges)


# ---------------------------------------------------------------------------
# Novelty gate
# ---------------------------------------------------------------------------

_DEFAULT_NOVELTY_DECAY_S = 30 * 24 * 3600  # 30 days


def cluster_novelty(
    cluster_id: str,
    user_id: str,
    seen_set: dict[tuple[str, str], float],
    now_ts: float,
    *,
    decay_s: float = _DEFAULT_NOVELTY_DECAY_S,
) -> float:
    """Return 1.0 (novel) or 0.0 (seen) for a (user, cluster) pair.

    `seen_set` maps (user_id, cluster_id) -> seen_at timestamp.
    A cluster seen more than `decay_s` seconds ago is treated as novel again.
    """
    seen_at = seen_set.get((user_id, cluster_id))
    if seen_at is None:
        return 1.0
    return 1.0 if (now_ts - seen_at) > decay_s else 0.0
