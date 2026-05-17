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


# Global cache instance for item metadata
_item_cache = TTLCache(ttl_seconds=300, max_size=256)


def get_item_cache() -> TTLCache:
    """Return the global item metadata cache."""
    return _item_cache
