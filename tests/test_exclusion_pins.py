# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""What the discovery tools skip today, pinned before table-level exclusion.

``exclude_tables:`` and a way to keep a staging schema while dropping its
change-capture tables are added next to the rules below. These tests say what
every existing rule does, and pass before and after:

* the default ``STAGING_PATTERNS`` (schema ``*_Stg`` / ``*stg*`` / ``*staging*``
  and tables ``*_Inserts`` / ``*_Updates`` / ``*_Deletes``; the singular
  ``_Insert`` is not recognised) and ``--include-staging``;
* the draft selection (``database.schema_draft.select_tables``) and its report
  line, rule by rule and in this order: system, configured, cli, chosen,
  not-included, exclude-tables (bare names, any schema), staging;
* the wizard's plan (``setup_project._plan_reading`` / ``_skip_tables``) and its
  one-sentence report;
* the sync: a table in an excluded schema is neither listed nor added, a table
  ``schema.yaml`` already lists is synced in any schema, the report line;
* ``datasources.yaml`` refuses a key it does not know, and the settings'
  defaults.
"""

from __future__ import annotations

import config as cfg
from core import config_doctor as doctor
from database.catalogue import TableInfo as CatalogueTable
from database.datasources import validate_datasources_yaml_text
from database.schema_draft import describe_skipped, select_tables
from database.schema_filter import SchemaExclusion
from schema_data.sync import SyncOptions, build_report, sync_schema_text
from tests._sync_fixtures import catalogue, tbl

DEFAULT_STAGING = "*_Stg,*stg*,*staging*,table:*_Inserts,table:*_Updates,table:*_Deletes"


def _catalogue(*pairs: tuple[str, str]) -> dict[tuple[str, str], CatalogueTable]:
    return {
        (schema.lower(), name.lower()): tbl(schema, name, [("ID", "int")]) for schema, name in pairs
    }


WAREHOUSE = _catalogue(
    ("sales", "Trade"), ("sales", "Trade_Inserts"), ("sales", "Trade_Insert"), ("sales", "Order"),
    ("X_Stg", "Trade"), ("X_Stg", "Trade_Insert"), ("X_Stg", "Trade_Updates"),
    ("stg", "Item"), ("Staging_Area", "Item"),
    ("ref", "Broker"), ("Scratch", "Tmp"), ("cdc", "Change"),
)


def _kept(result) -> list[str]:
    return sorted(f"{info.schema}.{info.name}" for info in result[0].values())


class TestTheDefaultsDoNotMove:
    def test_the_staging_patterns_default(self):
        assert cfg.Settings().staging_patterns == DEFAULT_STAGING

    def test_exclude_schemas_default_is_empty(self):
        assert cfg.Settings().exclude_schemas == ""

    def test_staging_patterns_parse_to_schema_and_table_patterns(self):
        parsed = doctor.parse_staging_patterns(DEFAULT_STAGING)
        assert [(p.pattern, p.on_table) for p in parsed] == [
            ("*_stg", False), ("*stg*", False), ("*staging*", False),
            ("*_inserts", True), ("*_updates", True), ("*_deletes", True),
        ]

    def test_the_singular_change_capture_names_are_not_recognised_by_default(self):
        patterns = doctor.parse_staging_patterns(DEFAULT_STAGING)
        assert doctor.is_staging("sales", "Trade_Inserts", patterns)
        assert not doctor.is_staging("sales", "Trade_Insert", patterns)
        assert not doctor.is_staging("sales", "Trade_Update", patterns)
        assert not doctor.is_staging("sales", "Trade_Delete", patterns)


class TestDraftSelection:
    STAGING = doctor.parse_staging_patterns(DEFAULT_STAGING)

    def test_staging_drops_a_staging_schema_whole_and_the_plural_change_tables(self):
        kept, skipped = select_tables(WAREHOUSE, exclusion=SchemaExclusion(), staging=self.STAGING)
        assert _kept((kept,)) == [
            "Scratch.Tmp", "ref.Broker", "sales.Order", "sales.Trade", "sales.Trade_Insert",
        ]
        assert skipped.tables == {"system": 1, "staging": 6}

    def test_include_staging_keeps_every_staging_table(self):
        kept, skipped = select_tables(WAREHOUSE, exclusion=SchemaExclusion(), staging=())
        assert "X_Stg.Trade" in _kept((kept,)) and "sales.Trade_Inserts" in _kept((kept,))
        assert skipped.tables == {"system": 1}

    def test_the_schema_exclusion_rules_and_their_order(self):
        exclusion = SchemaExclusion(configured=("Scratch",), cli=("ref", "scratch"))
        kept, skipped = select_tables(WAREHOUSE, exclusion=exclusion)
        assert "Scratch.Tmp" not in _kept((kept,)) and "ref.Broker" not in _kept((kept,))
        # Scratch matches both lists: it is counted under the first, "configured".
        assert skipped.tables == {"system": 1, "configured": 1, "cli": 1}

    def test_include_schemas_keeps_only_those(self):
        kept, skipped = select_tables(WAREHOUSE, exclusion=SchemaExclusion(), include_schemas=["SALES"])
        assert _kept((kept,)) == ["sales.Order", "sales.Trade", "sales.Trade_Insert", "sales.Trade_Inserts"]
        assert skipped.tables["not-included"] == 7

    def test_exclude_tables_are_bare_names_in_any_schema(self):
        kept, skipped = select_tables(WAREHOUSE, exclusion=SchemaExclusion(), exclude_tables=["trade", "Item"])
        names = _kept((kept,))
        assert not any(n.lower().endswith((".trade", ".item")) for n in names)
        assert skipped.tables["exclude-tables"] == 4

    def test_the_rule_order_is_system_configured_cli_not_included_exclude_tables_staging(self):
        # X_Stg.Trade is excluded by name, outside --include-schemas AND staging: the first rule counts.
        _, skipped = select_tables(
            _catalogue(("X_Stg", "Trade")), exclusion=SchemaExclusion(),
            staging=self.STAGING, include_schemas=["sales"], exclude_tables=["Trade"],
        )
        assert skipped.tables == {"not-included": 1}
        _, skipped = select_tables(
            _catalogue(("X_Stg", "Trade")), exclusion=SchemaExclusion(),
            staging=self.STAGING, exclude_tables=["Trade"],
        )
        assert skipped.tables == {"exclude-tables": 1}

    def test_the_report_lines(self):
        _, skipped = select_tables(WAREHOUSE, exclusion=SchemaExclusion(configured=("Scratch",)), staging=self.STAGING)
        assert describe_skipped({"main": skipped}) == [
            "  system schemas: 1 table(s) in 1 schema(s)",
            "  configured exclusion (EXCLUDE_SCHEMAS / exclude_schemas:): 1 table(s) in 1 schema(s)",
            "  staging (STAGING_PATTERNS; --include-staging keeps them): 6 table(s) in 4 schema(s)",
        ]


class TestWizardPlan:
    def test_a_staging_schema_is_not_read_and_a_change_table_is_passed_as_an_excluded_name(self):
        from setup_project import _plan_reading

        listing = {"sales": ["Trade", "Trade_Inserts", "Trade_Insert"], "X_Stg": ["Trade", "Other"], "ref": ["Region"]}
        plan = _plan_reading(
            listing, None, SchemaExclusion(cli=("ref",)), doctor.parse_staging_patterns(DEFAULT_STAGING),
        )
        assert plan.schemas == ["sales"]
        # Trade_Inserts is the only change-capture name; the singular Trade_Insert is read.
        assert plan.exclude_tables == ["Trade_Inserts"]
        assert plan.report.describe() == (
            "Skipped 4 of 6 tables: 1 in excluded schemas, "
            "3 staging or change-capture (STAGING_PATTERNS; --include-staging keeps them)."
        )

    def test_staging_kept_with_include_staging(self):
        from setup_project import _plan_reading

        plan = _plan_reading({"X_Stg": ["Trade"], "sales": ["Order"]}, None, SchemaExclusion(), [])
        assert sorted(plan.schemas) == ["X_Stg", "sales"] and plan.exclude_tables == []

    def test_a_shared_bare_name_is_read_and_dropped_afterwards(self):
        from types import SimpleNamespace as NS

        from setup_project import SkipReport, _skip_tables

        snap = NS(
            tables=[NS(name="Trade_Inserts", schema="sales"), NS(name="Trade_Inserts", schema="ref"), NS(name="T", schema="ref")],
            relationships=[],
        )
        report = SkipReport(found=3)
        _skip_tables(snap, [], doctor.parse_staging_patterns(DEFAULT_STAGING), report)
        assert [t.name for t in snap.tables] == ["T"] and report.staging == 2


SCHEMA = """\
tables:
  sales.Trade:
    description: "One row per trade"
    columns:
      TradeID: "Primary key"
  old_stage.Kept:
    description: "Listed by hand, in a schema nobody scans"
    columns:
      ID: "Primary key"
"""
DB = catalogue(
    tbl("sales", "Trade", [("TradeID", "int")], pk=["TradeID"]),
    tbl("sales", "Order", [("ID", "int")]),
    tbl("X_Stg", "Item", [("ID", "int")]),
    tbl("old_stage", "Kept", [("ID", "int"), ("Extra", "int")]),
    tbl("old_stage", "Other", [("ID", "int")]),
)


def _sync(**options):
    return sync_schema_text(SCHEMA, ["main"], "main", {"main": DB}, SyncOptions(**options))


class TestSync:
    def test_a_table_in_an_excluded_schema_is_neither_listed_nor_added(self):
        result = _sync(add_tables=("*",), exclude_schemas={"main": ("X_Stg", "old_stage")})
        assert [n.key for n in result.plan.new_tables] == ["Order"]
        assert sorted(result.plan.excluded) == [("X_Stg", "Item"), ("old_stage", "Other")]
        assert "X_Stg.Item" not in result.text

    def test_a_table_schema_yaml_lists_is_synced_in_an_excluded_schema(self):
        result = _sync(exclude_schemas={"main": ("old_stage",)})
        assert "Extra" in result.text  # the column of the listed table is still added

    def test_without_exclusion_every_candidate_is_listed(self):
        result = _sync()
        assert sorted(name for name, _ in result.plan.unlisted) == ["X_Stg.Item", "old_stage.Other", "sales.Order"]

    def test_the_report_counts_excluded_tables_per_schema(self):
        report = build_report(_sync(exclude_schemas={"main": ("X_Stg", "old_stage")}))
        assert (
            "== tables in excluded schemas, not listed or added "
            "(EXCLUDE_SCHEMAS, exclude_schemas:, --exclude-schemas): 2 =="
        ) in report
        assert "X_Stg: 1" in report and "old_stage: 1" in report


class TestDatasourcesYaml:
    def test_an_unknown_key_is_still_refused(self):
        text = "datasources:\n  main:\n    url_env: DB_URL_MAIN\n    exclude_table: [x]\n"
        try:
            validate_datasources_yaml_text(text)
        except ValueError as exc:
            assert "exclude_table" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("a misspelt key must be refused")

    def test_exclude_schemas_is_the_only_exclusion_key_today(self):
        text = "datasources:\n  main:\n    url_env: DB_URL_MAIN\n    exclude_schemas: [Scratch]\n"
        assert validate_datasources_yaml_text(text).datasources["main"].exclude_schemas == ["Scratch"]
