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
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import override_settings
from scripts.issue_api_key import build_entry, issue_key
from scripts.verify_deployment import (
    check_api_key_authenticates,
    check_audit_log_writable,
    check_project_config_loads,
    check_rate_limit_sane_for_deployment,
    check_session_store_writable,
)

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
