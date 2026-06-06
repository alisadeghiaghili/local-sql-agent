"""Integration tests: question → SQL → (mock) execute → Excel → log.

All external I/O (Ollama HTTP, SQL Server) is mocked.  The test
verifies that the modules are correctly wired together and that
interface changes between layers are caught.

Coverage
--------
- generate_sql() → validate_sql() → execute_sql() → export_excel() → save_log()
- OUT_OF_SCOPE sentinel propagates cleanly through the stack
- SQL validation error propagates cleanly
- Row-cap is respected end-to-end (override_settings propagates to executor)
- Log file receives correct status on success, error, and out-of-scope
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import config as cfg
from config import override_settings
from database.executor import execute_sql
from exporters.excel_exporter import export_excel
from llm.ollama_client import generate_sql
from logs.logger import save_log
from logs.query_log import QueryLog
from security.sql_guard import validate_sql


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_query_log(question: str, sql: str, status: str, error: str | None = None) -> QueryLog:
    return QueryLog(
        timestamp=datetime.now(),
        question=question,
        generated_sql=sql,
        model_name=cfg.settings.ollama_model,
        status=status,  # type: ignore[arg-type]
        execution_time_seconds=0.1,
        row_count=0,
        error_message=error,
    )


# ---------------------------------------------------------------------------
# Full happy-path: question → SQL → DataFrame → Excel → log
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_full_pipeline_success(self, tmp_path):
        """Mocked Ollama returns valid SQL → executor returns DataFrame → Excel written."""
        mock_sql = "SELECT TOP 5 Name FROM [Auction_Dim].[Customer]"
        mock_df  = pd.DataFrame({"Name": ["Alice", "Bob", "Carol"]})

        with override_settings(log_dir=str(tmp_path), export_dir=str(tmp_path)):
            with patch("llm.ollama_client.requests.post") as mock_post, \
                 patch("database.executor.get_engine") as mock_engine:

                # --- mock Ollama ---
                mock_resp = MagicMock()
                mock_resp.json.return_value = {"response": mock_sql}
                mock_resp.raise_for_status.return_value = None
                mock_post.return_value = mock_resp

                # --- mock SQLAlchemy engine ---
                mock_conn = MagicMock()
                mock_result = MagicMock()
                mock_result.fetchmany.return_value = [
                    ("Alice",), ("Bob",), ("Carol",)
                ]
                mock_result.keys.return_value = ["Name"]
                mock_conn.__enter__ = MagicMock(return_value=mock_conn)
                mock_conn.__exit__ = MagicMock(return_value=False)
                mock_conn.execute.return_value = mock_result
                mock_engine.return_value.connect.return_value = mock_conn

                # --- run pipeline ---
                system_prompt = "You are a SQL agent."
                sql = generate_sql("top 5 customers", system_prompt)
                validate_sql(sql)
                df  = execute_sql(sql)

                assert sql == mock_sql
                assert list(df.columns) == ["Name"]
                assert len(df) == 3

                # --- export ---
                excel_path = export_excel(df)
                assert Path(excel_path).exists()
                assert excel_path.endswith(".xlsx")

                # --- log ---
                log = _make_query_log("top 5 customers", sql, "SUCCESS")
                save_log(log)
                log_lines = (tmp_path / "query_log.jsonl").read_text().strip().splitlines()
                assert len(log_lines) == 1
                record = json.loads(log_lines[0])
                assert record["status"] == "SUCCESS"
                assert record["question"] == "top 5 customers"

    def test_row_cap_respected_end_to_end(self, tmp_path):
        """override_settings(max_rows_returned=2) must be seen by executor at runtime."""
        with override_settings(max_rows_returned=2, export_dir=str(tmp_path)):
            with patch("database.executor.get_engine") as mock_engine:
                mock_conn   = MagicMock()
                mock_result = MagicMock()
                # fetchmany is called with cfg.settings.max_rows_returned — must be 2
                mock_result.fetchmany.side_effect = lambda n: [("x",)] * n
                mock_result.keys.return_value = ["col"]
                mock_conn.__enter__ = MagicMock(return_value=mock_conn)
                mock_conn.__exit__  = MagicMock(return_value=False)
                mock_conn.execute.return_value = mock_result
                mock_engine.return_value.connect.return_value = mock_conn

                df = execute_sql("SELECT TOP 2 col FROM t")

            assert len(df) == 2
            # Confirm fetchmany was called with the patched cap
            mock_result.fetchmany.assert_called_once_with(2)


# ---------------------------------------------------------------------------
# OUT_OF_SCOPE propagation
# ---------------------------------------------------------------------------

class TestOutOfScope:
    def test_out_of_scope_raises_value_error(self, tmp_path):
        """When Ollama returns OUT_OF_SCOPE, generate_sql must raise ValueError."""
        with patch("llm.ollama_client.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"response": "OUT_OF_SCOPE"}
            mock_resp.raise_for_status.return_value = None
            mock_post.return_value = mock_resp

            with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
                generate_sql("delete all records", "system prompt")

    def test_out_of_scope_logged_correctly(self, tmp_path):
        with override_settings(log_dir=str(tmp_path)):
            log = _make_query_log("delete all", "", "OUT_OF_SCOPE", error="OUT_OF_SCOPE")
            save_log(log)
            record = json.loads((tmp_path / "query_log.jsonl").read_text())
            assert record["status"] == "OUT_OF_SCOPE"
            assert record["generated_sql"] == ""


# ---------------------------------------------------------------------------
# SQL validation error propagation
# ---------------------------------------------------------------------------

class TestValidationError:
    def test_forbidden_keyword_raises(self):
        with pytest.raises(ValueError, match="Forbidden keyword"):
            validate_sql("DELETE FROM [dbo].[Customer]")

    def test_limit_clause_raises(self):
        with pytest.raises(ValueError, match="LIMIT"):
            validate_sql("SELECT Name FROM [dbo].[T] LIMIT 10")

    def test_error_status_logged(self, tmp_path):
        with override_settings(log_dir=str(tmp_path)):
            log = _make_query_log("bad q", "DELETE FROM t", "ERROR", error="Forbidden keyword")
            save_log(log)
            record = json.loads((tmp_path / "query_log.jsonl").read_text())
            assert record["status"] == "ERROR"
            assert "DELETE" in record["generated_sql"]


# ---------------------------------------------------------------------------
# Ollama unreachable
# ---------------------------------------------------------------------------

class TestOllamaUnreachable:
    def test_raises_runtime_error_after_retries(self):
        import requests as req
        with patch("llm.ollama_client.requests.post",
                   side_effect=req.ConnectionError("refused")), \
             patch("llm.ollama_client.time.sleep"):   # skip back-off waits
            with pytest.raises(RuntimeError, match="unreachable"):
                generate_sql("any question", "system prompt")


# ---------------------------------------------------------------------------
# Excel export integrity
# ---------------------------------------------------------------------------

class TestExcelExport:
    def test_excel_file_readable(self, tmp_path):
        df = pd.DataFrame({"A": [1, 2], "B": ["x", "y"]})
        with override_settings(export_dir=str(tmp_path)):
            path = export_excel(df)
        result = pd.read_excel(path)
        assert list(result.columns) == ["A", "B"]
        assert len(result) == 2

    def test_export_dir_created_automatically(self, tmp_path):
        df   = pd.DataFrame({"X": [1]})
        new_dir = str(tmp_path / "deep" / "nested")
        with override_settings(export_dir=new_dir):
            path = export_excel(df)
        assert Path(path).exists()
