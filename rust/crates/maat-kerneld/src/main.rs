//! `maat-kerneld` — the deterministic spine (D20).
//!
//! Single writer over the canonical store: consumes events from NATS, appends them to the
//! append-only `events` log in Postgres (the source of truth), and folds the minimal
//! projections. Mechanical tools and richer folds grow from here. Run with `--smoke` to
//! self-test the whole spine (publish one event, record it, check the projection).

use anyhow::Result;
use async_nats::jetstream::{self, consumer::pull, consumer::AckPolicy, stream};
use futures::StreamExt;
use serde::{Deserialize, Serialize};
use sqlx::postgres::PgPoolOptions;
use sqlx::types::Json;
use sqlx::PgPool;
use std::time::Duration;

/// Durable, file-backed JetStream stream + consumer so a kerneld restart/outage REPLAYS events
/// rather than dropping them — a core-NATS subscription keeps nothing for an offline subscriber,
/// which silently lost a real beta signup (2026-06-16).
const STREAM_NAME: &str = "MAAT_EVENTS";
const SUBJECT: &str = "maat.events.>";
const DURABLE: &str = "kerneld";

#[derive(Debug, Serialize, Deserialize)]
struct EventEnvelope {
    stream_id: String,
    #[serde(rename = "type")]
    typ: String,
    data: serde_json::Value,
    #[serde(default = "default_tenant")]
    tenant_id: String,
}

fn default_tenant() -> String {
    "cauri".to_string()
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    let db_url = std::env::var("DATABASE_URL")?;
    let nats_url = std::env::var("NATS_URL").unwrap_or_else(|_| "nats://localhost:4222".to_string());

    let pool = PgPoolOptions::new().max_connections(5).connect(&db_url).await?;
    sqlx::migrate!().run(&pool).await?;
    tracing::info!("postgres connected + migrated");

    let client = async_nats::connect(&nats_url).await?;
    tracing::info!("nats connected: {nats_url}");
    let js = jetstream::new(client.clone());

    // Retain every maat.events.* publish durably (30-day / 2 GiB rolling window). A core publish
    // is captured by the stream, so publishers don't change — only the kernel's read path does.
    let events_stream = js
        .get_or_create_stream(stream::Config {
            name: STREAM_NAME.to_string(),
            subjects: vec![SUBJECT.to_string()],
            retention: stream::RetentionPolicy::Limits,
            storage: stream::StorageType::File,
            max_age: Duration::from_secs(60 * 60 * 24 * 30),
            max_bytes: 2 * 1024 * 1024 * 1024,
            discard: stream::DiscardPolicy::Old,
            ..Default::default()
        })
        .await?;
    tracing::info!("jetstream stream {STREAM_NAME} ready");

    let smoke = std::env::args().any(|a| a == "--smoke");
    if smoke {
        let ev = EventEnvelope {
            stream_id: "smoke-article-1".to_string(),
            typ: "article.ingested".to_string(),
            data: serde_json::json!({
                "title": "Smoke test", "source": "local", "language": "en", "body": "hello"
            }),
            tenant_id: "cauri".to_string(),
        };
        client
            .publish("maat.events.article.ingested", serde_json::to_vec(&ev)?.into())
            .await?;
        client.flush().await?;
        tracing::info!("smoke event published");
    }

    // Durable pull consumer: at-least-once, explicit ack. On restart it resumes from the last ack,
    // so anything published while kerneld was down is redelivered and folded — never dropped.
    let consumer = events_stream
        .get_or_create_consumer(
            DURABLE,
            pull::Config {
                durable_name: Some(DURABLE.to_string()),
                filter_subject: SUBJECT.to_string(),
                ack_policy: AckPolicy::Explicit,
                ack_wait: Duration::from_secs(60),
                max_deliver: 6,
                ..Default::default()
            },
        )
        .await?;
    let mut messages = consumer.messages().await?;
    tracing::info!("consuming {STREAM_NAME} via durable consumer {DURABLE}");

    while let Some(item) = messages.next().await {
        let msg = match item {
            Ok(m) => m,
            Err(e) => {
                tracing::warn!("jetstream message error: {e}");
                continue;
            }
        };
        // The JetStream sequence is the dedup key; `delivered` lets us retry transient failures.
        let (seq, delivered) = msg
            .info()
            .map(|i| (i.stream_sequence as i64, i.delivered))
            .unwrap_or((0, 1));
        match serde_json::from_slice::<EventEnvelope>(&msg.payload) {
            Ok(ev) => match record_and_project(&pool, &ev, seq).await {
                Ok(newly) => {
                    if newly {
                        tracing::info!("recorded event type={} stream={} seq={seq}", ev.typ, ev.stream_id);
                    } else {
                        tracing::info!("skip duplicate seq={seq} type={}", ev.typ);
                    }
                    let _ = msg.ack().await;
                    if smoke {
                        let n: i64 = sqlx::query_scalar("select count(*) from articles")
                            .fetch_one(&pool)
                            .await?;
                        tracing::info!("smoke ok: articles projected = {n}");
                        break;
                    }
                }
                Err(e) => {
                    tracing::error!("project failed (type={} seq={seq} delivered={delivered}): {e}", ev.typ);
                    if delivered >= 6 {
                        // Retries exhausted — record the failure for the operator console (P8 F4)
                        // and ack so one poison event can't wedge the whole consumer.
                        let dl = sqlx::query(
                            "insert into dead_letters (stream_id, type, data, error, tenant_id) \
                             values ($1, $2, $3, $4, $5)",
                        )
                        .bind(&ev.stream_id)
                        .bind(&ev.typ)
                        .bind(Json(&ev.data))
                        .bind(e.to_string())
                        .bind(&ev.tenant_id)
                        .execute(&pool)
                        .await;
                        if let Err(de) = dl {
                            tracing::error!("dead-letter insert failed: {de}");
                        }
                        let _ = msg.ack().await;
                    } else {
                        // Possibly transient (e.g. a DB blip) — let JetStream redeliver after ack_wait.
                        let _ = msg.ack_with(async_nats::jetstream::AckKind::Nak(None)).await;
                    }
                }
            },
            Err(e) => {
                tracing::warn!("bad event payload (seq={seq}): {e}");
                let _ = msg.ack().await; // unparseable — drop, don't wedge the consumer
            }
        }
    }
    Ok(())
}

/// Append the event to the log (source of truth), then fold the minimal projection — all in ONE
/// transaction, keyed by the JetStream sequence. Returns Ok(true) if newly recorded, Ok(false) if
/// the message was already processed on a prior delivery (a duplicate → exactly-once no-op).
async fn record_and_project(pool: &PgPool, ev: &EventEnvelope, js_seq: i64) -> Result<bool> {
    let mut tx = pool.begin().await?;
    let recorded = sqlx::query(
        "insert into events (stream_id, type, data, tenant_id, js_seq) \
         values ($1, $2, $3, $4, $5) on conflict (js_seq) do nothing",
    )
    .bind(&ev.stream_id)
    .bind(&ev.typ)
    .bind(Json(&ev.data))
    .bind(&ev.tenant_id)
    .bind(js_seq)
    .execute(&mut *tx)
    .await?;
    if recorded.rows_affected() == 0 {
        tx.commit().await?; // already folded on a prior delivery — nothing to do
        return Ok(false);
    }

    if ev.typ == "article.ingested" {
        sqlx::query(
            "insert into articles (id, tenant_id, title, source, url, language, body, image_url) \
             values ($1, $2, $3, $4, $5, $6, $7, $8) \
             on conflict (id) do update set title = excluded.title, body = excluded.body, \
               image_url = excluded.image_url",
        )
        .bind(&ev.stream_id)
        .bind(&ev.tenant_id)
        .bind(ev.data.get("title").and_then(|v| v.as_str()))
        .bind(ev.data.get("source").and_then(|v| v.as_str()))
        .bind(ev.data.get("url").and_then(|v| v.as_str()))
        .bind(ev.data.get("language").and_then(|v| v.as_str()))
        .bind(ev.data.get("body").and_then(|v| v.as_str()))
        .bind(ev.data.get("image_url").and_then(|v| v.as_str()))
        .execute(&mut *tx)
        .await?;
    }

    if ev.typ == "claims.extracted" {
        let empty: Vec<serde_json::Value> = Vec::new();
        let claims = ev.data.get("claims").and_then(|v| v.as_array()).unwrap_or(&empty);
        let article_id = ev
            .data
            .get("article_id")
            .and_then(|v| v.as_str())
            .unwrap_or(ev.stream_id.as_str());
        for c in claims {
            sqlx::query(
                "insert into claims \
                 (id, tenant_id, article_id, text, voice, speaker, relay_chain, in_headline, \
                  evidence_span, kind, is_synthesis, horizon) \
                 values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) \
                 on conflict (id) do nothing",
            )
            .bind(c.get("id").and_then(|v| v.as_str()))
            .bind(&ev.tenant_id)
            .bind(article_id)
            .bind(c.get("text").and_then(|v| v.as_str()))
            .bind(c.get("voice").and_then(|v| v.as_str()))
            .bind(c.get("speaker").and_then(|v| v.as_str()))
            .bind(c.get("relay_chain").map(Json))
            .bind(c.get("in_headline").and_then(|v| v.as_bool()).unwrap_or(false))
            .bind(c.get("evidence_span").and_then(|v| v.as_str()))
            .bind(c.get("kind").and_then(|v| v.as_str()))
            .bind(c.get("is_synthesis").and_then(|v| v.as_bool()).unwrap_or(false))
            .bind(c.get("horizon").and_then(|v| v.as_str()))
            .execute(&mut *tx)
            .await?;
        }
    }

    if ev.typ == "claims.classified" {
        let empty: Vec<serde_json::Value> = Vec::new();
        let cls = ev
            .data
            .get("classifications")
            .and_then(|v| v.as_array())
            .unwrap_or(&empty);
        for c in cls {
            // `and not corrected`: never clobber an operator fix (P8 F3) on a pipeline re-run.
            sqlx::query(
                "update claims set kind = $2, is_synthesis = $3, horizon = $4 \
                 where id = $1::uuid and not corrected",
            )
            .bind(c.get("id").and_then(|v| v.as_str()))
            .bind(c.get("kind").and_then(|v| v.as_str()))
            .bind(c.get("is_synthesis").and_then(|v| v.as_bool()).unwrap_or(false))
            .bind(c.get("horizon").and_then(|v| v.as_str()))
            .execute(&mut *tx)
            .await?;
        }
    }

    if ev.typ == "cluster.corroborated" {
        let d = &ev.data;
        sqlx::query(
            "insert into clusters \
             (id, tenant_id, fact, sources, originators, independent_originators, has_primary, claim_ids, confidence, extremity) \
             values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) \
             on conflict (id) do update set fact = excluded.fact, sources = excluded.sources, \
               originators = excluded.originators, \
               independent_originators = excluded.independent_originators, \
               has_primary = excluded.has_primary, claim_ids = excluded.claim_ids, \
               confidence = excluded.confidence, extremity = excluded.extremity",
        )
        .bind(d.get("id").and_then(|v| v.as_str()))
        .bind(&ev.tenant_id)
        .bind(d.get("fact").and_then(|v| v.as_str()))
        .bind(d.get("sources").map(Json))
        .bind(d.get("originators").map(Json))
        .bind(d.get("independent_originators").and_then(|v| v.as_i64()).unwrap_or(0) as i32)
        .bind(d.get("has_primary").and_then(|v| v.as_bool()).unwrap_or(false))
        .bind(d.get("claim_ids").map(Json))
        .bind(d.get("confidence").and_then(|v| v.as_f64()).unwrap_or(0.0))
        .bind(d.get("extremity").and_then(|v| v.as_str()).unwrap_or("notable"))
        .execute(&mut *tx)
        .await?;
    }

    if ev.typ == "cluster.snapshot" {
        // Projection-harvester (#39): a point-in-time snapshot of a cluster's corroboration state.
        // Idempotent per (cluster_id, calendar-day) — a second run the same day upserts, so a
        // trajectory accrues one row/day without duplicates (the events log has no on-conflict).
        let d = &ev.data;
        sqlx::query(
            "insert into cluster_snapshots \
             (cluster_id, tenant_id, snapshot_day, fact, independent_originators, has_primary, extremity, confidence, harvested_at, sources, originators, corrected, grounding) \
             values ($1, $2, ($3::timestamptz)::date, $4, $5, $6, $7, $8, $3::timestamptz, $9, $10, $11, $12) \
             on conflict (cluster_id, snapshot_day) do update set \
               fact = excluded.fact, independent_originators = excluded.independent_originators, \
               has_primary = excluded.has_primary, extremity = excluded.extremity, \
               confidence = excluded.confidence, harvested_at = excluded.harvested_at, \
               sources = excluded.sources, originators = excluded.originators, corrected = excluded.corrected, \
               grounding = excluded.grounding",
        )
        .bind(d.get("cluster_id").and_then(|v| v.as_str()))
        .bind(&ev.tenant_id)
        .bind(d.get("harvested_at").and_then(|v| v.as_str()))
        .bind(d.get("fact").and_then(|v| v.as_str()))
        .bind(d.get("independent_originators").and_then(|v| v.as_i64()).unwrap_or(0) as i32)
        .bind(d.get("has_primary").and_then(|v| v.as_bool()).unwrap_or(false))
        .bind(d.get("extremity").and_then(|v| v.as_str()).unwrap_or("notable"))
        .bind(d.get("confidence").and_then(|v| v.as_f64()).unwrap_or(0.0))
        // #39 closing the loop: the reputation fold needs per-source independence (sources +
        // collapsed originator groups); `corrected` carries the operator/reader refutation already
        // on member claims into resolve_outcome. Default empty/false for pre-enrichment payloads.
        .bind(Json(d.get("sources").cloned().unwrap_or_else(|| serde_json::json!([]))))
        .bind(Json(d.get("originators").cloned().unwrap_or_else(|| serde_json::json!([]))))
        .bind(d.get("corrected").and_then(|v| v.as_bool()).unwrap_or(false))
        // #228: the primary-source grounding verdict rides the trajectory so a contradiction
        // resolves the fact to REFUTED over time.
        .bind(d.get("grounding").and_then(|v| v.as_str()))
        .execute(&mut *tx)
        .await?;
    }

    if ev.typ == "cluster.grounded" {
        // Primary-source grounding (#228): record the verdict + the grounding-refined confidence on
        // the cluster the corroborate pass just (re)built. A no-op if the cluster id is already gone.
        let d = &ev.data;
        sqlx::query("update clusters set grounding = $2, confidence = $3 where id = $1")
            .bind(d.get("cluster_id").and_then(|v| v.as_str()))
            .bind(d.get("grounding").and_then(|v| v.as_str()))
            .bind(d.get("confidence").and_then(|v| v.as_f64()).unwrap_or(0.0))
            .execute(&mut *tx)
            .await?;
    }

    if ev.typ == "story.graph.rebuilt" {
        // Story graph (#42/#43/#44, P4): the builder rebuilds the whole graph each run and emits
        // it as one event. Project atomically — clear this tenant's prior graph, then insert the
        // nodes, typed edges, node↔cluster threading, and claim↔node links.
        let d = &ev.data;
        let empty: Vec<serde_json::Value> = Vec::new();
        let mut tx = pool.begin().await?;
        for t in ["story_nodes", "story_edges", "story_node_clusters", "claim_node_links"] {
            sqlx::query(&format!("delete from {t} where tenant_id = $1"))
                .bind(&ev.tenant_id)
                .execute(&mut *tx)
                .await?;
        }
        for n in d.get("nodes").and_then(|v| v.as_array()).unwrap_or(&empty) {
            sqlx::query(
                "insert into story_nodes (id, tenant_id, headline, entity_spine, first_seen, last_updated, cluster_count) \
                 values ($1, $2, $3, $4, $5, $6, $7) on conflict (id) do update set \
                   headline = excluded.headline, entity_spine = excluded.entity_spine, \
                   last_updated = excluded.last_updated, cluster_count = excluded.cluster_count",
            )
            .bind(n.get("id").and_then(|v| v.as_str()))
            .bind(&ev.tenant_id)
            .bind(n.get("headline").and_then(|v| v.as_str()))
            .bind(n.get("entity_spine").cloned().map(Json))
            .bind(n.get("first_seen").and_then(|v| v.as_f64()).unwrap_or(0.0))
            .bind(n.get("last_updated").and_then(|v| v.as_f64()).unwrap_or(0.0))
            .bind(n.get("cluster_count").and_then(|v| v.as_i64()).unwrap_or(0) as i32)
            .execute(&mut *tx)
            .await?;
        }
        for e in d.get("edges").and_then(|v| v.as_array()).unwrap_or(&empty) {
            sqlx::query("insert into story_edges (tenant_id, kind, from_id, to_id) values ($1, $2, $3, $4)")
                .bind(&ev.tenant_id)
                .bind(e.get("kind").and_then(|v| v.as_str()))
                .bind(e.get("from_id").and_then(|v| v.as_str()))
                .bind(e.get("to_id").and_then(|v| v.as_str()))
                .execute(&mut *tx)
                .await?;
        }
        for nc in d.get("node_clusters").and_then(|v| v.as_array()).unwrap_or(&empty) {
            sqlx::query("insert into story_node_clusters (tenant_id, node_id, cluster_id) values ($1, $2, $3)")
                .bind(&ev.tenant_id)
                .bind(nc.get("node_id").and_then(|v| v.as_str()))
                .bind(nc.get("cluster_id").and_then(|v| v.as_str()))
                .execute(&mut *tx)
                .await?;
        }
        for l in d.get("claim_node_links").and_then(|v| v.as_array()).unwrap_or(&empty) {
            sqlx::query("insert into claim_node_links (tenant_id, claim_id, node_id, cluster_id) values ($1, $2, $3, $4)")
                .bind(&ev.tenant_id)
                .bind(l.get("claim_id").and_then(|v| v.as_str()))
                .bind(l.get("node_id").and_then(|v| v.as_str()))
                .bind(l.get("cluster_id").and_then(|v| v.as_str()))
                .execute(&mut *tx)
                .await?;
        }
        tx.commit().await?;
    }

    // --- Admin / operator-console projections (P8, F3) -------------------------------------
    // Operator fixes are events; we fold them like any other and set `corrected` so the
    // pipeline (claims.classified) will not overwrite the fix on a re-run.
    if ev.typ == "admin.classification.corrected" {
        let d = &ev.data;
        sqlx::query(
            "update claims set kind = coalesce($2, kind), voice = coalesce($3, voice), \
             speaker = coalesce($4, speaker), corrected = true, corrected_at = now() \
             where id = $1::uuid",
        )
        .bind(d.get("target").and_then(|v| v.as_str()))
        .bind(d.get("kind").and_then(|v| v.as_str()))
        .bind(d.get("voice").and_then(|v| v.as_str()))
        .bind(d.get("speaker").and_then(|v| v.as_str()))
        .execute(&mut *tx)
        .await?;
    }

    if ev.typ == "admin.laundering.flagged" {
        let d = &ev.data;
        sqlx::query("update claims set laundering_flag = $2, corrected = true where id = $1::uuid")
            .bind(d.get("target").and_then(|v| v.as_str()))
            .bind(d.get("abuse").and_then(|v| v.as_str()))
            .execute(&mut *tx)
            .await?;
    }

    // A split / merge / move publishes cluster.removed for the superseded cluster(s) and
    // cluster.corroborated for the new one(s); the §5.5 recompute runs in the Python console
    // (where that logic lives). Here we only drop the superseded projection row.
    if ev.typ == "cluster.removed" {
        sqlx::query("delete from clusters where id = $1")
            .bind(ev.data.get("id").and_then(|v| v.as_str()))
            .execute(&mut *tx)
            .await?;
    }

    // A prompt edit (P8): append a new version and make it the active one. Append-only history,
    // one active row per key — the agents read the active row at run time.
    if ev.typ == "admin.prompt.updated" {
        let d = &ev.data;
        let key = d.get("key").and_then(|v| v.as_str());
        sqlx::query("update prompts set active = false where key = $1")
            .bind(key)
            .execute(&mut *tx)
            .await?;
        sqlx::query(
            "insert into prompts (key, version, text, active, reason, actor) values \
             ($1, coalesce((select max(version) from prompts where key = $1), 0) + 1, $2, true, \
             $3, $4)",
        )
        .bind(key)
        .bind(d.get("text").and_then(|v| v.as_str()))
        .bind(d.get("reason").and_then(|v| v.as_str()))
        .bind(d.get("actor").and_then(|v| v.as_str()))
        .execute(&mut *tx)
        .await?;
    }

    // --- Acquisition funnel (maat.press → console /acquisition) ---------------------------
    // The public marketing site publishes these (tenant_id="public"): a page view, a
    // "Download on the App Store" tap, and an optional launch-notify email. Fold view / click /
    // notify into acquisition_signals; dedupe emails into acquisition_signups. Funnel telemetry
    // rides the same append-only log as everything else (D5/D20).
    if ev.typ == "acquisition.page_viewed"
        || ev.typ == "acquisition.cta_clicked"
        || ev.typ == "acquisition.notify_requested"
    {
        let d = &ev.data;
        let kind = match ev.typ.as_str() {
            "acquisition.page_viewed" => "view",
            "acquisition.cta_clicked" => "click",
            _ => "notify",
        };
        sqlx::query(
            "insert into acquisition_signals \
             (tenant_id, kind, platform, path, referrer, utm_source, utm_medium, utm_campaign, \
              ua_family, visitor) \
             values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        )
        .bind(&ev.tenant_id)
        .bind(kind)
        .bind(d.get("platform").and_then(|v| v.as_str()))
        .bind(d.get("path").and_then(|v| v.as_str()))
        .bind(d.get("referrer").and_then(|v| v.as_str()))
        .bind(d.get("utm_source").and_then(|v| v.as_str()))
        .bind(d.get("utm_medium").and_then(|v| v.as_str()))
        .bind(d.get("utm_campaign").and_then(|v| v.as_str()))
        .bind(d.get("ua_family").and_then(|v| v.as_str()))
        .bind(d.get("visitor").and_then(|v| v.as_str()))
        .execute(&mut *tx)
        .await?;

        if ev.typ == "acquisition.notify_requested" {
            if let Some(email) = d.get("email").and_then(|v| v.as_str()) {
                sqlx::query(
                    "insert into acquisition_signups (email, platform, referrer, utm_source, beta) \
                     values ($1, $2, $3, $4, $5) \
                     on conflict (email) do update set last_seen = now(), \
                       hits = acquisition_signups.hits + 1, \
                       beta = acquisition_signups.beta or excluded.beta",
                )
                .bind(email)
                .bind(d.get("platform").and_then(|v| v.as_str()))
                .bind(d.get("referrer").and_then(|v| v.as_str()))
                .bind(d.get("utm_source").and_then(|v| v.as_str()))
                .bind(d.get("beta").and_then(|v| v.as_bool()).unwrap_or(false))
                .execute(&mut *tx)
                .await?;
            }
        }
    }

    tx.commit().await?;
    Ok(true)
}
