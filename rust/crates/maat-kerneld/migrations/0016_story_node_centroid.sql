-- 0016 — incremental story-graph deltas (#42 at scale). The full-snapshot `story.graph.rebuilt`
-- event outgrew NATS's 1 MB max_payload once the corpus passed ~1k clusters, so the rebuild
-- silently stopped landing. The builder now folds only NEW clusters each tick and emits small
-- `story.graph.delta` events the kernel applies incrementally (insert/upsert, no full replace).
--
-- Two things that needs:
--   1. Node centroids must survive between runs so the next tick can place new clusters against the
--      existing graph and infer develops/merges edges — persist the running topic-embedding centroid
--      on the node (nullable; only ACTIVE nodes carry one, settled nodes leave it null).
--   2. Incremental inserts must be idempotent (a re-delivered delta must not duplicate rows), so the
--      append targets get natural-key unique indexes for `on conflict do nothing`.

alter table story_nodes add column if not exists topic_embedding jsonb;

-- Dedupe any pre-existing duplicate rows first (the old whole-graph rebuild appended without a
-- natural key), keeping one of each group by physical ctid, so the unique indexes below always
-- create cleanly regardless of the projection's prior state.
delete from story_node_clusters a using story_node_clusters b
    where a.ctid < b.ctid and a.tenant_id = b.tenant_id
      and a.node_id = b.node_id and a.cluster_id = b.cluster_id;

delete from claim_node_links a using claim_node_links b
    where a.ctid < b.ctid and a.tenant_id = b.tenant_id and a.claim_id = b.claim_id
      and a.node_id = b.node_id and a.cluster_id = b.cluster_id;

delete from story_edges a using story_edges b
    where a.ctid < b.ctid and a.tenant_id = b.tenant_id and a.kind = b.kind
      and a.from_id = b.from_id and a.to_id = b.to_id;

create unique index if not exists story_node_clusters_uniq
    on story_node_clusters (tenant_id, node_id, cluster_id);

create unique index if not exists claim_node_links_uniq
    on claim_node_links (tenant_id, claim_id, node_id, cluster_id);

create unique index if not exists story_edges_uniq
    on story_edges (tenant_id, kind, from_id, to_id);
