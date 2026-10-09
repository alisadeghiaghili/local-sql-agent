# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``schema_data.yaml_text``: the line-level primitives the
``schema.yaml`` tools use to edit text without losing a comment."""

from __future__ import annotations

import pytest

from schema_data.yaml_text import (
    LayoutError,
    apply_edits,
    child_span,
    datasource_span,
    parse_entries,
    parse_tables,
    quote,
    yaml_key,
)

TEXT = '''\
# header

tables:

  # ---- first ----
  Alpha:   # note
    description: "a"
    columns:
      ID: "Primary key"
      "Total Amount": "multi
        line"        # tail
      # a comment between columns
      Code: x

    resolvable_columns: [Code]

  'Beta':
    columns:
      ID: y
  # trailing comment of Beta

relationships: []
'''


def _lines() -> list[str]:
    return TEXT.split("\n")


class TestParseTables:
    def test_each_table_gets_its_line_region_and_child_indent(self):
        layout = parse_tables(_lines(), ["Alpha", "Beta"])
        alpha, beta = layout.tables["Alpha"], layout.tables["Beta"]
        assert (alpha.line, beta.line) == (5, 16)
        assert alpha.end == beta.line
        assert alpha.child_indent == 4
        assert _lines()[alpha.content_end - 1].strip() == "resolvable_columns: [Code]"
        assert layout.key_indent == 2
        assert _lines()[layout.stop].startswith("relationships:")
        assert _lines()[layout.content_end - 1].strip() == "ID: y"

    def test_a_missing_block_style_tables_mapping_is_refused_only_when_tables_exist(self):
        assert parse_tables(["relationships: []"], []) is None
        with pytest.raises(LayoutError, match="tables:"):
            parse_tables(["tables: {A: {columns: {X: y}}}"], ["A"])

    def test_a_flow_style_table_is_refused_by_name(self):
        with pytest.raises(LayoutError, match="Flow"):
            parse_tables(["tables:", "  Flow: {columns: {X: y}}"], ["Flow"])


class TestChildSpanAndEntries:
    def test_columns_block_ends_at_its_last_content_line(self):
        lines = _lines()
        block = parse_tables(lines, ["Alpha", "Beta"]).tables["Alpha"]
        first, stop = child_span(lines, block, "columns")
        assert lines[first].strip() == "columns:"
        assert lines[stop - 1].strip() == "Code: x"

    def test_entries_include_multiline_values_and_skip_comments(self):
        lines = _lines()
        block = parse_tables(lines, ["Alpha", "Beta"]).tables["Alpha"]
        first, stop = child_span(lines, block, "columns")
        entries = parse_entries(lines, first, stop)
        assert [e.name for e in entries] == ["ID", "Total Amount", "Code"]
        assert (entries[1].stop - entries[1].line) == 2
        assert entries[0].indent == 6

    def test_a_missing_child_is_none_and_an_inline_one_is_refused(self):
        lines = _lines()
        block = parse_tables(lines, ["Alpha", "Beta"]).tables["Alpha"]
        assert child_span(lines, block, "column_types") is None
        inline = ["tables:", "  T:", "    columns: {A: x}"]
        with pytest.raises(LayoutError, match="inline"):
            child_span(inline, parse_tables(inline, ["T"]).tables["T"], "columns")

    def test_datasource_span_covers_a_block_list(self):
        lines = ["  datasource:", "    - a", "    - b", "  description: d"]
        assert datasource_span(lines, 0, 4, 2) == (0, 3)


class TestApplyEdits:
    def test_replace_insert_and_delete_in_one_pass(self):
        assert apply_edits(["a", "b", "c", "d"], [(0, 1, ["A"]), (2, 3, []), (4, 4, ["e"])]) == [
            "A", "b", "d", "e",
        ]

    def test_insertions_at_one_index_keep_their_order(self):
        assert apply_edits(["a"], [(1, 1, ["x"]), (1, 1, ["y"])]) == ["a", "x", "y"]

    def test_overlapping_edits_are_a_bug_not_a_merge(self):
        with pytest.raises(LayoutError, match="overlap"):
            apply_edits(["a", "b", "c"], [(0, 2, []), (1, 2, ["x"])])


class TestYamlScalars:
    @pytest.mark.parametrize("name, expected", [
        ("CustomerID", "CustomerID"),
        ("_x1", "_x1"),
        ("Total Amount", '"Total Amount"'),
        ("true", '"true"'),
        ("No", '"No"'),
        ("123", '"123"'),
        ("نام", '"نام"'),
        ('say "hi"', '"say \\"hi\\""'),
    ])
    def test_a_key_is_quoted_unless_it_is_a_plain_identifier(self, name, expected):
        assert yaml_key(name) == expected

    def test_quote_keeps_non_ascii_text(self):
        assert quote("نام") == '"نام"'
