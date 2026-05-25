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
    """Thread-safe LRU cache with TTL support + optional SQLite persistence.
    Use for: repo_index, search_results, project_info.
    """

    def __init__(self, maxsize: int = 64, default_ttl: float = 300.0, db_path: str | None = None) -> None:
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._lock = threading.RLock()
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._db_path = db_path

    def get(self, key: str) -> object | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                if 0 < entry.expire_at < time.monotonic():
                    del self._store[key]
                    entry = None
                else:
                    entry.hits += 1
                    self._store.move_to_end(key)
                    return entry.value
            # Fallback: try SQLite if configured
            if self._db_path is not None:
                return self._db_get(key)
            return None

    def _db_get(self, key: str) -> object | None:
        if self._db_path is None:
            return None
        try:
            import sqlite3, json
            conn = sqlite3.connect(self._db_path, timeout=5)
            row = conn.execute("SELECT result_json, expire_at FROM query_cache WHERE key=?", (key,)).fetchone()
            conn.close()
            if row is None:
                return None
            expire_at = row[1]
            if 0 < expire_at and expire_at < time.time():
                return None
            return json.loads(row[0])
        except Exception:
            return None

    def set(self, key: str, value: object, ttl: float | None = None) -> None:
        expire = time.monotonic() + (ttl or self._default_ttl)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = CacheEntry(value=value, expire_at=expire)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)
        # Persist to SQLite if configured (use time.time() base for cross-process compat)
        if self._db_path is not None:
            expire_time = time.time() + (ttl or self._default_ttl)
            self._db_set(key, value, expire_time)

    def _db_set(self, key: str, value: object, expire_at: float) -> None:
        if self._db_path is None:
            return
        try:
            import sqlite3, json
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS query_cache (
                    key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    expire_at REAL
                )
            """)
            conn.execute("INSERT OR REPLACE INTO query_cache(key, result_json, expire_at) VALUES(?,?,?)",
                         (key, json.dumps(value, ensure_ascii=False, default=str), expire_at))
            conn.commit()
            conn.close()
        except Exception:
            pass

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


def _get_search_cache(db_path: str | None = None) -> Cache:
    """Return search cache backed by SQLite for cross-request persistence."""
    global _search_cache
    if db_path is not None and getattr(_search_cache, '_db_path', None) != db_path:
        _search_cache = Cache(maxsize=128, default_ttl=30.0, db_path=db_path)
    return _search_cache


def _get_repo_cache() -> Cache:
    return _repo_cache


def repo_cache() -> Cache:
    return _repo_cache


def search_cache() -> Cache:
    return _search_cache
