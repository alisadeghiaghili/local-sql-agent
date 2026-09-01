# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Security regression suite for security/sql_guard.py.

This module documents, as **tests that must keep passing**, every way the
pre-Phase-1 string-blocklist guard in ``security/sql_guard.py`` could be
bypassed or misfired. Every case here was originally verified by direct
execution against the guard at baseline commit ``96d8f2b`` — none were
hypothetical — and is now verified against the Phase 1 sqlglot-AST-based
guard instead.

History: ``xfail(strict=True)``, now removed
-----------------------------------------------
Until Phase 1 landed, every case below was wrapped in
``@pytest.mark.xfail(strict=True, reason=...)``: ``strict=True`` meant an
*unexpected pass* (XPASS) was treated as a test **failure**, so the moment
``sql_guard.py`` was fixed in a way that also fixed the assertion below, CI
turned red until the marker was removed — making "remove the marker" the
explicit, reviewable signal that the underlying bug was actually fixed, not
an accidental side effect. That let this file document known bypasses
without turning the pre-Phase-1 suite red before any fix existed.

Phase 1 replaced the blocklist with a parser-based guard (sqlglot, pinned to
the ``tsql`` dialect) that fixes every case below by construction rather
than by pattern-matching around it — see ``security/sql_guard.py``'s module
docstring for how. All ``xfail`` markers have been removed accordingly; a
regression in any of them now fails this suite directly, the same as any
other test.

Every assertion in this file targets **behaviour** (does ``validate_sql``
raise? does ``ensure_top``/``clean_sql`` return the semantically-correct
SQL string?) — never the wording of an exception message, so a future fix
that raises a different but still-correct error message does not
spuriously break these tests.

Run::

    pytest tests/test_sql_guard_bypass.py -v
"""

from __future__ import annotations

import pytest

from security.sql_guard import clean_sql, ensure_top, validate_sql


# ---------------------------------------------------------------------------
# Attack family 1: non-space separators defeat the space-suffixed blocklist
# ---------------------------------------------------------------------------
#
# _FORBIDDEN entries like "DROP " and "DELETE " require a literal trailing
# space character after the keyword to match. Any other whitespace or
# separator character (newline, tab, semicolon-then-newline, or simply the
# keyword being the last token in the string) slips straight past the
# `kw in upper` substring check.

class TestNonSpaceSeparatorBypass:
    """Mechanism: `"DROP " in sql.upper()` requires a literal space after
    the keyword. A newline, tab, or any non-space whitespace character
    immediately after the keyword defeats the check entirely, even though
    T-SQL treats all of these as ordinary statement/token separators.
    """

    def test_drop_followed_by_newline_is_not_blocked(self):
        sql = "SELECT 1;DROP\nTABLE Contract"
        with pytest.raises(ValueError):
            validate_sql(sql)

    def test_delete_followed_by_tab_is_not_blocked(self):
        sql = "SELECT 1; DELETE\tFROM t"
        with pytest.raises(ValueError):
            validate_sql(sql)


# ---------------------------------------------------------------------------
# Attack family 2: write / DDL operations entirely absent from the blocklist
# ---------------------------------------------------------------------------
#
# _FORBIDDEN only lists: DELETE, UPDATE, INSERT, DROP, ALTER, TRUNCATE,
# MERGE, EXEC, EXECUTE, XP_, SP_. Several T-SQL constructs that write data,
# create objects, or change permissions are not on that list at all.

class TestUnlistedWriteOperations:
    """Mechanism: whole categories of destructive/write T-SQL statements
    were simply never added to `_FORBIDDEN`, so no substring check catches
    them regardless of separator/formatting.
    """

    def test_select_into_creates_a_table(self):
        """`SELECT ... INTO` is a write (creates NewTbl) despite starting
        with the allowed keyword SELECT."""
        sql = "SELECT * INTO NewTbl FROM Contract"
        with pytest.raises(ValueError):
            validate_sql(sql)

    def test_create_table_not_blocked(self):
        """README.md explicitly claims CREATE is blocked; it is not."""
        sql = "SELECT 1; CREATE TABLE x (a int)"
        with pytest.raises(ValueError):
            validate_sql(sql)

    def test_grant_not_blocked(self):
        """GRANT changes permissions -- a privilege-escalation vector."""
        sql = "SELECT 1; GRANT CONTROL TO public"
        with pytest.raises(ValueError):
            validate_sql(sql)


class TestDenialOfServiceOperations:
    """Mechanism: statements that don't write data but can make the
    database unusable (deliberate delay, full shutdown) are absent from
    the blocklist."""

    def test_waitfor_delay_not_blocked(self):
        """WAITFOR DELAY lets a single query hold a connection/lock
        indefinitely -- a trivial DoS vector via the NLQ endpoint."""
        sql = "SELECT 1; WAITFOR DELAY '00:00:10'"
        with pytest.raises(ValueError):
            validate_sql(sql)

    def test_shutdown_not_blocked(self):
        sql = "SELECT 1; SHUTDOWN"
        with pytest.raises(ValueError):
            validate_sql(sql)


class TestRemoteAndFileAccessOperations:
    """Mechanism: OPENROWSET/OPENQUERY-style functions let a query read
    arbitrary remote data sources or local files -- a data-exfiltration
    vector -- and are absent from the blocklist."""

    def test_openrowset_not_blocked(self):
        sql = "SELECT * FROM OPENROWSET(1)"
        with pytest.raises(ValueError):
            validate_sql(sql)


# ---------------------------------------------------------------------------
# Attack family 3: trailing-token / comment interaction
# ---------------------------------------------------------------------------

class TestTrailingTokenBypass:
    """Mechanism: `"DROP "` (note the trailing space in the blocklist
    entry) never matches when DROP is the very last token in the string --
    there is nothing after it to supply that trailing space. This is the
    same root cause as `TestNonSpaceSeparatorBypass` (a naive
    space-suffixed substring check instead of proper tokenisation),
    surfaced here through a SQL line comment where the forbidden keyword
    is the final token before the string ends. The guard does no comment
    stripping/awareness at all, so this is really just "keyword at
    end-of-string", coincidentally spelled as a comment.
    """

    def test_forbidden_keyword_as_final_token_is_not_blocked(self):
        sql = "SELECT a FROM t WHERE x=1 -- DROP"
        with pytest.raises(ValueError):
            validate_sql(sql)


# ---------------------------------------------------------------------------
# Attack family 4: stacked statements are not rejected as a class
# ---------------------------------------------------------------------------

class TestStackedStatementsGeneral:
    """Mechanism: the guard has no concept of "this string must contain
    exactly one statement". It only greps the whole string for forbidden
    keywords. A stacked statement whose second half contains no forbidden
    keyword sails through untouched -- e.g. any future DBA-added stored
    procedure or extended function not yet on the blocklist becomes an
    instant bypass the moment it's reachable via a semicolon.
    """

    def test_stacked_benign_looking_statement_is_not_rejected_outright(self):
        """A defence-in-depth guard should refuse *any* multi-statement
        input outright, not rely solely on recognising each statement's
        individual keyword."""
        sql = "SELECT 1; SELECT 2"
        with pytest.raises(ValueError):
            validate_sql(sql)


# ---------------------------------------------------------------------------
# Attack family 5: false positives -- legitimate SQL currently rejected
# ---------------------------------------------------------------------------

class TestSubstringFalsePositives:
    """Mechanism: `"XP_"` / `"SP_"` are blocked as bare substrings (no
    trailing space, unlike the other entries), so they match inside
    ordinary, unrelated column names that merely happen to contain that
    letter sequence.
    """

    def test_exp_date_column_rejected_due_to_xp_substring(self):
        """'EXP_DATE' contains 'XP_' as a substring. The table is a real
        one (Contract, from schema_data/columns.py) -- Phase 1 added a
        real table allowlist, and this case is about the column name, not
        the table, so it must not incidentally trip on an unrelated
        placeholder table."""
        validate_sql("SELECT EXP_DATE FROM Contract")

    def test_resp_code_column_rejected_due_to_sp_substring(self):
        """'RESP_CODE' contains 'SP_' as a substring. Real table, same
        reasoning as above."""
        validate_sql("SELECT RESP_CODE FROM Contract")


class TestKeywordInsideStringLiteralFalsePositive:
    """Mechanism: the blocklist scan runs over the raw SQL text with no
    awareness of string-literal boundaries, so a forbidden keyword that
    appears inside a quoted string value (data, not syntax) is
    indistinguishable from the keyword appearing as an actual SQL command.
    """

    def test_drop_inside_string_literal_is_not_a_drop_statement(self):
        """Real table (Contract) -- this case is about the string literal,
        not the table name."""
        sql = "SELECT * FROM Contract WHERE Note = N'please DROP the box'"
        validate_sql(sql)


# ---------------------------------------------------------------------------
# ensure_top() correctness: TOP must land on the OUTERMOST select
# ---------------------------------------------------------------------------
#
# ensure_top() finds the *first* `SELECT` keyword anywhere in the string
# via `re.sub(..., count=1)` and inserts `TOP n` right after it. That is
# correct only when the first SELECT in the string is also the outermost
# one. It is wrong whenever a CTE, DISTINCT, or an already-capped subquery
# appears before (or as) the true outer SELECT.

class TestEnsureTopOutermostSelect:
    """Mechanism: `ensure_top` blindly replaces the *first* occurrence of
    the literal word SELECT. In a `WITH cte AS (SELECT ...) SELECT ...`
    query, the first SELECT is the one *inside* the CTE body -- so the row
    cap lands on the CTE's internal query instead of the query that
    actually returns rows to the caller, leaving the real output
    unbounded.
    """

    def test_top_lands_on_outer_select_not_inside_cte(self):
        sql = "WITH cte AS (SELECT 1) SELECT * FROM cte"
        result = ensure_top(sql, 20)
        assert result == "WITH cte AS (SELECT 1) SELECT TOP 20 * FROM cte"


class TestEnsureTopDistinctOrdering:
    """Mechanism: `ensure_top` inserts `TOP n` immediately after the
    literal word SELECT with no regard for a following DISTINCT keyword,
    producing `SELECT TOP n DISTINCT ...` -- invalid T-SQL. (`clean_sql`
    has a dedicated regex to fix this ordering when *it* is the one adding
    TOP, but `ensure_top` -- used as a defence-in-depth safety net after
    validation -- has no equivalent fix.)
    """

    def test_top_must_precede_distinct_correctly_not_between_select_and_distinct(self):
        sql = "SELECT DISTINCT Name FROM Customer"
        result = ensure_top(sql, 20)
        assert result == "SELECT DISTINCT TOP 20 Name FROM Customer"


class TestEnsureTopSubqueryOuterCap:
    """Mechanism: `ensure_top` treats "does the string contain the literal
    word TOP anywhere" as "the query is already capped" and returns the
    input unchanged. When the *only* TOP present belongs to an inner
    subquery, the outer query -- the one whose row count actually reaches
    the API layer -- remains completely uncapped.
    """

    def test_outer_query_still_capped_when_only_subquery_has_top(self):
        sql = "SELECT * FROM (SELECT TOP 1 a FROM t) z"
        result = ensure_top(sql, 10)
        assert result == "SELECT TOP 10 * FROM (SELECT TOP 1 a FROM t) z"


# ---------------------------------------------------------------------------
# clean_sql() correctness: LIMIT -> TOP must target the OUTERMOST select
# ---------------------------------------------------------------------------

class TestCleanSqlLimitTargetsOutermostSelect:
    """Mechanism: identical root cause to `TestEnsureTopOutermostSelect`.
    `clean_sql`'s LIMIT->TOP conversion also does
    `re.sub(r"\\bSELECT\\b", f"SELECT TOP {n}", sql, count=1, ...)`, so a
    MySQL-style `LIMIT n` trailing a CTE query gets converted into a TOP
    clause injected into the CTE's inner SELECT -- changing the query's
    semantics (the CTE materialises only n rows internally, rather than
    the final result being capped at n rows).
    """

    def test_limit_on_cte_query_converts_outer_select_not_inner(self):
        sql = "WITH c AS (SELECT x FROM t) SELECT * FROM c LIMIT 5"
        result = clean_sql(sql)
        assert result == "WITH c AS (SELECT x FROM t) SELECT TOP 5 * FROM c"
