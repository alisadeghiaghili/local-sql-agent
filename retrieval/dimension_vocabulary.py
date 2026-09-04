# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Small-dimension vocabulary prefetch and in-question matching -- Phase 5b redesign.

``retrieval/value_resolver.py`` originally searched the *database* for a
span extracted from the question (a ``mention``). Wired into
``ContextRetriever`` with the whole question as that span (this codebase
has no free-text extractor), the measured cost was 1 query per allowlisted
column per request, each a leading-wildcard ``LIKE`` (an unindexable scan)
against potentially-large tables, for a guaranteed miss -- a full sentence
never equals a short dimension value. Correctness-safe, cost-unsafe. See
``retrieval.value_resolver``'s module docstring for the measured numbers
and why that call site was removed.

This module inverts the direction instead: for the small-cardinality
dimensions listed in :data:`PREFETCH_COLUMNS`, fetch each one's *entire*
distinct value set (cached, refreshed out of band -- never *awaited* on the
request path), and search the *question* for one of those values. This is
exactly the same direction ``retrieval.value_retriever.ValueRetriever.extract_ring``
already searches a hand-maintained alias file -- the only difference is the
vocabulary comes from the warehouse instead of ``project_config/aliases.yaml``,
so it stays current without anyone hand-editing YAML. Because the search
runs against an in-memory list the request already has, not the database,
the extraction problem that blocked ``resolve_value`` here dissolves rather
than needing to be solved.

Deliberately excluded: ``Customer.Name`` / ``Supplier.Customer_Name``
--------------------------------------------------------------------------
``Customer`` and ``Supplier`` can be very large tables -- prefetching their
entire vocabulary into memory is not sane, and they are exactly the two
dimensions that genuinely need a real mention extractor rather than either
of this phase's two approaches. They stay resolvable only through
``retrieval.value_resolver.resolve_value``, which remains fully built and
tested but is not called from anywhere in this codebase today (see that
module's docstring) -- there is deliberately no call site at all rather
than one that would guarantee a miss and pay a scan for it.

Stale-while-revalidate, warmed lazily
----------------------------------------
The first version of this module made the request path a pure cache read
that returned ``None`` on any miss -- fresh or expired -- and relied
entirely on an *opt-in*, default-``False``
:data:`~config.Settings.dimension_vocabulary_warm_on_startup` flag to ever
populate the cache at all. Out of the box, with the flag untouched, that
meant the cache was NEVER filled and the whole feature was silently inert
-- a different-shaped version of the exact "safe no-op nobody notices"
problem that got the previous ``resolve_value``-as-mention-search wiring
removed. It also meant that even with the flag on, resolution stopped
working an hour (the TTL) after every deploy until the next restart, with
nothing to diagnose why.

This version fixes both by making :func:`match_question_against_vocabulary`
behave according to the cache entry's state, per column:

* **Fresh** -- serve it. No query, no background action.
* **Stale** (past TTL, but a previous value exists) -- **serve the stale
  values immediately** *and* kick off a background refresh for next time.
  A stale trading-hall or commodity name is overwhelmingly better than no
  name at all -- these are slow-moving reference values, not volatile
  figures -- so this module does not throw away a perfectly good previous
  answer just because its TTL passed.
* **Absent** (never cached) -- return no candidates immediately (today's
  safe-miss behaviour, unchanged) *and* kick off a background refresh so
  the *next* request has something to serve.

No request ever blocks on a dimension scan (the background refresh is
fire-and-forget, never awaited), and the feature is no longer inert by
default: the first real question that mentions an uncached dimension is
still a miss, but it also self-heals the cache for the next one, with no
env var required. :data:`~config.Settings.dimension_vocabulary_warm_on_startup`
still exists and is still worth turning on for a real deployment, but its
meaning has changed: it is now a pure *optimisation* -- it pays the first
dimension's fetch cost at startup instead of on the first request that
needs it -- not the only thing standing between this feature and doing
nothing.

Background refresh: single-flight, non-poisoning, rate-limited on failure
-------------------------------------------------------------------------
Three properties the background trigger (:func:`_trigger_background_refresh`)
guarantees:

1. **Single-flight per ``(table, column)``.** A module-level in-flight set,
   guarded by a lock, means a cold or stale cache under concurrent requests
   launches at most one refresh per key -- every other concurrent caller
   for the same key sees the in-flight marker and does not submit a
   second one; it just gets served whatever the cache currently holds
   (stale or nothing), exactly as if no refresh were running at all.
2. **A failure never poisons the entry.** :func:`refresh_vocabulary` only
   ever writes to the cache *after* a successful fetch (see its body) --
   an exception raised while fetching is caught by the background
   wrapper, logged, and the previous cached state (stale or absent) is
   left completely untouched. A later successful refresh is never blocked
   by an earlier failed one.
3. **A failing key backs off** -- :data:`_BACKGROUND_REFRESH_BACKOFF_SECONDS`
   (60s) must elapse after a failed background attempt for a given key
   before another automatic one is triggered for it. Without this, a
   database outage would turn every request that mentions the affected
   dimension into another doomed connection attempt -- a self-inflicted
   retry storm on top of an already-down dependency. The backoff applies
   only to the *automatic* trigger; an explicit :func:`refresh_vocabulary`
   / :func:`warm_all` call (an operator's own retry, or the opt-in startup
   warm-up) always attempts immediately, ignoring it.

:func:`set_background_refresh_enabled` is the escape hatch this suite's
``tests/conftest.py`` uses to turn the trigger off globally -- see "Test
isolation" below.

Test isolation -- no test may reach a real database because of this
------------------------------------------------------------------------
``tests/conftest.py``'s existing ``_no_real_database`` autouse fixture
already makes ``database.connection.create_engine`` raise
``AssertionError`` for every test (see its docstring: an unmocked engine
construction against the placeholder ``DB_CONNECTION_URL`` would otherwise
hang on DNS/login timeout for ~21s). That turns an accidental background
refresh into a *fast*, caught, logged failure rather than a hang -- but a
background thread quietly reaching that mock on every cold/stale
dimension lookup during ordinary route tests is still exactly the class of
hidden-async-work bug that produced this phase's one real test flake (a
different module's orphaned ``time.sleep`` in a shared thread pool, still
running during unrelated, later tests -- see
``tests/test_value_resolver.py``'s docstring on
``test_timeout_falls_back_to_no_match_instead_of_raising``). Silently
swallowing the resulting ``AssertionError`` would hide that class of
problem again, just one module over.

So ``tests/conftest.py`` carries a second autouse fixture,
``_no_background_dimension_refresh``, that calls
:func:`set_background_refresh_enabled(False) <set_background_refresh_enabled>`
for the duration of every test and restores it afterwards -- mirroring
``_no_real_database``'s exact shape. With the trigger disabled, a
cold/stale lookup during an ordinary test still returns synchronously
(no candidates, or stale candidates) but launches nothing. The tests in
``tests/test_dimension_vocabulary.py`` that specifically exercise the
background-refresh mechanism re-enable it locally, always with an injected
``execute_fn`` -- never the real database -- and always inside a
``try/finally`` that restores the disabled state.

Two more properties earned the hard way, both by a full-suite run
actually reaching a real (if unreachable) database once during
development of this feature, not by inspection:

* **Every background thread is a daemon thread**, not a shared
  ``ThreadPoolExecutor`` worker. A ``ThreadPoolExecutor``'s workers are
  non-daemon by design, so the interpreter's ``atexit`` machinery waits
  for every queued/running task before the process can exit. A task still
  running past its owning test's teardown was observed keeping the whole
  ``pytest`` process alive *after* the final summary line had already
  printed -- by which point every per-test mock, including
  ``_no_real_database``'s, had already unwound -- so the leftover task
  ran unsupervised against a completely unpatched environment and reached
  a real, if unreachable, database. A daemon thread cannot do that: if the
  interpreter wants to exit, it is simply cut off.
* **The two enable/disable fixtures save and restore the previous value**
  (via :func:`is_background_refresh_enabled`), never a hardcoded constant.
  ``_no_background_dimension_refresh`` (conftest, broad) and
  ``TestBackgroundRefresh``'s own class fixture (narrow) both run at
  function scope; a test in that class sees conftest's setup (``False``)
  run first, then the class fixture's setup (``True``) run second, and
  teardown unwinds in the opposite order. A conftest teardown that
  hardcoded ``True`` would win the last write for every test in that
  class, leaving the trigger armed for whichever *ordinary* test happened
  to run next -- which is exactly the second half of how the real
  connection attempt above happened. Save/restore makes each fixture
  responsible only for the value it actually changed.

Matching rules
--------------
* Both the question and every vocabulary value are normalised through
  :func:`core.persian.normalize_for_matching` before comparison -- the same
  normaliser every other Persian-text match in this codebase uses.
* **Longest match wins.** If a table's vocabulary contains both «فولاد» and
  «فولاد مبارکه» and the question contains «فولاد مبارکه», the match is the
  longer value -- a shorter value that is also a substring of a longer
  match present in the same vocabulary never wins over it.
* :data:`MIN_MATCH_LENGTH` (3 normalised characters) -- values shorter than
  this are never used for matching at all. A 1- or 2-character dimension
  value (there is no live database in this environment to confirm whether
  any allowlisted column actually has one) would otherwise appear inside
  unrelated Persian words constantly and produce false matches; 3
  characters is short enough to keep legitimate short codes (e.g. a
  3-letter commodity symbol) while excluding that noise. Excluded values
  are counted and logged by :func:`refresh_vocabulary` so an operator can
  see exactly what this threshold is dropping for their real data, rather
  than this module guessing blind.
* **Several dimensions may match in one question.** A question naming both
  a commodity and a trading hall must resolve both --
  :func:`match_question_against_vocabulary` returns one entry per table
  that matched, not just the first.
* **Ties are ambiguous, not silently picked.** If two distinct values in
  the same table's vocabulary both match at the longest length found, that
  table's result is a :class:`~session.models.Clarification` naming both,
  never one silently chosen -- the exact same "several matches -> declare,
  don't pick" contract ``resolve_value`` upholds on its own path.

ACL
---
:func:`match_question_against_vocabulary` takes the caller's
:class:`~security.auth.Principal` and excludes any column in
``principal.denied_columns`` from the vocabulary pool it searches --
enforced every time a question is matched, regardless of which principal
(or the background refresher, which has none) populated the shared cache.
The underlying values are not principal-specific (unlike ``resolve_value``'s
per-scope cache, there is no ``Principal``-scoped cache partition here at
all): two principals see the identical warehouse data, but a principal
whose ACL denies a column can never have that column's values used to
explain a match, exactly mirroring what
:func:`~security.sql_guard.validate_sql`'s ``denied_columns`` already
enforces for generated SQL.

Audit
-----
Like ``resolve_value``, this module's result carries ``resolved_columns``
-- ``"Table.Column"`` identifier strings for every column actually
consulted (cache hit, fresh or stale) during a match -- and never a
matched *value*. The matched values themselves reach
``RetrievalContext.filters`` the same way any other filter does; the audit
trail's existing "names, never values" guarantee (see
``observability.audit.AuditRecord``) is what keeps them out of the
compliance log, not anything specific to this module.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Sequence

import config as cfg
from core.persian import normalize_for_matching
from retrieval.value_resolver import ExecuteParamsFn
from schema_data.registry import get_prefetchable_columns, get_table_schema_qualifiers
from security.auth import ANONYMOUS, Principal
from security.dialects import get_dialect_profile
from security.sql_guard import transpile_sql
from session.models import Clarification

logger = logging.getLogger(__name__)

#: table -> its schema/db qualifier (e.g. "Auction_Dim"), loaded from
#: schema.yaml -- see ``retrieval.value_resolver``'s identical-shaped
#: constant; not shared with that module beyond both calling the same
#: ``schema_data.registry`` accessor, since the two allowlists (prefetch
#: vs. resolve) can legitimately differ in which tables they cover.
_TABLE_SCHEMAS: dict[str, str] = get_table_schema_qualifiers()

#: table -> allowlisted, prefetchable columns, from schema.yaml's per-table
#: `prefetchable_columns` field -- see
#: schema_data.registry.get_prefetchable_columns. A strict SUBSET of
#: ``retrieval.value_resolver.RESOLVABLE_COLUMNS`` in every deployment so
#: far -- ``Customer`` and ``Supplier`` are deliberately excluded from it;
#: see the module docstring.
PREFETCH_COLUMNS: dict[str, tuple[str, ...]] = get_prefetchable_columns()

#: Minimum normalised length (characters) for a vocabulary value to be used
#: for matching at all -- see the module docstring's "Matching rules".
MIN_MATCH_LENGTH = 3

#: Floor between automatic background-refresh attempts for a key that just
#: failed -- see the module docstring's "Background refresh" section.
#: Does not apply to an explicit refresh_vocabulary()/warm_all() call.
_BACKGROUND_REFRESH_BACKOFF_SECONDS = 60.0


def _prefetch_query(table: str, column: str, dialect: str = "tsql") -> str:
    """The fixed template for fetching a dimension's whole vocabulary.

    No ``WHERE``, no user input of any kind -- ``table``/``column`` are
    always drawn from :data:`PREFETCH_COLUMNS`, and the schema qualifier
    from :data:`_TABLE_SCHEMAS`, never from a question, so there is
    nothing here for ``retrieval.value_resolver``'s injection discussion to
    apply to. ``TOP (?)`` is still a defensive cap (bound, not
    interpolated) in case a table's cardinality is ever larger than
    expected -- not because this path is meant to hit it in practice.

    Multi-dialect: same "author in tsql, transpile" approach as
    ``retrieval.value_resolver._build_query`` -- see that function's
    docstring for the full reasoning, including why a schema-less dialect
    (``dialect_profile.schema_qualification == "none"`` -- SQLite) must
    have its schema qualifier omitted *before* transpiling, not stripped
    after.

    Examples
    --------
    The exact schema qualifier depends on ``schema.yaml`` (see
    :data:`_TABLE_SCHEMAS`) -- not asserted literally here so this doctest
    passes under any deployment's config, including CI's
    ``project_config.example/``:

    >>> sql = _prefetch_query("Customer", "Name")
    >>> "WHERE" in sql
    False
    >>> sql.startswith("SELECT DISTINCT TOP (?) [Name] FROM [")
    True
    >>> sql.endswith("].[Customer]")
    True
    """
    profile = get_dialect_profile(dialect)
    if profile.schema_qualification == "none":
        table_ref = f"[{table}]"
    else:
        schema = _TABLE_SCHEMAS[table]
        table_ref = f"[{schema}].[{table}]"
    tsql = f"SELECT DISTINCT TOP (?) [{column}] FROM {table_ref}"
    if dialect == "tsql":
        return tsql
    return transpile_sql(tsql, source_dialect="tsql", target_dialect=dialect)


# ---------------------------------------------------------------------------
# Cache -- keyed on (table, column) ONLY, no principal/scope component.
# The underlying values are not principal-specific; ACL is enforced by
# excluding a denied column from the search pool at match time (see
# match_question_against_vocabulary), not by partitioning the cache.
#
# Unlike a plain TTL cache, an expired entry is NOT deleted on read -- it
# is kept and reported as "stale" so match_question_against_vocabulary can
# still serve it while a refresh runs in the background. It is only ever
# replaced by a subsequent successful refresh_vocabulary() call.
# ---------------------------------------------------------------------------


class _VocabularyCache:
    def __init__(self) -> None:
        self._store: OrderedDict[tuple[str, str], tuple[list[str], float]] = OrderedDict()
        self._lock = threading.Lock()

    def get_with_state(self, table: str, column: str) -> tuple[list[str] | None, bool]:
        """Return ``(values, is_fresh)``.

        ``values`` is ``None`` only when the key has never been cached at
        all. Once anything has been cached, ``values`` keeps returning the
        most recently fetched list forever (until a newer fetch replaces
        it) even past its TTL -- ``is_fresh`` is what tells the caller
        whether that TTL has passed, not whether a value exists.
        """
        key = (table, column)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None, False
            values, expires_at = entry
            return list(values), time.monotonic() <= expires_at

    def set(self, table: str, column: str, values: list[str]) -> None:
        ttl = cfg.settings.dimension_vocabulary_ttl_seconds
        expires_at = time.monotonic() + ttl if ttl > 0 else time.monotonic() - 1
        with self._lock:
            # ttl <= 0 ("disabled") still stores the value -- unlike the
            # old delete-on-read design, storing nothing here would mean
            # NEVER serving a stale fallback either, which defeats the
            # point of this cache existing at all. Instead it is stored
            # already-expired, so get_with_state() reports it as stale
            # immediately: it is served once, and a fresh background
            # refresh is triggered on every subsequent read, matching the
            # old "effectively uncached" behaviour without discarding a
            # value this module already has in hand.
            self._store[(table, column)] = (list(values), expires_at)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_cache = _VocabularyCache()


def clear_vocabulary_cache() -> None:
    """Flush the module-level vocabulary cache. Test-only escape hatch."""
    _cache.clear()


def get_cached_vocabulary(table: str, column: str) -> list[str] | None:
    """Pure cache read -- never issues a query, never blocks.

    Returns the most recently cached values for ``(table, column)``,
    fresh or stale, or ``None`` if never cached at all. Callers that care
    about freshness (this module's own
    :func:`match_question_against_vocabulary`) use
    :meth:`_VocabularyCache.get_with_state` directly instead; this
    function is the simple "whatever we've got" read for anything else.
    """
    values, _fresh = _cache.get_with_state(table, column)
    return values


def _default_execute_fn(sql: str, params):
    """Lazily-imported production ``execute_fn`` -- avoids pulling in the
    database/SQLAlchemy dependency chain for callers (most of this test
    suite) that always inject their own."""
    from database.executor import execute_sql_params

    return execute_sql_params(sql, params)


def refresh_vocabulary(
    table: str, column: str, execute_fn: ExecuteParamsFn | None = None,
) -> list[str]:
    """Fetch ``(table, column)``'s full distinct value set and cache it.

    The only function in this module that touches the database. Called by
    :func:`warm_all` (startup), an operator's own refresh schedule, or the
    background self-healing trigger (:func:`_trigger_background_refresh`)
    -- never synchronously by the per-request match path.

    Parameters
    ----------
    table, column:
        Must be an entry in :data:`PREFETCH_COLUMNS` — this function does
        not itself enforce the allowlist (callers are internal/operational,
        not request-driven), but every call site in this codebase only
        ever iterates :data:`PREFETCH_COLUMNS` to begin with.
    execute_fn:
        ``(sql, params) -> DataFrame``. Defaults to
        :func:`database.executor.execute_sql_params`. Inject a fake to
        test with no live database.

    Returns
    -------
    list[str]
        Every distinct value fetched (including ones shorter than
        :data:`MIN_MATCH_LENGTH` — the length filter is applied at match
        time, not here, so the cache always reflects exactly what the
        warehouse holds).

    Raises
    ------
    Exception
        Whatever *execute_fn* itself raises, propagated unchanged -- the
        cache is only written to on success (see the body below), so a
        raise here never touches (and never "poisons") whatever was
        already cached. Callers that must not raise (the background
        trigger) catch this themselves; :func:`warm_all` catches it
        per-column so one bad dimension doesn't stop the rest.
    """
    if execute_fn is None:
        execute_fn = _default_execute_fn

    sql = _prefetch_query(table, column, dialect=cfg.settings.sql_dialect)
    frame = execute_fn(sql, (cfg.settings.default_top_n,))
    values = [] if frame is None or frame.empty else [str(v) for v in frame.iloc[:, 0].tolist()]
    _cache.set(table, column, values)  # only reached on success

    excluded = sum(1 for v in values if len(normalize_for_matching(v)) < MIN_MATCH_LENGTH)
    logger.info(
        "dimension_vocabulary: refreshed %s.%s -> %d values (%d below "
        "MIN_MATCH_LENGTH=%d excluded from matching)",
        table, column, len(values), excluded, MIN_MATCH_LENGTH,
    )
    return values


def warm_all(execute_fn: ExecuteParamsFn | None = None) -> dict[str, int]:
    """Refresh every :data:`PREFETCH_COLUMNS` entry. Meant for startup.

    A failure fetching one ``(table, column)`` is logged and does not stop
    the rest -- one bad dimension (a renamed column, a transient DB hiccup
    at startup) must not leave every OTHER dimension's cache cold too. Not
    called from any request path -- see the module docstring. Bypasses the
    background trigger's failure backoff entirely (it doesn't go through
    :func:`_trigger_background_refresh` at all) -- an explicit call, either
    an operator's or the opt-in startup warm-up's, always attempts
    immediately.

    Returns
    -------
    dict[str, int]
        ``{"Table.Column": value_count}`` for every pair that refreshed
        successfully — omits any pair that failed, so a caller can log or
        assert on exactly what warmed.
    """
    counts: dict[str, int] = {}
    for table, columns in PREFETCH_COLUMNS.items():
        for column in columns:
            key = f"{table}.{column}"
            try:
                values = refresh_vocabulary(table, column, execute_fn)
            except Exception as exc:  # noqa: BLE001 - one bad dimension must not block the rest
                logger.warning(
                    "dimension_vocabulary: warm_all failed for %s: %s", key, exc,
                )
                continue
            counts[key] = len(values)
    return counts


# ---------------------------------------------------------------------------
# Background self-healing refresh -- single-flight, non-blocking,
# rate-limited on failure. See the module docstring's "Background refresh"
# section for the three guarantees this provides.
#
# Each triggered refresh gets its own daemon thread rather than a shared
# ThreadPoolExecutor. Single-flight already caps concurrent work to at
# most one thread per (table, column) key -- bounded by the number of
# (table, column) pairs across PREFETCH_COLUMNS's values (a handful, for
# any deployment's schema.yaml) -- so a pool's queuing/reuse has no real
# benefit here.
# daemon=True matters far more than that: a ThreadPoolExecutor's worker
# threads are NOT daemons, which means the interpreter's atexit machinery
# waits for every queued/running task to finish before the process can
# exit. In this test suite that turned into a real, observed bug: a task
# left running past its owning test's teardown kept the process alive
# after pytest had already printed its final summary and every per-test
# mock (including tests/conftest.py's own database.connection.create_engine
# refusal) had unwound -- so the leftover task ran against a completely
# unpatched environment and reached a real, if unreachable, database. A
# daemon thread cannot do that: if the interpreter wants to exit, it is
# simply cut off, never granted a lingering window to run unsupervised
# against whatever real resources happen to be unmocked at that moment.
# ---------------------------------------------------------------------------

_bg_lock = threading.Lock()
#: Keys with a refresh currently running in a background thread.
_in_flight: set[tuple[str, str]] = set()
#: Keys whose most recent automatic attempt failed, and when -- consulted
#: only by the automatic trigger, never by refresh_vocabulary/warm_all.
_last_failure: dict[tuple[str, str], float] = {}

#: Module-wide on/off switch for the automatic background trigger. Default
#: True (production behaviour) -- see :func:`set_background_refresh_enabled`
#: and the module docstring's "Test isolation" section for who turns this
#: off, and why every test in this suite runs with it off.
_background_refresh_enabled = True


def is_background_refresh_enabled() -> bool:
    """Current value of the switch :func:`set_background_refresh_enabled` sets.

    A getter, not direct access to the module-level flag, so callers that
    need to save-and-restore it (``tests/conftest.py``'s autouse fixture,
    and ``tests/test_dimension_vocabulary.py``'s ``TestBackgroundRefresh``
    class fixture) restore whatever value was actually there beforehand
    rather than a hardcoded assumption -- two nested fixtures that both
    hardcode "restore to X" can leave the flag wrong for whichever test
    runs immediately after both unwind, which is exactly the bug this
    getter exists to let callers avoid.
    """
    return _background_refresh_enabled


def set_background_refresh_enabled(enabled: bool) -> None:
    """Enable/disable :func:`_trigger_background_refresh` process-wide.

    Production default is enabled (see :data:`_background_refresh_enabled`).
    ``tests/conftest.py``'s autouse ``_no_background_dimension_refresh``
    fixture disables this for the duration of every test in this suite and
    restores it afterwards, so a cold/stale cache during an ordinary route
    test never launches a background thread that (were it not for
    ``_no_real_database`` also being autouse) would try to reach a real
    database. See the module docstring's "Test isolation" section.
    """
    global _background_refresh_enabled
    _background_refresh_enabled = enabled


def _trigger_background_refresh(table: str, column: str) -> None:
    """Fire-and-forget refresh of ``(table, column)``. Never blocks, never raises.

    A no-op when :func:`set_background_refresh_enabled` has turned this
    off (every test in this suite), when a refresh for this exact key is
    already running (single-flight), or when this key failed recently and
    is still inside :data:`_BACKGROUND_REFRESH_BACKOFF_SECONDS` of that
    failure.
    """
    if not _background_refresh_enabled:
        return

    key = (table, column)
    now = time.monotonic()
    with _bg_lock:
        if key in _in_flight:
            return
        last_failure = _last_failure.get(key)
        if last_failure is not None and (now - last_failure) < _BACKGROUND_REFRESH_BACKOFF_SECONDS:
            return
        _in_flight.add(key)

    def _run() -> None:
        try:
            refresh_vocabulary(table, column)
            with _bg_lock:
                _last_failure.pop(key, None)
        except Exception as exc:  # noqa: BLE001 - must never propagate into a request
            logger.warning(
                "dimension_vocabulary: background refresh failed for %s.%s "
                "(will not retry automatically for %.0fs): %s",
                table, column, _BACKGROUND_REFRESH_BACKOFF_SECONDS, exc,
            )
            with _bg_lock:
                _last_failure[key] = time.monotonic()
        finally:
            with _bg_lock:
                _in_flight.discard(key)

    threading.Thread(
        target=_run, name=f"dim-vocab-bg-refresh-{table}.{column}", daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VocabularyMatchResult:
    """The outcome of one :func:`match_question_against_vocabulary` call.

    Parameters
    ----------
    filters:
        ``{table_name: matched_value}`` — one entry per candidate table
        with exactly one longest-length match. Ready to merge into
        ``RetrievalContext.filters`` unchanged.
    clarifications:
        One :class:`~session.models.Clarification` per candidate table
        whose longest match was a tie between several distinct values —
        never also present in ``filters`` for that same table.
    resolved_columns:
        ``"Table.Column"`` strings for every column actually consulted
        (cache hit, fresh or stale) during this match — column
        *identifiers* only, for the audit trail. A table left out
        entirely (cache miss / ACL-denied / not a prefetch table)
        contributes nothing here.
    """

    filters: dict[str, str] = field(default_factory=dict)
    clarifications: list[Clarification] = field(default_factory=list)
    resolved_columns: tuple[str, ...] = field(default_factory=tuple)


def match_question_against_vocabulary(
    question_normalized: str,
    candidate_tables: Sequence[str],
    *,
    principal: Principal = ANONYMOUS,
) -> VocabularyMatchResult:
    """Search *question_normalized* for a cached dimension value. Never blocks.

    Reads the cache and, on a stale or absent entry, fires a background
    refresh (see :func:`_trigger_background_refresh`) -- no database call
    happens synchronously as part of this function, directly or
    indirectly. A candidate table with no cached vocabulary at all (cold
    cache, not one of :data:`PREFETCH_COLUMNS`, or every allowlisted
    column denied by ``principal``) simply contributes nothing; this
    function never raises for any of those.

    Parameters
    ----------
    question_normalized:
        The question, already normalised through
        :func:`core.persian.normalize_for_matching` by the caller.
    candidate_tables:
        Table names to search — typically what
        ``retrieval.entity_retriever.EntityRetriever.retrieve`` selected.
        A name not in :data:`PREFETCH_COLUMNS` is silently skipped.
    principal:
        See the module docstring's "ACL" section.

    Examples
    --------
    >>> clear_vocabulary_cache()
    >>> import pandas as pd
    >>> def fake_execute(sql, params):
    ...     return pd.DataFrame({"Name": ["تالار محصولات صنعتی", "تالار پتروشیمی"]})
    >>> _ = refresh_vocabulary("Ring", "Name", execute_fn=fake_execute)
    >>> result = match_question_against_vocabulary(
    ...     normalize_for_matching("گرانترین معامله در تالار محصولات صنعتی"),
    ...     ["Ring"],
    ... )
    >>> result.filters
    {'Ring': 'تالار محصولات صنعتی'}
    >>> clear_vocabulary_cache()
    """
    denied = {c.lower() for c in principal.denied_columns}
    filters: dict[str, str] = {}
    clarifications: list[Clarification] = []
    resolved_columns: list[str] = []

    for table in dict.fromkeys(candidate_tables):  # order-preserving dedup
        columns = PREFETCH_COLUMNS.get(table)
        if columns is None:
            continue

        # (normalised_value, raw_value) pool merged across every allowed,
        # cached (fresh or stale) column for this table.
        pool: list[tuple[str, str]] = []
        for column in columns:
            if column.lower() in denied:
                continue
            values, fresh = _cache.get_with_state(table, column)
            if values is None:
                logger.debug(
                    "dimension_vocabulary: %s.%s never cached -- no "
                    "candidates this request, background refresh triggered",
                    table, column,
                )
                _trigger_background_refresh(table, column)
                continue
            if not fresh:
                logger.debug(
                    "dimension_vocabulary: %s.%s cache stale -- serving "
                    "%d previous value(s) while a background refresh runs",
                    table, column, len(values),
                )
                _trigger_background_refresh(table, column)

            resolved_columns.append(f"{table}.{column}")
            for raw in values:
                normalized = normalize_for_matching(raw)
                if len(normalized) >= MIN_MATCH_LENGTH:
                    pool.append((normalized, raw))

        matches = [(norm, raw) for norm, raw in pool if norm in question_normalized]
        if not matches:
            continue

        max_len = max(len(norm) for norm, _raw in matches)
        # Longest match wins: only values tied at the longest length found
        # are candidates; a shorter value that also happens to be a
        # substring of the question loses outright, not just the tie-break.
        winners: dict[str, None] = {}
        for norm, raw in matches:
            if len(norm) == max_len:
                winners.setdefault(raw, None)

        if len(winners) == 1:
            (value,) = winners
            filters[table] = value
        else:
            clarifications.append(
                Clarification(
                    field=table,
                    prompt=f"کدام مورد برای «{table}» مدنظر است؟",
                    options=sorted(winners),
                )
            )

    return VocabularyMatchResult(
        filters=filters,
        clarifications=clarifications,
        resolved_columns=tuple(resolved_columns),
    )
