# Spike: Events-log read-model snapshots + compaction strategy

**Issue:** #287 — Events log: read-model snapshots + compaction strategy
**Part of:** #293 (P12 — hardening / scale-readiness), Lane B
**Status:** DECIDED — built (`maat-kerneld --snapshot` / `--rebuild`, migration 0019)
**Author:** agent (autonomous, 2026-06-19)

---

## 1. The problem: two logs, one of them unbounded

Maat is event-sourced. There are actually **two** logs, and conflating them is the trap:

| | **JetStream `MAAT_EVENTS`** | **Postgres `events` table** |
|---|---|---|
| Role | delivery / at-least-once transport | the canonical source of truth |
| Retention | **30 days / 2 GiB**, rolling (`discard: Old`) — [main.rs](../../rust/crates/maat-kerneld/src/main.rs) | **append-only, forever** — [0001_init.sql](../../rust/crates/maat-kerneld/migrations/0001_init.sql) |
| Written by | every `maat.events.*` publisher | `record_and_project`, deduped by `js_seq` |
| Read by (today) | the live kerneld consumer | nobody replays it |

The kernel consumes from JetStream, appends each event to the Postgres `events` table, and folds the
projections — all in one transaction keyed by the JetStream sequence (`js_seq`).

Two costs grow **without bound**:

1. **The `events` table grows forever.** Every article, claim, classification, corroboration, and
   funnel signal is an immortal row. At production cadence this is the dominant table.
2. **"Rebuild the projections" has no bounded path** — and, worse, the obvious path is *silently
   incomplete*. The only rebuild mechanism today is "truncate the projections, reset the durable
   consumer, let JetStream redeliver." But JetStream only retains 30 days / 2 GiB. **Anything older
   has already been discarded from the stream**, so a stream-replay rebuild silently reconstructs
   only recent history. The complete history lives in Postgres — which nothing replays.

So a projection rebuild (after drift, a bad backfill, or a new projection that needs the full past)
is either impossible or wrong, and its cost — if it worked — would scale with *all* history, not with
how often we checkpoint.

## 2. Decision

**Rebuild from the Postgres `events` table (the complete log), not from JetStream. Bound that
rebuild with periodic read-model snapshots.**

- A **snapshot** captures every projection table at a known event-id **watermark** `W = max(events.id)`.
- **Rebuild** = restore the latest snapshot, then replay only `events WHERE id > W` through the *same*
  fold the live consumer uses.
- Rebuild cost is therefore **bounded by snapshot cadence**, not by total history. ✅ (the issue's
  "Done when").

This keeps the single-writer invariant intact (D20): the fold lives in exactly one place
([`project` in lib.rs](../../rust/crates/maat-kerneld/src/lib.rs)), and *both* the live path and the
offline rebuild drive it. A from-Postgres rebuild can never drift from live folding because it **is**
the same code — the `maat-kernel` contract ("same event log ⇒ same derived state") made executable.

## 3. Mechanism (built)

### Migration `0019_projection_snapshots.sql`
A manifest table indexing each snapshot: `watermark`, `js_seq` (JetStream cursor alignment),
`schema_name`, a `tables` array of `{table, rows}` for observability, `event_count`, `created_at`.

### `maat-kerneld --snapshot`
1. Read `W = max(events.id)`, `max(js_seq)`, `count(*)`.
2. Clone every projection table into a fresh `snap_<W>` schema (`create table snap_W.t as table public.t`).
3. Insert the manifest row.
4. **Retention:** drop every snapshot older than the most recent `MAAT_SNAPSHOT_KEEP` (default 3) —
   `drop schema … cascade` + delete the manifest row.

### `maat-kerneld --rebuild`
1. Find the latest snapshot.
2. In one transaction: `TRUNCATE` all projection tables `CASCADE`, then restore each table from the
   snapshot schema. If there is no snapshot, start from empty (`W = 0`).
3. Replay `events WHERE id > W ORDER BY id`, batched (1 000/page), each event folded in its own
   transaction — matching the live path's per-event semantics.

Restart-safe: rebuild always truncates+restores first, so re-running after an interrupted replay
simply starts over.

### Two correctness details that bit (and are handled)
- **Additive migrations.** A snapshot taken under an older schema is restored by the columns the
  snapshot and the live table *share* (explicit column list, not `select *`), so a column added by a
  later migration just takes its default. (`restore_table`)
- **Sequence-backed ids.** Restoring rows with explicit `id`s leaves the table's sequence behind; a
  replayed insert that uses the default would then collide. After restore we fast-forward every
  serial sequence past the restored max (`setval(pg_get_serial_sequence(...), max(id))`). The
  integration test exercises exactly this with `acquisition_signals` (restored id + a replayed id,
  asserted distinct).

### Which tables are projections
`PROJECTION_TABLES` (snapshotted, truncated, replayed): `articles`, `claims`, `clusters`,
`cluster_snapshots`, `story_nodes`, `story_edges`, `story_node_clusters`, `claim_node_links`,
`claim_relations`, `acquisition_signals`, `acquisition_signups`, `prompts`.

Deliberately **excluded**:
- `events` — the log itself (the source of truth; never a fold target).
- `dead_letters` — operational state written *on fold failure*, not folded *from* an event, so it
  isn't reconstructible by replay (and must survive a rebuild).
- `embedding_cache` — a derived, rebuildable cache that lives **outside** the log by design (#286 /
  [0017](../../rust/crates/maat-kerneld/migrations/0017_embedding_cache.sql)).
- `projection_snapshots` — this feature's own manifest.

## 4. Retention / compaction policy for the log

The issue asks for "a retention/compaction policy for high-volume, fully-projected event types." The
policy:

### Snapshots
Keep the most recent **N = 3** (`MAAT_SNAPSHOT_KEEP`). Three is enough to roll back a bad rebuild and
to keep one comfortably outside the JetStream window; the cost is ~N× the projection footprint (small
next to the `events` table). Cadence: **daily**, alongside the existing harvest/clock tick.

### The raw `events` log — what is safe to prune, and what is NOT
Once a snapshot at watermark `W` exists, every raw event with `id ≤ W` is **redundant for rebuild** —
its effect is already baked into the snapshot. That is the compaction lever. But the log is not *only*
a rebuild input, so pruning is **policy-gated, not automatic**, and splits by purpose:

- **Safe to archive/prune (id well below the oldest retained snapshot):** the high-volume,
  fully-projected pipeline types whose entire downstream meaning is the projection —
  `article.ingested` bodies, `claims.extracted`, `claims.classified`, `cluster.corroborated`,
  `acquisition.page_viewed`/`cta_clicked`. These dominate growth and are fully captured by a snapshot.
- **Must be preserved (do not prune):**
  - the **truth-over-time / calibration** trail — but note this is *already* separately materialised
    in `cluster_snapshots` (the #39 harvester), so the calibration loop does **not** depend on the
    raw pre-`W` events surviving;
  - low-volume **audit / operator** events (`admin.*`, `cluster.removed`, `claim.disputed`) — tiny,
    high evidentiary value, never worth pruning;
  - anything inside the **JetStream window** (last 30 days) — the live consumer may still redeliver it.

**Recommended retention:** keep raw `events` to **max(90 days, the oldest retained snapshot
watermark)**. Below that, archive the safe-to-prune high-volume types to cold storage (object store
as newline-JSON, partitioned by month) and `DELETE` them from `events`; keep audit/admin events
indefinitely. The archive remains a valid (if cold) extension of the log: rebuild from the oldest
snapshot never needs it, and a full-history audit can re-ingest it.

This deletion is **deliberately not wired to run automatically** — it is the one destructive operation
against the source of truth, and it should be an explicit, reviewed operator action once production
volume actually warrants it (it does not yet). The mechanism that makes it *safe* — snapshots that
let rebuild start above the pruned region — is what #287 builds now; the pruning job is a
documented, gated follow-on (see §6).

### Alternative considered: a separate JetStream stream per high-volume type
Routing `article.ingested`/`claims.*` to their own stream with tighter retention was considered and
**rejected for now**: it fragments the single ordered log (the `id`-ordered replay is what makes
rebuild simple and deterministic), and it solves only the *transport* window, not the Postgres
growth. The snapshot+watermark approach addresses both the rebuild bound and the compaction lever
without splitting the log. Revisit only if a single type's transport volume alone threatens the 2 GiB
stream cap before the daily snapshot can checkpoint it.

## 5. No serving path reads the raw log

The third bullet ("ensure no serving path reads the raw `events` log") is owned by the feed-serving
work and already holds: serving reads the **projections** (`articles`, `clusters`, `story_*`,
`stories`/feed views), never `events`. Rebuild/snapshot are the *only* readers of the raw log, and
they are offline maintenance ops, not request-path code. A `test_layering`-style guard against a
serving query over `events` is noted as a cheap future assertion.

## 6. Operations

Both ops are **one-shot** (they exit when done) and must run with the **live consumer stopped**, so
`max(events.id)` is a gap-free high-water mark (no in-flight event transaction can later commit an id
*below* the watermark and be missed by the tail replay):

The image's `ENTRYPOINT` is `maat-kerneld`, so `docker compose run` appends the flag as an argument:

```
docker compose stop kerneld
docker compose run --rm kerneld --snapshot   # daily checkpoint
docker compose run --rm kerneld --rebuild    # restore latest snapshot + replay the tail
docker compose start kerneld                 # durable consumer resumes from its ack-floor
```

The live durable consumer is left untouched by a rebuild: on restart it resumes from its JetStream
ack-floor, and the idempotent `events` insert (`on conflict (js_seq) do nothing`) plus the
idempotent folds make any redelivery a no-op.

**Follow-on (tracked, not in this change):**
- a daily `--snapshot` tick wired into the clock/cron on the box;
- the gated cold-storage archiver + `DELETE` for the safe-to-prune high-volume types, once volume
  warrants it;
- a `test_layering` assertion that no serving module queries `events`.

## 7. What was built & verified

- `0019_projection_snapshots.sql` — the manifest.
- `maat-kerneld` split into lib + bin; the fold extracted to `project()` so live + rebuild share one
  code path; `snapshot()` / `rebuild()` / retention added; `--snapshot` / `--rebuild` CLI modes.
- `tests/rebuild.rs` — an integration test against real Postgres proving: rebuild-from-scratch
  replays the whole log; a snapshot captures state at a watermark; after corrupting the projections,
  rebuild restores the snapshot and replays **only the tail** (`replayed == 2`, not total history);
  serial ids survive without collision; a second rebuild is idempotent.
- CI: a pgvector Postgres service added to the `rust` job so this runs on the merge gate (it
  hard-fails in CI rather than skipping). Locally it skips when no DB is present.
