# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for T-SQL identifier quoting in the two retrieval query builders.

``retrieval.dimension_vocabulary._prefetch_query`` and
``retrieval.value_resolver._build_query`` used to hand-wrap every
schema/table/column name in ``f"[{name}]"``. T-SQL requires a literal
``]`` *inside* a bracketed identifier to be doubled (``]]``) -- hand
wrapping never does that, so a name containing ``]`` produced a bracket
expression that terminated early: broken SQL for a non-tsql target
(``sqlglot.errors.ParseError`` when ``transpile_sql`` tried to parse it)
and malformed-but-unparsed SQL sent straight to the database for the
``tsql`` target itself.

The fix is a single shared helper,
``security.dialects.quote_tsql_identifier``, used for every identifier in
both builders. Severity in production is low -- ``table``/``column``/
``schema`` always come from deployment configuration
(``PREFETCH_COLUMNS``/``RESOLVABLE_COLUMNS``/``_TABLE_SCHEMAS``, all
sourced from ``schema.yaml``), never from a question -- so every
"hostile" identifier below is an obviously-synthetic name standing in for
a real deployment's config, never anything resembling a real customer's
schema.
"""

from __future__ import annotations

import sqlglot
import pytest

import retrieval.dimension_vocabulary as dimension_vocabulary
import retrieval.value_resolver as value_resolver
from security.dialects import quote_tsql_identifier

#: Every non-tsql dialect this project supports transpiling *to* -- see
#: security.dialects.DIALECT_PROFILES.
NON_TSQL_DIALECTS = ("postgres", "sqlite", "mysql")

#: Synthetic identifiers standing in for a hostile/awkward deployment
#: config value -- never a real table/column/schema name.
HOSTILE_NAMES = ["Na]me", "Tab]le", "Col[umn", "Weird Name", 'Quo"te']


# ---------------------------------------------------------------------------
# 1. The helper itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_quote_tsql_identifier_round_trips(name: str) -> None:
    """Every hostile name round-trips through sqlglot back to itself.

    This is the property the whole fix rests on: rendering must produce
    tsql text that, when parsed back as tsql, yields the exact original
    identifier -- not a truncated or mis-escaped one.
    """
    rendered = quote_tsql_identifier(name)
    parsed = sqlglot.parse_one(rendered, read="tsql")
    assert parsed.name == name


def test_quote_tsql_identifier_ordinary_name_unchanged() -> None:
    """An ordinary name renders exactly as the old hand-written
    ``f"[{name}]"`` did -- guards the byte-identical requirement the two
    builders' doctests rely on."""
    assert quote_tsql_identifier("Name") == "[Name]"
    assert quote_tsql_identifier("Customer") == "[Customer]"


# ---------------------------------------------------------------------------
# 2. Both builders, with a hostile identifier in every position
# ---------------------------------------------------------------------------


def test_prefetch_query_hostile_column_and_table(monkeypatch: pytest.MonkeyPatch) -> None:
    table, column, schema = "Tab]le", "Col]umn", "Sch]ema"
    monkeypatch.setitem(dimension_vocabulary._TABLE_SCHEMAS, table, schema)

    tsql = dimension_vocabulary._prefetch_query(table, column, dialect="tsql")
    parsed = sqlglot.parse_one(tsql, read="tsql")
    select_col = parsed.expressions[0]
    assert select_col.name == column
    table_exp = parsed.find(sqlglot.exp.Table)
    assert table_exp.name == table
    assert table_exp.db == schema

    for target in NON_TSQL_DIALECTS:
        transpiled = dimension_vocabulary._prefetch_query(table, column, dialect=target)
        # Must not raise ParseError, and must parse back to the same names.
        parsed_target = sqlglot.parse_one(transpiled, read=target)
        assert parsed_target.expressions[0].name == column
        assert parsed_target.find(sqlglot.exp.Table).name == table


def test_prefetch_query_hostile_schemaless_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schema-less dialect (sqlite) only ever quotes the table/column --
    still must survive a hostile name with no schema lookup involved."""
    table, column = "Tab]le", "Col]umn"

    tsql = dimension_vocabulary._prefetch_query(table, column, dialect="sqlite")
    parsed = sqlglot.parse_one(tsql, read="sqlite")
    assert parsed.expressions[0].name == column
    assert parsed.find(sqlglot.exp.Table).name == table


def test_build_query_hostile_column_table_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    table, column, schema = "Tab]le", "Col]umn", "Sch]ema"
    monkeypatch.setitem(value_resolver._TABLE_SCHEMAS, table, schema)

    tsql = value_resolver._build_query(table, column, dialect="tsql")
    assert "LIKE ? ESCAPE" in tsql
    parsed = sqlglot.parse_one(tsql, read="tsql")
    select_col = parsed.expressions[0]
    assert select_col.name == column
    table_exp = parsed.find(sqlglot.exp.Table)
    assert table_exp.name == table
    assert table_exp.db == schema

    for target in NON_TSQL_DIALECTS:
        transpiled = value_resolver._build_query(table, column, dialect=target)
        parsed_target = sqlglot.parse_one(transpiled, read=target)
        assert parsed_target.expressions[0].name == column
        assert parsed_target.find(sqlglot.exp.Table).name == table


def test_build_query_hostile_schemaless_table() -> None:
    table, column = "Tab]le", "Col]umn"

    tsql = value_resolver._build_query(table, column, dialect="sqlite")
    parsed = sqlglot.parse_one(tsql, read="sqlite")
    assert parsed.expressions[0].name == column
    assert parsed.find(sqlglot.exp.Table).name == table


# ---------------------------------------------------------------------------
# 3. Ordinary identifiers still render byte-identically
# ---------------------------------------------------------------------------


def test_prefetch_query_ordinary_name_unchanged() -> None:
    sql = dimension_vocabulary._prefetch_query("Customer", "Name")
    assert sql.startswith("SELECT DISTINCT TOP (?) [Name] FROM [")
    assert sql.endswith("].[Customer]")


def test_build_query_ordinary_name_unchanged() -> None:
    sql = value_resolver._build_query("Customer", "Name")
    assert sql.startswith("SELECT DISTINCT TOP (?) [Name] FROM [")
    assert sql.endswith("].[Customer] WHERE [Name] LIKE ? ESCAPE '\\'")
