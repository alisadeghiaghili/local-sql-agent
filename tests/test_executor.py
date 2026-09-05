# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for database/executor.py.

All SQLAlchemy calls are mocked — no real database needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from database.executor import execute_sql, execute_sql_params


def _make_engine_mock(rows, columns):
    """Build a mock engine whose connect() context manager returns fake results.

    ``execution_options()`` is configured to return the *same* mock object
    it was called on, matching real ``sqlalchemy.engine.Connection``
    behaviour (it mutates and returns ``self``) — this keeps
    ``conn = conn.execution_options(...)`` inside ``execute_sql`` pointing
    at the object every other assertion in this file inspects.
    """
    result_mock = MagicMock()
    result_mock.fetchmany.return_value = rows
    result_mock.keys.return_value = columns

    conn_mock = MagicMock()
    conn_mock.exec_driver_sql.return_value = result_mock
    conn_mock.execution_options.return_value = conn_mock
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)

    engine_mock = MagicMock()
    engine_mock.connect.return_value = conn_mock
    return engine_mock


def _conn(engine_mock):
    """The mock Connection object execute_sql() actually operates on."""
    return engine_mock.connect.return_value.__enter__.return_value


class TestExecuteSql:
    def test_returns_dataframe(self):
        engine = _make_engine_mock([("Ali", 90)], ["Name", "Score"])
        with patch("database.executor.get_engine", return_value=engine):
            df = execute_sql("SELECT Name, Score FROM t")
        assert isinstance(df, pd.DataFrame)

    def test_dataframe_has_correct_columns(self):
        engine = _make_engine_mock([("Ali", 90)], ["Name", "Score"])
        with patch("database.executor.get_engine", return_value=engine):
            df = execute_sql("SELECT Name, Score FROM t")
        assert list(df.columns) == ["Name", "Score"]

    def test_dataframe_has_correct_rows(self):
        rows = [("Ali", 90), ("Sara", 85)]
        engine = _make_engine_mock(rows, ["Name", "Score"])
        with patch("database.executor.get_engine", return_value=engine):
            df = execute_sql("SELECT Name, Score FROM t")
        assert len(df) == 2
        assert df.iloc[0]["Name"] == "Ali"

    def test_empty_result_returns_empty_dataframe(self):
        engine = _make_engine_mock([], ["Name", "Score"])
        with patch("database.executor.get_engine", return_value=engine):
            df = execute_sql("SELECT Name, Score FROM t")
        assert df.empty
        assert list(df.columns) == ["Name", "Score"]

    def test_wraps_sqlalchemy_error_in_runtime_error(self):
        engine = MagicMock()
        engine.connect.side_effect = OperationalError("conn", {}, Exception("fail"))
        with patch("database.executor.get_engine", return_value=engine):
            with pytest.raises(RuntimeError, match="Database error"):
                execute_sql("SELECT 1")

    def test_wraps_engine_acquisition_failure_in_runtime_error(self):
        """get_engine() itself failing (e.g. a bad connection URL resolved
        at engine-construction time) must also come back as RuntimeError —
        not propagate as a bare SQLAlchemyError — since it now runs inside
        the same try block as the rest of the database round trip."""
        with patch(
            "database.executor.get_engine",
            side_effect=SQLAlchemyError("cannot construct engine"),
        ):
            with pytest.raises(RuntimeError, match="Database error"):
                execute_sql("SELECT 1")

    def test_uses_exec_driver_sql_not_text(self):
        """`conn.execute(text(sql))` would let SQLAlchemy's `text()`
        construct interpret a literal `:name` inside *sql* as a bind
        parameter placeholder. `exec_driver_sql` sends the string to the
        driver unmodified, so a query containing `N' :label'` executes as
        written instead of failing on an unbound parameter."""
        engine = _make_engine_mock([], ["x"])
        with patch("database.executor.get_engine", return_value=engine):
            execute_sql("SELECT ' :label' AS x")
        conn = _conn(engine)
        executed_sql = [c.args[0] for c in conn.exec_driver_sql.call_args_list]
        assert "SELECT ' :label' AS x" in executed_sql

    def test_lock_timeout_is_set(self):
        """SET LOCK_TIMEOUT must be issued via exec_driver_sql, before the
        query itself."""
        engine = _make_engine_mock([], ["x"])
        with patch("database.executor.get_engine", return_value=engine):
            execute_sql("SELECT 1")
        conn = _conn(engine)
        calls = [c.args[0] for c in conn.exec_driver_sql.call_args_list]
        assert any("LOCK_TIMEOUT" in c for c in calls)
        # LOCK_TIMEOUT must run before the query itself.
        lock_idx = next(i for i, c in enumerate(calls) if "LOCK_TIMEOUT" in c)
        query_idx = calls.index("SELECT 1")
        assert lock_idx < query_idx

    def test_driver_level_query_timeout_is_set(self):
        """The raw pyodbc connection's `.timeout` attribute must be set
        from cfg.settings.query_timeout_seconds -- this bounds a heavy,
        lock-free query that SET LOCK_TIMEOUT alone would never catch."""
        engine = _make_engine_mock([], ["x"])
        import config as cfg
        from config import override_settings
        with patch("database.executor.get_engine", return_value=engine):
            with override_settings(query_timeout_seconds=45, max_rows_returned=100):
                execute_sql("SELECT 1")
        conn = _conn(engine)
        assert conn.connection.dbapi_connection.timeout == 45

    def test_stream_results_execution_option_is_set(self):
        engine = _make_engine_mock([], ["x"])
        with patch("database.executor.get_engine", return_value=engine):
            execute_sql("SELECT 1")
        raw_conn = engine.connect.return_value.__enter__.return_value
        raw_conn.execution_options.assert_called_once_with(stream_results=True)

    def test_transaction_is_started_and_rolled_back_on_success(self):
        """The query runs inside an explicit transaction that is always
        rolled back, never committed -- T-SQL has no native read-only
        transaction mode, so this is the practical equivalent for a
        connection that must never persist a write."""
        engine = _make_engine_mock([], ["x"])
        with patch("database.executor.get_engine", return_value=engine):
            execute_sql("SELECT 1")
        conn = _conn(engine)
        conn.begin.assert_called_once()
        conn.begin.return_value.rollback.assert_called_once()
        conn.begin.return_value.commit.assert_not_called()

    def test_transaction_is_rolled_back_even_when_query_raises(self):
        engine = _make_engine_mock([], ["x"])
        conn = _conn(engine)
        conn.exec_driver_sql.side_effect = [None, SQLAlchemyError("boom")]
        with patch("database.executor.get_engine", return_value=engine):
            with pytest.raises(RuntimeError):
                execute_sql("SELECT 1")
        conn.begin.return_value.rollback.assert_called_once()
        conn.begin.return_value.commit.assert_not_called()

    def test_respects_max_rows_setting(self):
        engine = _make_engine_mock([], ["x"])
        import config as cfg
        from config import override_settings
        with patch("database.executor.get_engine", return_value=engine):
            with override_settings(query_timeout_seconds=60, max_rows_returned=42):
                execute_sql("SELECT 1")
        result = _conn(engine).exec_driver_sql.return_value
        result.fetchmany.assert_called_once_with(42)


class TestExecuteSqlParams:
    """The Phase 5b parameterised sibling of execute_sql.

    Same mocked-engine discipline as TestExecuteSql above -- this proves
    execute_sql_params is the SAME execution path (same timeout, same
    rollback-only transaction, same row cap) with one addition: params are
    passed straight through to exec_driver_sql, never folded into the SQL
    string.
    """

    def test_passes_params_to_exec_driver_sql_unmodified(self):
        engine = _make_engine_mock([("شرکت فولاد مبارکه اصفهان",)], ["Name"])
        with patch("database.executor.get_engine", return_value=engine):
            df = execute_sql_params(
                "SELECT DISTINCT TOP (?) [Name] FROM [Auction_Dim].[Customer] WHERE [Name] LIKE ?",
                (10, "%مبارکه%"),
            )
        assert list(df["Name"]) == ["شرکت فولاد مبارکه اصفهان"]
        conn = _conn(engine)
        # The query call (not the SET LOCK_TIMEOUT call) must have been
        # made with exactly (sql, params) -- two positional args, params
        # untouched.
        query_call = next(
            c for c in conn.exec_driver_sql.call_args_list if "LOCK_TIMEOUT" not in c.args[0]
        )
        assert query_call.args == (
            "SELECT DISTINCT TOP (?) [Name] FROM [Auction_Dim].[Customer] WHERE [Name] LIKE ?",
            (10, "%مبارکه%"),
        )

    def test_wraps_sqlalchemy_error_in_runtime_error(self):
        engine = MagicMock()
        engine.connect.side_effect = OperationalError("conn", {}, Exception("fail"))
        with patch("database.executor.get_engine", return_value=engine):
            with pytest.raises(RuntimeError, match="Database error"):
                execute_sql_params("SELECT ? ", (1,))

    def test_transaction_is_started_and_rolled_back_on_success(self):
        engine = _make_engine_mock([], ["x"])
        with patch("database.executor.get_engine", return_value=engine):
            execute_sql_params("SELECT ?", (1,))
        conn = _conn(engine)
        conn.begin.assert_called_once()
        conn.begin.return_value.rollback.assert_called_once()
        conn.begin.return_value.commit.assert_not_called()

    def test_driver_level_query_timeout_is_set(self):
        engine = _make_engine_mock([], ["x"])
        from config import override_settings
        with patch("database.executor.get_engine", return_value=engine):
            with override_settings(query_timeout_seconds=45, max_rows_returned=100):
                execute_sql_params("SELECT ?", (1,))
        conn = _conn(engine)
        assert conn.connection.dbapi_connection.timeout == 45

    def test_respects_max_rows_setting(self):
        engine = _make_engine_mock([], ["x"])
        from config import override_settings
        with patch("database.executor.get_engine", return_value=engine):
            with override_settings(query_timeout_seconds=60, max_rows_returned=7):
                execute_sql_params("SELECT ?", (1,))
        result = _conn(engine).exec_driver_sql.return_value
        result.fetchmany.assert_called_once_with(7)

    def test_execute_sql_params_is_a_distinct_callable_from_execute_sql(self):
        assert execute_sql_params is not execute_sql


class TestRealConnectionTransaction:
    """Drive a REAL SQLAlchemy connection, not a substituted one.

    Every other test in this file replaces the engine, which is right for
    what they assert but structurally blind to one whole class of bug: the
    transaction lifecycle belongs to SQLAlchemy, so a mock cannot exhibit
    it and cannot contradict it either.

    That blindness shipped a real one. SQLAlchemy 2.0 autobegins a
    transaction on a connection's first statement, so issuing the
    session-timeout statement before ``conn.begin()`` left an implicit
    transaction open and the explicit ``begin()`` raised
    ``InvalidRequestError`` -- on *every* query, against every dialect
    that has a session-timeout statement configured. The suite stayed
    green: the unit tests mocked the engine, and the integration tests
    that use a real one are gated behind ``RUN_LIVE_DB_TESTS``, which is
    off by default and needs a warehouse nobody has in CI.

    SQLite is used here because it is the one engine that needs no
    server, and its profile is given a session-timeout statement it
    actually accepts (``PRAGMA busy_timeout``) so that the ordering under
    test is genuinely exercised -- SQLite's real profile has none, so
    without this substitution the setup step would no-op and the test
    would pass while proving nothing.
    """

    @staticmethod
    def _sqlite_engine_with_rows():
        from sqlalchemy import create_engine

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE t (name TEXT, amount INTEGER)")
            conn.exec_driver_sql("INSERT INTO t VALUES ('alpha', 10), ('beta', 20)")
        return engine

    @staticmethod
    def _profile_with_session_setup():
        """SQLite's real profile, but with a session statement SQLite accepts."""
        import dataclasses

        from security.dialects import get_dialect_profile

        return dataclasses.replace(
            get_dialect_profile("sqlite"),
            session_timeout_statement="PRAGMA busy_timeout = {timeout_ms}",
        )

    def test_a_query_runs_when_the_dialect_has_a_session_timeout_statement(self):
        engine = self._sqlite_engine_with_rows()
        profile = self._profile_with_session_setup()

        from config import override_settings

        with override_settings(sql_dialect="sqlite", max_rows_returned=100):
            with patch("database.executor.get_engine", return_value=engine), \
                 patch("database.executor.get_dialect_profile", return_value=profile):
                df = execute_sql("SELECT name, amount FROM t ORDER BY amount")

        assert list(df["name"]) == ["alpha", "beta"], (
            "the session-timeout statement autobegins a transaction; if it runs "
            "before conn.begin() then begin() raises InvalidRequestError and no "
            "query can execute at all"
        )

    def test_the_transaction_is_rolled_back_not_committed(self):
        """The read-only contract still holds once the ordering is fixed."""
        engine = self._sqlite_engine_with_rows()
        profile = self._profile_with_session_setup()

        from config import override_settings

        with override_settings(sql_dialect="sqlite", max_rows_returned=100):
            with patch("database.executor.get_engine", return_value=engine), \
                 patch("database.executor.get_dialect_profile", return_value=profile):
                # RETURNING so this goes down the same rows/keys path a
                # SELECT does -- the point is the rollback, not the shape.
                execute_sql("INSERT INTO t VALUES ('gamma', 30) RETURNING name")

        with engine.connect() as conn:
            remaining = conn.exec_driver_sql("SELECT COUNT(*) FROM t").scalar()
        assert remaining == 2, (
            "execute_sql must always roll back -- a write that reached this "
            "layer must not survive it (the guard is what refuses writes; this "
            "is the belt underneath it)"
        )
