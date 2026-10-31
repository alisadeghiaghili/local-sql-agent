# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``database.setup_sources``: which data sources a setup tool reads, and how it names them."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine

import config as cfg
from database.datasources import reset_datasources_cache
from database.schema_inspector import SchemaInspector
from database.setup_sources import SourceSelectionError, configured_sources, datasources_file_in_use

TWO = """\
default: sales
datasources:
  sales:
    url_env: DB_URL_SALES
  reports:
    url_env: DB_URL_REPORTS
"""


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_URL_SALES", "sqlite:///sales.db")
    monkeypatch.setenv("DB_URL_REPORTS", "postgresql://user:pw-secret@host/db")
    reset_datasources_cache()
    with cfg.override_settings(project_config_dir=str(tmp_path)):
        yield tmp_path
    reset_datasources_cache()


def test_every_source_default_first_with_a_redacted_label(project):
    (project / "datasources.yaml").write_text(TWO, encoding="utf-8")
    sources, names, default = configured_sources()
    assert [s.name for s in sources] == ["sales", "reports"] and names == ["sales", "reports"]
    assert default == "sales"
    assert "pw-secret" not in sources[1].label and "***" in sources[1].label
    assert "pw-secret" in sources[1].secret_url  # only for scrubbing driver messages


def test_a_narrowed_run_still_reports_every_configured_name(project):
    (project / "datasources.yaml").write_text(TWO, encoding="utf-8")
    sources, names, _ = configured_sources(["reports"])
    assert [s.name for s in sources] == ["reports"] and names == ["sales", "reports"]


def test_the_engine_is_the_applications(project):
    (project / "datasources.yaml").write_text(TWO, encoding="utf-8")
    engine = create_engine("sqlite://")
    with patch("database.connection.get_engine", lambda name=None: engine):
        (sales, _) = configured_sources()[0]
        assert sales.open_engine() is engine


def test_an_unknown_source_names_the_configured_ones(project):
    (project / "datasources.yaml").write_text(TWO, encoding="utf-8")
    with pytest.raises(SourceSelectionError, match=r"unknown data source nope, other; configured: sales, reports"):
        configured_sources(["nope", "sales", "other"])


def test_an_invalid_file_is_reported_with_its_first_line(project):
    (project / "datasources.yaml").write_text("datasources: {}\n", encoding="utf-8")
    with pytest.raises(SourceSelectionError, match="datasources.yaml cannot be used"):
        configured_sources()
    with pytest.raises(SourceSelectionError, match="datasources.yaml cannot be used"):
        datasources_file_in_use()


def test_an_unset_connection_variable_is_reported(project, monkeypatch):
    (project / "datasources.yaml").write_text(TWO, encoding="utf-8")
    monkeypatch.delenv("DB_URL_REPORTS")
    with pytest.raises(SourceSelectionError, match=r"data source 'reports' cannot be used: .*DB_URL_REPORTS"):
        configured_sources()


STRUCTURED = """\
default: Auction_DM
datasources:
  Auction_DM:
    host: db1
    database: AuctionDM
    username: nlq
    password_env: Auction_DM_PASSWORD
"""


def _pin_dotenv_state(monkeypatch, path: str) -> None:
    """What ``config`` recorded at import depends on the runner (``PYTHON_DOTENV_DISABLED``, a real .env)."""
    monkeypatch.setattr(cfg, "_dotenv_path", path)
    monkeypatch.setattr(cfg, "_dotenv_disabled", False)
    monkeypatch.setattr(cfg, "_dotenv_missing", False)
    monkeypatch.setattr(cfg, "_dotenv_scan_unavailable", False)


def test_an_unset_password_says_what_the_dotenv_file_holds(project, monkeypatch, tmp_path_factory):
    # Issue #237: "is not set or is empty" alone, although the password was in .env.
    (project / "datasources.yaml").write_text(STRUCTURED, encoding="utf-8")
    dotenv = tmp_path_factory.mktemp("app") / ".env"
    dotenv.write_text("Auction_DM_PASSWORD=pw-secret\n", encoding="utf-8")
    monkeypatch.delenv("Auction_DM_PASSWORD", raising=False)
    _pin_dotenv_state(monkeypatch, str(dotenv))
    with pytest.raises(SourceSelectionError) as excinfo:
        configured_sources()
    text = str(excinfo.value)
    assert text.startswith(
        "data source 'Auction_DM' cannot be used: data source 'Auction_DM': environment variable "
        "Auction_DM_PASSWORD (named by password_env) is not set or is empty\n  "
    )
    assert f"{dotenv} line 1 assigns Auction_DM_PASSWORD, but Auction_DM_PASSWORD is not in the process environment" in text
    assert "pw-secret" not in text


def test_an_unset_password_says_python_dotenv_is_missing(project, monkeypatch):
    # Issue #237: python-dotenv absent from the environment, so .env was never read.
    (project / "datasources.yaml").write_text(STRUCTURED, encoding="utf-8")
    monkeypatch.delenv("Auction_DM_PASSWORD", raising=False)
    _pin_dotenv_state(monkeypatch, "")
    monkeypatch.setattr(cfg, "_dotenv_missing", True)
    with pytest.raises(SourceSelectionError) as excinfo:
        configured_sources()
    text = str(excinfo.value)
    assert text.startswith(
        "data source 'Auction_DM' cannot be used: data source 'Auction_DM': environment variable "
        "Auction_DM_PASSWORD (named by password_env) is not set or is empty\n  "
        "python-dotenv is not installed in this Python environment ("
    )
    assert sys.executable in text
    assert text.endswith("pip install -r requirements.lock")


def test_an_unset_connection_variable_says_no_dotenv_was_found(project, monkeypatch):
    (project / "datasources.yaml").write_text(TWO, encoding="utf-8")
    monkeypatch.delenv("DB_URL_REPORTS")
    _pin_dotenv_state(monkeypatch, "")
    with pytest.raises(SourceSelectionError, match=r"(?s)DB_URL_REPORTS.*\n  no \.env file was found"):
        configured_sources()


def test_without_a_file_the_one_implicit_source(project):
    assert datasources_file_in_use() is False
    with cfg.override_settings(db_connection_url="sqlite:///only.db"):
        sources, names, default = configured_sources()
    assert [s.name for s in sources] == names == [default] == ["default"]


def test_for_engine_leaves_a_borrowed_engine_open():
    engine = create_engine("sqlite://")
    inspector = SchemaInspector.for_engine(engine, label="sales (sqlite://)")
    assert inspector.inspect(fetch_row_counts=False).source_url == "sales (sqlite://)"
    inspector.close()
    with engine.connect() as conn:
        assert conn is not None
