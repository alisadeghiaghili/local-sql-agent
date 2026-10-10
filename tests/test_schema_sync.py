# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``schema_data.sync``: every merge rule of the structural sync.

A fake catalogue (``tests/_sync_fixtures.py``) stands in for each source's
``INFORMATION_SCHEMA``; nothing here connects to anything. The schema text
used throughout is realistic: header comments, section dividers, quoted
keys, trailing comments, a qualified key, a described-only table, a table
shared between two sources, a stale column and a type that has drifted.
"""

from __future__ import annotations

import dataclasses
import difflib
import random
from pathlib import Path

import pytest

from schema_data.registry import validate_schema_yaml_text
from schema_data.sync import (
    DRAFT_COMMENT,
    STALE_COLUMN_COMMENT,
    SyncOptions,
    build_report,
    compute_plan,
    expected_tables,
    sync_schema_text,
    verify_structural_only,
)
from schema_data.yaml_text import LayoutError
from tests._sync_fixtures import catalogue, tbl

SCHEMA = '''\
# Warehouse schema for the exchange DMs.
# Curated by hand -- keep the comments.

tables:

  # ---- sales ----
  sales_dim.Broker:   # brokers
    description: "Broker master data"
    columns:
      ID: "Primary key"
      Code: "Broker code"          # dropped from the database
      Name: "Display name"         # shown to analysts

  "sales_fact.Trade":
    description: "One row per trade"
    columns:
      TradeID: "Primary key"
      # the broker who executed it
      BrokerID: "FK to broker"
    column_types:
      TradeID: "bigint"
    resolvable_columns: [BrokerID]

  # ---- shared ----
  shared_dim.Date:
    description: "Calendar"
    datasource: sales
    columns:
      DateID: "Primary key"

  # ---- described only ----
  Status:
    db_schema: sales_dim
    description: "Lookup, not queryable"

  # ---- gone ----
  Old_Dim.Nothing:
    description: "Dropped long ago"
    columns:
      ID: "Primary key"

relationships:
  - from_table: sales_fact.Trade
    to_table: sales_dim.Broker
    join_sql: "[sales_fact].[Trade].[BrokerID] = [sales_dim].[Broker].[ID]"
'''

SOURCES = ("sales", "inventory")

SALES = catalogue(
    tbl("sales_dim", "Broker", [("ID", "int"), ("Name", "nvarchar(80)"), ("Region", "nvarchar(20)")], pk=["ID"]),
    tbl("sales_dim", "Status", [("ID", "int")], pk=["ID"]),
    tbl("sales_fact", "Trade", [("TradeID", "int"), ("BrokerID", "int"), ("Amount", "decimal(18,2)")],
        pk=["TradeID"], fks=[(["BrokerID"], "sales_dim", "Broker", ["ID"])]),
    tbl("sales_fact", "Audit", [("ID", "int")]),
    tbl("shared_dim", "Date", [("DateID", "int")]),
)
INVENTORY = catalogue(
    tbl("stock_dim", "Symbol", [("SymbolID", "int")], pk=["SymbolID"]),
    tbl("shared_dim", "Date", [("DateID", "int"), ("SeqID", "int")]),
)
CATALOGUES = {"sales": SALES, "inventory": INVENTORY}


def run(text: str = SCHEMA, *, sources=SOURCES, catalogues=None, options=SyncOptions()):
    cats = catalogues or CATALOGUES
    return sync_schema_text(text, list(sources), sources[0], {s: cats[s] for s in sources}, options)


def removed_lines(before: str, after: str) -> list[str]:
    """Original lines that are not in *after* (an edit replaced or deleted them)."""
    a, b = before.split("\n"), after.split("\n")
    gone: list[str] = []
    for tag, i1, i2, _, _ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("delete", "replace"):
            gone.extend(a[i1:i2])
    return gone


def is_structural_line(line: str) -> bool:
    """A line a default (non-prune) sync is allowed to replace or drop."""
    stripped = line.strip()
    return (
        stripped.startswith("datasource:")
        or stripped.startswith("# not found in any data source")
        or stripped.startswith("# not in database")
        or stripped.split(":")[0] in ("TradeID", "ID", "DateID")  # a recorded or corrected type
    )


# ---------------------------------------------------------------------------
# Planning: what differs
# ---------------------------------------------------------------------------

class TestPlan:
    def plan(self, **kwargs):
        schema = validate_schema_yaml_text(SCHEMA)
        return {
            p.key: p
            for p in compute_plan(
                schema, SOURCES, "sales", CATALOGUES, kwargs.get("options", SyncOptions()),
            ).tables
        }

    def test_columns_the_database_has_and_schema_yaml_lacks_are_added_with_their_type(self):
        plan = self.plan()
        assert [(a.name, a.data_type) for a in plan["sales_dim.Broker"].adds] == [
            ("Region", "nvarchar(20)"),
        ]
        assert [(a.name, a.data_type) for a in plan["sales_fact.Trade"].adds] == [
            ("Amount", "decimal(18,2)"),
        ]

    def test_columns_the_database_lacks_are_stale(self):
        assert self.plan()["sales_dim.Broker"].stale == ("Code",)
        assert self.plan()["sales_fact.Trade"].stale == ()

    def test_a_recorded_type_that_differs_is_a_change_and_a_missing_one_is_recorded(self):
        trade = self.plan()["sales_fact.Trade"]
        assert [(c.column, c.old, c.new) for c in trade.changed] == [("TradeID", "bigint", "int")]
        assert trade.recorded == (("BrokerID", "int"),)

    def test_a_type_differing_only_in_case_or_spaces_is_not_a_change(self):
        text = SCHEMA.replace('TradeID: "bigint"', 'TradeID: " INT "')
        schema = validate_schema_yaml_text(text)
        plan = compute_plan(schema, SOURCES, "sales", CATALOGUES)
        assert {p.key: p for p in plan.tables}["sales_fact.Trade"].changed == ()

    def test_a_table_in_several_sources_is_placed_in_all_and_its_columns_are_the_union(self):
        date = self.plan()["shared_dim.Date"]
        assert date.placement.found_in == ("sales", "inventory")
        assert [a.name for a in date.adds] == ["SeqID"]
        assert date.shape == ("SeqID: only in inventory",)

    def test_a_type_that_differs_between_sources_is_reported(self):
        inventory = dict(INVENTORY)
        inventory[("shared_dim", "date")] = tbl("shared_dim", "Date", [("DateID", "bigint")])
        schema = validate_schema_yaml_text(SCHEMA)
        plan = compute_plan(schema, SOURCES, "sales", {"sales": SALES, "inventory": inventory})
        date = {p.key: p for p in plan.tables}["shared_dim.Date"]
        assert date.shape == ("DateID: type differs (sales int, inventory bigint)",)

    def test_a_table_with_no_columns_key_is_left_alone(self):
        status = self.plan()["Status"]
        assert status.described_only
        assert (status.adds, status.stale, status.recorded, status.changed) == ((), (), (), ())

    def test_a_table_found_nowhere_is_flagged(self):
        assert self.plan()["Old_Dim.Nothing"].nowhere

    def test_a_table_whose_columns_the_login_cannot_see_is_not_judged(self):
        sales = dict(SALES)
        sales[("sales_dim", "broker")] = tbl("sales_dim", "Broker", [])
        schema = validate_schema_yaml_text(SCHEMA)
        plan = compute_plan(schema, SOURCES, "sales", {"sales": sales, "inventory": INVENTORY})
        broker = {p.key: p for p in plan.tables}["sales_dim.Broker"]
        assert broker.unverifiable and broker.stale == () and broker.adds == ()

    def test_matching_ignores_letter_case(self):
        sales = dict(SALES)
        sales[("sales_dim", "broker")] = tbl(
            "SALES_DIM", "BROKER", [("id", "int"), ("NAME", "nvarchar(80)"), ("code", "int")],
        )
        schema = validate_schema_yaml_text(SCHEMA)
        plan = compute_plan(schema, SOURCES, "sales", {"sales": sales, "inventory": INVENTORY})
        broker = {p.key: p for p in plan.tables}["sales_dim.Broker"]
        assert broker.stale == () and broker.adds == ()

    def test_database_tables_the_schema_lacks_are_listed_not_added(self):
        schema = validate_schema_yaml_text(SCHEMA)
        plan = compute_plan(schema, SOURCES, "sales", CATALOGUES)
        assert dict(plan.unlisted) == {
            "sales_fact.Audit": ("sales",), "stock_dim.Symbol": ("inventory",),
        }
        assert plan.new_tables == ()


# ---------------------------------------------------------------------------
# Rendering: the text edit
# ---------------------------------------------------------------------------

EXPECTED_DEFAULT = '''\
# Warehouse schema for the exchange DMs.
# Curated by hand -- keep the comments.

tables:

  # ---- sales ----
  sales_dim.Broker:   # brokers
    datasource: sales
    description: "Broker master data"
    columns:
      ID: "Primary key"
      # not in database (sync_schema.py)
      Code: "Broker code"          # dropped from the database
      Name: "Display name"         # shown to analysts
      Region: "nvarchar(20) column"  # TO BE FILLED (added by sync_schema.py)
    column_types:
      ID: "int"
      Name: "nvarchar(80)"
      Region: "nvarchar(20)"

  "sales_fact.Trade":
    datasource: sales
    description: "One row per trade"
    columns:
      TradeID: "Primary key"
      # the broker who executed it
      BrokerID: "FK to broker"
      Amount: "decimal(18,2) column"  # TO BE FILLED (added by sync_schema.py)
    column_types:
      TradeID: "int"
      BrokerID: "int"
      Amount: "decimal(18,2)"
    resolvable_columns: [BrokerID]

  # ---- shared ----
  shared_dim.Date:
    description: "Calendar"
    datasource: [sales, inventory]
    columns:
      DateID: "Primary key"
      SeqID: "int column"  # TO BE FILLED (added by sync_schema.py)
    column_types:
      DateID: "int"
      SeqID: "int"

  # ---- described only ----
  Status:
    datasource: sales
    db_schema: sales_dim
    description: "Lookup, not queryable"

  # ---- gone ----
  Old_Dim.Nothing:
    # not found in any data source
    description: "Dropped long ago"
    columns:
      ID: "Primary key"

relationships:
  - from_table: sales_fact.Trade
    to_table: sales_dim.Broker
    join_sql: "[sales_fact].[Trade].[BrokerID] = [sales_dim].[Broker].[ID]"
'''


class TestDefaultMerge:
    def test_the_whole_file_after_a_default_sync(self):
        assert run().text == EXPECTED_DEFAULT

    def test_the_result_validates_with_the_projects_own_validator(self):
        parsed = validate_schema_yaml_text(run().text)
        assert parsed.tables["shared_dim.Date"].datasource == ("sales", "inventory")
        assert parsed.tables["sales_dim.Broker"].column_types["Region"] == "nvarchar(20)"

    def test_nothing_is_deleted_silently(self):
        """A default sync keeps every original column, table and comment."""
        result = run()
        for line in removed_lines(SCHEMA, result.text):
            assert is_structural_line(line), line
        before = validate_schema_yaml_text(SCHEMA)
        after = validate_schema_yaml_text(result.text)
        assert list(after.tables) == list(before.tables)
        for key, table in before.tables.items():
            assert set(table.columns or {}) <= set(after.tables[key].columns or {})

    def test_every_comment_survives(self):
        result = run()
        comments = [line for line in SCHEMA.split("\n") if line.strip().startswith("#")]
        out = result.text.split("\n")
        for line in comments:
            assert line in out, line
        assert "# dropped from the database" in result.text
        assert "# shown to analysts" in result.text

    def test_a_stale_column_is_marked_not_removed(self):
        text = run().text
        assert f"      {STALE_COLUMN_COMMENT}\n      Code:" in text
        assert validate_schema_yaml_text(text).tables["sales_dim.Broker"].columns["Code"] == "Broker code"

    def test_a_table_found_nowhere_is_marked_not_removed(self):
        assert "# not found in any data source" in run().text
        assert "Old_Dim.Nothing" in validate_schema_yaml_text(run().text).tables

    def test_curated_fields_are_untouched(self):
        before = validate_schema_yaml_text(SCHEMA)
        after = validate_schema_yaml_text(run().text)
        for key, table in before.tables.items():
            assert after.tables[key].description == table.description
            assert after.tables[key].db_schema == table.db_schema
            assert after.tables[key].resolvable_columns == table.resolvable_columns
            for column, description in (table.columns or {}).items():
                assert after.tables[key].columns[column] == description
        assert after.relationships == before.relationships

    def test_added_columns_come_last_in_database_order_with_a_draft_description(self):
        columns = validate_schema_yaml_text(run().text).tables["sales_dim.Broker"].columns
        assert list(columns) == ["ID", "Code", "Name", "Region"]
        assert columns["Region"] == "nvarchar(20) column"

    def test_added_columns_are_marked_as_drafts(self):
        assert run().text.count(DRAFT_COMMENT) == 3


class TestIdempotence:
    @pytest.mark.parametrize("options", [
        SyncOptions(),
        SyncOptions(prune=True),
        SyncOptions(add_tables=("*",)),
        SyncOptions(prune=True, add_tables=("sales_fact.*", "stock_dim.Symbol")),
    ])
    def test_running_it_on_its_own_output_changes_nothing(self, options):
        first = run(options=options)
        assert first.changed
        second = run(first.text, options=options)
        assert not second.changed
        assert second.text == first.text

    def test_a_schema_already_in_sync_is_returned_byte_for_byte(self):
        synced = run().text
        assert run(synced).text == synced

    def test_a_marker_is_not_added_twice(self):
        text = run(run().text).text
        assert text.count(STALE_COLUMN_COMMENT) == 1
        assert text.count("# not found in any data source") == 1


class TestMarkersFollowTheDatabase:
    def test_a_marked_column_that_comes_back_loses_its_marker(self):
        marked = run().text
        sales = dict(SALES)
        sales[("sales_dim", "broker")] = tbl(
            "sales_dim", "Broker",
            [("ID", "int"), ("Code", "nvarchar(10)"), ("Name", "nvarchar(80)"), ("Region", "nvarchar(20)")],
        )
        result = run(marked, catalogues={"sales": sales, "inventory": INVENTORY})
        assert STALE_COLUMN_COMMENT not in result.text
        assert result.stats.unmarked_columns == ["sales_dim.Broker.Code"]
        assert 'Code: "Broker code"          # dropped from the database' in result.text

    def test_a_marked_table_that_appears_loses_its_marker(self):
        marked = run().text
        sales = dict(SALES)
        sales[("old_dim", "nothing")] = tbl("Old_Dim", "Nothing", [("ID", "int")])
        result = run(marked, catalogues={"sales": sales, "inventory": INVENTORY})
        assert "# not found in any data source" not in result.text
        assert "datasource: sales" in result.text.split("Old_Dim.Nothing:")[1]


class TestTypes:
    def test_types_go_to_column_types_never_into_descriptions(self):
        after = validate_schema_yaml_text(run().text)
        assert after.tables["sales_fact.Trade"].columns["TradeID"] == "Primary key"
        assert after.tables["sales_fact.Trade"].column_types["TradeID"] == "int"

    def test_a_corrected_type_keeps_its_comment_and_is_reported(self):
        text = SCHEMA.replace('TradeID: "bigint"', 'TradeID: "bigint"   # legacy')
        result = run(text)
        assert 'TradeID: "int"   # legacy' in result.text
        report = build_report(result)
        assert "== column types corrected: 1 ==" in report
        assert "sales_fact.Trade.TradeID: bigint -> int" in report

    def test_a_type_without_a_comment_is_replaced_in_place(self):
        assert '      TradeID: "int"\n      BrokerID: "int"' in run().text

    def test_new_types_are_appended_to_an_existing_block(self):
        text = SCHEMA.replace(
            '    column_types:\n      TradeID: "bigint"\n',
            '    column_types:\n      TradeID: "int"\n      # note\n      BrokerID: "int"\n',
        )
        out = run(text).text
        assert '      # note\n      BrokerID: "int"\n      Amount: "decimal(18,2)"\n    resolvable_columns' in out

    def test_the_types_of_a_stale_column_are_left_as_recorded(self):
        text = SCHEMA.replace(
            '      Name: "Display name"         # shown to analysts\n',
            '      Name: "Display name"         # shown to analysts\n'
            '    column_types:\n      Code: "varchar(5)"\n',
        )
        parsed = validate_schema_yaml_text(run(text).text)
        assert parsed.tables["sales_dim.Broker"].column_types["Code"] == "varchar(5)"


class TestPrune:
    OPTIONS = SyncOptions(prune=True)

    def test_a_stale_column_is_removed_with_its_type_and_nothing_else(self):
        text = SCHEMA.replace(
            '      Name: "Display name"         # shown to analysts\n',
            '      Name: "Display name"         # shown to analysts\n'
            '    column_types:\n      Code: "varchar(5)"\n',
        )
        parsed = validate_schema_yaml_text(run(text, options=self.OPTIONS).text)
        broker = parsed.tables["sales_dim.Broker"]
        assert list(broker.columns) == ["ID", "Name", "Region"]
        assert "Code" not in broker.column_types
        assert broker.columns["Name"] == "Display name"

    def test_the_removed_columns_marker_goes_with_it(self):
        marked = run().text
        result = run(marked, options=self.OPTIONS)
        assert STALE_COLUMN_COMMENT not in result.text
        assert "Code" not in validate_schema_yaml_text(result.text).tables["sales_dim.Broker"].columns

    def test_other_columns_comments_survive(self):
        out = run(options=self.OPTIONS).text
        assert "# shown to analysts" in out and "# the broker who executed it" in out

    def test_a_table_found_nowhere_is_removed_and_the_blank_line_goes_too(self):
        out = run(options=self.OPTIONS).text
        assert "Old_Dim.Nothing" not in out
        assert "# ---- gone ----" in out
        assert "\n\n\n" not in out

    def test_a_table_the_relationships_still_name_is_kept(self):
        text = SCHEMA.replace(
            "relationships:\n",
            "relationships:\n  - from_table: Old_Dim.Nothing\n    to_table: sales_dim.Broker\n"
            "    join_sql: \"x\"\n",
        )
        result = run(text, options=self.OPTIONS)
        assert "Old_Dim.Nothing" in result.text
        assert "still named in relationships:" in build_report(result)

    def test_a_column_still_flagged_resolvable_is_kept(self):
        sales = dict(SALES)
        sales[("sales_fact", "trade")] = tbl("sales_fact", "Trade", [("TradeID", "int")])
        result = run(catalogues={"sales": sales, "inventory": INVENTORY}, options=self.OPTIONS)
        assert "BrokerID" in validate_schema_yaml_text(result.text).tables["sales_fact.Trade"].columns
        assert "still named in resolvable_columns" in build_report(result)

    def test_a_table_left_with_only_new_columns_still_has_columns(self):
        """Every column stale: the database's own columns are added first, so
        pruning can never leave a queryable table with an empty ``columns``."""
        sales = dict(SALES)
        sales[("sales_dim", "broker")] = tbl("sales_dim", "Broker", [("Other", "int")])
        result = run(catalogues={"sales": sales, "inventory": INVENTORY}, options=self.OPTIONS)
        broker = validate_schema_yaml_text(result.text).tables["sales_dim.Broker"]
        assert list(broker.columns) == ["Other"]

    def test_prune_without_the_flag_never_happens(self):
        out = run().text
        assert "Code" in out and "Old_Dim.Nothing" in out


class TestAddTables:
    def test_the_default_adds_nothing_and_only_reports(self):
        result = run()
        assert "sales_fact.Audit" not in result.text
        assert "sales_fact.Audit [sales]" in build_report(result)

    def test_a_pattern_adds_the_matching_tables_at_the_end_of_tables(self):
        result = run(options=SyncOptions(add_tables=("sales_fact.*",)))
        parsed = validate_schema_yaml_text(result.text)
        assert list(parsed.tables)[-1] == "Audit"
        audit = parsed.tables["Audit"]
        assert audit.db_schema == "sales_fact"
        assert audit.datasource == ("sales",)
        assert audit.columns == {"ID": "int column"}
        assert audit.column_types == {"ID": "int"}
        assert audit.description == "sales_fact.Audit"
        assert result.text.index("Audit:") < result.text.index("relationships:")

    def test_the_glob_is_on_schema_dot_table_and_ignores_case(self):
        result = run(options=SyncOptions(add_tables=("STOCK_DIM.sym*",)))
        assert "Symbol" in validate_schema_yaml_text(result.text).tables

    def test_a_pattern_that_matches_nothing_adds_nothing(self):
        result = run(options=SyncOptions(add_tables=("nope.*",)))
        assert result.text == run().text

    def test_added_tables_carry_the_draft_comment(self):
        out = run(options=SyncOptions(add_tables=("sales_fact.Audit",))).text
        assert f'description: "sales_fact.Audit"  {DRAFT_COMMENT}' in out
        assert f'ID: "int column"  {DRAFT_COMMENT}' in out

    def test_a_table_in_two_sources_is_added_with_a_datasource_list(self):
        sales = dict(SALES)
        sales[("ref", "ring")] = tbl("ref", "Ring", [("ID", "int")])
        inventory = dict(INVENTORY)
        inventory[("ref", "ring")] = tbl("ref", "Ring", [("ID", "int"), ("Code", "varchar(5)")])
        result = run(catalogues={"sales": sales, "inventory": inventory},
                     options=SyncOptions(add_tables=("ref.Ring",)))
        ring = validate_schema_yaml_text(result.text).tables["Ring"]
        assert ring.datasource == ("sales", "inventory")
        assert list(ring.columns) == ["ID", "Code"]

    def test_a_view_is_added_as_a_view(self):
        sales = dict(SALES)
        sales[("sales_dim", "v_broker")] = tbl("sales_dim", "v_Broker", [("ID", "int")], view=True)
        result = run(catalogues={"sales": sales, "inventory": INVENTORY},
                     options=SyncOptions(add_tables=("sales_dim.v_*",)))
        assert validate_schema_yaml_text(result.text).tables["v_Broker"].description == "sales_dim.v_Broker (view)"

    def test_a_bare_name_that_already_exists_in_another_schema_is_written_qualified(self):
        sales = dict(SALES)
        sales[("ref", "broker")] = tbl("ref", "Broker", [("ID", "int")])
        result = run(catalogues={"sales": sales, "inventory": INVENTORY},
                     options=SyncOptions(add_tables=("ref.Broker",)))
        parsed = validate_schema_yaml_text(result.text)
        assert "ref.Broker" in parsed.tables
        assert parsed.tables["ref.Broker"].db_schema == ""   # the key carries the schema
        assert "sales_dim.Broker" in parsed.tables

    def test_two_new_tables_with_one_bare_name_are_both_qualified(self):
        sales = dict(SALES)
        sales[("a", "thing")] = tbl("a", "Thing", [("ID", "int")])
        sales[("b", "thing")] = tbl("b", "Thing", [("ID", "int")])
        result = run(catalogues={"sales": sales, "inventory": INVENTORY},
                     options=SyncOptions(add_tables=("a.Thing", "b.Thing")))
        assert {"a.Thing", "b.Thing"} <= set(validate_schema_yaml_text(result.text).tables)

    def test_a_name_that_is_not_an_identifier_is_bracketed_in_the_key(self):
        sales = dict(SALES)
        sales[("sales_dim", "order items")] = tbl("sales_dim", "Order Items", [("Total Amount", "int")])
        result = run(catalogues={"sales": sales, "inventory": INVENTORY},
                     options=SyncOptions(add_tables=("sales_dim.Order*",)))
        parsed = validate_schema_yaml_text(result.text)
        assert "[Order Items]" in parsed.tables
        assert list(parsed.tables["[Order Items]"].columns) == ["Total Amount"]

    def test_a_table_the_login_sees_no_column_of_is_not_added(self):
        sales = dict(SALES)
        sales[("sales_dim", "hidden")] = tbl("sales_dim", "Hidden", [])
        result = run(catalogues={"sales": sales, "inventory": INVENTORY},
                     options=SyncOptions(add_tables=("sales_dim.Hidden",)))
        assert "Hidden" not in validate_schema_yaml_text(result.text).tables
        assert "the login sees none of its columns" in build_report(result)

    def test_single_source_tables_get_no_datasource_line(self):
        result = run(sources=("sales",), options=SyncOptions(add_tables=("sales_fact.Audit",)))
        assert validate_schema_yaml_text(result.text).tables["Audit"].datasource == ()


class TestSchemaQualifiedKeys:
    TEXT = (
        "tables:\n"
        "  sales.Order:\n"
        "    columns:\n"
        "      ID: pk\n"
        "  ref.Order:\n"
        "    columns:\n"
        "      ID: pk\n"
        "  Customer:\n"
        "    db_schema: OtherDb.sales\n"
        "    columns:\n"
        "      ID: pk\n"
    )
    CATALOGUE = catalogue(
        tbl("sales", "Order", [("ID", "int"), ("Total", "money")]),
        tbl("ref", "Order", [("ID", "int"), ("Code", "char(3)")]),
        tbl("sales", "Customer", [("ID", "int"), ("Name", "nvarchar(40)")]),
    )

    def test_the_same_bare_name_in_two_schemas_is_matched_by_schema(self):
        result = run(self.TEXT, sources=("a",), catalogues={"a": self.CATALOGUE})
        tables = validate_schema_yaml_text(result.text).tables
        assert list(tables["sales.Order"].columns) == ["ID", "Total"]
        assert list(tables["ref.Order"].columns) == ["ID", "Code"]

    def test_a_multi_part_db_schema_is_matched_on_its_last_part(self):
        result = run(self.TEXT, sources=("a",), catalogues={"a": self.CATALOGUE})
        assert list(validate_schema_yaml_text(result.text).tables["Customer"].columns) == ["ID", "Name"]

    def test_a_table_is_not_confused_with_the_same_name_in_another_schema(self):
        only_ref = catalogue(tbl("ref", "Order", [("ID", "int")]))
        result = run(self.TEXT, sources=("a",), catalogues={"a": only_ref})
        assert result.plan.tables[0].nowhere            # sales.Order
        assert not result.plan.tables[1].nowhere        # ref.Order


class TestSharedTables:
    def test_a_shared_table_gets_a_list_in_datasources_order(self):
        assert "datasource: [sales, inventory]" in run().text

    def test_a_wrong_single_source_is_repaired_in_place_keeping_its_comment(self):
        text = SCHEMA.replace("datasource: sales\n", "datasource: sales   # copied\n")
        assert "datasource: [sales, inventory]  # copied" in run(text).text

    def test_a_block_list_is_replaced_whole(self):
        text = SCHEMA.replace("datasource: sales\n", "datasource:\n      - sales\n")
        out = run(text).text
        assert "datasource: [sales, inventory]" in out and "      - sales" not in out

    def test_the_default_source_is_named_explicitly(self):
        assert "  sales_dim.Broker:   # brokers\n    datasource: sales\n" in run().text

    def test_one_data_source_writes_no_datasource_lines(self):
        result = run(sources=("sales",), catalogues={"sales": SALES})
        assert "datasource:" not in result.text.replace("datasource: sales\n    columns:\n      DateID", "")
        assert validate_schema_yaml_text(result.text).tables["sales_dim.Broker"].datasource == ()

    def test_a_table_found_in_the_wrong_source_only_is_repaired(self):
        text = SCHEMA.replace("  Status:\n", "  Status:\n    datasource: inventory\n")
        assert "datasource: sales" in run(text).text.split("Status:")[1].split("---- gone")[0]


# ---------------------------------------------------------------------------
# Layout, line endings, refusals
# ---------------------------------------------------------------------------

class TestLayout:
    def test_windows_line_endings_survive(self):
        crlf = SCHEMA.replace("\n", "\r\n")
        result = run(crlf)
        assert "\n" not in result.text.replace("\r\n", "")
        assert validate_schema_yaml_text(result.text).tables["sales_dim.Broker"].column_types

    def test_a_missing_final_newline_stays_missing(self):
        text = SCHEMA.rstrip("\n")
        assert not run(text).text.endswith("\n")

    def test_the_files_own_indentation_is_used(self):
        text = (
            "tables:\n"
            "    T:\n"
            "        description: d\n"
            "        columns:\n"
            "            ID: pk\n"
        )
        cat = catalogue(tbl("", "T", [("ID", "int"), ("Note", "text")]))
        out = run(text, sources=("a",), catalogues={"a": {("dbo", "t"): cat[("", "t")]}}).text
        assert '            Note: "text column"' in out
        assert "        column_types:\n            ID: \"int\"" in out

    def test_a_flow_style_columns_map_is_refused_by_name(self):
        text = "tables:\n  T:\n    columns: {ID: pk}\n"
        with pytest.raises(LayoutError, match="inline"):
            run(text, sources=("a",), catalogues={"a": {("dbo", "t"): tbl("dbo", "T", [("ID", "int")])}})

    def test_a_flow_style_table_is_refused_by_name(self):
        text = "tables:\n  T: {columns: {ID: pk}}\n"
        with pytest.raises(LayoutError, match="T"):
            run(text, sources=("a",), catalogues={"a": {("dbo", "t"): tbl("dbo", "T", [("ID", "int")])}})

    def test_an_invalid_schema_is_refused(self):
        with pytest.raises(ValueError, match="datasource"):
            run("tables:\n  A:\n    datasource: []\n")

    @pytest.mark.parametrize("text", ["relationships: []\n", "tables: {}\n"])
    def test_a_file_with_no_block_tables_mapping_cannot_take_added_tables(self, text):
        with pytest.raises(LayoutError, match="tables"):
            run(text, options=SyncOptions(add_tables=("*",)))


# ---------------------------------------------------------------------------
# The structural-only guarantee
# ---------------------------------------------------------------------------

class TestStructuralGuarantee:
    def _parts(self):
        schema = validate_schema_yaml_text(SCHEMA)
        plan = compute_plan(schema, SOURCES, "sales", CATALOGUES)
        return schema, plan, run().text

    def test_the_output_equals_the_original_with_exactly_the_plan_applied(self):
        schema, plan, text = self._parts()
        verify_structural_only(schema, validate_schema_yaml_text(text), plan)
        assert list(expected_tables(schema, plan)) == list(validate_schema_yaml_text(text).tables)

    @pytest.mark.parametrize("tamper", [
        lambda t: t.replace("Broker master data", "Changed"),
        lambda t: t.replace('ID: "Primary key"', 'ID: "Changed"', 1),
        lambda t: t.replace("resolvable_columns: [BrokerID]", "resolvable_columns: []"),
        lambda t: t.replace("db_schema: sales_dim", "db_schema: other"),
        lambda t: t.replace('join_sql: "[sales_fact]', 'join_sql: "[x]'),
        lambda t: t.replace("Display name", "Shown name"),
    ])
    def test_a_changed_curated_field_is_caught(self, tamper):
        schema, plan, text = self._parts()
        with pytest.raises(ValueError):
            verify_structural_only(schema, validate_schema_yaml_text(tamper(text)), plan)

    def test_a_reordered_column_list_is_caught(self):
        schema, plan, text = self._parts()
        tampered = text.replace(
            '      ID: "Primary key"\n      # not in database (sync_schema.py)\n      Code: "Broker code"          # dropped from the database\n',
            '      # not in database (sync_schema.py)\n      Code: "Broker code"          # dropped from the database\n      ID: "Primary key"\n',
        )
        assert tampered != text
        with pytest.raises(ValueError, match="reordered"):
            verify_structural_only(schema, validate_schema_yaml_text(tampered), plan)

    def test_a_table_the_plan_does_not_know_is_caught(self):
        schema, plan, text = self._parts()
        short = dataclasses.replace(plan, tables=plan.tables[:-1])
        with pytest.raises(ValueError, match="different tables"):
            verify_structural_only(schema, validate_schema_yaml_text(text), short)

    def test_a_column_the_plan_does_not_know_is_caught(self):
        schema, plan, text = self._parts()
        tampered = text.replace(
            '      Name: "Display name"         # shown to analysts\n',
            '      Name: "Display name"         # shown to analysts\n      Extra: "x"\n',
        )
        with pytest.raises(ValueError, match="unexpectedly"):
            verify_structural_only(schema, validate_schema_yaml_text(tampered), plan)

    def test_a_renderer_that_edits_a_description_fails_the_run(self, monkeypatch):
        import schema_data.sync as sync

        real = sync.render_synced_text

        def sabotaged(original, schema, plan):
            text, stats = real(original, schema, plan)
            return text.replace("Calendar", "Calendars"), stats

        monkeypatch.setattr(sync, "render_synced_text", sabotaged)
        with pytest.raises(ValueError, match="unexpectedly"):
            run()


class TestCuratedFieldsOfTheExampleSchema:
    """Property-style: whatever the databases look like, the example schema's
    curated text comes through a sync unchanged."""

    EXAMPLE = Path(__file__).resolve().parent.parent / "project_config.example" / "schema.yaml"

    def _catalogue(self, schema, rng):
        """A random database for the example schema: some columns gone, some
        new, some types odd, some tables missing, some extra tables."""
        tables = []
        for key, table in schema.tables.items():
            if rng.random() < 0.15:
                continue                                        # table missing
            names = list(table.columns or {"ID": ""})
            names = [n for n in names if rng.random() > 0.2] or names[:1]      # columns gone
            columns = [(n, rng.choice(["int", "nvarchar(50)", "decimal(18,2)", "datetime2"])) for n in names]
            columns += [(f"Extra{i}", "bit") for i in range(rng.randint(0, 2))]  # new columns
            qualifier = table.db_schema or "dbo"
            tables.append(tbl(qualifier.split(".")[-1], key.split(".")[-1], columns, pk=[names[0]]))
        tables.append(tbl("sales", "Brand New", [("ID", "int")]))
        return catalogue(*tables)

    @pytest.mark.parametrize("seed", range(40))
    @pytest.mark.parametrize("options", [
        SyncOptions(),
        SyncOptions(prune=True, add_tables=("*",)),
    ], ids=["default", "prune+add"])
    def test_descriptions_flags_and_comments_survive(self, seed, options):
        original = self.EXAMPLE.read_text(encoding="utf-8")
        schema = validate_schema_yaml_text(original)
        rng = random.Random(seed)
        cats = {"a": self._catalogue(schema, rng), "b": self._catalogue(schema, rng)}
        result = sync_schema_text(original, ["a", "b"], "a", cats, options)
        out = validate_schema_yaml_text(result.text)

        # Idempotent.
        assert sync_schema_text(result.text, ["a", "b"], "a", cats, options).text == result.text

        # Every table that is still there keeps every curated field; every
        # column that is still there keeps its description and its order.
        removed_tables = set(schema.tables) - set(out.tables)
        if not options.prune:
            assert not removed_tables
        for key, table in schema.tables.items():
            if key in removed_tables:
                continue
            kept = out.tables[key]
            assert kept.description == table.description
            assert kept.db_schema == table.db_schema
            assert kept.resolvable_columns == table.resolvable_columns
            assert kept.prefetchable_columns == table.prefetchable_columns
            assert (kept.columns is None) == (table.columns is None)
            if table.columns is not None:
                survivors = [c for c in table.columns if c in kept.columns]
                assert [c for c in kept.columns if c in table.columns] == survivors
                for column in survivors:
                    assert kept.columns[column] == table.columns[column]
                if not options.prune:
                    assert survivors == list(table.columns)
        assert out.relationships == schema.relationships
        # The order of the original tables is kept.
        assert [k for k in out.tables if k in schema.tables] == [k for k in schema.tables if k in out.tables]

        # Comments: every comment line outside a removed table survives.
        out_lines = result.text.split("\n")
        for line in original.split("\n"):
            if line.strip().startswith("#") and (line.startswith("#") or not options.prune):
                assert line in out_lines, line
        assert out.tables is not None

    def test_the_example_schema_is_a_valid_input_with_no_column_types(self):
        schema = validate_schema_yaml_text(self.EXAMPLE.read_text(encoding="utf-8"))
        assert all(not t.column_types for t in schema.tables.values())


class TestReport:
    def test_it_names_tables_columns_and_counts_only(self):
        report = build_report(run())
        assert "== columns added (in the database, not in schema.yaml): 3 ==" in report
        assert "sales_dim.Broker.Region nvarchar(20)" in report
        assert "sales_dim.Broker.Code (kept, marked)" in report
        assert "== tables in schema.yaml found in no data source: 1 ==" in report
        assert "Old_Dim.Nothing (kept, marked)" in report
        assert "sales_dim.Broker: (none) -> sales" in report

    def test_a_long_section_is_cut_and_counted(self):
        names = [(f"C{i}", "int") for i in range(260)]
        text = "tables:\n  T:\n    columns:\n      ID: pk\n"
        cats = {"a": catalogue(tbl("", "T", [("ID", "int"), *names]))}
        result = sync_schema_text(
            text, ["a"], "a", {"a": {("dbo", "t"): cats["a"][("", "t")]}},
        )
        report = build_report(result)
        assert "== columns added (in the database, not in schema.yaml): 260 ==" in report
        assert "... and 60 more" in report

    def test_a_removed_item_says_so(self):
        report = build_report(run(options=SyncOptions(prune=True)))
        assert "sales_dim.Broker.Code (removed)" in report
        assert "Old_Dim.Nothing (removed)" in report


# ---------------------------------------------------------------------------
# A file written before column_types: existed is not out of step
# ---------------------------------------------------------------------------

#: Written before 6.9.0: every column listed, no ``column_types:`` map at all.
PRE_RECORDED = "tables:\n  T:\n    db_schema: s\n    columns:\n      ID: pk\n      Name: a name\n"
PRE_RECORDED_CATALOGUE = {"a": catalogue(tbl("s", "T", [("ID", "int"), ("Name", "nvarchar(50)")]))}


def run_single(text: str, cats=None, options=SyncOptions()):
    return sync_schema_text(text, ["a"], "a", cats or PRE_RECORDED_CATALOGUE, options)


class TestOnlyRecordsTypes:
    def test_a_file_with_no_column_types_only_needs_them_recorded(self):
        result = run_single(PRE_RECORDED)
        assert result.changed and result.only_records_types
        assert result.types_to_record == 2
        assert "column_types:" in result.text and 'Name: "nvarchar(50)"' in result.text

    def test_the_text_without_the_records_is_the_input(self):
        """The decision is the rendered text, not a list of cases."""
        from schema_data.sync import render_synced_text

        schema = validate_schema_yaml_text(PRE_RECORDED)
        plan = compute_plan(schema, ["a"], "a", PRE_RECORDED_CATALOGUE)
        bare = dataclasses.replace(plan, tables=tuple(dataclasses.replace(t, recorded=()) for t in plan.tables))
        assert render_synced_text(PRE_RECORDED, schema, bare)[0] == PRE_RECORDED

    def test_one_missing_entry_in_a_partly_recorded_file_counts_too(self):
        result = run_single(PRE_RECORDED + "    column_types:\n      ID: int\n")
        assert result.only_records_types and result.types_to_record == 1

    def test_a_file_that_records_every_type_is_in_sync(self):
        result = run_single(PRE_RECORDED + "    column_types:\n      ID: int\n      Name: nvarchar(50)\n")
        assert not result.changed and not result.only_records_types and result.types_to_record == 0

    def test_a_column_the_database_has_is_drift(self):
        cats = {"a": catalogue(tbl("s", "T", [("ID", "int"), ("Name", "nvarchar(50)"), ("Extra", "int")]))}
        result = run_single(PRE_RECORDED, cats)
        assert result.changed and not result.only_records_types
        assert result.types_to_record == 2

    def test_a_recorded_type_that_differs_is_drift(self):
        result = run_single(PRE_RECORDED + "    column_types:\n      ID: bigint\n")
        assert not result.only_records_types
        assert result.types_to_record == 1 and len(result.plan.tables[0].changed) == 1

    def test_a_column_the_database_lost_is_drift(self):
        cats = {"a": catalogue(tbl("s", "T", [("ID", "int")]))}
        result = run_single(PRE_RECORDED, cats)
        assert not result.only_records_types and result.stats.marked_columns == ["T.Name"]

    def test_pruning_is_an_effect_beyond_recording(self):
        cats = {"a": catalogue(tbl("s", "T", [("ID", "int")]))}
        text = PRE_RECORDED.replace("      Name: a name\n", "      # not in database (sync_schema.py)\n      Name: a name\n")
        assert run_single(text, cats).only_records_types
        assert not run_single(text, cats, SyncOptions(prune=True)).only_records_types

    def test_a_table_the_database_lost_is_drift(self):
        result = run_single(PRE_RECORDED, {"a": catalogue()})
        assert not result.only_records_types and result.stats.marked_tables == ["T"]

    def test_a_datasource_line_to_set_is_drift(self):
        cats = {"a": catalogue(tbl("s", "T", [("ID", "int"), ("Name", "nvarchar(50)")])), "b": catalogue()}
        result = sync_schema_text(PRE_RECORDED, ["a", "b"], "a", cats)
        assert "datasource:" in result.text and not result.only_records_types

    def test_adding_tables_is_an_effect_beyond_recording(self):
        cats = {"a": catalogue(
            tbl("s", "T", [("ID", "int"), ("Name", "nvarchar(50)")]), tbl("s", "U", [("ID", "int")]),
        )}
        assert run_single(PRE_RECORDED, cats).only_records_types
        assert not run_single(PRE_RECORDED, cats, SyncOptions(add_tables=("s.U",))).only_records_types

    def test_a_file_with_real_drift_is_not_a_records_only_file(self):
        result = run()
        assert result.changed and not result.only_records_types
        assert not run(result.text).changed


class TestDescribeDrift:
    @staticmethod
    def result(**plan_fields):
        from schema_data.placement import Placement
        from schema_data.sync import RenderStats, SyncPlan, SyncResult, TablePlan

        placement = Placement("T", ("a",), ("a",), ("a",), {})
        tables = (TablePlan("T", placement, **plan_fields),)
        return SyncResult(SyncPlan(("a",), "a", tables), "changed", True, RenderStats())

    def test_unrecorded_types_are_named_with_the_command(self):
        from schema_data.sync import unrecorded_types_note

        note = unrecorded_types_note(run_single(PRE_RECORDED))
        assert note.startswith("2 column type(s) not recorded yet -- run python scripts/sync_schema.py once")
        assert "schema.synced.yaml" in note and "later type changes are detected" in note

    def test_real_drift_with_types_to_record_adds_their_count(self):
        from schema_data.sync import ColumnAdd, describe_drift

        detail = describe_drift(self.result(
            adds=(ColumnAdd("X", "int", ("a",)),), recorded=(("ID", "int"), ("Name", "text")),
        ))
        assert detail.startswith("1 column(s) missing from schema.yaml; ")
        assert "0 column type(s) differ" in detail and "2 column type(s) to record" in detail
        assert "python scripts/sync_schema.py" in detail

    def test_a_count_of_nothing_to_record_is_not_listed(self):
        from schema_data.sync import ColumnAdd, describe_drift

        assert "to record" not in describe_drift(self.result(adds=(ColumnAdd("X", "int", ("a",)),)))

    def test_a_changed_result_whose_counts_are_all_zero_says_so_instead(self):
        from schema_data.sync import UNCOUNTED_DRIFT_NOTE, describe_drift

        detail = describe_drift(self.result())
        assert detail == UNCOUNTED_DRIFT_NOTE
        assert "0 column" not in detail and "--dry-run" in detail

    def test_the_fallback_keeps_a_types_to_record_count(self):
        from schema_data.sync import UNCOUNTED_DRIFT_NOTE, describe_drift

        detail = describe_drift(self.result(recorded=(("ID", "int"),)))
        assert detail == f"1 column type(s) to record; {UNCOUNTED_DRIFT_NOTE}"

    def test_pruned_and_added_tables_are_counted_not_reported_as_uncovered(self):
        from schema_data.sync import describe_drift

        cats = {"a": catalogue(tbl("s", "T", [("ID", "int")]), tbl("s", "U", [("ID", "int")]))}
        text = PRE_RECORDED.replace("      Name: a name\n", "      # not in database (sync_schema.py)\n      Name: a name\n")
        detail = describe_drift(run_single(text, cats, SyncOptions(prune=True, add_tables=("s.U",))))
        assert "1 column(s) and 0 table(s) to remove (--prune)" in detail
        assert "1 table(s) to add (--add-tables)" in detail
        assert "do not cover" not in detail
