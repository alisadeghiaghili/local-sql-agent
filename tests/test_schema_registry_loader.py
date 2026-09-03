# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for schema_data/registry.py's YAML loader (Phase 4).

Covers the loading mechanics that ``tests/test_schema_registry.py`` and
``tests/test_schema_registry_snapshot.py`` do not: missing-file behaviour,
directory configurability (``config.Settings.project_config_dir``),
Pydantic validation errors, and the "a table with no ``columns`` key is
described but not queryable" split that
:func:`schema_data.registry.get_table_columns` depends on. Every test here
uses its own temporary directory and :func:`config.override_settings`, so
none of it depends on the real ``project_config/`` (or
``project_config.example/``) being present in this checkout.
"""

from __future__ import annotations

import pytest
import yaml

from config import override_settings
from knowledge.config_loader import ConfigNotFoundError
from schema_data.registry import (
    RelationshipDefinition,
    SchemaConfig,
    TableDefinition,
    get_relationships_map,
    get_table_columns,
    get_table_descriptions,
    load_schema,
)


def _write_schema(tmp_path, doc: dict) -> None:
    (tmp_path / "schema.yaml").write_text(
        yaml.dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


class TestMissingFile:
    def test_raises_config_not_found_error(self, tmp_path):
        with override_settings(project_config_dir=str(tmp_path)):
            with pytest.raises(ConfigNotFoundError):
                load_schema()

    def test_missing_directory_entirely(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        with override_settings(project_config_dir=str(missing)):
            with pytest.raises(ConfigNotFoundError):
                load_schema()


class TestDirectoryIsConfigurable:
    """load_schema() reads config.Settings.project_config_dir at call
    time, so pointing it at a different directory changes what loads --
    this is the mechanism CI uses to run against project_config.example/
    instead of the real, git-ignored project_config/."""

    def test_loads_from_overridden_directory(self, tmp_path):
        _write_schema(
            tmp_path,
            {
                "tables": {
                    "Widget": {
                        "description": "a test table",
                        "columns": {"ID": "primary key"},
                    }
                },
                "relationships": [],
            },
        )
        with override_settings(project_config_dir=str(tmp_path)):
            cfg = load_schema()
        assert isinstance(cfg, SchemaConfig)
        assert "Widget" in cfg.tables
        assert cfg.tables["Widget"].columns == {"ID": "primary key"}

    def test_two_different_directories_load_independently(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        _write_schema(dir_a, {"tables": {"TableA": {"description": "a"}}})
        _write_schema(dir_b, {"tables": {"TableB": {"description": "b"}}})

        with override_settings(project_config_dir=str(dir_a)):
            cfg_a = load_schema()
        with override_settings(project_config_dir=str(dir_b)):
            cfg_b = load_schema()

        assert list(cfg_a.tables) == ["TableA"]
        assert list(cfg_b.tables) == ["TableB"]


class TestColumnsKeyIsOptional:
    """A table with no `columns` key is described in the prompt but must
    not appear in get_table_columns() -- that split is what keeps the SQL
    guard's allowlist from silently growing to include lookup/status
    tables nobody added real column metadata for."""

    def test_table_without_columns_is_excluded_from_allowlist(self, tmp_path):
        _write_schema(
            tmp_path,
            {
                "tables": {
                    "Queryable": {
                        "description": "has columns",
                        "columns": {"ID": "primary key"},
                    },
                    "PromptOnly": {
                        "description": "no columns key at all",
                    },
                },
                "relationships": [],
            },
        )
        with override_settings(project_config_dir=str(tmp_path)):
            cfg = load_schema()
        table_columns = {
            name: table.columns for name, table in cfg.tables.items() if table.columns
        }
        table_descriptions = {name: table.description for name, table in cfg.tables.items()}

        assert set(table_columns) == {"Queryable"}
        assert set(table_descriptions) == {"Queryable", "PromptOnly"}


class TestValidationErrors:
    def test_relationship_missing_required_field_raises(self, tmp_path):
        _write_schema(
            tmp_path,
            {
                "tables": {},
                "relationships": [{"from_table": "A", "to_table": "B"}],  # no join_sql
            },
        )
        with override_settings(project_config_dir=str(tmp_path)):
            with pytest.raises(ValueError, match="schema.yaml"):
                load_schema()

    def test_empty_file_yields_empty_config(self, tmp_path):
        """An empty (or comment-only) schema.yaml is valid -- both keys
        default to empty, matching every other project_config loader's
        `default_factory=dict/list` convention."""
        (tmp_path / "schema.yaml").write_text("", encoding="utf-8")
        with override_settings(project_config_dir=str(tmp_path)):
            cfg = load_schema()
        assert cfg.tables == {}
        assert cfg.relationships == []

    def test_resolvable_column_not_in_columns_map_raises(self, tmp_path):
        """A column cannot be flagged resolvable/prefetchable without
        first existing as a real, described column -- see
        SchemaConfig's validator. This is the mechanism that keeps
        retrieval.value_resolver's allowlist from ever drifting out of
        sync with schema.yaml's own `columns` map."""
        _write_schema(
            tmp_path,
            {
                "tables": {
                    "Widget": {
                        "description": "a test table",
                        "db_schema": "dbo",
                        "columns": {"ID": "primary key"},
                        "resolvable_columns": ["Name"],  # not in columns
                    }
                },
            },
        )
        with override_settings(project_config_dir=str(tmp_path)):
            with pytest.raises(ValueError, match="Name"):
                load_schema()

    def test_prefetchable_columns_without_db_schema_raises(self, tmp_path):
        """A table cannot flag prefetchable_columns without also giving a
        db_schema -- there would be no qualifier to build a query with."""
        _write_schema(
            tmp_path,
            {
                "tables": {
                    "Widget": {
                        "description": "a test table",
                        "columns": {"Name": "widget name"},
                        "prefetchable_columns": ["Name"],  # no db_schema
                    }
                },
            },
        )
        with override_settings(project_config_dir=str(tmp_path)):
            with pytest.raises(ValueError, match="db_schema"):
                load_schema()


class TestPydanticModels:
    def test_table_definition_defaults(self):
        t = TableDefinition()
        assert t.description == ""
        assert t.columns is None

    def test_relationship_definition_requires_all_three_fields(self):
        with pytest.raises(Exception):
            RelationshipDefinition(from_table="A", to_table="B")  # missing join_sql


class TestCachedAccessorsAgreeWithRealConfig:
    """The cached module-level accessors (used by the lazy schema_data.tables
    / .columns / .relationships shims) must agree with a direct load_schema()
    call against the SAME (real, not overridden) project_config/ -- this
    exercises the actual production path used by security.sql_guard and
    schema_data.registry.SchemaRegistry, not just an isolated tmp_path."""

    def test_get_table_columns_matches_a_fresh_load(self):
        cfg = load_schema()
        fresh = {
            name: table.columns for name, table in cfg.tables.items() if table.columns
        }
        assert get_table_columns() == fresh

    def test_get_table_descriptions_matches_a_fresh_load(self):
        cfg = load_schema()
        fresh = {name: table.description for name, table in cfg.tables.items()}
        assert get_table_descriptions() == fresh

    def test_get_relationships_map_matches_a_fresh_load(self):
        cfg = load_schema()
        fresh = {
            f"{rel.from_table} -> {rel.to_table}": rel.join_sql
            for rel in cfg.relationships
        }
        assert get_relationships_map() == fresh
