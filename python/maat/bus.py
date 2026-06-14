"""NATS bus plumbing for the Python agents (D20: choreography over the bus)."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import nats


async def connect():
    return await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))


async def run_agent(
    name: str,
    subject: str,
    handler: Callable[[Any, dict[str, Any]], Awaitable[None]],
) -> None:
    """Subscribe to `subject`, decode each event, run `handler(nc, event)` — forever."""
    nc = await connect()
    sub = await nc.subscribe(subject)
    print(f"[{name}] subscribed to {subject}", flush=True)
    async for msg in sub.messages:
        try:
            event = json.loads(msg.data)
            await handler(nc, event)
        except Exception as exc:  # noqa: BLE001 - an agent must not die on one bad event
            print(f"[{name}] error: {exc}", flush=True)
