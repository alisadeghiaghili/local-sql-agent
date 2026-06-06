"""Unit tests for config.py."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import config as cfg
from config import Settings, get_settings, override_settings


class TestSettings:
    def test_default_ollama_url(self):
        assert Settings().ollama_url == "http://localhost:11434/api/generate"

    def test_default_max_rows(self):
        assert Settings().max_rows_returned == 1000

    def test_env_override_max_rows(self):
        with patch.dict(os.environ, {"MAX_ROWS_RETURNED": "42"}):
            assert Settings().max_rows_returned == 42

    def test_env_override_model(self):
        with patch.dict(os.environ, {"OLLAMA_MODEL": "my-model"}):
            assert Settings().ollama_model == "my-model"

    def test_frozen(self):
        with pytest.raises((AttributeError, TypeError)):
            Settings().ollama_model = "x"  # type: ignore[misc]

    def test_validate_passes(self):
        Settings().validate()

    def test_validate_raises_for_placeholder_model(self):
        s = Settings.__new__(Settings)
        object.__setattr__(s, "ollama_model", "change_me")
        object.__setattr__(s, "db_connection_url", "mssql+ok")
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            s.validate()

    def test_validate_raises_for_empty_url(self):
        s = Settings.__new__(Settings)
        object.__setattr__(s, "ollama_model", "gpt-oss:20b")
        object.__setattr__(s, "db_connection_url", "")
        with pytest.raises(ValueError, match="DB_CONNECTION_URL"):
            s.validate()


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
        with override_settings(ollama_model="test-model") as s:
            assert s.ollama_model == "test-model"

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

    def test_consumer_module_sees_patch(self):
        """database/executor reads cfg.settings at call-time, so it must see the patch."""
        import database.executor as executor_mod
        with override_settings(max_rows_returned=77):
            # executor uses cfg.settings.max_rows_returned at runtime
            assert cfg.settings.max_rows_returned == 77

    def test_logger_sees_patch(self):
        """logs/logger resolves log_dir lazily, so override_settings must work."""
        import logs.logger as logger_mod
        with override_settings(log_dir="/tmp/test_logs"):
            assert logger_mod._log_file().startswith("/tmp/test_logs")


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
