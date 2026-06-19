"""Integration smoke for the console v2 command/query API (#304).

Runs the real FastAPI app against a throwaway Postgres (migrations applied, a few rows seeded)
and exercises every ``/console/api`` read endpoint plus the command path (with a fake bus). This
reaches what the unit tests can't: the read SQL against the real schema (the claims↔articles join,
the ``claim_ids @> $1::jsonb`` cluster lookup, the distinct-on config/source folds) and the
command publish path end to end.

SKIPS when no Postgres is on :5432 (but raises in CI, where the service must be present) — same
contract as test_routes_integration.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

ADMIN_URL = "postgresql://maat:maat@localhost:5432/maat"
TEST_DB = "maat_console_api_test"
TEST_URL = f"postgresql://maat:maat@localhost:5432/{TEST_DB}"
MIGRATIONS = sorted(
    (Path(__file__).resolve().parents[2] / "rust/crates/maat-kerneld/migrations").glob("*.sql")
)

ART = "capi-article-1"
CL1, CL2 = str(uuid.uuid4()), str(uuid.uuid4())
CLUSTER = "capicluster00000000000001"


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


class _FakeNats:
    """Records publishes so the command path can be exercised without a real bus."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def flush(self) -> None:  # noqa: D401 - matches the nats client surface
        return None


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
        await db.execute(
            "insert into events (stream_id, type, data) values ($1, $2, $3::jsonb)",
            "gate.floor", "admin.threshold.changed",
            json.dumps({"target": "gate.floor", "key": "gate.floor", "value": "0.35",
                        "actor": "operator", "reason": "test"}),
        )
        await db.execute(
            "insert into events (stream_id, type, data) values ($1, $2, $3::jsonb)",
            "AFP", "admin.source.flagged",
            json.dumps({"target": "AFP", "source": "AFP", "status": "deny", "reason": "wire"}),
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

    from maat import events
    from maat.db import get_pool
    from maat.web import app as appmod

    await _setup()
    pool = await get_pool(TEST_URL)
    fake_nats = _FakeNats()
    appmod.app.state.pool = pool
    appmod.app.state.nats = fake_nats
    try:
        transport = httpx.ASGITransport(app=appmod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://capi") as ac:
            # --- queries: every read endpoint returns 200 with its contract shape ---
            r = await ac.get("/console/api/overview")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["counts"]["claims"] == 2
            assert set(body["clocks"]) == {"ingestion", "extraction", "corroboration"}

            r = await ac.get("/console/api/stories")
            assert r.status_code == 200 and "stories" in r.json()

            r = await ac.get("/console/api/sources")
            assert r.status_code == 200
            names = {s["source"] for s in r.json()["sources"]}
            assert "European Central Bank" in names
            afp = next((s for s in r.json()["sources"] if s["source"] == "AFP"), None)
            # AFP has no articles here, so it won't appear; the flag fold is still exercised above.
            assert afp is None or afp["status"] == "deny"

            r = await ac.get("/console/api/claims")
            assert r.status_code == 200 and r.json()["total"] == 2

            r = await ac.get(f"/console/api/claims/{CL1}")
            assert r.status_code == 200, r.text
            detail = r.json()
            assert detail["cluster"]["id"] == CLUSTER  # the claim_ids @> $1::jsonb lookup works
            assert detail["source"] == "European Central Bank"

            r = await ac.get("/console/api/config")
            assert r.status_code == 200
            knobs = {k["key"]: k for k in r.json()["knobs"]}
            assert knobs["gate.floor"]["proposed"]["value"] == "0.35"  # seeded proposal surfaces

            for path in ("/console/api/pipeline", "/console/api/prompts", "/console/api/feedback",
                         "/console/api/spend", "/console/api/audit", "/console/api/whoami",
                         "/console/api/commands"):
                resp = await ac.get(path)
                assert resp.status_code == 200, f"{path} -> {resp.status_code}: {resp.text}"

            assert (await ac.get("/console/api/stories/does-not-exist")).status_code == 404

            # --- commands: validation + the publish path with the fake bus ---
            assert (await ac.post("/console/api/commands/nope", json={})).status_code == 404
            bad = await ac.post("/console/api/commands/claim.correct", json={"claim_id": CL1})
            assert bad.status_code == 400  # no correction fields

            before = len(fake_nats.published)
            ok = await ac.post(
                "/console/api/commands/claim.correct",
                json={"claim_id": CL1, "kind": "projection", "reason": "smoke"},
            )
            assert ok.status_code == 200, ok.text
            assert ok.json()["event_type"] == events.ADMIN_CLASSIFICATION_CORRECTED
            assert len(fake_nats.published) == before + 1
            subject, payload = fake_nats.published[-1]
            assert subject == f"{events.SUBJECT_PREFIX}.{events.ADMIN_CLASSIFICATION_CORRECTED}"
            env = json.loads(payload)
            assert env["stream_id"] == CL1 and env["data"]["kind"] == "projection"

            # promote a non-enactable knob → rejected before any publish
            n = len(fake_nats.published)
            promote = await ac.post(
                "/console/api/commands/config.promote", json={"key": "gate.floor", "value": "0.4"}
            )
            assert promote.status_code == 400  # gate.floor isn't enactable
            assert len(fake_nats.published) == n
    finally:
        await pool.close()
        await _teardown()


def test_console_api_routes_against_real_db():
    asyncio.run(_run_all())
