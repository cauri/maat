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
