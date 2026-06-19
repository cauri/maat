"""Integration smoke for the console's DB-backed routes (P8).

Runs the real FastAPI app against a throwaway Postgres database (migrations applied, a few
rows seeded) and asserts every read route renders. This reaches what the pure-function unit
tests cannot: the route SQL + glue — joins, the `any($1::uuid[])` cast on cluster members,
the uuid id binding, the distinct-on config/source queries.

It SKIPS when no Postgres is on :5432, so it adds coverage locally (and in CI once a Postgres
service is enabled) without breaking the deterministic gate today. NATS is left disconnected
(app.state.nats = None) — the read routes don't need it. Run: `make db-up` then `pytest`.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

ADMIN_URL = "postgresql://maat:maat@localhost:5432/maat"
TEST_DB = "maat_admin_console_test"
TEST_URL = f"postgresql://maat:maat@localhost:5432/{TEST_DB}"
MIGRATIONS = sorted(
    (Path(__file__).resolve().parents[2] / "rust/crates/maat-kerneld/migrations").glob("*.sql")
)

ART = "itg-article-1"
CL1, CL2 = str(uuid.uuid4()), str(uuid.uuid4())
CLUSTER = "itgcluster000000000000001"


def _pg_up() -> bool:
    import asyncpg

    async def ping():
        c = await asyncpg.connect(ADMIN_URL)
        await c.close()

    try:
        asyncio.run(asyncio.wait_for(ping(), 3))
        return True
    except Exception:
        # In CI the Postgres service must be reachable — a silent skip there would defeat the
        # whole point of gating on it. Locally (no CI), skip gracefully when there's no DB.
        if os.environ.get("CI"):
            raise
        return False


pytestmark = pytest.mark.skipif(not _pg_up(), reason="no Postgres on :5432 (run make db-up)")


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
        await db.execute(
            "insert into articles (id, title, source, url, language, body) "
            "values ($1, $2, $3, $4, $5, $6)",
            ART, "ECB raises rates", "European Central Bank", "http://x", "en",
            "The ECB raised rates today.",
        )
        for cid, txt in ((CL1, "rates up"), (CL2, "rates up, our desk confirms")):
            await db.execute(
                "insert into claims (id, article_id, text, voice, kind) "
                "values ($1::uuid, $2, $3, $4, $5)",
                cid, ART, txt, "own", "fact",
            )
        await db.execute(
            "insert into clusters (id, fact, sources, originators, independent_originators, "
            "has_primary, claim_ids, confidence, extremity) "
            "values ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            CLUSTER, "rates up", json.dumps(["European Central Bank"]), json.dumps([[ART]]),
            1, True, json.dumps([CL1, CL2]), 0.75, "notable",
        )
        for stream, typ, data in (
            (CL1, "admin.classification.corrected",
             {"target": CL1, "actor": "operator", "reason": "test", "kind": "fact"}),
            ("gate.floor", "admin.threshold.changed",
             {"target": "gate.floor", "key": "gate.floor", "value": "0.35", "reason": "test"}),
            ("AFP", "admin.source.flagged",
             {"target": "AFP", "source": "AFP", "status": "deny", "reason": "wire"}),
        ):
            await db.execute(
                "insert into events (stream_id, type, data) values ($1, $2, $3::jsonb)",
                stream, typ, json.dumps(data),
            )
    finally:
        await db.close()


async def _teardown() -> None:
    import asyncpg

    admin = await asyncpg.connect(ADMIN_URL)
    await admin.execute(f"drop database if exists {TEST_DB}")
    await admin.close()


async def _run_all() -> None:
    import httpx

    from maat.db import get_pool
    from maat.web import app as appmod

    # Keep this harness LLM-free + deterministic: the /prompts/test smoke must take the
    # graceful no-key path, never a live (paid) call, even if keys are in the shell env.
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("MISTRAL_API_KEY", None)
    await _setup()
    pool = await get_pool(TEST_URL)
    appmod.app.state.pool = pool
    appmod.app.state.nats = None  # read routes don't need the bus
    try:
        transport = httpx.ASGITransport(app=appmod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://itg") as ac:
            checks = [
                ("/", "Maat"),
                ("/runs", "Activity"),
                ("/clocks", "Updates"),  # A1 — ingestion clock status off the event log
                ("/config", "0.35"),  # the seeded threshold proposal surfaces
                ("/sources", "European Central Bank"),  # registry off articles
                ("/eval", "Quality"),
                ("/prompts", "Prompts"),  # P8 prompt editor renders (seeds when no edits)
                ("/audit", "fixed a claim"),  # admin.* rendered as plain action labels
                ("/acquisition", "Acquisition"),  # marketing funnel page renders (zeros, no rows)
                (f"/cluster/{CLUSTER}", "rates up"),  # any($1::uuid[]) member fetch works
                (f"/claim/{CL1}", "rates up"),  # uuid id binding works
            ]
            for path, must in checks:
                r = await ac.get(path)
                assert r.status_code == 200, f"{path} -> {r.status_code}"
                assert must in r.text, f"{path} missing {must!r}"
            missing = await ac.get("/cluster/does-not-exist")
            assert missing.status_code == 200 and "No such cluster" in missing.text

            # POST handlers: with NATS down, _publish is a safe no-op and the route redirects
            # (303). This still exercises form parsing, handler logic, and — for split — the
            # recompute SQL path (_claimrows' any($1::uuid[]) + corroborate_fixed).
            posts = [
                ("/config/set", {"key": "gate.floor", "value": "0.5", "reason": "smoke"}),
                ("/sources/flag", {"source": "AFP", "deny": "1", "reason": "smoke"}),
                ("/clocks/set", {"clock": "ingestion", "paused": "true", "reason": "smoke"}),
                (f"/claim/{CL1}/correct", {"kind": "projection", "reason": "smoke"}),
                ("/prompts/save", {"key": "extremity", "text": "rate {claim}", "reason": "smoke"}),
                # eval-on-change with no API key set -> graceful redirect, no LLM call
                ("/prompts/test", {"key": "extremity", "text": "rate {claim}"}),
                (f"/cluster/{CLUSTER}/split", {"claim_ids": CL1, "reason": "smoke"}),
            ]
            for path, form in posts:
                r = await ac.post(path, data=form)
                assert r.status_code == 303, f"{path} -> {r.status_code}"

            # Regression: when a newer projection table hasn't been migrated yet, the page must
            # degrade — not 500 (the Activity/Prompts bug). Drop the tables and re-hit.
            await pool.execute("drop table dead_letters")
            await pool.execute("drop table prompts")
            await pool.execute("drop table acquisition_signals")
            await pool.execute("drop table acquisition_signups")
            for path, must in (
                ("/runs", "restart the kernel"),
                ("/prompts", "restart the kernel"),
                ("/acquisition", "restart the kernel"),
            ):
                r = await ac.get(path)
                assert r.status_code == 200, f"{path} regressed to {r.status_code} on missing table"
                assert must in r.text, f"{path} missing the degrade note"
    finally:
        await pool.close()
        await _teardown()


def test_console_read_routes_render_against_real_db():
    asyncio.run(_run_all())
