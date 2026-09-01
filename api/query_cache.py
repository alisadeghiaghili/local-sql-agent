"""Thread-safe TTL query-result cache.

Caches the full ``QueryResponse`` keyed on ``(prefix_version, question, mode)``,
and — Phase 2 task 6 — a second time on ``(prefix_version, sql, mode)`` so a
*different* question that happens to generate the same SQL can reuse the
execution result without hitting the database again.

Design
------
* **LRU + TTL**: entries are evicted when they exceed ``ttl_seconds`` OR
  when the store exceeds ``max_size`` (oldest-inserted evicted first via
  ``OrderedDict``).
* **Thread-safe**: a single ``threading.Lock`` guards all mutations.
* **Opt-out**: callers pass ``cache=False`` to ``run_query`` to skip the
  cache entirely (e.g. ``mode='sql'`` where freshness matters).
* **Metrics**: ``hits``, ``misses``, ``evictions`` counters for observability.
* **Normalised keys**: the question half of the key is normalised first
  (see :func:`_normalize_question`) so whitespace differences, Persian vs.
  Arabic-Indic digits, ZWNJ, and ي/ك vs. ی/ک don't create separate cache
  entries for what is, for every practical purpose, the same question.
* **Prefix-versioned keys**: every key embeds
  ``prompt_engine.static_prefix.prefix_version`` for the system prompt in
  effect, so a knowledge-base change (a business rule edited, a table
  added) invalidates old entries by construction — they simply become
  unreachable under the new version's keys, rather than requiring an
  explicit flush that's easy to forget.

Usage::

    from api.query_cache import query_cache

    entry = query_cache.get("سوال", "full", prefix_version="a1b2c3")
    if entry is None:
        ...  # compute
        query_cache.set("سوال", "full", response, prefix_version="a1b2c3")

    query_cache.clear()          # flush all
    query_cache.stats()          # {hits, misses, evictions, size}
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import NamedTuple

import config as cfg
from api.models import QueryResponse

# ---------------------------------------------------------------------------
# Question normalisation
# ---------------------------------------------------------------------------

#: Persian digits (U+06F0-06F9) and Arabic-Indic digits (U+0660-0669) mapped
#: to ASCII 0-9, so "۱۴۰۲" and "١٤٠٢" and "1402" all normalise to the same
#: cache key.
_DIGIT_MAP = {
    **{chr(0x06F0 + i): str(i) for i in range(10)},  # Persian
    **{chr(0x0660 + i): str(i) for i in range(10)},   # Arabic-Indic
}
_DIGIT_TABLE = str.maketrans(_DIGIT_MAP)

#: Zero-width non-joiner (U+200C) — common inside Persian compound words
#: (می‌خواهم) but irrelevant to cache-key equality; stripped outright.
_ZWNJ = "‌"

#: Arabic-form letters folded to their Persian equivalents: ي (U+064A) -> ی
#: (U+06CC), ك (U+0643) -> ک (U+06A9). Both spellings are common in
#: Persian text depending on input method/keyboard.
_LETTER_MAP = {"ي": "ی", "ك": "ک"}
_LETTER_TABLE = str.maketrans(_LETTER_MAP)

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_question(question: str) -> str:
    """Fold a question to a canonical form for cache-key equality.

    Applies, in order: Persian/Arabic-Indic digit folding, ZWNJ removal,
    ي/ك → ی/ک letter folding, whitespace collapsing, ASCII lowercasing, and
    stripping. None of this changes the *meaning* of the question — it only
    removes differences a user would never notice when phrasing the exact
    same request twice.

    Parameters
    ----------
    question:
        Raw question text.

    Returns
    -------
    str
        Normalised form, safe to use as (part of) a cache key.

    Examples
    --------
    >>> _normalize_question("  خرید   در ۱۴۰۲  ")
    'خرید در 1402'
    >>> _normalize_question("خرید در ١٤٠٢") == _normalize_question("خرید در 1402")
    True
    >>> _normalize_question("علي") == _normalize_question("علی")
    True
    >>> _normalize_question("می‌خواهم") == _normalize_question("میخواهم")
    True

    Disabled via ``Settings.cache_normalize_questions=False``, only
    whitespace is stripped (the pre-Phase-2 behaviour) — kept as an
    escape hatch in case aggressive folding ever proves wrong for a
    specific deployment's data:

    >>> from config import override_settings
    >>> with override_settings(cache_normalize_questions=False):
    ...     _normalize_question("  خرید در ۱۴۰۲  ")
    'خرید در ۱۴۰۲'
    """
    if not cfg.settings.cache_normalize_questions:
        return question.strip()
    text = question.translate(_DIGIT_TABLE)
    text = text.replace(_ZWNJ, "")
    text = text.translate(_LETTER_TABLE)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.lower()


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
        Maximum number of entries before LRU eviction kicks in. Applied
        independently to the question-keyed store and the SQL-keyed store.
    """

    def __init__(self, ttl_seconds: int = 300, max_size: int = 256) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._store: OrderedDict[tuple[str, str, str, str], _CacheEntry] = OrderedDict()
        # Secondary index: (prefix_version, mode, normalised_sql, scope_key) -> entry.
        # Populated alongside `_store` by set() when `sql` is supplied, so a
        # DIFFERENT question that happens to generate the SAME SQL can reuse
        # the execution result (see api/runner.py's sql_cache_lookup hook
        # passed into SQLAgent.run) without hitting the database again.
        self._sql_store: OrderedDict[tuple[str, str, str, str], _CacheEntry] = OrderedDict()
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

    @staticmethod
    def _question_key(
        question: str, mode: str, prefix_version: str, scope_key: str = "",
    ) -> tuple[str, str, str, str]:
        return (prefix_version, _normalize_question(question), mode, scope_key)

    @staticmethod
    def _sql_key(
        sql: str, mode: str, prefix_version: str, scope_key: str = "",
    ) -> tuple[str, str, str, str]:
        # SQL is not natural-language text, so only whitespace collapsing
        # applies -- Persian-digit/letter folding would be meaningless (and
        # potentially wrong: a literal N'١٤٠٢' string value should not be
        # rewritten).
        normalized = _WHITESPACE_RE.sub(" ", sql).strip()
        return (prefix_version, normalized, mode, scope_key)

    def get(
        self, question: str, mode: str, *, prefix_version: str = "", scope_key: str = "",
    ) -> QueryResponse | None:
        """Return a copy of the cached response, or ``None`` (miss / expired / disabled).

        A fresh ``model_copy()`` is returned on every call — never the
        instance actually stored — so a caller mutating the returned
        object (e.g. ``api/server.py`` setting ``elapsed_seconds`` after
        every request) can never corrupt the cached entry, whether this
        is the only reader or one of many concurrent cache hits sharing
        the same key.

        Parameters
        ----------
        question, mode:
            As before.
        prefix_version:
            Fingerprint of the static prompt prefix in effect (see
            ``prompt_engine.static_prefix.prefix_version``). Defaults to
            ``""`` for callers that don't version their cache (e.g. tests
            constructing a bare ``QueryCache``) — a knowledge-base change
            then relies on an explicit ``clear()`` instead of automatic
            invalidation.
        scope_key:
            The caller's cache-partition key (see
            :func:`security.auth.scope_key`) — two callers with
            identical scope keys share entries; different scope keys can
            never collide, even for the same question/mode/prefix.
            Defaults to ``""``, a single shared partition, for callers
            that don't scope their cache (e.g. tests constructing a bare
            ``QueryCache``, or any caller with no authenticated
            principal at all).
        """
        if not self.enabled:
            return None

        key = self._question_key(question, mode, prefix_version, scope_key)
        return self._get_from(self._store, key)

    def get_by_sql(
        self, sql: str, mode: str, *, prefix_version: str = "", scope_key: str = "",
    ) -> QueryResponse | None:
        """Look up a cached response by generated SQL text instead of question.

        Lets two different questions that happen to generate the same SQL
        share one execution result. Callers that only need the rows should
        read ``.result`` off the returned :class:`~api.models.QueryResponse`
        (e.g. to rebuild a ``pandas.DataFrame``) rather than returning this
        object to the client directly — it reflects whichever question
        generated it first, not the caller's own question.

        See :meth:`get` for *scope_key*.
        """
        if not self.enabled:
            return None

        key = self._sql_key(sql, mode, prefix_version, scope_key)
        return self._get_from(self._sql_store, key)

    def _get_from(
        self,
        store: OrderedDict[tuple[str, str, str, str], _CacheEntry],
        key: tuple[str, str, str, str],
    ) -> QueryResponse | None:
        now = time.monotonic()
        with self._lock:
            entry = store.get(key)
            if entry is None:
                self._misses += 1
                return None

            if now > entry.expires_at:
                del store[key]
                self._evictions += 1
                self._misses += 1
                return None

            store.move_to_end(key)
            self._hits += 1
            return entry.response.model_copy()

    def set(
        self,
        question: str,
        mode: str,
        response: QueryResponse,
        *,
        prefix_version: str = "",
        sql: str | None = None,
        scope_key: str = "",
    ) -> None:
        """Store a copy of *response* under ``(prefix_version, question, mode, scope_key)``.

        Storing ``response.model_copy()`` rather than *response* itself
        matters just as much as copying in :meth:`get`: the caller
        (``api/runner.py``) hands the very same object it just passed
        here straight back to ``api/server.py``, which then mutates
        ``elapsed_seconds`` on it — that mutation must not reach the
        object living inside the cache either.

        Parameters
        ----------
        question, mode, response:
            As before.
        prefix_version:
            See :meth:`get`.
        sql:
            The generated SQL text for this response, if known. When
            given, the response is ALSO indexed under
            ``(prefix_version, sql, mode, scope_key)`` so a different
            question that later generates the same SQL can reuse it (see
            :meth:`get_by_sql`). ``None`` (the default) skips SQL
            indexing — e.g. there is nothing meaningful to index for an
            empty/failed generation.
        scope_key:
            See :meth:`get`.
        """
        if not self.enabled:
            return

        stored = response.model_copy()
        expires_at = time.monotonic() + self._ttl

        with self._lock:
            self._set_in(
                self._store,
                self._question_key(question, mode, prefix_version, scope_key),
                stored, expires_at,
            )
            if sql:
                self._set_in(
                    self._sql_store,
                    self._sql_key(sql, mode, prefix_version, scope_key),
                    stored, expires_at,
                )

    def _set_in(
        self,
        store: OrderedDict[tuple[str, str, str, str], _CacheEntry],
        key: tuple[str, str, str, str],
        stored: QueryResponse,
        expires_at: float,
    ) -> None:
        if key in store:
            store.move_to_end(key)
            store[key] = _CacheEntry(stored, expires_at)
            return

        while len(store) >= self._max_size:
            store.popitem(last=False)
            self._evictions += 1

        store[key] = _CacheEntry(stored, expires_at)

    def invalidate(
        self, question: str, mode: str, *, prefix_version: str = "", scope_key: str = "",
    ) -> bool:
        """Remove a specific question-keyed entry.  Returns True if it was present.

        Does not remove the corresponding SQL-keyed entry (if any) — that
        entry may still legitimately serve a *different* question that
        generated the same SQL, and this call has no way to know whether
        the caller wants that shared entry gone too.
        """
        key = self._question_key(question, mode, prefix_version, scope_key)
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        """Flush the entire cache (both the question- and SQL-keyed stores)."""
        with self._lock:
            self._store.clear()
            self._sql_store.clear()

    def stats(self) -> dict:
        """Return a snapshot of cache metrics."""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "size": len(self._store),
                "sql_index_size": len(self._sql_store),
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
            self._sql_store.clear()


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ---------------------------------------------------------------------------

query_cache = QueryCache(
    ttl_seconds=cfg.settings.cache_ttl_seconds,
    max_size=cfg.settings.cache_max_size,
)
