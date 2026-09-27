# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A tiny TTL cache shared by the admin panel's warehouse-touching cards.

2026 warehouse-load audit
--------------------------
The DBA reported a steady stream of ``SELECT 1`` traffic (plus, on the
deployment-checks card, catalogue scans, a rolled-back ``CREATE TABLE``
DDL attempt, and a ``WAITFOR DELAY`` probe) arriving from this
application while nobody was running anything. The source was
``web/admin/main.js``'s 30-second auto-refresh, which re-ran EVERY card,
including ``GET /admin/health/checks`` (every one of
``scripts.verify_deployment``'s checks, run fresh) and
``GET /admin/schema-drift`` (a full catalogue reflection --
``get_table_names``/``get_columns`` for every table of every schema) --
with one admin tab left open all day, that is thousands of catalogue
scans and DDL attempts nobody asked for.

Two independent fixes land together:

1. ``web/admin/main.js``'s auto-refresh no longer includes these cards at
   all (see that file's ``EXPENSIVE_CARDS`` set) -- they load once when
   the page opens and again only when the operator presses that card's
   own refresh button.
2. Even a deliberate press should not force a fresh, expensive run every
   single time -- an operator who reloads the page twice in a minute, or
   who has the panel open in two tabs, should not double the warehouse
   load a single glance would have cost. This module is that second
   fix: a small process-wide cache, one entry per card, that
   :mod:`api.admin_routes` and :mod:`api.admin_ops_routes` consult before
   running the expensive thing again.

Deliberately NOT the same object as :mod:`api.query_cache`'s
``QueryCache`` (that cache is keyed on a question/mode pair and exists to
avoid re-asking the LLM+warehouse for an *end-user* query) or
:mod:`api.health`'s own ``_cache_lock``/``_cached_response`` pair (a
single, unkeyed slot purpose-built for exactly one probe). This module
generalises the same "collapse concurrent callers onto one computation,
serve the same cached result for the same input"" shape
(:mod:`api.health`'s own "Result caching" section explains the
reasoning) to more than one card, each under its own key, without
duplicating that lock-held-across-the-whole-computation pattern a third
time.

A failed computation is never cached: this module only ever stores a
computation that returned normally, so a warehouse that is briefly
unreachable does not get pinned as "unreachable" in the cache for the
rest of the TTL once it recovers -- the next call (cached or not) always
tries again. This also keeps ``GET /admin/schema-drift``'s existing
503-on-unreachable-warehouse behaviour unchanged: nothing here catches or
reshapes that exception, it simply is not cached.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass
class CachedResult:
    """What :meth:`TTLCache.get_or_compute` hands back."""

    value: object
    cached: bool
    age_seconds: float
    ttl_seconds: float


class TTLCache:
    """One cached value per string key, each guarded by the SAME lock.

    A single ``threading.Lock`` shared across every key (rather than one
    lock per key) is a deliberate simplification: this cache holds at
    most a handful of entries (one per expensive admin card, currently
    two), all of them administrator-triggered and none of them on any
    latency-sensitive request path -- unlike :class:`api.query_cache.QueryCache`,
    which every analyst question can pass through. Serialising the rare
    case where two DIFFERENT expensive cards are refreshed at literally
    the same instant is a trivial cost; a second lock object per key
    would be complexity this cache does not need to earn.

    Held for the full computation, not just the read/write around it --
    same reasoning as :mod:`api.health`'s ``_cache_lock``: a burst of
    calls that arrives while the cache is cold (or being force-refreshed)
    collapses onto a single in-flight computation instead of each caller
    racing to start its own.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, object] = {}
        self._computed_at: dict[str, float] = {}

    def get_or_compute(
        self,
        key: str,
        ttl_seconds: float,
        compute: Callable[[], T],
        *,
        force: bool = False,
    ) -> CachedResult:
        """Return the cached value for *key*, or compute and cache a fresh one.

        Parameters
        ----------
        key:
            Cache slot. Independent cards must use distinct keys.
        ttl_seconds:
            How long a cached value stays valid. ``<= 0`` disables
            caching entirely -- every call computes fresh (mirrors
            :attr:`config.Settings.cache_ttl_seconds`'s own "0 =
            disabled" convention).
        compute:
            Zero-argument callable that produces the value to cache.
            Invoked while holding this cache's lock (see class
            docstring), and if it raises, the exception propagates to
            the caller unchanged and NOTHING is cached (see module
            docstring's "a failed computation is never cached").
        force:
            Bypass a valid cached value and compute (and cache) fresh --
            what a card's own "refresh" button (``?refresh=1``) sets.
        """
        with self._lock:
            now = time.monotonic()
            computed_at = self._computed_at.get(key)
            if (
                not force
                and computed_at is not None
                and ttl_seconds > 0
                and (now - computed_at) < ttl_seconds
            ):
                return CachedResult(
                    value=self._values[key],
                    cached=True,
                    age_seconds=now - computed_at,
                    ttl_seconds=ttl_seconds,
                )

            value = compute()
            now = time.monotonic()
            self._values[key] = value
            self._computed_at[key] = now
            return CachedResult(
                value=value, cached=False, age_seconds=0.0, ttl_seconds=ttl_seconds,
            )

    def reset(self, key: str | None = None) -> None:
        """Drop one cached entry, or (``key=None``) every entry.

        Test-only seam -- mirrors :func:`api.health.reset_health_cache`.
        Without it, whichever test populates a key first would leak its
        cached result into every later test that shares this process,
        exactly the risk that function's own docstring describes.
        """
        with self._lock:
            if key is None:
                self._values.clear()
                self._computed_at.clear()
            else:
                self._values.pop(key, None)
                self._computed_at.pop(key, None)


#: Process-wide singleton, one entry per expensive admin card. Keys currently
#: in use: "health_checks" / "health_checks_deep" (api.admin_routes) and
#: "schema_drift" (api.admin_ops_routes).
admin_expensive_cache = TTLCache()


def reset_admin_expensive_cache() -> None:
    """Clear every entry in :data:`admin_expensive_cache`. Test-only seam."""
    admin_expensive_cache.reset()
