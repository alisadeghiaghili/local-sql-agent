# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for the deployment-readiness checks added to
scripts/verify_deployment.py.

Only the four NEW checks are covered here (API key authentication, audit
log writability, project_config/ loading, rate-limit sanity) -- the
pre-existing DB/model checks need a live database/LLM endpoint and are
exercised by ``tests/integration/test_executor_live.py`` and this script's
own manual usage instead, not a unit test.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from appdb import key_store
from config import override_settings
from database.datasources import reset_datasources_cache
from scripts.issue_api_key import build_entry, issue_key
from scripts.verify_deployment import (
    build_checks,
    check_api_key_authenticates,
    check_audit_log_writable,
    check_project_config_loads,
    check_rate_limit_sane_for_deployment,
    check_session_store_writable,
)
from scripts.verify_deployment import check_table_datasources as verify_check_table_datasources

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"


# ---------------------------------------------------------------------------
# check_api_key_authenticates
# ---------------------------------------------------------------------------

class TestCheckApiKeyAuthenticates:
    def test_fails_when_auth_required_and_no_keys(self):
        with override_settings(auth_required=True, api_keys_json=""):
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert "no configured keys" in result.detail

    def test_passes_when_auth_not_required(self):
        with override_settings(auth_required=False, api_keys_json=""):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert "AUTH_REQUIRED=false" in result.detail

    def test_fails_on_malformed_api_keys_json(self):
        with override_settings(auth_required=True, api_keys_json="not json"):
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert "invalid" in result.detail

    def test_passes_without_verify_api_key_when_keys_configured(self, monkeypatch):
        monkeypatch.delenv("VERIFY_API_KEY", raising=False)
        raw_key = issue_key()
        entry = build_entry("analyst-1", "Analyst One", raw_key)
        with override_settings(auth_required=True, api_keys_json=json.dumps([entry])):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert "1 key(s) configured" in result.detail

    def test_the_hint_is_spelled_for_the_shell_it_is_printed_into(self, monkeypatch):
        """The hint used to read ``VERIFY_API_KEY=<raw key> ...`` -- the
        POSIX inline-environment form, which PowerShell does not have.

        This project's setup guide walks Windows operators through every
        step in ``powershell`` blocks, so on the platform most likely to
        read this line, pasting it produced ``The term 'VERIFY_API_KEY=...'
        is not recognized as a name of a cmdlet``. That does not read as
        "your shell spells this differently"; it reads as "this script is
        broken", which is the opposite of what a check whose whole job is
        building confidence should do.
        """
        monkeypatch.delenv("VERIFY_API_KEY", raising=False)
        entry = build_entry("analyst-1", "Analyst One", issue_key())
        with override_settings(auth_required=True, api_keys_json=json.dumps([entry])):
            result = check_api_key_authenticates()

        assert result.status == "PASS"
        if os.name == "nt":
            assert "$env:VERIFY_API_KEY" in result.detail
            assert "VERIFY_API_KEY=" not in result.detail, (
                "the POSIX inline-environment form is a parse error in "
                "PowerShell, which is the shell this line was just printed into"
            )
        else:
            assert "VERIFY_API_KEY=" in result.detail
            assert "$env:" not in result.detail

    def test_verify_api_key_matching_passes_and_names_principal(self, monkeypatch):
        raw_key = issue_key()
        entry = build_entry("analyst-1", "Analyst One", raw_key)
        monkeypatch.setenv("VERIFY_API_KEY", raw_key)
        with override_settings(auth_required=True, api_keys_json=json.dumps([entry])):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert "analyst-1" in result.detail

    def test_verify_api_key_not_matching_fails(self, monkeypatch):
        raw_key = issue_key()
        entry = build_entry("analyst-1", "Analyst One", raw_key)
        wrong_key = issue_key()
        monkeypatch.setenv("VERIFY_API_KEY", wrong_key)
        with override_settings(auth_required=True, api_keys_json=json.dumps([entry])):
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert "did not match" in result.detail

    # -- keys in the application database ---------------------------------
    # api/server.py asks "is any key usable" against API_KEYS_JSON merged with
    # the application database, so this check has to as well. The autouse
    # ``_fresh_app_db`` fixture gives each test an empty in-memory database.

    @staticmethod
    def _unreadable_app_db(monkeypatch, message: str = "connection refused") -> None:
        def _boom():
            raise RuntimeError(message)

        monkeypatch.setattr(key_store, "get_active_principals", _boom)

    def test_passes_when_keys_exist_only_in_the_application_database(self, monkeypatch):
        monkeypatch.delenv("VERIFY_API_KEY", raising=False)
        key_store.issue_key("analyst-db", "Analyst From Admin Panel")
        with override_settings(auth_required=True, api_keys_json=""):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert "1 key(s) configured" in result.detail
        assert "1 from the application database" in result.detail
        assert "not readable" not in result.detail

    def test_detail_counts_both_sources(self, monkeypatch):
        monkeypatch.delenv("VERIFY_API_KEY", raising=False)
        key_store.issue_key("analyst-db", "From DB")
        entry = build_entry("analyst-env", "From Env", issue_key())
        with override_settings(auth_required=True, api_keys_json=json.dumps([entry])):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert "2 key(s) configured" in result.detail
        assert "1 from API_KEYS_JSON, 1 from the application database" in result.detail

    def test_does_not_import_environment_keys_into_the_database(self, monkeypatch):
        monkeypatch.delenv("VERIFY_API_KEY", raising=False)
        entry = build_entry("analyst-env", "From Env", issue_key())
        with override_settings(auth_required=True, api_keys_json=json.dumps([entry])):
            assert check_api_key_authenticates().status == "PASS"
        assert key_store.list_keys() == []

    def test_env_key_revoked_in_the_database_does_not_count(self, monkeypatch):
        monkeypatch.delenv("VERIFY_API_KEY", raising=False)
        raw_key = issue_key()
        entry = build_entry("analyst-env", "From Env", raw_key)
        with override_settings(auth_required=True, api_keys_json=json.dumps([entry])):
            key_store.bootstrap_from_env()
            key_store.revoke_key(entry["key_sha256"])
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert "revoked or disabled" in result.detail

    def test_unreadable_application_database_falls_back_to_env_keys(self, monkeypatch):
        monkeypatch.delenv("VERIFY_API_KEY", raising=False)
        self._unreadable_app_db(monkeypatch)
        entry = build_entry("analyst-1", "Analyst One", issue_key())
        with override_settings(auth_required=True, api_keys_json=json.dumps([entry])):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert "1 key(s) configured (1 from API_KEYS_JSON)" in result.detail
        assert (
            "(application database not readable: RuntimeError: connection "
            "refused; counted API_KEYS_JSON only)"
        ) in result.detail

    def test_unreadable_application_database_and_no_env_keys_fails(self, monkeypatch):
        self._unreadable_app_db(monkeypatch)
        with override_settings(auth_required=True, api_keys_json=""):
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert "no configured keys" in result.detail
        assert "application database not readable" in result.detail

    def test_unreadable_reason_is_kept_to_one_short_line(self, monkeypatch):
        self._unreadable_app_db(monkeypatch, "first line\n" + "x" * 500)
        with override_settings(auth_required=True, api_keys_json=""):
            result = check_api_key_authenticates()
        assert "\n" not in result.detail
        assert len(result.detail) < 700

    def test_invalid_api_keys_json_fails_even_with_database_keys(self):
        key_store.issue_key("analyst-db", "From DB")
        with override_settings(auth_required=True, api_keys_json="not json"):
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert "API_KEYS_JSON is invalid" in result.detail

    def test_auth_not_required_does_not_touch_the_application_database(self, monkeypatch):
        self._unreadable_app_db(monkeypatch)
        with override_settings(auth_required=False, api_keys_json=""):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert "not readable" not in result.detail

    def test_verify_api_key_matching_a_database_only_key_passes(self, monkeypatch):
        raw_key, _entry = key_store.issue_key("analyst-db", "Analyst From Admin Panel")
        monkeypatch.setenv("VERIFY_API_KEY", raw_key)
        with override_settings(auth_required=True, api_keys_json=""):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        assert "analyst-db" in result.detail
        assert "1 from the application database" in result.detail

    def test_verify_api_key_not_in_either_source_fails(self, monkeypatch):
        key_store.issue_key("analyst-db", "From DB")
        monkeypatch.setenv("VERIFY_API_KEY", issue_key())
        with override_settings(auth_required=True, api_keys_json=""):
            result = check_api_key_authenticates()
        assert result.status == "FAIL"
        assert "did not match" in result.detail

    def test_each_missing_denied_columns_warning_is_logged_once(self, monkeypatch, caplog):
        """The check parses ``API_KEYS_JSON`` itself and again through
        ``get_active_principals``; it used to print every warning twice."""
        monkeypatch.delenv("VERIFY_API_KEY", raising=False)
        entries = [
            build_entry("analyst-1", "One", issue_key()),
            build_entry("analyst-2", "Two", issue_key()),
        ]
        with caplog.at_level(logging.WARNING, logger="security.auth"), \
                override_settings(auth_required=True, api_keys_json=json.dumps(entries)):
            result = check_api_key_authenticates()
        assert result.status == "PASS"
        warnings = [
            r.getMessage() for r in caplog.records
            if "has no denied_columns field" in r.getMessage()
        ]
        assert len(warnings) == 2
        assert "id='analyst-1'" in warnings[0]
        assert "id='analyst-2'" in warnings[1]


# ---------------------------------------------------------------------------
# check_audit_log_writable
# ---------------------------------------------------------------------------

class TestCheckAuditLogWritable:
    def test_passes_for_writable_new_directory(self, tmp_path):
        log_dir = tmp_path / "logs"
        assert not log_dir.exists()
        with override_settings(log_dir=str(log_dir)):
            result = check_audit_log_writable()
        assert result.status == "PASS"
        assert log_dir.exists()  # created as a side effect
        # The probe file must not be left behind, and the real audit log
        # must never be touched by this check.
        assert list(log_dir.iterdir()) == []

    def test_passes_and_leaves_existing_audit_log_untouched(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        audit_log = log_dir / "audit_log.jsonl"
        audit_log.write_text('{"request_id": "keep-me"}\n', encoding="utf-8")
        with override_settings(log_dir=str(log_dir)):
            result = check_audit_log_writable()
        assert result.status == "PASS"
        assert audit_log.read_text(encoding="utf-8") == '{"request_id": "keep-me"}\n'

    def test_fails_when_directory_cannot_be_created(self, tmp_path):
        # A file where a directory needs to go: mkdir(parents=True) raises.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        bad_dir = str(blocker / "nested_logs")
        with override_settings(log_dir=bad_dir):
            result = check_audit_log_writable()
        assert result.status == "FAIL"


# ---------------------------------------------------------------------------
# check_session_store_writable
# ---------------------------------------------------------------------------

class TestCheckSessionStoreWritable:
    def test_skips_when_persistence_disabled(self):
        with override_settings(session_store_path=""):
            result = check_session_store_writable()
        assert result.status == "SKIP"

    def test_passes_for_writable_new_directory(self, tmp_path):
        store_dir = tmp_path / "store"
        db_path = store_dir / "sessions.db"
        assert not store_dir.exists()
        with override_settings(session_store_path=str(db_path)):
            result = check_session_store_writable()
        assert result.status == "PASS"
        assert store_dir.exists()  # created as a side effect
        assert list(store_dir.iterdir()) == []  # probe file removed, no db created

    def test_fails_when_directory_cannot_be_created(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        bad_path = str(blocker / "nested" / "sessions.db")
        with override_settings(session_store_path=bad_path):
            result = check_session_store_writable()
        assert result.status == "FAIL"


# ---------------------------------------------------------------------------
# check_project_config_loads
# ---------------------------------------------------------------------------

class TestCheckProjectConfigLoads:
    def test_passes_against_example_config(self):
        with override_settings(project_config_dir=str(_EXAMPLE_CONFIG_DIR)):
            result = check_project_config_loads()
        assert result.status == "PASS"

    def test_fails_when_directory_missing(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        with override_settings(project_config_dir=str(missing)):
            result = check_project_config_loads()
        assert result.status == "FAIL"
        assert "not found" in result.detail

    def test_fails_on_schema_missing_required_field(self, tmp_path):
        """A schema.yaml whose 'tables' entry sets a flag list without the
        db_schema qualifier fails SchemaConfig's own validation -- exactly
        the "stale schema.yaml" scenario this check exists to catch before
        a real query does."""
        dest = tmp_path / "project_config"
        shutil.copytree(_EXAMPLE_CONFIG_DIR, dest)
        (dest / "schema.yaml").write_text(
            "tables:\n"
            "  Foo:\n"
            "    description: test table\n"
            "    resolvable_columns: [Name]\n",
            encoding="utf-8",
        )
        with override_settings(project_config_dir=str(dest)):
            result = check_project_config_loads()
        assert result.status == "FAIL"
        assert "schema.yaml" in result.detail


# ---------------------------------------------------------------------------
# check_rate_limit_sane_for_deployment
# ---------------------------------------------------------------------------

class TestCheckRateLimitSane:
    def test_passes_at_shipped_defaults_for_ten_analysts(self, monkeypatch):
        monkeypatch.delenv("VERIFY_EXPECTED_ANALYSTS", raising=False)
        with override_settings(
            rate_limit_requests=600, rate_limit_window_seconds=60, rate_limit_burst=40,
        ):
            result = check_rate_limit_sane_for_deployment()
        assert result.status == "PASS"

    def test_fails_when_rate_too_low_for_expected_concurrency(self, monkeypatch):
        monkeypatch.delenv("VERIFY_EXPECTED_ANALYSTS", raising=False)
        with override_settings(
            rate_limit_requests=5, rate_limit_window_seconds=60, rate_limit_burst=0,
        ):
            result = check_rate_limit_sane_for_deployment()
        assert result.status == "FAIL"

    def test_respects_verify_expected_analysts_override(self, monkeypatch):
        monkeypatch.setenv("VERIFY_EXPECTED_ANALYSTS", "1")
        with override_settings(
            rate_limit_requests=60, rate_limit_window_seconds=60, rate_limit_burst=0,
        ):
            result = check_rate_limit_sane_for_deployment()
        # 60 req/min for a single expected analyst is comfortably above
        # the 0.1 req/sec/analyst floor.
        assert result.status == "PASS"
        assert "1 concurrent analysts" in result.detail


# ---------------------------------------------------------------------------
# check_table_datasources (the verify_deployment.py wrapper, not
# database.datasources's own function of the same name -- imported above
# under an alias to keep the two apart)
# ---------------------------------------------------------------------------

class TestCheckTableDatasourcesCheck:
    def test_passes_for_the_example_schema(self):
        with override_settings(project_config_dir=str(_EXAMPLE_CONFIG_DIR)):
            reset_datasources_cache()
            result = verify_check_table_datasources()
        assert result.status == "PASS"
        assert "1 data source" in result.detail

    def test_fails_naming_the_offending_table(self, tmp_path):
        dest = tmp_path / "project_config"
        shutil.copytree(_EXAMPLE_CONFIG_DIR, dest)
        doc = (dest / "schema.yaml").read_text(encoding="utf-8")
        doc = doc.replace(
            "Customer:\n    description:",
            "Customer:\n    datasource: does-not-exist\n    description:",
            1,
        )
        (dest / "schema.yaml").write_text(doc, encoding="utf-8")
        import schema_data.registry as registry_module
        registry_module._cache.clear()
        try:
            with override_settings(project_config_dir=str(dest)):
                reset_datasources_cache()
                result = verify_check_table_datasources()
        finally:
            registry_module._cache.clear()
        assert result.status == "FAIL"
        assert "does-not-exist" in result.detail


# ---------------------------------------------------------------------------
# build_checks() -- per-source expansion
# ---------------------------------------------------------------------------

class TestBuildChecks:
    def test_single_source_keeps_plain_check_names(self):
        with override_settings(project_config_dir=str(_EXAMPLE_CONFIG_DIR)):
            reset_datasources_cache()
            checks = build_checks()
        names = [c.__name__ for c in checks]
        assert "check_db_connectivity" in names
        # No "[source]" suffix anywhere -- these are the plain functions.
        assert names.count("check_db_connectivity") == 1

    def test_several_sources_expand_the_per_source_checks(self, tmp_path):
        (tmp_path / "datasources.yaml").write_text(
            "default: main\n"
            "datasources:\n"
            "  main:\n    url_env: DB_URL_MAIN\n"
            "  archive:\n    url_env: DB_URL_ARCHIVE\n",
            encoding="utf-8",
        )
        with override_settings(project_config_dir=str(tmp_path)):
            reset_datasources_cache()
            checks = build_checks()
        names = [c.__name__ for c in checks]
        # Four per-source checks, each run once per source -> 8 entries,
        # every one still named after its underlying function (__name__
        # is what api/admin_routes.py filters _DEEP_CHECK_NAMES by).
        assert names.count("check_db_connectivity") == 2
        assert names.count("check_login_is_read_only") == 2
        assert names.count("check_row_cap") == 2
        assert names.count("check_query_timeout") == 2

    def test_per_source_check_runs_against_the_right_source(self, tmp_path):
        (tmp_path / "datasources.yaml").write_text(
            "default: main\n"
            "datasources:\n"
            "  main:\n    url_env: DB_URL_MAIN\n"
            "  archive:\n    url_env: DB_URL_ARCHIVE\n",
            encoding="utf-8",
        )
        with override_settings(project_config_dir=str(tmp_path)):
            reset_datasources_cache()
            checks = build_checks()
        db_checks = [c for c in checks if c.__name__ == "check_db_connectivity"]
        assert len(db_checks) == 2
        with patch("database.connection.get_engine", side_effect=RuntimeError("down")):
            results = [c() for c in db_checks]
        assert {r.name for r in results} == {
            "Database connectivity [main]", "Database connectivity [archive]",
        }

    def test_table_datasources_check_runs_before_the_per_source_checks(self):
        with override_settings(project_config_dir=str(_EXAMPLE_CONFIG_DIR)):
            reset_datasources_cache()
            checks = build_checks()
        names = [c.__name__ for c in checks]
        assert names[0] == "check_settings_valid"
        assert names[1] == "check_table_datasources"


# ---------------------------------------------------------------------------
# The connectivity check never prints a password
# ---------------------------------------------------------------------------

class TestConnectivityCheckRedactsTheUrl:
    SECRET = "hunter2@%:/"

    @pytest.fixture()
    def project_dir(self, tmp_path, monkeypatch):
        (tmp_path / "datasources.yaml").write_text(
            "datasources:\n"
            "  auction:\n"
            "    host: db1.example.test\n"
            "    database: SalesDW\n"
            "    username: nlq_reader\n"
            "    password_env: DB_PASSWORD_SALES\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("DB_PASSWORD_SALES", self.SECRET)
        reset_datasources_cache()
        yield tmp_path
        reset_datasources_cache()

    def test_a_successful_check_shows_the_masked_url(self, project_dir):
        from unittest.mock import MagicMock

        from scripts.verify_deployment import check_db_connectivity

        with override_settings(project_config_dir=str(project_dir)), \
             patch("database.connection.get_engine", return_value=MagicMock()):
            result = check_db_connectivity()
        assert result.status == "PASS"
        assert "hunter2" not in result.render()
        assert "nlq_reader:***@db1.example.test:1433/SalesDW" in result.detail

    def test_a_failed_check_shows_the_masked_url(self, project_dir):
        from sqlalchemy.exc import OperationalError

        from scripts.verify_deployment import check_db_connectivity

        failure = OperationalError("SELECT 1", {}, Exception("login failed"))
        with override_settings(project_config_dir=str(project_dir)), \
             patch("database.connection.get_engine", side_effect=failure):
            result = check_db_connectivity()
        assert result.status == "FAIL"
        assert "hunter2" not in result.render()
        assert "%40" not in result.render()
        assert "nlq_reader:***@db1.example.test" in result.detail

    def test_a_missing_variable_is_reported_by_name_not_value(self, project_dir, monkeypatch):
        from scripts.verify_deployment import check_db_connectivity

        monkeypatch.delenv("DB_PASSWORD_SALES")
        with override_settings(project_config_dir=str(project_dir)):
            result = check_db_connectivity()
        assert result.status == "FAIL"
        assert "DB_PASSWORD_SALES" in result.detail
        assert "auction" in result.detail
