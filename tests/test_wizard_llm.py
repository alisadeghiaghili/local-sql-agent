"""TDD tests for llm/wizard_llm.py — auto provider selection + build_backend.

Contracts
---------
select_provider()
  - Prefers the `prefer` provider when it is reachable.
  - Falls back to the fastest reachable provider otherwise.
  - Raises RuntimeError when no candidate is reachable.
  - Skips unconfigured providers (e.g. openai without an API key).

build_backend()
  - "ollama" → OllamaBackend
  - "openai" → OpenAIBackend
  - "mock"   → MockBackend
  - "auto"   → resolves via select_provider then maps to a backend
  - unknown  → ValueError

OpenAIBackend.generate()
  - Returns raw text from chat-completions response.
  - Raises ValueError("OUT_OF_SCOPE") for the sentinel.
  - Sends the Bearer header when an API key is configured.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

import llm.wizard_llm as wz


# ---------------------------------------------------------------------------
# select_provider
# ---------------------------------------------------------------------------

class TestSelectProvider:
    """Config is mocked so probe results are deterministic and independent of
    the real .env / os.environ state."""

    @staticmethod
    def _fake_cfg() -> MagicMock:
        cfg = MagicMock()
        cfg.settings.ollama_model = "llama3"
        cfg.settings.ollama_url = "http://localhost:11434/api/generate"
        cfg.settings.openai_api_key = "sk-test"
        cfg.settings.openai_model = "gpt-4o-mini"
        cfg.settings.openai_base_url = "http://vllm:8000/v1"
        cfg.settings.anthropic_api_key = ""
        cfg.settings.anthropic_model = "claude-3-5-sonnet-20241022"
        return cfg

    def test_prefers_requested_provider_when_reachable(self):
        with patch.object(wz, "_PROBE_CANDIDATES", ("ollama", "openai")):
            with patch.object(
                wz, "_OllamaProvider", spec=True
            ) as ollama_cls, patch.object(
                wz, "_OpenAIProvider", spec=True
            ) as openai_cls:
                ollama_cls.return_value.test_connection.return_value = True
                openai_cls.return_value.test_connection.return_value = True
                with patch.object(wz, "_config_or_none", return_value=self._fake_cfg()):
                    assert wz.select_provider(prefer="ollama") == "ollama"

    def test_falls_back_when_prefer_unreachable(self):
        with patch.object(wz, "_PROBE_CANDIDATES", ("ollama", "openai")):
            with patch.object(
                wz, "_OllamaProvider", spec=True
            ) as ollama_cls, patch.object(
                wz, "_OpenAIProvider", spec=True
            ) as openai_cls:
                ollama_cls.return_value.test_connection.return_value = False
                openai_cls.return_value.test_connection.return_value = True
                with patch.object(wz, "_config_or_none", return_value=self._fake_cfg()):
                    with patch.object(
                        wz.time, "perf_counter", side_effect=[0.0, 0.1, 0.0, 0.05]
                    ):
                        assert wz.select_provider(prefer="ollama") == "openai"

    def test_skips_unconfigured_openai(self):
        """OpenAI without an API key must not be selected."""
        cfg = self._fake_cfg()
        cfg.settings.openai_api_key = ""
        with patch.object(wz, "_PROBE_CANDIDATES", ("ollama", "openai")):
            with patch.object(
                wz, "_OllamaProvider", spec=True
            ) as ollama_cls, patch.object(
                wz, "_OpenAIProvider", spec=True
            ) as openai_cls:
                ollama_cls.return_value.test_connection.return_value = False
                openai_cls.return_value.test_connection.return_value = True
                with patch.object(wz, "_config_or_none", return_value=cfg):
                    with pytest.raises(RuntimeError, match="No LLM provider"):
                        wz.select_provider(prefer="ollama")

    def test_raises_when_nothing_reachable(self):
        with patch.object(wz, "_PROBE_CANDIDATES", ("ollama", "openai")):
            with patch.object(
                wz, "_OllamaProvider", spec=True
            ) as ollama_cls, patch.object(
                wz, "_OpenAIProvider", spec=True
            ) as openai_cls:
                ollama_cls.return_value.test_connection.return_value = False
                openai_cls.return_value.test_connection.return_value = False
                with patch.object(wz, "_config_or_none", return_value=self._fake_cfg()):
                    with pytest.raises(RuntimeError, match="No LLM provider"):
                        wz.select_provider(prefer="ollama")


# ---------------------------------------------------------------------------
# build_backend
# ---------------------------------------------------------------------------

class TestBuildBackend:
    def test_ollama_maps_to_ollama_backend(self):
        with patch.object(wz, "_config_or_none", return_value=None):
            from llm.ollama_backend import OllamaBackend
            assert isinstance(wz.build_backend("ollama"), OllamaBackend)

    def test_openai_maps_to_openai_backend(self):
        with patch.object(wz, "_config_or_none", return_value=None):
            assert isinstance(wz.build_backend("openai"), wz.OpenAIBackend)

    def test_mock_maps_to_mock_backend(self):
        with patch.object(wz, "_config_or_none", return_value=None):
            assert isinstance(wz.build_backend("mock"), wz.MockBackend)

    def test_auto_resolves_then_maps(self):
        with patch.object(wz, "select_provider", return_value="openai"):
            with patch.object(wz, "_config_or_none", return_value=None):
                assert isinstance(wz.build_backend("auto"), wz.OpenAIBackend)

    def test_unknown_provider_raises(self):
        with patch.object(wz, "_config_or_none", return_value=None):
            with pytest.raises(ValueError, match="Unsupported LLM provider"):
                wz.build_backend("grok")


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
