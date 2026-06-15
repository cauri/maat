-- 0006 — dead-letter store (P8, F4). The kernel folds events as it consumes them; when a
-- projection fails it logs + skips so one bad event cannot wedge the spine. This records those
-- failures so the operator Run console can surface them instead of leaving them only in logs.
create table if not exists dead_letters (
    id         bigserial primary key,
    stream_id  text,
    type       text        not null,
    data       jsonb,
    error      text        not null,
    tenant_id  text        not null default 'cauri',
    created_at timestamptz not null default now()
);
