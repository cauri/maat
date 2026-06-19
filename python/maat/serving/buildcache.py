"""Version-keyed in-process build cache (#283).

The curated feed and the stories list are GLOBAL folds — de-US capping rotates a country's share
across the WHOLE feed, so they can't be sliced in SQL; the page bounds the payload, not the work.
But they only CHANGE when new events land. So cache each built payload keyed by the request params
plus a cheap data-version (``max(events.id)``), and serve every request between ticks straight from
the cache instead of re-folding the corpus per request. Bounded (LRU) and per-process — a lossy
cache across workers is fine; the version guarantees it is never stale.

Correctness: every feed-affecting change IS an event (cluster recompute, source flags, geo
overrides, pivots), so any change bumps ``max(events.id)`` and invalidates the entry. Treat cached
values as READ-ONLY (responses are serialized, never mutated).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

_DEFAULT_MAX = 128


class VersionCache:
    """A tiny LRU keyed by (key, version): a get only hits when the stored version matches."""

    def __init__(self, maxsize: int = _DEFAULT_MAX) -> None:
        self._d: OrderedDict[Any, tuple[int, Any]] = OrderedDict()
        self._max = maxsize

    def get(self, key: Any, version: int) -> Any | None:
        hit = self._d.get(key)
        if hit is not None and hit[0] == version:
            self._d.move_to_end(key)  # LRU touch
            return hit[1]
        return None

    def put(self, key: Any, version: int, value: Any) -> None:
        self._d[key] = (version, value)
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        self._d.clear()


async def data_version(pool: Any) -> int:
    """A monotone version that bumps on ANY new event — clusters, source flags, geo, pivots are all
    events — so a cache entry invalidates the moment the feed could change. Cheap: ``id`` is the
    primary key, so ``max(id)`` is an index lookup. Returns -1 on error → never a hit (always rebuild)."""
    try:
        return int(await pool.fetchval("select max(id) from events") or 0)
    except Exception:  # noqa: BLE001 - events table unavailable → force a rebuild, never a stale hit
        return -1
