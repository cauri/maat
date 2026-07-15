"""The watchdog (#417) — asserted against the ACTUAL outage it exists to have caught.

The corroboration engine died 2026-06-17 and nothing said a word for 27 days. The detector was
already correct; it had no caller and no mouth. These tests pin the two properties that failure
turned on: it must go unhealthy on a stalled CRITICAL stage, and it must check each stage
INDEPENDENTLY — an aggregate max() across event types is exactly what let live ingestion mask a
dead cluster stage.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from scripts import watchdog


class _FakePool:
    """Returns per-stage (type, last_seen) exactly as the real bounded aggregate query does."""

    def __init__(self, last_seen: dict[str, datetime]):
        self._rows = [{"type": t, "last_seen": ts} for t, ts in last_seen.items()]

    async def fetch(self, _sql, _types):
        return self._rows


def _ago(**kw) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kw)


def test_catches_the_real_outage_a_dead_cluster_stage_behind_live_ingestion():
    """THE regression test for 2026-06-17→07-14. Ingestion/extract/classify were all healthy and
    recent the entire time — only `cluster` was dead. A max() over all event types reads "4h stale"
    here and never fires; per-stage reads "27 days stalled" and does."""
    pool = _FakePool({
        "article.ingested": _ago(hours=4),      # alive — this is what masked it
        "claims.extracted": _ago(hours=4),      # alive
        "claims.classified": _ago(hours=4),     # alive
        "cluster.corroborated": _ago(days=27),  # DEAD — the whole product is stale
    })
    healthy, alerts, health = asyncio.run(watchdog.check(pool))

    assert healthy is False
    assert any("cluster" in a and "stalled" in a for a in alerts)
    assert any("CRITICAL" in a for a in alerts)
    # the live stages must NOT be alerted on — the signal has to be specific to be actionable
    assert not any("acquire" in a for a in alerts)
    by_stage = {s["stage"]: s["freshness"] for s in health}
    # This is the real shape of the outage: ingestion merely "stale" (4h > the 1h bar) while cluster
    # is "stalled" (27d). A max() across types reports the 4h and stays quiet forever; only checking
    # each stage on its own surfaces the death underneath the noise.
    assert by_stage["cluster"] == "stalled"
    assert by_stage["acquire"] == "stale" and by_stage["acquire"] != "stalled"


def test_a_stage_that_never_ran_is_unhealthy():
    # ownership/translate/geotag emitted ZERO events in their entire life and nothing noticed.
    pool = _FakePool({
        "article.ingested": _ago(minutes=5),
        "claims.extracted": _ago(minutes=5),
        "claims.classified": _ago(minutes=5),
        # cluster.corroborated absent entirely
    })
    healthy, alerts, _ = asyncio.run(watchdog.check(pool))
    assert healthy is False
    assert any("never" in a for a in alerts)


def test_healthy_when_every_stage_is_fresh():
    pool = _FakePool({t: _ago(minutes=5) for t in watchdog.STAGE_EVENT_TYPES.values()})
    healthy, alerts, _ = asyncio.run(watchdog.check(pool))
    assert healthy is True
    assert alerts == []


def test_healthy_pings_the_switch_and_unhealthy_starves_it(monkeypatch):
    """The delivery contract, and the reason it survives its own author dying: the dead-man's switch
    is fed ONLY while healthy. Unhealthy ⇒ withhold the ping (the external monitor fires) AND post
    the detail. If the watchdog/box/DB dies, the pings stop and you get paged anyway."""
    pinged: list[str] = []
    posted: list[dict] = []

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): pinged.append(url)
        async def post(self, url, json): posted.append({"url": url, "json": json})

    monkeypatch.setattr(watchdog.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(watchdog, "HEARTBEAT_URL", "https://switch.example/ping")
    monkeypatch.setattr(watchdog, "ALERT_WEBHOOK", "https://hook.example/x")

    asyncio.run(watchdog._deliver(True, []))
    assert pinged == ["https://switch.example/ping"] and posted == []

    pinged.clear()
    asyncio.run(watchdog._deliver(False, ["CRITICAL stage 'cluster' is stalled — last seen 27.0d ago"]))
    assert pinged == [], "pinged the dead-man's switch while UNHEALTHY — it would never fire"
    assert len(posted) == 1 and "cluster" in posted[0]["json"]["text"]


def test_stage_query_is_bounded_not_a_scan():
    """The watchdog must never become the problem it reports: an earlier careless query over the
    events table OOM'd the box. One row per STAGE comes back, never one per event."""
    seen: dict = {}

    class _Pool:
        async def fetch(self, sql, types):
            seen["sql"], seen["types"] = sql, types
            return [{"type": "cluster.corroborated", "last_seen": _ago(days=27)}]

    rows = asyncio.run(watchdog._stage_rows(_Pool()))
    assert len(rows) == 1                      # O(stages), not O(events)
    assert "max(created_at)" in seen["sql"] and "group by type" in seen["sql"]
    assert "select *" not in seen["sql"].lower()
    assert set(seen["types"]) == set(watchdog.STAGE_EVENT_TYPES.values())
