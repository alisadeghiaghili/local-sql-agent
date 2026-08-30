"""Integration-level tests for the full pipeline.

All external I/O (LLM HTTP, SQL Server) is replaced by mocks.
Patch target: llm.providers.requests  (the module that actually uses
requests, not the shim llm.wizard_llm.generate_sql).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests as requests_lib

_PATCH_POST  = "llm.providers.requests.post"
_PATCH_SLEEP = "llm.providers.time.sleep"


def _mock_response(text: str, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = {"choices": [{"message": {"content": text}}]}
    m.raise_for_status = MagicMock()
    return m


SIMPLE_SQL  = "SELECT TOP 10 * FROM [Auction_Dim].[Customer]"
SIMPLE_DF   = pd.DataFrame({"Id": [1, 2], "Name": ["علی", "سارا"]})
SYSTEM_PROMPT = "You are an SQL generator."


# ---------------------------------------------------------------------------
# Fixture: fake DB executor
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_execute(monkeypatch):
    """Replace database.executor.execute_query with a stub."""
    def _execute(sql: str, **kw):
        return SIMPLE_DF.copy()
    monkeypatch.setattr("database.executor.execute_query", _execute)
    return _execute


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_full_pipeline_success(self, fake_execute):
        from llm.wizard_llm import generate_sql
        with patch(_PATCH_POST, return_value=_mock_response(SIMPLE_SQL)):
            sql = generate_sql("لیست مشتریان", SYSTEM_PROMPT)
        assert "Customer" in sql

    def test_pipeline_returns_string_sql(self, fake_execute):
        from llm.wizard_llm import generate_sql
        with patch(_PATCH_POST, return_value=_mock_response(SIMPLE_SQL)):
            result = generate_sql("سوال", SYSTEM_PROMPT)
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Out-of-scope
# ---------------------------------------------------------------------------

class TestOutOfScope:
    def test_out_of_scope_raises_value_error(self, fake_execute):
        from llm.wizard_llm import generate_sql
        with patch(_PATCH_POST, return_value=_mock_response("OUT_OF_SCOPE")):
            with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
                generate_sql("سوال بی ربط", SYSTEM_PROMPT)


# ---------------------------------------------------------------------------
# Endpoint unreachable
# ---------------------------------------------------------------------------

class TestEndpointUnreachable:
    def test_raises_runtime_error_after_retries(self, fake_execute):
        from llm.wizard_llm import generate_sql
        with patch(_PATCH_POST, side_effect=requests_lib.ConnectionError("refused")), \
             patch(_PATCH_SLEEP):
            with pytest.raises(RuntimeError, match="unreachable"):
                generate_sql("سوال", SYSTEM_PROMPT)


# ---------------------------------------------------------------------------
# Log file parsing (kept from original suite — no mocks needed)
# ---------------------------------------------------------------------------

class TestLogParsing:
    def test_reads_valid_json_log(self, tmp_path):
        log_file = tmp_path / "test.jsonl"
        entries = [
            {"status": "SUCCESS", "question": "سوال", "generated_sql": SIMPLE_SQL},
            {"status": "FAILED",  "question": "دیگر",   "generated_sql": ""},
        ]
        log_file.write_text("\n".join(json.dumps(e) for e in entries))

        from core.analyze_misses import analyse
        # analyse reads a Path object
        result = analyse(log_file)
        assert isinstance(result, list)

    def test_empty_log_returns_empty_list(self, tmp_path):
        log_file = tmp_path / "empty.jsonl"
        log_file.write_text("")
        from core.analyze_misses import analyse
        result = analyse(log_file)
        assert result == []
