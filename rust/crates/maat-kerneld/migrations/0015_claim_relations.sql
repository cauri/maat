-- 0015 — automated contradiction detection (#229).
--
-- The contradiction agent runs NLI between a claim and its nearest neighbours and records each
-- detected relation here (event-sourced via claim.related). Standalone for now; the story graph
-- (#42) will later fold the SAME events into typed contradicts/corroborates edges — this is the
-- interim home, not a throwaway. Idempotent per (claim_a, claim_b, relation).
create table if not exists claim_relations (
    id         bigserial primary key,
    tenant_id  text not null default 'cauri',
    claim_a    uuid not null,
    claim_b    uuid not null,
    relation   text not null,                  -- 'contradicts' | 'entails'
    score      double precision not null,       -- the NLI model's probability for that relation [0,1]
    created_at timestamptz not null default now(),
    unique (claim_a, claim_b, relation)
);
create index if not exists claim_relations_a_idx on claim_relations (claim_a);
create index if not exists claim_relations_b_idx on claim_relations (claim_b);

-- A claim that a STRONGER, contradicting claim refutes (arbitrated by grounding / confidence). The
-- harvester already derives a cluster's `corrected` (→ REFUTED) from its member claims, so flagging
-- the loser claim `disputed` feeds the existing refutation path with no new snapshot plumbing.
alter table claims add column if not exists disputed boolean not null default false;
