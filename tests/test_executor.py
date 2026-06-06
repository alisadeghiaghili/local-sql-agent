"""Unit tests for database/executor.py.

All SQLAlchemy calls are mocked — no real database needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError

from database.executor import execute_sql


def _make_engine_mock(rows, columns):
    """Build a mock engine whose connect() context manager returns fake results."""
    result_mock = MagicMock()
    result_mock.fetchmany.return_value = rows
    result_mock.keys.return_value = columns

    conn_mock = MagicMock()
    conn_mock.execute.return_value = result_mock
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)

    engine_mock = MagicMock()
    engine_mock.connect.return_value = conn_mock
    return engine_mock


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

    def test_lock_timeout_is_set(self):
        """Verify SET LOCK_TIMEOUT is the first statement executed."""
        engine = _make_engine_mock([], ["x"])
        with patch("database.executor.get_engine", return_value=engine):
            execute_sql("SELECT 1")
        conn = engine.connect.return_value.__enter__.return_value
        first_call_arg = str(conn.execute.call_args_list[0])
        assert "LOCK_TIMEOUT" in first_call_arg

    def test_respects_max_rows_setting(self):
        engine = _make_engine_mock([], ["x"])
        with patch("database.executor.get_engine", return_value=engine), \
             patch("database.executor.settings") as mock_settings:
            mock_settings.query_timeout_seconds = 60
            mock_settings.max_rows_returned = 42
            execute_sql("SELECT 1")
        conn = engine.connect.return_value.__enter__.return_value
        result = conn.execute.return_value
        result.fetchmany.assert_called_once_with(42)
