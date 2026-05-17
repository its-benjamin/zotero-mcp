import time

from zotero_mcp.cache import TTLCache


def test_ttl_cache_hit():
    cache = TTLCache(ttl_seconds=60, max_size=10)
    cache.set("a", {"value": 1})

    assert cache.get("a") == {"value": 1}


def test_ttl_cache_miss():
    cache = TTLCache(ttl_seconds=60, max_size=10)

    assert cache.get("missing") is None


def test_ttl_cache_expires():
    cache = TTLCache(ttl_seconds=0.01, max_size=10)
    cache.set("a", 1)
    time.sleep(0.02)

    assert cache.get("a") is None


def test_ttl_cache_evicts_oldest_when_full():
    cache = TTLCache(ttl_seconds=60, max_size=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)

    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_ttl_cache_invalidate():
    cache = TTLCache(ttl_seconds=60, max_size=10)
    cache.set("a", 1)
    cache.invalidate("a")

    assert cache.get("a") is None


def test_ttl_cache_invalidate_prefix():
    cache = TTLCache(ttl_seconds=60, max_size=10)
    cache.set("item:A", 1)
    cache.set("item:B", 2)
    cache.set("collection:A", 3)
    cache.invalidate_prefix("item:")

    assert cache.get("item:A") is None
    assert cache.get("item:B") is None
    assert cache.get("collection:A") == 3
