-- 0007 — editable agent prompts (P8). The canonical prompt for each stage lives in code;
-- the operator console saves audited, versioned OVERRIDES here. The agents read the active
-- row at run time (falling back to the code seed), so an edit takes effect on the next run and
-- rollback is just re-activating an older version. Append-only history; one active row per key.
create table if not exists prompts (
    id         bigserial primary key,
    key        text        not null,            -- stage: extract | classify | extremity
    version    int         not null,
    text       text        not null,
    active     boolean     not null default true,
    reason     text,
    actor      text        not null default 'operator',
    created_at timestamptz not null default now()
);
create index if not exists prompts_key_idx on prompts (key, version desc);
