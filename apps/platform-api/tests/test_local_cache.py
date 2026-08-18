"""Unit tests for the process-local L1 TTL cache (tail-latency hardening).

``LocalTTLCache`` is a pure in-memory structure with no I/O and no Redis/DB
dependency, so it is exercised directly here. The integration of this L1 into
the actor / list read paths is covered by ``test_list_cache.py`` and the
cross-container performance run.
"""

from __future__ import annotations

import threading
import time

import pytest

from workama_platform.modules.cache import LocalTTLCache


def test_set_and_get_hit():
    c = LocalTTLCache(ttl=60, maxsize=16)
    c.set("k", {"v": 1})
    assert c.get("k") == {"v": 1}


def test_get_miss_returns_none():
    c = LocalTTLCache(ttl=60, maxsize=16)
    assert c.get("missing") is None


def test_get_returns_none_after_ttl_expiry():
    c = LocalTTLCache(ttl=0.05, maxsize=16)
    c.set("k", "v")
    assert c.get("k") == "v"
    time.sleep(0.08)
    assert c.get("k") is None


def test_ttl_expired_entry_is_evicted_from_store():
    c = LocalTTLCache(ttl=0.05, maxsize=16)
    c.set("k", "v")
    time.sleep(0.08)
    c.get("k")  # triggers lazy eviction
    assert len(c) == 0


def test_lru_eviction_respects_maxsize():
    c = LocalTTLCache(ttl=60, maxsize=3)
    for i in range(5):
        c.set(f"k{i}", i)
    # only the 3 most-recently-set keys survive
    assert len(c) == 3
    assert c.get("k0") is None  # oldest evicted
    assert c.get("k4") == 4
    assert c.get("k3") == 3
    assert c.get("k2") == 2


def test_lru_touch_keeps_recently_used_key():
    c = LocalTTLCache(ttl=60, maxsize=2)
    c.set("a", 1)
    c.set("b", 2)
    c.get("a")  # touch "a" so it becomes most-recently-used
    c.set("c", 3)  # should evict "b" (the LRU), not "a"
    assert c.get("a") == 1
    assert c.get("b") is None
    assert c.get("c") == 3


def test_delete_removes_entry():
    c = LocalTTLCache(ttl=60, maxsize=16)
    c.set("k", "v")
    c.delete("k")
    assert c.get("k") is None
    assert len(c) == 0


def test_clear_empties_store():
    c = LocalTTLCache(ttl=60, maxsize=16)
    c.set("a", 1)
    c.set("b", 2)
    c.clear()
    assert len(c) == 0
    assert c.get("a") is None


def test_len_tracks_entries():
    c = LocalTTLCache(ttl=60, maxsize=16)
    assert len(c) == 0
    c.set("a", 1)
    c.set("b", 2)
    assert len(c) == 2


def test_delete_missing_key_is_safe():
    c = LocalTTLCache(ttl=60, maxsize=16)
    c.delete("nope")  # must not raise


def test_concurrent_set_get_no_crash():
    c = LocalTTLCache(ttl=60, maxsize=256)
    errors = []

    def worker(i: int) -> None:
        try:
            for j in range(200):
                c.set(f"k{i}-{j}", j)
                _ = c.get(f"k{i}-{j % 50}")
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent access raised: {errors}"


def test_value_is_returned_by_reference_not_cloned():
    c = LocalTTLCache(ttl=60, maxsize=16)
    obj = {"nested": [1, 2, 3]}
    c.set("k", obj)
    got = c.get("k")
    assert got is obj  # same object, no serialization/copy on L1 path
