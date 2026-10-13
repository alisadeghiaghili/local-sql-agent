# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Drafts written by ``database.schema_inspector`` and its CLI.

Pins three things:

* A snapshot with the same table name in several schemas yields a
  ``schema.yaml`` draft the strict loader and ``SchemaConfig`` accept: one
  key per table (bare when the name is unique, ``Schema.Table`` for every
  occurrence of a shared name), ``db_schema`` on every table, and
  relationship ends spelled like the keys. ``entities.yaml`` follows the
  same naming.
* ``--exclude-schemas`` (fnmatch, case-insensitive, wins over
  ``--include-schemas``) reaches the inspector's table discovery.
* The CLI ends a run with a per-schema table count, the first-install note
  and, above 150 tables, a warning.

No database is opened: the snapshot is built by hand and the SQLAlchemy
inspector is replaced by a small fake.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.yaml_loading import safe_load_strict
from database import schema_inspector_cli
from database.schema_inspector import (
    ColumnInfo,
    RelationshipInfo,
    SchemaInspector,
    SchemaSnapshot,
    TableInfo,
    schema_is_excluded,
)
from schema_data.registry import validate_schema_yaml_text


def _table(name: str, schema: str | None, *cols: str) -> TableInfo:
    return TableInfo(
        name=name,
        schema=schema,
        columns=[
            ColumnInfo(name=c, type="int", is_pk=(i == 0))
            for i, c in enumerate(cols or ("Id",))
        ],
        row_count=3,
    )


def _snapshot() -> SchemaSnapshot:
    """``Trade`` in three schemas, ``Customer`` in one, a cross-schema FK."""
    return SchemaSnapshot(
        tables=[
            _table("Trade", "sales", "Id", "CustomerId"),
            _table("Trade", "ref", "Id"),
            _table("Trade", "staging", "Id"),
            _table("Customer", "sales", "Id"),
        ],
        relationships=[
            RelationshipInfo("Trade", "sales", "CustomerId", "Customer", "sales", "Id"),
            RelationshipInfo("Trade", "staging", "Id", "Trade", "ref", "Id"),
        ],
        source_url="mssql://host/db",
    )


def _schema_draft(snapshot: SchemaSnapshot) -> str:
    return SchemaInspector("sqlite://").draft_schema_yaml(snapshot)


class TestSchemaDraftIsValid:
    def test_same_name_in_two_schemas_passes_the_registry_validator(self):
        snap = SchemaSnapshot(tables=[_table("Trade", "sales"), _table("Trade", "ref")])

        config = validate_schema_yaml_text(_schema_draft(snap))

        assert list(config.tables) == ["sales.Trade", "ref.Trade"]
        assert config.tables["sales.Trade"].db_schema == "sales"
        assert config.tables["ref.Trade"].db_schema == "ref"

    def test_unique_name_stays_bare_and_still_gets_db_schema(self):
        config = validate_schema_yaml_text(_schema_draft(_snapshot()))

        assert "Customer" in config.tables
        assert config.tables["Customer"].db_schema == "sales"
        assert "sales.Customer" not in config.tables

    def test_every_occurrence_of_a_shared_name_is_qualified(self):
        config = validate_schema_yaml_text(_schema_draft(_snapshot()))

        assert sorted(config.tables) == [
            "Customer",
            "ref.Trade",
            "sales.Trade",
            "staging.Trade",
        ]
        assert "Trade" not in config.tables

    def test_relationship_ends_are_spelled_like_the_keys(self):
        config = validate_schema_yaml_text(_schema_draft(_snapshot()))

        ends = [(r.from_table, r.to_table) for r in config.relationships]
        assert ends == [
            ("sales.Trade", "Customer"),
            ("staging.Trade", "ref.Trade"),
        ]
        keys = set(config.tables)
        assert all(a in keys and b in keys for a, b in ends)

    def test_names_differing_only_by_case_are_treated_as_shared(self):
        snap = SchemaSnapshot(tables=[_table("Trade", "sales"), _table("TRADE", "ref")])

        config = validate_schema_yaml_text(_schema_draft(snap))

        assert sorted(config.tables) == ["ref.TRADE", "sales.Trade"]

    def test_a_name_with_a_dot_is_bracket_quoted_and_still_valid(self):
        snap = SchemaSnapshot(
            tables=[_table("a.b", "sales"), _table("a.b", "ref"), _table("c.d", "sales")]
        )

        config = validate_schema_yaml_text(_schema_draft(snap))

        assert sorted(config.tables) == ["[c.d]", "[ref].[a.b]", "[sales].[a.b]"]

    def test_a_dotted_schema_name_is_carried_by_the_key_not_db_schema(self):
        snap = SchemaSnapshot(tables=[_table("Trade", "a.b"), _table("Trade", "ref")])

        config = validate_schema_yaml_text(_schema_draft(snap))

        assert config.tables["[a.b].[Trade]"].db_schema == ""
        assert config.tables["ref.Trade"].db_schema == "ref"

    def test_relationship_to_a_table_outside_the_snapshot_is_qualified(self):
        snap = SchemaSnapshot(
            tables=[_table("Trade", "sales")],
            relationships=[
                RelationshipInfo("Trade", "sales", "OtherId", "Other", "excluded", "Id")
            ],
        )

        config = validate_schema_yaml_text(_schema_draft(snap))

        assert [(r.from_table, r.to_table) for r in config.relationships] == [
            ("Trade", "excluded.Other")
        ]

    def test_a_snapshot_with_no_schema_still_yields_valid_bare_keys(self):
        snap = SchemaSnapshot(tables=[_table("Trade", None), _table("Customer", None)])

        config = validate_schema_yaml_text(_schema_draft(snap))

        assert sorted(config.tables) == ["Customer", "Trade"]
        assert all(t.db_schema == "" for t in config.tables.values())

    def test_a_table_without_columns_is_still_listed_with_db_schema(self):
        snap = SchemaSnapshot(
            tables=[
                TableInfo(name="Lookup", schema="ref"),
                TableInfo(name="Lookup", schema="sales"),
            ]
        )

        config = validate_schema_yaml_text(_schema_draft(snap))

        assert config.tables["ref.Lookup"].columns is None
        assert config.tables["ref.Lookup"].db_schema == "ref"


class TestEntitiesDraft:
    def test_entity_names_are_disambiguated_and_have_no_duplicate_keys(self):
        text = SchemaInspector("sqlite://").draft_entities_yaml(_snapshot())

        # safe_load_strict refuses a repeated key at any level.
        entities = safe_load_strict(text)["entities"]

        assert sorted(entities) == [
            "Customer",
            "ref.Trade",
            "sales.Trade",
            "staging.Trade",
        ]
        assert entities["sales.Trade"]["table"] == "Trade"
        assert entities["sales.Trade"]["schema"] == "sales"

    def test_a_bracketed_entity_name_is_quoted_so_the_file_parses(self):
        snap = SchemaSnapshot(tables=[_table("a.b", "sales"), _table("a.b", "ref")])

        text = SchemaInspector("sqlite://").draft_entities_yaml(snap)

        assert sorted(safe_load_strict(text)["entities"]) == ["[ref].[a.b]", "[sales].[a.b]"]


class _FakeSqlaInspector:
    """The slice of SQLAlchemy's ``Inspector`` that ``inspect`` uses."""

    def __init__(self, tables_by_schema: dict[str, list[str]]) -> None:
        self._tables = tables_by_schema

    def get_schema_names(self):
        return list(self._tables)

    def get_table_names(self, schema=None):
        return list(self._tables[schema])

    def get_pk_constraint(self, table, schema=None):
        return {"constrained_columns": ["Id"]}

    def get_columns(self, table, schema=None):
        return [{"name": "Id", "type": "INTEGER", "nullable": False}]

    def get_foreign_keys(self, table, schema=None):
        return []


def _discover(tables_by_schema, **kwargs) -> list[str]:
    """Run ``SchemaInspector.inspect`` against a fake catalogue."""
    inspector = SchemaInspector("sqlite://", sample_rows=0)
    with patch.object(SchemaInspector, "_get_engine", return_value=object()), patch(
        "database.schema_inspector.sa_inspect",
        return_value=_FakeSqlaInspector(tables_by_schema),
    ):
        snapshot = inspector.inspect(fetch_row_counts=False, **kwargs)
    return [t.full_name for t in snapshot.tables]


_CATALOGUE = {
    "sales": ["Trade"],
    "Sales_Stg": ["Trade"],
    "ref": ["Currency"],
    "Replay": ["Trade"],
    "Alerts": ["Hit"],
}


class TestExcludeSchemasPatterns:
    def test_pattern_is_case_insensitive(self):
        assert schema_is_excluded("Sales_Stg", ["*_stg"])
        assert schema_is_excluded("sales_stg", ["*_STG"])

    def test_plain_name_matches_only_itself(self):
        assert schema_is_excluded("REPLAY", ["Replay"])
        assert not schema_is_excluded("Replayed", ["Replay"])

    def test_no_patterns_exclude_nothing(self):
        assert not schema_is_excluded("sales", None)
        assert not schema_is_excluded("sales", [])


class TestExcludeSchemasInDiscovery:
    def test_without_the_option_every_schema_is_read(self):
        assert len(_discover(_CATALOGUE)) == 5

    def test_patterns_skip_matching_schemas(self):
        found = _discover(_CATALOGUE, exclude_schemas=["*_Stg", "replay", "Alerts"])

        assert found == ["sales.Trade", "ref.Currency"]

    def test_exclude_wins_over_include(self):
        found = _discover(
            _CATALOGUE,
            include_schemas=["sales", "Sales_Stg", "ref"],
            exclude_schemas=["*_stg"],
        )

        assert found == ["sales.Trade", "ref.Currency"]

    def test_system_schemas_stay_filtered(self):
        found = _discover(
            {"sys": ["x"], "information_schema": ["y"], "sales": ["Trade"]},
            exclude_schemas=["nothing"],
        )

        assert found == ["sales.Trade"]


class _FakeInspector:
    """Stands in for ``SchemaInspector`` inside the CLI; records ``inspect``."""

    instances: list["_FakeInspector"] = []
    snapshot: SchemaSnapshot = SchemaSnapshot()

    def __init__(self, db_url: str, sample_rows: int = 10) -> None:
        self.inspect_kwargs: dict = {}
        type(self).instances.append(self)

    def inspect(self, **kwargs):
        self.inspect_kwargs = kwargs
        return type(self).snapshot

    def close(self) -> None:
        pass

    def draft_entities_yaml(self, snapshot):
        return "entities: {}\n"

    draft_aliases_yaml = draft_relationships_yaml = draft_schema_yaml = draft_entities_yaml


def _run_cli(tmp_path, capsys, tables: list[TableInfo], *extra: str):
    _FakeInspector.instances = []
    _FakeInspector.snapshot = SchemaSnapshot(tables=tables)
    argv = ["--db-url", "sqlite://", "--output-dir", str(tmp_path / "draft"), *extra]
    with patch("database.schema_inspector.SchemaInspector", _FakeInspector):
        code = schema_inspector_cli.main(argv)
    return code, capsys.readouterr().err, _FakeInspector.instances[0]


class TestCliExcludeSchemasOption:
    def test_comma_separated_patterns_reach_the_inspector(self, tmp_path, capsys):
        code, _, inspector = _run_cli(
            tmp_path,
            capsys,
            [_table("T", "sales")],
            "--exclude-schemas",
            "*_Stg, Replay ,,Alerts",
        )

        assert code == 0
        assert inspector.inspect_kwargs["exclude_schemas"] == ["*_Stg", "Replay", "Alerts"]

    def test_absent_option_passes_none(self, tmp_path, capsys):
        _, _, inspector = _run_cli(tmp_path, capsys, [_table("T", "sales")])

        assert inspector.inspect_kwargs["exclude_schemas"] is None

    def test_help_shows_the_option_and_an_example(self, capsys):
        with pytest.raises(SystemExit):
            schema_inspector_cli.main(["--help"])

        out = capsys.readouterr().out
        assert "--exclude-schemas" in out
        assert '--exclude-schemas "*_Stg,Replay,Alerts"' in out
        assert "sync_schema.py --add-tables" in out


class TestCliRunSummary:
    def test_prints_count_per_schema_and_total(self, tmp_path, capsys):
        tables = [_table("A", "sales"), _table("B", "sales"), _table("C", "ref")]

        _, err, _ = _run_cli(tmp_path, capsys, tables)

        assert "Tables per schema:" in err
        assert "  ref: 1" in err
        assert "  sales: 2" in err
        assert "  Total: 3" in err

    def test_says_drafts_are_for_a_first_install(self, tmp_path, capsys):
        _, err, _ = _run_cli(tmp_path, capsys, [_table("A", "sales")])

        assert "for a first install only" in err
        assert "python scripts/sync_schema.py --add-tables 'Schema.*'" in err
        assert "keeps the curated text" in err

    def test_no_warning_at_exactly_150_tables(self, tmp_path, capsys):
        tables = [_table(f"T{i}", "sales") for i in range(150)]

        _, err, _ = _run_cli(tmp_path, capsys, tables)

        assert "WARNING" not in err

    def test_warns_above_150_tables(self, tmp_path, capsys):
        tables = [_table(f"T{i}", "sales") for i in range(151)]

        _, err, _ = _run_cli(tmp_path, capsys, tables)

        assert "WARNING: 151 tables were written (more than 150)" in err

    def test_summary_goes_to_stderr_not_stdout(self, tmp_path, capsys):
        _FakeInspector.snapshot = SchemaSnapshot(tables=[_table("A", "sales")])

        with patch("database.schema_inspector.SchemaInspector", _FakeInspector):
            schema_inspector_cli.main(
                ["--db-url", "sqlite://", "--output-dir", str(tmp_path / "d"), "--dry-run"]
            )

        captured = capsys.readouterr()
        assert "Tables per schema" in captured.err
        assert "Tables per schema" not in captured.out
