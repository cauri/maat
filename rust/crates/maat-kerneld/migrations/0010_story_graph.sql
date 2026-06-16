-- Story graph (#42/#43/#44, P4): event-nodes joined by typed edges, with a many-to-many
-- claim↔node mapping, so the feed can THREAD related clusters into a developing story.
-- The builder (maat.agents.story_graph_agent) folds the live clusters and emits one
-- `story.graph.rebuilt` event; maat-kerneld projects it atomically into these tables.

create table if not exists story_nodes (
    id            text primary key,           -- content-addressed from entity spine + seed cluster
    tenant_id     text not null default 'cauri',
    headline      text,                        -- shortest corroborated fact that names the event
    entity_spine  jsonb not null default '[]'::jsonb,
    first_seen    double precision not null default 0,
    last_updated  double precision not null default 0,
    cluster_count integer not null default 0
);

create table if not exists story_edges (
    id        bigserial primary key,
    tenant_id text not null default 'cauri',
    kind      text not null,                   -- 'develops' | 'spawns' | 'merges'
    from_id   text not null,
    to_id     text not null
);
create index if not exists story_edges_from_idx on story_edges (from_id);

-- Which clusters belong to which event-node (the threading the feed groups on).
create table if not exists story_node_clusters (
    tenant_id  text not null default 'cauri',
    node_id    text not null,
    cluster_id text not null
);
create index if not exists story_node_clusters_cluster_idx on story_node_clusters (cluster_id);
create index if not exists story_node_clusters_node_idx on story_node_clusters (node_id);

-- The many-to-many claim↔node join (#43), queryable in both directions.
create table if not exists claim_node_links (
    tenant_id  text not null default 'cauri',
    claim_id   text not null,
    node_id    text not null,
    cluster_id text not null
);
create index if not exists claim_node_links_claim_idx on claim_node_links (claim_id);
create index if not exists claim_node_links_node_idx on claim_node_links (node_id);
