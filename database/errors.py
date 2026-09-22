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
  ``https://sqlalche.me/…`` background-info URL — and for a pyodbc/SQL
  Server driver, the driver's own message routinely names the server host
  or instance, the database name, and the login, e.g. ``TCP Provider: No
  such host is known`` (SQLSTATE ``08001``) or ``Login failed for user
  'svc_auction_readonly'`` (SQLSTATE ``28000``).
* Both ``session/engine.py`` (the v2 turn path) and ``api/runner.py`` (the
  v1 ``/query`` path) then put that ``RuntimeError``'s ``str()`` — or a
  fragile substring test over it — straight into what the client reads,
  with every failure (connection lost, wrong login, query genuinely wrong)
  reported identically as ``QUERY_EXECUTION_ERROR``.

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
_GENERIC_STATEMENT_MESSAGE = "The database returned an error for this query."

#: A SQLSTATE is always a 5-character alphanumeric code (ODBC/DBAPI
#: convention) -- e.g. "08001", "28000", "42S22", "HYT00".
_SQLSTATE_RE = re.compile(r"^[0-9A-Za-z]{5}$")

#: Leading bracketed tags SQLAlchemy/pyodbc prepend to the driver's own
#: message -- SQLSTATE, ODBC vendor, driver name/version, backend product,
#: e.g. "[28000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]" --
#: stripped so only the database's own sentence remains.
_LEADING_BRACKET_TAGS_RE = re.compile(r"^(?:\[[^\]]*\]\s*)+")

#: SQLSTATE class "08" is "connection exception" in the ODBC/SQL standard
#: (host unreachable, link failure, connection rejected, ...).
_CONNECTION_SQLSTATE_PREFIXES = ("08",)
#: "28000" is "invalid authorization specification" -- a login failure.
_LOGIN_SQLSTATES = frozenset({"28000"})
#: Query/statement timeout SQLSTATEs.
_TIMEOUT_SQLSTATES = frozenset({"HYT00", "HYT01"})

#: Substring fallbacks for connectivity/availability failures whose
#: SQLSTATE is absent, non-standard, or (like "Cannot open database",
#: SQLSTATE 42000) shared with unrelated statement errors -- matched
#: against the driver's own message text, never against the SQL or
#: parameters (which are not part of that text; see
#: :func:`_sqlstate_and_driver_message`).
_CONNECTION_KEYWORDS = (
    "no such host is known",
    "server is not found or not accessible",
    "communication link failure",
    "could not connect",
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
)
_POOL_KEYWORDS = (
    "queuepool limit",
    "connection timed out",
)
_TIMEOUT_KEYWORDS = (
    "query timeout expired",
    "timeout expired",
    "lock request time out",
)


@dataclass(frozen=True)
class DatabaseErrorClassification:
    """The two facts the client is allowed to learn about a DB failure."""

    code: str  # "DATABASE_UNAVAILABLE" | "QUERY_TIMEOUT" | "QUERY_EXECUTION_ERROR"
    client_message: str


def _strip_driver_preamble(message: str) -> str:
    return _LEADING_BRACKET_TAGS_RE.sub("", message).strip()


def _sqlstate_and_driver_message(exc: SQLAlchemyError) -> tuple[str | None, str]:
    """Best-effort ``(sqlstate, driver_message)`` from *exc*'s ``.orig``.

    ``.orig`` (set by every :class:`~sqlalchemy.exc.StatementError`
    subclass -- ``OperationalError``, ``ProgrammingError``, ``DataError``,
    …) is the original DBAPI exception, e.g. pyodbc's, whose ``args`` is
    conventionally ``(sqlstate, message)``. Reading it directly -- instead
    of regex-parsing ``str(exc)`` -- is what keeps this function from ever
    seeing the ``[SQL: …]``/``[parameters: …]``/``sqlalche.me`` text
    SQLAlchemy's own ``__str__`` appends: none of that lives on ``.orig``.
    """
    orig = getattr(exc, "orig", None)
    if orig is not None:
        args = getattr(orig, "args", None)
        if args and len(args) >= 2 and isinstance(args[0], str) and _SQLSTATE_RE.match(args[0]):
            return args[0].upper(), str(args[-1])
        if orig:
            return None, str(orig)
    return None, str(exc)


def _classify_sqlalchemy_error(exc: SQLAlchemyError) -> DatabaseErrorClassification:
    # sqlalchemy.exc.TimeoutError: a connection-*pool* checkout timeout
    # (QueuePool exhausted). It has no `.orig` -- it never reached a
    # driver -- so it must be caught before `_sqlstate_and_driver_message`
    # (which would otherwise fall through to `str(exc)` and match nothing).
    # Pool exhaustion is an availability problem, not a query timeout.
    if type(exc).__name__ == "TimeoutError" and getattr(exc, "orig", None) is None:
        return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)

    sqlstate, driver_message = _sqlstate_and_driver_message(exc)
    lowered = driver_message.lower()

    if sqlstate is not None:
        if sqlstate.startswith(_CONNECTION_SQLSTATE_PREFIXES) or sqlstate in _LOGIN_SQLSTATES:
            return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)
        if sqlstate in _TIMEOUT_SQLSTATES:
            return DatabaseErrorClassification("QUERY_TIMEOUT", QUERY_TIMEOUT_MESSAGE)

    if any(kw in lowered for kw in _CONNECTION_KEYWORDS) or any(kw in lowered for kw in _POOL_KEYWORDS):
        return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)
    if any(kw in lowered for kw in _TIMEOUT_KEYWORDS):
        return DatabaseErrorClassification("QUERY_TIMEOUT", QUERY_TIMEOUT_MESSAGE)

    # Statement error: the query itself is what is wrong. The client gets
    # the database's own sentence -- stripped of the SQLSTATE/vendor/driver
    # preamble -- never the SQL text, the bound parameter values, or the
    # sqlalche.me URL (none of those are part of `driver_message` in the
    # first place -- see `_sqlstate_and_driver_message`'s docstring).
    stripped = _strip_driver_preamble(driver_message)
    return DatabaseErrorClassification("QUERY_EXECUTION_ERROR", stripped or _GENERIC_STATEMENT_MESSAGE)


def _classify_by_text(message: str) -> DatabaseErrorClassification:
    """Fallback for an exception with no :class:`SQLAlchemyError` anywhere
    on it (bare ``RuntimeError``, hand-built by a caller/test rather than
    raised by ``database.executor``). Kept deliberately narrow -- real
    production failures always carry the original ``SQLAlchemyError`` as
    ``__cause__`` (``database.executor._execute`` sets it via ``raise ...
    from exc``) and are classified precisely by
    :func:`_classify_sqlalchemy_error` instead.
    """
    lowered = message.lower()
    if "lock_timeout" in lowered or "lock timeout" in lowered or "timeout" in lowered:
        return DatabaseErrorClassification("QUERY_TIMEOUT", QUERY_TIMEOUT_MESSAGE)
    if "cannot connect" in lowered or "connection" in lowered:
        return DatabaseErrorClassification("DATABASE_UNAVAILABLE", DATABASE_UNAVAILABLE_MESSAGE)

    stripped = message
    if stripped.lower().startswith("database error:"):
        stripped = stripped.split(":", 1)[1].strip()
    stripped = _strip_driver_preamble(stripped)
    return DatabaseErrorClassification("QUERY_EXECUTION_ERROR", stripped or _GENERIC_STATEMENT_MESSAGE)


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
        genuine statement error (``QUERY_EXECUTION_ERROR``), which itself
        cannot describe infrastructure it never touched.

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
    """
    if isinstance(exc, SQLAlchemyError):
        return _classify_sqlalchemy_error(exc)
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, SQLAlchemyError):
        return _classify_sqlalchemy_error(cause)
    return _classify_by_text(str(exc))
