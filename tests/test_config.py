"""Unit tests for sql_agent.config.Settings."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from sql_agent.config import Settings


class TestSettings:
    def _minimal_env(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_MODEL",  "gemma3:12b")
        monkeypatch.setenv("DB_SERVER",     "myserver")
        monkeypatch.setenv("DB_NAME",       "mydb")
        monkeypatch.setenv("DB_USER",       "sa")
        monkeypatch.setenv("DB_PASSWORD",   "secret")

    def test_validate_passes_with_full_env(self, monkeypatch):
        self._minimal_env(monkeypatch)
        Settings().validate()   # must not raise

    def test_validate_raises_on_missing_model(self, monkeypatch):
        self._minimal_env(monkeypatch)
        monkeypatch.delenv("OLLAMA_MODEL")
        with pytest.raises(ValueError, match="OLLAMA_MODEL"):
            Settings().validate()

    def test_validate_raises_on_placeholder(self, monkeypatch):
        self._minimal_env(monkeypatch)
        monkeypatch.setenv("DB_PASSWORD", "your_password_here")
        with pytest.raises(ValueError):
            Settings().validate()

    def test_sqlserver_uri_sql_auth(self, monkeypatch):
        self._minimal_env(monkeypatch)
        uri = Settings().sqlserver_uri()
        assert "mssql+pyodbc" in uri
        assert "myserver" in uri
        assert "mydb" in uri

    def test_sqlserver_uri_trusted(self, monkeypatch):
        self._minimal_env(monkeypatch)
        monkeypatch.setenv("DB_TRUSTED_CONNECTION", "yes")
        uri = Settings().sqlserver_uri()
        assert "trusted_connection=yes" in uri
        assert "secret" not in uri   # password must not appear

    def test_sqlite_db_path_default(self):
        cfg = Settings()
        assert cfg.sqlite_db_path == os.getenv("SQLITE_DB_PATH", "sample.db")
