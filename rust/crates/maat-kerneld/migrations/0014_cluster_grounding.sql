-- 0014 — primary-source grounding (#228). The grounding agent judges whether a cluster's fact is
-- SUPPORTED / CONTRADICTED / NOT_ADDRESSED by its primary source; the kernel records the verdict on
-- the cluster (with the grounding-refined confidence), and the harvester carries it into the
-- snapshot trajectory so a contradiction resolves the fact to REFUTED over time. Nullable: a cluster
-- is ungrounded until the (gated) grounding pass judges it.
alter table clusters          add column if not exists grounding text;
alter table cluster_snapshots add column if not exists grounding text;
