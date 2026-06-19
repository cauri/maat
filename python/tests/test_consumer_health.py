"""#299 — per-stage durable-consumer health + dead-letter persistence.

Pure unit tests: JetStream consumer_info, the DB pool, and get_pool are stubbed, so we assert the
health shape (lag/in-flight/redelivered/throughput; absent when a consumer isn't bound or NATS is
down), the per-stage dead-letter coalesce, that run_agent persists a dead-letter tagged by stage,
and that the cat-cafe emit is a no-op without an OTLP endpoint.
"""

import asyncio
from types import SimpleNamespace

import asyncpg

import maat.bus as bus
from maat.serving.consumer_health import (
    STAGES,
    StageHealth,
    consumer_health,
    dead_letters_by_stage,
    health_as_dicts,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeJS:
    def __init__(self, infos):
        self._infos = infos

    async def consumer_info(self, _stream, durable):
        v = self._infos.get(durable)
        if isinstance(v, Exception):
            raise v
        return v


class _FakeNC:
    def __init__(self, js):
        self._js = js

    def jetstream(self):
        return self._js


def _info(pending, ack, redel, delivered):
    return SimpleNamespace(
        num_pending=pending, num_ack_pending=ack, num_redelivered=redel,
        delivered=SimpleNamespace(consumer_seq=delivered),
    )


def test_consumer_health_reports_lag_and_marks_unbound_absent():
    js = _FakeJS({
        "kerneld": _info(5, 2, 1, 100),
        "extract": _info(0, 0, 0, 50),
        "classify": RuntimeError("consumer not bound"),  # → absent, not an error
    })
    by = {r.stage: r for r in _run(consumer_health(_FakeNC(js), {"kerneld": 3}))}
    assert list(by) == list(STAGES)  # one row per stage, in order
    k = by["kerneld"]
    assert (k.pending, k.in_flight, k.redelivered, k.delivered, k.dead_letters, k.present) == (5, 2, 1, 100, 3, True)
    assert by["extract"].present and by["extract"].pending == 0
    assert by["classify"].present is False  # unbound consumer surfaces as absent, never 500s the page


def test_consumer_health_handles_no_nats():
    rows = _run(consumer_health(None))
    assert len(rows) == len(STAGES) and all(not r.present for r in rows)


def test_health_as_dicts_is_json_friendly():
    d = health_as_dicts([StageHealth("extract", 1, 2, 3, 4, 5, True)])[0]
    assert d == {"stage": "extract", "pending": 1, "in_flight": 2, "redelivered": 3,
                 "delivered": 4, "dead_letters": 5, "present": True}


class _FakePool:
    def __init__(self, rows=None, *, raise_on=None):
        self._rows = rows or []
        self._raise = raise_on
        self.executed: list = []

    async def fetch(self, _q, *_a):
        if self._raise:
            raise self._raise
        return self._rows

    async def execute(self, q, *a):
        self.executed.append((q, a))

    async def close(self):
        pass


def test_dead_letters_by_stage_coalesces_to_counts():
    pool = _FakePool([{"stage": "kerneld", "n": 4}, {"stage": "extract", "n": 1}])
    assert _run(dead_letters_by_stage(pool)) == {"kerneld": 4, "extract": 1}


def test_dead_letters_by_stage_degrades_without_the_column():
    pool = _FakePool(raise_on=asyncpg.UndefinedColumnError("column stage does not exist"))
    assert _run(dead_letters_by_stage(pool)) == {}


def test_persist_dead_letter_inserts_tagged_with_stage(monkeypatch):
    pool = _FakePool()

    async def fake_get_pool():
        return pool

    monkeypatch.setattr("maat.db.get_pool", fake_get_pool)
    event = {"stream_id": "s1", "type": "article.ingested", "data": {"x": 1}}
    _run(bus._persist_dead_letter("extract", event, RuntimeError("boom")))

    assert len(pool.executed) == 1
    q, args = pool.executed[0]
    assert "insert into dead_letters" in q
    assert args[0] == "s1" and args[1] == "article.ingested" and args[4] == "extract"  # stage tagged


def test_persist_dead_letter_never_raises(monkeypatch):
    async def boom_pool():
        raise RuntimeError("db down")

    monkeypatch.setattr("maat.db.get_pool", boom_pool)
    _run(bus._persist_dead_letter("extract", {"type": "x"}, RuntimeError("boom")))  # swallowed, logs


def test_emit_consumer_health_noop_without_otlp_endpoint(monkeypatch):
    from maat import obs

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    obs._state.clear()  # reset the lazily-built tracer cache
    obs.emit_consumer_health([{"stage": "extract", "pending": 1}])  # no endpoint → no-op, no crash
