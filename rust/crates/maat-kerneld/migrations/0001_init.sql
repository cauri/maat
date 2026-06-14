-- 0001_init — the event log + initial projections (D20; BRIEF §10: simplest durable store).
create extension if not exists vector;

-- Append-only event log: the source of truth.
create table if not exists events (
    id         bigserial primary key,
    event_id   uuid        not null default gen_random_uuid(),
    stream_id  text        not null,
    type       text        not null,
    data       jsonb       not null,
    tenant_id  text        not null default 'cauri',
    created_at timestamptz not null default now()
);
create index if not exists events_stream_idx on events (stream_id, id);
create index if not exists events_type_idx on events (type, id);

-- Projections (read models; folded from events, can be rebuilt).
create table if not exists articles (
    id          text primary key,
    tenant_id   text not null default 'cauri',
    title       text,
    source      text,
    url         text,
    language    text,
    body        text,
    ingested_at timestamptz not null default now()
);

create table if not exists claims (
    id            uuid primary key default gen_random_uuid(),
    tenant_id     text    not null default 'cauri',
    article_id    text    not null references articles (id),
    text          text    not null,
    voice         text    not null,          -- own | attributed
    speaker       text,
    relay_chain   jsonb,
    in_headline   boolean not null default false,
    evidence_span text,
    kind          text,                       -- fact | projection (null until classified)
    is_synthesis  boolean not null default false,
    horizon       text,
    created_at    timestamptz not null default now()
);
create index if not exists claims_article_idx on claims (article_id);
