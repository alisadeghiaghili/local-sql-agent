# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Qualified ``schema.yaml`` table keys -- ``docs/design/TABLE-NAMES.md``.

Covers the whole feature end to end:

1. ``schema_data.registry`` key parsing (:func:`split_table_key`,
   :func:`effective_qualifier`, :func:`table_reference_sql`) and the
   ``SchemaConfig`` validation rules a qualified key introduces
   (qualifier/``db_schema`` agreement, duplicate detection).
2. ``security.sql_guard``'s qualifier-aware table resolution: a qualified
   match, the ``[hr].[Customer]`` hole this feature closes, the documented
   "no known qualifier accepts any" residual gap, an unambiguous
   unqualified reference, ``ambiguous_table`` for an ambiguous one, and
   the refusal of an unqualified reference to a table whose only qualifier
   is multi-part (another database).
3. A self-join of two same-bare-name, different-qualifier tables, with
   aliases and qualified columns.
4. ``extract_touched_tables`` / ``database.routing`` with qualified keys.
5. ``retrieval.value_resolver`` / ``retrieval.dimension_vocabulary`` SQL
   generation for a qualified key.
6. Prompt rendering: unchanged for today's example config, and a
   ``Reference as:`` line for a duplicate-bare-name deployment.
7. ``schema_data.drift`` with duplicate bare names in two schemas
   (SQLite's ``ATTACH DATABASE ... AS <schema>`` emulates a real
   multi-schema warehouse).

Duplicate-name fixtures are built by copying ``project_config.example/``
into ``tmp_path`` and rewriting its ``schema.yaml`` -- never a hand-rolled
schema disconnected from the example CI already exercises elsewhere.
``security.sql_guard`` binds its table/column/qualifier lookup once, at
import time (see ``tests/test_routing.py``'s own note on this) --
:func:`security.sql_guard.refresh_schema_lookup` is the explicit, test-only
escape hatch this phase added so a test can swap ``schema.yaml`` mid-suite
and have the guard actually see the new one; every fixture below that
swaps the schema restores the real one through the same function on
teardown.
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text

import schema_data.drift as drift_module
import schema_data.registry as registry_module
import security.sql_guard as sql_guard
from config import override_settings
from database.routing import group_tables_by_datasource, resolve_datasource
from schema_data.registry import (
    SchemaConfig,
    bare_table_name,
    effective_qualifier,
    split_table_key,
    table_reference_sql,
    validate_schema_yaml_text,
)
from security.sql_guard import (
    CorrectableRejection,
    extract_touched_tables,
    validate_sql,
)

_EXAMPLE_CONFIG_DIR = Path(__file__).resolve().parent.parent / "project_config.example"


# ---------------------------------------------------------------------------
# Fixture: project_config.example/ copied to tmp_path, Customer split into
# sales.Customer / ref.Customer -- exactly the bug reproduction in the
# design brief.
# ---------------------------------------------------------------------------


def _duplicate_customer_config_dir(tmp_path: Path) -> Path:
    dest = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE_CONFIG_DIR, dest)

    schema_path = dest / "schema.yaml"
    raw = yaml.safe_load(schema_path.read_text(encoding="utf-8"))

    customer = raw["tables"].pop("Customer")
    customer["db_schema"] = "sales"
    raw["tables"]["sales.Customer"] = customer
    raw["tables"]["ref.Customer"] = {
        "description": "ref.Customer -- a second Customer table, in a different schema",
        "db_schema": "ref",
        "columns": {
            "ID": "Primary key",
            "Name": "Customer name in the ref schema",
        },
        # Deliberately no resolvable_columns/prefetchable_columns -- the
        # design brief's own repro.
    }
    for rel in raw.get("relationships", []):
        if rel.get("from_table") == "Customer":
            rel["from_table"] = "sales.Customer"
        if rel.get("to_table") == "Customer":
            rel["to_table"] = "sales.Customer"

    schema_path.write_text(
        yaml.dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )
    return dest


@contextmanager
def _using_config_dir(path: Path):
    """Point ``PROJECT_CONFIG_DIR`` at *path* and refresh every process-
    lifetime cache that a swapped ``schema.yaml`` would otherwise leave
    stale -- ``schema_data.registry``'s own cache (cleared directly, the
    same way ``tests/test_schema_drift.py`` does) and
    ``security.sql_guard``'s derived lookup (via
    :func:`security.sql_guard.refresh_schema_lookup`). Restores both, to
    the real config this test run loaded with, on the way out -- so a test
    in this module never leaks a synthetic schema into whatever runs next.
    """
    registry_module._cache.clear()
    with override_settings(project_config_dir=str(path)):
        sql_guard.refresh_schema_lookup()
        try:
            yield
        finally:
            pass
    registry_module._cache.clear()
    sql_guard.refresh_schema_lookup()


@pytest.fixture()
def duplicate_customer_dir(tmp_path):
    return _duplicate_customer_config_dir(tmp_path)


@pytest.fixture()
def duplicate_customer_schema(duplicate_customer_dir):
    """Everything under this fixture runs against the sales.Customer /
    ref.Customer duplicate-name config; the real schema is restored on
    teardown."""
    with _using_config_dir(duplicate_customer_dir):
        yield duplicate_customer_dir


# ---------------------------------------------------------------------------
# 1. schema_data.registry -- key parsing
# ---------------------------------------------------------------------------


class TestSplitTableKey:
    def test_bare_key(self):
        ref = split_table_key("Customer")
        assert ref.parts == ("Customer",)
        assert ref.name == "Customer"
        assert ref.qualifier == ()

    def test_one_part_qualifier(self):
        ref = split_table_key("sales.Customer")
        assert ref.qualifier == ("sales",)
        assert ref.name == "Customer"

    def test_two_part_qualifier(self):
        ref = split_table_key("OtherDb.dbo.Customer")
        assert ref.qualifier == ("OtherDb", "dbo")
        assert ref.name == "Customer"

    def test_three_part_qualifier(self):
        ref = split_table_key("Linked.OtherDb.dbo.Customer")
        assert ref.qualifier == ("Linked", "OtherDb", "dbo")
        assert ref.name == "Customer"

    def test_four_part_qualifier_is_rejected(self):
        with pytest.raises(ValueError, match="at most three leading qualifier parts"):
            split_table_key("a.b.c.d.e")

    def test_brackets_are_parsed_and_stripped(self):
        ref = split_table_key("[sales].[Customer]")
        assert ref.parts == ("sales", "Customer")

    def test_doubled_bracket_escape(self):
        ref = split_table_key("[my]]table]")
        assert ref.parts == ("my]table",)

    def test_case_is_preserved_not_normalised(self):
        assert split_table_key("Sales.Customer").qualifier == ("Sales",)

    def test_invalid_syntax_raises_value_error(self):
        with pytest.raises(ValueError, match="invalid table key"):
            split_table_key("[unterminated")

    def test_bare_table_name_helper(self):
        assert bare_table_name("sales.Customer") == "Customer"
        assert bare_table_name("Customer") == "Customer"


class TestEffectiveQualifier:
    def test_qualified_key_wins_over_nothing(self):
        assert effective_qualifier("sales.Customer") == ("sales",)

    def test_bare_key_uses_db_schema(self):
        assert effective_qualifier("Customer", "sales") == ("sales",)

    def test_bare_key_no_db_schema_is_empty(self):
        assert effective_qualifier("Customer") == ()

    def test_qualified_key_and_agreeing_db_schema(self):
        assert effective_qualifier("sales.Customer", "sales") == ("sales",)


class TestTableReferenceSql:
    def test_bare(self):
        assert table_reference_sql("Customer") == "[Customer]"

    def test_one_part_qualifier(self):
        assert table_reference_sql("sales.Customer", "sales") == "[sales].[Customer]"

    def test_qualifier_independent_of_key_shape(self):
        # The qualifier passed in is what renders -- table_reference_sql
        # does not re-derive it from the key a second time.
        assert table_reference_sql("Customer", "OtherDb.dbo") == "[OtherDb].[dbo].[Customer]"

    def test_only_the_bare_name_is_used_from_a_qualified_key(self):
        assert table_reference_sql("sales.Customer", "sales") == table_reference_sql(
            "Customer", "sales",
        )


# ---------------------------------------------------------------------------
# 1b. SchemaConfig validation
# ---------------------------------------------------------------------------


class TestSchemaConfigQualifiedKeyValidation:
    def test_qualified_key_alone_is_valid(self):
        cfg = validate_schema_yaml_text(
            "tables:\n"
            "  sales.Customer:\n"
            "    columns: {ID: pk}\n"
        )
        assert "sales.Customer" in cfg.tables

    def test_qualifier_and_db_schema_may_agree(self):
        cfg = validate_schema_yaml_text(
            "tables:\n"
            "  sales.Customer:\n"
            "    db_schema: sales\n"
            "    columns: {ID: pk}\n"
        )
        assert "sales.Customer" in cfg.tables

    def test_qualifier_and_db_schema_conflict_is_rejected(self):
        with pytest.raises(ValueError, match="does not match its"):
            validate_schema_yaml_text(
                "tables:\n"
                "  sales.Customer:\n"
                "    db_schema: ref\n"
                "    columns: {ID: pk}\n"
            )

    def test_two_keys_resolving_to_the_same_table_are_rejected(self):
        with pytest.raises(ValueError, match="both resolve to the same table"):
            validate_schema_yaml_text(
                "tables:\n"
                "  sales.Customer:\n"
                "    columns: {ID: pk}\n"
                "  Customer:\n"
                "    db_schema: sales\n"
                "    columns: {ID: pk}\n"
            )

    def test_different_qualifiers_sharing_a_bare_name_are_not_a_collision(self):
        cfg = validate_schema_yaml_text(
            "tables:\n"
            "  sales.Customer:\n"
            "    columns: {ID: pk}\n"
            "  ref.Customer:\n"
            "    columns: {ID: pk}\n"
        )
        assert set(cfg.tables) == {"sales.Customer", "ref.Customer"}

    def test_resolvable_columns_satisfied_by_a_qualified_key_alone(self):
        """A qualified key with NO db_schema still satisfies the
        resolvable_columns/prefetchable_columns qualifier requirement --
        the effective qualifier comes from the key itself."""
        cfg = validate_schema_yaml_text(
            "tables:\n"
            "  sales.Customer:\n"
            "    columns: {Name: n}\n"
            "    resolvable_columns: [Name]\n"
        )
        assert cfg.tables["sales.Customer"].resolvable_columns == ("Name",)

    def test_resolvable_columns_still_requires_some_qualifier(self):
        with pytest.raises(ValueError, match="no qualifier"):
            validate_schema_yaml_text(
                "tables:\n"
                "  Customer:\n"
                "    columns: {Name: n}\n"
                "    resolvable_columns: [Name]\n"
            )

    def test_invalid_key_syntax_is_reported_as_a_schema_validation_error(self):
        with pytest.raises(ValueError, match="schema.yaml"):
            validate_schema_yaml_text(
                "tables:\n"
                "  \"[unterminated\":\n"
                "    columns: {ID: pk}\n"
            )


# ---------------------------------------------------------------------------
# 2. security.sql_guard -- qualifier-aware table resolution
# ---------------------------------------------------------------------------


class TestGuardQualifiedResolution:
    def test_qualified_match_is_accepted(self, duplicate_customer_schema):
        validate_sql("SELECT TOP 5 c.Name FROM [sales].[Customer] c")
        validate_sql("SELECT TOP 5 c.ID FROM [ref].[Customer] c")

    def test_mismatched_qualifier_is_rejected(self, duplicate_customer_schema):
        """The [hr].[Customer] hole this feature closes: a qualifier that
        names no known candidate for this bare name must not sail through
        just because the guard used to ignore qualifiers altogether."""
        with pytest.raises(CorrectableRejection) as exc_info:
            validate_sql("SELECT TOP 5 c.Name FROM [hr].[Customer] c")
        assert exc_info.value.reason == "unknown_table"
        assert exc_info.value.is_refusal is True
        assert "did you mean" in str(exc_info.value)
        assert "[sales].[Customer]" in str(exc_info.value)

    def test_unqualified_reference_to_a_duplicated_bare_name_is_ambiguous(
        self, duplicate_customer_schema,
    ):
        with pytest.raises(CorrectableRejection) as exc_info:
            validate_sql("SELECT TOP 5 Name FROM Customer")
        assert exc_info.value.reason == "ambiguous_table"
        assert exc_info.value.is_refusal is True
        assert exc_info.value.subject == "Customer"

    def test_unknown_qualifier_bare_key_still_accepts_any_qualifier(self, tmp_path):
        """A candidate with NO db_schema set at all is the documented
        residual gap: its qualifier cannot be checked, so any qualifier is
        accepted for it -- this is unrelated to the sales/ref duplicate
        pair and uses its own single-table config."""
        dest = tmp_path / "project_config"
        shutil.copytree(_EXAMPLE_CONFIG_DIR, dest)
        schema_path = dest / "schema.yaml"
        raw = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        raw["tables"]["Widget"] = {"description": "no db_schema at all", "columns": {"ID": "pk"}}
        schema_path.write_text(yaml.dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

        with _using_config_dir(dest):
            validate_sql("SELECT TOP 5 ID FROM [anything].[Widget]")
            validate_sql("SELECT TOP 5 ID FROM Widget")

    def test_unqualified_reference_to_multi_part_qualified_table_is_rejected(self, tmp_path):
        dest = tmp_path / "project_config"
        shutil.copytree(_EXAMPLE_CONFIG_DIR, dest)
        schema_path = dest / "schema.yaml"
        raw = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        raw["tables"]["Widget"] = {
            "description": "lives in another database",
            "db_schema": "OtherDb.dbo",
            "columns": {"ID": "pk"},
        }
        schema_path.write_text(yaml.dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

        with _using_config_dir(dest):
            validate_sql("SELECT TOP 5 ID FROM [OtherDb].[dbo].[Widget]")
            with pytest.raises(CorrectableRejection) as exc_info:
                validate_sql("SELECT TOP 5 ID FROM Widget")
            assert exc_info.value.reason == "unknown_table"
            assert exc_info.value.is_refusal is True

    def test_self_join_with_aliases_and_qualified_columns(self, duplicate_customer_schema):
        sql = (
            "SELECT TOP 5 s.Name, r.Name AS RefName "
            "FROM [sales].[Customer] s "
            "JOIN [ref].[Customer] r ON s.NationalID = r.ID"
        )
        validate_sql(sql)

    def test_column_allowlist_applies_to_the_resolved_key(self, duplicate_customer_schema):
        """ref.Customer only has ID/Name -- a NationalID reference against
        its alias must be rejected even though sales.Customer has one."""
        with pytest.raises(CorrectableRejection, match="Unknown column"):
            validate_sql(
                "SELECT TOP 5 r.NationalID FROM [ref].[Customer] r"
            )

    def test_star_expansion_resolves_the_qualified_table(self, duplicate_customer_schema):
        # No denied_columns policy active -- '*' is always allowed; this
        # just proves the query validates for both same-bare-name tables.
        validate_sql("SELECT TOP 5 * FROM [sales].[Customer]")
        validate_sql("SELECT TOP 5 * FROM [ref].[Customer]")

    def test_star_expansion_under_a_denied_columns_policy(self, duplicate_customer_schema):
        from security.sql_guard import PolicyRejection

        # sales.Customer has NationalID; ref.Customer does not -- a denied
        # NationalID policy must refuse '*' against sales.Customer...
        with pytest.raises(PolicyRejection):
            validate_sql(
                "SELECT TOP 5 * FROM [sales].[Customer]", denied_columns={"NationalID"},
            )
        # ...but must not falsely refuse the SAME '*' against ref.Customer,
        # which has no such column at all.
        validate_sql("SELECT TOP 5 * FROM [ref].[Customer]", denied_columns={"NationalID"})


# ---------------------------------------------------------------------------
# 4. extract_touched_tables / database.routing
# ---------------------------------------------------------------------------


class TestExtractTouchedTablesAndRouting:
    def test_extract_touched_tables_returns_qualified_keys(self, duplicate_customer_schema):
        touched = extract_touched_tables(
            "SELECT s.Name, r.Name FROM [sales].[Customer] s "
            "JOIN [ref].[Customer] r ON s.NationalID = r.ID"
        )
        assert touched == ["ref.Customer", "sales.Customer"]

    def test_routing_groups_by_the_qualified_key(self, duplicate_customer_schema):
        from unittest.mock import patch

        with patch.multiple(
            "database.routing",
            table_datasources=lambda: {"sales.Customer": "main", "ref.Customer": "archive"},
            default_datasource_name=lambda: "main",
        ):
            groups = group_tables_by_datasource(["sales.Customer", "ref.Customer"])
            assert groups == {"main": ["sales.Customer"], "archive": ["ref.Customer"]}

            with pytest.raises(Exception):
                resolve_datasource(
                    "SELECT s.ID FROM [sales].[Customer] s "
                    "JOIN [ref].[Customer] r ON s.NationalID = r.ID"
                )


# ---------------------------------------------------------------------------
# 5. retrieval.value_resolver / retrieval.dimension_vocabulary SQL
# ---------------------------------------------------------------------------


class TestValueResolverAndVocabularySqlForQualifiedKeys:
    def test_value_resolver_build_query_uses_the_bare_name_only(self, monkeypatch):
        import retrieval.value_resolver as value_resolver

        monkeypatch.setitem(value_resolver._TABLE_SCHEMAS, "sales.Customer", "sales")
        sql = value_resolver._build_query("sales.Customer", "Name")
        assert sql == (
            "SELECT DISTINCT TOP (?) [Name] FROM [sales].[Customer] "
            "WHERE [Name] LIKE ? ESCAPE '\\'"
        )

    def test_value_resolver_build_query_schema_less_dialect_uses_bare_name(self, monkeypatch):
        import retrieval.value_resolver as value_resolver

        monkeypatch.setitem(value_resolver._TABLE_SCHEMAS, "sales.Customer", "sales")
        sql = value_resolver._build_query("sales.Customer", "Name", dialect="sqlite")
        assert '"Customer"' in sql or "[Customer]" in sql
        assert "sales" not in sql.split("FROM")[1].split("WHERE")[0]

    def test_dimension_vocabulary_prefetch_query_uses_the_bare_name_only(self, monkeypatch):
        import retrieval.dimension_vocabulary as dimension_vocabulary

        monkeypatch.setitem(dimension_vocabulary._TABLE_SCHEMAS, "ref.Customer", "ref")
        sql = dimension_vocabulary._prefetch_query("ref.Customer", "Name")
        assert sql == "SELECT DISTINCT TOP (?) [Name] FROM [ref].[Customer]"


# ---------------------------------------------------------------------------
# 6. Prompt rendering
# ---------------------------------------------------------------------------


class TestPromptRendering:
    def test_todays_example_config_renders_unchanged(self):
        """No duplicate bare names, no multi-part qualifiers -- the
        baseline example config must render exactly what it always has:
        no 'Reference as:' line at all."""
        from schema_data.registry import SchemaRegistry

        ctx = SchemaRegistry.build_schema_context(["Customer"])
        assert "Reference as:" not in ctx
        assert ctx.startswith("Table: Customer")

    def test_duplicate_bare_name_gets_a_reference_as_line(self, duplicate_customer_schema):
        from schema_data.registry import SchemaRegistry

        ctx = SchemaRegistry.build_schema_context(["sales.Customer", "ref.Customer"])
        assert "Table: sales.Customer" in ctx
        assert "Table: ref.Customer" in ctx
        assert "Reference as: [sales].[Customer]" in ctx
        assert "Reference as: [ref].[Customer]" in ctx


# ---------------------------------------------------------------------------
# 7. schema_data.drift -- duplicate bare names in two schemas
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_drift_baseline_and_registry_cache():
    registry_module._cache.clear()
    yield
    registry_module._cache.clear()


@pytest.fixture()
def drift_baseline_file(tmp_path):
    path = tmp_path / "schema_drift_baseline.json"
    drift_module._DRIFT_BASELINE_FILE = str(path)
    yield path
    drift_module._DRIFT_BASELINE_FILE = ""


class TestDriftWithDuplicateBareNamesAcrossSchemas:
    def test_both_customers_verify_cleanly_against_their_own_schema(
        self, tmp_path, drift_baseline_file,
    ):
        schema_dir = tmp_path / "project_config"
        schema_dir.mkdir()
        (schema_dir / "schema.yaml").write_text(
            yaml.dump({
                "tables": {
                    "sales.Customer": {
                        "description": "sales Customer",
                        "db_schema": "sales",
                        "columns": {"ID": "pk", "Name": "n"},
                    },
                    "ref.Customer": {
                        "description": "ref Customer",
                        "db_schema": "ref",
                        "columns": {"ID": "pk", "Name": "n"},
                    },
                },
                "relationships": [],
            }, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        db_path = tmp_path / "warehouse.db"
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            # SQLite has no real schema concept -- ATTACH DATABASE emulates
            # one: two distinct files, each holding a "Customer" table,
            # attached under the names schema.yaml's db_schema values name.
            sales_db = tmp_path / "sales.db"
            ref_db = tmp_path / "ref.db"
            conn.execute(text(f"ATTACH DATABASE '{sales_db}' AS sales"))
            conn.execute(text(f"ATTACH DATABASE '{ref_db}' AS ref"))
            conn.execute(text("CREATE TABLE sales.Customer (ID INTEGER, Name TEXT)"))
            conn.execute(text("CREATE TABLE ref.Customer (ID INTEGER, Name TEXT)"))

        with _using_config_dir(schema_dir):
            report = drift_module.check_schema_drift(engine=engine)

        assert report.warehouse_only == ()
        assert report.schema_only == ()

    def test_a_third_unmatched_table_is_reported_qualified_by_its_schema(
        self, tmp_path, drift_baseline_file,
    ):
        # "ref" must have at least one schema.yaml table for the scan to
        # even look at that schema at all (check_schema_drift only scans
        # schemas a configured table actually names) -- ref.Broker plays
        # that role here so ref.Customer, which schema.yaml never
        # mentions, is discovered as a genuine warehouse-only table.
        schema_dir = tmp_path / "project_config"
        schema_dir.mkdir()
        (schema_dir / "schema.yaml").write_text(
            yaml.dump({
                "tables": {
                    "sales.Customer": {
                        "description": "sales Customer",
                        "db_schema": "sales",
                        "columns": {"ID": "pk"},
                    },
                    "ref.Broker": {
                        "description": "ref Broker",
                        "db_schema": "ref",
                        "columns": {"ID": "pk"},
                    },
                },
                "relationships": [],
            }, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        db_path = tmp_path / "warehouse.db"
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            sales_db = tmp_path / "sales.db"
            ref_db = tmp_path / "ref.db"
            conn.execute(text(f"ATTACH DATABASE '{sales_db}' AS sales"))
            conn.execute(text(f"ATTACH DATABASE '{ref_db}' AS ref"))
            conn.execute(text("CREATE TABLE sales.Customer (ID INTEGER)"))
            conn.execute(text("CREATE TABLE ref.Broker (ID INTEGER)"))
            # ref.Customer is not in schema.yaml at all -- a warehouse-only
            # table sharing a bare name with an allowlisted one elsewhere.
            conn.execute(text("CREATE TABLE ref.Customer (ID INTEGER)"))

        with _using_config_dir(schema_dir):
            report = drift_module.check_schema_drift(engine=engine)

        assert "ref.Customer.ID" in report.warehouse_only
        assert report.schema_only == ()
