"""Clock helpers shared by the ingestion clock (scripts/clock.py) and the console (P8 A1).

The ingestion clock is a single-pass tick run on an external schedule (cron / systemd timer),
so there is no daemon to stop — 'pause' is a flag the tick reads: the console records an
``admin.clock.set`` event and the next tick consults it and skips. Ops control, not veracity
core, so it genuinely applies (no sign-off gate).
"""

from __future__ import annotations

import os
from pathlib import Path


def read_topics(root: Path) -> list[str]:
    """Tracked ingestion topics — MAAT_TOPICS (comma-separated) then config/topics.txt.

    Mirrors the clock's own resolution (minus the CLI-args path, which is per-invocation) so the
    console shows what a scheduled tick would actually pull.
    """
    env = os.environ.get("MAAT_TOPICS")
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    f = root / "config" / "topics.txt"
    if f.exists():
        return [
            ln.strip()
            for ln in f.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
    return []


def is_paused(clock_events: list[dict], clock: str = "ingestion") -> bool:
    """Is `clock` currently paused? `clock_events` = admin.clock.set payloads, newest first. Pure."""
    for d in clock_events:
        if d.get("clock") == clock:
            return bool(d.get("paused"))
    return False
