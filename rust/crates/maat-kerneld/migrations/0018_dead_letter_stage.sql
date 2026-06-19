-- #299: tag agent dead-letters with the stage (durable consumer) that produced them, so the
-- operator console can show a per-stage dead-letter count next to the live consumer lag (which
-- stage is poison-ing?). The kernel's existing folding dead-letters predate this column and stay
-- NULL — read-side coalesces them to 'kerneld'.
alter table dead_letters add column if not exists stage text;
