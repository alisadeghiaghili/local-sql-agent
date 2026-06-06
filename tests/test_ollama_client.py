"""Unit tests for llm/ollama_client.py.

All HTTP calls are mocked — no real Ollama needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from llm.ollama_client import generate_sql


def _mock_response(response_text: str, status_code: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = {"response": response_text}
    mock.raise_for_status = MagicMock()
    return mock


SYSTEM_PROMPT = "You are an SQL generator."


class TestGenerateSql:
    def test_returns_clean_sql(self):
        sql = "SELECT TOP 10 Name FROM [Auction_Dim].[Customer]"
        with patch("llm.ollama_client.requests.post") as mock_post:
            mock_post.return_value = _mock_response(sql)
            result = generate_sql("مشتریان", SYSTEM_PROMPT)
        assert "SELECT" in result.upper()

    def test_strips_markdown_fence_from_response(self):
        raw = "```sql\nSELECT TOP 5 Name FROM [Auction_Dim].[Customer]\n```"
        with patch("llm.ollama_client.requests.post") as mock_post:
            mock_post.return_value = _mock_response(raw)
            result = generate_sql("مشتریان", SYSTEM_PROMPT)
        assert result.startswith("SELECT")
        assert "```" not in result

    def test_raises_value_error_on_out_of_scope(self):
        with patch("llm.ollama_client.requests.post") as mock_post:
            mock_post.return_value = _mock_response("OUT_OF_SCOPE")
            with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
                generate_sql("شهر باران ایران کیست", SYSTEM_PROMPT)

    def test_raises_value_error_on_out_of_scope_with_whitespace(self):
        with patch("llm.ollama_client.requests.post") as mock_post:
            mock_post.return_value = _mock_response("  out_of_scope  ".upper())
            with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
                generate_sql("irrelevant question", SYSTEM_PROMPT)

    def test_retries_on_connection_error_then_succeeds(self):
        sql = "SELECT TOP 1 Name FROM [Auction_Dim].[Customer]"
        responses = [
            requests.ConnectionError("refused"),
            _mock_response(sql),
        ]
        with patch("llm.ollama_client.requests.post", side_effect=responses), \
             patch("llm.ollama_client.time.sleep"):
            result = generate_sql("مشتری", SYSTEM_PROMPT)
        assert "SELECT" in result.upper()

    def test_raises_runtime_error_after_all_retries_exhausted(self):
        with patch(
            "llm.ollama_client.requests.post",
            side_effect=requests.ConnectionError("refused"),
        ), patch("llm.ollama_client.time.sleep"):
            with pytest.raises(RuntimeError, match="unreachable"):
                generate_sql("سوال", SYSTEM_PROMPT)

    def test_raises_runtime_error_on_http_error(self):
        mock = MagicMock()
        mock.raise_for_status.side_effect = requests.HTTPError("500")
        with patch("llm.ollama_client.requests.post", return_value=mock), \
             patch("llm.ollama_client.time.sleep"):
            with pytest.raises(RuntimeError, match="unreachable"):
                generate_sql("سوال", SYSTEM_PROMPT)

    def test_prompt_includes_question(self):
        sql = "SELECT 1 AS n"
        captured = {}
        def fake_post(url, json, timeout):
            captured["payload"] = json
            return _mock_response(sql)
        with patch("llm.ollama_client.requests.post", side_effect=fake_post):
            generate_sql("تعداد قراردادها", SYSTEM_PROMPT)
        assert "تعداد قراردادها" in captured["payload"]["prompt"]

    def test_payload_stream_is_false(self):
        captured = {}
        def fake_post(url, json, timeout):
            captured["payload"] = json
            return _mock_response("SELECT 1")
        with patch("llm.ollama_client.requests.post", side_effect=fake_post):
            generate_sql("سوال", SYSTEM_PROMPT)
        assert captured["payload"]["stream"] is False
