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


# ---------------------------------------------------------------------------
# Round 2 -- independent verification found the classifier failed OPEN and
# was shaped only for T-SQL/pyodbc. config.Settings.sql_dialect also
# supports postgres, mysql and sqlite (security/dialects.py); every case
# below is from that verification's table.
# ---------------------------------------------------------------------------

_HOSTMARK = "HOSTMARK"
_LOGINMARK = "LOGINMARK"
_IPMARK = "10.0.0.5"
_PORTMARK = "5432"
_PARAMVALUE = "PARAMVALUE"

#: Markers planted in the round-2 cases below -- must never reach a client,
#: whatever dialect or exception shape produced them.
_ROUND2_MARKERS = [
    _HOSTMARK, _LOGINMARK, _IPMARK, _PORTMARK,
    "ODBC Driver", "Microsoft", "SQLExecDirectW", _PARAMVALUE,
    "[SQL:", "[parameters:", "sqlalche.me",
]


def _assert_none_leaked(text: str, markers: list, context: str) -> None:
    leaked = [m for m in markers if m.lower() in (text or "").lower()]
    assert not leaked, f"{context} exposes {leaked!r} to the caller.\n  text: {text!r}"


def _wrap(cls, orig, *, statement: str = _STATEMENT, params=(_PARAMVALUE,)):
    return cls(statement, params, orig)


def _mysql_orig(errno: int, message: str) -> Exception:
    """A MySQL-driver-shaped exception: ``args = (errno, message)`` --
    MySQL drivers use a numeric code, never a SQLSTATE tuple."""
    return Exception(errno, message)


def _text_orig(message: str) -> Exception:
    """A driver exception whose ``args`` collapse to a single string --
    the shape of a psycopg2 error with no ``.pgcode`` (a client-side
    connect failure, before the server ever assigns one), a sqlite3
    error, or any exception shape this module has never specifically
    seen."""
    return Exception(message)


def _pg_orig_with_sqlstate(sqlstate: str, message: str) -> Exception:
    """A psycopg2/3-shaped exception exposing a real SQLSTATE via
    ``.pgcode``/``.pgerror`` -- never in ``.args``, unlike pyodbc."""
    class _PgOrig(Exception):
        pass

    orig = _PgOrig(message)
    orig.pgcode = sqlstate
    orig.pgerror = message
    return orig


class TestFailsClosedAcrossDialects:
    """Every row the independent verification measured as leaking or
    misclassified against the round-1 (T-SQL-only) classifier."""

    def test_mysql_access_denied_does_not_leak_login_or_ip(self):
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _mysql_orig(
            1045, f"Access denied for user '{_LOGINMARK}'@'{_IPMARK}' (using password: YES)"
        ))
        result = classify_database_error(exc)
        assert result.code == "DATABASE_UNAVAILABLE"
        _assert_none_leaked(result.client_message, _ROUND2_MARKERS, "mysql access-denied message")

    def test_postgres_auth_failure_does_not_leak_host_login_or_port(self):
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _text_orig(
            f'connection to server at "{_HOSTMARK}" ({_IPMARK}), port {_PORTMARK} failed: '
            f'FATAL:  password authentication failed for user "{_LOGINMARK}"'
        ))
        result = classify_database_error(exc)
        assert result.code == "DATABASE_UNAVAILABLE"
        _assert_none_leaked(result.client_message, _ROUND2_MARKERS, "postgres auth-failure message")

    def test_tsql_single_string_tuple_repr_shape_is_still_scrubbed(self):
        """``.orig.args`` collapsed to ONE string that is itself the repr
        of a ``(sqlstate, message)`` tuple, instead of a real two-element
        tuple -- the shape that leaked the whole raw string, brackets and
        all, before round 2."""
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.ProgrammingError, _text_orig(
            "('42S22', \"[42S22] [Microsoft][ODBC Driver 17 for SQL Server]"
            "[SQL Server]Invalid column name 'X'. (207) (SQLExecDirectW)\")"
        ))
        result = classify_database_error(exc)
        assert result.code == "QUERY_EXECUTION_ERROR"
        _assert_none_leaked(result.client_message, _ROUND2_MARKERS, "single-string-tuple-repr message")
        assert "Invalid column name 'X'. (207)" in result.client_message

    def test_real_pyodbc_shape_strips_trailing_odbc_api_name(self):
        """The trailing ``(SQLExecDirectW)`` ODBC API-call name is
        implementation noise, not part of the database's own sentence."""
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.ProgrammingError, Exception(
            "42S22",
            "[42S22] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"
            "Invalid column name 'X'. (207) (SQLExecDirectW)",
        ))
        result = classify_database_error(exc)
        assert result.code == "QUERY_EXECUTION_ERROR"
        _assert_none_leaked(result.client_message, _ROUND2_MARKERS, "pyodbc-shaped message")
        assert result.client_message == "Invalid column name 'X'. (207)"

    def test_sqlite_database_is_locked_is_unavailable_not_a_statement_error(self):
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _text_orig("database is locked"))
        result = classify_database_error(exc)
        assert result.code == "DATABASE_UNAVAILABLE", (
            "a locked database is a transient availability condition, not "
            "a bad statement -- the analyst's question was not the problem"
        )

    def test_sqlite_database_is_busy_is_also_unavailable(self):
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _text_orig("database is busy"))
        assert classify_database_error(exc).code == "DATABASE_UNAVAILABLE"

    def test_mysql_connection_refused_is_still_correct(self):
        """Regression guard: this one was already right before round 2."""
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _mysql_orig(
            2003, f"Can't connect to MySQL server on '{_HOSTMARK}' ({_IPMARK})"
        ))
        result = classify_database_error(exc)
        assert result.code == "DATABASE_UNAVAILABLE"
        _assert_none_leaked(result.client_message, _ROUND2_MARKERS, "mysql connection-refused message")

    def test_sqlite_no_such_table_stays_a_recognised_statement_error(self):
        """Regression guard: this one was already right before round 2."""
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _text_orig("no such table: missing_table"))
        result = classify_database_error(exc)
        assert result.code == "QUERY_EXECUTION_ERROR"
        assert "no such table: missing_table" in result.client_message

    def test_postgres_statement_error_keeps_only_the_first_line(self):
        """Regression guard: this one was already right before round 2 --
        proven here against the multi-line LINE/caret shape a real
        psycopg2 ``ProgrammingError.pgerror`` carries."""
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.ProgrammingError, _text_orig(
            'ERROR:  column "foo" does not exist\n'
            "LINE 1: SELECT foo FROM bar\n"
            "               ^\n"
        ))
        result = classify_database_error(exc)
        assert result.code == "QUERY_EXECUTION_ERROR"
        assert result.client_message == 'column "foo" does not exist'
        assert "LINE 1" not in result.client_message


class TestOtherDialectCodesFromThePlan:
    """The specific structured codes requested: postgres via ``.pgcode``
    (SQLSTATE classes 08/28, admin shutdown 57P0x, query-cancel timeout
    57014), and MySQL's remaining numeric codes."""

    @pytest.mark.parametrize("sqlstate", ["08006", "08001", "28P01", "28000"])
    def test_postgres_connection_and_auth_sqlstates_are_unavailable(self, sqlstate):
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _pg_orig_with_sqlstate(
            sqlstate, f'connection to server at "{_HOSTMARK}" ({_IPMARK}), port {_PORTMARK} failed'
        ))
        result = classify_database_error(exc)
        assert result.code == "DATABASE_UNAVAILABLE"
        _assert_none_leaked(result.client_message, _ROUND2_MARKERS, f"postgres {sqlstate} message")

    def test_postgres_admin_shutdown_is_unavailable(self):
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _pg_orig_with_sqlstate(
            "57P01", "terminating connection due to administrator command"
        ))
        assert classify_database_error(exc).code == "DATABASE_UNAVAILABLE"

    def test_postgres_query_cancelled_by_statement_timeout_is_query_timeout(self):
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _pg_orig_with_sqlstate(
            "57014", "canceling statement due to statement timeout"
        ))
        assert classify_database_error(exc).code == "QUERY_TIMEOUT"

    @pytest.mark.parametrize("errno", [2002, 2005, 2006, 2013, 1044, 1049])
    def test_mysql_connection_family_errnos_are_unavailable(self, errno):
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _mysql_orig(errno, f"connection problem near {_HOSTMARK}"))
        assert classify_database_error(exc).code == "DATABASE_UNAVAILABLE"

    @pytest.mark.parametrize("errno", [1205, 3024])
    def test_mysql_timeout_errnos_are_query_timeout(self, errno):
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.OperationalError, _mysql_orig(errno, "Lock wait timeout exceeded"))
        assert classify_database_error(exc).code == "QUERY_TIMEOUT"


class TestTheSafetyNet:
    """Point 4: after scrubbing, a client message that still looks
    identifying is replaced by the generic message -- even for a
    statement-family exception TYPE (positively recognised) whose TEXT
    happens to still name infrastructure (a shape nobody anticipated)."""

    def test_an_unrecognised_shape_containing_an_ip_gets_the_generic_message(self):
        from database.errors import classify_database_error

        exc = _wrap(sa_exc.ProgrammingError, _text_orig(f"unexpected failure talking to {_IPMARK}"))
        result = classify_database_error(exc)
        assert result.code == "QUERY_EXECUTION_ERROR"
        assert result.client_message == "The database rejected the query."
        _assert_none_leaked(result.client_message, [_IPMARK], "safety-net message")

    def test_the_safety_net_catches_ip_port_user_at_host_and_infrastructure_words(self):
        from database.errors import _apply_safety_net

        cases = [
            "failure near port 5432",
            f"'{_LOGINMARK}'@'{_HOSTMARK}'",
            f"{_LOGINMARK}@{_HOSTMARK}",
            "connection refused by server at somewhere",
            "the Microsoft driver reported an issue",
            "an ODBC problem occurred",
            "login rejected",
            f"talking to {_IPMARK}",
        ]
        for text in cases:
            result = _apply_safety_net(text)
            assert result.client_message == "The database rejected the query.", (
                f"safety net let {text!r} through unchanged"
            )

    def test_an_ordinary_statement_message_passes_the_safety_net_unchanged(self):
        from database.errors import _apply_safety_net

        result = _apply_safety_net("Invalid column name 'X'. (207)")
        assert result.client_message == "Invalid column name 'X'. (207)"


# ---------------------------------------------------------------------------
# Round 3 -- a second independent pass found one gap round 2 left OPEN on
# purpose: `_classify_by_text`, the fallback for an exception with no
# `SQLAlchemyError` anywhere on it, was documented as "deliberately
# narrow ... real production failures always carry the original
# SQLAlchemyError as __cause__." That premise was wrong:
# session/engine.py's `except Exception` around each execute() call hands
# ANY exception to classify_database_error, and database.executor._execute
# only wraps SQLAlchemyError -- a raw OSError/socket error (e.g. from the
# driver-level-timeout setattr on the raw DBAPI connection, which operates
# outside SQLAlchemy's own wrapping) reaches the fallback unwrapped, in
# production, not just from a hand-built test double.
# ---------------------------------------------------------------------------

def _run_v2_with_execute_fn(execute_fn) -> tuple[str, str]:
    """Like ``_run_v2``, but takes a raw ``execute_fn`` directly instead of
    a SQLAlchemy exception routed through the mocked engine -- this is what
    lets a case carry NO SQLAlchemyError anywhere, proving
    ``_classify_by_text`` (not ``_classify_sqlalchemy_error``) is what's
    under test, reached the same way session.engine.TurnEngine reaches it
    in production."""
    from llm.providers import MockBackend
    from llm.router import LLMRouter
    from session.engine import TurnEngine
    from session.store import SessionStore

    store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
    record = store.create()
    engine = TurnEngine(
        router=LLMRouter(default_chain=[MockBackend(response=_VALID_SQL)]),
        execute_fn=execute_fn,
    )
    turn = engine.ask(record, "مشتریان را نشان بده", "You are a T-SQL expert.")
    assert turn.error is not None, "expected the turn to carry an error"
    return turn.error.code, turn.error.message


class TestTheTextFallbackFailsClosedToo:
    def test_unrecognised_availability_wording_does_not_leak_a_hostname(self):
        from database.errors import classify_database_error

        exc = RuntimeError(f"Database error: server {_HOSTMARK}.corp unreachable")
        result = classify_database_error(exc)
        assert result.code == "DATABASE_UNAVAILABLE"
        _assert_none_leaked(result.client_message, [_HOSTMARK], "unreachable-server fallback message")

    def test_a_raw_oserror_through_the_real_engine_path_does_not_leak_a_hostname(self):
        """Proves production reachability: no SQLAlchemyError anywhere on
        this exception, driven through the real
        session.engine.TurnEngine.ask() -- exactly the shape a real
        getaddrinfo failure from the raw pyodbc connection takes."""
        def _raise_oserror(sql):
            raise OSError(f"[Errno 11001] getaddrinfo failed for {_HOSTMARK}")

        code, message = _run_v2_with_execute_fn(_raise_oserror)
        assert code == "DATABASE_UNAVAILABLE"
        _assert_none_leaked(message, [_HOSTMARK], "OSError-through-engine TurnErrorInfo.message")

    def test_a_raw_dbapi_style_statement_sentence_survives(self):
        """A real statement-error sentence, from an exception with no
        SQLAlchemyError anywhere on it, still reaches the client -- the
        allowlist keeps this path useful, not just safe."""
        from database.errors import classify_database_error

        exc = RuntimeError("Invalid column name 'X'. (207)")
        result = classify_database_error(exc)
        assert result.code == "QUERY_EXECUTION_ERROR"
        assert result.client_message == "Invalid column name 'X'. (207)"

    def test_a_non_database_exception_gets_the_generic_message_not_its_own_text(self):
        """A KeyError/ValueError from unrelated code (e.g. pandas building
        the result frame) is not database text at all and must not be
        echoed to the client on the strength of "we don't recognise it as
        anything else.\""""
        from database.errors import classify_database_error

        exc = KeyError("secret_internal_key")
        result = classify_database_error(exc)
        assert result.code == "QUERY_EXECUTION_ERROR"
        assert result.client_message == "The database rejected the query."
        _assert_none_leaked(result.client_message, ["secret_internal_key"], "KeyError fallback message")
