"""#283 — version-keyed in-process build cache for the feed / stories."""

import asyncio

from maat.serving.buildcache import VersionCache, data_version


def test_cache_hits_only_on_matching_version():
    c = VersionCache()
    c.put("k", 5, "payload-v5")
    assert c.get("k", 5) == "payload-v5"  # same version → hit
    assert c.get("k", 6) is None          # bumped version → miss (stale, must rebuild)
    assert c.get("other", 5) is None      # different key → miss


def test_cache_overwrites_on_new_version():
    c = VersionCache()
    c.put("k", 1, "old")
    c.put("k", 2, "new")
    assert c.get("k", 2) == "new" and c.get("k", 1) is None


def test_cache_evicts_least_recently_used():
    c = VersionCache(maxsize=2)
    c.put("a", 1, "A")
    c.put("b", 1, "B")
    assert c.get("a", 1) == "A"  # touch a → b becomes the LRU victim
    c.put("c", 1, "C")           # overflow → evict b
    assert c.get("a", 1) == "A" and c.get("c", 1) == "C" and c.get("b", 1) is None


def test_cache_clear():
    c = VersionCache()
    c.put("k", 1, "v")
    c.clear()
    assert c.get("k", 1) is None


class _FakePool:
    def __init__(self, val):
        self._val = val

    async def fetchval(self, _q):
        if isinstance(self._val, Exception):
            raise self._val
        return self._val


def test_data_version_reads_max_event_id():
    assert asyncio.run(data_version(_FakePool(42))) == 42
    assert asyncio.run(data_version(_FakePool(None))) == 0  # empty events log → 0


def test_data_version_degrades_to_minus_one_on_error():
    # -1 can never equal a stored non-negative version, so a broken probe forces a rebuild, never a
    # stale hit.
    assert asyncio.run(data_version(_FakePool(RuntimeError("events unavailable")))) == -1
