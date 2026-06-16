-- 0009_acquisition.sql — marketing-site acquisition funnel (maat.press → console /acquisition).
--
-- Event-sourced (D5/D20): the public marketing service publishes acquisition.* events with
-- tenant_id='public'; maat-kerneld folds them here. Pre-user telemetry — no PII beyond an
-- optional, visitor-volunteered launch-notify email (acquisition_signups).

create table if not exists acquisition_signals (
    id           bigserial   primary key,
    tenant_id    text        not null default 'public',
    kind         text        not null,                 -- 'view' | 'click' | 'notify'
    platform     text,                                  -- 'ios' | 'mac' (clicks / notify)
    path         text,
    referrer     text,
    utm_source   text,
    utm_medium   text,
    utm_campaign text,
    ua_family    text,                                  -- coarse: ios/mac/android/windows/linux/other
    visitor      text,                                  -- ephemeral per-page-load id (view↔click join), not persistent
    created_at   timestamptz not null default now()
);
create index if not exists acquisition_signals_kind_created on acquisition_signals (kind, created_at);

-- Deduped launch-notify list (one row per email), folded from acquisition.notify_requested.
-- `beta` is an explicit opt-in (the launch form's unticked "beta tester" checkbox); once a
-- visitor opts in it stays true across repeat submits (the fold OR-merges it).
create table if not exists acquisition_signups (
    email      text        primary key,
    platform   text,
    referrer   text,
    utm_source text,
    beta       boolean     not null default false,
    first_seen timestamptz not null default now(),
    last_seen  timestamptz not null default now(),
    hits       integer     not null default 1
);
