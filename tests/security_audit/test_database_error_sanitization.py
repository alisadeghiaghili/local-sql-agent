# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Finding 11, one layer lower -- the *database* error path.

``test_turn_error_sanitization.py`` closed finding 11 for the LLM endpoint
on the v2 turn path; ``test_error_message_sanitization.py`` closed it for
the v1 ``/query`` path. Neither touched the database side, which had the
identical shape and was still open:

``database/executor.py`` wrapped every ``SQLAlchemyError`` as::

    raise RuntimeError(f"Database error: {exc}") from exc

``str()`` of a SQLAlchemy ``DBAPIError`` (what a pyodbc/SQL Server driver
error becomes) is the driver's own message plus SQLAlchemy's own
``[SQL: …]``, ``[parameters: …]`` and a ``https://sqlalche.me/…``
background-info line — and the driver's own message routinely names the
server host/instance, the port, the database, and the login, e.g.::

    (pyodbc.OperationalError) ('08001', '[08001] [Microsoft][ODBC Driver 17
    for SQL Server]TCP Provider: No such host is known. (11001)
    (SQLDriverConnect); Server: sql-prod-01.internal.corp,1433\\SQLPROD')
    [SQL: SELECT TOP 10 [CustomerName], [Email] FROM [Auction_Dim].[Customer]
    WHERE [Region] = ?]
    [parameters: ('Northwest-Region-Secret',)]
    (Background on this error at: https://sqlalche.me/e/20/e3q8)

That whole string reached ``TurnErrorInfo.message`` on the v2 path
(``session/engine.py``, both the fresh-generation and the correction-retry
branches) and, on the v1 path, either the same full string (a connection
failure whose message happened not to match ``api/runner.py``'s old
substring tests — "unreachable"/"LOCK_TIMEOUT"/"Cannot connect"/"connection"
— fell straight into ``QueryExecutionError``'s client-facing ``message``)
or, for the one substring that did match, a generic message with the raw
text correctly routed to ``detail`` instead. *Every* database failure —
connection lost, wrong login, a genuinely wrong query — surfaced identically
as ``QUERY_EXECUTION_ERROR``, because the classification depended on
substrings the driver's real wording usually does not contain.

The fix, mirroring finding 11's pattern exactly: ``database/errors.py``'s
``classify_database_error`` is the one place, used by both paths, that
decides a connection/login/availability failure or a timeout is not a
statement problem (``DATABASE_UNAVAILABLE`` / ``QUERY_TIMEOUT``, fixed
generic client text) and that a genuine statement error carries only the
database's own error sentence — never the SQL, the bound parameter values,
the host/instance/port/database/login, the driver name, or the
``sqlalche.me`` URL. The raw error is unaffected: ``database.executor``
logs it in full before wrapping, and it stays attached as ``__cause__`` for
an operator-facing ``detail`` field (v1) or the server log (v2, which like
``TurnErrorInfo`` for ``MODEL_UNAVAILABLE`` has no ``detail`` field at
all).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy.exc as sa_exc

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Synthetic exceptions -- shaped like real pyodbc/SQL Server driver errors,
# never a real database connection.
# ---------------------------------------------------------------------------

_STATEMENT = "SELECT TOP 10 [CustomerName], [Email] FROM [Auction_Dim].[Customer] WHERE [Region] = ?"
_PARAMS = ("Northwest-Region-Secret",)  # a bound value that must never echo back
_HOST = "sql-prod-01.internal.corp"
_PORT = "1433"
_LOGIN = "svc_auction_readonly"
_DBNAME = "Auction_DM"

#: Fragments that must never appear in anything handed to the client, for
#: ANY of the cases below -- host, port, login, database name, driver
#: name/version, the SQL, the bound parameter value, and SQLAlchemy's own
#: wrapper text. Same discipline as the sibling modules' `_LEAKY` list.
_LEAKY = [
    _HOST, _PORT, _LOGIN, _DBNAME,
    "ODBC Driver", "Microsoft", "pyodbc",
    "[SQL:", "[parameters:", "sqlalche.me",
    "Northwest-Region-Secret",
    "CustomerName",  # column list from the SQL text itself
]


def _assert_clean(text: str, context: str) -> None:
    leaked = [f for f in _LEAKY if f.lower() in (text or "").lower()]
    assert not leaked, (
        f"{context} exposes {leaked!r} to the caller.\n  text: {text!r}\n"
        "database.errors.classify_database_error should have kept this out "
        "of the client-facing message."
    )


class _FakeDriverError(Exception):
    """Shaped like pyodbc's exception: ``args = (sqlstate, message)``."""

    def __init__(self, sqlstate: str, message: str):
        super().__init__(sqlstate, message)
        self.args = (sqlstate, message)


def _sa_error(cls, sqlstate: str, message: str):
    """A SQLAlchemy DBAPI error wrapping a synthetic pyodbc-shaped ``orig``,
    built with a real statement and real bound parameters so ``str()``
    matches what SQLAlchemy actually produces in production."""
    orig = _FakeDriverError(sqlstate, message)
    return cls(_STATEMENT, _PARAMS, orig)


# name -> (sqlalchemy exception, expected code, a fragment of the DB's own
# message that a QUERY_EXECUTION_ERROR case must still surface)
CASES: dict[str, tuple[object, str, str | None]] = {
    "conn_08001_no_host": (
        _sa_error(
            sa_exc.OperationalError, "08001",
            f"[08001] [Microsoft][ODBC Driver 17 for SQL Server]TCP Provider: "
            f"No such host is known.  (11001) (SQLDriverConnect); "
            f"Server: {_HOST},{_PORT}",
        ),
        "DATABASE_UNAVAILABLE", None,
    ),
    "conn_08001_not_accessible": (
        _sa_error(
            sa_exc.OperationalError, "08001",
            "[08001] [Microsoft][ODBC Driver 17 for SQL Server]A "
            "network-related or instance-specific error occurred while "
            "establishing a connection to SQL Server. Server is not found "
            f"or not accessible. (server = {_HOST}, port = {_PORT})",
        ),
        "DATABASE_UNAVAILABLE", None,
    ),
    "conn_08S01_link_failure": (
        _sa_error(
            sa_exc.OperationalError, "08S01",
            "[08S01] [Microsoft][ODBC Driver 17 for SQL Server]"
            "Communication link failure (10054) (SQLExecDirectW)",
        ),
        "DATABASE_UNAVAILABLE", None,
    ),
    "login_28000_bad_login": (
        _sa_error(
            sa_exc.OperationalError, "28000",
            f"[28000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"
            f"Login failed for user '{_LOGIN}'. (18456)",
        ),
        "DATABASE_UNAVAILABLE", None,
    ),
    "login_cannot_open_db": (
        _sa_error(
            sa_exc.OperationalError, "42000",
            f'[42000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]'
            f'Cannot open database "{_DBNAME}" requested by the login. '
            f'The login failed. (4060)',
        ),
        "DATABASE_UNAVAILABLE", None,
    ),
    "timeout_HYT00": (
        _sa_error(
            sa_exc.OperationalError, "HYT00",
            "[HYT00] [Microsoft][ODBC Driver 17 for SQL Server]"
            "Query timeout expired (0)",
        ),
        "QUERY_TIMEOUT", None,
    ),
    "stmt_42S22_invalid_column": (
        _sa_error(
            sa_exc.ProgrammingError, "42S22",
            "[42S22] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"
            "Invalid column name 'CustomerNam'. (207)",
        ),
        "QUERY_EXECUTION_ERROR", "Invalid column name 'CustomerNam'. (207)",
    ),
    "stmt_42S02_invalid_object": (
        _sa_error(
            sa_exc.ProgrammingError, "42S02",
            "[42S02] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"
            "Invalid object name 'Auction_Dim.Customerz'. (208)",
        ),
        "QUERY_EXECUTION_ERROR", "Invalid object name 'Auction_Dim.Customerz'. (208)",
    ),
    "stmt_22018_conversion": (
        _sa_error(
            sa_exc.DataError, "22018",
            "[22018] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"
            "Conversion failed when converting the varchar value 'abc' to "
            "data type int. (245)",
        ),
        "QUERY_EXECUTION_ERROR",
        "Conversion failed when converting the varchar value 'abc' to data type int. (245)",
    ),
    "stmt_22012_divide_by_zero": (
        _sa_error(
            sa_exc.DataError, "22012",
            "[22012] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"
            "Divide by zero error encountered. (8134)",
        ),
        "QUERY_EXECUTION_ERROR", "Divide by zero error encountered. (8134)",
    ),
}


# ---------------------------------------------------------------------------
# Layer 1: the shared classifier, unit-tested directly.
# ---------------------------------------------------------------------------

class TestClassifyDatabaseError:
    """``database.errors.classify_database_error`` is the one function both
    paths call. If this layer is right, both paths inherit it for free --
    proven separately in ``TestBothPathsAreSanitised`` below."""

    @pytest.mark.parametrize("name", list(CASES))
    def test_code_and_message_are_correct_and_clean(self, name):
        from database.errors import classify_database_error

        exc, expected_code, expected_fragment = CASES[name]
        result = classify_database_error(exc)

        assert result.code == expected_code, (
            f"{name}: classified as {result.code!r}, expected {expected_code!r}"
        )
        _assert_clean(result.client_message, f"{name}'s classify_database_error() message")
        if expected_fragment is not None:
            assert expected_fragment in result.client_message, (
                f"{name}: statement errors must still carry the database's "
                f"own message -- got {result.client_message!r}"
            )

    def test_pool_exhaustion_is_database_unavailable_not_a_query_timeout(self):
        """``sqlalchemy.exc.TimeoutError`` (QueuePool exhausted) never
        reaches a driver -- it has no ``.orig`` -- and is an availability
        problem, not a slow query, even though its own wording contains
        "timeout"."""
        from database.errors import classify_database_error

        exc = sa_exc.TimeoutError(
            "QueuePool limit of size 10 overflow 20 reached, connection "
            f"timed out, timeout 30 (host: {_HOST})"
        )
        result = classify_database_error(exc)
        assert result.code == "DATABASE_UNAVAILABLE"
        _assert_clean(result.client_message, "pool-exhaustion message")

    def test_a_runtime_error_wrapping_the_sqlalchemy_error_classifies_identically(self):
        """This is the real production shape: ``database.executor._execute``
        raises ``RuntimeError(...) from exc``, so the SQLAlchemyError is
        ``__cause__``, not the exception itself."""
        from database.errors import classify_database_error

        sa_error, expected_code, _ = CASES["conn_08001_no_host"]
        wrapper = RuntimeError("Database error: something")
        wrapper.__cause__ = sa_error
        result = classify_database_error(wrapper)
        assert result.code == expected_code
        _assert_clean(result.client_message, "RuntimeError-wrapped classification")


# ---------------------------------------------------------------------------
# Layer 2: driven through the REAL v2 (session.engine.TurnEngine) and v1
# (api.runner._safe_run) paths, monkeypatching only
# database.executor.get_engine -- the lowest layer -- so the real
# executor/engine/runner code does the wrapping and classification.
# ---------------------------------------------------------------------------

def _engine_mock_raising(sa_exception) -> MagicMock:
    conn_mock = MagicMock()
    conn_mock.exec_driver_sql.side_effect = sa_exception
    conn_mock.execution_options.return_value = conn_mock
    conn_mock.connection.dbapi_connection = None
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)

    engine_mock = MagicMock()
    engine_mock.connect.return_value = conn_mock
    return engine_mock


_VALID_SQL = "SELECT TOP 10 [CustomerName] FROM [Auction_Dim].[Customer]"


def _run_v2(sa_exception) -> tuple[str, str]:
    from database.executor import execute_sql
    from llm.providers import MockBackend
    from llm.router import LLMRouter
    from session.engine import TurnEngine
    from session.store import SessionStore

    engine_mock = _engine_mock_raising(sa_exception)
    with patch("database.executor.get_engine", return_value=engine_mock):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response=_VALID_SQL)]),
            execute_fn=execute_sql,
        )
        turn = engine.ask(record, "مشتریان را نشان بده", "You are a T-SQL expert.")
    assert turn.error is not None, "expected the turn to carry an error"
    return turn.error.code, turn.error.message


def _run_v1(sa_exception) -> tuple[str, str, str]:
    from api.errors import NLQError
    from api.runner import _safe_run
    from database.executor import execute_sql
    from llm.providers import MockBackend
    from llm.sql_agent import SQLAgent
    from observability.timing import StageTimer

    engine_mock = _engine_mock_raising(sa_exception)
    with patch("database.executor.get_engine", return_value=engine_mock):
        agent = SQLAgent(backend=MockBackend(response=_VALID_SQL), execute_fn=execute_sql, max_corrections=0)
        with pytest.raises(NLQError) as exc_info:
            _safe_run(agent, "مشتریان را نشان بده", system_prompt="You are a T-SQL expert.", timer=StageTimer())
    err = exc_info.value
    return err.error_code, err.message, (err.detail or "")


#: One representative case per bucket -- the full classification matrix is
#: already covered by TestClassifyDatabaseError; this proves the WIRING
#: (executor -> engine/runner) rather than re-testing the classifier logic.
_WIRING_CASES = [
    "conn_08001_no_host",
    "login_28000_bad_login",
    "timeout_HYT00",
    "stmt_42S22_invalid_column",
]


class TestBothPathsAreSanitised:
    @pytest.mark.parametrize("name", _WIRING_CASES)
    def test_v2_turn_error_is_clean(self, name):
        sa_error, expected_code, expected_fragment = CASES[name]
        code, message = _run_v2(sa_error)
        assert code == expected_code, f"{name}: v2 TurnErrorInfo.code = {code!r}, expected {expected_code!r}"
        _assert_clean(message, f"{name}: v2 TurnErrorInfo.message")
        if expected_fragment is not None:
            assert expected_fragment in message

    @pytest.mark.parametrize("name", _WIRING_CASES)
    def test_v1_error_response_is_clean(self, name):
        sa_error, expected_code, expected_fragment = CASES[name]
        code, message, detail = _run_v1(sa_error)
        assert code == expected_code, f"{name}: v1 error_code = {code!r}, expected {expected_code!r}"
        _assert_clean(message, f"{name}: v1 NLQError.message (client-facing)")
        if expected_fragment is not None:
            assert expected_fragment in message
        # The operator-facing `detail` field is NOT sanitised -- it is meant
        # to carry the full raw error (see TestTheOperatorStillGetsTheDetail
        # below); assert it still does, so the fix did not silently drop it.
        assert _HOST in detail or _LOGIN in detail or "42S22" in detail or "HYT00" in detail or "207" in detail


# ---------------------------------------------------------------------------
# Layer 3: sanitising is not deleting -- the raw error must still reach an
# operator, from the server log.
# ---------------------------------------------------------------------------

class TestTheOperatorStillGetsTheDetail:
    def test_the_raw_error_is_logged_by_the_executor(self, caplog):
        """``database.executor._execute`` logs the full raw SQLAlchemy error
        (host, driver, SQL, bound parameters and all) BEFORE sanitising it
        for the RuntimeError it raises. Asserted behaviourally: the leaky
        text appears in a log record even though the exception's own
        message (and the classify_database_error() result derived from it)
        does not."""
        from database.executor import execute_sql

        sa_error, _, _ = CASES["conn_08001_no_host"]
        engine_mock = _engine_mock_raising(sa_error)

        with caplog.at_level(logging.ERROR, logger="database.executor"):
            with patch("database.executor.get_engine", return_value=engine_mock):
                with pytest.raises(RuntimeError) as exc_info:
                    execute_sql("SELECT 1")

        logged = "\n".join(rec.getMessage() for rec in caplog.records)
        assert _HOST in logged, (
            "the raw driver error was not logged anywhere -- moving it out "
            "of the client-facing message must not mean discarding it"
        )
        _assert_clean(str(exc_info.value), "the RuntimeError raised to the caller")

    def test_v1_detail_field_still_carries_the_raw_cause(self):
        """The v1 NLQError.detail field (operator-only, per api/errors.py's
        own docstring) must keep the ORIGINAL SQLAlchemy error, not this
        RuntimeError's own already-sanitised text."""
        sa_error, _, _ = CASES["login_28000_bad_login"]
        _, _, detail = _run_v1(sa_error)
        assert _LOGIN in detail, (
            f"v1 detail={detail!r} lost the raw login name an operator "
            "needs to diagnose the failure"
        )


# ---------------------------------------------------------------------------
# Layer 4: the OLD substring classification is gone, not just shadowed.
# ---------------------------------------------------------------------------

class TestOldFragileClassificationIsReplaced:
    """The pre-fix ``api/runner.py`` matched literal ``"Cannot connect"`` /
    ``"connection"`` in the wrapped message text -- which a real pyodbc
    connection failure's own wording usually does not contain (see the
    module docstring). This is a regression guard against that specific
    fragility reappearing."""

    def test_a_connection_failure_whose_text_says_neither_connect_nor_connection(self):
        from database.errors import classify_database_error

        # No "connect"/"connection" substring anywhere in this message --
        # exactly the shape that used to fall through to QUERY_EXECUTION_ERROR.
        exc = _sa_error(
            sa_exc.OperationalError, "08S01",
            "[08S01] [Microsoft][ODBC Driver 17 for SQL Server]"
            "Communication link failure (10054) (SQLExecDirectW)",
        )
        message_text = str(exc.orig.args[1])
        assert "connect" not in message_text.lower()

        result = classify_database_error(exc)
        assert result.code == "DATABASE_UNAVAILABLE"
