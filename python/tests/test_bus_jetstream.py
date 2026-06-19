"""#296 — durable consumer integration: a live JetStream round-trip + NAK→redelivery.

Skipped unless NATS JetStream is reachable on :4222 (CI runs Postgres but no broker, exactly like
the route-integration harness). Proves the two properties unit tests can't: events published to
MAAT_EVENTS actually reach the handler through a durable pull consumer, and a transient handler
failure is NAK'd and redelivered (at-least-once) rather than lost.
"""

import asyncio
import json

import nats
import pytest
from nats.js.api import StreamConfig

import maat.bus as bus


def _js_up() -> bool:
    async def ping():
        nc = await nats.connect("nats://localhost:4222")
        try:
            await nc.jetstream().account_info()
        finally:
            await nc.close()

    try:
        asyncio.run(asyncio.wait_for(ping(), 3))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _js_up(), reason="no NATS JetStream on :4222 (run docker compose up nats)")

_DURABLE = "itest_296"
_SUBJECT = "maat.events.test.itest296"


async def _scenario():
    nc = await nats.connect("nats://localhost:4222")
    js = nc.jetstream()
    # The kernel owns MAAT_EVENTS in prod; create it here if the kernel isn't running locally.
    try:
        await js.stream_info(bus.EVENTS_STREAM)
    except Exception:
        await js.add_stream(StreamConfig(name=bus.EVENTS_STREAM, subjects=["maat.events.>"]))
    # Clean slate so a prior run's ack-floor can't mask redelivery.
    try:
        await js.delete_consumer(bus.EVENTS_STREAM, _DURABLE)
    except Exception:
        pass

    processed: list[str] = []
    failed_once = {"flag": False}

    async def handler(_nc, event):
        sid = event["stream_id"]
        if sid == "needs-retry" and not failed_once["flag"]:
            failed_once["flag"] = True
            raise RuntimeError("transient")  # first delivery fails → NAK → redeliver
        processed.append(sid)

    # Bind FIRST (DeliverPolicy.NEW only sees events published after the consumer exists), then publish.
    task = asyncio.create_task(bus.run_agent(_DURABLE, _SUBJECT, handler))
    await asyncio.sleep(1.5)  # let the durable consumer bind
    for sid in ("a", "needs-retry", "b"):
        await js.publish(_SUBJECT, json.dumps({"stream_id": sid, "type": "test", "data": {}}).encode())

    for _ in range(50):  # poll up to ~10s for all three (the failing one after redelivery)
        if {"a", "b", "needs-retry"} <= set(processed):
            break
        await asyncio.sleep(0.2)

    task.cancel()
    try:
        await task
    except BaseException:
        pass
    try:
        await js.delete_consumer(bus.EVENTS_STREAM, _DURABLE)
    except Exception:
        pass
    await nc.close()
    return processed, failed_once["flag"]


def test_durable_consumer_processes_and_redelivers():
    processed, failed_once = asyncio.run(asyncio.wait_for(_scenario(), 30))
    assert {"a", "b", "needs-retry"} <= set(processed), f"all events processed; got {processed}"
    assert failed_once, "the transient-failure path (NAK → redeliver) was exercised"


# --- #298: two replicas of a stage share its work-queue (no duplicate processing) ---------------
_DURABLE_DIST = "itest_298"
_SUBJECT_DIST = "maat.events.test.itest298"


async def _distribution_scenario():
    nc = await nats.connect("nats://localhost:4222")
    js = nc.jetstream()
    try:
        await js.stream_info(bus.EVENTS_STREAM)
    except Exception:
        await js.add_stream(StreamConfig(name=bus.EVENTS_STREAM, subjects=["maat.events.>"]))
    try:
        await js.delete_consumer(bus.EVENTS_STREAM, _DURABLE_DIST)
    except Exception:
        pass

    seen: list[str] = []  # every stream_id processed, pooled across BOTH replicas

    async def handler(_nc, event):
        await asyncio.sleep(0.02)  # a little work so neither replica drains the whole batch instantly
        seen.append(event["stream_id"])

    # Two replicas of the SAME stage: identical durable name → they SHARE the work-queue (#298),
    # exactly what `--scale extract=N` produces.
    tasks = [asyncio.create_task(bus.run_agent(_DURABLE_DIST, _SUBJECT_DIST, handler)) for _ in range(2)]
    await asyncio.sleep(1.5)  # let both bind

    n = 12
    for i in range(n):
        await js.publish(_SUBJECT_DIST, json.dumps({"stream_id": f"m{i}", "type": "test", "data": {}}).encode())
    for _ in range(60):  # poll up to ~12s for all of them
        if len(seen) >= n:
            break
        await asyncio.sleep(0.2)

    for t in tasks:
        t.cancel()
        try:
            await t
        except BaseException:
            pass
    try:
        await js.delete_consumer(bus.EVENTS_STREAM, _DURABLE_DIST)
    except Exception:
        pass
    await nc.close()
    return seen, n


def test_two_replicas_share_the_queue_without_duplicates():
    seen, n = asyncio.run(asyncio.wait_for(_distribution_scenario(), 40))
    # The defining property of horizontal scaling (#298): each event is processed EXACTLY once across
    # the two replicas — a shared durable is a work-queue, not fan-out. (Which replica gets which is
    # timing-dependent, so the split ratio is deliberately not asserted.)
    assert sorted(seen) == sorted(f"m{i}" for i in range(n)), f"every event processed once; got {seen}"
    assert len(set(seen)) == len(seen), "no event processed by both replicas (no duplication)"
