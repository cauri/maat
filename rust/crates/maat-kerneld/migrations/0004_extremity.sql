-- 0004 — the prior against the fact (§5.6); scales the confidence bar.
alter table clusters add column if not exists extremity text not null default 'notable';
