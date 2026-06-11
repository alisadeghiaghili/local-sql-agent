"""TDD tests for llm/ollama_backend.py.

Contracts
---------
- Returns raw text from Ollama response JSON.
- Raises ValueError("OUT_OF_SCOPE") when model returns the sentinel.
- Retries on connection/HTTP errors up to _RETRIES times.
- Raises RuntimeError after all retries exhausted.
- Raises ValueError on Timeout (ModelTimeoutError is mapped upstream).
- name property returns 'ollama:<model>'.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from llm.ollama_backend import OllamaBackend


@pytest.fixture()
def backend():
    return OllamaBackend(model="test-model", url="http://fake/api/generate", retries=3)


def _mock_response(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"response": text}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


class TestOllamaBackendName:
    def test_name_includes_model(self, backend):
        assert backend.name == "ollama:test-model"


class TestOllamaBackendGenerate:
    def test_returns_raw_text(self, backend):
        with patch("requests.post", return_value=_mock_response("SELECT 1")):
            assert backend.generate("prompt") == "SELECT 1"

    def test_strips_whitespace(self, backend):
        with patch("requests.post", return_value=_mock_response("  SELECT 1  \n")):
            assert backend.generate("prompt") == "SELECT 1"

    def test_out_of_scope_raises_value_error(self, backend):
        with patch("requests.post", return_value=_mock_response("OUT_OF_SCOPE")):
            with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
                backend.generate("prompt")

    def test_out_of_scope_case_insensitive(self, backend):
        with patch("requests.post", return_value=_mock_response("out_of_scope")):
            with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
                backend.generate("prompt")

    def test_retries_on_connection_error(self, backend):
        ok = _mock_response("SELECT 1")
        with patch(
            "requests.post",
            side_effect=[requests.ConnectionError(), requests.ConnectionError(), ok],
        ):
            with patch("time.sleep"):  # don't actually wait
                result = backend.generate("prompt")
        assert result == "SELECT 1"

    def test_raises_runtime_error_after_all_retries(self, backend):
        with patch(
            "requests.post",
            side_effect=requests.ConnectionError("unreachable"),
        ):
            with patch("time.sleep"):
                with pytest.raises(RuntimeError, match="unreachable"):
                    backend.generate("prompt")

    def test_timeout_raises_requests_timeout(self, backend):
        """Timeout is NOT caught by the retry loop — it propagates.
        runner.py maps it to ModelTimeoutError."""
        with patch("requests.post", side_effect=requests.Timeout()):
            with pytest.raises(requests.Timeout):
                backend.generate("prompt")
