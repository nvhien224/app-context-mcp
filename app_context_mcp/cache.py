"""
Fast in-memory LRU cache for App Context MCP.
No dependency; pure stdlib.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class CacheEntry:
    value: object
    expire_at: float = 0.0
    hits: int = 0


class Cache:
    """Thread-safe LRU cache with TTL support.
    Use for: repo_index, search_results, project_info.
    """

    def __init__(self, maxsize: int = 64, default_ttl: float = 300.0) -> None:
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._lock = threading.RLock()
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()

    def get(self, key: str) -> object | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if 0 < entry.expire_at < time.monotonic():
                del self._store[key]
                return None
            entry.hits += 1
            self._store.move_to_end(key)
            return entry.value

    def set(self, key: str, value: object, ttl: float | None = None) -> None:
        expire = time.monotonic() + (ttl or self._default_ttl)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = CacheEntry(value=value, expire_at=expire)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def invalidate(self, key: str | None = None, prefix: str | None = None) -> int:
        """Remove by exact key or prefix. Returns count removed."""
        with self._lock:
            if key is not None and key in self._store:
                del self._store[key]
                return 1
            if prefix is not None:
                removed = 0
                for k in list(self._store.keys()):
                    if k.startswith(prefix):
                        del self._store[k]
                        removed += 1
                return removed
            return 0

    def stats(self) -> dict[str, int | float]:
        with self._lock:
            total_hits = sum(e.hits for e in self._store.values())
            return {
                "size": len(self._store),
                "maxsize": self._maxsize,
                "total_hits": total_hits,
            }


# Singletons — imported by server/context
_repo_cache: Cache = Cache(maxsize=16, default_ttl=60.0)   # short TTL: index may change
_search_cache: Cache = Cache(maxsize=128, default_ttl=30.0)  # very short: cheap to recompute


def repo_cache() -> Cache:
    return _repo_cache


def search_cache() -> Cache:
    return _search_cache
