# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""An early warning for a response cache in front of the model server.

A gateway between this application and the model can keep a *response
cache* (semantic or prefix) and answer a new prompt with the reply stored for
a similar one. For SQL generation that is a silent wrong answer: "count of
active customers" gets the SQL cached for "count of inactive customers", the
guard accepts it (it is valid SQL), and the audit record shows a clean
request. ``scripts/verify_deployment.py`` has a check that looks for such a
cache on demand; this module is the cheap runtime counterpart.

The signal is two things at once, per request that reached the model:

* the generated SQL is **byte-identical** to the SQL of a **different**
  question seen within ``window_seconds``, and
* the model's reply took **less than** ``threshold_ms`` (a real generation
  of a statement takes longer than that on any model server; a cached reply
  is milliseconds).

Either alone proves nothing: two questions can legitimately map to the same
SQL, and a short reply can be a tiny statement. Together they are worth one
log line and a flag.

It never blocks, delays or changes a request. It keeps the last
``capacity`` (question hash, SQL hash) pairs in one :class:`~collections.OrderedDict`
per process, so a check is a hash and a dictionary lookup: O(1), no I/O, no
call to the model. The clock is injectable so a test needs no sleeping.

A streak of suspected requests logs **one** ``WARNING`` (per data source);
the streak ends after ``window_seconds`` without a suspected request.

Examples
--------
>>> now = [0.0]
>>> watch = ResponseCacheWatch(threshold_ms=50, window_seconds=60, clock=lambda: now[0])
>>> watch.observe("a", "count of active customers", "SELECT 1", 900)
False
>>> now[0] = 5.0
>>> watch.observe("a", "count of inactive customers", "SELECT 1", 8)
True
>>> watch.observe("a", "count of inactive customers", "SELECT 1", 900)
False
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Callable

logger = logging.getLogger(__name__)

__all__ = ["ResponseCacheWatch", "mark_suspected", "default_watch"]

def _digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8).digest()


def _question_digest(question: str) -> bytes:
    """Digest of *question* with case and whitespace folded (one question, many spellings)."""
    return _digest(" ".join((question or "").split()).casefold())


class ResponseCacheWatch:
    """Remembers recent (question, SQL) pairs and flags a fast repeat of SQL for another question.

    Args:
        threshold_ms: A reply faster than this counts as "too fast to be a
            generation". ``0`` (or less) turns the watch off.
        capacity: Entries kept, 200 by default (least recently used out first).
        window_seconds: How long an entry counts, and how long a streak
            lasts, 120 by default.
        clock: A monotonic clock returning seconds; replaceable in a test.
    """

    def __init__(
        self,
        *,
        threshold_ms: int = 50,
        capacity: int = 200,
        window_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold_ms = threshold_ms
        self.capacity = max(1, int(capacity))
        self.window_seconds = window_seconds
        self._clock = clock
        # (source, sql digest) -> (question digest, seen at)
        self._seen: OrderedDict[tuple[str, bytes], tuple[bytes, float]] = OrderedDict()
        # source -> when the last suspected request was seen
        self._last_suspect: dict[str, float] = {}
        self._lock = threading.Lock()

    def observe(self, source: str | None, question: str, sql: str, total_ms: int | None) -> bool:
        """Record one answered request; return whether it looks like a cached reply.

        Args:
            source: The data source the SQL was generated for (``None`` or
                ``""`` with a single source).
            question: The question as asked.
            sql: The generated SQL, as the model returned it after cleaning.
            total_ms: How long the model's reply took, ``None`` when unknown
                (never suspected then).

        Returns:
            True when a different question produced this exact SQL within the
            window and this reply took less than ``threshold_ms``.
        """
        if self.threshold_ms <= 0 or not sql or not question:
            return False
        source_key = source or ""
        key = (source_key, _digest(sql))
        question_hash = _question_digest(question)
        now = self._clock()
        with self._lock:
            previous = self._seen.get(key)
            suspected = (
                previous is not None
                and previous[0] != question_hash
                and now - previous[1] <= self.window_seconds
                and total_ms is not None
                and total_ms < self.threshold_ms
            )
            self._seen[key] = (question_hash, now)
            self._seen.move_to_end(key)
            while len(self._seen) > self.capacity:
                self._seen.popitem(last=False)
            new_streak = False
            if suspected:
                last = self._last_suspect.get(source_key)
                new_streak = last is None or now - last > self.window_seconds
                self._last_suspect[source_key] = now
        if new_streak:
            logger.warning(
                "Suspected response cache in front of the model%s: a different question "
                "returned byte-identical SQL in %d ms (under %d ms). A cache that answers "
                "similar prompts with a stored reply can serve the wrong SQL for a similar "
                "question. Run python scripts/verify_deployment.py (check 'Model endpoint "
                "answers each prompt itself'); the fix is to disable response caching for "
                "this client at the gateway or to point OPENAI_BASE_URL at the model server "
                "directly (runbook section 20.10). Requests are not blocked; the audit "
                "record's llm block carries suspected_cached_response.",
                f" (data source {source_key})" if source_key else "",
                total_ms, self.threshold_ms,
            )
        return suspected


_default_lock = threading.Lock()
_default: ResponseCacheWatch | None = None


def default_watch() -> ResponseCacheWatch:
    """The process-wide watch, built on first use from ``LLM_CACHE_SUSPECT_MS``."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                import config as cfg

                _default = ResponseCacheWatch(threshold_ms=cfg.settings.llm_cache_suspect_ms)
    return _default


def mark_suspected(
    llm: dict | None,
    *,
    source: str | None,
    question: str,
    sql: str,
    watch: ResponseCacheWatch | None = None,
) -> dict | None:
    """Set ``suspected_cached_response`` in the audit ``llm`` block when the watch flags the request.

    Args:
        llm: The ``llm`` block of the request (changed in place); ``None``
            leaves nothing to mark.
        source: The data source the SQL was generated for.
        question: The question as asked.
        sql: The generated SQL.
        watch: The watch to ask; the process-wide one by default.

    Returns:
        *llm*, with ``suspected_cached_response: True`` added when flagged
        and untouched otherwise. Never raises: a failure here must not touch
        the request.

    Examples
    --------
    >>> w = ResponseCacheWatch(clock=lambda: 0.0)
    >>> mark_suspected({"total_ms": 900}, source=None, question="q1", sql="SELECT 1", watch=w)
    {'total_ms': 900}
    >>> mark_suspected({"total_ms": 9}, source=None, question="q2", sql="SELECT 1", watch=w)
    {'total_ms': 9, 'suspected_cached_response': True}
    """
    if not isinstance(llm, dict):
        return llm
    try:
        if (watch or default_watch()).observe(source, question, sql, llm.get("total_ms")):
            llm["suspected_cached_response"] = True
    except Exception:  # noqa: BLE001 - an early warning must never fail a request
        logger.debug("response cache watch failed", exc_info=True)
    return llm
