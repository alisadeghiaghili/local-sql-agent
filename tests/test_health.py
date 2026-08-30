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
    def test_true_on_200(self):
        with cfg.override_settings(openai_base_url="http://localhost:8000/v1", openai_api_key="k"):
            with patch("requests.get", return_value=_response(200)) as get:
                assert _ping_openai() is True
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer k"
        assert get.call_args[0][0] == "http://localhost:8000/v1/models"

    def test_false_on_non_200(self):
        with patch("requests.get", return_value=_response(500)):
            assert _ping_openai() is False

    def test_false_on_exception(self):
        with patch("requests.get", side_effect=OSError("no network")):
            assert _ping_openai() is False

    def test_works_with_empty_api_key(self):
        with cfg.override_settings(openai_api_key=""):
            with patch("requests.get", return_value=_response(200)):
                assert _ping_openai() is True


class TestPingDb:
    def test_true_when_select_1_succeeds(self):
        fake_conn = MagicMock()
        fake_engine = MagicMock()
        fake_engine.connect.return_value.__enter__.return_value = fake_conn
        with patch("database.connection.get_engine", return_value=fake_engine):
            assert _ping_db() is True

    def test_false_on_exception(self):
        with patch("database.connection.get_engine", side_effect=RuntimeError("down")):
            assert _ping_db() is False


class TestCheckHealth:
    def test_ok_when_both_reachable(self):
        with patch("api.health._ping_openai", return_value=True), \
             patch("api.health._ping_db", return_value=True):
            resp = check_health()
        assert resp.status == "ok"
        assert resp.openai is True
        assert resp.database is True

    def test_degraded_when_only_one_reachable(self):
        with patch("api.health._ping_openai", return_value=True), \
             patch("api.health._ping_db", return_value=False):
            resp = check_health()
        assert resp.status == "degraded"

    def test_down_when_neither_reachable(self):
        with patch("api.health._ping_openai", return_value=False), \
             patch("api.health._ping_db", return_value=False):
            resp = check_health()
        assert resp.status == "down"

    def test_model_reflects_current_settings(self):
        with cfg.override_settings(openai_model="gpt-oss-20b"):
            with patch("api.health._ping_openai", return_value=True), \
                 patch("api.health._ping_db", return_value=True):
                resp = check_health()
        assert resp.model == "gpt-oss-20b"
