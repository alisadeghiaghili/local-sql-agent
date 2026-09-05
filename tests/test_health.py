# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for api/health.py — the OpenAI-compatible /models probe + DB ping.

api/test_api_endpoints.py::TestHealth exercises the endpoint with
check_health() mocked wholesale; this file exercises check_health's own
two probes directly, with requests/the DB engine mocked instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import config as cfg
from api.health import _ping_db, _ping_openai, check_health


def _response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    return resp


class TestPingOpenAI:
    """The probe must agree with the engine, or its light lies.

    Both assertions below encode a real incident: the CLI answered
    questions perfectly through the configured endpoint while this UI
    showed the LLM light red, because the probe and
    ``llm.providers.OpenAIBackend`` disagreed about two things.
    """

    def test_true_on_200(self):
        with cfg.override_settings(openai_base_url="http://localhost:8000/v1", openai_api_key="k"):
            with patch("requests.get", return_value=_response(200)) as get:
                ok, detail = _ping_openai()
                assert ok is True
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer k"
        assert get.call_args[0][0] == "http://localhost:8000/v1/models"
        assert "200" in detail

    def test_no_authorization_header_at_all_when_the_key_is_empty(self):
        """An empty key means NO header, never ``Bearer `` with nothing after it.

        Many self-hosted OpenAI-compatible servers check no credentials,
        so an empty key is legitimate -- and a malformed header is not the
        same as an absent one: plenty of servers answer the first with 401
        and the second with 200. ``OpenAIBackend._headers`` omits it, so
        the engine worked where this probe did not.
        """
        with cfg.override_settings(openai_api_key=""):
            with patch("requests.get", return_value=_response(200)) as get:
                ok, _ = _ping_openai()
                assert ok is True
        assert "Authorization" not in get.call_args.kwargs["headers"], (
            "sending 'Bearer ' with an empty token is what made a working "
            "endpoint report as unreachable"
        )

    def test_missing_models_endpoint_is_not_a_fault(self):
        """404 on /models means only that a server lacks an endpoint we never call.

        Generation goes to /chat/completions. Reporting this as down is a
        false negative, and a health light that cries wolf gets ignored
        exactly when it matters.
        """
        for status in (404, 405):
            with patch("requests.get", return_value=_response(status)):
                ok, detail = _ping_openai()
            assert ok is True, f"HTTP {status} on /models must not be reported as down"
            assert "chat/completions" in detail

    def test_rejected_credentials_are_a_fault_and_say_so(self):
        for status in (401, 403):
            with patch("requests.get", return_value=_response(status)):
                ok, detail = _ping_openai()
            assert ok is False
            assert "OPENAI_API_KEY" in detail, (
                "a reachable endpoint that refuses our key is a different "
                "problem from an unreachable one, and needs a different fix"
            )

    def test_false_on_other_non_200(self):
        with patch("requests.get", return_value=_response(500)):
            ok, detail = _ping_openai()
        assert ok is False
        assert "500" in detail

    def test_false_on_exception_names_the_failure(self):
        with patch("requests.get", side_effect=OSError("no network")):
            ok, detail = _ping_openai()
        assert ok is False
        assert "unreachable" in detail and "OSError" in detail


class TestPingDb:
    def test_true_when_select_1_succeeds(self):
        fake_conn = MagicMock()
        fake_engine = MagicMock()
        fake_engine.connect.return_value.__enter__.return_value = fake_conn
        with patch("database.connection.get_engine", return_value=fake_engine):
            ok, detail = _ping_db()
        assert ok is True
        assert "SELECT 1" in detail

    def test_false_on_exception_names_the_exception(self):
        with patch("database.connection.get_engine", side_effect=RuntimeError("down")):
            ok, detail = _ping_db()
        assert ok is False
        assert "RuntimeError" in detail


class TestCheckHealth:
    def test_ok_when_both_reachable(self):
        with patch("api.health._ping_openai", return_value=(True, "up")),              patch("api.health._ping_db", return_value=(True, "up")):
            resp = check_health()
        assert resp.status == "ok"
        assert resp.openai is True
        assert resp.database is True

    def test_degraded_when_only_one_reachable(self):
        with patch("api.health._ping_openai", return_value=(True, "up")),              patch("api.health._ping_db", return_value=(False, "nope")):
            resp = check_health()
        assert resp.status == "degraded"

    def test_down_when_neither_reachable(self):
        with patch("api.health._ping_openai", return_value=(False, "nope")),              patch("api.health._ping_db", return_value=(False, "nope")):
            resp = check_health()
        assert resp.status == "down"

    def test_model_reflects_current_settings(self):
        with cfg.override_settings(openai_model="gpt-oss-20b"):
            with patch("api.health._ping_openai", return_value=(True, "up")),                  patch("api.health._ping_db", return_value=(True, "up")):
                resp = check_health()
        assert resp.model == "gpt-oss-20b"

    def test_the_reason_reaches_the_response(self):
        """A red light with no reason is what made this undiagnosable."""
        with patch("api.health._ping_openai", return_value=(False, "endpoint refused our key")),              patch("api.health._ping_db", return_value=(True, "SELECT 1 succeeded")):
            resp = check_health()
        assert resp.openai_detail == "endpoint refused our key"
        assert resp.database_detail == "SELECT 1 succeeded"
