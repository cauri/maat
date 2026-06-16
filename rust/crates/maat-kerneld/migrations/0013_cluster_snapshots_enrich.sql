-- 0013 — enrich cluster_snapshots so the truth-over-time fold reads the FULL trajectory off the
-- projection (#39, closing the loop). The reputation fold needs per-source independence (the
-- cluster's `sources` + collapsed `originators` groups); `corrected` carries the operator/reader
-- refutation already recorded on member claims (corrected / laundering_flag) so resolve_outcome
-- can resolve a fact to REFUTED. Historical snapshots predate these columns and backfill to
-- empty/false — reputation degrades gracefully for them, full signal accrues going forward.
alter table cluster_snapshots add column if not exists sources     jsonb   not null default '[]';
alter table cluster_snapshots add column if not exists originators jsonb   not null default '[]';
alter table cluster_snapshots add column if not exists corrected   boolean not null default false;
