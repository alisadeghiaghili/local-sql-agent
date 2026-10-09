# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``schema_data.relationship_proposals``: declared foreign keys
and the ``X_ID -> X.ID`` naming convention, including what must NOT be
inferred. A fake catalogue stands in for the databases."""

from __future__ import annotations

import yaml

from schema_data.registry import validate_schema_yaml_text
from schema_data.relationship_proposals import (
    propose_relationships,
    render_proposals_yaml,
)
from tests._sync_fixtures import catalogue, tbl


def schema_for(*tables: str, extra: str = "") -> object:
    """A schema.yaml with the given ``Key|db_schema|col,col`` tables (``|-`` = no columns)."""
    lines = ["tables:"]
    for spec in tables:
        key, schema, columns = spec.split("|")
        lines.append(f"  {key}:")
        if schema:
            lines.append(f"    db_schema: {schema}")
        if columns != "-":
            lines.append("    columns:")
            lines.extend(f"      {c}: x" for c in columns.split(","))
    return validate_schema_yaml_text("\n".join(lines) + "\n" + extra)


def pairs(result):
    return [(p.from_table, p.from_column, p.to_table, p.to_column) for p in result.proposals]


INT = "int"


class TestDeclaredForeignKeys:
    def setup_method(self):
        self.schema = schema_for("Order|sales|ID,CustomerID", "Customer|sales|ID")
        self.cat = {"a": catalogue(
            tbl("sales", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"],
                fks=[(["CustomerID"], "sales", "Customer", ["ID"])]),
            tbl("sales", "Customer", [("ID", INT)], pk=["ID"]),
        )}

    def test_a_declared_key_is_a_proposal_with_its_constraint_as_the_basis(self):
        result = propose_relationships(self.schema, ["a"], self.cat)
        (p,) = result.proposals
        assert (p.from_table, p.from_schema, p.from_column) == ("Order", "sales", "CustomerID")
        assert (p.to_table, p.to_schema, p.to_column) == ("Customer", "sales", "ID")
        assert p.basis == "declared foreign key FK_Order_0"
        assert p.confidence == "declared"
        assert p.join_hint == "JOIN [sales].[Customer] ON [sales].[Order].[CustomerID] = [sales].[Customer].[ID]"
        assert (result.declared, result.inferred) == (1, 0)

    def test_a_declared_key_is_not_also_guessed_from_the_name(self):
        result = propose_relationships(self.schema, ["a"], self.cat)
        assert len(result.proposals) == 1 and result.inferred == 0

    def test_the_declared_key_wins_over_a_name_that_points_elsewhere(self):
        schema = schema_for("Order|sales|ID,CustomerID", "Customer|sales|ID", "Party|sales|ID")
        cat = {"a": catalogue(
            tbl("sales", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"],
                fks=[(["CustomerID"], "sales", "Party", ["ID"])]),
            tbl("sales", "Customer", [("ID", INT)], pk=["ID"]),
            tbl("sales", "Party", [("ID", INT)], pk=["ID"]),
        )}
        assert pairs(propose_relationships(schema, ["a"], cat)) == [("Order", "CustomerID", "Party", "ID")]

    def test_a_composite_key_is_one_proposal_with_an_and_join(self):
        schema = schema_for("Line|sales|OrderID,Seq", "Order|sales|OrderID,Seq")
        cat = {"a": catalogue(
            tbl("sales", "Line", [("OrderID", INT), ("Seq", INT)],
                fks=[(["OrderID", "Seq"], "sales", "Order", ["OrderID", "Seq"])]),
            tbl("sales", "Order", [("OrderID", INT), ("Seq", INT)], pk=["OrderID", "Seq"]),
        )}
        (p,) = propose_relationships(schema, ["a"], cat).proposals
        assert (p.from_column, p.to_column) == ("OrderID, Seq", "OrderID, Seq")
        assert " AND " in p.join_hint

    def test_a_key_to_a_table_schema_yaml_does_not_list_is_dropped_and_said_so(self):
        schema = schema_for("Order|sales|ID,CustomerID")
        result = propose_relationships(schema, ["a"], self.cat)
        assert result.proposals == ()
        assert result.skipped == (("Order.CustomerID -> customer", "the referenced table is not in schema.yaml with columns"),)

    def test_a_key_to_a_described_only_table_is_dropped(self):
        schema = schema_for("Order|sales|ID,CustomerID", "Customer|sales|-")
        assert propose_relationships(schema, ["a"], self.cat).proposals == ()

    def test_a_key_column_missing_from_schema_yaml_is_dropped(self):
        schema = schema_for("Order|sales|ID", "Customer|sales|ID")
        result = propose_relationships(schema, ["a"], self.cat)
        assert result.proposals == ()
        assert result.skipped[0][1] == "a key column is not listed in schema.yaml"

    def test_a_key_declared_in_two_sources_is_one_proposal_naming_both(self):
        both = {"a": self.cat["a"], "b": self.cat["a"]}
        (p,) = propose_relationships(self.schema, ["a", "b"], both).proposals
        assert p.datasource == ("a", "b")

    def test_a_single_source_proposal_has_no_datasource(self):
        (p,) = propose_relationships(self.schema, ["a"], self.cat).proposals
        assert p.datasource == ()


class TestNamingConvention:
    def run(self, tables, infos, sources=("a",), existing=(), extra=""):
        schema = schema_for(*tables, extra=extra)
        cats = {s: catalogue(*i) for s, i in zip(sources, infos)} if isinstance(infos[0], list) else {
            "a": catalogue(*infos)}
        return propose_relationships(schema, list(cats), cats, existing)

    def basic(self, column="CustomerID", target_pk=("ID",), target_cols=(("ID", INT),),
              source_type=INT, target_type=None):
        order = tbl("sales", "Order", [("ID", INT), (column, source_type)], pk=["ID"])
        customer = tbl("sales", "Customer", list(target_cols), pk=list(target_pk))
        cols = ",".join(c for c, _ in target_cols)
        return self.run(["Order|sales|ID," + column, f"Customer|sales|{cols}"], [order, customer])

    def test_x_id_points_at_x_dot_id(self):
        result = self.basic()
        (p,) = result.proposals
        assert pairs(result) == [("Order", "CustomerID", "Customer", "ID")]
        assert p.basis == "naming convention: CustomerID -> Customer.ID"
        assert p.confidence == "high"
        assert (result.declared, result.inferred) == (0, 1)

    def test_the_common_spellings_of_the_suffix(self):
        for column in ("CustomerID", "Customer_ID", "CustomerId", "Customer_Id", "customer_id", "CUSTOMER_ID"):
            assert pairs(self.basic(column)) == [("Order", column, "Customer", "ID")], column

    def test_a_role_prefixed_column_points_at_the_table_that_ends_its_stem(self):
        schema = ["Order|sales|ID,OrderDate_ID", "Date|sales|ID"]
        infos = [tbl("sales", "Order", [("ID", INT), ("OrderDate_ID", INT)], pk=["ID"]),
                 tbl("sales", "Date", [("ID", INT)], pk=["ID"])]
        result = self.run(schema, infos)
        (p,) = result.proposals
        assert (p.from_column, p.to_table, p.to_column) == ("OrderDate_ID", "Date", "ID")
        assert p.confidence == "medium"

    def test_camel_case_role_prefix(self):
        infos = [tbl("sales", "Order", [("ID", INT), ("ShipDateID", INT)], pk=["ID"]),
                 tbl("sales", "Date", [("ID", INT)], pk=["ID"])]
        assert pairs(self.run(["Order|sales|ID,ShipDateID", "Date|sales|ID"], infos)) == [
            ("Order", "ShipDateID", "Date", "ID")]

    def test_the_longest_matching_table_name_wins(self):
        infos = [tbl("s", "Line", [("ID", INT), ("OrderDateID", INT)], pk=["ID"]),
                 tbl("s", "Date", [("ID", INT)], pk=["ID"]),
                 tbl("s", "OrderDate", [("ID", INT)], pk=["ID"])]
        assert pairs(self.run(["Line|s|ID,OrderDateID", "Date|s|ID", "OrderDate|s|ID"], infos)) == [
            ("Line", "OrderDateID", "OrderDate", "ID")]

    def test_the_target_is_the_declared_primary_key_even_when_not_called_id(self):
        result = self.basic(target_pk=("CustomerKey",), target_cols=(("CustomerKey", INT),))
        assert pairs(result) == [("Order", "CustomerID", "Customer", "CustomerKey")]

    def test_with_no_declared_key_a_column_named_id_is_the_target_at_medium_confidence(self):
        result = self.basic(target_pk=())
        (p,) = result.proposals
        assert p.to_column == "ID" and p.confidence == "medium"

    def test_a_self_reference_by_role_is_kept(self):
        infos = [tbl("s", "Category", [("ID", INT), ("ParentCategoryID", INT)], pk=["ID"])]
        assert pairs(self.run(["Category|s|ID,ParentCategoryID"], infos)) == [
            ("Category", "ParentCategoryID", "Category", "ID")]

    def test_the_join_is_qualified_with_the_schema_of_each_side(self):
        schema = ["Order|sales|ID,RingID", "Ring|ref|ID"]
        infos = [tbl("sales", "Order", [("ID", INT), ("RingID", INT)], pk=["ID"]),
                 tbl("ref", "Ring", [("ID", INT)], pk=["ID"])]
        (p,) = self.run(schema, infos).proposals
        assert (p.from_schema, p.to_schema) == ("sales", "ref")
        assert p.join_hint == "JOIN [ref].[Ring] ON [sales].[Order].[RingID] = [ref].[Ring].[ID]"

    def test_a_table_in_another_database_keeps_its_three_part_name(self):
        schema = validate_schema_yaml_text(
            "tables:\n  Order:\n    db_schema: sales\n    columns: {ID: x, RingID: x}\n"
            "  Ring:\n    db_schema: OtherDb.ref\n    columns: {ID: x}\n"
        )
        cat = {"a": catalogue(
            tbl("sales", "Order", [("ID", INT), ("RingID", INT)], pk=["ID"]),
            tbl("ref", "Ring", [("ID", INT)], pk=["ID"]),
        )}
        (p,) = propose_relationships(schema, ["a"], cat).proposals
        assert p.to_schema == "OtherDb.ref"
        assert "[OtherDb].[ref].[Ring]" in p.join_hint

    def test_the_same_bare_name_in_two_schemas_is_resolved_by_the_source_tables_schema(self):
        schema = ["Order|sales|ID,CustomerID", "sales.Customer||ID", "ref.Customer||ID"]
        infos = [tbl("sales", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"]),
                 tbl("sales", "Customer", [("ID", INT)], pk=["ID"]),
                 tbl("ref", "Customer", [("ID", INT)], pk=["ID"])]
        (p,) = self.run(schema, infos).proposals
        assert (p.to_table, p.to_schema) == ("Customer", "sales")

    def test_both_tables_must_share_a_data_source(self):
        order = tbl("s", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"])
        customer = tbl("s", "Customer", [("ID", INT)], pk=["ID"])
        schema = schema_for("Order|s|ID,CustomerID", "Customer|s|ID")
        cats = {"a": catalogue(order), "b": catalogue(customer)}
        assert propose_relationships(schema, ["a", "b"], cats).proposals == ()

    def test_with_several_sources_the_proposal_names_the_shared_ones(self):
        order = tbl("s", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"])
        customer = tbl("s", "Customer", [("ID", INT)], pk=["ID"])
        schema = schema_for("Order|s|ID,CustomerID", "Customer|s|ID")
        cats = {"a": catalogue(order, customer), "b": catalogue(customer)}
        (p,) = propose_relationships(schema, ["a", "b"], cats).proposals
        assert p.datasource == ("a",)


class TestNamingConventionNegativeCases:
    def run(self, tables, infos, **kwargs):
        schema = schema_for(*tables)
        return propose_relationships(schema, ["a"], {"a": catalogue(*infos)}, **kwargs)

    def order_and(self, column, other_name="Customer", other_pk=("ID",), other_cols=(("ID", INT),),
                  column_type=INT, other_listed=None):
        order = tbl("s", "Order", [("ID", INT), (column, column_type)], pk=["ID"])
        other = tbl("s", other_name, list(other_cols), pk=list(other_pk))
        listed = ",".join(other_listed or [c for c, _ in other_cols])
        return self.run([f"Order|s|ID,{column}", f"{other_name}|s|{listed}"], [order, other])

    def test_a_column_called_id_is_a_key_not_a_link(self):
        infos = [tbl("s", "Order", [("ID", INT)], pk=["ID"]), tbl("s", "Customer", [("ID", INT)], pk=["ID"])]
        assert self.run(["Order|s|ID", "Customer|s|ID"], infos).proposals == ()

    def test_a_stem_that_names_no_table_proposes_nothing_and_says_nothing(self):
        result = self.order_and("WarehouseID")
        assert result.proposals == () and result.skipped == ()

    def test_a_tables_own_key_is_not_a_link(self):
        infos = [tbl("s", "Order", [("ID", INT), ("OrderID", INT)], pk=["ID"])]
        result = self.run(["Order|s|ID,OrderID"], infos)
        assert result.proposals == () and result.skipped == ()

    def test_a_lowercase_id_ending_inside_a_word_is_not_an_id(self):
        for column in ("Paid", "Valid", "Void", "Android", "Squid"):
            assert self.order_and(column).proposals == (), column

    def test_a_table_name_found_inside_a_word_without_a_boundary_is_not_a_match(self):
        """``UpdateID`` ends in ``date`` but not at a word boundary."""
        infos = [tbl("s", "Log", [("ID", INT), ("UpdateID", INT)], pk=["ID"]),
                 tbl("s", "Date", [("ID", INT)], pk=["ID"])]
        assert self.run(["Log|s|ID,UpdateID", "Date|s|ID"], infos).proposals == ()

    def test_a_very_short_table_name_is_never_matched_by_suffix(self):
        infos = [tbl("s", "Log", [("ID", INT), ("MyAbID", INT)], pk=["ID"]),
                 tbl("s", "Ab", [("ID", INT)], pk=["ID"])]
        assert self.run(["Log|s|ID,MyAbID", "Ab|s|ID"], infos).proposals == ()

    def test_ambiguous_schemas_are_dropped_not_guessed(self):
        order = tbl("other", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"])
        infos = [order,
                 tbl("sales", "Customer", [("ID", INT)], pk=["ID"]),
                 tbl("ref", "Customer", [("ID", INT)], pk=["ID"])]
        result = self.run(["Order|other|ID,CustomerID", "sales.Customer||ID", "ref.Customer||ID"], infos)
        assert result.proposals == ()
        assert result.skipped == (("Order.CustomerID", "several tables match: ref.Customer, sales.Customer"),)

    def test_a_target_with_a_composite_key_is_dropped(self):
        result = self.order_and("CustomerID", other_pk=("A", "B"), other_cols=(("A", INT), ("B", INT)))
        assert result.proposals == ()
        assert result.skipped[0][1] == "Customer has a composite primary key"

    def test_a_target_with_no_key_and_no_id_column_is_dropped(self):
        result = self.order_and("CustomerID", other_pk=(), other_cols=(("Name", "nvarchar(20)"),))
        assert result.proposals == ()
        assert result.skipped[0][1] == "Customer has no primary key and no ID column"

    def test_columns_of_different_kinds_are_dropped(self):
        result = self.order_and("CustomerID", column_type="nvarchar(20)")
        assert result.proposals == ()
        assert result.skipped[0][1] == "types differ (nvarchar(20) vs int)"

    def test_int_and_bigint_are_the_same_kind(self):
        result = self.order_and("CustomerID", other_cols=(("ID", "bigint"),))
        assert len(result.proposals) == 1

    def test_a_target_key_schema_yaml_does_not_list_is_dropped(self):
        result = self.order_and("CustomerID", other_cols=(("ID", INT), ("Name", "nvarchar(5)")),
                                other_listed=["Name"])
        assert result.proposals == ()
        assert result.skipped[0][1] == "Customer.ID is not listed in schema.yaml"

    def test_a_described_only_table_is_never_a_target(self):
        order = tbl("s", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"])
        customer = tbl("s", "Customer", [("ID", INT)], pk=["ID"])
        assert self.run(["Order|s|ID,CustomerID", "Customer|s|-"], [order, customer]).proposals == ()

    def test_a_column_schema_yaml_does_not_list_is_not_considered(self):
        order = tbl("s", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"])
        customer = tbl("s", "Customer", [("ID", INT)], pk=["ID"])
        assert self.run(["Order|s|ID", "Customer|s|ID"], [order, customer]).proposals == ()

    def test_a_table_missing_from_the_database_is_ignored(self):
        schema = schema_for("Order|s|ID,CustomerID", "Customer|s|ID")
        cat = {"a": catalogue(tbl("s", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"]))}
        assert propose_relationships(schema, ["a"], cat).proposals == ()


class TestAlreadyKnownRelationships:
    def setup_method(self):
        self.schema_text = ["Order|s|ID,CustomerID", "Customer|s|ID"]
        self.cat = {"a": catalogue(
            tbl("s", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"]),
            tbl("s", "Customer", [("ID", INT)], pk=["ID"]),
        )}

    def test_a_relationship_already_in_relationships_yaml_is_not_proposed_again(self):
        existing = [{"from_table": "order", "from_column": "customerid",
                     "to_table": "Customer", "to_column": "ID", "join_hint": "x"}]
        result = propose_relationships(schema_for(*self.schema_text), ["a"], self.cat, existing)
        assert result.proposals == () and result.already_known == 1

    def test_either_direction_counts(self):
        existing = [{"from_table": "Customer", "from_column": "ID",
                     "to_table": "Order", "to_column": "CustomerID"}]
        result = propose_relationships(schema_for(*self.schema_text), ["a"], self.cat, existing)
        assert result.already_known == 1

    def test_a_join_already_written_in_schema_yaml_counts(self):
        extra = (
            "relationships:\n  - from_table: Order\n    to_table: Customer\n"
            "    join_sql: \"JOIN s.Customer c ON o.CustomerID = c.ID\"\n"
        )
        result = propose_relationships(schema_for(*self.schema_text, extra=extra), ["a"], self.cat)
        assert result.proposals == () and result.already_known == 1

    def test_a_different_relationship_between_the_same_tables_is_still_proposed(self):
        existing = [{"from_table": "Order", "from_column": "BillingID",
                     "to_table": "Customer", "to_column": "ID"}]
        result = propose_relationships(schema_for(*self.schema_text), ["a"], self.cat, existing)
        assert len(result.proposals) == 1 and result.already_known == 0

    def test_incomplete_existing_entries_are_ignored(self):
        result = propose_relationships(schema_for(*self.schema_text), ["a"], self.cat, [{"from_table": "Order"}])
        assert len(result.proposals) == 1


class TestRendering:
    def result(self):
        schema = schema_for("Order|sales|ID,CustomerID", "Customer|sales|ID")
        cat = {"a": catalogue(
            tbl("sales", "Order", [("ID", INT), ("CustomerID", INT)], pk=["ID"]),
            tbl("sales", "Customer", [("ID", INT)], pk=["ID"]),
        )}
        return propose_relationships(schema, ["a"], cat)

    def test_the_file_says_it_is_a_proposal_and_nothing_reads_it(self):
        text = render_proposals_yaml(self.result().proposals)
        assert text.startswith("# relationships.proposed.yaml -- PROPOSALS ONLY")
        assert "Nothing reads this file" in text

    def test_it_is_valid_yaml_in_the_shape_of_relationships_yaml(self):
        data = yaml.safe_load(render_proposals_yaml(self.result().proposals))
        (entry,) = data["relationships"]
        assert entry == {
            "from_table": "Order", "from_schema": "sales", "from_column": "CustomerID",
            "to_table": "Customer", "to_schema": "sales", "to_column": "ID",
            "join_hint": "JOIN [sales].[Customer] ON [sales].[Order].[CustomerID] = [sales].[Customer].[ID]",
            "basis": "naming convention: CustomerID -> Customer.ID",
            "confidence": "high",
        }

    def test_the_runtime_relationship_loader_can_read_it(self, tmp_path):
        from database.relationship_map import _load_from_yaml

        path = tmp_path / "relationships.proposed.yaml"
        path.write_text(render_proposals_yaml(self.result().proposals), encoding="utf-8")
        assert _load_from_yaml(path) == {
            ("Order", "Customer"): [
                "JOIN [sales].[Customer] ON [sales].[Order].[CustomerID] = [sales].[Customer].[ID]"],
            ("Customer", "Order"): [
                "JOIN [sales].[Customer] ON [sales].[Order].[CustomerID] = [sales].[Customer].[ID]"],
        }

    def test_the_same_proposals_give_the_same_bytes_and_no_timestamp(self):
        assert render_proposals_yaml(self.result().proposals) == render_proposals_yaml(self.result().proposals)

    def test_no_proposals_is_an_empty_list(self):
        assert yaml.safe_load(render_proposals_yaml([])) == {"relationships": []}

    def test_a_name_with_a_quote_in_it_is_escaped(self):
        schema = validate_schema_yaml_text(
            'tables:\n  "[O\'Rder]":\n    db_schema: s\n    columns: {ID: x, CustomerID: x}\n'
            "  Customer:\n    db_schema: s\n    columns: {ID: x}\n"
        )
        cat = {"a": catalogue(
            tbl("s", "O'Rder", [("ID", INT), ("CustomerID", INT)], pk=["ID"]),
            tbl("s", "Customer", [("ID", INT)], pk=["ID"]),
        )}
        text = render_proposals_yaml(propose_relationships(schema, ["a"], cat).proposals)
        assert yaml.safe_load(text)["relationships"][0]["from_table"] == "O'Rder"
