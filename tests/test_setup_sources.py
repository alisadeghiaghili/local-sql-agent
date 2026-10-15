# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``database.setup_sources``: which data sources a setup tool reads, and how it names them."""

from __future__ import annotations

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
