-- 0012_events_js_seq.sql — dedup key for the kernel's durable JetStream consumer (D20).
--
-- The kernel now consumes events from a durable JetStream consumer (at-least-once delivery), so
-- a message can be redelivered — e.g. if kerneld crashes after committing the fold but before
-- acking. Each delivered message carries its JetStream stream sequence; we record it here with a
-- unique constraint and fold each event inside one transaction, so a redelivered message is a
-- no-op (insert hits the conflict, fold is skipped) — the fold is exactly-once.
--
-- Existing rows keep js_seq NULL (Postgres treats NULLs as distinct, so the unique index allows
-- the whole back-catalogue); only events recorded by the new consumer carry a sequence.
alter table events add column if not exists js_seq bigint;
create unique index if not exists events_js_seq_uniq on events (js_seq);
