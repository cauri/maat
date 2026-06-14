-- 0003 — the confidence read on a corroboration cluster (§5.6-5.7).
alter table clusters add column if not exists confidence double precision not null default 0;
