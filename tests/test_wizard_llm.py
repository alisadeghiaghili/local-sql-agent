"""Tests for llm/wizard_llm.py — openai + mock only.

The Ollama-specific and Anthropic transports are gone (see
``llm/providers.py``'s module docstring); this file covers the two
remaining providers plus ``build_backend``/``generate_sql``, the small
non-router helpers ``app.py``'s REPL uses directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import config as cfg
from llm.providers import MockBackend, OpenAIBackend
from llm.wizard_llm import WizardLLM, build_backend


def _mock_response(json_body: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    return resp


class TestBuildBackend:
    def test_builds_openai_backend_from_settings(self):
        with cfg.override_settings(openai_api_key="sk-fake", openai_base_url="http://localhost:8000/v1"):
            backend = build_backend(model="m")
        assert isinstance(backend, OpenAIBackend)
        assert backend.name == "openai:m"
        assert backend.endpoint == "http://localhost:8000/v1"

    def test_explicit_base_url_overrides_settings(self):
        with cfg.override_settings(openai_api_key="sk-fake"):
            backend = build_backend(model="m", base_url="http://other:9000/v1")
        assert backend.endpoint == "http://other:9000/v1"


class TestBackendSelection:
    def test_mock_provider_builds_mock_backend(self):
        llm = WizardLLM(provider="mock", model="mock")
        assert isinstance(llm._backend, MockBackend)

    def test_openai_provider_builds_openai_backend(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm = WizardLLM(provider="openai", model="gpt-4o-mini")
        assert isinstance(llm._backend, OpenAIBackend)
        assert llm._backend.name == "openai:gpt-4o-mini"

    def test_openai_provider_without_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            WizardLLM(provider="openai", model="gpt-4o-mini")

    def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported provider"):
            WizardLLM(provider="ollama", model="x")


class TestGenerate:
    def test_mock_generate_returns_stub_json(self):
        llm = WizardLLM(provider="mock", model="mock")
        result = llm.generate("anything", expect_json=True)
        assert result == {"aliases": [], "description": "", "rules": {"rule_text": ""}, "examples": []}

    def test_expect_json_false_returns_raw_text(self):
        llm = WizardLLM(provider="mock", model="mock")
        raw = llm.generate("anything", expect_json=False)
        assert isinstance(raw, str)
        assert "aliases" in raw

    def test_openai_generate_parses_json(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm = WizardLLM(provider="openai", model="m", base_url="http://fake/v1")
        body = {"choices": [{"message": {"content": '{"x": 1}'}}]}
        with patch("requests.post", return_value=_mock_response(body)):
            assert llm.generate("prompt") == {"x": 1}

    def test_retries_once_on_parse_failure_then_succeeds(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm = WizardLLM(provider="openai", model="m", base_url="http://fake/v1")
        first = _mock_response({"choices": [{"message": {"content": "not json at all"}}]})
        second = _mock_response({"choices": [{"message": {"content": '{"x": 2}'}}]})
        with patch("requests.post", side_effect=[first, second]):
            assert llm.generate("prompt") == {"x": 2}

    def test_raises_after_retry_also_fails(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm = WizardLLM(provider="openai", model="m", base_url="http://fake/v1")
        bad = _mock_response({"choices": [{"message": {"content": "still not json"}}]})
        with patch("requests.post", return_value=bad):
            with pytest.raises(ValueError, match=r"\[WizardLLM\]"):
                llm.generate("prompt")


class TestTestConnection:
    def test_mock_always_true(self):
        assert WizardLLM(provider="mock", model="mock").test_connection() is True

    def test_openai_delegates_to_backend(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm = WizardLLM(provider="openai", model="m", base_url="http://fake/v1")
        with patch("requests.get", return_value=_mock_response({}, status=200)):
            assert llm.test_connection() is True


class TestFromConfig:
    def test_defaults_to_openai(self, monkeypatch):
        monkeypatch.delenv("WIZARD_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        llm = WizardLLM.from_config()
        assert llm.provider == "openai"
