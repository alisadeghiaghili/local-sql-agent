"""Unit tests for database/executor.py.

All SQLAlchemy calls are mocked — no real database needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from database.executor import execute_sql


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
