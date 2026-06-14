-- 0005 — admin / operator console (P8, F3). Operator corrections are events; these columns
-- let a human fix STICK (the pipeline must not clobber it on re-run) and record a flagged
-- laundering abuse (§5.2). The audit trail itself is the events log (no table needed).
alter table claims add column if not exists corrected boolean not null default false;
alter table claims add column if not exists corrected_at timestamptz;
alter table claims add column if not exists laundering_flag text;
-- laundering_flag ∈ endorsement | bare_repetition | selective_amplification (§5.2)
