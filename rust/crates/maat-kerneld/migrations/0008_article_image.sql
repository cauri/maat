-- 0008 — article hero image (og:image), captured at ingest (#1, Apple client).
-- Display-only enrichment served through the reader's image proxy; never a veracity signal.
alter table articles add column if not exists image_url text;
