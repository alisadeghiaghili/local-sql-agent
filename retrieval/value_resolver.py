# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Deterministic entity-value resolution against the warehouse -- Phase 5b.

The problem this closes: ``retrieval/value_retriever.py`` resolves values
from a **static** alias table (``project_config/aliases.yaml``). Anything
not in that file, the model has to guess -- asked about a customer or
symbol name it has never seen, it must invent the exact string stored in
the warehouse. :func:`resolve_value` removes that guess for whichever
name-bearing dimension columns this deployment's ``schema.yaml`` flags
``resolvable_columns`` for (see :data:`RESOLVABLE_COLUMNS`) by looking the
mention up in the database itself, deterministically, **before** the SQL-generation
prompt is built -- not via an LLM tool call (a tool call is non-deterministic:
the model might not make it, which just moves the guess one layer back
rather than removing it). :data:`RESOLVE_VALUE_TOOL_DEFINITION` is exported
alongside the plain function for Phase 7's bounded-escalation tool-calling
loop to pick up later; nothing here wires it into an agentic loop yet.

DORMANT as of the Phase 5b redesign -- not called from ``ContextRetriever``
-----------------------------------------------------------------------------
This module's ``mention``-based ``LIKE`` search was originally wired into
``retrieval.context_retriever.ContextRetriever.retrieve``, passing the
*whole normalised question* as ``mention`` because this codebase has no
free-text span extractor (something that pulls "فولاد مبارکه" out of a full
sentence). Measured in production shape, that wiring built 1 query per
allowlisted column per request (5 for a `Customer`-only question), issued a
leading-wildcard ``LIKE`` (unindexable -- a full scan) against
potentially-large tables, and the whole-sentence mention could never
actually match a short dimension value -- so every one of those round trips
was a guaranteed miss, paid for on every request. Correctness-safe,
cost-unsafe.

That call site has been **removed**. For the small-cardinality dimensions
(``Ring``, ``Currency``, ``Broker``, ``DeliveryPlace``, ``Symbol``),
``ContextRetriever`` now uses
:mod:`retrieval.dimension_vocabulary` instead -- the inverted design: fetch
each dimension's small, distinct value set *once* (cached, refreshed out of
band, never per-request), and search the *question* for one of *those*
values, the same direction ``retrieval.value_retriever.ValueRetriever.extract_ring``
already searches a static alias list. That dissolves the extraction problem
for anything whose vocabulary is small enough to hold in memory.

``Customer`` and ``Supplier`` are excluded from that prefetch path --
their cardinality can be very large, so a full-vocabulary prefetch is not
sane, and they are exactly the two dimensions that genuinely need a real
mention extractor. This module -- the ``execute_fn``-injected, allowlisted,
parameterised ``LIKE`` search below -- is kept for exactly that future use
(the Phase 7 tool-call seam, or a real extractor once one exists), fully
tested, but **no code path in this codebase currently calls
:func:`resolve_value`**. Wire it up again only once something upstream can
hand it a real short mention instead of a whole question.

Precedence with the static alias path (for whenever this is wired up again)
-------------------------------------------------------------------------------
``retrieval.value_retriever.ValueRetriever`` (ring aliases, Persian
dates/years/months/seasons/weekdays) is faster and needs no database round
trip, so it always wins when it matches. This module was designed to be
consulted only as the **fallback** for what that static path does not
cover, and for what :mod:`retrieval.dimension_vocabulary`'s small-dimension
prefetch does not cover either (i.e. ``Customer``/``Supplier``) -- see the
"DORMANT" section above for why nothing calls it that way today.

The security constraint that dominates this module
-----------------------------------------------------
User-supplied text becomes part of a ``WHERE`` clause here -- the first
place in this system that happens. **The SQL is never built from that
text.** :func:`resolve_value` and its helpers only ever build a query from
a *fixed template* (:func:`_build_query`) whose ``<schema>``, ``<table>``,
and ``<column>`` come from :data:`RESOLVABLE_COLUMNS` / :data:`_TABLE_SCHEMAS`
below -- read once from ``schema.yaml`` at import time, a **closed set fixed
for the life of the process** -- never from the question, never from the
model, and never re-derived per request. The only thing that varies with
user input is a **bound parameter** value, passed through
:func:`database.executor.execute_sql_params` (never interpolated). See
``tests/test_value_resolver.py::TestResolveValueInjection`` for the
byte-identical-SQL-text proof this design exists to satisfy.

If a requested target (table or column) is not in the allowlist, or is in
the calling :class:`~security.auth.Principal`'s ``denied_columns``,
resolution is refused for that column *before* any SQL is built at all --
:func:`resolve_value` never calls ``execute_fn`` for a refused pair. A
principal's ACL is enforced the same explicit-parameter-threading way
Phase 8 wired ``denied_columns`` to the guard: no module globals, no
context vars for security state.

Three outcomes (``ValueResolution.status``)
---------------------------------------------
* ``"matched"``    -- exactly one value found; it reaches
  ``RetrievalContext.filters`` under the matched table's name, extending
  the same plain ``dict`` shape ``PromptBuilder`` already reads. Nothing
  downstream needs to learn a new shape.
* ``"ambiguous"``  -- more than one value found. No value is silently
  picked. The candidates surface as a
  :class:`~session.models.Clarification` -- the v2 API contract's own
  existing machinery for declaring ambiguity (``docs/api-contract-v2.md``
  §5), reused here rather than inventing a parallel mechanism.
* ``"no_match"``   -- nothing found, or resolution was refused/erred/timed
  out. The pipeline behaves exactly as it did before this phase: the model
  keeps guessing. **This function never raises** for any of these
  cases -- a resolver that fails a question the system used to answer
  would be a regression. ``miss_reason`` records *why*, for Phase 6's
  later decision about which intents deserve a deterministic template.

What is deliberately NOT covered
-----------------------------------
``Date.Persian*Name`` columns are not in the allowlist. They are a closed,
fixed vocabulary that ``ValueRetriever`` already resolves correctly with
no database round trip; adding them here would be a latency regression for
no accuracy gain.

Latency
-------
* **Cache** -- resolutions are cached (TTL, see
  :data:`~config.Settings.resolve_value_cache_ttl_seconds`) keyed on
  ``(mention, table, column, scope_key)``.  The scope key is
  :func:`security.auth.scope_key` -- the exact same partition key
  ``api.query_cache.QueryCache`` uses -- so two principals with different
  column visibility can never share a cached resolution.
* **Row cap** -- every query caps its own result with
  ``TOP (?)`` bound to ``cfg.settings.default_top_n``, the same "how many
  rows do we ever want back" setting :func:`~security.sql_guard.ensure_top`
  already uses elsewhere, rather than a new magic number.
* **Timeout** -- the whole resolution step for one (table, column) pair
  runs under :data:`~config.Settings.resolve_value_timeout_seconds`. A
  breach, or *any* exception ``execute_fn`` raises, is treated as that
  pair's miss -- it is folded into the overall ``"no_match"`` outcome
  rather than propagated. See the module docstring's note on
  :func:`_run_with_timeout` for exactly what "timeout" can and cannot
  preempt in pure Python.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

import pandas as pd

import config as cfg
from schema_data.registry import get_resolvable_columns, get_table_schema_qualifiers
from security.auth import ANONYMOUS, Principal, scope_key
from security.dialects import get_dialect_profile
from security.sql_guard import transpile_sql
from session.models import Clarification

logger = logging.getLogger(__name__)

#: Signature every ``execute_fn`` passed to :func:`resolve_value` must
#: satisfy -- ``(sql, params) -> DataFrame``, mirroring
#: ``database.executor.execute_sql_params``. Injectable so this module is
#: fully testable with no live database, the same discipline
#: ``llm.sql_agent.SQLAgent(execute_fn=...)`` already uses throughout this
#: suite.
ExecuteParamsFn = Callable[[str, Sequence[object]], "pd.DataFrame"]

# ---------------------------------------------------------------------------
# The allowlist -- loaded from <PROJECT_CONFIG_DIR>/schema.yaml (see
# schema_data.registry), never hardcoded in source and never derived from
# schema_data.columns.TABLE_COLUMNS. TABLE_COLUMNS lists every column a
# *generated SQL query* may reference (TotalPrice, NationalID, ...); this
# allowlist is deliberately much narrower -- only the name-bearing
# dimension columns a free-text mention could plausibly resolve to, i.e.
# whichever ones this deployment's schema.yaml flags `resolvable_columns`
# for. Read once at import time (mirroring security.sql_guard's own
# eager, schema.yaml-derived TABLE_COLUMNS/_TABLE_LOOKUP) -- a schema.yaml
# edit takes effect on process restart, exactly like the guard's allowlist.
# ---------------------------------------------------------------------------

#: table -> allowlisted column names, from schema.yaml's per-table
#: `resolvable_columns` field -- see schema_data.registry.get_resolvable_columns.
#: ``Date.Persian*Name`` is deliberately excluded from every deployment's
#: schema.yaml so far -- see the module docstring's "What is deliberately
#: NOT covered" section.
RESOLVABLE_COLUMNS: dict[str, tuple[str, ...]] = get_resolvable_columns()

#: table -> its schema/db qualifier (e.g. "Auction_Dim"), same source --
#: schema_data.registry.get_table_schema_qualifiers. Per-table rather than
#: one shared constant because a warehouse routinely spans more than one
#: schema; every table named in RESOLVABLE_COLUMNS is guaranteed an entry
#: here (schema_data.registry.SchemaConfig's validator enforces that a
#: table cannot declare resolvable_columns without also giving a
#: db_schema).
_TABLE_SCHEMAS: dict[str, str] = get_table_schema_qualifiers()

#: Phase 7 seam: a tool-call-shaped description of :func:`resolve_value`,
#: exported for a future bounded agentic loop to register -- nothing in
#: this module wires it into one. See the module docstring.
RESOLVE_VALUE_TOOL_DEFINITION: dict[str, Any] = {
    "name": "resolve_value",
    "description": (
        "Resolve a user-mentioned entity name (a customer, broker, "
        "currency, delivery place, ring, supplier, or symbol name) to its "
        "exact, canonical string as stored in the warehouse, instead of "
        "guessing the spelling. Returns exactly one canonical value, a "
        "list of ambiguous candidates, or nothing if no match was found "
        "-- never invents a value."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mention": {
                "type": "string",
                "description": "The entity name as it appears in the user's question.",
            },
            "candidate_tables": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(RESOLVABLE_COLUMNS)},
                "description": (
                    "Dimension tables the mention plausibly refers to -- "
                    "bounds the search instead of scanning every table on "
                    "every request."
                ),
            },
        },
        "required": ["mention", "candidate_tables"],
    },
}


#: T-SQL ``LIKE`` treats ``%``, ``_``, and ``[`` as wildcards regardless of
#: where they came from -- a *bound* value is not vulnerable to injection
#: (the SQL text never changes), but an un-escaped mention still produces
#: the WRONG match: a literal mention of ``"50%"`` would match any row
#: whose value starts with ``"50"``, and a bare ``"%"`` mention matches
#: every row in the table up to the TOP cap. :data:`_build_query` pairs an
#: explicit ``ESCAPE`` clause with :func:`_escape_like_wildcards` below so
#: these three characters are always matched literally.
_LIKE_ESCAPE_CHAR = "\\"
_LIKE_WILDCARDS_RE = re.compile(r"([%_\[])")


def _escape_like_wildcards(value: str) -> str:
    """Escape T-SQL ``LIKE`` wildcard characters so *value* matches literally.

    Only ``%``, ``_``, and ``[`` are special to T-SQL ``LIKE`` (``]`` is
    literal outside a ``[...]`` bracket expression and needs no escaping).
    Each is prefixed with :data:`_LIKE_ESCAPE_CHAR`, paired with the
    ``ESCAPE '\\'`` clause :func:`_build_query` always appends.

    Uses a replacement *function*, not a replacement string, so this never
    depends on ``re.sub``'s own backslash/group-reference escaping rules --
    a source of exactly the kind of subtle, easy-to-get-backwards bug this
    module exists to avoid at the SQL layer. The lambda below simply
    prepends the escape character to whatever single wildcard character
    matched, with no string-escaping puzzle involved.

    Examples
    --------
    >>> _escape_like_wildcards("50%")
    '50\\\\%'
    >>> _escape_like_wildcards("under_score")
    'under\\\\_score'
    >>> _escape_like_wildcards("[a-z]")
    '\\\\[a-z]'
    >>> _escape_like_wildcards("plain text")
    'plain text'
    """
    return _LIKE_WILDCARDS_RE.sub(lambda m: _LIKE_ESCAPE_CHAR + m.group(1), value)


def _build_query(table: str, column: str, dialect: str = "tsql") -> str:
    """The one, fixed SQL template every resolution query uses.

    ``table`` and ``column`` are f-string-interpolated here, but both are
    always drawn from :data:`RESOLVABLE_COLUMNS` -- never from *mention* or
    any other user-controlled value -- so this is not the injection surface
    the module docstring warns about; the mention travels only as a bound
    ``?``/``%s`` parameter, never through this function at all. The schema
    qualifier likewise comes only from :data:`_TABLE_SCHEMAS` (itself
    derived from ``schema.yaml``, never from *mention*). The trailing
    ``ESCAPE '\\'`` clause is part of this fixed template too -- it is
    always present, regardless of whether the mention actually contains a
    wildcard character -- see :func:`_escape_like_wildcards`.

    Multi-dialect
    -----------------
    This template is always *authored* in tsql (``TOP (?)``, bracketed
    identifiers) and transpiled to *dialect* with
    :func:`~security.sql_guard.transpile_sql` when *dialect* is anything
    else -- the same "generate tsql, transpile" architecture the
    LLM-generated SQL pipeline uses (see
    :func:`~security.sql_guard.transpile_and_revalidate`'s docstring), not
    a second hand-written per-dialect query string. This is also where
    :attr:`~security.dialects.DialectProfile.schema_qualification` is
    consulted: a dialect with no schema concept at all
    (``"none"`` -- SQLite) gets the *unqualified* table reference
    (``[Customer]``, never ``[Auction_Dim].[Customer]``) built into the
    tsql text **before** transpilation, not stripped out after -- SQLite
    would otherwise interpret the schema qualifier as an ``ATTACH``ed
    database name that does not exist, and fail at execution, exactly the
    class of "looked right, failed at execution" gap this phase's
    verification exists to catch (see the module docstring). For
    ``schema_qualification in ("schema", "database")`` (tsql, PostgreSQL,
    MySQL), the qualifier is included exactly as before.

    Examples
    --------
    The exact schema qualifier depends on ``schema.yaml`` (see
    :data:`_TABLE_SCHEMAS`) -- not asserted literally here so this doctest
    passes under any deployment's config, including CI's
    ``project_config.example/``:

    >>> sql = _build_query("Customer", "Name")
    >>> sql.startswith("SELECT DISTINCT TOP (?) [Name] FROM [")
    True
    >>> sql.endswith("].[Customer] WHERE [Name] LIKE ? ESCAPE '\\\\'")
    True

    A schema-less dialect gets no schema qualifier at all:

    >>> sql = _build_query("Customer", "Name", dialect="sqlite")
    >>> "Customer" in sql and "." not in sql.split("FROM")[1].split("WHERE")[0]
    True
    """
    profile = get_dialect_profile(dialect)
    if profile.schema_qualification == "none":
        table_ref = f"[{table}]"
    else:
        schema = _TABLE_SCHEMAS[table]
        table_ref = f"[{schema}].[{table}]"
    tsql = (
        f"SELECT DISTINCT TOP (?) [{column}] FROM {table_ref} "
        f"WHERE [{column}] LIKE ? ESCAPE '{_LIKE_ESCAPE_CHAR}'"
    )
    if dialect == "tsql":
        return tsql
    return transpile_sql(tsql, source_dialect="tsql", target_dialect=dialect)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

ResolutionStatus = Literal["matched", "ambiguous", "no_match"]


@dataclass(frozen=True)
class ValueResolution:
    """The outcome of one :func:`resolve_value` call.

    Parameters
    ----------
    status:
        One of ``"matched"``, ``"ambiguous"``, ``"no_match"`` -- see the
        module docstring's "Three outcomes" section.
    filters:
        Populated only when ``status == "matched"`` -- ``{table_name:
        canonical_value}``, ready to merge into
        ``RetrievalContext.filters`` unchanged.
    clarification:
        Populated only when ``status == "ambiguous"`` -- a
        :class:`~session.models.Clarification` naming every candidate as an
        ``options`` entry. Never partially resolved: no candidate is ever
        also written into ``filters``.
    miss_reason:
        Populated only when ``status == "no_match"``, one of
        ``"not_in_allowlist"``, ``"denied_by_acl"``, ``"no_rows"``,
        ``"timeout"``, or ``"error"`` -- recorded so the misses can be read
        later (Phase 6's input for which intents deserve a deterministic
        template). Never derived from, or containing, any row value.
    resolved_columns:
        ``"Table.Column"`` strings for every allowlisted, ACL-permitted
        pair actually queried (cache hit or miss alike) -- column
        *identifiers* only, exactly the same "names, never values"
        discipline ``observability.audit.AuditRecord.columns`` already
        enforces structurally.
    """

    status: ResolutionStatus
    filters: dict[str, str] = field(default_factory=dict)
    clarification: Clarification | None = None
    miss_reason: str | None = None
    resolved_columns: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Cache -- TTL, keyed on (mention, table, column, scope_key). Mirrors
# api.query_cache.QueryCache's LRU+TTL discipline at a much smaller scale
# (a handful of dimension lookups, not full query responses).
# ---------------------------------------------------------------------------


class _ResolveCache:
    def __init__(self) -> None:
        self._store: OrderedDict[tuple[str, str, str, str], tuple[list[str], float]] = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str, str, str]) -> list[str] | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            values, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return list(values)

    def set(self, key: tuple[str, str, str, str], values: list[str]) -> None:
        ttl = cfg.settings.resolve_value_cache_ttl_seconds
        if ttl <= 0:
            return
        max_size = cfg.settings.resolve_value_cache_max_size
        expires_at = time.monotonic() + ttl
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            else:
                while len(self._store) >= max_size:
                    self._store.popitem(last=False)
            self._store[key] = (list(values), expires_at)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_cache = _ResolveCache()


def clear_resolution_cache() -> None:
    """Flush the module-level resolution cache. Test-only escape hatch.

    Mirrors ``api.query_cache.QueryCache.clear`` -- exposed at module scope
    (rather than requiring callers to reach into a private instance)
    because tests need a clean cache between cases exactly the way
    ``query_cache`` tests do.
    """
    _cache.clear()


# ---------------------------------------------------------------------------
# Timeout wrapper
# ---------------------------------------------------------------------------

#: Signalled whenever a worker finishes and frees a slot; guards
#: :data:`_in_flight_resolutions`. A ``Condition`` rather than a
#: ``Semaphore`` because the bound
#: (``cfg.settings.resolve_value_max_concurrency``) is read at call time,
#: and a semaphore fixes its count at construction -- which would pin the
#: bound to whatever the setting happened to be at import and ignore
#: ``override_settings`` entirely.
_resolution_slots = threading.Condition()

#: Workers started and not yet finished. Incremented by the caller before
#: a worker starts, decremented by that worker itself -- so a worker the
#: caller has already abandoned still returns its slot when it finishes.
_in_flight_resolutions = 0


class _ResolveTimeout(Exception):
    """Internal signal: one (table, column) query exceeded its deadline."""


def _run_under_deadline(
    execute_fn: ExecuteParamsFn,
    sql: str,
    params: tuple[object, ...],
    *,
    timeout: float,
) -> pd.DataFrame | None:
    """Run one resolution query in a daemon thread under a **soft** deadline.

    "Soft" because Python has no portable, safe way to forcibly abort a
    running thread. A breach here means :func:`resolve_value` STOPS
    WAITING and reports a miss, not that the underlying call is
    cancelled: it keeps running and its result is discarded. That is
    still a real timeout from the caller's point of view, and the query
    itself remains bounded by ``database.executor``'s own driver-level
    ``query_timeout_seconds`` -- this is an *additional*, tighter,
    resolution-specific bound on top of that, not a replacement.

    Why a daemon thread rather than a ``ThreadPoolExecutor``
    --------------------------------------------------------
    This module used to hold a module-level
    ``ThreadPoolExecutor(max_workers=8)`` and call
    ``future.result(timeout=...)`` on it. That combination is unsafe
    precisely *because* the deadline is soft:

    A pool worker's threads are **not** daemons, and
    ``concurrent.futures`` registers an ``atexit`` hook that joins every
    one of them before the interpreter may exit. So an abandoned
    query -- which this function creates by design, every time a deadline
    is breached -- was guaranteed to be joined during interpreter
    finalisation, running arbitrary caller-supplied ``execute_fn`` code
    (in this suite, a test-local closure or a mock) at a point where the
    module globals and objects it touches are already being torn down.

    The observed symptom was a segmentation fault (exit code 139) *after*
    the test run had already reported every test passing, on Python 3.12,
    intermittently -- the worst diagnostic shape available: no failing
    test, no traceback, and a green summary immediately above it.
    ``tests/test_value_resolver.py``'s timeout test had already narrowed
    the orphaned sleep from seconds to 0.2s to shrink that window, which
    made the race rarer without removing it.

    A daemon thread cannot do this. If the interpreter wants to exit, an
    abandoned worker is simply cut off, never granted a window to run
    unsupervised against a half-finalised process. This is the same
    reasoning, and the same conclusion, as
    ``retrieval.dimension_vocabulary``'s background refresh -- whose own
    comment argues against exactly the pool this replaces.

    The concurrency bound the pool used to provide as ``max_workers`` is
    kept by :data:`_in_flight_resolutions`, read from
    ``cfg.settings.resolve_value_max_concurrency`` at call time. Waiting
    for a free slot spends the caller's deadline, because a deadline is a
    deadline: a saturated resolver must report a miss on time, not queue
    past it.

    Raises
    ------
    _ResolveTimeout
        No slot became free, or the query did not finish, within
        *timeout* seconds in total.
    Exception
        Whatever *execute_fn* raised, re-raised in the calling thread.
    """
    global _in_flight_resolutions

    started = time.monotonic()
    limit = max(cfg.settings.resolve_value_max_concurrency, 1)
    with _resolution_slots:
        if not _resolution_slots.wait_for(
            lambda: _in_flight_resolutions < limit, timeout=max(timeout, 0.0),
        ):
            raise _ResolveTimeout(
                f"resolve_value: no free resolution slot within {timeout}s "
                f"({limit} queries already in flight)"
            )
        _in_flight_resolutions += 1

    done = threading.Event()
    outcome: dict[str, Any] = {}

    def _target() -> None:
        global _in_flight_resolutions
        try:
            outcome["value"] = execute_fn(sql, params)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            outcome["error"] = exc
        finally:
            done.set()
            with _resolution_slots:
                _in_flight_resolutions -= 1
                _resolution_slots.notify()

    threading.Thread(target=_target, name="resolve-value", daemon=True).start()

    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0 or not done.wait(remaining):
        raise _ResolveTimeout(f"resolve_value: query exceeded {timeout}s")

    error = outcome.get("error")
    if error is not None:
        raise error
    return outcome.get("value")


def _query_one(
    table: str, column: str, mention: str, execute_fn: ExecuteParamsFn,
) -> list[str]:
    """Run one allowlisted (table, column) resolution query and return its values.

    Raises
    ------
    _ResolveTimeout
        If the query does not complete within
        ``cfg.settings.resolve_value_timeout_seconds``.
    Exception
        Whatever *execute_fn* itself raises (e.g. the ``RuntimeError``
        ``database.executor.execute_sql_params`` wraps a driver failure in)
        -- propagated to the caller, which treats any exception here as
        that pair's miss. Never raised past :func:`resolve_value` itself.
    """
    sql = _build_query(table, column, dialect=cfg.settings.sql_dialect)
    # _escape_like_wildcards + _build_query's ESCAPE clause together make a
    # literal "%", "_", or "[" in *mention* match itself, not act as a
    # wildcard -- a mention of "50%" must never match every row starting
    # with "50", and a bare "%" must never match the whole table.
    like_value = f"%{_escape_like_wildcards(mention)}%"
    params: tuple[object, ...] = (cfg.settings.default_top_n, like_value)

    try:
        frame = _run_under_deadline(
            execute_fn, sql, params,
            timeout=cfg.settings.resolve_value_timeout_seconds,
        )
    except _ResolveTimeout as exc:
        raise _ResolveTimeout(
            f"resolve_value: {table}.{column} exceeded "
            f"{cfg.settings.resolve_value_timeout_seconds}s"
        ) from exc

    if frame is None or frame.empty:
        return []
    return [str(v) for v in frame.iloc[:, 0].tolist()]


def _default_execute_fn(sql: str, params: Sequence[object]) -> pd.DataFrame:
    """The production ``execute_fn`` -- ``database.executor``'s parameterised
    entry point. Imported lazily so importing this module never pulls in
    the database/SQLAlchemy dependency chain for callers (e.g. most of this
    test suite) that always inject their own ``execute_fn``.
    """
    from database.executor import execute_sql_params

    return execute_sql_params(sql, params)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_value(
    mention: str,
    candidate_tables: Sequence[str],
    *,
    principal: Principal = ANONYMOUS,
    execute_fn: ExecuteParamsFn | None = None,
) -> ValueResolution:
    """Deterministically resolve *mention* against the warehouse.

    See the module docstring for the full design rationale (why this is a
    plain function rather than an LLM tool call, the injection-safety
    guarantee, the three outcomes, and the ACL/allowlist refusal
    semantics). This docstring covers only the call contract.

    Parameters
    ----------
    mention:
        The entity name as mentioned in the question. Expected to already
        be normalised through :func:`core.persian.normalize_for_matching`
        by the caller -- this function does not normalise it again.
    candidate_tables:
        Table names to search -- typically what
        ``retrieval.entity_retriever.EntityRetriever.retrieve`` selected
        for the question, so the search is bounded to entity kinds the
        question plausibly concerns rather than scanning every allowlisted
        table on every request. A name not in
        :data:`RESOLVABLE_COLUMNS` is silently skipped (not an error) --
        e.g. a fact table like ``"Contract"`` passed in alongside
        ``"Customer"`` simply narrows nothing, it doesn't refuse the call.
    principal:
        The caller's identity (Phase 8). Any allowlisted column in
        ``principal.denied_columns`` is refused for *this call* before any
        SQL is built for it. Defaults to
        :data:`~security.auth.ANONYMOUS` (no restriction) for callers with
        no authenticated principal (the CLI/REPL path).
    execute_fn:
        ``(sql, params) -> DataFrame``. Defaults to
        :func:`database.executor.execute_sql_params` when omitted. Inject a
        fake here to test with no live database.

    Returns
    -------
    ValueResolution
        Never raises for a database failure, a timeout, an ACL refusal, or
        an allowlist miss -- all of these collapse into
        ``status="no_match"`` with a diagnostic ``miss_reason``, exactly
        matching this system's pre-Phase-5b behaviour of leaving the model
        to guess. Only a programming error inside *execute_fn* that is not
        an ``Exception`` (e.g. ``KeyboardInterrupt``) would propagate.

    Examples
    --------
    A single match reaches ``filters`` under the matched table's name:

    >>> import pandas as pd
    >>> def fake_execute(sql, params):
    ...     return pd.DataFrame({"Name": ["شرکت فولاد مبارکه اصفهان"]})
    >>> result = resolve_value("فولاد مبارکه", ["Customer"], execute_fn=fake_execute)
    >>> result.status
    'matched'
    >>> result.filters
    {'Customer': 'شرکت فولاد مبارکه اصفهان'}

    A table outside the allowlist never reaches *execute_fn* at all:

    >>> calls = []
    >>> def counting_execute(sql, params):
    ...     calls.append(sql)
    ...     return pd.DataFrame({"Name": []})
    >>> result = resolve_value("x", ["HR_Payroll"], execute_fn=counting_execute)
    >>> result.status, result.miss_reason, calls
    ('no_match', 'not_in_allowlist', [])
    """
    if execute_fn is None:
        execute_fn = _default_execute_fn

    denied = {c.lower() for c in principal.denied_columns}
    scope = scope_key(principal)

    any_allowlisted = False
    pairs: list[tuple[str, str]] = []
    for table in dict.fromkeys(candidate_tables):  # order-preserving dedup
        columns = RESOLVABLE_COLUMNS.get(table)
        if columns is None:
            continue
        any_allowlisted = True
        for column in columns:
            if column.lower() in denied:
                continue
            pairs.append((table, column))

    if not pairs:
        reason = "denied_by_acl" if any_allowlisted else "not_in_allowlist"
        return ValueResolution(status="no_match", miss_reason=reason)

    collected: dict[str, tuple[str, str]] = {}  # value -> (table, column) first seen
    resolved_columns: list[str] = []
    timed_out = False
    errored = False

    for table, column in pairs:
        resolved_columns.append(f"{table}.{column}")
        cache_key = (mention, table, column, scope)
        values = _cache.get(cache_key)
        if values is None:
            try:
                values = _query_one(table, column, mention, execute_fn)
            except _ResolveTimeout as exc:
                logger.warning(str(exc))
                timed_out = True
                continue
            except Exception as exc:  # noqa: BLE001 - any execute_fn failure is a miss
                logger.warning(
                    "resolve_value: query failed for %s.%s: %s", table, column, exc,
                )
                errored = True
                continue
            _cache.set(cache_key, values)

        for value in values:
            collected.setdefault(value, (table, column))

    if not collected:
        if timed_out:
            reason = "timeout"
        elif errored:
            reason = "error"
        else:
            reason = "no_rows"
        return ValueResolution(
            status="no_match", miss_reason=reason, resolved_columns=tuple(resolved_columns),
        )

    if len(collected) == 1:
        value, (table, _column) = next(iter(collected.items()))
        return ValueResolution(
            status="matched",
            filters={table: value},
            resolved_columns=tuple(resolved_columns),
        )

    tables_touched = {t for t, _c in collected.values()}
    field_name = next(iter(tables_touched)) if len(tables_touched) == 1 else "value"
    clarification = Clarification(
        field=field_name,
        prompt=f"کدام مورد برای «{mention}» مدنظر است؟",
        options=sorted(collected),
    )
    return ValueResolution(
        status="ambiguous",
        clarification=clarification,
        resolved_columns=tuple(resolved_columns),
    )
