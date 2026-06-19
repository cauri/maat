"""#288 — the shared pool factory owns the DSN default + env-tunable pool sizing.

No real Postgres needed: asyncpg.create_pool is stubbed so we assert exactly what the
factory hands it (dsn + min/max sizing + passthrough kwargs).
"""

import asyncio

import maat.db as db


def test_database_url_default(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert db.database_url() == db.DEFAULT_DSN


def test_database_url_env_wins(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user@host/custom")
    assert db.database_url() == "postgresql://user@host/custom"


def _capture(monkeypatch) -> dict:
    """Stub asyncpg.create_pool to record its args and return a sentinel pool."""
    calls: dict = {}

    async def fake_create_pool(dsn, **kwargs):
        calls["dsn"] = dsn
        calls["kwargs"] = kwargs
        return "POOL_SENTINEL"

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    return calls


def test_get_pool_uses_defaults(monkeypatch):
    for var in ("DATABASE_URL", "MAAT_DB_POOL_MIN", "MAAT_DB_POOL_MAX"):
        monkeypatch.delenv(var, raising=False)
    calls = _capture(monkeypatch)

    pool = asyncio.run(db.get_pool())

    assert pool == "POOL_SENTINEL"
    assert calls["dsn"] == db.DEFAULT_DSN
    assert calls["kwargs"]["min_size"] == db.DEFAULT_POOL_MIN
    assert calls["kwargs"]["max_size"] == db.DEFAULT_POOL_MAX


def test_get_pool_env_tunes_sizing(monkeypatch):
    monkeypatch.setenv("MAAT_DB_POOL_MIN", "3")
    monkeypatch.setenv("MAAT_DB_POOL_MAX", "25")
    calls = _capture(monkeypatch)

    asyncio.run(db.get_pool())

    assert calls["kwargs"]["min_size"] == 3
    assert calls["kwargs"]["max_size"] == 25


def test_get_pool_per_call_args_beat_env_and_pass_through(monkeypatch):
    monkeypatch.setenv("MAAT_DB_POOL_MAX", "25")
    calls = _capture(monkeypatch)

    asyncio.run(db.get_pool("postgresql://explicit/db", max_size=7, command_timeout=5))

    assert calls["dsn"] == "postgresql://explicit/db"
    assert calls["kwargs"]["max_size"] == 7  # explicit arg beats the env value
    assert calls["kwargs"]["command_timeout"] == 5  # unknown kwargs pass straight through


def test_get_pool_ignores_garbage_env(monkeypatch):
    monkeypatch.setenv("MAAT_DB_POOL_MAX", "not-an-int")
    calls = _capture(monkeypatch)

    asyncio.run(db.get_pool())

    assert calls["kwargs"]["max_size"] == db.DEFAULT_POOL_MAX
