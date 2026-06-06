"""Unit tests for config.Settings."""

from __future__ import annotations

import os
import pytest

# Patch env before importing config
os.environ.setdefault("OLLAMA_MODEL", "test-model")
os.environ.setdefault("DB_CONNECTION_URL", "mssql+pyodbc://sa@testserver/testdb?driver=ODBC+Driver+17+for+SQL+Server")

from config import Settings  # noqa: E402


class TestSettings:
    def _make(self, **overrides) -> Settings:
        """Create a Settings instance with controlled env."""
        env = {
            "OLLAMA_MODEL":      "gemma3:12b",
            "DB_CONNECTION_URL": "mssql+pyodbc://sa@myserver/mydb?driver=ODBC+Driver+17+for+SQL+Server",
        }
        env.update(overrides)
        original = {k: os.environ.get(k) for k in env}
        for k, v in env.items():
            os.environ[k] = v
        s = Settings()
        # restore
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return s

    def test_validate_passes_with_full_config(self):
        s = self._make()
        s.validate()   # must not raise

    def test_validate_raises_on_missing_model(self):
        s = self._make(OLLAMA_MODEL="")
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            s.validate()

    def test_validate_raises_on_placeholder_password(self):
        s = self._make(DB_CONNECTION_URL="your_password_here")
        with pytest.raises(ValueError):
            s.validate()

    def test_defaults(self):
        s = Settings()
        assert s.query_timeout_seconds == int(os.getenv("QUERY_TIMEOUT_SECONDS", "60"))
        assert s.max_rows_returned == int(os.getenv("MAX_ROWS_RETURNED", "1000"))
        assert s.log_dir == os.getenv("LOG_DIR", "logs")
        assert s.export_dir == os.getenv("EXPORT_DIR", "exports")

    def test_connection_url_included_in_settings(self):
        s = self._make()
        assert "mssql+pyodbc" in s.db_connection_url
