# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for app.py.

Strategy
--------
app.py is a REPL — its surface area splits cleanly into three layers:

1. **Pure helpers** (_make_log, _print_sql, _print_results)
   Tested directly with no mocking overhead.

2. **Side-effectful helpers** (_enforce_rate_limit, _load_system_prompt)
   Tested by patching ``time.monotonic``, ``time.sleep``, and filesystem.

3. **main() REPL loop**
   Tested by feeding a controlled sequence of lines via ``builtins.input``
   and mocking all I/O (generate_sql, execute_sql, export_excel, save_log).
   stdout is captured with ``capsys``.
"""

from __future__ import annotations

import time
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

import app
import config as cfg
from config import override_settings
from logs.query_log import QueryLog


# ===========================================================================
# _make_log
# ===========================================================================

class TestMakeLog:
    def test_returns_query_log_instance(self):
        log = app._make_log("q", "SELECT 1", "SUCCESS")
        assert isinstance(log, QueryLog)

    def test_fields_populated_correctly(self):
        log = app._make_log(
            "my question", "SELECT TOP 5 * FROM t", "SUCCESS",
            error=None, excel_file="/tmp/x.xlsx", row_count=5, elapsed=1.2345,
        )
        assert log.question              == "my question"
        assert log.generated_sql         == "SELECT TOP 5 * FROM t"
        assert log.status                == "SUCCESS"
        assert log.excel_file            == "/tmp/x.xlsx"
        assert log.row_count             == 5
        assert log.execution_time_seconds == 1.235   # rounded half-up to 3 dp
        assert log.error_message         is None

    def test_elapsed_rounded_to_3dp(self):
        log = app._make_log("q", "", "ERROR", elapsed=1.23456789)
        assert log.execution_time_seconds == 1.235

    def test_model_name_comes_from_cfg(self):
        with override_settings(openai_model="test-model-x"):
            log = app._make_log("q", "", "SUCCESS")
        assert log.model_name == "test-model-x"

    def test_error_status_fields(self):
        log = app._make_log("q", "bad sql", "ERROR", error="boom")
        assert log.status        == "ERROR"
        assert log.error_message == "boom"

    def test_out_of_scope_status(self):
        log = app._make_log("q", "", "OUT_OF_SCOPE", error="OUT_OF_SCOPE")
        assert log.status == "OUT_OF_SCOPE"

    def test_timestamp_is_recent(self):
        before = datetime.now()
        log = app._make_log("q", "", "SUCCESS")
        after  = datetime.now()
        assert before <= log.timestamp <= after

    def test_defaults_row_count_zero(self):
        log = app._make_log("q", "", "SUCCESS")
        assert log.row_count == 0

    def test_defaults_excel_file_none(self):
        log = app._make_log("q", "", "SUCCESS")
        assert log.excel_file is None


# ===========================================================================
# _print_sql
# ===========================================================================

class TestPrintSql:
    def test_prints_sql(self, capsys):
        app._print_sql("SELECT TOP 5 Name FROM [dbo].[T]")
        out = capsys.readouterr().out
        assert "SELECT TOP 5 Name FROM [dbo].[T]" in out

    def test_prints_separator_and_header(self, capsys):
        app._print_sql("SELECT 1")
        out = capsys.readouterr().out
        assert "GENERATED SQL" in out
        assert "=" * 10 in out   # separator present


# ===========================================================================
# _print_results
# ===========================================================================

class TestPrintResults:
    def test_empty_dataframe(self, capsys):
        app._print_results(pd.DataFrame())
        assert "No data found" in capsys.readouterr().out

    def test_small_dataframe_shows_all_rows(self, capsys):
        df = pd.DataFrame({"Name": ["Alice", "Bob"], "Score": [10, 20]})
        app._print_results(df)
        out = capsys.readouterr().out
        assert "Alice" in out
        assert "Bob"   in out
        assert "Total rows returned: 2" in out

    def test_large_dataframe_truncates_at_20(self, capsys):
        df = pd.DataFrame({"x": range(35)})
        app._print_results(df)
        out = capsys.readouterr().out
        assert "15 more rows not shown" in out
        assert "Total rows returned: 35" in out

    def test_exactly_20_rows_no_truncation_message(self, capsys):
        df = pd.DataFrame({"x": range(20)})
        app._print_results(df)
        out = capsys.readouterr().out
        assert "more rows not shown" not in out
        assert "Total rows returned: 20" in out

    def test_single_column(self, capsys):
        df = pd.DataFrame({"Val": [42]})
        app._print_results(df)
        assert "42" in capsys.readouterr().out

    def test_header_always_printed(self, capsys):
        app._print_results(pd.DataFrame({"a": [1]}))
        assert "QUERY RESULT" in capsys.readouterr().out


# ===========================================================================
# _enforce_rate_limit
# ===========================================================================

class TestEnforceRateLimit:
    def _reset(self):
        """Reset module-level timer so tests are independent."""
        app._last_query_time = 0.0

    def test_no_wait_on_first_call(self):
        self._reset()
        with patch("app.time.sleep") as mock_sleep, \
             patch("app.time.monotonic", return_value=1_000.0):
            app._enforce_rate_limit()
        mock_sleep.assert_not_called()

    def test_waits_when_called_too_quickly(self, capsys):
        """Simulate two calls 0.5 s apart — expect sleep(≈1.5)."""
        self._reset()
        with patch("app.time.monotonic", return_value=1_000.0), \
             patch("app.time.sleep"):
            app._enforce_rate_limit()

        sleep_calls: list[float] = []
        with patch("app.time.monotonic", return_value=1_000.5), \
             patch("app.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            app._enforce_rate_limit()

        assert len(sleep_calls) == 1
        assert abs(sleep_calls[0] - 1.5) < 0.01

    def test_no_wait_after_sufficient_time(self):
        self._reset()
        with patch("app.time.monotonic", return_value=1_000.0), \
             patch("app.time.sleep"):
            app._enforce_rate_limit()

        with patch("app.time.monotonic", return_value=1_003.0), \
             patch("app.time.sleep") as mock_sleep:
            app._enforce_rate_limit()

        mock_sleep.assert_not_called()

    def test_prints_wait_message_when_throttled(self, capsys):
        self._reset()
        with patch("app.time.monotonic", return_value=1_000.0), \
             patch("app.time.sleep"):
            app._enforce_rate_limit()

        with patch("app.time.monotonic", return_value=1_000.5), \
             patch("app.time.sleep"):
            app._enforce_rate_limit()

        assert "Please wait" in capsys.readouterr().out

    def test_updates_last_query_time(self):
        self._reset()
        with patch("app.time.monotonic", return_value=5_000.0), \
             patch("app.time.sleep"):
            app._enforce_rate_limit()
        assert app._last_query_time == 5_000.0


# ===========================================================================
# _load_system_prompt
# ===========================================================================

class TestPromptPathIsCwdIndependent:
    """_PROMPT_PATH used to be Path("prompts/system_prompt.md") — relative
    to whatever the current working directory happened to be at read
    time, not to this module's own location. Running `python app.py`
    (or importing app.py) from any directory other than the repo root
    silently broke it (item 14)."""

    def test_prompt_path_is_absolute(self):
        assert app._PROMPT_PATH.is_absolute()

    def test_prompt_path_resolves_regardless_of_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert app._PROMPT_PATH.exists()


class TestLoadSystemPrompt:
    def test_returns_file_contents(self, tmp_path):
        prompt_file = tmp_path / "system_prompt.md"
        prompt_file.write_text("You are a SQL agent.", encoding="utf-8")
        with patch.object(app, "_PROMPT_PATH", prompt_file):
            result = app._load_system_prompt()
        assert result == "You are a SQL agent."

    def test_exits_when_file_missing(self, tmp_path):
        missing = tmp_path / "no_such_file.md"
        with patch.object(app, "_PROMPT_PATH", missing), \
             pytest.raises(SystemExit):
            app._load_system_prompt()


# ===========================================================================
# main() REPL — controlled via mocked input()
# ===========================================================================

class TestMainRepl:
    """Feed synthetic input lines and assert on stdout + side-effects."""

    @pytest.fixture(autouse=True)
    def _valid_settings(self):
        """main() now validates config at startup (item 2) — give every
        test in this class a non-placeholder configuration so that check
        passes and the REPL behaviour under test is reached."""
        with override_settings(
            openai_model="test-model",
            db_connection_url="mssql+pyodbc://test-host:1433/TestDB"
            "?driver=ODBC+Driver+17+for+SQL+Server",
        ):
            yield

    def _run_main(
        self,
        input_lines: list[str],
        *,
        sql_response: str = "SELECT TOP 5 * FROM [Auction_Dim].[Customer]",
        df_rows: list[tuple] = None,
        df_columns: list[str] = None,
        generate_side_effect=None,
    ):
        """Run main() with mocked I/O and return (stdout, mock_save)."""
        df_rows    = df_rows    or [("Alice",)]
        df_columns = df_columns or ["Name"]
        mock_df    = pd.DataFrame(df_rows, columns=df_columns)

        # Each question needs 2 perf_counter calls (start + end).
        # Count non-exit lines that will actually run a query.
        n_questions = sum(
            1 for l in input_lines
            if l and l.lower() not in ("exit", "quit")
        )
        perf_values = []
        for _ in range(n_questions):
            perf_values += [0.0, 1.5]

        with patch("builtins.input", side_effect=iter(input_lines)), \
             patch.object(app, "_load_system_prompt", return_value="SYS"), \
             patch.object(app, "_enforce_rate_limit"), \
             patch("app.generate_sql",
                   side_effect=generate_side_effect or (lambda q, p: sql_response)), \
             patch("app.validate_sql"), \
             patch("app.execute_sql",   return_value=mock_df), \
             patch("app.export_excel",  return_value="/tmp/result.xlsx"), \
             patch("app.save_log") as mock_save, \
             patch("app.time.perf_counter", side_effect=perf_values):
            import sys
            captured = StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                app.main()
            finally:
                sys.stdout = old_stdout

        return captured.getvalue(), mock_save

    # -----------------------------------------------------------------------
    # exit / quit / empty input
    # -----------------------------------------------------------------------

    def test_exit_command_terminates(self, capsys):
        with patch("builtins.input", side_effect=["exit"]), \
             patch.object(app, "_load_system_prompt", return_value="SYS"), \
             patch.object(app, "_enforce_rate_limit"):
            app.main()
        assert "Bye" in capsys.readouterr().out

    def test_quit_command_terminates(self, capsys):
        with patch("builtins.input", side_effect=["quit"]), \
             patch.object(app, "_load_system_prompt", return_value="SYS"), \
             patch.object(app, "_enforce_rate_limit"):
            app.main()
        assert "Bye" in capsys.readouterr().out

    def test_empty_input_terminates(self, capsys):
        with patch("builtins.input", side_effect=[""]), \
             patch.object(app, "_load_system_prompt", return_value="SYS"), \
             patch.object(app, "_enforce_rate_limit"):
            app.main()
        assert "Bye" in capsys.readouterr().out

    def test_eof_terminates_gracefully(self, capsys):
        with patch("builtins.input", side_effect=EOFError), \
             patch.object(app, "_load_system_prompt", return_value="SYS"):
            app.main()
        assert "Bye" in capsys.readouterr().out

    def test_keyboard_interrupt_terminates(self, capsys):
        with patch("builtins.input", side_effect=KeyboardInterrupt), \
             patch.object(app, "_load_system_prompt", return_value="SYS"):
            app.main()
        assert "Bye" in capsys.readouterr().out

    # -----------------------------------------------------------------------
    # Successful query
    # -----------------------------------------------------------------------

    def test_success_prints_sql_and_results(self):
        out, _ = self._run_main(["top 5 customers", "exit"])
        assert "GENERATED SQL"  in out
        assert "QUERY RESULT"   in out
        assert "Alice"          in out

    def test_success_saves_log_with_correct_status(self):
        _, mock_save = self._run_main(["top 5 customers", "exit"])
        mock_save.assert_called_once()
        log: QueryLog = mock_save.call_args[0][0]
        assert log.status   == "SUCCESS"
        assert log.question == "top 5 customers"

    def test_success_prints_excel_path(self):
        out, _ = self._run_main(["top 5 customers", "exit"])
        assert "/tmp/result.xlsx" in out

    def test_success_prints_elapsed_time(self):
        out, _ = self._run_main(["top 5 customers", "exit"])
        assert "Elapsed" in out

    # -----------------------------------------------------------------------
    # OUT_OF_SCOPE
    # -----------------------------------------------------------------------

    def test_out_of_scope_prints_warning(self):
        def _oos(q, p): raise ValueError("OUT_OF_SCOPE")
        out, _ = self._run_main(["delete all", "exit"], generate_side_effect=_oos)
        assert "only answers Auction" in out

    def test_out_of_scope_logged_correctly(self):
        def _oos(q, p): raise ValueError("OUT_OF_SCOPE")
        _, mock_save = self._run_main(["delete all", "exit"], generate_side_effect=_oos)
        log: QueryLog = mock_save.call_args[0][0]
        assert log.status == "OUT_OF_SCOPE"

    # -----------------------------------------------------------------------
    # Validation error (ValueError other than OUT_OF_SCOPE)
    # -----------------------------------------------------------------------

    def test_validation_error_prints_message(self):
        def _bad(q, p): raise ValueError("Forbidden keyword: DELETE")
        out, _ = self._run_main(["bad q", "exit"], generate_side_effect=_bad)
        assert "Validation error" in out
        assert "Forbidden keyword" in out

    def test_validation_error_logged_as_error(self):
        def _bad(q, p): raise ValueError("Forbidden keyword: DELETE")
        _, mock_save = self._run_main(["bad q", "exit"], generate_side_effect=_bad)
        log: QueryLog = mock_save.call_args[0][0]
        assert log.status == "ERROR"

    # -----------------------------------------------------------------------
    # RuntimeError (e.g. DB down)
    # -----------------------------------------------------------------------

    def test_runtime_error_prints_message(self):
        def _rt(q, p): raise RuntimeError("Database error: timeout")
        out, _ = self._run_main(["some q", "exit"], generate_side_effect=_rt)
        assert "Runtime error" in out
        assert "timeout"        in out

    def test_runtime_error_logged_as_error(self):
        def _rt(q, p): raise RuntimeError("DB down")
        _, mock_save = self._run_main(["some q", "exit"], generate_side_effect=_rt)
        log: QueryLog = mock_save.call_args[0][0]
        assert log.status == "ERROR"

    # -----------------------------------------------------------------------
    # Unexpected exception
    # -----------------------------------------------------------------------

    def test_unexpected_exception_prints_message(self):
        def _wild(q, p): raise MemoryError("OOM")
        out, _ = self._run_main(["q", "exit"], generate_side_effect=_wild)
        assert "Unexpected error" in out

    def test_unexpected_exception_logged_as_error(self):
        def _wild(q, p): raise MemoryError("OOM")
        _, mock_save = self._run_main(["q", "exit"], generate_side_effect=_wild)
        log: QueryLog = mock_save.call_args[0][0]
        assert log.status == "ERROR"

    # -----------------------------------------------------------------------
    # Multiple questions in one session
    # -----------------------------------------------------------------------

    def test_multiple_questions_each_logged(self):
        _, mock_save = self._run_main(
            ["q1", "q2", "exit"],
            generate_side_effect=None,
        )
        assert mock_save.call_count == 2

    def test_second_question_after_first_succeeds(self):
        with patch("builtins.input", side_effect=["q1", "q2", "exit"]), \
             patch.object(app, "_load_system_prompt", return_value="SYS"), \
             patch.object(app, "_enforce_rate_limit"), \
             patch("app.generate_sql",
                   return_value="SELECT TOP 1 * FROM [Auction_Dim].[Customer]"), \
             patch("app.validate_sql"), \
             patch("app.execute_sql",
                   return_value=pd.DataFrame({"Name": ["X"]})), \
             patch("app.export_excel", return_value="/x.xlsx"), \
             patch("app.save_log") as mock_save, \
             patch("app.time.perf_counter",
                   side_effect=[0.0, 1.0, 0.0, 1.0]):
            import sys
            buf = StringIO()
            sys.stdout, old = buf, sys.stdout
            try:
                app.main()
            finally:
                sys.stdout = old
        assert mock_save.call_count == 2
        logs = [mock_save.call_args_list[i][0][0] for i in range(2)]
        assert logs[0].question == "q1"
        assert logs[1].question == "q2"

    # -----------------------------------------------------------------------
    # Banner / header
    # -----------------------------------------------------------------------

    def test_banner_shows_model_name(self, capsys):
        with override_settings(openai_model="test-banner-model"), \
             patch("builtins.input", side_effect=["exit"]), \
             patch.object(app, "_load_system_prompt", return_value="SYS"), \
             patch.object(app, "_enforce_rate_limit"):
            app.main()
        assert "test-banner-model" in capsys.readouterr().out


# ===========================================================================
# main() fails fast on invalid configuration (item 2)
# ===========================================================================

class TestMainValidatesConfigAtStartup:
    """cfg.settings.validate() was previously only ever called from tests
    — running `python app.py` on an unfilled .env silently used the
    hardcoded placeholder DB_CONNECTION_URL instead of failing fast."""

    def test_exits_before_loading_prompt_when_db_url_is_placeholder(self, capsys):
        with override_settings(
            openai_model="llama3",
            db_connection_url=(
                "mssql+pyodbc://username@server:1433/Auction_DM"
                "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
            ),
        ), patch.object(app, "_load_system_prompt", return_value="SYS") as mock_load_prompt, \
             patch("builtins.input", side_effect=["exit"]):
            with pytest.raises(SystemExit):
                app.main()

        mock_load_prompt.assert_not_called()
        assert "Configuration error" in capsys.readouterr().out
