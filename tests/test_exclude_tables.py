# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Table-level exclusion and kept staging schemas for the setup tools.

The operator's request, with generic names: for one database skip the schemas
``Meta, Audit_Stg, Old, dbo``; from the staging schema ``X_Stg`` keep every
table except those ending ``_Insert`` / ``_Update`` / ``_Delete``; skip the one
table ``X_Fact.Order``. Today ``exclude_schemas:`` covers whole schemas and
``STAGING_PATTERNS`` drops a staging schema and its change-capture tables
together. Added, with the existing behaviour pinned in
``tests/test_exclusion_pins.py``:

* ``exclude_tables:`` per source and ``EXCLUDE_TABLES`` for every source:
  ``fnmatch`` on ``schema.table``, case-insensitive;
* ``keep_staging_schemas:`` per source: a schema the *schema* patterns of
  ``STAGING_PATTERNS`` do not drop.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import config as cfg
from core import config_doctor as doctor
from database.catalogue import TableInfo as CatalogueTable
from database.datasources import (
    DataSourceDefinition,
    datasource_exclude_tables,
    datasource_keep_staging_schemas,
    reset_datasources_cache,
    validate_datasources_yaml_text,
)
from database.datasources_edit import (
    DatasourcesEditError,
    add_exclude_tables,
    exclude_tables_snippet,
    record_exclude_tables,
)
from database.schema_draft import describe_skipped, select_tables
from database.schema_filter import (
    SchemaExclusion,
    exclude_tables_env_line,
    schema_exclusion,
    table_exclusion_patterns,
    table_is_excluded,
)
from schema_data.sync import SyncOptions, build_report, sync_schema_text
from tests._sync_fixtures import catalogue, tbl

STAGING = doctor.parse_staging_patterns("*_Stg,*stg*,*staging*,table:*_Inserts,table:*_Updates,table:*_Deletes")

YAML = """\
default: main
datasources:
  main:
    url_env: DB_URL_MAIN
    exclude_schemas: [Meta, Audit_Stg, Old, dbo]
    keep_staging_schemas: [X_Stg]
    exclude_tables: ["X_Stg.*_Insert", "X_Stg.*_Update", "X_Stg.*_Delete", "X_Fact.Order"]
  other:
    url_env: DB_URL_OTHER
    exclude_schemas: [dbo, Meta, Alerts]
    keep_staging_schemas: [stg]
    exclude_tables: ["stg.*_Insert", "stg.*_Update", "stg.*_Delete"]
"""


@pytest.fixture(autouse=True)
def _fresh_datasources():
    reset_datasources_cache()
    yield
    reset_datasources_cache()


def _project(tmp_path: Path) -> Path:
    (tmp_path / "datasources.yaml").write_text(YAML, encoding="utf-8")
    return tmp_path


def _catalogue(*pairs: tuple[str, str]) -> dict[tuple[str, str], CatalogueTable]:
    return {(s.lower(), n.lower()): tbl(s, n, [("ID", "int")]) for s, n in pairs}


def _kept(kept) -> list[str]:
    return sorted(f"{i.schema}.{i.name}" for i in kept.values())


MAIN = _catalogue(
    ("dbo", "Anything"), ("Meta", "Dict"), ("Audit_Stg", "Log"), ("Old", "Archive"),
    ("X_Stg", "Trade"), ("X_Stg", "Trade_Insert"), ("X_Stg", "Trade_Update"), ("X_Stg", "Trade_Delete"),
    ("X_Stg", "Trade_Inserts"), ("X_Stg", "Item"),
    ("X_Fact", "Order"), ("X_Fact", "Trade"), ("Y_Stg", "Tmp"), ("sales", "Customer"),
)


class TestThePatternRule:
    @pytest.mark.parametrize("schema, table, pattern", [
        ("X_Fact", "Order", "X_Fact.Order"),
        ("x_fact", "ORDER", "x_FACT.order"),
        ("X_Stg", "Trade_Delete", "X_Stg.*_Delete"),
        ("sales", "Order", "*.Order"),
        ("ref", "order", "Order"),                 # no dot: the table name alone, in any schema
        ("sales", "Trade_Insert", "*_Insert"),
        ("", "Order", "Order"),                    # a database with no schema
    ])
    def test_matches(self, schema, table, pattern):
        assert table_is_excluded(schema, table, [pattern])

    @pytest.mark.parametrize("schema, table, pattern", [
        ("sales", "Order", "X_Fact.Order"),
        ("X_Fact", "Order_Lines", "X_Fact.Order"),
        ("X_Fact", "Order", "X_Fact.Ord"),
        ("sales", "Order_Lines", "Order"),
        ("X_Stg", "Trade_Inserts", "X_Stg.*_Insert"),   # the plural is another name
    ])
    def test_does_not_match(self, schema, table, pattern):
        assert not table_is_excluded(schema, table, [pattern])

    def test_nothing_configured_excludes_nothing(self):
        assert not table_is_excluded("sales", "Order", None)
        assert not table_is_excluded("sales", "Order", [])


class TestConfiguration:
    def test_the_setting_defaults_to_empty_and_reads_the_environment(self, monkeypatch):
        monkeypatch.delenv("EXCLUDE_TABLES", raising=False)
        assert cfg.Settings().exclude_tables == ""
        monkeypatch.setenv("EXCLUDE_TABLES", "*.Order,X_Stg.*_Delete")
        assert cfg.Settings().exclude_tables == "*.Order,X_Stg.*_Delete"

    def test_env_example_documents_it_empty(self):
        from dotenv import dotenv_values

        values = dotenv_values(Path(__file__).resolve().parent.parent / ".env.example")
        assert values["EXCLUDE_TABLES"] in (None, "")

    def test_the_keys_are_lists_of_trimmed_patterns_and_default_to_empty(self):
        definition = DataSourceDefinition(url_env="X", exclude_tables=[" X_Fact.Order "], keep_staging_schemas=[" X_Stg"])
        assert definition.exclude_tables == ["X_Fact.Order"] and definition.keep_staging_schemas == ["X_Stg"]
        bare = DataSourceDefinition(url_env="X")
        assert bare.exclude_tables == [] and bare.keep_staging_schemas == []

    @pytest.mark.parametrize("key", ["exclude_tables", "keep_staging_schemas"])
    @pytest.mark.parametrize("value, message", [
        ("a.b", "must be a list"),
        (["ok", 3], r"\[1\] must be a string"),
        (["ok", "  "], r"\[1\] must not be empty"),
        ({"a": 1}, "must be a list"),
    ])
    def test_anything_else_is_refused_with_the_key_and_position(self, key, value, message):
        with pytest.raises(ValueError, match=f"{key}.*{message}|{key}{message}"):
            DataSourceDefinition(url_env="X", **{key: value})

    def test_the_error_names_the_file_and_the_key(self):
        text = "datasources:\n  a:\n    url_env: X\n    exclude_tables: 5\n"
        with pytest.raises(ValueError, match=r"\[datasources.yaml\] validation error at 'datasources -> a -> exclude_tables'"):
            validate_datasources_yaml_text(text)

    def test_a_misspelt_key_is_still_refused(self):
        with pytest.raises(ValueError, match="exclude_table"):
            validate_datasources_yaml_text("datasources:\n  a:\n    url_env: X\n    exclude_table: [x]\n")

    def test_accessors_list_every_source_default_first(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(_project(tmp_path))):
            assert datasource_exclude_tables()["other"] == ("stg.*_Insert", "stg.*_Update", "stg.*_Delete")
            assert datasource_keep_staging_schemas() == {"main": ("X_Stg",), "other": ("stg",)}

    def test_accessors_without_the_file(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(tmp_path)):
            assert datasource_exclude_tables() == {"default": ()}
            assert datasource_keep_staging_schemas() == {"default": ()}

    def test_patterns_compose_global_then_source_without_repeats(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(_project(tmp_path)), exclude_tables="*.Tmp, x_fact.order"):
            patterns = table_exclusion_patterns("main")
            other = table_exclusion_patterns("other")
            none = table_exclusion_patterns(None)
        assert patterns == (
            "*.Tmp", "x_fact.order", "X_Stg.*_Insert", "X_Stg.*_Update", "X_Stg.*_Delete",
        )
        assert other[0] == "*.Tmp" and "X_Fact.Order" not in other
        assert none == ("*.Tmp", "x_fact.order")

    def test_schema_exclusion_carries_the_new_lists(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(_project(tmp_path)), exclude_tables=""):
            rule = schema_exclusion("main", ["Cli"])
        assert rule.configured == ("Meta", "Audit_Stg", "Old", "dbo") and rule.cli == ("Cli",)
        assert rule.keep_staging == ("X_Stg",) and "X_Fact.Order" in rule.tables

    def test_the_env_line_extends_the_current_setting(self):
        with cfg.override_settings(exclude_tables="*.Order"):
            assert exclude_tables_env_line(["X_Stg.*_Delete", "*.order"]) == "EXCLUDE_TABLES=*.Order,X_Stg.*_Delete"

    def test_the_commented_examples_in_the_template_are_valid_when_uncommented(self):
        template = Path(__file__).resolve().parent.parent / "project_config.example" / "datasources.example.yaml"
        lines = template.read_text(encoding="utf-8").splitlines()
        wanted = ("# keep_staging_schemas:", "# exclude_tables:")
        uncommented = [
            ln.replace("# ", "", 1) if ln.lstrip().startswith(wanted) and "[" in ln else ln for ln in lines
        ]
        parsed = validate_datasources_yaml_text("\n".join(uncommented))
        assert parsed.datasources["sales"].keep_staging_schemas == ["Sales_Stg"]
        assert "Sales_Fact.Order" in parsed.datasources["sales"].exclude_tables


class TestTheOperatorsRequestOnTheFirstDatabase:
    def _select(self, tmp_path, staging=STAGING, **kwargs):
        with cfg.override_settings(project_config_dir=str(_project(tmp_path)), exclude_tables=""):
            exclusion = schema_exclusion("main")
        return select_tables(MAIN, exclusion=exclusion, staging=staging, **kwargs)

    def test_the_staging_schema_is_kept_without_its_change_capture_tables_and_the_one_table_is_skipped(self, tmp_path):
        kept, skipped = self._select(tmp_path)
        assert _kept(kept) == ["X_Fact.Trade", "X_Stg.Item", "X_Stg.Trade", "sales.Customer"]
        # Y_Stg is a staging schema nobody kept: the default rule still drops it.
        assert skipped.tables == {"configured": 4, "configured-tables": 4, "staging": 2}

    def test_without_keep_staging_the_whole_staging_schema_goes(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(tmp_path), exclude_tables=""):
            exclusion = SchemaExclusion(
                configured=("Meta", "Audit_Stg", "Old", "dbo"), tables=("X_Fact.Order",),
            )
        kept, _ = select_tables(MAIN, exclusion=exclusion, staging=STAGING)
        assert not any(name.startswith("X_Stg.") for name in _kept(kept))

    def test_the_table_patterns_of_staging_patterns_still_apply_in_a_kept_schema(self, tmp_path):
        kept, skipped = self._select(tmp_path)
        assert "X_Stg.Trade_Inserts" not in _kept(kept)

    def test_exclude_schemas_still_wins_over_keep_staging(self):
        exclusion = SchemaExclusion(configured=("X_Stg",), keep_staging=("X_Stg",))
        kept, skipped = select_tables(MAIN, exclusion=exclusion, staging=STAGING)
        assert not any(name.startswith("X_Stg.") for name in _kept(kept))
        assert skipped.tables["configured"] == 6

    def test_include_staging_keeps_everything_but_the_configured_exclusions(self, tmp_path):
        kept, skipped = self._select(tmp_path, staging=())
        assert "Y_Stg.Tmp" in _kept(kept) and "X_Stg.Trade_Inserts" in _kept(kept)
        assert "X_Fact.Order" not in _kept(kept) and "X_Stg.Trade_Delete" not in _kept(kept)
        assert "staging" not in skipped.tables

    def test_the_report_names_the_new_rule(self, tmp_path):
        _, skipped = self._select(tmp_path)
        lines = describe_skipped({"main": skipped})
        assert "  configured table exclusion (EXCLUDE_TABLES / exclude_tables:): 4 table(s) in 2 schema(s)" in lines

    def test_a_cli_table_name_still_counts_under_its_own_rule_first(self, tmp_path):
        _, skipped = self._select(tmp_path, exclude_tables=["Customer", "Order"])
        assert skipped.tables["exclude-tables"] == 2

    def test_the_singular_names_are_only_dropped_by_exclude_tables_or_an_added_pattern(self):
        kept, _ = select_tables(MAIN, exclusion=SchemaExclusion(keep_staging=("X_Stg",)), staging=STAGING)
        assert "X_Stg.Trade_Insert" in _kept(kept)             # the default knows the plural only
        extended = doctor.parse_staging_patterns(
            "*_Stg,*stg*,*staging*,table:*_Inserts,table:*_Updates,table:*_Deletes,"
            "table:*_Insert,table:*_Update,table:*_Delete",
        )
        kept, _ = select_tables(MAIN, exclusion=SchemaExclusion(keep_staging=("X_Stg",)), staging=extended)
        assert "X_Stg.Trade_Insert" not in _kept(kept) and "X_Stg.Item" in _kept(kept)


class TestWizardPlan:
    LISTING = {
        "X_Stg": ["Trade", "Trade_Insert", "Trade_Delete", "Item"],
        "X_Fact": ["Order", "Trade"],
        "Y_Stg": ["Tmp"],
        "Meta": ["Dict"],
    }
    RULE = SchemaExclusion(
        configured=("Meta",), keep_staging=("X_Stg",),
        tables=("X_Stg.*_Insert", "X_Stg.*_Delete", "X_Fact.Order"),
    )

    def test_a_kept_staging_schema_is_read_minus_the_named_tables(self):
        from setup_project import _plan_reading

        plan = _plan_reading(self.LISTING, None, self.RULE, STAGING)
        assert sorted(plan.schemas) == ["X_Fact", "X_Stg"]
        assert sorted(plan.exclude_tables) == ["Order", "Trade_Delete", "Trade_Insert"]
        assert plan.per_schema == {"X_Stg": 2, "X_Fact": 1}
        assert plan.report.describe() == (
            "Skipped 5 of 8 tables: 1 in schemas excluded by configuration (EXCLUDE_SCHEMAS or exclude_schemas:), "
            "3 named by a table pattern (EXCLUDE_TABLES or exclude_tables:), "
            "1 staging or change-capture (STAGING_PATTERNS; --include-staging keeps them)."
        )

    def test_a_schema_whose_every_table_is_named_is_not_read(self):
        from setup_project import _plan_reading

        plan = _plan_reading({"X_Fact": ["Order"], "ref": ["Region"]}, None, self.RULE, [])
        assert plan.schemas == ["ref"] and plan.report.excluded_tables == 1

    def test_a_shared_bare_name_is_read_and_dropped_afterwards(self):
        from setup_project import SkipReport, _plan_reading, _skip_tables

        listing = {"X_Fact": ["Order", "Trade"], "ref": ["Order", "Region"]}
        plan = _plan_reading(listing, None, self.RULE, [])
        assert sorted(plan.schemas) == ["X_Fact", "ref"] and plan.exclude_tables == []
        snap = NS(
            tables=[
                NS(name="Order", schema="X_Fact"), NS(name="Trade", schema="X_Fact"),
                NS(name="Order", schema="ref"), NS(name="Region", schema="ref"),
            ],
            relationships=[],
        )
        report = SkipReport(found=4)
        _skip_tables(snap, [], [], report, exclusion=self.RULE)
        assert [(t.schema, t.name) for t in snap.tables] == [("X_Fact", "Trade"), ("ref", "Order"), ("ref", "Region")]
        assert report.excluded_tables == 1

    def test_the_log_record_has_the_new_count_and_round_trips(self):
        from setup_project import SkipReport, _plan_reading

        plan = _plan_reading(self.LISTING, None, self.RULE, STAGING)
        log = plan.report.to_log()
        assert log["excluded_tables"] == 3
        assert SkipReport.from_log(log).describe() == plan.report.describe()

    def test_the_staging_helper_honours_the_keep_list(self):
        from setup_project import _is_staging

        assert _is_staging(NS(schema="X_Stg", name="Trade"), STAGING)
        assert not _is_staging(NS(schema="X_Stg", name="Trade"), STAGING, ["x_stg"])
        assert _is_staging(NS(schema="X_Stg", name="Trade_Inserts"), STAGING, ["x_stg"])


SCHEMA = """\
tables:
  sales.Trade:
    description: "One row per trade"
    columns:
      TradeID: "Primary key"
  X_Fact.Kept:
    description: "Listed by hand although a pattern names it"
    columns:
      ID: "Primary key"
      Extra: "A column the sync adds to a listed table"
"""
DB = catalogue(
    tbl("sales", "Trade", [("TradeID", "int")], pk=["TradeID"]),
    tbl("X_Fact", "Order", [("ID", "int")]),
    tbl("X_Fact", "Kept", [("ID", "int"), ("Extra", "int")]),
    tbl("X_Stg", "Item", [("ID", "int")]),
    tbl("X_Stg", "Item_Delete", [("ID", "int")]),
)


def _sync(**options):
    return sync_schema_text(SCHEMA, ["main"], "main", {"main": DB}, SyncOptions(**options))


class TestSync:
    TABLES = {"main": ("X_Fact.Order", "X_Stg.*_Delete", "X_Fact.Kept")}

    def test_a_named_table_is_neither_listed_nor_added(self):
        result = _sync(add_tables=("*",), exclude_tables=self.TABLES)
        assert [n.key for n in result.plan.new_tables] == ["Item"]
        assert sorted(result.plan.excluded_tables) == [("X_Fact", "Order"), ("X_Stg", "Item_Delete")]
        assert "Order" not in result.text and "Item_Delete" not in result.text

    def test_without_the_patterns_they_are_candidates(self):
        names = sorted(name for name, _ in _sync().plan.unlisted)
        assert names == ["X_Fact.Order", "X_Stg.Item", "X_Stg.Item_Delete"]

    def test_a_listed_table_is_synced_even_when_a_pattern_names_it(self):
        result = _sync(exclude_tables=self.TABLES)
        assert "Extra" in result.text and result.plan.excluded_tables
        assert ("X_Fact", "Kept") not in result.plan.excluded_tables

    def test_the_report_has_a_section_of_its_own(self):
        report = build_report(_sync(exclude_tables=self.TABLES))
        assert (
            "== tables named by an excluded table pattern, not listed or added "
            "(EXCLUDE_TABLES, exclude_tables:): 2 =="
        ) in report
        assert "X_Fact: 1" in report and "X_Stg: 1" in report
        assert "tables in excluded schemas" not in report

    def test_a_schema_exclusion_takes_the_table_first(self):
        result = _sync(exclude_schemas={"main": ("X_Fact",)}, exclude_tables=self.TABLES)
        assert ("X_Fact", "Order") in result.plan.excluded
        assert ("X_Fact", "Order") not in result.plan.excluded_tables

    def test_a_table_another_source_does_not_exclude_is_still_a_candidate(self):
        both = {"a": DB, "b": DB}
        result = sync_schema_text(
            SCHEMA, ["a", "b"], "a", both, SyncOptions(exclude_tables={"a": ("X_Fact.Order",), "b": ()}),
        )
        assert "X_Fact.Order" in [name for name, _ in result.plan.unlisted]
        assert ("X_Fact", "Order") not in result.plan.excluded_tables

    def test_no_option_means_no_change(self):
        assert _sync().plan.excluded_tables == ()


class TestPreflight:
    def test_a_listed_table_named_by_a_pattern_is_advice(self, tmp_path):
        import scripts.verify_deployment as verify

        (tmp_path / "schema.yaml").write_text(
            "tables:\n  Order:\n    db_schema: X_Fact\n    description: d\n    columns: {ID: k}\n"
            "  Trade:\n    db_schema: sales\n    description: d\n    columns: {ID: k}\n",
            encoding="utf-8",
        )
        with cfg.override_settings(project_config_dir=str(tmp_path), exclude_tables="X_Fact.Order"):
            result = verify.check_no_table_in_excluded_schema()
        assert result.status == "PASS"
        assert result.detail.startswith("advice: 1 of 2 table(s) are in a schema the setup tools are told not to scan, or named by a table pattern: Order")
        assert "EXCLUDE_TABLES" in result.detail and "exclude_tables:" in result.detail

    def test_a_pattern_that_names_nothing_listed_passes_quietly(self, tmp_path):
        import scripts.verify_deployment as verify

        (tmp_path / "schema.yaml").write_text(
            "tables:\n  Trade:\n    db_schema: sales\n    description: d\n    columns: {ID: k}\n", encoding="utf-8",
        )
        with cfg.override_settings(project_config_dir=str(tmp_path), exclude_tables="X_Fact.Order"):
            result = verify.check_no_table_in_excluded_schema()
        assert result.detail == "none of the 1 table(s) is in an excluded schema or named by an excluded table pattern"

    def test_the_doctor_function_needs_every_source_to_name_the_table(self):
        view = doctor.SchemaView(parsed=True, tables=(
            doctor.SchemaTable("Order", "X_Fact", "Order", "", ("a", "b"), ()),
        ))
        assert doctor.tables_matching_excluded_tables(view, {"a": ["X_Fact.Order"], "b": []}, "a") == []
        assert len(doctor.tables_matching_excluded_tables(view, {"a": ["X_Fact.Order"], "b": ["*.Order"]}, "a")) == 1

    def test_the_staging_check_does_not_advise_about_a_kept_schema(self, tmp_path):
        import scripts.verify_deployment as verify

        (tmp_path / "datasources.yaml").write_text(YAML, encoding="utf-8")
        (tmp_path / "schema.yaml").write_text(
            "tables:\n"
            "  Trade:\n    db_schema: X_Stg\n    description: d\n    columns: {ID: k}\n"
            "  Y:\n    db_schema: Y_Stg\n    description: d\n    columns: {ID: k}\n"
            "  Trade_Inserts:\n    db_schema: X_Stg\n    description: d\n    columns: {ID: k}\n",
            encoding="utf-8",
        )
        with cfg.override_settings(project_config_dir=str(tmp_path)):
            result = verify.check_no_staging_tables()
        assert "Y_Stg: 1" in result.detail and "X_Stg: 1" in result.detail  # only the _Inserts table of X_Stg


class TestRecordingInDatasourcesYaml:
    TEXT = "datasources:\n  sales:\n    url_env: DB_URL_SALES\n    exclude_schemas: [Meta]\n"

    def test_a_new_line_is_inserted_after_the_sources_settings(self):
        new, added = add_exclude_tables(self.TEXT, {"sales": ["X_Fact.Order", "X_Stg.*_Delete"]})
        assert new.endswith('    exclude_tables: ["X_Fact.Order", "X_Stg.*_Delete"]\n')
        assert added == {"sales": ("X_Fact.Order", "X_Stg.*_Delete")}
        parsed = validate_datasources_yaml_text(new)
        assert parsed.datasources["sales"].exclude_tables == ["X_Fact.Order", "X_Stg.*_Delete"]
        assert parsed.datasources["sales"].exclude_schemas == ["Meta"]

    def test_the_items_join_an_existing_flow_list_and_block_list(self):
        flow = self.TEXT + '    exclude_tables: ["A.B"]  # note\n'
        new, _ = add_exclude_tables(flow, {"sales": ["C.D"]})
        assert '    exclude_tables: ["A.B", "C.D"]  # note' in new
        block = self.TEXT + "    exclude_tables:\n      - A.B\n"
        new, _ = add_exclude_tables(block, {"sales": ["C.D"]})
        assert validate_datasources_yaml_text(new).datasources["sales"].exclude_tables == ["A.B", "C.D"]

    def test_a_listed_pattern_is_not_added_again(self):
        again = self.TEXT + '    exclude_tables: ["A.B"]\n'
        assert add_exclude_tables(again, {"sales": ["a.b"]}) == (again, {})

    def test_the_exclude_schemas_list_is_not_touched(self):
        new, _ = add_exclude_tables(self.TEXT, {"sales": ["A.B"]})
        assert "exclude_schemas: [Meta]" in new

    def test_an_unknown_source_is_refused(self):
        with pytest.raises(DatasourcesEditError, match="unknown data source"):
            add_exclude_tables(self.TEXT, {"nope": ["A.B"]})

    def test_the_snippet(self):
        assert exclude_tables_snippet({"sales": ["A.B"]}) == 'datasources:\n  sales:\n    exclude_tables: ["A.B"]'

    def test_record_makes_a_backup_and_replaces_the_file(self, tmp_path):
        path = tmp_path / "datasources.yaml"
        path.write_text(self.TEXT, encoding="utf-8")
        done = record_exclude_tables(path, {"sales": ["A.B"]}, stamp="20260101-000000")
        assert done.backup is not None and done.backup.read_text(encoding="utf-8") == self.TEXT
        assert validate_datasources_yaml_text(path.read_text(encoding="utf-8")).datasources["sales"].exclude_tables == ["A.B"]
        assert record_exclude_tables(path, {"sales": ["a.b"]}, stamp="20260101-000001").backup is None
