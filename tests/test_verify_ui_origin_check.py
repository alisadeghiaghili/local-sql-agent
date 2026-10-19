# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The preflight check ``Web UI origin is allowed by CORS``.

The 6.14.0 incident: ``verify_deployment`` passed fully, yet the web UI showed
every light red, because the page was opened as ``http://localhost:8077`` while
``CORS_ALLOWED_ORIGINS`` held only ``http://172.16.101.42:8077``. The preflight,
the CLI and ``curl`` send no ``Origin``, so none of them could notice. With
``VERIFY_UI_ORIGIN`` the check speaks for the browser; without it, it advises.
"""

from __future__ import annotations

import pytest

from config import override_settings
from scripts.verify_deployment import build_checks, check_web_ui_origin_allowed

NAME = "Web UI origin is allowed by CORS"
IP_ORIGIN = "http://172.16.101.42:8077"
LOCAL_ORIGIN = "http://localhost:8077"


@pytest.fixture(autouse=True)
def _no_ambient_ui_origin(monkeypatch):
    monkeypatch.delenv("VERIFY_UI_ORIGIN", raising=False)


def _run(allowed, *, api_host="127.0.0.1"):
    with override_settings(cors_allowed_origins=tuple(allowed), api_host=api_host):
        return check_web_ui_origin_allowed()


class TestWithVerifyUiOrigin:
    def test_pass_when_the_origin_is_listed(self, monkeypatch):
        monkeypatch.setenv("VERIFY_UI_ORIGIN", IP_ORIGIN)

        result = _run([IP_ORIGIN, LOCAL_ORIGIN])

        assert (result.name, result.status) == (NAME, "PASS")
        assert IP_ORIGIN in result.detail
        assert not result.detail.startswith("advice:")

    def test_fail_when_the_origin_is_not_listed_and_the_env_line_is_printed(self, monkeypatch):
        monkeypatch.setenv("VERIFY_UI_ORIGIN", LOCAL_ORIGIN)

        result = _run([IP_ORIGIN])

        assert (result.name, result.status) == (NAME, "FAIL")
        assert f"CORS_ALLOWED_ORIGINS={IP_ORIGIN},{LOCAL_ORIGIN}" in result.detail
        assert "exactly an allowed address (scheme, host and port)" in result.detail
        assert "Failed to fetch" in result.detail

    def test_a_trailing_slash_or_path_is_the_same_origin(self, monkeypatch):
        monkeypatch.setenv("VERIFY_UI_ORIGIN", IP_ORIGIN + "/index.html")

        assert _run([IP_ORIGIN]).status == "PASS"

    def test_scheme_and_port_are_part_of_the_origin(self, monkeypatch):
        for typed in ("https://172.16.101.42:8077", "http://172.16.101.42:8078", "http://172.16.101.42"):
            monkeypatch.setenv("VERIFY_UI_ORIGIN", typed)
            assert _run([IP_ORIGIN]).status == "FAIL", typed

    def test_a_value_that_is_not_an_address_fails_and_says_how_to_write_it(self, monkeypatch):
        monkeypatch.setenv("VERIFY_UI_ORIGIN", "172.16.101.42:8077")

        result = _run([IP_ORIGIN])

        assert result.status == "FAIL"
        assert "scheme://host[:port]" in result.detail

    def test_the_wildcard_allows_any_origin(self, monkeypatch):
        monkeypatch.setenv("VERIFY_UI_ORIGIN", LOCAL_ORIGIN)

        assert _run(["*"]).status == "PASS"

    def test_an_empty_list_fails_with_just_the_origin_in_the_line(self, monkeypatch):
        monkeypatch.setenv("VERIFY_UI_ORIGIN", LOCAL_ORIGIN)

        result = _run([])

        assert result.status == "FAIL"
        assert f"CORS_ALLOWED_ORIGINS={LOCAL_ORIGIN} " in result.detail + " "
        assert "(none)" in result.detail


class TestWithoutVerifyUiOrigin:
    def test_loopback_only_list_with_a_network_facing_api_is_advice(self):
        result = _run(["http://localhost:8080", "http://127.0.0.1:8080"], api_host="0.0.0.0")

        assert result.status == "PASS"
        assert result.detail.startswith("advice:")
        assert "API_HOST=0.0.0.0" in result.detail
        assert "IP address, host name, localhost" in result.detail
        assert "VERIFY_UI_ORIGIN" in result.detail

    def test_loopback_only_list_with_a_loopback_api_is_plain_pass(self):
        result = _run(["http://localhost:8080", "http://127.0.0.1:8080"], api_host="127.0.0.1")

        assert result.status == "PASS"
        assert not result.detail.startswith("advice:")

    def test_a_single_non_loopback_origin_is_advice(self):
        result = _run([IP_ORIGIN])

        assert result.status == "PASS"
        assert result.detail.startswith("advice:")
        assert f"only {IP_ORIGIN} is allowed" in result.detail
        assert "IP address, host name, localhost" in result.detail
        assert "VERIFY_UI_ORIGIN" in result.detail

    def test_the_incident_configuration_is_advice_not_a_failure(self):
        """The exact shape of 6.14.0: it cannot be a FAIL without knowing the address."""
        assert _run([IP_ORIGIN], api_host="0.0.0.0").status == "PASS"

    def test_several_origins_including_the_ip_address_need_no_advice(self):
        result = _run([IP_ORIGIN, LOCAL_ORIGIN], api_host="0.0.0.0")

        assert result.status == "PASS"
        assert not result.detail.startswith("advice:")
        assert "2 origin(s) allowed" in result.detail

    def test_the_wildcard_passes_without_advice(self):
        result = _run(["*"], api_host="0.0.0.0")

        assert (result.status, result.detail.startswith("advice:")) == ("PASS", False)

    def test_it_never_fails_without_the_variable(self):
        for allowed in ([], [IP_ORIGIN], ["http://localhost:8080"], [IP_ORIGIN, LOCAL_ORIGIN]):
            assert _run(allowed, api_host="0.0.0.0").status == "PASS"


class TestRegistration:
    def test_it_is_one_global_check_and_not_a_deep_check(self):
        from api.admin_routes import _DEEP_CHECK_NAMES

        names = [c.__name__ for c in build_checks()]

        assert names.count("check_web_ui_origin_allowed") == 1
        assert "check_web_ui_origin_allowed" not in _DEEP_CHECK_NAMES

    def test_with_one_data_source_the_preflight_has_twenty_four_checks(self):
        """Two leading, four per-source and eighteen global: the number the guides state."""
        from pathlib import Path

        from database.datasources import reset_datasources_cache

        example = Path(__file__).resolve().parent.parent / "project_config.example"
        with override_settings(project_config_dir=str(example)):
            reset_datasources_cache()
            count = len(build_checks())

        assert count == 24
