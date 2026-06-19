"""#296 — durable run_agent: the at-least-once ack policy of _process_msg.

Deterministic + NATS-free: a fake message records ack/nak so we pin exactly when the consumer
acks (success / poison JSON), naks (transient error, retries remain), and dead-letters+acks (a
poison event that exhausted MaxDeliver — so it can never wedge the stage). The live JetStream
round-trip is exercised by tests/test_bus_jetstream.py (skipped without a broker).
"""

import asyncio
import json
from types import SimpleNamespace

import maat.bus as bus


class _Msg:
    def __init__(self, data, num_delivered=1):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self._nd = num_delivered
        self.acks: list[str] = []

    @property
    def metadata(self):
        return SimpleNamespace(num_delivered=self._nd)

    async def ack(self):
        self.acks.append("ack")

    async def nak(self):
        self.acks.append("nak")


def _process(handler, msg, max_deliver=6):
    asyncio.run(bus._process_msg("test", handler, None, msg, max_deliver))


async def _ok(nc, event):
    pass


async def _boom(nc, event):
    raise RuntimeError("boom")


def test_success_acks():
    m = _Msg({"type": "x", "stream_id": "a"})
    _process(_ok, m)
    assert m.acks == ["ack"]


def test_transient_error_naks_for_redelivery():
    m = _Msg({"type": "x", "stream_id": "a"}, num_delivered=1)
    _process(_boom, m, max_deliver=6)
    assert m.acks == ["nak"]  # retries remain → redeliver


def test_poison_event_deadletters_and_acks_at_maxdeliver():
    m = _Msg({"type": "x", "stream_id": "a"}, num_delivered=6)
    _process(_boom, m, max_deliver=6)
    assert m.acks == ["ack"]  # retries exhausted → dead-letter (logged) + ack so it can't wedge


def test_unparseable_json_acks_and_skips_handler():
    ran: list[int] = []

    async def handler(nc, event):
        ran.append(1)

    m = _Msg(b"not json{", num_delivered=1)
    _process(handler, m)
    assert m.acks == ["ack"] and not ran  # poison JSON dropped without running the handler


def test_handler_receives_decoded_event():
    seen: list[dict] = []

    async def handler(nc, event):
        seen.append(event)

    _process(handler, _Msg({"type": "article.ingested", "stream_id": "gd-1", "data": {"x": 1}}))
    assert seen == [{"type": "article.ingested", "stream_id": "gd-1", "data": {"x": 1}}]
