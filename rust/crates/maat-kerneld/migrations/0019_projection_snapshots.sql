-- 0019_projection_snapshots — read-model snapshot manifest (#287).
--
-- Two logs exist (see docs/spikes/events-log-snapshots.md): the append-only `events` table (0001)
-- is the COMPLETE source of truth and grows forever, while the JetStream MAAT_EVENTS stream is only
-- a 30-day / 2 GiB delivery window. A projection REBUILD must therefore replay the Postgres log, not
-- the stream. To keep that bounded by snapshot cadence rather than total history, `maat-kerneld
-- --snapshot` clones every projection table into its own `snap_<watermark>` schema at a known
-- event-id watermark, and `maat-kerneld --rebuild` restores the latest snapshot then replays only
-- events with id > watermark.
--
-- This table is the index over those snapshots: which watermark, which schema holds the bytes, how
-- big. Retention keeps the most recent N (MAAT_SNAPSHOT_KEEP, default 3) and drops the rest.
create table if not exists projection_snapshots (
    id          bigserial   primary key,
    tenant_id   text        not null default 'cauri',
    watermark   bigint      not null,                       -- max(events.id) folded into this snapshot (gap-free: taken with the consumer stopped)
    js_seq      bigint,                                     -- max(events.js_seq) at the watermark, for JetStream cursor alignment
    schema_name text        not null,                       -- the snap_<watermark> schema holding the cloned projection tables
    tables      jsonb       not null default '[]'::jsonb,   -- [{"table": ..., "rows": ...}] captured, for observability
    event_count bigint      not null default 0,             -- count(events) with id <= watermark at capture time
    created_at  timestamptz not null default now(),
    note        text
);

create index if not exists projection_snapshots_watermark_idx
    on projection_snapshots (watermark desc);
