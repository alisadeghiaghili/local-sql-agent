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
from tests._inspector_fixtures import attached, build_database


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


class TestDraftsQuoteTheValuesTheyQuote:
    """Sample values come from live rows: a ``"`` or a ``\\`` in one must not break a draft.

    ``schema.yaml``'s draft always went through ``_yaml_str``; the entities,
    aliases and relationships drafts wrote ``"{value}"`` by hand, so the first
    value like ``Pipe 5" steel`` made the file unreadable.
    """

    @staticmethod
    def _snapshot_with_awkward_values() -> SchemaSnapshot:
        table = TableInfo(
            name='Odd Name', schema="sales",
            columns=[
                ColumnInfo(name='Col"umn', type="varchar", sample_values=['Pipe 5" steel', "Path\\to", "شرکت نمونه"]),
            ],
            foreign_keys=[],
        )
        other = _table("Plain", "sales")
        return SchemaSnapshot(
            tables=[table, other],
            relationships=[RelationshipInfo('Odd Name', "sales", 'Col"umn', "Plain", "sales", "Id")],
            source_url="mssql://host/db",
        )

    def test_entities_draft_parses_and_keeps_the_values(self):
        text = SchemaInspector("sqlite://").draft_entities_yaml(self._snapshot_with_awkward_values())

        entity = safe_load_strict(text)["entities"]['Odd Name']

        assert entity["table"] == 'Odd Name'
        (column,) = entity["columns"]
        assert column["name"] == 'Col"umn'
        assert column["sample_values"] == ['Pipe 5" steel', "Path\\to", "شرکت نمونه"]

    def test_aliases_draft_parses_and_keeps_the_values(self):
        text = SchemaInspector("sqlite://").draft_aliases_yaml(self._snapshot_with_awkward_values())

        synonyms = safe_load_strict(text)["synonyms"]

        assert set(synonyms) == {'pipe 5" steel', "path\\to", "شرکت نمونه"}
        assert sorted(synonyms["path\\to"]) == ['col"umn', "odd name"]

    def test_relationships_draft_without_any_foreign_key_is_an_empty_list_not_null(self):
        # A warehouse that declares no foreign key at all; `relationships:` alone is null.
        snap = SchemaSnapshot(tables=[_table("A", "sales")], source_url="mssql://host/db")

        text = SchemaInspector("sqlite://").draft_relationships_yaml(snap)

        assert safe_load_strict(text) == {"relationships": []}

    def test_relationships_draft_parses_and_keeps_the_values(self):
        text = SchemaInspector("sqlite://").draft_relationships_yaml(self._snapshot_with_awkward_values())

        (entry,) = safe_load_strict(text)["relationships"]

        assert entry["from_table"] == 'Odd Name'
        assert entry["from_column"] == 'Col"umn'


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


def _run_cli(tmp_path, capsys, layout, *extra: str):
    """Run the CLI with ``--db-url`` on a database built from *layout*; ``(code, stderr, output dir)``."""
    database = build_database(tmp_path, "db", layout)
    out = tmp_path / "draft"
    with attached(database):
        code = schema_inspector_cli.main(["--db-url", database.url, "--output-dir", str(out), *extra])
    return code, capsys.readouterr().err, out


def _tables_in(*schemas: str, count: int = 1) -> dict:
    return {s: {f"T{i}": None for i in range(count)} for s in schemas}


class TestCliExcludeSchemasOption:
    def test_comma_separated_patterns_skip_schemas(self, tmp_path, capsys):
        layout = {"sales": {"T": None}, "Sales_Stg": {"T": None}, "Replay": {"T": None}, "Alerts": {"H": None}}

        code, err, out = _run_cli(tmp_path, capsys, layout, "--include-staging", "--exclude-schemas", "*_Stg, Replay ,,Alerts")

        assert code == 0
        assert list(validate_schema_yaml_text((out / "schema.yaml").read_text(encoding="utf-8")).tables) == ["T"]
        assert "--exclude-schemas: 3 table(s) in 3 schema(s)" in err

    def test_absent_option_keeps_every_user_schema(self, tmp_path, capsys):
        _, err, out = _run_cli(tmp_path, capsys, {"sales": {"T": None}, "ref": {"R": None}})

        assert sorted(validate_schema_yaml_text((out / "schema.yaml").read_text(encoding="utf-8")).tables) == ["R", "T"]
        assert "--exclude-schemas" not in err

    def test_help_shows_the_options_and_an_example(self, capsys):
        with pytest.raises(SystemExit):
            schema_inspector_cli.main(["--help"])

        out = capsys.readouterr().out
        for flag in ("--exclude-schemas", "--datasource", "--include-staging", "--sample-values", "--db-url"):
            assert flag in out
        assert '--exclude-schemas "*_Stg,Replay,Alerts"' in out
        assert "sync_schema.py --add-tables" in out


class TestCliRunSummary:
    def test_prints_count_per_schema_and_total(self, tmp_path, capsys):
        layout = {"sales": {"A": None, "B": None}, "ref": {"C": None}}

        _, err, _ = _run_cli(tmp_path, capsys, layout)

        assert "Tables per schema:" in err
        assert "  ref: 1" in err
        assert "  sales: 2" in err
        assert "  Total: 3" in err

    def test_says_drafts_are_for_a_first_install(self, tmp_path, capsys):
        _, err, _ = _run_cli(tmp_path, capsys, {"sales": {"A": None}})

        assert "for a first install only" in err
        assert "python scripts/sync_schema.py --add-tables 'Schema.*'" in err
        assert "keeps the curated text" in err

    def test_no_warning_at_exactly_150_tables(self, tmp_path, capsys):
        _, err, _ = _run_cli(tmp_path, capsys, _tables_in("sales", count=150), "--no-row-counts")

        assert "WARNING" not in err

    def test_warns_above_150_tables(self, tmp_path, capsys):
        _, err, _ = _run_cli(tmp_path, capsys, _tables_in("sales", count=151), "--no-row-counts")

        assert "WARNING: 151 tables were written (more than 150)" in err

    def test_summary_goes_to_stderr_not_stdout(self, tmp_path, capsys):
        database = build_database(tmp_path, "db", {"sales": {"A": None}})
        with attached(database):
            schema_inspector_cli.main(["--db-url", database.url, "--output-dir", str(tmp_path / "d"), "--dry-run"])

        captured = capsys.readouterr()
        assert "Tables per schema" in captured.err
        assert "Tables per schema" not in captured.out
        assert "tables:" in captured.out
