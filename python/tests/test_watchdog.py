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


# --- #419: an alarm that always fires is its own outage -----------------------------------------


def test_intentionally_silent_stages_do_not_alert(monkeypatch):
    """The watchdog must not fire on the state the box was ASKED to be in (#419).

    Observed on the live box: cauri paused intake in 2026-06 (console-v2 work) and the engine is
    gated off pending the corroborate fixes. The watchdog alerted CRITICAL on acquire, extract,
    classify AND cluster — every 15 minutes, forever — while the box was in exactly its intended
    state. An alarm that always fires trains you to ignore it, which is the same failure as the
    27-day outage wearing the opposite mask: a signal nobody can act on and a signal nobody sends
    are worth the same.
    """
    import asyncio

    import scripts.watchdog as wd

    monkeypatch.setenv("MAAT_INTAKE_PAUSED", "1")
    monkeypatch.setenv("MAAT_CORROBORATE_ENABLED", "0")

    async def fake_rows(_pool):
        # every stage long dead — but all four are dead ON PURPOSE
        return [
            {"stage": "acquire", "last_seen": None},
            {"stage": "extract", "last_seen": None},
            {"stage": "classify", "last_seen": None},
            {"stage": "cluster", "last_seen": None},
        ]

    monkeypatch.setattr(wd, "_stage_rows", fake_rows)
    healthy, alerts, health = asyncio.run(wd.check(object()))

    assert healthy is True, "the box is in its intended state — the dead-man's switch must stay fed"
    assert alerts == [], f"must not alert on a deliberate pause, got: {alerts}"
    assert {s["stage"] for s in health if s["freshness"] == "paused"} == {
        "acquire", "extract", "classify", "cluster",
    }
    # and every paused stage must carry the reason — "paused" is never allowed to be unexplained
    assert all(s.get("paused_because") for s in health if s["freshness"] == "paused")


def test_a_stage_pages_the_moment_its_switch_is_turned_back_on(monkeypatch):
    """The other half, and the one that actually matters (#419).

    Suppressing an alert is only safe if the suppression lifts the instant the reason does. If the
    engine is armed and `cluster` is STILL dead, that is the original outage — and it must page on
    the very next check, not wait for anything to be noticed.
    """
    import asyncio

    import scripts.watchdog as wd

    monkeypatch.setenv("MAAT_INTAKE_PAUSED", "0")
    monkeypatch.setenv("MAAT_CORROBORATE_ENABLED", "1")  # engine ARMED

    async def fake_rows(_pool):
        return [{"stage": "cluster", "last_seen": None}]  # …and cluster is still dead

    monkeypatch.setattr(wd, "_stage_rows", fake_rows)
    healthy, alerts, _ = asyncio.run(wd.check(object()))

    assert healthy is False, "an armed engine with a dead cluster stage is THE outage — it must page"
    assert any("cluster" in a for a in alerts)


def test_suppression_requires_an_explicit_switch_never_a_default(monkeypatch):
    """An ABSENT variable must alert, not suppress (#419).

    The first cut of `_expected_silent` read `MAAT_CORROBORATE_ENABLED != "1"` → silent. That mutes
    the cluster alarm whenever the variable is merely missing — a typo'd compose key, a dropped env
    in a future deploy — permanently, and rebuilds the 27-day outage inside the thing built to catch
    it. The existing outage tests caught it, which is exactly what they are for.
    """
    import asyncio

    import scripts.watchdog as wd

    monkeypatch.delenv("MAAT_CORROBORATE_ENABLED", raising=False)
    monkeypatch.delenv("MAAT_INTAKE_PAUSED", raising=False)

    async def fake_rows(_pool):
        return [{"stage": "cluster", "last_seen": None}]

    monkeypatch.setattr(wd, "_stage_rows", fake_rows)
    healthy, alerts, _ = asyncio.run(wd.check(object()))

    assert healthy is False, "an UNSET gate must alert — silence is only ever opt-in"
    assert any("cluster" in a for a in alerts)

    # a malformed value is not an off-switch either
    monkeypatch.setenv("MAAT_CORROBORATE_ENABLED", "false")
    healthy, _, _ = asyncio.run(wd.check(object()))
    assert healthy is False, "only the exact string '0' suppresses; anything else alerts"
