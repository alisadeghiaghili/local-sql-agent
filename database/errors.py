# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Classify a database failure and build the message an analyst may see.

Finding 11 (2026 audit) closed the same leak for the LLM endpoint: a raw
transport error reaching the client's screen, fixed by summarising in the
field the client reads and logging the raw text server-side (see
``session/engine.py::_classify_router_failure`` and
``tests/security_audit/test_turn_error_sanitization.py``). A follow-up pass
found the identical shape on the *database* side, one layer lower and in
two places at once:

* ``database/executor.py`` wraps every ``SQLAlchemyError`` as
  ``RuntimeError(f"Database error: {exc}")``.  ``str()`` of a SQLAlchemy
  ``DBAPIError`` includes the driver's own message *plus* the ``[SQL: …]``
  and ``[parameters: …]`` SQLAlchemy appends, plus a
  ``https://sqlalche.me/…`` background-info URL — and the driver's own
  message routinely names the server host or instance, the database name,
  and the login.
* Both ``session/engine.py`` (the v2 turn path) and ``api/runner.py`` (the
  v1 ``/query`` path) then put that ``RuntimeError``'s ``str()`` — or a
  fragile substring test over it — straight into what the client reads.

Round 2 (independent verification of the first fix): the original version
of this module was shaped for T-SQL/pyodbc and, worse, *failed open* — an
error type or shape it did not recognise fell through to
``QUERY_EXECUTION_ERROR`` with the raw driver text attached. A MySQL
"access denied" error, a Postgres connection-refused error, and a
pyodbc-shaped error whose ``.orig.args`` collapsed to one pre-formatted
string all leaked host/login/IP text this way. ``config.Settings.sql_dialect``
supports ``tsql``, ``postgres``, ``mysql`` and ``sqlite`` (see
``security/dialects.py``), so this module now classifies all four, and —
this is the load-bearing change — **fails closed**: a statement error's raw
text is shown to the client only when it is *positively* recognised as one
(by SQLSTATE class, a driver-specific error code, or an explicit
message pattern); everything else, including any exception type or shape
this module does not specifically know about, defaults to
``DATABASE_UNAVAILABLE``. A final safety net additionally re-scans
whatever text *is* about to be shown for an IP address, a port number, a
``user@host``/``'user'@'host'`` fragment, or literal driver/host/login
wording, and downgrades to the generic message if any of it is still
there — so a scrubbing gap degrades to "less specific," never to "leaks."

Round 3 (a second independent pass): round 2 left one path still failing
open on purpose -- ``_classify_by_text``, the fallback for an exception
with no ``SQLAlchemyError`` anywhere on it, documented at the time as
"deliberately narrow" because "real production failures always carry the
original SQLAlchemyError as ``__cause__``." That premise was wrong.
``session/engine.py``'s ``except Exception`` around each ``execute()``
call hands *any* exception to :func:`classify_database_error`, and
``database.executor._execute`` only wraps :class:`SQLAlchemyError` — a raw
``OSError``/socket error (e.g. ``getaddrinfo failed``) from the
driver-level-timeout ``setattr`` on the raw DBAPI connection (outside
SQLAlchemy's own wrapping) reaches the fallback unwrapped, in production.
``_classify_by_text`` now fails closed the same way the SQLAlchemy-shaped
path does: an explicit allowlist of statement-error phrasings this module
already relies on elsewhere is the only text it may show; availability/
timeout wording is classified accordingly; anything else — including a
non-database exception like ``KeyError`` — gets the generic message.

This module is the **one place** that decides, from the exception a query
execution raised: whether the failure is a connectivity/availability
problem the analyst cannot fix by changing their question
(``DATABASE_UNAVAILABLE``), a timeout (``QUERY_TIMEOUT``), or a genuine
statement error worth showing the analyst in their own database's words
(``QUERY_EXECUTION_ERROR``) — and what text is safe to hand back for each.
Both ``session/engine.py`` and ``api/runner.py`` call
:func:`classify_database_error` instead of each keeping (and drifting from)
its own copy of this logic.

The raw exception is never discarded, only kept out of the client-facing
field: ``database.executor._execute`` already logs it in full
(``logger.error("SQL execution failed: %s", exc)``) before wrapping it, and
``exc.__cause__``/``RuntimeError.__cause__`` keeps the original
``SQLAlchemyError`` attached for any caller (this module included) that
needs to re-derive the classification from the real error rather than an
already-summarised message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

#: Fixed, generic client-facing text for each non-statement code. Deliberately
#: name nothing about the deployment: no host, port, instance, database,
#: login, or driver -- see the module docstring and finding 11.
DATABASE_UNAVAILABLE_MESSAGE = "The database is currently unavailable. Please try again shortly."
QUERY_TIMEOUT_MESSAGE = (
    "The query took too long to run and was cancelled. Please try again "
    "or narrow your request."
)
#: What a statement error degrades to when it cannot be positively
#: recognised, or when the safety net still finds something identifying in
#: its scrubbed text -- see the module docstring's "fail closed" note.
_GENERIC_STATEMENT_MESSAGE = "The database rejected the query."


@dataclass(frozen=True)
class DatabaseErrorClassification:
    """The two facts the client is allowed to learn about a DB failure."""

    code: str  # "DATABASE_UNAVAILABLE" | "QUERY_TIMEOUT" | "QUERY_EXECUTION_ERROR"
    client_message: str


# ---------------------------------------------------------------------------
# Extracting a structured signal from the driver exception
# ---------------------------------------------------------------------------

#: A SQLSTATE is always a 5-character alphanumeric code (ODBC/DBAPI
#: convention) -- e.g. "08001", "28000", "42S22", "HYT00", "57014".
_SQLSTATE_RE = re.compile(r"^[0-9A-Za-z]{5}$")

#: Some caller/test-double shapes collapse a real ``(sqlstate, message)``
#: two-tuple into a single pre-formatted string that looks like the tuple's
#: own ``repr()`` -- e.g. ``"('42S22', \"[42S22] ... \")"``. Recognised so
#: it degrades the same way the real two-tuple does, not by leaking the
#: whole repr (quotes, parens, sqlstate and all) as the "message".
_TUPLE_REPR_RE = re.compile(
    r"""^\(\s*'([0-9A-Za-z]{5})'\s*,\s*(?:"([^"]*)"|'([^']*)')\s*\)$""",
    re.DOTALL,
)


@dataclass(frozen=True)
class _DriverSignal:
    #: Upper-cased 5-char SQLSTATE, when one could be identified -- from a
    #: pyodbc-style ``(sqlstate, message)`` tuple, or from a psycopg2/3
    #: ``.pgcode``/``.sqlstate`` attribute. ``None`` if not available.
    sqlstate: str | None
    #: A MySQL numeric error code, from ``orig.args[0]`` when it is an
    #: ``int`` (MySQL drivers do not use SQLSTATE tuples). ``None`` otherwise.
    mysql_errno: int | None
    #: The driver's own message text -- never SQLAlchemy's ``[SQL: …]``/
    #: ``[parameters: …]``/``sqlalche.me`` wrapper, which live only in
    #: ``str(exc)``, not on ``.orig`` or its ``args``.
    message: str


def _driver_signal(exc: SQLAlchemyError) -> _DriverSignal:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return _DriverSignal(None, None, str(exc))

    # psycopg2 (`.pgcode`) / psycopg (3) (`.sqlstate`): the SQLSTATE lives
    # on a dedicated attribute, not in `.args` -- `.args` for these drivers
    # is typically a single already-formatted multi-line string.
    pg_state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if isinstance(pg_state, str) and _SQLSTATE_RE.match(pg_state):
        pg_message = getattr(orig, "pgerror", None) or str(orig)
        return _DriverSignal(pg_state.upper(), None, str(pg_message))

    args = getattr(orig, "args", None)
    if args:
        if len(args) >= 2 and isinstance(args[0], str) and _SQLSTATE_RE.match(args[0]):
            # pyodbc convention: args = (sqlstate, message).
            return _DriverSignal(args[0].upper(), None, str(args[-1]))
        if len(args) >= 1 and isinstance(args[0], int):
            # MySQL driver convention: args = (errno, message).
            message = str(args[-1]) if len(args) >= 2 else str(orig)
            return _DriverSignal(None, args[0], message)
        if len(args) == 1 and isinstance(args[0], str):
            m = _TUPLE_REPR_RE.match(args[0].strip())
            if m:
                inner = m.group(2) if m.group(2) is not None else m.group(3)
                return _DriverSignal(m.group(1).upper(), None, inner)

    return _DriverSignal(None, None, str(orig) if orig else str(exc))


# ---------------------------------------------------------------------------
# Classification tables -- one row per dialect's documented shape
# ---------------------------------------------------------------------------

#: SQLAlchemy exception TYPE NAMES (not dialects): PEP 249 reserves
#: ``OperationalError``/``InterfaceError`` for problems "not necessarily
#: under the control of the programmer" (lost connection, data source not
#: found, transaction could not be processed, ...) -- i.e. availability,
#: not a bad statement. ``DisconnectionError`` is SQLAlchemy's own pool
#: invalidation signal and never describes a statement either.
_AVAILABILITY_TYPE_NAMES = frozenset({"OperationalError", "InterfaceError", "DisconnectionError"})
#: PEP 249 reserves these for a statement-level problem: wrong number of
#: parameters / bad object reference (ProgrammingError), invalid data
#: (DataError), a constraint violation (IntegrityError), an internal
#: database error while running a statement (InternalError), an
#: unsupported API/method (NotSupportedError). The exception TYPE alone is
#: treated as positive recognition for these -- SQLAlchemy assigns it from
#: the DBAPI's own PEP 249 category, not from this module's own guesswork.
_STATEMENT_TYPE_NAMES = frozenset(
    {"ProgrammingError", "DataError", "IntegrityError", "InternalError", "NotSupportedError"}
)

#: ODBC/ANSI SQLSTATE class "08" = connection exception, "28" = invalid
#: authorization specification (login/auth). Shared by tsql (pyodbc) and
#: postgres (psycopg, whose SQLSTATEs follow the same ANSI classes).
_CONNECTION_OR_AUTH_SQLSTATE_CLASSES = frozenset({"08", "28"})
#: Exact timeout SQLSTATEs: tsql's "HYT00"/"HYT01", postgres's "57014"
#: (query_canceled, e.g. statement_timeout).
_TIMEOUT_SQLSTATES = frozenset({"HYT00", "HYT01", "57014"})
#: Postgres admin/crash shutdown and "cannot connect now" ("57P0x"), plus
#: "3D000" invalid catalog name (`database "x" does not exist`) -- all
#: availability, not statement, problems.
_UNAVAILABLE_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "3D000"})
#: ANSI SQLSTATE classes "42" (syntax error / access rule violation), "22"
#: (data exception), "23" (integrity constraint violation) -- a genuine
#: statement problem, shared by tsql and postgres. Checked only AFTER the
#: keyword fallback below, because tsql's "Cannot open database" and
#: "Login failed" both carry class "42" too (SQL Server does not give
#: login/availability failures their own SQLSTATE class) and must not be
#: caught here first.
_STATEMENT_SQLSTATE_CLASSES = frozenset({"42", "22", "23"})

#: MySQL numeric error codes (`orig.args[0]`, no SQLSTATE tuple):
#: connection lost/refused/timed out (2002/2003/2005/2006/2013), access
#: denied (1044/1045), unknown database (1049).
_MYSQL_CONNECTION_ERRNOS = frozenset({2002, 2003, 2005, 2006, 2013, 1044, 1045, 1049})
#: Lock wait timeout (1205), max execution time exceeded (3024).
_MYSQL_TIMEOUT_ERRNOS = frozenset({1205, 3024})

#: SQLite has no SQLSTATE and no numeric code -- every failure arrives as
#: ``sqlite3.OperationalError``, so its own wording is the only signal.
#: Recognised as a genuine statement problem:
_SQLITE_STATEMENT_KEYWORDS = (
    "no such table",
    "no such column",
    "no such function",
    "no such module",
    "no such index",
    "syntax error",
    'near "',
    "ambiguous column name",
    "unrecognized token",
)
#: Recognised as a transient/availability condition, never shown raw:
_SQLITE_UNAVAILABLE_KEYWORDS = (
    "database is locked",
    "database is busy",
    "unable to open database file",
    "disk i/o error",
    "database disk image is malformed",
)

#: Substring fallbacks for connectivity/availability failures whose
#: SQLSTATE/error code is absent, non-standard, or (like tsql's "Cannot
#: open database", SQLSTATE 42000) shared with unrelated statement errors
#: -- matched against the driver's own message text, never against the SQL
#: or parameters (which are not part of that text; see
#: :func:`_driver_signal`'s docstring). Covers tsql/pyodbc and the
#: postgres client-side connect failures that never reach the server (and
#: so never get a SQLSTATE at all, e.g. "connection refused").
_CONNECTION_KEYWORDS = (
    "no such host is known",
    "server is not found or not accessible",
    "communication link failure",
    "could not connect",
    "connection to server",
    "connection refused",
    "connection is broken",
    "network-related",
    "login failed",
    "login timeout expired",
    "cannot open database",
    "can't open lib",
    "data source name not found",
    "unable to connect",
    "server was not found",
    "password authentication failed",
    "server closed the connection",
    "terminating connection",
)
_POOL_KEYWORDS = (
    "queuepool limit",
    "connection timed out",
)
_TIMEOUT_KEYWORDS = (
    "query timeout expired",
    "timeout expired",
    "lock request time out",
    "query canceled",
    "canceling statement due to statement timeout",
)


def _statement_result(driver_message: str) -> DatabaseErrorClassification:
    """The one path that is allowed to show the client database-authored
    text: scrub it, then run it past the safety net before returning it."""
    return _apply_safety_net(_scrub_statement_text(driver_message))


def _classify_sqlalchemy_error(exc: SQLAlchemyError) -> DatabaseErrorClassification:
    type_name = type(exc).__name__

    # sqlalchemy.exc.TimeoutError: a connection-*pool* checkout timeout
    # (QueuePool exhausted). It has no `.orig` -- it never reached a
    # driver -- so it must be caught before `_driver_signal` (which would
    # otherwise fall through to `str(exc)` and match nothing). Pool
    # exhaustion is an availability problem, not a query timeout.
    if type_name == "TimeoutError" and getattr(exc, "orig", None) is None:
        return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)

    signal = _driver_signal(exc)
    lowered = signal.message.lower()

    # --- MySQL numeric codes: unambiguous, driver-specific -- checked first.
    if signal.mysql_errno is not None:
        if signal.mysql_errno in _MYSQL_CONNECTION_ERRNOS:
            return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)
        if signal.mysql_errno in _MYSQL_TIMEOUT_ERRNOS:
            return DatabaseErrorClassification("QUERY_TIMEOUT", QUERY_TIMEOUT_MESSAGE)

    # --- SQLSTATE: exact/class matches that are NEVER a statement problem,
    # checked before the generic 42/22/23 statement classes below (tsql's
    # 42000 "Cannot open database"/"Login failed" both carry class "42").
    if signal.sqlstate is not None:
        state = signal.sqlstate
        state_class = state[:2]
        if state_class in _CONNECTION_OR_AUTH_SQLSTATE_CLASSES:
            return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)
        if state in _TIMEOUT_SQLSTATES:
            return DatabaseErrorClassification("QUERY_TIMEOUT", QUERY_TIMEOUT_MESSAGE)
        if state in _UNAVAILABLE_SQLSTATES:
            return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)

    # --- Text keyword fallback: works across dialects, and is what catches
    # tsql's SQLSTATE-42000-but-really-a-login-failure cases, and postgres
    # client-side connect failures that occur before the server ever
    # assigns a SQLSTATE at all.
    if any(kw in lowered for kw in _CONNECTION_KEYWORDS) or any(kw in lowered for kw in _POOL_KEYWORDS):
        return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)
    if any(kw in lowered for kw in _TIMEOUT_KEYWORDS):
        return DatabaseErrorClassification("QUERY_TIMEOUT", QUERY_TIMEOUT_MESSAGE)

    # --- Positive statement recognition, SQLSTATE-based (tsql/postgres).
    if signal.sqlstate is not None and signal.sqlstate[:2] in _STATEMENT_SQLSTATE_CLASSES:
        return _statement_result(signal.message)

    # --- SQLite: no SQLSTATE, no numeric code -- message text is the only
    # signal, checked both ways so an unrecognised sqlite OperationalError
    # (unknown pragma, extension error, ...) still fails closed below
    # rather than defaulting to a statement error.
    if any(kw in lowered for kw in _SQLITE_UNAVAILABLE_KEYWORDS):
        return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)
    if any(kw in lowered for kw in _SQLITE_STATEMENT_KEYWORDS):
        return _statement_result(signal.message)

    # --- Fail closed. Nothing above positively recognised this as a
    # statement error. An availability-family exception TYPE
    # (OperationalError/InterfaceError/DisconnectionError) that reaches
    # here unrecognised is treated as unavailable -- never shown its raw
    # text on the strength of "we don't know what this is." A
    # statement-family TYPE (ProgrammingError/DataError/...) is trusted on
    # the DBAPI's own PEP 249 categorisation and gets its message scrubbed
    # and safety-netted. Anything else -- a type this module has never
    # seen -- is unavailable too.
    if type_name in _AVAILABILITY_TYPE_NAMES:
        return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)
    if type_name in _STATEMENT_TYPE_NAMES:
        return _statement_result(signal.message)
    return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)


#: Round 3: this fallback is NOT limited to hand-built test doubles --
#: session.engine.TurnEngine's `except Exception` (session/engine.py, the
#: execute() call sites) hands ANY exception to classify_database_error,
#: and database.executor._execute only wraps SQLAlchemyError. A raw
#: OSError/socket error (e.g. "getaddrinfo failed") from the driver-level
#: timeout setattr on the raw DBAPI connection (database/executor.py,
#: around the `setattr(raw_conn, profile.driver_level_timeout_attr, ...)`
#: line -- that call operates outside SQLAlchemy's own wrapping) reaches
#: here unwrapped, in production, not just in a test. So this function
#: fails closed exactly like `_classify_sqlalchemy_error` does: text is
#: shown to the client only when it POSITIVELY matches a known
#: statement-error sentence, never by default.
_FALLBACK_TIMEOUT_KEYWORDS = ("timed out", "timeout")
_FALLBACK_AVAILABILITY_KEYWORDS = (
    "cannot connect", "connection", "unreachable", "refused", "getaddrinfo",
    "network", "could not connect", "connection reset", "connection closed",
    "connection lost", "login", "password",
)
#: An explicit allowlist of statement-error phrasings this module already
#: relies on elsewhere (tsql/mssql, postgres, sqlite) -- the only text this
#: fallback is allowed to show the client, and even then only after
#: scrubbing and the safety net.
_FALLBACK_STATEMENT_ALLOWLIST = (
    "invalid column name",
    "invalid object name",
    "incorrect syntax near",
    "conversion failed",
    "divide by zero",
    "arithmetic overflow",
    "does not exist",  # postgres: relation/column "x" does not exist
    *_SQLITE_STATEMENT_KEYWORDS,
)


def _classify_by_text(message: str) -> DatabaseErrorClassification:
    """Fallback for an exception with no :class:`SQLAlchemyError` anywhere
    on it: a raw ``OSError``/socket error reaching
    ``session.engine.TurnEngine``'s ``except Exception`` unwrapped (see
    the module-level note above), or a hand-built ``RuntimeError`` in a
    test. Real production ``SQLAlchemyError`` failures are classified
    precisely by :func:`_classify_sqlalchemy_error` instead; this function
    fails closed the same way that one does -- an unrecognised shape
    degrades to the generic statement message, never to ``str(exc))``.
    """
    lowered = message.lower()

    if any(kw in lowered for kw in _FALLBACK_TIMEOUT_KEYWORDS):
        return DatabaseErrorClassification("QUERY_TIMEOUT", QUERY_TIMEOUT_MESSAGE)
    if any(kw in lowered for kw in _FALLBACK_AVAILABILITY_KEYWORDS):
        return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)

    if any(kw in lowered for kw in _FALLBACK_STATEMENT_ALLOWLIST):
        stripped = message
        if stripped.lower().startswith("database error:"):
            stripped = stripped.split(":", 1)[1].strip()
        return _statement_result(stripped)

    # Fail closed: nothing positively recognised this as a statement error,
    # and no availability/timeout wording matched either -- e.g. a
    # KeyError/ValueError from unrelated code, or any exception shape this
    # module has never seen. Never fall back to showing `message` itself.
    return DatabaseErrorClassification("QUERY_EXECUTION_ERROR", _GENERIC_STATEMENT_MESSAGE)


def classify_database_error(exc: BaseException) -> DatabaseErrorClassification:
    """Classify a database-execution failure for a client-facing response.

    Parameters
    ----------
    exc:
        Either a :class:`~sqlalchemy.exc.SQLAlchemyError` directly, or any
        exception whose ``__cause__`` is one -- which is exactly the shape
        ``database.executor._execute`` raises
        (``raise RuntimeError(...) from exc``, the SQLAlchemy error
        attached as ``__cause__``). Falls back to a narrow text heuristic
        (:func:`_classify_by_text`) when neither is true.

    Returns
    -------
    DatabaseErrorClassification
        ``code`` is one of ``"DATABASE_UNAVAILABLE"``, ``"QUERY_TIMEOUT"``,
        ``"QUERY_EXECUTION_ERROR"``. ``client_message`` is free of any SQL
        text, bound parameter values, host/instance/port, database name,
        login name, driver name/version, or ``sqlalche.me`` URL -- the only
        things it may echo are the database's own error sentence for a
        *positively recognised* statement error (``QUERY_EXECUTION_ERROR``),
        and even then only after the safety net in
        :func:`_apply_safety_net` finds nothing identifying left in it. An
        error this function does not recognise degrades to
        ``DATABASE_UNAVAILABLE`` with the generic message -- it is never
        shown raw. See the module docstring for the full "fail closed"
        rationale.

    Examples
    --------
    >>> from sqlalchemy.exc import ProgrammingError
    >>> class _Orig(Exception):
    ...     pass
    >>> orig = _Orig()
    >>> orig.args = ("42S22", "[42S22] [Microsoft][ODBC Driver 17 for SQL Server]"
    ...     "[SQL Server]Invalid column name 'Foo'. (207)")
    >>> exc = ProgrammingError("SELECT [Foo] FROM t", (), orig)
    >>> result = classify_database_error(exc)
    >>> result.code
    'QUERY_EXECUTION_ERROR'
    >>> result.client_message
    "Invalid column name 'Foo'. (207)"

    An error this module does not recognise fails closed rather than
    leaking its text:

    >>> class _Mystery(Exception):
    ...     pass
    >>> mystery_orig = _Mystery("something odd at 10.0.0.5")
    >>> mystery_exc = ProgrammingError("SELECT 1", (), mystery_orig)
    >>> mystery_exc.__class__.__name__  # a *statement-family* type...
    'ProgrammingError'
    >>> classify_database_error(mystery_exc).client_message  # ...but the text still trips the safety net
    'The database rejected the query.'
    """
    if isinstance(exc, SQLAlchemyError):
        return _classify_sqlalchemy_error(exc)
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, SQLAlchemyError):
        return _classify_sqlalchemy_error(cause)
    return _classify_by_text(str(exc))


# ---------------------------------------------------------------------------
# Scrubbing a positively-recognised statement error's text
# ---------------------------------------------------------------------------

#: Leading bracketed tags SQLAlchemy/pyodbc prepend to the driver's own
#: message -- SQLSTATE, ODBC vendor, driver name/version, backend product,
#: e.g. "[28000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]" --
#: stripped so only the database's own sentence remains.
_LEADING_BRACKET_TAGS_RE = re.compile(r"^(?:\[[^\]]*\]\s*)+")
#: A trailing ODBC API-call name pyodbc appends, e.g. "(SQLExecDirectW)",
#: "(SQLPrepare)", "(SQLDriverConnect)" -- implementation noise, not part
#: of the database's own sentence.
_TRAILING_ODBC_API_RE = re.compile(r"\s*\((?:SQL[A-Za-z]+)\)\s*$")
#: Postgres prefixes its own message with a severity tag.
_PG_SEVERITY_PREFIX_RE = re.compile(r"^(?:ERROR|FATAL|PANIC|WARNING):\s*", re.IGNORECASE)


def _scrub_statement_text(message: str) -> str:
    """Reduce a positively-recognised statement error's driver text to the
    database's own sentence, whatever shape it arrived in.

    Order matters: postgres's ``LINE n:``/caret/``DETAIL:`` continuation
    lines echo the SQL text itself, so the first-line split happens before
    the (tsql-shaped) bracket/API-name stripping, which only ever matches
    on that first line anyway.
    """
    text = (message or "").strip()
    if not text:
        return ""

    first_line = text.splitlines()[0].strip()
    first_line = _PG_SEVERITY_PREFIX_RE.sub("", first_line)
    first_line = _LEADING_BRACKET_TAGS_RE.sub("", first_line)
    first_line = _TRAILING_ODBC_API_RE.sub("", first_line)
    return first_line.strip()


# ---------------------------------------------------------------------------
# Final safety net -- applied to every piece of text this module is about
# to hand to a client, after scrubbing. A scrubbing gap then degrades to
# the generic message instead of leaking.
# ---------------------------------------------------------------------------

_IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b")
_IPV6_RE = re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{0,4}\b")
_PORT_RE = re.compile(r"\bport\b\s*[:=]?\s*\d{1,5}\b", re.IGNORECASE)
#: `user@host` or MySQL's `'user'@'host'`.
_USER_AT_HOST_RE = re.compile(r"'[^']+'@'[^']+'|\b[A-Za-z0-9_.+-]+@[A-Za-z0-9_.-]+\b")
#: Standalone words/phrases that name infrastructure rather than describe
#: a statement problem -- checked as whole words so they do not fire on an
#: unrelated identifier that merely contains one as a substring (e.g. a
#: column named ``HostName`` does not contain the standalone word "host").
_IDENTIFYING_WORDS_RE = re.compile(
    r"server\s+at|\bhost\b|\bdriver\b|\bmicrosoft\b|\bodbc\b|\blogin\b",
    re.IGNORECASE,
)


def _looks_identifying(text: str) -> bool:
    return bool(
        _IPV4_RE.search(text)
        or _IPV6_RE.search(text)
        or _PORT_RE.search(text)
        or _USER_AT_HOST_RE.search(text)
        or _IDENTIFYING_WORDS_RE.search(text)
    )


def _apply_safety_net(client_message: str) -> DatabaseErrorClassification:
    if not client_message or _looks_identifying(client_message):
        return DatabaseErrorClassification("QUERY_EXECUTION_ERROR", _GENERIC_STATEMENT_MESSAGE)
    return DatabaseErrorClassification("QUERY_EXECUTION_ERROR", client_message)
