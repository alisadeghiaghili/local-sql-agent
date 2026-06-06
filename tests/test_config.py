"""Unit tests for config.py — Settings dataclass and override_settings()."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import config as cfg
from config import Settings, get_settings, override_settings


class TestSettings:
    def test_default_ollama_url(self):
        s = Settings()
        assert s.ollama_url == "http://localhost:11434/api/generate"

    def test_default_max_rows(self):
        s = Settings()
        assert s.max_rows_returned == 1000

    def test_env_override_max_rows(self):
        with patch.dict(os.environ, {"MAX_ROWS_RETURNED": "42"}):
            s = Settings()
        assert s.max_rows_returned == 42

    def test_env_override_model(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "my-model"}):
            s = Settings()
        assert s.ollama_model == "my-model"

    def test_frozen(self):
        s = Settings()
        with pytest.raises((AttributeError, TypeError)):
            s.ollama_model = "x"  # type: ignore[misc]

    def test_validate_passes_for_valid_settings(self):
        s = Settings()
        s.validate()  # should not raise

    def test_validate_raises_for_placeholder_model(self):
        s = Settings.__new__(Settings)
        object.__setattr__(s, "ollama_model", "change_me")
        object.__setattr__(s, "db_connection_url", "mssql+pyodbc://ok")
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            s.validate()

    def test_validate_raises_for_empty_connection_url(self):
        s = Settings.__new__(Settings)
        object.__setattr__(s, "ollama_model", "gpt-oss:20b")
        object.__setattr__(s, "db_connection_url", "")
        with pytest.raises(ValueError, match="DB_CONNECTION_URL"):
            s.validate()


class TestGetSettings:
    def test_returns_settings_instance(self):
        assert isinstance(get_settings(), Settings)

    def test_singleton_returns_same_object(self):
        assert get_settings() is get_settings()


class TestOverrideSettings:
    def test_patches_single_field(self):
        with override_settings(max_rows_returned=7) as s:
            assert s.max_rows_returned == 7

    def test_restores_original_after_exit(self):
        original_max = cfg.settings.max_rows_returned
        with override_settings(max_rows_returned=999):
            pass
        assert cfg.settings.max_rows_returned == original_max

    def test_module_alias_updated(self):
        with override_settings(ollama_model="test-model"):
            assert cfg.settings.ollama_model == "test-model"

    def test_restores_on_exception(self):
        original = cfg.settings.ollama_model
        try:
            with override_settings(ollama_model="boom"):
                raise RuntimeError("oops")
        except RuntimeError:
            pass
        assert cfg.settings.ollama_model == original

    def test_multiple_fields(self):
        with override_settings(max_rows_returned=3, query_timeout_seconds=5) as s:
            assert s.max_rows_returned == 3
            assert s.query_timeout_seconds == 5


class TestLoggerThreadSafety:
    """Verify that concurrent save_log calls don't interleave lines."""

    def test_concurrent_writes_produce_valid_jsonl(self, tmp_path):
        import json
        import threading
        from datetime import datetime
        from unittest.mock import patch

        from logs.query_log import QueryLog
        from logs import logger as log_mod

        log_file = tmp_path / "query_log.jsonl"

        def make_log(n: int) -> QueryLog:
            return QueryLog(
                timestamp=datetime.now(),
                question=f"q{n}",
                generated_sql="SELECT 1",
                model_name="m",
                status="SUCCESS",
                execution_time_seconds=0.1,
            )

        with patch.object(log_mod, "_LOG_FILE", str(log_file)), \
             patch.object(log_mod, "settings") as ms:
            ms.log_dir = str(tmp_path)
            threads = [threading.Thread(target=log_mod.save_log, args=(make_log(i),)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 20
        for line in lines:
            record = json.loads(line)   # must be valid JSON
            assert "question" in record
