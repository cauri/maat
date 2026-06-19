"""NATS bus plumbing for the Python agents (D20: choreography over the bus).

``run_agent`` is a **durable JetStream pull-consumer + bounded worker pool** (#296). It replaces
the old core-NATS ``subscribe`` (serial, ephemeral, no ack) that dropped in-flight events on a
restart — the agent half of the durability gap the kernel closed for itself in #221.

- One **durable consumer per stage** on the kernel's ``MAAT_EVENTS`` stream, filtered to the
  stage's subject, with **explicit ack**. A restart resumes from the ack-floor, so un-acked events
  are redelivered rather than lost.
- Each fetched batch runs through a **bounded concurrent pool** (``MAAT_AGENT_CONCURRENCY``).
- Delivery is **at-least-once**, so handlers must be idempotent (#297): ack on success, NAK to
  redeliver on a transient error, and after ``MaxDeliver`` dead-letter + ack so one poison event
  can't wedge the stage.
- ``DeliverPolicy.NEW`` so a first start consumes only live events (matching the old behaviour);
  the durable then resumes from the ack-floor on every restart thereafter.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import nats
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

EVENTS_STREAM = "MAAT_EVENTS"

Handler = Callable[[Any, dict[str, Any]], Awaitable[None]]


async def connect():
    return await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _dead_letter(name: str, event: dict[str, Any], exc: Exception, delivered: int) -> None:
    """A poison event exhausted its retries — record it (structured log) so an operator can find it;
    the caller then acks so it stops redelivering. Never raises."""
    sid = event.get("stream_id") or (event.get("data") or {}).get("article_id") or "?"
    print(f"[{name}] DEAD-LETTER after {delivered} deliveries (stream_id={sid}, type={event.get('type','?')}): {exc!r}", flush=True)


async def _persist_dead_letter(name: str, event: dict[str, Any], exc: Exception) -> None:
    """Best-effort: record the dead-letter in the ``dead_letters`` table, tagged with the stage, so
    the operator console can SURFACE and replay it (#299) — not just the log line above. Failure here
    never raises (a short-lived pool; the structured log from ``_dead_letter`` is the floor)."""
    try:
        from maat.db import get_pool

        pool = await get_pool()
        try:
            await pool.execute(
                "insert into dead_letters (stream_id, type, data, error, stage) "
                "values ($1, $2, $3::jsonb, $4, $5)",
                event.get("stream_id"), event.get("type") or "?",
                json.dumps(event.get("data") or {}), repr(exc), name,
            )
        finally:
            await pool.close()
    except Exception as persist_exc:  # noqa: BLE001 - best-effort; the structured log is the floor
        print(f"[{name}] dead-letter persist failed: {persist_exc}", flush=True)


async def _process_msg(name: str, handler: Handler, nc: Any, msg: Any, max_deliver: int) -> None:
    """Decode → run handler → ack / nak / dead-letter ONE message.

    Factored out of the fetch loop so the at-least-once ack policy is unit-testable without a live
    JetStream (the loop just supplies fetched messages).
    """
    try:
        delivered = msg.metadata.num_delivered
    except Exception:  # noqa: BLE001 - metadata shape varies; treat as first delivery
        delivered = 1
    try:
        event = json.loads(msg.data)
    except Exception as exc:  # noqa: BLE001 - poison JSON: skip, never wedge the consumer
        print(f"[{name}] unparseable event, dropping: {exc}", flush=True)
        await msg.ack()
        return
    try:
        await handler(nc, event)
    except Exception as exc:  # noqa: BLE001 - a handler error must not kill the worker
        if delivered >= max_deliver:
            _dead_letter(name, event, exc, delivered)
            await _persist_dead_letter(name, event, exc)  # #299: surface on the console, not just logs
            await msg.ack()  # acked so the poison event can't wedge the stage
        else:
            print(f"[{name}] handler error (delivery {delivered}/{max_deliver}), nak: {exc}", flush=True)
            await msg.nak()
        return
    await msg.ack()


async def _bind_consumer(js: Any, name: str, subject: str, *, ack_wait: float, max_deliver: int):
    """Bind this stage's durable pull consumer to ``MAAT_EVENTS``, retrying until the kernel (the
    single writer) has created the stream — so an agent that boots before the kernel waits instead
    of crashing."""
    cfg = ConsumerConfig(
        durable_name=name,
        filter_subject=subject,
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=ack_wait,  # seconds (nats-py converts to ns)
        max_deliver=max_deliver,
        deliver_policy=DeliverPolicy.NEW,
    )
    last: Exception | None = None
    for _ in range(_env_int("MAAT_AGENT_BIND_ATTEMPTS", 30)):
        try:
            return await js.pull_subscribe(subject, durable=name, stream=EVENTS_STREAM, config=cfg)
        except Exception as exc:  # noqa: BLE001 - stream not yet created by the kernel
            last = exc
            await asyncio.sleep(2)
    raise RuntimeError(f"[{name}] MAAT_EVENTS unavailable after retries: {last}")


async def run_agent(name: str, subject: str, handler: Handler) -> None:
    """Consume ``subject`` from ``MAAT_EVENTS`` via a durable pull consumer + bounded worker pool,
    forever. Same signature as before, so the per-stage agents need no change."""
    nc = await connect()
    js = nc.jetstream()
    concurrency = _env_int("MAAT_AGENT_CONCURRENCY", 8)
    ack_wait = float(os.environ.get("MAAT_AGENT_ACK_WAIT", "60"))
    max_deliver = _env_int("MAAT_AGENT_MAX_DELIVER", 6)
    sub = await _bind_consumer(js, name, subject, ack_wait=ack_wait, max_deliver=max_deliver)
    print(f"[{name}] durable consumer on {subject} (concurrency={concurrency}, ack_wait={ack_wait}s)", flush=True)
    while True:
        try:
            msgs = await sub.fetch(batch=concurrency, timeout=5)
        except nats.errors.TimeoutError:
            continue  # idle window — poll again
        except Exception as exc:  # noqa: BLE001 - transient fetch/connection error: back off, retry
            print(f"[{name}] fetch error: {exc}", flush=True)
            await asyncio.sleep(1)
            continue
        await asyncio.gather(*(_process_msg(name, handler, nc, m, max_deliver) for m in msgs))
