"""Shared asyncpg connection-pool factory (#288).

One place owns the ``DATABASE_URL`` default **and** pool sizing, instead of the same
``asyncpg.create_pool(os.environ.get("DATABASE_URL", ...))`` copy-pasted across every
agent, script, and the web app. Pool min/max size is tunable via env
(``MAAT_DB_POOL_MIN`` / ``MAAT_DB_POOL_MAX``) so the serving path can be widened without
touching call sites — the #293 audit flagged unsized pools as a serving concurrency wall.
"""

from __future__ import annotations

import os
from typing import Any

import asyncpg

# Local-dev default — matches docker-compose.yml. Prod sets DATABASE_URL.
DEFAULT_DSN = "postgresql://maat:maat@localhost:5432/maat"

# asyncpg's own default is min=max=10, which (a) eagerly opens 10 connections per process —
# wasteful across the box's ~dozen long-lived agent processes — and (b) caps serving
# concurrency at 10. Own both knobs here; tune via env without editing any call site.
DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 10


def database_url() -> str:
    """The configured Postgres DSN (``DATABASE_URL``), or the local-dev default."""
    return os.environ.get("DATABASE_URL", DEFAULT_DSN)


def _env_int(name: str, default: int) -> int:
    """``int`` from env ``name``, falling back to ``default`` when unset/blank/garbage."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


async def get_pool(
    dsn: str | None = None,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
    **kwargs: Any,
) -> asyncpg.Pool:
    """Create an asyncpg pool with Maat's shared DSN + sizing defaults.

    Every call site goes through here so pool sizing is owned in one place. ``dsn`` defaults
    to :func:`database_url`; ``min_size`` / ``max_size`` default to ``MAAT_DB_POOL_MIN`` /
    ``MAAT_DB_POOL_MAX`` (env), then the module defaults. Extra keyword args pass straight
    through to ``asyncpg.create_pool``.
    """
    return await asyncpg.create_pool(
        dsn or database_url(),
        min_size=min_size if min_size is not None else _env_int("MAAT_DB_POOL_MIN", DEFAULT_POOL_MIN),
        max_size=max_size if max_size is not None else _env_int("MAAT_DB_POOL_MAX", DEFAULT_POOL_MAX),
        **kwargs,
    )
