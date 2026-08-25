"""TDD tests for llm/wizard_llm.py — build_backend + OpenAIBackend + WizardLLM.

Provider surface is intentionally small: ``openai`` (OpenAI-compatible
endpoint) and ``mock`` only.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

import llm.wizard_llm as wz
from llm.wizard_llm import WizardLLM


class TestJsonHelpers:
    def test_strip_fences(self):
        assert wz._strip_fences("```json\n{\"a\": 1}\n```") == '{"a": 1}'

    def test_find_json_substring(self):
        assert wz._find_json_substring("prefix {\"a\": 1}") == '{"a": 1}'

    def test_find_json_substring_missing(self):
        with pytest.raises(ValueError, match="No JSON"):
            wz._find_json_substring("no json here")

    def test_parse_json(self):
        assert wz._parse_json("```json\n{\"x\": [1, 2]}\n```") == {"x": [1, 2]}


# ---------------------------------------------------------------------------
# build_backend
# ---------------------------------------------------------------------------

class TestBuildBackend:
    def test_default_maps_to_openai_backend(self):
        assert isinstance(wz.build_backend(), wz.OpenAIBackend)

    def test_openai_maps_to_openai_backend(self):
        assert isinstance(wz.build_backend("openai"), wz.OpenAIBackend)

    def test_auto_maps_to_openai_backend(self):
        assert isinstance(wz.build_backend("auto"), wz.OpenAIBackend)

    def test_mock_maps_to_mock_backend(self):
        assert isinstance(wz.build_backend("mock"), wz.MockBackend)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported LLM provider"):
            wz.build_backend("ollama")


# ---------------------------------------------------------------------------
# OpenAIBackend
# ---------------------------------------------------------------------------

def _mock_response(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


class TestOpenAIBackend:
    def _backend(self, **kwargs):
        defaults = dict(
            model="test-model",
            url="http://fake/v1",
            api_key="sk-test",
            retries=1,
        )
        defaults.update(kwargs)
        return wz.OpenAIBackend(**defaults)

    def test_name_includes_model(self):
        assert self._backend().name == "openai:test-model"

    def test_returns_raw_text(self):
        with patch("requests.post", return_value=_mock_response("SELECT 1")) as post:
            assert self._backend().generate("prompt") == "SELECT 1"

    def test_sends_bearer_header_and_chat_payload(self):
        with patch("requests.post", return_value=_mock_response("ok")) as post:
            self._backend().generate("hello")
        args, kwargs = post.call_args
        assert args[0] == "http://fake/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
        assert kwargs["json"]["model"] == "test-model"
        assert kwargs["json"]["messages"] == [{"role": "user", "content": "hello"}]

    def test_out_of_scope_raises_value_error(self):
        with patch("requests.post", return_value=_mock_response("OUT_OF_SCOPE")):
            with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
                self._backend().generate("prompt")

    def test_timeout_propagates(self):
        with patch("requests.post", side_effect=requests.Timeout()):
            with pytest.raises(requests.Timeout):
                self._backend().generate("prompt")

    def test_raises_runtime_error_after_retries(self):
        with patch("requests.post", side_effect=requests.ConnectionError("down")):
            with patch("time.sleep"):
                with pytest.raises(RuntimeError, match="down"):
                    self._backend().generate("prompt")


# ---------------------------------------------------------------------------
# WizardLLM
# ---------------------------------------------------------------------------

class TestWizardLLM:
    def test_auto_resolves_to_openai(self):
        llm = WizardLLM(provider="auto", model="m", base_url="http://fake/v1")
        assert llm.provider == "openai"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported provider"):
            WizardLLM(provider="anthropic")

    def test_mock_provider_works_without_key(self):
        llm = WizardLLM(provider="mock", model="mock")
        assert llm.test_connection() is True

    def test_openai_requires_api_key(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            with patch.object(wz, "_config_or_none", return_value=None):
                with pytest.raises(ValueError, match="OPENAI_API_KEY"):
                    WizardLLM(provider="openai")

    def test_mock_generate_returns_json(self):
        llm = WizardLLM(provider="mock", model="mock")
        result = llm.generate("anything", expect_json=True)
        assert isinstance(result, dict)
