"""Integration tests for llm.wizard_llm.generate_sql.

Patch target: llm.wizard_llm.requests.post  (the module that actually uses
requests).  ``generate_sql`` wires retrieval → prompt → LLM → clean → validate.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from llm.wizard_llm import generate_sql

_PATCH_POST = "llm.wizard_llm.requests.post"
_PATCH_SLEEP = "llm.wizard_llm.time.sleep"


def _openai_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    resp.raise_for_status = MagicMock()
    return resp


class TestGenerateSql:
    def test_returns_cleaned_sql(self):
        raw = "Sure! Here is the SQL:\n```sql\nSELECT TOP 10 * FROM [Auction_Dim].[Customer]\n```"
        with patch(_PATCH_POST, return_value=_openai_response(raw)), patch(_PATCH_SLEEP):
            sql = generate_sql("How many customers?", "You are a T-SQL expert.")
        assert sql.startswith("SELECT TOP 10")

    def test_converts_limit_to_top(self):
        with patch(_PATCH_POST, return_value=_openai_response("SELECT * FROM t LIMIT 5")), patch(_PATCH_SLEEP):
            sql = generate_sql("q", "sys")
        assert "TOP 5" in sql and "LIMIT" not in sql

    def test_out_of_scope_raises(self):
        with patch(_PATCH_POST, return_value=_openai_response("OUT_OF_SCOPE")), patch(_PATCH_SLEEP):
            with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
                generate_sql("Who is the president?", "sys")
