# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for api/runner.py::_interpret's data-governance gate (Phase 2 task 5).

Exit criterion 8: "_interpret's row data is provably gated when an
untrusted endpoint is configured -- a test asserting refusal."

Trust is per-endpoint now (see llm/trust.py, llm/router.py::is_trusted_backend),
not per-class -- so the two ``OpenAIBackend`` instances below differ only in
``base_url``, one defaulting to untrusted (the factory-default hosted API)
and one defaulting to trusted (a loopback address), exactly the two
directions ``tests/test_llm_router.py::TestTrust`` proves at the router level.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import config as cfg
from api.runner import _interpret
from llm.providers import MockBackend, OpenAIBackend
from llm.sql_agent import SQLAgent

_ROWS = [{"n": i} for i in range(5)]


def _agent_with(backend) -> SQLAgent:  # noqa: ANN001
    return SQLAgent(backend=backend, execute_fn=lambda sql: None)


class TestUntrustedEndpointRefused:
    def test_hosted_openai_backend_refused_without_opt_in(self):
        """No base_url override -> the factory-default hosted API -> untrusted."""
        backend = OpenAIBackend(model="gpt-4o-mini", api_key="sk-fake")
        assert backend.trusted is False
        backend.generate = MagicMock(side_effect=AssertionError("must not be called"))
        agent = _agent_with(backend)

        with cfg.override_settings(llm_allow_remote=False):
            result = _interpret(agent, "how many?", _ROWS)

        assert result == ""
        backend.generate.assert_not_called()

    def test_refusal_logs_an_error_naming_the_backend(self, caplog):
        import logging

        backend = OpenAIBackend(model="gpt-4o-mini", api_key="sk-fake")
        backend.generate = MagicMock(side_effect=AssertionError("must not be called"))
        agent = _agent_with(backend)

        with cfg.override_settings(llm_allow_remote=False):
            with caplog.at_level(logging.ERROR, logger="api.runner"):
                _interpret(agent, "how many?", _ROWS)

        assert any("REFUSED" in r.message for r in caplog.records)
        assert any("openai:gpt-4o-mini" in r.message for r in caplog.records)


class TestUntrustedEndpointAllowedWithOptIn:
    def test_hosted_openai_backend_proceeds_once_allow_remote_is_true(self):
        backend = OpenAIBackend(model="gpt-4o-mini", api_key="sk-fake")
        backend.generate = MagicMock(return_value="the interpretation")
        agent = _agent_with(backend)

        with cfg.override_settings(llm_allow_remote=True):
            result = _interpret(agent, "how many?", _ROWS)

        assert result == "the interpretation"
        backend.generate.assert_called_once()


class TestTrustedEndpointNeverGated:
    def test_localhost_openai_backend_proceeds_regardless_of_allow_remote(self):
        """A local-looking base_url is trusted by default -- no opt-in needed."""
        backend = OpenAIBackend(
            model="gpt-oss-20b", api_key="", base_url="http://localhost:8000/v1",
        )
        assert backend.trusted is True
        backend.generate = MagicMock(return_value="local interpretation")
        agent = _agent_with(backend)

        with cfg.override_settings(llm_allow_remote=False):
            result = _interpret(agent, "how many?", _ROWS)

        assert result == "local interpretation"
        backend.generate.assert_called_once()

    def test_mock_backend_proceeds_regardless_of_allow_remote(self):
        backend = MockBackend(response="local interpretation")
        agent = _agent_with(backend)

        with cfg.override_settings(llm_allow_remote=False):
            result = _interpret(agent, "how many?", _ROWS)

        assert result == "local interpretation"
