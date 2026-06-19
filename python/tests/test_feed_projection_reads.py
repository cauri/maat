"""Integration: the feed/stories serving path reads PROJECTIONS, never a full events-log scan (#283).

PR #343 made the feed/stories build run at most once per tick (the version cache). This closes the
other half: on a cache MISS the build must read off projections + bounded latest-per-entity reads,
not unbounded `select … from events` folds. Whole-corpus folds can't be unit-tested for that, so we
run the real FastAPI app against a throwaway Postgres and assert the OBSERVABLE result of each
bounded read:

  * denied (`admin.source.flagged`)  — the LATEST flag per source wins (allow→deny denies),
  * pending (`source.*`)             — the LATEST state per source wins (registered, never activated → held),
  * corroboration (`cluster_snapshots`) — reputation comes from the SNAPSHOT projection, NOT the
    legacy `cluster.corroborated` stream (a sentinel source planted only in the legacy event must
    never surface),
  * pivots (`claim.pivot`)           — the LATEST English gloss per claim wins.

Each entity is seeded with an older + a newer event, so a full-scan fold and the bounded read would
only agree if the bounded read truly picks the latest. SKIPS without Postgres on :5432 (raises in
CI) — same contract as test_routes_integration.py / test_console_api_integration.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ADMIN_URL = "postgresql://maat:maat@localhost:5432/maat"
TEST_DB = "maat_feed_projection_test"
TEST_URL = f"postgresql://maat:maat@localhost:5432/{TEST_DB}"
MIGRATIONS = sorted(
    (Path(__file__).resolve().parents[2] / "rust/crates/maat-kerneld/migrations").glob("*.sql")
)

# Three single-source stories: one denied, one pending, one served.
A_AFP, A_REU, A_NEW = "fpr-art-afp", "fpr-art-reuters", "fpr-art-newbie"
C_AFP, C_REU, C_NEW = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
CL_AFP = "fprclusterafp0000000000001"
CL_REU = "fprclusterreuters000000002"
CL_NEW = "fprclusternewbie0000000003"
NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _pg_up() -> bool:
    import asyncpg

    async def ping():
        c = await asyncpg.connect(ADMIN_URL)
        await c.close()

    try:
        asyncio.run(asyncio.wait_for(ping(), 3))
        return True
    except Exception:
        if os.environ.get("CI"):
            raise
        return False


pytestmark = pytest.mark.skipif(not _pg_up(), reason="no Postgres on :5432 (run make db-up)")


async def _ev(db, stream, typ, data) -> None:
    await db.execute(
        "insert into events (stream_id, type, data) values ($1, $2, $3::jsonb)",
        stream, typ, json.dumps(data),
    )


async def _snapshot(db, cluster_id, fact, source, art) -> None:
    await db.execute(
        "insert into cluster_snapshots (cluster_id, snapshot_day, fact, independent_originators, "
        "has_primary, extremity, confidence, harvested_at, sources, originators, corrected, "
        "grounding) values ($1, ($2::timestamptz)::date, $3, $4, $5, $6, $7, $2::timestamptz, "
        "$8, $9, $10, $11)",
        cluster_id, NOW, fact, 1, False, "notable", 0.7,
        json.dumps([source]), json.dumps([[art]]), False, None,
    )


async def _setup() -> None:
    import asyncpg

    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f"drop database if exists {TEST_DB}")
    await admin.execute(f"create database {TEST_DB}")
    await admin.close()

    db = await asyncpg.connect(TEST_URL)
    try:
        for m in MIGRATIONS:
            await db.execute(m.read_text())
        for aid, src in ((A_AFP, "afp.example"), (A_REU, "reuters.example"), (A_NEW, "newbie.example")):
            await db.execute(
                "insert into articles (id, title, source, url, language, body) "
                "values ($1,$2,$3,$4,$5,$6)",
                aid, "t", src, "http://x", "en", "body",
            )
        # The reuters claim text matches its cluster fact so the English-pivot gloss resolves.
        for cid, aid, txt in (
            (C_AFP, A_AFP, "afp only fact"),
            (C_REU, A_REU, "rates up confirmed"),
            (C_NEW, A_NEW, "newbie fact"),
        ):
            await db.execute(
                "insert into claims (id, article_id, text, voice, kind) values ($1::uuid,$2,$3,$4,$5)",
                cid, aid, txt, "own", "fact",
            )
        for cl, fact, art, cid in (
            (CL_AFP, "afp only fact", A_AFP, C_AFP),
            (CL_REU, "rates up confirmed", A_REU, C_REU),
            (CL_NEW, "newbie fact", A_NEW, C_NEW),
        ):
            await db.execute(
                "insert into clusters (id, fact, sources, originators, independent_originators, "
                "has_primary, claim_ids, confidence, extremity) values ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                cl, fact, json.dumps([_src_of(art)]), json.dumps([[art]]), 1, False,
                json.dumps([str(cid)]), 0.7, "notable",
            )

        # Corroboration trajectory: the SNAPSHOT projection carries the real per-cluster sources.
        await _snapshot(db, CL_AFP, "afp only fact", "afp.example", A_AFP)
        await _snapshot(db, CL_REU, "rates up confirmed", "reuters.example", A_REU)
        await _snapshot(db, CL_NEW, "newbie fact", "newbie.example", A_NEW)
        # A legacy cluster.corroborated event with a SENTINEL source: the serving path must read the
        # snapshot projection, so this source must NEVER appear in the reputation map.
        await _ev(db, CL_REU, "cluster.corroborated", {
            "id": CL_REU, "fact": "rates up confirmed", "sources": ["legacy-sentinel.example"],
            "originators": [[A_REU]], "independent_originators": 1, "has_primary": False,
            "extremity": "notable", "confidence": 0.7, "claim_ids": [],
        })

        # admin.source.flagged — latest wins. afp: allow→DENY (denied). reuters: deny→ALLOW (served).
        await _ev(db, "afp.example", "admin.source.flagged", {"source": "afp.example", "status": "allow"})
        await _ev(db, "afp.example", "admin.source.flagged", {"source": "afp.example", "status": "deny"})
        await _ev(db, "reuters.example", "admin.source.flagged", {"source": "reuters.example", "status": "deny"})
        await _ev(db, "reuters.example", "admin.source.flagged", {"source": "reuters.example", "status": "allow"})

        # source registry — latest wins. reuters: registered→ACTIVE (served). newbie: registered (PENDING).
        await _ev(db, "reuters.example", "source.registered",
                  {"source": "reuters.example", "state": "registered", "provider": "rss", "at": "t1"})
        await _ev(db, "reuters.example", "source.state_changed",
                  {"source": "reuters.example", "state": "active", "provider": "rss", "at": "t2"})
        await _ev(db, "newbie.example", "source.registered",
                  {"source": "newbie.example", "state": "registered", "provider": "rss", "at": "t1"})

        # story.geo_inferred — latest wins (US→FR); exercises the bounded geo read on the miss path.
        await _ev(db, CL_REU, "story.geo_inferred", {"cluster_id": CL_REU, "country": "US"})
        await _ev(db, CL_REU, "story.geo_inferred", {"cluster_id": CL_REU, "country": "FR"})

        # claim.pivot — latest wins. The reuters claim's English gloss: "Old gloss"→"New gloss".
        await _ev(db, C_REU, "claim.pivot", {"claim_id": C_REU, "text_en": "Old gloss"})
        await _ev(db, C_REU, "claim.pivot", {"claim_id": C_REU, "text_en": "New gloss"})
    finally:
        await db.close()


def _src_of(art: str) -> str:
    return {A_AFP: "afp.example", A_REU: "reuters.example", A_NEW: "newbie.example"}[art]


async def _teardown() -> None:
    import asyncpg

    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f"drop database if exists {TEST_DB}")
    await admin.close()


async def _run_all() -> None:
    import httpx

    from maat.db import get_pool
    from maat.web import app as appmod

    await _setup()
    pool = await get_pool(TEST_URL)
    appmod.app.state.pool = pool
    appmod.app.state.nats = None  # the served read routes don't need the bus
    try:
        transport = httpx.ASGITransport(app=appmod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://fpr") as ac:
            # --- /api/v2/feed: denied + pending bounded reads, latest-per-source ---------------
            feed = (await ac.get("/api/v2/feed")).json()
            ids = {s["id"] for s in feed["stories"]}
            assert ids == {CL_REU}, f"expected only the served reuters story, got {ids}"
            # afp DENIED (allow→deny, latest wins) so its sole-source story is dropped…
            assert CL_AFP not in ids
            # …and newbie PENDING (registered, never activated) so its story is held.
            assert CL_NEW not in ids

            # --- /api/v2/feed?accuracy=1&reputation=1: corroboration off the SNAPSHOT projection --
            ann = (await ac.get("/api/v2/feed?accuracy=1&reputation=1")).json()
            rep = ann.get("source_reputation", {})
            assert "reuters.example" in rep, f"snapshot-sourced reputation missing: {rep}"
            # The sentinel planted ONLY in the legacy cluster.corroborated stream must not surface —
            # proves the serving path reads cluster_snapshots, not the raw event scan.
            assert "legacy-sentinel.example" not in rep, "serving read the legacy event scan, not the projection"
            assert all("accuracy_state" in s for s in ann["stories"]), "accuracy annotation missing"

            # --- /api/v2/stories: pivot bounded read, latest English gloss per claim -------------
            stories = (await ac.get("/api/v2/stories")).json()
            reu = next(s for s in stories["stories"] if s["headline_orig"] == "rates up confirmed")
            assert reu["headline"] == "New gloss", f"pivot latest-wins failed: {reu['headline']!r}"

            # --- /api/v2/source-preferences: learned prefs fold over the snapshot trajectory ------
            prefs_resp = await ac.get("/api/v2/source-preferences")
            assert prefs_resp.status_code == 200
            assert isinstance(prefs_resp.json().get("ranked"), list)
    finally:
        await pool.close()
        await _teardown()


def test_feed_serving_reads_projections_not_event_scans():
    asyncio.run(_run_all())
