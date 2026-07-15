"""Pipeline watchdog — the mouth the detector never had (#417).

On 2026-06-17 the corroboration engine died. It stayed dead for 27 days. Nothing told anyone.

The bitter part: the correct detector already existed and was already right. ``obs_metrics.stage_health``
maps ``cluster -> cluster.corroborated`` with a 24h "stalled" threshold, and fed today's data it
emits *"Stage 'cluster' last ran 656h ago"*. Its docstring even says alerts are "human-readable;
forward to alerting sinks". Nothing forwarded them: its only caller was an HTTP handler behind
WireGuard + Google OAuth. **Maat's entire liveness story terminated in an act of human attention.**

This service closes that loop. It is deliberately:

  * **its own container**, never a clock step — the clock is exactly what fails, and a hung tick
    must not be able to silence the thing that reports the hung tick;
  * **per-stage, never an aggregate** — ``throughput_freshness`` takes a max() over all event types,
    so live ingestion (4h old) masked a dead cluster stage (27 days). A max() cannot see a partial
    death. We check every stage independently;
  * **push, not pull** — it reaches OUT (webhook + dead-man's switch) rather than waiting to be
    looked at;
  * **fail-loud, fail-safe** — if the watchdog itself dies, the dead-man's-switch ping stops and the
    external monitor pages you. A heartbeat that must be STARVED to fire is the only kind that
    survives its own author crashing.

Delivery (all optional, all env-gated — with none set it still logs, which is strictly better than
today, but set at least one):
  MAAT_HEARTBEAT_URL  — dead-man's switch (healthchecks.io / Better Stack). Pinged ONLY while
                        healthy; the monitor pages when the pings STOP. This is the one that
                        survives the box going away entirely.
  MAAT_ALERT_WEBHOOK  — Slack/Discord/generic webhook, POSTed {"text": ...} on unhealthy.

Run: uv run --no-dev python scripts/watchdog.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import httpx

from maat.db import get_pool
from maat.obs_metrics import STAGE_EVENT_TYPES, STALLED_THRESHOLD_S, stage_health

INTERVAL_S = int(os.environ.get("MAAT_WATCHDOG_INTERVAL", "900"))  # 15 min
HEARTBEAT_URL = os.environ.get("MAAT_HEARTBEAT_URL", "").strip()
ALERT_WEBHOOK = os.environ.get("MAAT_ALERT_WEBHOOK", "").strip()
# Stages whose stall is an EMERGENCY (the product is lying to users), vs merely notable.
CRITICAL_STAGES = {"cluster", "extract", "classify"}


def _fmt_age(age_s: float | None) -> str:
    if age_s is None:
        return "never"
    h = age_s / 3600
    return f"{h:.1f}h" if h < 48 else f"{h / 24:.1f}d"


async def _stage_rows(pool) -> list[dict]:
    """Per-stage freshness in ONE bounded, indexed aggregate — never a scan of the events table.

    ``stage_health`` takes one dict per EVENT and folds them to (count, max timestamp). We hand it
    exactly ONE row per stage carrying that stage's latest timestamp, so the fold is O(stages), not
    O(events). Only ``freshness``/``age_s`` are consumed here, both of which depend solely on the max
    — the ``count`` it derives is meaningless at this cardinality and is deliberately unused.

    (Fabricating one dict per real event would allocate 68k+ dicts on every check — precisely the
    unbounded-allocation class that took the engine down. The watchdog must never become the
    problem it reports.)"""
    rows = await pool.fetch(
        "select type, max(created_at) as last_seen "
        "from events where type = any($1::text[]) group by type",
        list(STAGE_EVENT_TYPES.values()),
    )
    return [{"type": r["type"], "created_at": r["last_seen"]} for r in rows]


async def check(pool) -> tuple[bool, list[str], list[dict]]:
    """(healthy, alert lines, per-stage detail). Healthy = no CRITICAL stage stalled or never-run."""
    health = stage_health(await _stage_rows(pool))
    alerts: list[str] = []
    healthy = True
    for s in health:
        bad = s["freshness"] in ("stalled", "never")
        if bad and s["stage"] in CRITICAL_STAGES:
            healthy = False
            alerts.append(
                f"CRITICAL stage '{s['stage']}' ({s['event_type']}) is {s['freshness']} — "
                f"last seen {_fmt_age(s['age_s'])} ago (threshold {STALLED_THRESHOLD_S / 3600:.0f}h)"
            )
        elif bad:
            alerts.append(
                f"stage '{s['stage']}' ({s['event_type']}) is {s['freshness']} — "
                f"last seen {_fmt_age(s['age_s'])} ago"
            )
    return healthy, alerts, health


async def _deliver(healthy: bool, alerts: list[str]) -> None:
    """Push the verdict OUT. Healthy → feed the dead-man's switch. Unhealthy → withhold the ping
    (so the external monitor fires) AND say so on the webhook."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        if healthy and HEARTBEAT_URL:
            try:
                await client.get(HEARTBEAT_URL)
            except Exception as e:  # noqa: BLE001 - a dead switch is the monitor's problem to notice
                print(f"[watchdog] heartbeat ping failed: {type(e).__name__}", flush=True)
        if not healthy and ALERT_WEBHOOK:
            text = "🔴 Maat pipeline unhealthy\n" + "\n".join(f"• {a}" for a in alerts)
            try:
                await client.post(ALERT_WEBHOOK, json={"text": text})
            except Exception as e:  # noqa: BLE001
                print(f"[watchdog] webhook post failed: {type(e).__name__}", flush=True)


async def once() -> bool:
    pool = await get_pool()
    healthy, alerts, health = await check(pool)
    stamp = datetime.now(timezone.utc).isoformat()
    detail = " | ".join(f"{s['stage']}={s['freshness']}({_fmt_age(s['age_s'])})" for s in health)
    print(f"[watchdog] {stamp} healthy={healthy} :: {detail}", flush=True)
    for a in alerts:
        print(f"[watchdog] ALERT {a}", flush=True)
    await _deliver(healthy, alerts)
    return healthy


async def main() -> int:
    if not HEARTBEAT_URL and not ALERT_WEBHOOK:
        print(
            "[watchdog] WARNING: neither MAAT_HEARTBEAT_URL nor MAAT_ALERT_WEBHOOK is set — "
            "alerts will only be logged, which is what let a 27-day outage go unnoticed. "
            "Set a dead-man's switch (healthchecks.io) to actually get paged.",
            flush=True,
        )
    print(f"[watchdog] up — checking every {INTERVAL_S}s", flush=True)
    while True:
        try:
            await once()
        except Exception as e:  # noqa: BLE001 - never die quietly; the switch starving IS the alert
            print(f"[watchdog] check failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        await asyncio.sleep(INTERVAL_S)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
