-- 0002 — corroboration clusters (§5.5).
create table if not exists clusters (
    id                      text primary key,    -- stable hash of the member claim ids
    tenant_id               text    not null default 'cauri',
    fact                    text    not null,
    sources                 jsonb   not null default '[]',
    originators             jsonb   not null default '[]',  -- list of collapsed originator groups (article ids)
    independent_originators int     not null default 0,
    has_primary             boolean not null default false,
    claim_ids               jsonb   not null default '[]',
    created_at              timestamptz not null default now()
);
