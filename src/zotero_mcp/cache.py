"""Lightweight in-memory TTL cache for expensive API reads."""

from __future__ import annotations

import threading
import time
from typing import Any


class TTLCache:
    """Thread-safe in-memory cache with TTL expiry and bounded size."""

    def __init__(self, ttl_seconds: float = 300, max_size: int = 256):
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        """Return cached value if present and not expired, else None."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._cache[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        """Store value with TTL. Evicts oldest entries if at max size."""
        with self._lock:
            now = time.monotonic()
            # Evict expired entries first
            expired = [k for k, (exp, _) in self._cache.items() if now > exp]
            for k in expired:
                del self._cache[k]
            # Evict oldest if still at capacity
            while len(self._cache) >= self._max_size:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
                del self._cache[oldest_key]
            self._cache[key] = (now + self._ttl, value)

    def invalidate(self, key: str) -> None:
        """Remove a specific key from cache."""
        with self._lock:
            self._cache.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        """Remove all keys starting with prefix."""
        with self._lock:
            to_remove = [k for k in self._cache if k.startswith(prefix)]
            for k in to_remove:
                del self._cache[k]

    def clear(self) -> None:
        """Remove all entries."""
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        """Return number of entries (including possibly expired)."""
        return len(self._cache)


# Global cache instances — one per resource type for independent TTLs
_item_cache = TTLCache(ttl_seconds=300, max_size=1024)
_children_cache = TTLCache(ttl_seconds=300, max_size=512)
_collections_cache = TTLCache(ttl_seconds=600, max_size=256)
_tags_cache = TTLCache(ttl_seconds=600, max_size=128)
_annotations_cache = TTLCache(ttl_seconds=120, max_size=256)


def get_item_cache() -> TTLCache:
    """Return the global item metadata cache."""
    return _item_cache


def get_children_cache() -> TTLCache:
    """Return the global item-children cache."""
    return _children_cache


def get_collections_cache() -> TTLCache:
    """Return the global collections-list cache."""
    return _collections_cache


def get_tags_cache() -> TTLCache:
    """Return the global tags-list cache."""
    return _tags_cache


def get_annotations_cache() -> TTLCache:
    """Return the global annotations cache."""
    return _annotations_cache


def invalidate_all_caches() -> None:
    """Clear every global cache — called after write operations."""
    _item_cache.clear()
    _children_cache.clear()
    _collections_cache.clear()
    _tags_cache.clear()
    _annotations_cache.clear()
