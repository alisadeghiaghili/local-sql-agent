# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``security.sql_format.format_sql``: the house SQL layout, display only.

``Turn.sql_display`` is what the UI shows and the copy button copies, and it
must read the way the maintainers write T-SQL by hand. These tests pin the
layout rule by rule, and -- because a formatter that quietly changes a query
is worse than none -- pin the safety net: every result is parsed back and
compared with the parse of the input, and anything that does not match, does
not parse, or is a construct the layout does not handle, falls back to
sqlglot's pretty printer and then to the input itself.

The alignment rule, exactly
---------------------------
* Select items: the ``AS`` column sits **two spaces after the longest aliased
  expression** (an expression longer than 60 characters, or spanning several
  lines, is left out of that measurement and takes a single space before its
  ``AS``). Items with no alias are not padded.
* Join lines: the ``AS`` column is two spaces after the longest
  ``<JOIN keyword> <table>``, the ``ON`` column two spaces after the longest
  ``AS <alias>``, ``ON`` is followed by two spaces, and the ``=`` of a plain
  ``left = right`` first condition is two spaces after the longest left side.
* ``FROM table AS alias`` is not part of that alignment: single spaces.

The maintainer's hand-aligned sample puts seven spaces between the longest
select expression and ``AS``; no fixed rule produces that, so the select list
uses the same two-space rule as the join lines (which the sample does
follow, to the space).

All table names here are generic on purpose; ``tests/test_no_domain_literals.py``
keeps the real deployment's identifiers out of the tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sqlglot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from security import sql_format
from security.sql_format import format_sql
from security.sql_guard import pretty_sql


def _block(text: str) -> str:
    """Return *text* without the newline after the opening quotes and the last one."""
    assert text.startswith("\n") and text.endswith("\n")
    return text[1:-1]


def _house(sql: str) -> str:
    """The house layout itself, failing the test if it fell back."""
    out = sql_format._house_layout(sql, "tsql")
    assert out is not None, f"house layout declined: {sql!r}"
    return out


def _sqlglot_pretty(sql: str, dialect: str = "tsql") -> str:
    return sqlglot.transpile(sql, read=dialect, write=dialect, pretty=True)[0]


# ---------------------------------------------------------------------------
# The maintainer's sample
# ---------------------------------------------------------------------------

SAMPLE_INPUT = (
    "select T.ID, BC.NationalID as BuyerNationalID, BC.[Name] as BuyerCustomerName, "
    "C.Code as ContractCode, T.[Count], T.Price "
    "from sales_fact.Trade T "
    "join sales_dim.Customer BC on T.BuyerCustomer_ID = BC.ID "
    "inner join sales_dim.Contract C on T.Contract_ID = C.ID "
    "inner join shared_dim.Date D on T.Date_ID = D.ID"
)

SAMPLE_OUTPUT = _block("""
SELECT
     T.ID
    ,BC.NationalID  AS BuyerNationalID
    ,BC.[Name]      AS BuyerCustomerName
    ,C.Code         AS ContractCode
    ,T.[Count]
    ,T.Price

FROM sales_fact.Trade AS T
INNER JOIN sales_dim.Customer  AS BC  ON  T.BuyerCustomer_ID  = BC.ID
INNER JOIN sales_dim.Contract  AS C   ON  T.Contract_ID       = C.ID
INNER JOIN shared_dim.Date     AS D   ON  T.Date_ID           = D.ID
""")


class TestMaintainersSample:
    def test_the_sample_comes_out_exactly(self):
        assert format_sql(SAMPLE_INPUT) == SAMPLE_OUTPUT

    def test_the_join_lines_are_the_samples_to_the_space(self):
        """The only padding that differs from the hand-aligned original is
        the select list's; the FROM and join lines match it exactly."""
        assert SAMPLE_OUTPUT.split("\n")[8:] == [
            "FROM sales_fact.Trade AS T",
            "INNER JOIN sales_dim.Customer  AS BC  ON  T.BuyerCustomer_ID  = BC.ID",
            "INNER JOIN sales_dim.Contract  AS C   ON  T.Contract_ID       = C.ID",
            "INNER JOIN shared_dim.Date     AS D   ON  T.Date_ID           = D.ID",
        ]

    def test_the_sample_is_not_reformatted_again(self):
        assert format_sql(SAMPLE_OUTPUT) == SAMPLE_OUTPUT

    def test_pretty_sql_gives_the_same_layout_for_tsql(self):
        assert pretty_sql(SAMPLE_INPUT, "tsql") == SAMPLE_OUTPUT

    def test_pretty_sql_default_dialect_is_the_house_layout(self):
        assert pretty_sql("SELECT a FROM t") == "SELECT\n     a\n\nFROM t"


# ---------------------------------------------------------------------------
# Rule 1: SELECT and the select list
# ---------------------------------------------------------------------------

class TestSelectList:
    def test_select_is_alone_and_items_have_leading_commas(self):
        assert format_sql("SELECT a, b, c FROM t") == _block("""
SELECT
     a
    ,b
    ,c

FROM t
""")

    def test_the_first_item_is_five_in_and_the_rest_four_then_a_comma(self):
        lines = format_sql("SELECT a, b, c FROM t").split("\n")
        assert lines[1].startswith(" " * 5) and not lines[1].startswith(" " * 6)
        assert lines[2].startswith("    ,") and lines[3].startswith("    ,")
        # the expressions line up
        assert lines[1].index("a") == lines[2].index("b") == lines[3].index("c") == 5

    def test_one_item_is_still_on_its_own_line(self):
        assert format_sql("SELECT COUNT(*) FROM t") == "SELECT\n     COUNT(*)\n\nFROM t"

    def test_top_and_distinct_stay_on_the_select_line(self):
        assert format_sql("SELECT TOP 100 a FROM t").split("\n")[0] == "SELECT TOP (100)"
        assert format_sql("SELECT TOP (100) a FROM t").split("\n")[0] == "SELECT TOP (100)"
        assert format_sql("SELECT DISTINCT a FROM t").split("\n")[0] == "SELECT DISTINCT"
        assert (
            format_sql("SELECT DISTINCT TOP 5 a FROM t").split("\n")[0]
            == "SELECT DISTINCT TOP (5)"
        )

    def test_top_percent_and_a_placeholder_top(self):
        assert format_sql("SELECT TOP 10 PERCENT a FROM t").split("\n")[0] == "SELECT TOP (10) PERCENT"
        assert format_sql("SELECT TOP (?) a FROM t").split("\n")[0] == "SELECT TOP (?)"

    def test_aliases_share_one_column_two_spaces_after_the_longest(self):
        out = format_sql(
            "SELECT t.ID AS Id, t.TradePriceValue AS Value, SUM(t.Qty) AS Total FROM sales_fact.Trade t"
        )
        assert out.split("\n")[1:4] == [
            "     t.ID               AS Id",
            "    ,t.TradePriceValue  AS Value",
            "    ,SUM(t.Qty)         AS Total",
        ]

    def test_an_item_without_an_alias_is_not_padded(self):
        out = format_sql("SELECT t.LongColumnName AS L, t.B, t.C AS C FROM t")
        lines = out.split("\n")
        assert lines[2] == "    ,t.B"
        assert lines[1] == "     t.LongColumnName  AS L"
        assert lines[3] == "    ,t.C               AS C"

    def test_a_long_expression_does_not_stretch_the_alias_column(self):
        long_expr = " + ".join(f"t.Column{i}" for i in range(9))  # well over 60 characters
        assert len(long_expr) > 60
        out = format_sql(f"SELECT t.A AS X, ({long_expr}) AS Wide, t.Bb AS Y FROM t")
        lines = out.split("\n")
        assert lines[1] == "     t.A   AS X"
        assert lines[2] == f"    ,({long_expr}) AS Wide"
        assert lines[3] == "    ,t.Bb  AS Y"

    def test_a_case_item_is_a_block_and_does_not_stretch_the_alias_column(self):
        out = format_sql(
            "SELECT t.A AS X, CASE WHEN t.B = 1 THEN 'one' WHEN t.B = 2 THEN 'two' ELSE 'many' END AS Label, "
            "t.Cc AS Y FROM t"
        )
        assert out == _block("""
SELECT
     t.A   AS X
    ,CASE
         WHEN t.B = 1 THEN 'one'
         WHEN t.B = 2 THEN 'two'
         ELSE 'many'
     END AS Label
    ,t.Cc  AS Y

FROM t
""")

    def test_a_case_item_without_an_alias(self):
        assert format_sql("SELECT CASE t.Side WHEN 1 THEN 'B' ELSE 'S' END FROM t") == _block("""
SELECT
     CASE t.Side
         WHEN 1 THEN 'B'
         ELSE 'S'
     END

FROM t
""")

    def test_a_short_nested_case_stays_inline(self):
        out = format_sql("SELECT SUM(CASE WHEN a = 1 THEN 1 ELSE 0 END) AS n FROM t")
        assert out.split("\n")[1] == "     SUM(CASE WHEN a = 1 THEN 1 ELSE 0 END)  AS n"

    def test_a_long_nested_case_becomes_a_block_under_its_own_column(self):
        out = format_sql(
            "SELECT SUM(CASE WHEN t.Side = 1 THEN t.QuantityTraded * t.PriceAgreed "
            "WHEN t.Side = 2 THEN 0 - t.QuantityTraded * t.PriceAgreed ELSE 0 END) AS Net FROM t"
        )
        assert out == _block("""
SELECT
     SUM(CASE
             WHEN t.Side = 1 THEN t.QuantityTraded * t.PriceAgreed
             WHEN t.Side = 2 THEN 0 - t.QuantityTraded * t.PriceAgreed
             ELSE 0
         END) AS Net

FROM t
""")

    def test_select_star_and_qualified_star(self):
        assert format_sql("SELECT *, t.* FROM t") == "SELECT\n     *\n    ,t.*\n\nFROM t"


# ---------------------------------------------------------------------------
# Rule 2: blank line before FROM
# ---------------------------------------------------------------------------

class TestBlankLine:
    def test_one_blank_line_separates_the_list_from_from(self):
        lines = format_sql("SELECT a FROM t WHERE a = 1").split("\n")
        assert lines == ["SELECT", "     a", "", "FROM t", "WHERE a = 1"]

    def test_no_from_no_blank_line(self):
        assert format_sql("SELECT 1 AS one") == "SELECT\n     1  AS one"

    def test_the_blank_line_is_really_empty(self):
        assert "" in format_sql("SELECT a FROM t").split("\n")


# ---------------------------------------------------------------------------
# Rule 3: FROM and joins
# ---------------------------------------------------------------------------

class TestFromAndJoins:
    def test_from_is_one_line_with_as(self):
        assert format_sql("SELECT a FROM sales_fact.Trade T").split("\n")[3] == "FROM sales_fact.Trade AS T"
        assert format_sql("SELECT a FROM sales_fact.Trade").split("\n")[3] == "FROM sales_fact.Trade"

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("JOIN", "INNER JOIN"),
            ("INNER JOIN", "INNER JOIN"),
            ("inner join", "INNER JOIN"),
            ("LEFT JOIN", "LEFT JOIN"),
            ("LEFT OUTER JOIN", "LEFT JOIN"),
            ("RIGHT JOIN", "RIGHT JOIN"),
            ("RIGHT OUTER JOIN", "RIGHT JOIN"),
            ("FULL JOIN", "FULL JOIN"),
            ("FULL OUTER JOIN", "FULL JOIN"),
        ],
    )
    def test_join_keywords_are_written_in_full_upper_case(self, written, expected):
        out = format_sql(f"SELECT a FROM t {written} u ON t.ID = u.ID")
        assert out.split("\n")[4] == f"{expected} u  ON  t.ID  = u.ID"

    def test_cross_join_and_apply_have_no_on(self):
        out = format_sql(
            "SELECT a FROM t CROSS JOIN sales_dim.Calendar c "
            "CROSS APPLY (SELECT TOP 1 f.Qty FROM sales_fact.Fill f WHERE f.Trade_ID = t.ID) AS q "
            "OUTER APPLY (SELECT 1 AS z) AS r"
        )
        assert out == _block("""
SELECT
     a

FROM t
CROSS JOIN sales_dim.Calendar  AS c
CROSS APPLY (
    SELECT TOP (1)
         f.Qty

    FROM sales_fact.Fill AS f
    WHERE f.Trade_ID = t.ID
) AS q
OUTER APPLY (
    SELECT
         1  AS z
) AS r
""")

    def test_as_and_on_columns_line_up_across_join_lines(self):
        out = format_sql(
            "SELECT a FROM sales_fact.Trade T "
            "LEFT JOIN sales_dim.Customer BC ON T.BuyerCustomer_ID = BC.ID "
            "INNER JOIN shared_dim.Date D ON T.Date_ID = D.ID "
            "FULL OUTER JOIN sales_dim.Contract CC ON T.Contract_ID = CC.ID"
        )
        joins = out.split("\n")[4:]
        assert len(joins) == 3
        assert len({line.index(" AS ") for line in joins}) == 1
        assert len({line.index(" ON ") for line in joins}) == 1
        assert len({line.index("= ") for line in joins}) == 1

    def test_two_spaces_between_columns_and_after_on(self):
        out = format_sql("SELECT a FROM t JOIN sales_dim.Customer BC ON t.Customer_ID = BC.ID")
        line = out.split("\n")[4]
        assert line == "INNER JOIN sales_dim.Customer  AS BC  ON  t.Customer_ID  = BC.ID"

    def test_the_equals_signs_line_up_even_when_a_line_has_no_alias(self):
        out = format_sql(
            "SELECT a FROM t INNER JOIN u AS x ON t.A = x.A INNER JOIN sales_dim.V ON t.LongColumn = V.B"
        )
        joins = out.split("\n")[4:]
        assert joins[0].index("= ") == joins[1].index("= ")

    def test_a_multi_condition_on_continues_under_the_first_condition(self):
        out = format_sql(
            "SELECT a FROM t LEFT JOIN sales_dim.Customer BC "
            "ON t.BuyerCustomer_ID = BC.ID AND BC.IsActive = 1 AND BC.Region_ID IN (1, 2)"
        )
        assert out.split("\n")[4:] == [
            "LEFT JOIN sales_dim.Customer  AS BC  ON  t.BuyerCustomer_ID  = BC.ID",
            "                                     AND BC.IsActive = 1",
            "                                     AND BC.Region_ID IN (1, 2)",
        ]

    def test_an_or_in_on_is_right_aligned_so_conditions_line_up(self):
        out = format_sql("SELECT a FROM t LEFT JOIN u ON t.A = u.A OR t.B = u.B")
        first, second = out.split("\n")[4:]
        assert first == "LEFT JOIN u  ON  t.A  = u.A"
        assert second == "              OR t.B = u.B"
        assert first.index("t.A") == second.index("t.B")

    def test_a_derived_table_join_is_a_block(self):
        out = format_sql(
            "SELECT a FROM t LEFT JOIN (SELECT d.C_ID, SUM(d.V) AS S FROM sales_fact.D d GROUP BY d.C_ID) AS dd "
            "ON dd.C_ID = t.ID"
        )
        assert out == _block("""
SELECT
     a

FROM t
LEFT JOIN (
    SELECT
         d.C_ID
        ,SUM(d.V)  AS S

    FROM sales_fact.D AS d
    GROUP BY
         d.C_ID
) AS dd  ON  dd.C_ID = t.ID
""")

    def test_a_table_hint_follows_the_alias(self):
        out = format_sql("SELECT a FROM sales_fact.Trade AS T WITH (NOLOCK)")
        assert out.split("\n")[3] == "FROM sales_fact.Trade AS T WITH (NOLOCK)"


# ---------------------------------------------------------------------------
# Rule 4: WHERE
# ---------------------------------------------------------------------------

class TestWhere:
    def test_first_condition_on_the_where_line_and_and_or_right_aligned(self):
        out = format_sql("SELECT a FROM t WHERE a = 1 AND b = 2 AND c = 3")
        assert out.split("\n")[4:] == ["WHERE a = 1", "  AND b = 2", "  AND c = 3"]

    def test_or_is_right_aligned_under_where(self):
        out = format_sql("SELECT a FROM t WHERE a = 1 OR b = 2")
        assert out.split("\n")[4:] == ["WHERE a = 1", "   OR b = 2"]

    def test_conditions_all_start_in_one_column(self):
        out = format_sql("SELECT a FROM t WHERE alpha = 1 AND beta = 2 AND gamma = 3")
        starts = {line.index(word) for line, word in zip(out.split("\n")[4:], ("alpha", "beta", "gamma"))}
        assert starts == {6}

    def test_only_the_top_level_operator_is_split(self):
        out = format_sql("SELECT a FROM t WHERE a = 1 OR b = 2 AND c = 3")
        assert out.split("\n")[4:] == ["WHERE a = 1", "   OR b = 2 AND c = 3"]

    def test_a_short_parenthesised_group_stays_on_one_line(self):
        out = format_sql("SELECT a FROM t WHERE (a = 1 OR a = 2) AND b = 3")
        assert out.split("\n")[4:] == ["WHERE (a = 1 OR a = 2)", "  AND b = 3"]

    def test_a_long_parenthesised_group_breaks_inside_with_extra_indentation(self):
        values = " OR ".join(f"t.Code = {i}" for i in range(1, 12))
        assert len(f"({values})") >= 80
        out = format_sql(f"SELECT a FROM t WHERE t.Flag = 1 AND ({values})")
        lines = out.split("\n")[4:]
        assert lines[:4] == [
            "WHERE t.Flag = 1",
            "  AND (",
            "          t.Code = 1",
            "       OR t.Code = 2",
        ]
        assert lines[-1] == "      )"
        # the group's conditions start in one column, four in from the parenthesis
        assert {line.index("t.Code") for line in lines[2:-1]} == {10}

    def test_having_follows_the_same_rule(self):
        out = format_sql("SELECT a FROM t GROUP BY a HAVING COUNT(*) > 1 AND SUM(b) < 3")
        assert out.split("\n")[-2:] == ["HAVING COUNT(*) > 1", "   AND SUM(b) < 3"]

    def test_negated_predicates_are_written_naturally(self):
        out = format_sql(
            "SELECT a FROM t WHERE a IS NOT NULL AND b NOT IN (1, 2) AND c NOT LIKE 'x%' AND d NOT BETWEEN 1 AND 2"
        )
        assert out.split("\n")[4:] == [
            "WHERE a IS NOT NULL",
            "  AND b NOT IN (1, 2)",
            "  AND c NOT LIKE 'x%'",
            "  AND d NOT BETWEEN 1 AND 2",
        ]


# ---------------------------------------------------------------------------
# Rule 5: GROUP BY, ORDER BY, OFFSET / FETCH
# ---------------------------------------------------------------------------

class TestGroupOrder:
    def test_group_by_and_order_by_use_the_select_list_layout(self):
        out = format_sql(
            "SELECT a, b, COUNT(*) AS n FROM t GROUP BY a, b ORDER BY n DESC, a ASC, b"
        )
        assert out == _block("""
SELECT
     a
    ,b
    ,COUNT(*)  AS n

FROM t
GROUP BY
     a
    ,b
ORDER BY
     n DESC
    ,a ASC
    ,b
""")

    def test_offset_and_fetch_are_one_line_each(self):
        out = format_sql("SELECT a FROM t ORDER BY a OFFSET 20 ROWS FETCH NEXT 10 ROWS ONLY")
        assert out.split("\n")[-4:] == ["ORDER BY", "     a", "OFFSET 20 ROWS", "FETCH NEXT 10 ROWS ONLY"]

    def test_offset_without_fetch(self):
        out = format_sql("SELECT a FROM t ORDER BY a OFFSET ? ROWS")
        assert out.split("\n")[-1] == "OFFSET ? ROWS"

    def test_order_by_a_case_is_a_block(self):
        out = format_sql("SELECT a FROM t ORDER BY CASE WHEN a = 1 THEN 0 ELSE 1 END, a")
        assert out.split("\n")[4:] == [
            "ORDER BY",
            "     CASE",
            "         WHEN a = 1 THEN 0",
            "         ELSE 1",
            "     END",
            "    ,a",
        ]


# ---------------------------------------------------------------------------
# Rule 6: subqueries, CTEs, set operations
# ---------------------------------------------------------------------------

class TestNesting:
    def test_in_subquery(self):
        assert format_sql("SELECT a FROM t WHERE a IN (SELECT b FROM u WHERE b > 1)") == _block("""
SELECT
     a

FROM t
WHERE a IN (
    SELECT
         b

    FROM u
    WHERE b > 1
)
""")

    def test_exists_has_a_space_and_the_body_is_indented_one_level(self):
        out = format_sql(
            "SELECT a FROM t WHERE t.X = 1 AND EXISTS (SELECT 1 FROM u WHERE u.ID = t.ID AND u.K = 2)"
        )
        assert out == _block("""
SELECT
     a

FROM t
WHERE t.X = 1
  AND EXISTS (
    SELECT
         1

    FROM u
    WHERE u.ID = t.ID
      AND u.K = 2
)
""")

    def test_not_in_and_not_exists_subqueries(self):
        out = format_sql(
            "SELECT a FROM t WHERE a NOT IN (SELECT b FROM u) AND NOT EXISTS (SELECT 1 FROM v WHERE v.ID = t.ID)"
        )
        assert "WHERE a NOT IN (" in out
        assert "  AND NOT EXISTS (" in out

    def test_a_short_scalar_subquery_stays_inline(self):
        out = format_sql("SELECT (SELECT MAX(z) FROM v) AS m FROM t")
        assert out.split("\n")[1] == "     (SELECT MAX(z) FROM v)  AS m"

    def test_a_long_scalar_subquery_is_a_block_hanging_off_its_item(self):
        out = format_sql(
            "SELECT t.ID, (SELECT SUM(v.Amount) FROM sales_fact.Fill v WHERE v.Trade_ID = t.ID AND v.Qty > 0) AS Filled FROM t"
        )
        assert out == _block("""
SELECT
     t.ID
    ,(
         SELECT
              SUM(v.Amount)

         FROM sales_fact.Fill AS v
         WHERE v.Trade_ID = t.ID
           AND v.Qty > 0
     ) AS Filled

FROM t
""")

    def test_a_derived_table_in_from(self):
        out = format_sql("SELECT q.a FROM (SELECT a, ROW_NUMBER() OVER (ORDER BY a) AS rn FROM t) q WHERE q.rn < 5")
        assert out == _block("""
SELECT
     q.a

FROM (
    SELECT
         a
        ,ROW_NUMBER() OVER (ORDER BY a)  AS rn

    FROM t
) AS q
WHERE q.rn < 5
""")

    def test_subqueries_nest_recursively(self):
        out = format_sql("SELECT a FROM t WHERE b IN (SELECT x FROM u WHERE y IN (SELECT z FROM v WHERE w = 1))")
        assert out.split("\n")[4:] == [
            "WHERE b IN (",
            "    SELECT",
            "         x",
            "",
            "    FROM u",
            "    WHERE y IN (",
            "        SELECT",
            "             z",
            "",
            "        FROM v",
            "        WHERE w = 1",
            "    )",
            ")",
        ]

    def test_a_subquery_inside_a_broken_group_hangs_off_the_group(self):
        values = " OR ".join(f"t.Code = {i}" for i in range(1, 9))
        out = format_sql(
            f"SELECT a FROM t WHERE ({values} OR t.K IN (SELECT k FROM u)) AND t.X = 1"
        )
        assert "          t.Code = 1" in out
        assert "       OR t.K IN (\n              SELECT\n                   k\n\n              FROM u\n          )\n      )" in out

    def test_a_cte(self):
        out = format_sql(
            "WITH buyers AS (SELECT t.Buyer_ID AS B, SUM(t.Price) AS Total FROM sales_fact.Trade t GROUP BY t.Buyer_ID) "
            "SELECT TOP 5 B FROM buyers ORDER BY Total DESC"
        )
        assert out == _block("""
WITH buyers AS (
    SELECT
         t.Buyer_ID    AS B
        ,SUM(t.Price)  AS Total

    FROM sales_fact.Trade AS t
    GROUP BY
         t.Buyer_ID
)
SELECT TOP (5)
     B

FROM buyers
ORDER BY
     Total DESC
""")

    def test_several_ctes_and_a_column_list(self):
        out = format_sql("WITH a(x) AS (SELECT 1 AS x), b AS (SELECT x FROM a) SELECT x FROM b")
        assert out == _block("""
WITH a(x) AS (
    SELECT
         1  AS x
),
b AS (
    SELECT
         x

    FROM a
)
SELECT
     x

FROM b
""")

    @pytest.mark.parametrize(
        ("written", "expected"),
        [("UNION ALL", "UNION ALL"), ("UNION", "UNION"), ("EXCEPT", "EXCEPT"), ("INTERSECT", "INTERSECT")],
    )
    def test_set_operators_stand_alone_between_the_branches(self, written, expected):
        out = format_sql(f"SELECT a FROM t {written} SELECT b FROM u")
        assert out == f"SELECT\n     a\n\nFROM t\n{expected}\nSELECT\n     b\n\nFROM u"

    def test_a_chain_of_set_operations_keeps_its_order(self):
        out = format_sql("SELECT 1 AS x UNION ALL SELECT 2 UNION SELECT 3 EXCEPT SELECT 4 ORDER BY 1")
        operators = [line for line in out.split("\n") if line in {"UNION ALL", "UNION", "EXCEPT"}]
        assert operators == ["UNION ALL", "UNION", "EXCEPT"]
        assert [line.strip() for line in out.split("\n") if line.strip() in {"1  AS x", "2", "3", "4"}] == [
            "1  AS x", "2", "3", "4",
        ]

    def test_parenthesised_branches(self):
        assert format_sql("(SELECT a FROM t) UNION ALL (SELECT b FROM u)") == _block("""
(
    SELECT
         a

    FROM t
)
UNION ALL
(
    SELECT
         b

    FROM u
)
""")


# ---------------------------------------------------------------------------
# Rule 7: what the input wrote is what the output shows
# ---------------------------------------------------------------------------

class TestVerbatim:
    def test_identifier_quoting_is_kept_either_way(self):
        out = format_sql("SELECT t.[Name], t.Name, [a b].[c d], Count FROM [sales dim].[Customer Summary] AS [a b]")
        assert "t.[Name]" in out and "\n    ,t.Name\n" in out
        assert "[a b].[c d]" in out
        assert "\n    ,Count\n" in out
        assert "FROM [sales dim].[Customer Summary] AS [a b]" in out

    def test_unquoted_identifiers_gain_no_brackets(self):
        out = format_sql("SELECT a, b FROM s.t WHERE a = 1")
        assert "[" not in out

    def test_persian_literals_and_aliases_are_exact(self):
        out = format_sql(
            "SELECT c.City AS [شهر], N'سلام دنیا' AS Greeting FROM sales_dim.Customer c "
            "WHERE c.City = N'تهران' AND c.[Name] LIKE N'%علی%' AND c.Note = 'it''s'"
        )
        for fragment in ("[شهر]", "N'سلام دنیا'", "N'تهران'", "N'%علی%'", "'it''s'"):
            assert fragment in out
        assert out.index("N'تهران'") < out.index("N'%علی%'")

    def test_numeric_literals_are_exact(self):
        out = format_sql("SELECT 1.50 AS a, 007 AS b, 1e3 AS c FROM t WHERE x > 0.10 AND y = 100")
        assert "1.50" in out and "007" in out and "1e3" in out
        assert "0.10" in out and "y = 100" in out

    def test_placeholders_are_kept(self):
        out = format_sql("SELECT TOP (?) a FROM t WHERE b = ? AND c BETWEEN ? AND ?")
        assert out.count("?") == 4

    def test_keywords_are_upper_case_whatever_the_input_used(self):
        out = format_sql("select distinct top 3 a from t left outer join u on t.i = u.i where a is not null order by a desc")
        for word in ("SELECT DISTINCT TOP (3)", "FROM", "LEFT JOIN", "ON", "WHERE", "IS NOT NULL", "ORDER BY", "DESC"):
            assert word in out
        assert "select" not in out and "where" not in out


# ---------------------------------------------------------------------------
# Corpus of realistic statements
# ---------------------------------------------------------------------------

CORPUS: list[str] = [
    "SELECT TOP 10 * FROM sales_fact.Trade ORDER BY Price DESC",
    "select top (5) t.ID, t.Price from sales_fact.Trade t",
    "SELECT DISTINCT c.[Name] FROM sales_dim.Customer c WHERE c.IsActive = 1",
    "SELECT DISTINCT TOP 20 c.City, c.[Province Name] FROM sales_dim.Customer AS c ORDER BY c.City",
    "SELECT d.PersianYear, SUM(t.Price * t.[Count]) AS TotalValue, COUNT(*) AS Trades "
    "FROM sales_fact.Trade t INNER JOIN shared_dim.Date d ON t.Date_ID = d.ID "
    "GROUP BY d.PersianYear HAVING SUM(t.Price * t.[Count]) > 1000000 ORDER BY TotalValue DESC",
    "SELECT d.PersianYear, d.PersianMonth, COUNT(DISTINCT t.BuyerCustomer_ID) AS Buyers "
    "FROM sales_fact.Trade t JOIN shared_dim.Date d ON d.ID = t.Date_ID "
    "GROUP BY d.PersianYear, d.PersianMonth ORDER BY d.PersianYear, d.PersianMonth",
    "SELECT t.ID, CASE WHEN t.Price < 100 THEN N'ارزان' WHEN t.Price < 1000 THEN N'متوسط' ELSE N'گران' END AS PriceBand "
    "FROM sales_fact.Trade t",
    "SELECT CASE t.Side WHEN 1 THEN 'Buy' WHEN 2 THEN 'Sell' END FROM sales_fact.Trade t",
    "SELECT SUM(CASE WHEN t.Side = 1 THEN t.[Count] ELSE 0 END) AS BuyCount, "
    "SUM(CASE WHEN t.Side = 2 THEN t.[Count] ELSE 0 END) AS SellCount FROM sales_fact.Trade t",
    "SELECT t.ID, c.[Name] FROM sales_fact.Trade t LEFT JOIN sales_dim.Customer c "
    "ON t.BuyerCustomer_ID = c.ID AND c.IsActive = 1 AND c.Region_ID IN (1, 2, 3)",
    "SELECT t.ID FROM sales_fact.Trade t LEFT OUTER JOIN sales_dim.Customer c "
    "ON t.BuyerCustomer_ID = c.ID OR t.SellerCustomer_ID = c.ID",
    "SELECT t.ID FROM sales_fact.Trade t RIGHT JOIN sales_dim.Customer c ON t.BuyerCustomer_ID = c.ID "
    "FULL OUTER JOIN sales_dim.Contract k ON t.Contract_ID = k.ID",
    "SELECT t.ID FROM sales_fact.Trade t INNER JOIN sales_dim.Customer c ON t.BuyerCustomer_ID = c.ID "
    "WHERE t.ID IN (SELECT x.Trade_ID FROM sales_fact.Fill x WHERE x.Qty > 0)",
    "SELECT c.ID FROM sales_dim.Customer c WHERE c.ID NOT IN "
    "(SELECT t.BuyerCustomer_ID FROM sales_fact.Trade t WHERE t.BuyerCustomer_ID IS NOT NULL)",
    "SELECT c.ID FROM sales_dim.Customer c WHERE EXISTS "
    "(SELECT 1 FROM sales_fact.Trade t WHERE t.BuyerCustomer_ID = c.ID AND t.Price > 10)",
    "SELECT c.ID FROM sales_dim.Customer c WHERE NOT EXISTS "
    "(SELECT 1 FROM sales_fact.Trade t WHERE t.BuyerCustomer_ID = c.ID)",
    "SELECT c.ID, (SELECT MAX(t.Price) FROM sales_fact.Trade t WHERE t.BuyerCustomer_ID = c.ID) AS MaxPrice, "
    "(SELECT COUNT(*) FROM sales_fact.Trade t2 WHERE t2.SellerCustomer_ID = c.ID AND t2.Price > 100) AS Sells "
    "FROM sales_dim.Customer c",
    "WITH buyers AS (SELECT t.BuyerCustomer_ID AS CustomerID, SUM(t.Price) AS Total FROM sales_fact.Trade t "
    "GROUP BY t.BuyerCustomer_ID) SELECT TOP 10 c.[Name], b.Total FROM buyers b "
    "INNER JOIN sales_dim.Customer c ON b.CustomerID = c.ID ORDER BY b.Total DESC",
    "WITH a AS (SELECT ID FROM sales_dim.Customer), b AS (SELECT ID FROM a WHERE ID > 5) SELECT COUNT(*) FROM b",
    "WITH x(n) AS (SELECT 1 AS n UNION ALL SELECT 2) SELECT n FROM x",
    "SELECT c.ID FROM sales_dim.Customer c UNION ALL SELECT s.ID FROM sales_dim.Supplier s",
    "SELECT c.ID FROM sales_dim.Customer c UNION SELECT s.ID FROM sales_dim.Supplier s "
    "UNION SELECT w.ID FROM sales_dim.Warehouse w ORDER BY 1",
    "SELECT c.ID FROM sales_dim.Customer c EXCEPT SELECT t.BuyerCustomer_ID FROM sales_fact.Trade t",
    "SELECT c.ID FROM sales_dim.Customer c INTERSECT SELECT t.BuyerCustomer_ID FROM sales_fact.Trade t",
    "SELECT t.ID, ROW_NUMBER() OVER (PARTITION BY t.BuyerCustomer_ID ORDER BY t.Date_ID DESC) AS rn "
    "FROM sales_fact.Trade t",
    "SELECT t.ID, SUM(t.Price) OVER (PARTITION BY t.Contract_ID ORDER BY t.Date_ID ROWS BETWEEN UNBOUNDED PRECEDING "
    "AND CURRENT ROW) AS Running, LAG(t.Price, 1) OVER (ORDER BY t.ID) AS Prev FROM sales_fact.Trade t",
    "SELECT q.ID FROM (SELECT t.ID, ROW_NUMBER() OVER (ORDER BY t.Price DESC) AS rn FROM sales_fact.Trade t) AS q "
    "WHERE q.rn <= 10",
    "SELECT t.ID FROM sales_fact.Trade t ORDER BY t.ID OFFSET 20 ROWS FETCH NEXT 10 ROWS ONLY",
    "SELECT t.ID FROM sales_fact.Trade t ORDER BY t.Price DESC, t.ID ASC OFFSET 0 ROWS FETCH NEXT 50 ROWS ONLY",
    "SELECT c.[Name] FROM sales_dim.Customer c WHERE c.City = N'تهران' AND c.[Name] LIKE N'%علی%'",
    "SELECT [Customer Name], [Total Value] FROM [sales dim].[Customer Summary] WHERE [Total Value] > 100",
    "SELECT c.[Name] AS [Customer Name], c.City AS [شهر] FROM sales_dim.Customer c",
    "SELECT t.ID FROM sales_fact.Trade t WHERE t.Price > ? AND t.Date_ID BETWEEN ? AND ?",
    "SELECT TOP (?) t.ID FROM sales_fact.Trade t WHERE t.Contract_ID = ?",
    "SELECT t.ID FROM sales_fact.Trade t WHERE (t.Side = 1 OR t.Side = 2) AND t.Price > 0",
    "SELECT t.ID FROM sales_fact.Trade t WHERE t.Price > 0 AND (t.Side = 1 AND t.[Count] > 3 "
    "OR t.Side = 2 AND t.[Count] > 100 OR t.Contract_ID IN (SELECT k.ID FROM sales_dim.Contract k WHERE k.Active = 1))",
    "SELECT YEAR(t.TradeDate) AS Y, DATEADD(day, 7, t.TradeDate) AS Due, GETDATE() AS Now FROM sales_fact.Trade t",
    "SELECT ISNULL(c.[Name], N'نامشخص') AS Nm, COALESCE(c.City, c.Province, 'n/a') AS Place, "
    "CAST(t.Price AS DECIMAL(18, 2)) AS P, CONVERT(NVARCHAR(10), t.TradeDate, 120) AS D "
    "FROM sales_fact.Trade t LEFT JOIN sales_dim.Customer c ON c.ID = t.BuyerCustomer_ID",
    "SELECT c.FirstName + N' ' + c.LastName AS FullName, t.Price * 1.5 AS Marked, t.[Count] % 2 AS Parity "
    "FROM sales_dim.Customer c INNER JOIN sales_fact.Trade t ON t.BuyerCustomer_ID = c.ID",
    "SELECT t.ID FROM sales_fact.Trade t CROSS APPLY (SELECT TOP 1 f.Qty FROM sales_fact.Fill f "
    "WHERE f.Trade_ID = t.ID ORDER BY f.ID) AS q",
    "SELECT t.ID, q.Qty FROM sales_fact.Trade t OUTER APPLY (SELECT TOP 1 f.Qty FROM sales_fact.Fill f "
    "WHERE f.Trade_ID = t.ID ORDER BY f.ID DESC) AS q WHERE q.Qty IS NULL",
    "SELECT 1",
    "SELECT t.* FROM sales_fact.Trade t",
    "SELECT COUNT(*) FROM sales_fact.Trade",
    "SELECT t.ID FROM sales_fact.Trade AS t WITH (NOLOCK) "
    "INNER JOIN sales_dim.Customer AS c WITH (NOLOCK) ON t.BuyerCustomer_ID = c.ID",
    "SELECT\n  t.ID,\n  t.Price\nFROM\n  sales_fact.Trade t\nWHERE\n  t.Price > 1\n",
    "SELECT t.ID FROM sales_fact.Trade t WHERE t.Price = 1.50 AND t.[Count] >= 10 AND t.Side <> 3 "
    "AND t.Contract_ID != 4;",
    "SELECT TOP 100 PERCENT t.ID FROM sales_fact.Trade t ORDER BY t.ID",
    "SELECT t.Side, COUNT(*) AS n FROM sales_fact.Trade t WHERE t.Price > 0 GROUP BY t.Side "
    "HAVING COUNT(*) > 5 AND MAX(t.Price) < 10 OR MIN(t.Price) > 1000",
    "SELECT a.x FROM sales_fact.A a INNER JOIN sales_fact.B b ON a.ID = b.A_ID "
    "INNER JOIN sales_fact.C c ON b.ID = c.B_ID AND c.Flag = 1 "
    "LEFT JOIN (SELECT d.C_ID, SUM(d.V) AS S FROM sales_fact.D d GROUP BY d.C_ID) AS dd ON dd.C_ID = c.ID "
    "WHERE dd.S > 0",
]


def _same_tree(a: str, b: str) -> bool:
    ta, tb = sqlglot.parse(a, read="tsql"), sqlglot.parse(b, read="tsql")
    return (
        len(ta) == len(tb) == 1
        and sql_format._normalise(ta[0]) == sql_format._normalise(tb[0])
    )


class TestCorpus:
    def test_the_corpus_is_big_enough_to_mean_something(self):
        assert len(CORPUS) >= 40

    @pytest.mark.parametrize("sql", CORPUS)
    def test_the_house_layout_applies_and_means_the_same_statement(self, sql):
        out = _house(sql)
        assert format_sql(sql) == out
        assert _same_tree(sql, out)

    @pytest.mark.parametrize("sql", CORPUS)
    def test_formatting_is_idempotent(self, sql):
        once = format_sql(sql)
        assert format_sql(once) == once

    @pytest.mark.parametrize("sql", CORPUS)
    def test_whitespace_rules(self, sql):
        out = format_sql(sql)
        assert out == out.strip("\n"), "no leading or trailing newline"
        assert "\r" not in out and "\t" not in out
        for line in out.split("\n"):
            assert line == line.rstrip(), f"trailing whitespace: {line!r}"

    @pytest.mark.parametrize("sql", CORPUS)
    def test_the_tables_touched_are_unchanged(self, sql):
        from security.sql_guard import extract_touched_tables

        assert sorted(extract_touched_tables(format_sql(sql))) == sorted(extract_touched_tables(sql))

    @pytest.mark.parametrize("sql", CORPUS)
    def test_every_clause_keyword_is_upper_case_at_the_start_of_its_line(self, sql):
        for line in format_sql(sql).split("\n"):
            head = line.strip()
            for keyword in ("select", "from", "where", "group by", "order by", "having", "union", "inner join"):
                assert not head.startswith(keyword + " ") and head != keyword


# ---------------------------------------------------------------------------
# Rule 8: the safety net
# ---------------------------------------------------------------------------

class TestFallbacks:
    def test_a_comment_falls_back_to_sqlglots_pretty_output(self):
        sql = "SELECT a -- the key\nFROM t"
        assert sql_format._house_layout(sql, "tsql") is None
        assert format_sql(sql) == _sqlglot_pretty(sql)

    def test_a_block_comment_falls_back(self):
        sql = "SELECT /* pick */ a FROM t"
        assert sql_format._house_layout(sql, "tsql") is None
        assert format_sql(sql) == _sqlglot_pretty(sql)

    def test_a_dash_dash_inside_a_string_is_not_a_comment(self):
        out = format_sql("SELECT a FROM t WHERE b = '--not a comment' AND c = '/* nor this */'")
        assert out.startswith("SELECT\n     a\n")
        assert "'--not a comment'" in out and "'/* nor this */'" in out

    @pytest.mark.parametrize(
        "sql",
        [
            "MERGE INTO t USING u ON t.a = u.a WHEN MATCHED THEN DELETE",
            "SELECT a FROM t PIVOT (SUM(x) FOR y IN ([p], [q])) AS pv",
            "SELECT a FROM t, u WHERE t.i = u.i",
            "SELECT a FROM t JOIN u USING (id)",
            "SELECT a FROM t LEFT JOIN u ON t.a = u.a OPTION (RECOMPILE)",
            "SELECT a INTO #tmp FROM t",
            "SELECT a FROM t GROUP BY ROLLUP (a)",
            "INSERT INTO t (a) VALUES (1)",
            "UPDATE t SET a = 1 WHERE b = 2",
        ],
    )
    def test_an_unhandled_construct_falls_back_to_sqlglots_pretty_output(self, sql):
        assert sql_format._house_layout(sql, "tsql") is None
        assert format_sql(sql) == _sqlglot_pretty(sql)

    def test_several_statements_are_returned_unchanged_rather_than_truncated(self):
        sql = "SELECT 1; SELECT 2"
        assert sql_format._house_layout(sql, "tsql") is None
        assert format_sql(sql) == sql

    def test_a_construct_sqlglot_rewrites_falls_back(self):
        """sqlglot's T-SQL generator turns DATEDIFF into one over CASTs, which
        is not the same tree as what was written; the check refuses it."""
        sql = "SELECT DATEDIFF(day, t.A, t.B) AS d FROM t"
        assert sql_format._house_layout(sql, "tsql") is None
        assert format_sql(sql) == _sqlglot_pretty(sql)

    def test_another_dialect_goes_to_sqlglot(self):
        sql = "SELECT a, b FROM t WHERE a = 1 LIMIT 5"
        assert format_sql(sql, dialect="postgres") == _sqlglot_pretty(sql, "postgres")

    def test_dialect_name_case_does_not_matter(self):
        assert format_sql("SELECT a FROM t", dialect="TSQL") == "SELECT\n     a\n\nFROM t"

    def test_a_layout_that_changes_the_statement_is_discarded(self, monkeypatch):
        """If the layout ever produced a different statement, the user would
        be shown SQL that is not the SQL that ran."""
        monkeypatch.setattr(
            sql_format._Layout, "query", lambda self, node: ["SELECT", "     b", "", "FROM t"]
        )
        sql = "SELECT a FROM t"
        assert format_sql(sql) == _sqlglot_pretty(sql)

    def test_a_layout_that_does_not_parse_is_discarded(self, monkeypatch):
        monkeypatch.setattr(sql_format._Layout, "query", lambda self, node: ["SELECT ((("])
        sql = "SELECT a FROM t"
        assert format_sql(sql) == _sqlglot_pretty(sql)

    def test_an_unexpected_error_in_the_layout_falls_back(self, monkeypatch):
        def boom(self, node):
            raise RuntimeError("layout bug")

        monkeypatch.setattr(sql_format._Layout, "query", boom)
        sql = "SELECT a FROM t"
        assert format_sql(sql) == _sqlglot_pretty(sql)

    def test_if_sqlglots_pretty_fails_too_the_input_is_returned(self, monkeypatch):
        def boom(sql, dialect):
            raise RuntimeError("pretty bug")

        monkeypatch.setattr(sql_format, "_sqlglot_pretty", boom)
        sql = "SELECT a FROM t, u"  # comma join: not handled by the layout
        assert format_sql(sql) == sql

    def test_unparseable_input_is_returned_unchanged(self):
        assert format_sql("this is not sql at all ((") == "this is not sql at all (("

    def test_input_that_collides_with_the_internal_placeholder_is_left_to_sqlglot(self):
        sql = "SELECT '__fmt_slot_0__' AS a FROM t"
        assert sql_format._house_layout(sql, "tsql") is None
        assert "__fmt_slot_0__" in format_sql(sql)

    @pytest.mark.parametrize("value", ["", "   ", "\n\t "])
    def test_empty_input_is_returned_as_given(self, value):
        assert format_sql(value) == value

    @pytest.mark.parametrize(
        "sql",
        [
            ";",
            "SELECT",
            "SELECT FROM",
            "SELECT * FROM",
            "((((",
            "SELECT '",
            "SELECT 1 FROM t WHERE",
            "\x00SELECT 1",
            "SELECT N'س' FROM t WHERE x = '" + "a" * 5000 + "'",
            "SELECT " + "(" * 3000 + "1" + ")" * 3000,
            "SELECT " + " + ".join(["a"] * 4000) + " FROM t",
            "SELECT a FROM t WHERE " + " AND ".join(f"c{i} = {i}" for i in range(400)),
        ],
    )
    def test_it_never_raises(self, sql):
        out = format_sql(sql)
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# pretty_sql still honours its contract
# ---------------------------------------------------------------------------

class TestPrettySqlDelegation:
    def test_pretty_sql_delegates_for_tsql(self):
        sql = "SELECT a, b FROM t WHERE a = 1"
        assert pretty_sql(sql, "tsql") == format_sql(sql, "tsql")

    def test_other_dialects_keep_sqlglots_pretty_output(self):
        sql = "SELECT a, b FROM t WHERE a = 1"
        assert pretty_sql(sql, "postgres") == _sqlglot_pretty(sql, "postgres")

    def test_it_never_raises_and_returns_garbage_unchanged(self):
        assert pretty_sql("not sql ((((", "tsql") == "not sql (((("
