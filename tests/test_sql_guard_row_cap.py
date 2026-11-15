# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Row-cap and LIMIT regression tests for security/sql_guard.py (tsql).

For tsql, ``ensure_top`` decides on the parsed tree and splices the cap at a
token offset, and ``clean_sql`` finds its LIMIT and ``TOP ... DISTINCT``
rewrites from the same tokens. Before that change both worked on raw text
with regular expressions, so a string literal, a bracketed or quoted
identifier or a comment could be read as SQL, and ``TOP 100 PERCENT`` or
``TOP (5)`` was misread. This module pins the new behaviour:

* ``TOP n PERCENT`` is not a row cap: ``ensure_top`` refuses it.
* ``TOP (n)``, ``TOP n WITH TIES`` and ``OFFSET ... FETCH`` are row caps:
  the query is returned unchanged.
* ``LIMIT`` in a string literal, a bracketed or quoted identifier or a
  comment is text. ``validate_sql`` accepts it, and ``clean_sql`` never
  takes a TOP from it or edits it.
* A ``LIMIT`` that ends the statement becomes ``TOP n``. Any other ``LIMIT``
  (inside a subquery, ``LIMIT n OFFSET m``, ``LIMIT m, n``) is left in place
  for ``validate_sql`` to reject.
* The cap goes after the outermost ``SELECT``, or after its ``DISTINCT`` or
  ``ALL``, and every other character of the input is returned as written.

Run::

    pytest tests/test_sql_guard_row_cap.py -v
"""

from __future__ import annotations

import pytest

from security.sql_guard import (
    CorrectableRejection,
    clean_sql,
    ensure_top,
    validate_sql,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refusal(sql: str, n: int = 50) -> CorrectableRejection:
    """Return the CorrectableRejection that ``ensure_top`` raises for *sql*."""
    with pytest.raises(CorrectableRejection) as info:
        ensure_top(sql, n)
    return info.value


def _limit_rejection(sql: str) -> str:
    """Return the message ``validate_sql`` raises for a LIMIT clause in *sql*."""
    with pytest.raises(CorrectableRejection) as info:
        validate_sql(sql)
    assert info.value.reason == "other"
    return str(info.value)


# ---------------------------------------------------------------------------
# ensure_top: TOP forms that are or are not row caps
# ---------------------------------------------------------------------------


class TestTopPercentIsNotARowCap:
    """Bug a: ``TOP 100 PERCENT`` used to leave the query uncapped."""

    def test_top_percent_is_refused_with_a_correctable_reason(self) -> None:
        error = _refusal("SELECT TOP 100 PERCENT * FROM t")

        assert error.reason == "other"
        assert "PERCENT" in str(error)
        assert "TOP n" in str(error)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT TOP 10 PERCENT a FROM t",
            "SELECT TOP 10 PERCENT a FROM t WHERE b = 1 ORDER BY a",
            "WITH c AS (SELECT x FROM t) SELECT TOP 5 PERCENT x FROM c",
        ],
    )
    def test_percent_is_refused_wherever_it_is_the_outer_limit(self, sql: str) -> None:
        assert "PERCENT" in str(_refusal(sql))

    def test_percent_inside_a_subquery_does_not_stop_the_outer_cap(self) -> None:
        sql = "SELECT * FROM (SELECT TOP 10 PERCENT a FROM t) z"

        assert ensure_top(sql, 50) == "SELECT TOP 50 * FROM (SELECT TOP 10 PERCENT a FROM t) z"


class TestTopWithParenthesesIsCapped:
    """Bug b: ``TOP (5)`` used to get a second TOP in front of it."""

    def test_top_with_parenthesised_count_is_left_unchanged(self) -> None:
        assert ensure_top("SELECT TOP (5) * FROM t", 100) == "SELECT TOP (5) * FROM t"

    def test_no_second_top_is_inserted(self) -> None:
        assert "TOP 100 TOP" not in ensure_top("SELECT TOP (5) * FROM t", 100)


class TestOtherRowCapClausesCountAsCapped:
    """``TOP n WITH TIES`` and ``OFFSET ... FETCH`` already cap the rows."""

    def test_top_with_ties_is_left_unchanged(self) -> None:
        sql = "SELECT TOP 5 WITH TIES a FROM t ORDER BY a"

        assert ensure_top(sql, 100) == sql

    def test_offset_fetch_is_left_unchanged(self) -> None:
        sql = "SELECT a FROM t ORDER BY a OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY"

        assert ensure_top(sql, 100) == sql

    def test_offset_without_fetch_still_gets_a_top(self) -> None:
        # OFFSET alone caps nothing; the existing placement is kept.
        assert (
            ensure_top("SELECT a FROM t ORDER BY a OFFSET 0 ROWS", 50)
            == "SELECT TOP 50 a FROM t ORDER BY a OFFSET 0 ROWS"
        )


# ---------------------------------------------------------------------------
# ensure_top: where the cap goes
# ---------------------------------------------------------------------------


class TestCapPlacement:
    """The cap goes after the outermost SELECT, DISTINCT or ALL."""

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("SELECT * FROM Contract", "SELECT TOP 50 * FROM Contract"),
            ("SELECT DISTINCT Name FROM Customer", "SELECT DISTINCT TOP 50 Name FROM Customer"),
            ("SELECT ALL a FROM t", "SELECT ALL TOP 50 a FROM t"),
            (
                "WITH cte AS (SELECT 1) SELECT * FROM cte",
                "WITH cte AS (SELECT 1) SELECT TOP 50 * FROM cte",
            ),
            (
                "SELECT * FROM (SELECT TOP 1 a FROM t) z",
                "SELECT TOP 50 * FROM (SELECT TOP 1 a FROM t) z",
            ),
            ("SELECT Active=1, x FROM t", "SELECT TOP 50 Active=1, x FROM t"),
            ("SELECT TOP 5 DISTINCT a FROM t", "SELECT TOP 5 DISTINCT a FROM t"),
        ],
    )
    def test_cap_lands_on_the_outermost_select(self, sql: str, expected: str) -> None:
        assert ensure_top(sql, 50) == expected

    def test_lower_case_keywords_keep_their_case(self) -> None:
        assert ensure_top("select distinct name from customer", 20) == (
            "select distinct TOP 20 name from customer"
        )

    def test_text_outside_the_select_list_is_returned_as_written(self) -> None:
        sql = "SELECT\n  a,\n\tb\nFROM t -- note\nWHERE a = 1"

        assert ensure_top(sql, 7) == "SELECT TOP 7\n  a,\n\tb\nFROM t -- note\nWHERE a = 1"

    def test_union_wraps_the_whole_query_including_a_parenthesised_branch(self) -> None:
        # Before: TOP landed on the first branch only, so the second branch
        # (in parentheses) was not capped. Now the whole set operation is wrapped.
        assert ensure_top("SELECT a FROM t UNION (SELECT b FROM u)", 50) == (
            "SELECT TOP 50 * FROM (SELECT a FROM t UNION (SELECT b FROM u)) AS _ensure_top_capped"
        )

    def test_union_with_a_cte_prefix_keeps_the_cte_outside_the_wrapper(self) -> None:
        assert ensure_top("WITH c AS (SELECT x FROM t) SELECT x FROM c UNION SELECT y FROM u", 5) == (
            "WITH c AS (SELECT x FROM t) SELECT TOP 5 * FROM "
            "(SELECT x FROM c UNION SELECT y FROM u) AS _ensure_top_capped"
        )


# ---------------------------------------------------------------------------
# Quoted identifiers, string literals and comments are not SQL
# ---------------------------------------------------------------------------


class TestQuotedTextIsNotSql:
    """Bug f: ``SELECT`` inside brackets, quotes or comments was read as SQL."""

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("SELECT [SELECT] FROM t", "SELECT TOP 50 [SELECT] FROM t"),
            (
                "SELECT [a'b], x FROM t WHERE y IN (SELECT z FROM u)",
                "SELECT TOP 50 [a'b], x FROM t WHERE y IN (SELECT z FROM u)",
            ),
            ('SELECT "a(b" FROM t', 'SELECT TOP 50 "a(b" FROM t'),
            ("SELECT /* ( */ a FROM t", "SELECT TOP 50 /* ( */ a FROM t"),
            (
                "SELECT a FROM t -- SELECT TOP 9 x\nWHERE b = 1",
                "SELECT TOP 50 a FROM t -- SELECT TOP 9 x\nWHERE b = 1",
            ),
        ],
    )
    def test_cap_goes_after_the_real_outer_select(self, sql: str, expected: str) -> None:
        assert ensure_top(sql, 50) == expected

    def test_bracketed_select_is_not_mistaken_for_a_set_operation(self) -> None:
        assert "_ensure_top_capped" not in ensure_top("SELECT [SELECT] FROM t", 50)

    def test_clean_sql_leaves_a_bracketed_top_distinct_alone(self) -> None:
        assert clean_sql("SELECT [SELECT TOP 4 DISTINCT] FROM t") == (
            "SELECT [SELECT TOP 4 DISTINCT] FROM t"
        )

    def test_clean_sql_leaves_a_bracketed_limit_alone(self) -> None:
        assert clean_sql("SELECT a FROM [t limit 5]") == "SELECT a FROM [t limit 5]"

    def test_clean_sql_leaves_a_quoted_identifier_limit_alone(self) -> None:
        assert clean_sql('SELECT "LIMIT 5" FROM t') == 'SELECT "LIMIT 5" FROM t'

    def test_clean_sql_leaves_a_commented_limit_alone(self) -> None:
        assert clean_sql("SELECT a FROM t -- LIMIT 5") == "SELECT a FROM t -- LIMIT 5"
        assert clean_sql("SELECT a FROM t /* LIMIT 5 */") == "SELECT a FROM t /* LIMIT 5 */"


# ---------------------------------------------------------------------------
# clean_sql: LIMIT in a string literal
# ---------------------------------------------------------------------------


class TestLimitInsideStringLiteral:
    """Bugs c and d: the literal was destroyed and a wrong TOP was taken from it."""

    def test_literal_survives_and_no_top_is_taken_from_it(self) -> None:
        assert clean_sql("SELECT a FROM t WHERE n = 'LIMIT 5'") == (
            "SELECT a FROM t WHERE n = 'LIMIT 5'"
        )

    def test_real_trailing_limit_is_used_and_literal_keeps_its_text(self) -> None:
        assert clean_sql("SELECT a FROM t WHERE n = 'x LIMIT 5 y' LIMIT 10") == (
            "SELECT TOP 10 a FROM t WHERE n = 'x LIMIT 5 y'"
        )

    def test_n_prefixed_literal_is_not_rewritten(self) -> None:
        assert clean_sql("SELECT a FROM t WHERE n = N'LIMIT 5' LIMIT 3") == (
            "SELECT TOP 3 a FROM t WHERE n = N'LIMIT 5'"
        )

    def test_top_distinct_inside_a_literal_is_not_reordered(self) -> None:
        assert clean_sql("SELECT TOP 10 DISTINCT a FROM t WHERE n = 'SELECT TOP 2 DISTINCT'") == (
            "SELECT DISTINCT TOP 10 a FROM t WHERE n = 'SELECT TOP 2 DISTINCT'"
        )

    def test_doubled_quote_inside_literal_keeps_the_literal_intact(self) -> None:
        assert clean_sql("SELECT a FROM t WHERE n = 'it''s' LIMIT 4") == (
            "SELECT TOP 4 a FROM t WHERE n = 'it''s'"
        )


# ---------------------------------------------------------------------------
# validate_sql: LIMIT is a clause, not text
# ---------------------------------------------------------------------------


class TestValidateLimitRule:
    """Bug e: any ``LIMIT <digits>`` text was rejected, even in a literal."""

    def test_limit_text_inside_a_string_literal_is_accepted(self) -> None:
        validate_sql("SELECT TOP 5 Code FROM Broker WHERE Code = 'LIMIT 5'")

    def test_limit_text_inside_a_bracketed_identifier_is_accepted(self) -> None:
        validate_sql("SELECT TOP 5 [LIMIT 5] FROM Broker")

    def test_column_named_limit_is_accepted(self) -> None:
        validate_sql("SELECT Limit FROM Broker")

    def test_real_trailing_limit_is_still_rejected(self) -> None:
        message = _limit_rejection("SELECT TOP 5 Code FROM Broker LIMIT 5")

        assert message == "LIMIT is not valid T-SQL — use TOP instead"

    def test_limit_with_offset_is_still_rejected(self) -> None:
        assert "LIMIT is not valid T-SQL" in _limit_rejection(
            "SELECT Code FROM Broker LIMIT 5 OFFSET 2"
        )


# ---------------------------------------------------------------------------
# clean_sql: LIMIT -> TOP for the statement's own LIMIT only
# ---------------------------------------------------------------------------


class TestTrailingLimitBecomesTop:
    """A LIMIT that ends the statement becomes TOP; nothing else does."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("SELECT a FROM t LIMIT 10", "SELECT TOP 10 a FROM t"),
            ("SELECT a FROM t LIMIT 10;", "SELECT TOP 10 a FROM t;"),
            ("SELECT TOP 3 a FROM t LIMIT 10", "SELECT TOP 3 a FROM t"),
            ("SELECT DISTINCT a FROM t LIMIT 3", "SELECT DISTINCT TOP 3 a FROM t"),
            ("SELECT TOP 10 DISTINCT a FROM t LIMIT 3", "SELECT DISTINCT TOP 10 a FROM t"),
            ("select a from t limit 7", "select TOP 7 a from t"),
            ("SELECT\n  a\nFROM t\nLIMIT 7", "SELECT TOP 7\n  a\nFROM t"),
            (
                "WITH c AS (SELECT x FROM t) SELECT * FROM c LIMIT 5",
                "WITH c AS (SELECT x FROM t) SELECT TOP 5 * FROM c",
            ),
        ],
    )
    def test_trailing_limit_is_rewritten(self, raw: str, expected: str) -> None:
        assert clean_sql(raw) == expected

    def test_limit_inside_a_subquery_is_left_in_place(self) -> None:
        raw = "SELECT a FROM (SELECT b FROM u LIMIT 3) x"

        assert clean_sql(raw) == raw
        assert "LIMIT is not valid T-SQL" in _limit_rejection(clean_sql(raw))

    def test_limit_inside_a_cte_body_is_left_in_place(self) -> None:
        raw = "WITH c AS (SELECT x FROM t LIMIT 2) SELECT * FROM c"

        assert clean_sql(raw) == raw

    def test_limit_with_offset_is_left_in_place(self) -> None:
        raw = "SELECT a FROM t LIMIT 5 OFFSET 2"

        assert clean_sql(raw) == raw

    def test_limit_m_comma_n_is_left_in_place(self) -> None:
        raw = "SELECT a FROM t LIMIT 2, 5"

        assert clean_sql(raw) == raw

    def test_text_that_does_not_tokenize_is_returned_without_rewrites(self) -> None:
        raw = "SELECT 'abc"

        assert clean_sql(raw) == raw

    def test_ensure_top_refuses_text_that_does_not_tokenize(self) -> None:
        assert "does not tokenize" in str(_refusal("SELECT 'abc", 5))


# ---------------------------------------------------------------------------
# Byte-identical behaviour that must not change
# ---------------------------------------------------------------------------


class TestUnchangedBehaviour:
    """Queries the old code handled correctly come back byte-for-byte."""

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("SELECT * FROM Contract", "SELECT TOP 50 * FROM Contract"),
            ("SELECT TOP 10 * FROM Contract", "SELECT TOP 10 * FROM Contract"),
            ("SELECT DISTINCT Name FROM Customer", "SELECT DISTINCT TOP 50 Name FROM Customer"),
            ("SELECT TOP 10 DISTINCT Name FROM Customer", "SELECT TOP 10 DISTINCT Name FROM Customer"),
            ("WITH cte AS (SELECT 1) SELECT * FROM cte", "WITH cte AS (SELECT 1) SELECT TOP 50 * FROM cte"),
            (
                "SELECT a FROM t1 UNION SELECT b FROM t2",
                "SELECT TOP 50 * FROM (SELECT a FROM t1 UNION SELECT b FROM t2) AS _ensure_top_capped",
            ),
        ],
    )
    def test_output_is_byte_identical(self, sql: str, expected: str) -> None:
        assert ensure_top(sql, 50) == expected


class TestSetOperationOrderBy:
    """A set operation with a top-level ORDER BY is still refused."""

    def test_union_with_order_by_is_refused(self) -> None:
        error = _refusal("SELECT a FROM t1 UNION SELECT b FROM t2 ORDER BY 1")

        assert error.reason == "other"
        assert "ORDER BY" in str(error)
