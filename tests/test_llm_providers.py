# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for llm/providers.py — the OpenAI-compatible transport + MockBackend.

Every HTTP call is mocked (``requests.post``/``requests.get``); nothing
here opens a real network connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

import config as cfg
from llm.providers import (
    MockBackend,
    OpenAIBackend,
    parse_json_response,
)
from llm.router import PromptSegments


def _mock_response(json_body: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    return resp


class TestParseJsonResponse:
    def test_plain_json(self):
        assert parse_json_response('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}

    def test_trailing_prose_after_json_is_ignored(self):
        assert parse_json_response('here is the result: {"a": 1} done') == {"a": 1}

    def test_json_array(self):
        assert parse_json_response("[1, 2, 3]") == [1, 2, 3]

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="No JSON"):
            parse_json_response("no json here at all")

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError, match="JSON parse error"):
            parse_json_response("{not valid json")


class TestOpenAIBackendBasics:
    def test_name(self):
        backend = OpenAIBackend(model="gpt-4o-mini", api_key="sk-x")
        assert backend.name == "openai:gpt-4o-mini"

    def test_endpoint_is_base_url(self):
        backend = OpenAIBackend(model="m", api_key="k", base_url="http://localhost:8000/v1")
        assert backend.endpoint == "http://localhost:8000/v1"

    def test_generate_with_meta(self):
        backend = OpenAIBackend(model="gpt-4o-mini", api_key="sk-x")
        body = {"choices": [{"message": {"content": " SELECT 1 "}}]}
        with patch("requests.post", return_value=_mock_response(body)) as post:
            text, meta = backend.generate_with_meta("prompt")
        assert text == "SELECT 1"
        assert meta["endpoint_status"] == 200
        assert meta["attempts"] == 1
        assert isinstance(meta["total_ms"], int)
        sent = post.call_args.kwargs["json"]
        assert sent["temperature"] == 0.0
        assert sent["top_p"] == 1.0
        assert sent["seed"] == 7
        assert sent["max_tokens"] == 512
        assert sent["messages"] == [{"role": "user", "content": "prompt"}]

    def test_out_of_scope_raises_value_error_with_llm_meta(self):
        backend = OpenAIBackend(model="m", api_key="k")
        body = {"choices": [{"message": {"content": "OUT_OF_SCOPE"}}]}
        with patch("requests.post", return_value=_mock_response(body)):
            with pytest.raises(ValueError, match="OUT_OF_SCOPE") as exc_info:
                backend.generate("prompt")
        assert exc_info.value.llm_meta["endpoint_status"] == 200

    def test_generate_returns_raw_text(self):
        backend = OpenAIBackend(model="m", api_key="k")
        body = {"choices": [{"message": {"content": "SELECT 1"}}]}
        with patch("requests.post", return_value=_mock_response(body)):
            assert backend.generate("prompt") == "SELECT 1"

    def test_no_bearer_header_when_api_key_empty(self):
        backend = OpenAIBackend(model="m", api_key="")
        body = {"choices": [{"message": {"content": "ok"}}]}
        with patch("requests.post", return_value=_mock_response(body)) as post:
            backend.generate("prompt")
        assert "Authorization" not in post.call_args.kwargs["headers"]

    def test_bearer_header_when_api_key_set(self):
        backend = OpenAIBackend(model="m", api_key="sk-x")
        body = {"choices": [{"message": {"content": "ok"}}]}
        with patch("requests.post", return_value=_mock_response(body)) as post:
            backend.generate("prompt")
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-x"

    def test_timeout_propagates_immediately(self):
        backend = OpenAIBackend(model="m", api_key="k", retries=3)
        with patch("requests.post", side_effect=requests.Timeout()):
            with pytest.raises(requests.Timeout):
                backend.generate("prompt")

    def test_retries_then_raises_runtime_error(self):
        backend = OpenAIBackend(model="m", api_key="k", retries=2)
        with patch("requests.post", side_effect=requests.ConnectionError("refused")):
            with patch("time.sleep"):
                with pytest.raises(RuntimeError, match="unreachable"):
                    backend.generate("prompt")

    def test_retries_then_succeeds(self):
        backend = OpenAIBackend(model="m", api_key="k", retries=3)
        good = _mock_response({"choices": [{"message": {"content": "SELECT 1"}}]})
        with patch("requests.post", side_effect=[requests.ConnectionError("x"), good]):
            with patch("time.sleep"):
                assert backend.generate("prompt") == "SELECT 1"

    def test_generate_structured_sends_response_format(self):
        backend = OpenAIBackend(model="gpt-4o-mini", api_key="sk-x")
        body = {"choices": [{"message": {"content": '{"sql": "SELECT 1", "out_of_scope": false}'}}]}
        segments = PromptSegments(static_prefix="SCHEMA", question="q")
        with patch("requests.post", return_value=_mock_response(body)) as post:
            obj, meta = backend.generate_structured(segments, schema={"type": "object"})
        assert obj == {"sql": "SELECT 1", "out_of_scope": False}
        assert meta["structured_output"] is True
        sent = post.call_args.kwargs["json"]
        assert sent["response_format"]["type"] == "json_schema"
        assert sent["response_format"]["json_schema"]["strict"] is True
        # The router hands the adapter *segments*; the adapter is free to
        # flatten internally, but must receive the object, not a pre-joined
        # string (see llm.router's design point 3).
        assert "SCHEMA" in sent["messages"][0]["content"]

    def test_test_connection_true_on_200(self):
        backend = OpenAIBackend(model="m", api_key="k")
        with patch("requests.get", return_value=_mock_response({}, status=200)):
            assert backend.test_connection() is True

    def test_test_connection_false_on_error(self):
        backend = OpenAIBackend(model="m", api_key="k")
        with patch("requests.get", side_effect=OSError("no network")):
            assert backend.test_connection() is False


class TestOpenAIBackendTrust:
    """Exit criterion 6: a local-looking endpoint is trusted, a hosted one
    is not, by configuration -- see also tests/test_llm_router.py::TestTrust
    for the router-level (is_trusted_backend) side of the same property."""

    def test_default_base_url_is_untrusted(self):
        """The default with no config at all (no base_url override) must
        be untrusted -- fail closed."""
        backend = OpenAIBackend(model="m", api_key="k")
        assert backend.trusted is False

    def test_loopback_base_url_is_trusted_by_default(self):
        backend = OpenAIBackend(model="m", api_key="k", base_url="http://localhost:8000/v1")
        assert backend.trusted is True

    def test_private_network_base_url_is_trusted_by_default(self):
        backend = OpenAIBackend(model="m", api_key="k", base_url="http://192.168.1.50:8000/v1")
        assert backend.trusted is True

    def test_hosted_base_url_is_untrusted_by_default(self):
        backend = OpenAIBackend(model="m", api_key="k", base_url="https://api.openai.com/v1")
        assert backend.trusted is False

    def test_explicit_trusted_true_overrides_hosted_default(self):
        backend = OpenAIBackend(
            model="m", api_key="k", base_url="https://api.openai.com/v1", trusted=True,
        )
        assert backend.trusted is True

    def test_explicit_trusted_false_overrides_loopback_default(self):
        backend = OpenAIBackend(
            model="m", api_key="k", base_url="http://localhost:8000/v1", trusted=False,
        )
        assert backend.trusted is False


class TestOpenAIBackendFromSettings:
    def test_builds_from_config(self):
        with cfg.override_settings(
            openai_model="m", openai_api_key="k", openai_base_url="http://localhost:8000/v1",
        ):
            backend = OpenAIBackend.from_settings()
        assert backend.name == "openai:m"
        assert backend.endpoint == "http://localhost:8000/v1"
        assert backend.trusted is True


class TestMockBackend:
    def test_defaults(self):
        backend = MockBackend()
        assert backend.name == "mock:stub"
        assert backend.generate("anything") == "SELECT 1"
        assert backend.test_connection() is True

    def test_trusted_by_default(self):
        assert MockBackend().trusted is True

    def test_endpoint_is_none(self):
        assert MockBackend().endpoint is None

    def test_structured_returns_configured_dict(self):
        backend = MockBackend(structured={"sql": "SELECT 9", "out_of_scope": False})
        obj, meta = backend.generate_structured(PromptSegments(question="q"), schema={})
        assert obj == {"sql": "SELECT 9", "out_of_scope": False}
        assert meta["structured_output"] is True

    def test_structured_default_is_empty_dict(self):
        backend = MockBackend()
        obj, _meta = backend.generate_structured(PromptSegments(question="q"), schema={})
        assert obj == {}
