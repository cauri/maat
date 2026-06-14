//! `maat-kerneld` — the deterministic spine (D20).
//!
//! Single writer over the canonical store: consumes events from NATS, appends them to the
//! append-only `events` log in Postgres (the source of truth), and folds the minimal
//! projections. Mechanical tools and richer folds grow from here. Run with `--smoke` to
//! self-test the whole spine (publish one event, record it, check the projection).

use anyhow::Result;
use futures::StreamExt;
use serde::{Deserialize, Serialize};
use sqlx::postgres::PgPoolOptions;
use sqlx::types::Json;
use sqlx::PgPool;

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

    let nats = async_nats::connect(&nats_url).await?;
    tracing::info!("nats connected: {nats_url}");

    let smoke = std::env::args().any(|a| a == "--smoke");
    let mut sub = nats.subscribe("maat.events.>").await?;
    tracing::info!("subscribed to maat.events.>");

    if smoke {
        let ev = EventEnvelope {
            stream_id: "smoke-article-1".to_string(),
            typ: "article.ingested".to_string(),
            data: serde_json::json!({
                "title": "Smoke test", "source": "local", "language": "en", "body": "hello"
            }),
            tenant_id: "cauri".to_string(),
        };
        nats.publish("maat.events.article.ingested", serde_json::to_vec(&ev)?.into())
            .await?;
        nats.flush().await?;
        tracing::info!("smoke event published");
    }

    while let Some(msg) = sub.next().await {
        match serde_json::from_slice::<EventEnvelope>(&msg.payload) {
            Ok(ev) => {
                if let Err(e) = record_and_project(&pool, &ev).await {
                    tracing::error!("project failed (type={}): {e}", ev.typ);
                    continue;
                }
                tracing::info!("recorded event type={} stream={}", ev.typ, ev.stream_id);
                if smoke {
                    let n: i64 = sqlx::query_scalar("select count(*) from articles")
                        .fetch_one(&pool)
                        .await?;
                    tracing::info!("smoke ok: articles projected = {n}");
                    break;
                }
            }
            Err(e) => tracing::warn!("bad event payload: {e}"),
        }
    }
    Ok(())
}

/// Append the event to the log (source of truth), then fold the minimal projection.
async fn record_and_project(pool: &PgPool, ev: &EventEnvelope) -> Result<()> {
    sqlx::query("insert into events (stream_id, type, data, tenant_id) values ($1, $2, $3, $4)")
        .bind(&ev.stream_id)
        .bind(&ev.typ)
        .bind(Json(&ev.data))
        .bind(&ev.tenant_id)
        .execute(pool)
        .await?;

    if ev.typ == "article.ingested" {
        sqlx::query(
            "insert into articles (id, tenant_id, title, source, url, language, body) \
             values ($1, $2, $3, $4, $5, $6, $7) \
             on conflict (id) do update set title = excluded.title, body = excluded.body",
        )
        .bind(&ev.stream_id)
        .bind(&ev.tenant_id)
        .bind(ev.data.get("title").and_then(|v| v.as_str()))
        .bind(ev.data.get("source").and_then(|v| v.as_str()))
        .bind(ev.data.get("url").and_then(|v| v.as_str()))
        .bind(ev.data.get("language").and_then(|v| v.as_str()))
        .bind(ev.data.get("body").and_then(|v| v.as_str()))
        .execute(pool)
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
            .execute(pool)
            .await?;
        }
    }

    Ok(())
}
