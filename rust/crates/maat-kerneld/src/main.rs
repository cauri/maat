//! `maat-kerneld` — the deterministic spine (D20).
//!
//! Single writer over the canonical store: consumes events from NATS, appends them to the
//! append-only `events` log in Postgres (the source of truth), and folds the minimal projections.
//! The fold itself lives in the library (`lib.rs`) so the offline `--rebuild` replays the exact
//! same code (#287). Run with `--smoke` to self-test the whole spine (publish one event, record it,
//! check the projection); `--snapshot` / `--rebuild` are one-shot maintenance ops (see below).

use anyhow::Result;
use async_nats::jetstream::{self, consumer::pull, consumer::AckPolicy, stream};
use futures::StreamExt;
use maat_kerneld::{rebuild, record_and_project, run_migrations, snapshot, EventEnvelope};
use sqlx::postgres::PgPoolOptions;
use std::time::Duration;

/// Durable, file-backed JetStream stream + consumer so a kerneld restart/outage REPLAYS events
/// rather than dropping them — a core-NATS subscription keeps nothing for an offline subscriber,
/// which silently lost a real beta signup (2026-06-16).
const STREAM_NAME: &str = "MAAT_EVENTS";
const SUBJECT: &str = "maat.events.>";
const DURABLE: &str = "kerneld";

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    let db_url = std::env::var("DATABASE_URL")?;

    let pool = PgPoolOptions::new().max_connections(5).connect(&db_url).await?;
    run_migrations(&pool).await?;
    tracing::info!("postgres connected + migrated");

    // One-shot maintenance ops (#287): snapshot the projections / rebuild them from the Postgres
    // log. Run with the live consumer STOPPED — they need a gap-free watermark — and they EXIT when
    // done rather than starting to consume. See docs/spikes/events-log-snapshots.md.
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--snapshot") {
        let w = snapshot(&pool).await?;
        tracing::info!("snapshot captured at watermark {w}");
        return Ok(());
    }
    if args.iter().any(|a| a == "--rebuild") {
        let (from, n) = rebuild(&pool).await?;
        tracing::info!("rebuild complete: restored snapshot@{from}, replayed {n} events");
        return Ok(());
    }

    let nats_url = std::env::var("NATS_URL").unwrap_or_else(|_| "nats://localhost:4222".to_string());
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

    let smoke = args.iter().any(|a| a == "--smoke");
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
                        .bind(sqlx::types::Json(&ev.data))
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
