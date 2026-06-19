-- #286: persist claim-text embeddings so corroboration REUSES them across runs instead of
-- re-embedding the whole claim set every pass (≈0.8 GB of Mistral calls at 100k claims, recomputed
-- each tick). Content-addressed by a hash of the embedded text, so a new cross-lingual pivot (#240)
-- naturally gets its own entry and an unchanged claim is never re-embedded.
--
-- This is a DERIVED CACHE, not a projection: it is rebuildable by re-embedding and is never a source
-- of truth, so it deliberately lives OUTSIDE the event log (a 1024-d vector per claim would bloat the
-- append-only log, #287). The kernel only owns the schema here; the corroborate agent reads/writes it.
-- The `vector` extension is enabled in 0001_init.sql.
create table if not exists embedding_cache (
    text_hash   text primary key,
    embedding   vector(1024) not null,
    model       text not null,
    created_at  timestamptz not null default now()
);
