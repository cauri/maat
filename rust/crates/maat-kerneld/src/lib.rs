//! `maat-kerneld` core — the fold, plus the maintenance ops that replay it (#287).
//!
//! The fold (`project`) is the SINGLE place an event becomes projection state. Both the live
//! JetStream consumer (`main`) and the offline `rebuild` drive it, so there is exactly one
//! projection truth (D20) — a from-Postgres rebuild can never drift from live folding because it
//! *is* the same code.
//!
//! Two logs back the system (see `docs/spikes/events-log-snapshots.md`): the append-only `events`
//! table is the COMPLETE source of truth and grows forever, while the JetStream `MAAT_EVENTS` stream
//! is only a 30-day / 2 GiB delivery window. So a rebuild must replay the Postgres log.
//! `snapshot`/`rebuild` keep that bounded by snapshot cadence, not total history: a snapshot clones
//! the projection tables at an event-id watermark, and rebuild restores the latest snapshot then
//! replays only events past it.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use sqlx::types::Json;
use sqlx::{PgPool, Postgres, Transaction};

#[derive(Debug, Serialize, Deserialize)]
pub struct EventEnvelope {
    pub stream_id: String,
    #[serde(rename = "type")]
    pub typ: String,
    pub data: serde_json::Value,
    #[serde(default = "default_tenant")]
    pub tenant_id: String,
}

pub fn default_tenant() -> String {
    "cauri".to_string()
}

/// Every table that is a pure fold of the event log — TRUNCATEd then replayed on rebuild, cloned on
/// snapshot. Ordered parents-before-children so a snapshot RESTORE satisfies FKs (`claims.article_id
/// references articles`).
///
/// Deliberately EXCLUDED: `events` (the log itself, the source of truth), `dead_letters`
/// (operational — written on fold failure, not folded *from* an event, so not reconstructible by
/// replay), `embedding_cache` (a derived cache that lives OUTSIDE the log by design, #286 / 0017),
/// and `projection_snapshots` (this feature's own manifest). See the spike doc.
pub const PROJECTION_TABLES: &[&str] = &[
    "articles",
    "claims",
    "clusters",
    "cluster_snapshots",
    "story_nodes",
    "story_edges",
    "story_node_clusters",
    "claim_node_links",
    "claim_relations",
    "acquisition_signals",
    "acquisition_signups",
    "prompts",
];

/// Apply the migrations embedded from `migrations/` (resolved against the crate root).
pub async fn run_migrations(pool: &PgPool) -> Result<()> {
    sqlx::migrate!().run(pool).await?;
    Ok(())
}

/// Live path: append the event to the log (deduped by JetStream sequence), then fold it — all in
/// ONE transaction keyed by `js_seq`. Returns `Ok(true)` if newly recorded, `Ok(false)` if the
/// message was already processed on a prior delivery (a duplicate → exactly-once no-op).
pub async fn record_and_project(pool: &PgPool, ev: &EventEnvelope, js_seq: i64) -> Result<bool> {
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
    project(pool, &mut tx, ev).await?;
    tx.commit().await?;
    Ok(true)
}

/// Fold one event into the projections, in the caller's transaction. The ONE fold: the live path
/// calls it after appending to `events`; `rebuild` calls it per replayed event (no re-append).
/// `pool` is needed only by the story-graph branches, which open their own nested transaction
/// (pre-existing behaviour, preserved verbatim).
pub async fn project(
    pool: &PgPool,
    tx: &mut Transaction<'_, Postgres>,
    ev: &EventEnvelope,
) -> Result<()> {
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
        .execute(&mut **tx)
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
            .execute(&mut **tx)
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
            .execute(&mut **tx)
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
        .execute(&mut **tx)
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
        .execute(&mut **tx)
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
            .execute(&mut **tx)
            .await?;
    }

    if ev.typ == "claim.related" {
        // Automated contradiction detection (#229): record the NLI relation between two claims.
        // Idempotent per (claim_a, claim_b, relation); a re-run just refreshes the score.
        let d = &ev.data;
        sqlx::query(
            "insert into claim_relations (tenant_id, claim_a, claim_b, relation, score) \
             values ($1, $2::uuid, $3::uuid, $4, $5) \
             on conflict (claim_a, claim_b, relation) do update set score = excluded.score",
        )
        .bind(&ev.tenant_id)
        .bind(d.get("claim_a").and_then(|v| v.as_str()))
        .bind(d.get("claim_b").and_then(|v| v.as_str()))
        .bind(d.get("relation").and_then(|v| v.as_str()))
        .bind(d.get("score").and_then(|v| v.as_f64()).unwrap_or(0.0))
        .execute(&mut **tx)
        .await?;
    }

    if ev.typ == "claim.disputed" {
        // #229: a stronger contradicting claim refutes this one — flag it so the harvester folds it
        // into the cluster's `corrected` (→ REFUTED), the same path #227 built for operator fixes.
        sqlx::query("update claims set disputed = true where id = $1::uuid")
            .bind(ev.data.get("claim_id").and_then(|v| v.as_str()))
            .execute(&mut **tx)
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

    if ev.typ == "story.graph.delta" {
        // Story graph at scale (#42): the builder folds only the NEW clusters each tick and emits
        // the difference, chunked under NATS's payload cap (the whole-graph rebuild above outgrew
        // it). Apply with insert/upsert — NO full replace — so the projection grows incrementally.
        // A `reset` chunk truncates first (a deliberate full rebuild streamed in chunks); every
        // insert is idempotent (on conflict do nothing) so a re-delivered chunk can't duplicate.
        let d = &ev.data;
        let empty: Vec<serde_json::Value> = Vec::new();
        let mut tx = pool.begin().await?;
        if d.get("reset").and_then(|v| v.as_bool()).unwrap_or(false) {
            for t in ["story_nodes", "story_edges", "story_node_clusters", "claim_node_links"] {
                sqlx::query(&format!("delete from {t} where tenant_id = $1"))
                    .bind(&ev.tenant_id)
                    .execute(&mut *tx)
                    .await?;
            }
        }
        for n in d.get("nodes").and_then(|v| v.as_array()).unwrap_or(&empty) {
            // Upsert: a touched node carries its updated count/centroid/last_updated. The centroid is
            // omitted once a node settles, so coalesce keeps the last one we stored.
            sqlx::query(
                "insert into story_nodes (id, tenant_id, headline, entity_spine, first_seen, last_updated, cluster_count, topic_embedding) \
                 values ($1, $2, $3, $4, $5, $6, $7, $8) on conflict (id) do update set \
                   headline = excluded.headline, entity_spine = excluded.entity_spine, \
                   last_updated = excluded.last_updated, cluster_count = excluded.cluster_count, \
                   topic_embedding = coalesce(excluded.topic_embedding, story_nodes.topic_embedding)",
            )
            .bind(n.get("id").and_then(|v| v.as_str()))
            .bind(&ev.tenant_id)
            .bind(n.get("headline").and_then(|v| v.as_str()))
            .bind(n.get("entity_spine").cloned().map(Json))
            .bind(n.get("first_seen").and_then(|v| v.as_f64()).unwrap_or(0.0))
            .bind(n.get("last_updated").and_then(|v| v.as_f64()).unwrap_or(0.0))
            .bind(n.get("cluster_count").and_then(|v| v.as_i64()).unwrap_or(0) as i32)
            .bind(n.get("topic_embedding").cloned().map(Json))
            .execute(&mut *tx)
            .await?;
        }
        for e in d.get("edges").and_then(|v| v.as_array()).unwrap_or(&empty) {
            sqlx::query("insert into story_edges (tenant_id, kind, from_id, to_id) values ($1, $2, $3, $4) on conflict do nothing")
                .bind(&ev.tenant_id)
                .bind(e.get("kind").and_then(|v| v.as_str()))
                .bind(e.get("from_id").and_then(|v| v.as_str()))
                .bind(e.get("to_id").and_then(|v| v.as_str()))
                .execute(&mut *tx)
                .await?;
        }
        for nc in d.get("node_clusters").and_then(|v| v.as_array()).unwrap_or(&empty) {
            sqlx::query("insert into story_node_clusters (tenant_id, node_id, cluster_id) values ($1, $2, $3) on conflict do nothing")
                .bind(&ev.tenant_id)
                .bind(nc.get("node_id").and_then(|v| v.as_str()))
                .bind(nc.get("cluster_id").and_then(|v| v.as_str()))
                .execute(&mut *tx)
                .await?;
        }
        for l in d.get("claim_node_links").and_then(|v| v.as_array()).unwrap_or(&empty) {
            sqlx::query("insert into claim_node_links (tenant_id, claim_id, node_id, cluster_id) values ($1, $2, $3, $4) on conflict do nothing")
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
        .execute(&mut **tx)
        .await?;
    }

    if ev.typ == "admin.laundering.flagged" {
        let d = &ev.data;
        sqlx::query("update claims set laundering_flag = $2, corrected = true where id = $1::uuid")
            .bind(d.get("target").and_then(|v| v.as_str()))
            .bind(d.get("abuse").and_then(|v| v.as_str()))
            .execute(&mut **tx)
            .await?;
    }

    // A split / merge / move publishes cluster.removed for the superseded cluster(s) and
    // cluster.corroborated for the new one(s); the §5.5 recompute runs in the Python console
    // (where that logic lives). Here we only drop the superseded projection row.
    if ev.typ == "cluster.removed" {
        sqlx::query("delete from clusters where id = $1")
            .bind(ev.data.get("id").and_then(|v| v.as_str()))
            .execute(&mut **tx)
            .await?;
    }

    // A prompt edit (P8): append a new version and make it the active one. Append-only history,
    // one active row per key — the agents read the active row at run time.
    if ev.typ == "admin.prompt.updated" {
        let d = &ev.data;
        let key = d.get("key").and_then(|v| v.as_str());
        sqlx::query("update prompts set active = false where key = $1")
            .bind(key)
            .execute(&mut **tx)
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
        .execute(&mut **tx)
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
        .execute(&mut **tx)
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
                .execute(&mut **tx)
                .await?;
            }
        }
    }

    Ok(())
}

// ── #287: read-model snapshots + bounded rebuild ─────────────────────────────────────────────────

/// How many snapshots to retain; the rest are pruned. Override with `MAAT_SNAPSHOT_KEEP`.
fn snapshot_keep() -> i64 {
    std::env::var("MAAT_SNAPSHOT_KEEP")
        .ok()
        .and_then(|v| v.parse().ok())
        .filter(|n| *n >= 1)
        .unwrap_or(3)
}

/// Capture a point-in-time copy of every projection table at the current event-id watermark, so a
/// later [`rebuild`] replays only events past it. Each table is cloned into a fresh `snap_<watermark>`
/// schema and a manifest row is written; snapshots beyond the keep-N are pruned.
///
/// RUN WITH THE LIVE CONSUMER STOPPED: with no in-flight event transactions, `max(events.id)` is a
/// gap-free high-water mark, so "replay everything past the watermark" can't miss a late-committing
/// event below it. Returns the watermark captured.
pub async fn snapshot(pool: &PgPool) -> Result<i64> {
    let watermark: i64 = sqlx::query_scalar("select coalesce(max(id), 0) from events")
        .fetch_one(pool)
        .await?;
    let js_seq: Option<i64> = sqlx::query_scalar("select max(js_seq) from events")
        .fetch_one(pool)
        .await?;
    let event_count: i64 = sqlx::query_scalar("select count(*) from events")
        .fetch_one(pool)
        .await?;
    let schema = format!("snap_{watermark}");

    let mut tx = pool.begin().await?;
    // A re-run at the same watermark replaces the prior copy.
    sqlx::query(&format!("drop schema if exists {schema} cascade"))
        .execute(&mut *tx)
        .await?;
    sqlx::query(&format!("create schema {schema}"))
        .execute(&mut *tx)
        .await?;
    let mut manifest: Vec<serde_json::Value> = Vec::new();
    for t in PROJECTION_TABLES {
        // `create table as table` copies the rows (no constraints/indexes — a staging copy is all we
        // need); restore re-inserts into the live, constrained table.
        sqlx::query(&format!("create table {schema}.{t} as table public.{t}"))
            .execute(&mut *tx)
            .await?;
        let rows: i64 = sqlx::query_scalar(&format!("select count(*) from {schema}.{t}"))
            .fetch_one(&mut *tx)
            .await?;
        manifest.push(serde_json::json!({ "table": t, "rows": rows }));
    }
    sqlx::query(
        "insert into projection_snapshots (watermark, js_seq, schema_name, tables, event_count) \
         values ($1, $2, $3, $4, $5)",
    )
    .bind(watermark)
    .bind(js_seq)
    .bind(&schema)
    .bind(Json(serde_json::Value::Array(manifest)))
    .bind(event_count)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;

    // Retention: drop everything older than the most recent keep-N (schema + manifest row).
    let stale: Vec<(i64, String)> = sqlx::query_as(
        "select id, schema_name from projection_snapshots order by watermark desc offset $1",
    )
    .bind(snapshot_keep())
    .fetch_all(pool)
    .await?;
    for (id, sch) in stale {
        sqlx::query(&format!("drop schema if exists {sch} cascade"))
            .execute(pool)
            .await?;
        sqlx::query("delete from projection_snapshots where id = $1")
            .bind(id)
            .execute(pool)
            .await?;
    }
    Ok(watermark)
}

/// Restore one projection table from a snapshot schema, by the columns the two share (live order).
/// Inserting an explicit column list (rather than `select *`) lets a snapshot taken under an older
/// schema restore cleanly after additive migrations — new live columns just take their defaults.
async fn restore_table(
    tx: &mut Transaction<'_, Postgres>,
    schema: &str,
    table: &str,
) -> Result<()> {
    let cols: Vec<String> = sqlx::query_scalar(
        "select column_name from information_schema.columns \
         where table_schema = $1 and table_name = $2 \
           and column_name in ( \
             select column_name from information_schema.columns \
             where table_schema = 'public' and table_name = $2) \
         order by ordinal_position",
    )
    .bind(schema)
    .bind(table)
    .fetch_all(&mut **tx)
    .await?;
    if cols.is_empty() {
        return Ok(());
    }
    let list = cols
        .iter()
        .map(|c| format!("\"{c}\""))
        .collect::<Vec<_>>()
        .join(", ");
    sqlx::query(&format!(
        "insert into public.{table} ({list}) select {list} from {schema}.{table}"
    ))
    .execute(&mut **tx)
    .await?;

    // Fast-forward any sequence-backed column past the restored max, so replay inserts that use the
    // default (e.g. a new cluster_snapshots/acquisition_signals row) never collide with a restored id.
    let seq_cols: Vec<String> = sqlx::query_scalar(
        "select column_name from information_schema.columns \
         where table_schema = 'public' and table_name = $1 \
           and pg_get_serial_sequence('public.' || $1, column_name) is not null",
    )
    .bind(table)
    .fetch_all(&mut **tx)
    .await?;
    for col in seq_cols {
        sqlx::query(&format!(
            "select setval(pg_get_serial_sequence('public.{table}', '{col}'), \
                    greatest(coalesce((select max(\"{col}\") from public.{table}), 0), 1))"
        ))
        .execute(&mut **tx)
        .await?;
    }
    Ok(())
}

/// Rebuild every projection from the COMPLETE Postgres event log: restore the latest snapshot (if
/// any), then replay events past its watermark through [`project`]. Rebuild cost is bounded by
/// snapshot cadence, not total history (#287).
///
/// RUN WITH THE LIVE CONSUMER STOPPED (see [`snapshot`]). Restart-safe: it always truncates +
/// restores first, so re-running after an interrupted replay simply starts over. Returns
/// `(restored_from_watermark, events_replayed)`.
pub async fn rebuild(pool: &PgPool) -> Result<(i64, i64)> {
    let latest: Option<(i64, String)> = sqlx::query_as(
        "select watermark, schema_name from projection_snapshots order by watermark desc limit 1",
    )
    .fetch_optional(pool)
    .await?;

    // 1. Baseline: truncate all projections, then reload from the snapshot if there is one.
    let mut tx = pool.begin().await?;
    let truncate_list = PROJECTION_TABLES
        .iter()
        .map(|t| format!("public.{t}"))
        .collect::<Vec<_>>()
        .join(", ");
    sqlx::query(&format!("truncate {truncate_list} cascade"))
        .execute(&mut *tx)
        .await?;
    let start = match &latest {
        Some((w, schema)) => {
            for t in PROJECTION_TABLES {
                restore_table(&mut tx, schema, t).await?;
            }
            *w
        }
        None => 0,
    };
    tx.commit().await?;

    // 2. Replay the tail from the complete Postgres log, one event per transaction (matching the
    //    live path's semantics), batched to bound memory.
    let mut replayed = 0i64;
    let mut last_id = start;
    loop {
        let batch: Vec<(i64, String, String, serde_json::Value, String)> = sqlx::query_as(
            "select id, stream_id, type, data, tenant_id from events \
             where id > $1 order by id limit 1000",
        )
        .bind(last_id)
        .fetch_all(pool)
        .await?;
        if batch.is_empty() {
            break;
        }
        for (id, stream_id, typ, data, tenant_id) in batch {
            let ev = EventEnvelope { stream_id, typ, data, tenant_id };
            let mut tx = pool.begin().await?;
            project(pool, &mut tx, &ev)
                .await
                .with_context(|| format!("replay failed at event id={id} type={}", ev.typ))?;
            tx.commit().await?;
            last_id = id;
            replayed += 1;
        }
    }
    Ok((start, replayed))
}
