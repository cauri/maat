-- Projection-harvester (#39, §8): point-in-time snapshots of the last-write-wins `clusters`
-- projection, so the truth-over-time / calibration loop can fold how a fact's corroboration
-- evolved. The kernel updates `clusters` in place; the harvester (scripts/harvest.py) emits one
-- `cluster.snapshot` event per live cluster per run, which maat-kerneld folds here.
-- One row per (cluster_id, calendar day) — idempotent, so a retry or a second run the same day
-- upserts rather than duplicating.
create table if not exists cluster_snapshots (
    id                      bigserial primary key,
    cluster_id              text not null,
    tenant_id               text not null default 'cauri',
    snapshot_day            date not null,
    fact                    text,
    independent_originators integer not null default 0,
    has_primary             boolean not null default false,
    extremity               text not null default 'notable',
    confidence              double precision not null default 0,
    harvested_at            timestamptz not null default now(),
    unique (cluster_id, snapshot_day)
);

create index if not exists cluster_snapshots_cluster_idx
    on cluster_snapshots (cluster_id, snapshot_day);
