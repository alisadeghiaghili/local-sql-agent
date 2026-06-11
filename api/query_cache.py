"""Thread-safe TTL query-result cache.

Caches the full ``QueryResponse`` keyed on ``(question, mode)``.
Expired entries are evicted lazily on read and eagerly on ``clear()``.

Design
------
* **LRU + TTL**: entries are evicted when they exceed ``ttl_seconds`` OR
  when the store exceeds ``max_size`` (oldest-inserted evicted first via
  ``OrderedDict``).
* **Thread-safe**: a single ``threading.Lock`` guards all mutations.
* **Opt-out**: callers pass ``cache=False`` to ``run_query`` to skip the
  cache entirely (e.g. ``mode='sql'`` where freshness matters).
* **Metrics**: ``hits``, ``misses``, ``evictions`` counters for observability.

Usage::

    from api.query_cache import query_cache

    entry = query_cache.get("سوال", "full")
    if entry is None:
        ...  # compute
        query_cache.set("سوال", "full", response)

    query_cache.clear()          # flush all
    query_cache.stats()          # {hits, misses, evictions, size}
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import NamedTuple

from api.models import QueryResponse


class _CacheEntry(NamedTuple):
    response: QueryResponse
    expires_at: float  # monotonic time


class QueryCache:
    """LRU + TTL in-memory cache for query responses.

    Parameters
    ----------
    ttl_seconds:
        Time-to-live per entry in seconds.  ``0`` disables the cache.
    max_size:
        Maximum number of entries before LRU eviction kicks in.
    """

    def __init__(self, ttl_seconds: int = 300, max_size: int = 256) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._store: OrderedDict[tuple[str, str], _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """False when TTL is 0 — callers may short-circuit."""
        return self._ttl > 0

    def get(self, question: str, mode: str) -> QueryResponse | None:
        """Return cached response or ``None`` (miss / expired / disabled)."""
        if not self.enabled:
            return None

        key = (question.strip(), mode)
        now = time.monotonic()

        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None

            if now > entry.expires_at:
                # Expired — evict lazily
                del self._store[key]
                self._evictions += 1
                self._misses += 1
                return None

            # Move to end (most-recently-used)
            self._store.move_to_end(key)
            self._hits += 1
            return entry.response

    def set(self, question: str, mode: str, response: QueryResponse) -> None:
        """Store *response* under *(question, mode)*."""
        if not self.enabled:
            return

        key = (question.strip(), mode)
        expires_at = time.monotonic() + self._ttl

        with self._lock:
            if key in self._store:
                # Refresh existing entry
                self._store.move_to_end(key)
                self._store[key] = _CacheEntry(response, expires_at)
                return

            # LRU eviction when at capacity
            while len(self._store) >= self._max_size:
                self._store.popitem(last=False)
                self._evictions += 1

            self._store[key] = _CacheEntry(response, expires_at)

    def invalidate(self, question: str, mode: str) -> bool:
        """Remove a specific entry.  Returns True if it was present."""
        key = (question.strip(), mode)
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        """Flush the entire cache."""
        with self._lock:
            self._store.clear()

    def stats(self) -> dict:
        """Return a snapshot of cache metrics."""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "size": len(self._store),
                "max_size": self._max_size,
                "ttl_seconds": self._ttl,
                "enabled": self.enabled,
            }

    def reconfigure(self, ttl_seconds: int, max_size: int) -> None:
        """Hot-reload config and wipe stale entries.

        Clearing the store is mandatory: entries stored under the old TTL
        carry an ``expires_at`` timestamp that is meaningless under a new
        TTL.  Keeping them would allow expired data to be served (if the
        new TTL is longer) or hide a test-isolation bug (if shorter).
        """
        with self._lock:
            self._ttl = ttl_seconds
            self._max_size = max_size
            self._store.clear()   # ← key fix: wipe stale entries


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ---------------------------------------------------------------------------

import config as cfg  # noqa: E402  (after class definition)

query_cache = QueryCache(
    ttl_seconds=cfg.settings.cache_ttl_seconds,
    max_size=cfg.settings.cache_max_size,
)
