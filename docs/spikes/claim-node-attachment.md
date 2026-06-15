# Spike: Claim-Node Attachment Mechanism

**Issue:** #45 — Spike: resolve the attachment mechanism  
**Informs:** #42 (event-nodes + typed edges), #43 (claim↔node many-to-many), #44 ('you've seen this')  
**Part of:** #4 (P4 — Story graph)  
**Status:** DECIDED  
**Author:** agent (autonomous, 2026-06-15)

---

## 1. What is an event-node vs a corroboration cluster?

**Corroboration cluster** (already built — `corroborate.py`): a group of claims that assert the
_same atomic fact_, clustered by embedding cosine (§5.4), with originators collapsed (§5.5) and a
single `confidence` read (§5.6). A cluster is the unit of veracity. It answers: _"is this fact
corroborated?"_

**Event-node** (to build in #42): a named, persistent real-world occurrence — an entity in the
story graph. It answers: _"what happened?"_ An event-node is NOT a fact; it is the anchor that
multiple distinct facts (and therefore multiple corroboration clusters) circle around.

**The key distinction:** a single event ("the minister resigned") may generate dozens of
corroboration clusters over days — "the minister resigned on Tuesday", "the resignation followed a
corruption probe", "the minister's deputy takes over", "the prime minister accepted the
resignation". Each is a separately corroborated fact, each maps to the same event-node.

**Recommended shape for an event-node:**

```python
@dataclass
class EventNode:
    id: str                     # stable, content-addressed (see below)
    headline: str               # shortest corroborated fact that names the event
    entity_spine: list[str]     # canonical entity ids (persons, orgs, places) — the join key
    topic_embedding: list[float]  # centroid of member cluster embeddings, for drift detection
    first_seen: str             # ISO-8601, UTC — earliest claim timestamp
    last_updated: str           # ISO-8601, UTC — most recent member cluster
```

The `id` is content-addressed from the entity spine + temporal window (see §3 below).

---

## 2. How a claim/cluster attaches to one or more event-nodes (many-to-many)

**Recommendation: entity spine + temporal window (two-signal gate), with semantic similarity as a
tiebreaker.**

### Why not embedding similarity alone?

Pure semantic similarity fails at Maat's scale and scope:

- A claim about "the ECB rate decision" and one about "the Fed rate decision" will be highly similar
  semantically but are categorically distinct events with different entity spines. Embedding
  similarity alone would merge them.
- Wire syndication produces near-identical embeddings for the same story across languages and
  outlets — which is exactly what `collapse_originators` already handles at the intra-cluster level.
  At the inter-cluster / graph level, this ambiguity returns: two clusters with near-identical
  centroids are either the _same event_ (attach to the same node) or two _very similar events_
  (distinct nodes). Embeddings alone cannot resolve this.

### Why not shared entities alone?

Entity co-occurrence without temporal grounding over-merges: "President X met Minister Y" in January
and "President X arrested Minister Y" in June share an entity spine but are distinct events
separated by six months of development. Pure entity matching would incorrectly put them on the same
node.

### Recommended two-signal gate

A corroboration cluster **C** attaches to an existing event-node **N** if and only if:

1. **Entity overlap** — the canonical entities extracted from **C**'s claims have Jaccard overlap ≥
   τ_entity (recommended starting value: **0.40**) with **N**'s `entity_spine`. At least one
   entity must be a _named_ entity (person, org, or place), not just a topic tag.

2. **Temporal proximity** — the cluster's earliest claim timestamp falls within a rolling window of
   **N**'s `last_updated` (recommended starting window: **72 hours** for breaking stories; this
   should be a configurable parameter that the kernel can surface as a `Config` entry per §P8/§5).
   After the window lapses, a new cluster on the same entity spine opens a new node (a _sequel_
   event) connected by a `develops` edge (see §3).

If both signals pass, attach **C** to **N** and update `N.topic_embedding` as a running centroid.
If no existing node satisfies both, create a new node seeded by **C**.

**Semantic similarity as tiebreaker:** when two nodes both pass the entity + temporal gate (e.g.,
overlapping entity spines, same window), use embedding cosine between the cluster centroid and each
node's `topic_embedding`. Attach to the nearer node. This is a tiebreaker, not the primary signal.

### The many-to-many relationship

The mapping is a join table in the event log:

```
cluster_id  →  [node_id, node_id, …]   # a cluster can name multiple events (e.g. "X met Y to discuss Z")
node_id     →  [cluster_id, cluster_id, …]  # a node accumulates clusters over its lifetime
```

This is an append-only event (e.g. `graph.cluster_attached`) on the NATS bus; the kernel folds it
into the projection. No mutation of existing rows.

---

## 3. How typed edges (develops / spawns / merges) are inferred

**Do not infer edges with an LLM at attachment time.** Edge inference is expensive and ambiguous on
a single cluster in isolation. Instead: **infer edges lazily from node lifecycle signals**,
triggered by the attachment event.

### develops

The most common edge. Fired when:
- A new cluster attaches to a node **N** and the cluster's earliest timestamp is later than the
  previous cluster on **N** by more than the novelty window (see §4).
- The new cluster's `topic_embedding` cosine with **N**'s centroid clears a threshold (≥ 0.72 is a
  reasonable start — same event, new development).

Semantically: _"this node's story has progressed."_ The edge runs from the earlier cluster to the
newer one, both on the same node. This is an intra-node temporal edge.

### spawns

Fired when:
- A cluster that was attached to node **N** _also_ satisfies the entity+temporal gate for a _new_
  node **M** where **M** was not yet attached to **N**'s cluster set.
- The semantic distance between **N.topic_embedding** and **M.topic_embedding** clears a divergence
  threshold (cosine < 0.60 — they are now distinct stories).

Semantically: _"a sub-story has broken off and is developing independently."_ The edge runs N → M.

### merges

The inverse: fired when two nodes **N1** and **N2** that previously had no shared clusters both
satisfy the entity+temporal gate for the same new cluster **C**, AND their `topic_embedding`
centroids converge past a similarity threshold (≥ 0.78 — they have become the same story).

The `merges` edge runs N1 → N2 (or N2 → N1, whichever is older). **Do not delete the older node**
— it is part of the event log. Fold the projection so that queries for either node return the union.

### Implementation note for #42

All three edge types are pure functions of existing node state + the incoming cluster's signals.
No LLM needed. A Rust kernel rule can compute them as part of the `graph.cluster_attached` fold.
The Python layer fires the attachment event; the kernel decides the edges as effects.

---

## 4. How claim-novelty ('you've seen this') is computed

**Recommendation: novelty is a per-user read over the user's seen-cluster set, computed at serve
time, not at ingestion time.**

### What the user has "seen"

Track at the **cluster level**: when the client opens a story that contains cluster **C**, record
`(user_id, cluster_id, seen_at)` as an event. This is cheaper and more meaningful than tracking
at the claim level (claims inside a cluster are the same fact — seeing any of them means the user
has seen the fact).

### Novelty signal computation (for #44)

At serve time, for each cluster **C** in the feed:

```
novelty(C, user) = 1.0   if (user, C.id) not in seen_set
                 = 0.0   if (user, C.id) in seen_set
```

For **wire reprints** (the free collapse PLAN.md §8 promises): because `collapse_originators`
already collapses wire reprints into a single originator group _within_ a cluster, two articles
from Reuters and AP on the same fact are already the same cluster. The user sees the cluster once.
No additional deduplication needed here — it falls out of the corroboration design.

For **cross-node novelty** (the user has seen a related but distinct cluster on the same node):
surface a `develops` label, not a "you've seen this" suppression. "You've seen this" means _exactly
this fact_; "this story has developed" means _a new fact on a known event_. These are different UX
actions — the former is a dedup, the latter is a hook to re-engage.

### Novelty decay

A claim seen 30+ days ago should be treated as novel again (the user may have forgotten; events can
resurface). A simple `seen_at` timestamp check with a configurable decay window (default: **30 days**,
surfaced as a `Config` entry in §P8) is sufficient.

### Where this lives in the architecture

- **Kernel responsibility:** maintain the `user_seen_clusters` projection (a fold over
  `user.cluster.seen` events). The projection is per-tenant from day one (D21).
- **Python (serve time):** the curation/feed agent reads the projection and annotates each cluster
  in the served feed with `novelty: 1.0 | 0.0` and an optional `seen_label` ("seen 2 days ago",
  "story has developed").
- **Client:** the iOS app uses `novelty` to decide display order (novel facts first) and whether to
  show a "you've seen this" indicator. On-device re-ranking (D23/D25) can weight by novelty.

---

## 5. Summary of decisions

| Question | Decision |
|---|---|
| Event-node vs cluster | Node = named occurrence with entity spine; cluster = corroborated fact. One node, many clusters over time. |
| Attachment signal | Entity Jaccard ≥ 0.40 AND temporal window ≤ 72 h (both required; semantic similarity is a tiebreaker only) |
| Many-to-many | Append-only `graph.cluster_attached` event; join table projection in kernel |
| Edge inference | Lazy, from node lifecycle signals; pure functions in Rust kernel; no LLM |
| `develops` | Later cluster on same node, within centroid similarity threshold |
| `spawns` | Cluster attaches to two nodes whose centroids are diverging |
| `merges` | Two nodes converge; older node is never deleted (event log) |
| Novelty unit | Cluster (not claim, not article) |
| Novelty computation | Per-user seen-cluster set, checked at serve time, 30-day decay window |
| Wire dedup for novelty | Falls out of `collapse_originators` — no extra work |

---

## 6. Prototype

The following is a minimal reference implementation for the attachment gate and edge inference
signals. It is a standalone module — nothing imports it yet — placed alongside this doc so #42
and #43 have a concrete starting point.

See `python/maat/pipeline/story_graph_proto.py` (added alongside this doc).

---

## 7. Open questions for #42 / #43 / #44

1. **Entity extraction backend:** the prototype uses a stub. #42 must decide whether to use NER
   from Mistral (consistent with D8) or a deterministic NER library (spaCy). Given the bright-line
   (judgement → agent, mechanical → tool), a deterministic library is preferable for the spine
   extraction; Mistral only if language coverage requires it.

2. **τ_entity threshold (0.40):** start here; tune once real data arrives. The calibration harness
   from §7 (D16) should include an entity-overlap distribution over the golden corpus.

3. **72-hour window:** appropriate for breaking news (politics, finance). May need a per-topic or
   per-extremity multiplier (an "extraordinary" claim's window may be longer — extraordinary claims
   resurface as evidence arrives). Surfaced as a `Config` entry in §P8.

4. **`merges` detection cost:** checking all existing nodes for convergence on every attachment
   event is O(N_nodes × N_clusters). At P4 scale this is fine; at P2 volume, limit to nodes in a
   sliding recency window.

5. **User seen-set storage:** at single-user scale (D21), an in-memory set per request is fine.
   The schema carries `tenant_id` for when multi-tenancy is real (#46).
