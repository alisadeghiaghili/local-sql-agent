# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for config.py."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import config as cfg
from config import Settings, get_settings, override_settings


class TestSettings:
    def test_default_openai_base_url(self):
        assert Settings().openai_base_url == "https://api.openai.com/v1"

    def test_default_max_rows(self):
        assert Settings().max_rows_returned == 1000

    def test_env_override_max_rows(self):
        with patch.dict(os.environ, {"MAX_ROWS_RETURNED": "42"}):
            assert Settings().max_rows_returned == 42

    def test_default_openai_model(self):
        with patch.dict(os.environ):
            os.environ.pop("OPENAI_MODEL", None)
            assert Settings().openai_model == "gpt-4o-mini"

    def test_env_override_openai_model(self):
        with patch.dict(os.environ, {"OPENAI_MODEL": "gpt-oss-20:F16"}):
            assert Settings().openai_model == "gpt-oss-20:F16"

    def test_env_override_openai_base_url(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://vllm:8000/v1"}):
            assert Settings().openai_base_url == "http://vllm:8000/v1"

    def test_env_override_openai_api_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            assert Settings().openai_api_key == "sk-test"

    def test_frozen(self):
        with pytest.raises((AttributeError, TypeError)):
            Settings().openai_model = "x"  # type: ignore[misc]

    def test_validate_passes(self):
        Settings(
            openai_model="gpt-oss-20b",
            db_connection_url="mssql+pyodbc://prod-db-host:1433/RealDB"
            "?driver=ODBC+Driver+17+for+SQL+Server",
        ).validate()

    def test_validate_raises_for_factory_default_db_url(self):
        """The bogus factory-default DB_CONNECTION_URL is a full connection
        string, not a single token, so it is never an exact match against
        any entry in the placeholder set and used to sail through
        unnoticed. validate() must detect it by its placeholder host."""
        s = Settings(
            openai_model="gpt-oss-20b",
            db_connection_url=(
                "mssql+pyodbc://username@server:1433/Auction_DM"
                "?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes"
            ),
        )
        with pytest.raises(ValueError, match="DB_CONNECTION_URL"):
            s.validate()

    def test_validate_raises_for_placeholder_model(self):
        s = Settings.__new__(Settings)
        object.__setattr__(s, "openai_model", "change_me")
        object.__setattr__(s, "db_connection_url", "mssql+ok")
        with pytest.raises(ValueError, match="OPENAI_MODEL"):
            s.validate()

    def test_validate_raises_for_empty_url(self):
        s = Settings.__new__(Settings)
        object.__setattr__(s, "openai_model", "gpt-oss:20b")
        object.__setattr__(s, "db_connection_url", "")
        with pytest.raises(ValueError, match="DB_CONNECTION_URL"):
            s.validate()

    # --- multi-dialect: SQL_DIALECT (new) ---

    def test_default_sql_dialect_is_tsql(self):
        with patch.dict(os.environ):
            os.environ.pop("SQL_DIALECT", None)
            assert Settings().sql_dialect == "tsql"

    def test_env_override_sql_dialect(self):
        with patch.dict(os.environ, {"SQL_DIALECT": "postgres"}):
            assert Settings().sql_dialect == "postgres"

    def test_validate_rejects_unknown_sql_dialect(self):
        s = Settings(
            openai_model="gpt-oss-20b",
            db_connection_url="mssql+pyodbc://prod-db-host:1433/RealDB",
            sql_dialect="oracle",
        )
        with pytest.raises(ValueError, match="oracle"):
            s.validate()

    def test_validate_passes_for_every_supported_dialect_with_matching_url(self):
        """One dialect resolved from config at start-up, matching the
        connection URL's own backend -- validate() must accept all four
        supported combinations, not just tsql."""
        urls = {
            "tsql": "mssql+pyodbc://prod-db-host:1433/RealDB",
            "postgres": "postgresql+psycopg2://prod-db-host:5432/realdb",
            "mysql": "mysql+pymysql://prod-db-host:3306/realdb",
            "sqlite": "sqlite:////tmp/real.db",
        }
        for dialect, url in urls.items():
            Settings(
                openai_model="gpt-oss-20b", db_connection_url=url, sql_dialect=dialect,
            ).validate()

    def test_validate_rejects_sql_dialect_mismatched_with_connection_url(self):
        """SQL_DIALECT=postgres against a still-mssql DB_CONNECTION_URL is
        a real misconfiguration -- every query would fail at execution
        against the wrong dialect -- so this must fail closed at
        start-up, the same way the placeholder checks above do."""
        s = Settings(
            openai_model="gpt-oss-20b",
            db_connection_url="mssql+pyodbc://prod-db-host:1433/RealDB",
            sql_dialect="postgres",
        )
        with pytest.raises(ValueError, match="SQL_DIALECT"):
            s.validate()

    # --- cache settings (new) ---

    def test_default_cache_ttl_seconds(self):
        assert Settings().cache_ttl_seconds == 300

    def test_default_cache_max_size(self):
        assert Settings().cache_max_size == 256

    def test_env_override_cache_ttl(self):
        with patch.dict(os.environ, {"CACHE_TTL_SECONDS": "0"}):
            assert Settings().cache_ttl_seconds == 0

    def test_env_override_cache_max_size(self):
        with patch.dict(os.environ, {"CACHE_MAX_SIZE": "64"}):
            assert Settings().cache_max_size == 64

    def test_cache_ttl_zero_means_disabled(self):
        """TTL=0 is the conventional way to disable the cache."""
        with patch.dict(os.environ, {"CACHE_TTL_SECONDS": "0"}):
            s = Settings()
            assert s.cache_ttl_seconds == 0


class TestEndpointTrustSettings:
    """``llm_trusted`` is a tri-state (None/True/False) settings field --
    see llm/trust.py and llm/endpoints.py for how it's consumed."""

    def test_unset_is_none(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_TRUSTED", None)
            assert Settings().llm_trusted is None

    def test_env_true(self):
        with patch.dict(os.environ, {"LLM_TRUSTED": "true"}):
            assert Settings().llm_trusted is True

    def test_env_false(self):
        with patch.dict(os.environ, {"LLM_TRUSTED": "false"}):
            assert Settings().llm_trusted is False

    def test_default_llm_provider_is_openai(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_PROVIDER", None)
            assert Settings().llm_provider == "openai"

    def test_default_llm_endpoints_json_is_empty(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_ENDPOINTS", None)
            assert Settings().llm_endpoints_json == ""

    def test_default_llm_routes_json_is_empty(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_ROUTES", None)
            assert Settings().llm_routes_json == ""


class TestDotenvLoading:
    """config.py must load .env itself — python app.py / uvicorn must not
    silently run on defaults just because setup_project.py is the only
    place that calls load_dotenv()."""

    def test_env_file_is_loaded_on_import(self, tmp_path):
        """A fresh interpreter, cwd'd into a directory with a .env file,
        must see config.settings pick up the .env value with no other
        setup — reproduces `python app.py` / `uvicorn api.server:app`."""
        import subprocess
        import sys
        from pathlib import Path

        env_file = tmp_path / ".env"
        env_file.write_text("OPENAI_MODEL=dotenv-test-model\n", encoding="utf-8")

        repo_root = Path(__file__).resolve().parent.parent
        child_env = dict(os.environ)
        child_env.pop("OPENAI_MODEL", None)  # don't let the real shell shadow .env
        child_env["PYTHONPATH"] = str(repo_root)

        result = subprocess.run(
            [sys.executable, "-c", "import config; print(config.settings.openai_model)"],
            cwd=str(tmp_path),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "dotenv-test-model"


class TestGetSettings:
    def test_returns_settings_instance(self):
        assert isinstance(get_settings(), Settings)

    def test_singleton(self):
        assert get_settings() is get_settings()


class TestOverrideSettings:
    """Key guarantee: cfg.settings is patched for ALL modules during the block."""

    def test_patches_cfg_settings(self):
        with override_settings(max_rows_returned=7):
            assert cfg.settings.max_rows_returned == 7

    def test_restores_after_exit(self):
        original = cfg.settings.max_rows_returned
        with override_settings(max_rows_returned=999):
            pass
        assert cfg.settings.max_rows_returned == original

    def test_yields_patched_object(self):
        with override_settings(openai_model="test-model") as s:
            assert s.openai_model == "test-model"

    def test_restores_on_exception(self):
        original = cfg.settings.openai_model
        try:
            with override_settings(openai_model="boom"):
                raise RuntimeError("oops")
        except RuntimeError:
            pass
        assert cfg.settings.openai_model == original

    def test_multiple_fields(self):
        with override_settings(max_rows_returned=3, query_timeout_seconds=5) as s:
            assert s.max_rows_returned == 3
            assert s.query_timeout_seconds == 5

    def test_consumer_module_sees_patch(self):
        """database/executor reads cfg.settings at call-time, so it must see the patch."""
        import database.executor as executor_mod
        with override_settings(max_rows_returned=77):
            assert cfg.settings.max_rows_returned == 77

    def test_logger_sees_patch(self):
        """logs/logger resolves log_dir lazily, so override_settings must work."""
        import logs.logger as logger_mod
        with override_settings(log_dir="/tmp/test_logs"):
            assert logger_mod._log_file().startswith("/tmp/test_logs")

    def test_cache_ttl_override(self):
        with override_settings(cache_ttl_seconds=0) as s:
            assert s.cache_ttl_seconds == 0

    def test_cache_max_size_override(self):
        with override_settings(cache_max_size=10) as s:
            assert s.cache_max_size == 10


class TestLoggerThreadSafety:
    def test_concurrent_writes_produce_valid_jsonl(self, tmp_path):
        import json
        import threading
        from datetime import datetime
        from unittest.mock import patch as mpatch

        from logs.query_log import QueryLog
        import logs.logger as log_mod

        def make_log(n):
            return QueryLog(
                timestamp=datetime.now(), question=f"q{n}",
                generated_sql="SELECT 1", model_name="m",
                status="SUCCESS", execution_time_seconds=0.1,
            )

        with override_settings(log_dir=str(tmp_path)):
            threads = [
                threading.Thread(target=log_mod.save_log, args=(make_log(i),))
                for i in range(20)
            ]
            for t in threads: t.start()
            for t in threads: t.join()

        log_path = tmp_path / "query_log.jsonl"
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 20
        for line in lines:
            assert "question" in json.loads(line)
