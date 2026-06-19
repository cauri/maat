//! Integration test for the #287 snapshot + bounded-rebuild path, against a real Postgres.
//!
//! It SKIPS when no Postgres is reachable (so `cargo test --all` stays green locally without a DB),
//! but in CI (`CI` set) a missing DB is a hard failure — a silent skip there would defeat the point
//! of gating on it (mirrors `python/tests/test_routes_integration.py`). CI sets `DATABASE_URL` to the
//! `postgres` service; locally, run a pgvector container and export `DATABASE_URL` to exercise it.
//!
//! What it proves end-to-end:
//!   * rebuild-from-scratch replays the whole log into the projections;
//!   * a snapshot captures projection state at a watermark;
//!   * after corrupting the projections, rebuild RESTORES the snapshot and replays only the tail
//!     (bounded by snapshot cadence — `replayed == 2`, not the full history); and
//!   * sequence-backed ids survive the round-trip without collision (a replayed acquisition signal
//!     lands at a fresh id above the restored one).

use maat_kerneld::{project, rebuild, run_migrations, snapshot, EventEnvelope, PROJECTION_TABLES};
use sqlx::{PgPool, Row};

fn admin_url() -> String {
    std::env::var("DATABASE_URL").unwrap_or_else(|_| "postgres://maat:maat@localhost:5432/maat".into())
}

/// Swap the database name in a libpq URL (everything after the last `/`, minus any query string).
fn with_db(url: &str, db: &str) -> String {
    let (base, _old) = url.rsplit_once('/').expect("url has a path segment");
    match _old.split_once('?') {
        Some((_, query)) => format!("{base}/{db}?{query}"),
        None => format!("{base}/{db}"),
    }
}

async fn seed(pool: &PgPool, js_seq: i64, typ: &str, stream_id: &str, data: serde_json::Value) {
    // Append a raw event to the log exactly as the live insert would (minus the fold) — rebuild
    // folds it. js_seq is unique but irrelevant to replay (which orders by id).
    sqlx::query(
        "insert into events (stream_id, type, data, tenant_id, js_seq) values ($1, $2, $3, $4, $5)",
    )
    .bind(stream_id)
    .bind(typ)
    .bind(sqlx::types::Json(data))
    .bind("cauri")
    .bind(js_seq)
    .execute(pool)
    .await
    .unwrap();
}

async fn count(pool: &PgPool, table: &str) -> i64 {
    sqlx::query_scalar(&format!("select count(*) from {table}"))
        .fetch_one(pool)
        .await
        .unwrap()
}

#[tokio::test]
async fn snapshot_then_rebuild_restores_and_replays_only_the_tail() {
    let admin = admin_url();
    // Gate on DB availability (hard-fail in CI, skip locally).
    let admin_pool = match PgPool::connect(&admin).await {
        Ok(p) => p,
        Err(e) => {
            if std::env::var("CI").is_ok() {
                panic!("CI requires Postgres at {admin}: {e}");
            }
            eprintln!("skipping rebuild integration test — no Postgres at {admin} ({e})");
            return;
        }
    };

    // Throwaway database, migrated fresh.
    let test_db = "maat_kerneld_test";
    let _ = sqlx::query(&format!("drop database if exists {test_db} with (force)"))
        .execute(&admin_pool)
        .await;
    sqlx::query(&format!("create database {test_db}"))
        .execute(&admin_pool)
        .await
        .unwrap();
    let pool = PgPool::connect(&with_db(&admin, test_db)).await.unwrap();
    run_migrations(&pool).await.unwrap();

    let claim_id = "11111111-1111-1111-1111-111111111111";

    // --- Pre-snapshot history (folded into the snapshot at the watermark) ---
    seed(&pool, 1, "article.ingested", "A1",
        serde_json::json!({"title": "T1", "source": "src", "language": "en", "body": "b"})).await;
    seed(&pool, 2, "claims.extracted", "A1",
        serde_json::json!({"article_id": "A1", "claims": [
            {"id": claim_id, "text": "the sky is blue", "voice": "own"}]})).await;
    seed(&pool, 3, "cluster.corroborated", "C1",
        serde_json::json!({"id": "C1", "fact": "f1", "confidence": 0.8, "extremity": "notable",
            "independent_originators": 2, "has_primary": true,
            "sources": [], "originators": [], "claim_ids": [claim_id]})).await;
    seed(&pool, 4, "acquisition.page_viewed", "pub",
        serde_json::json!({"platform": "web", "path": "/"})).await;

    // 1. Rebuild from scratch (no snapshot yet) → replays the whole log.
    let (from, replayed) = rebuild(&pool).await.unwrap();
    assert_eq!(from, 0, "no snapshot → rebuild starts at 0");
    assert_eq!(replayed, 4, "replays all four seeded events");
    assert_eq!(count(&pool, "articles").await, 1);
    assert_eq!(count(&pool, "claims").await, 1);
    assert_eq!(count(&pool, "clusters").await, 1);
    assert_eq!(count(&pool, "acquisition_signals").await, 1);

    // 2. Snapshot at the current watermark (= 4).
    let watermark = snapshot(&pool).await.unwrap();
    assert_eq!(watermark, 4);
    let snap_rows: i64 = sqlx::query_scalar(
        "select count(*) from projection_snapshots where watermark = 4 and schema_name = 'snap_4'")
        .fetch_one(&pool).await.unwrap();
    assert_eq!(snap_rows, 1, "manifest row recorded");
    assert_eq!(count(&pool, "snap_4.articles").await, 1, "projection cloned into the snapshot schema");

    // --- Post-snapshot history (the tail rebuild must replay) ---
    seed(&pool, 5, "cluster.corroborated", "C2",
        serde_json::json!({"id": "C2", "fact": "f2", "confidence": 0.5, "extremity": "notable",
            "sources": [], "originators": [], "claim_ids": []})).await;
    seed(&pool, 6, "acquisition.page_viewed", "pub",
        serde_json::json!({"platform": "ios", "path": "/app"})).await;

    // 3. Corrupt the projections (simulate drift / a bad migration backfill). TRUNCATE CASCADE so
    //    the claims→articles FK doesn't block the wipe (the same statement rebuild uses internally).
    let wipe = PROJECTION_TABLES.iter().map(|t| format!("public.{t}")).collect::<Vec<_>>().join(", ");
    sqlx::query(&format!("truncate {wipe} cascade")).execute(&pool).await.unwrap();
    assert_eq!(count(&pool, "articles").await, 0);

    // 4. Rebuild: restores snap_4, then replays ONLY events 5 & 6.
    let (from, replayed) = rebuild(&pool).await.unwrap();
    assert_eq!(from, 4, "restored from the snapshot watermark");
    assert_eq!(replayed, 2, "replays only the tail — bounded by snapshot cadence, not total history");

    // Final state == full fold: pre-watermark rows came back via restore, the tail via replay.
    assert_eq!(count(&pool, "articles").await, 1, "A1 restored from snapshot");
    assert_eq!(count(&pool, "claims").await, 1, "claim restored from snapshot");
    assert_eq!(count(&pool, "clusters").await, 2, "C1 restored + C2 replayed");
    // Two signals, distinct ids: the restored one (id from the snapshot) and the replayed one, which
    // must NOT collide with it — proving the sequence was fast-forwarded past the restored max.
    assert_eq!(count(&pool, "acquisition_signals").await, 2, "signal restored + signal replayed, no PK collision");
    let distinct_ids: i64 = sqlx::query("select count(distinct id) from acquisition_signals")
        .fetch_one(&pool).await.unwrap().get(0);
    assert_eq!(distinct_ids, 2, "the two acquisition signals have distinct ids");

    // Replay is idempotent under `project` (a second rebuild reproduces the same state).
    let (_f, r2) = rebuild(&pool).await.unwrap();
    assert_eq!(r2, 2);
    assert_eq!(count(&pool, "clusters").await, 2, "still 2 after a second rebuild");

    // Exercise the live fold path too, so `project` parity is covered against a fresh tx.
    let mut tx = pool.begin().await.unwrap();
    project(&pool, &mut tx,
        &EventEnvelope {
            stream_id: "C3".into(),
            typ: "cluster.corroborated".into(),
            data: serde_json::json!({"id": "C3", "fact": "f3", "confidence": 0.9, "extremity": "notable",
                "sources": [], "originators": [], "claim_ids": []}),
            tenant_id: "cauri".into(),
        }).await.unwrap();
    tx.commit().await.unwrap();
    assert_eq!(count(&pool, "clusters").await, 3, "direct project() inserts a cluster");

    pool.close().await;
    let _ = sqlx::query(&format!("drop database if exists {test_db} with (force)"))
        .execute(&admin_pool)
        .await;
}
