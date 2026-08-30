"""Edge-case tests for ``session.composer`` not already covered by its doctests."""

from __future__ import annotations

import pandas as pd
import pytest

from security.sql_guard import validate_sql
from session.composer import CompositionError, compose_refinement_sql, check_scan_truncated


class TestStripDisplayCapErrors:
    def test_empty_sql_raises(self):
        from session.composer import strip_display_cap
        with pytest.raises(CompositionError):
            strip_display_cap("")

    def test_stacked_statements_raise(self):
        from session.composer import strip_display_cap
        with pytest.raises(CompositionError):
            strip_display_cap("SELECT 1; SELECT 2")

    def test_non_select_root_raises(self):
        from session.composer import strip_display_cap
        with pytest.raises(CompositionError):
            strip_display_cap("DELETE FROM Customer")


class TestComposeRefinementSqlRejectsOwnWithClause:
    def test_outer_query_with_its_own_with_clause_is_rejected(self):
        prev = "SELECT TOP 10 Name FROM Customer"
        outer = "WITH x AS (SELECT 1) SELECT * FROM x"
        with pytest.raises(CompositionError):
            compose_refinement_sql(prev, outer, cap=1000)


class TestComposedSqlPassesTheRealGuard:
    """Requirement 1: the composed statement is what validate_sql sees."""

    def test_benign_composition_is_allowed(self):
        prev = "SELECT TOP 100 c.Name AS CustomerName FROM Customer c WHERE c.IsActive = 1"
        outer = "SELECT TOP 10 c_Name FROM _prev ORDER BY c_Name"
        composed = compose_refinement_sql(prev, outer, cap=10_000)
        validate_sql(composed)  # must not raise

    def test_forbidden_table_hidden_inside_prev_is_still_caught(self):
        prev = "SELECT TOP 100 * FROM HR_Payroll"
        outer = "SELECT TOP 10 * FROM _prev"
        composed = compose_refinement_sql(prev, outer, cap=10_000)
        with pytest.raises(ValueError, match="HR_Payroll"):
            validate_sql(composed)

    def test_forbidden_statement_hidden_inside_outer_is_still_caught(self):
        prev = "SELECT TOP 100 Name FROM Customer"
        # A DROP has no SELECT/WITH keyword at all, so clean_sql() itself
        # already refuses it (it can't even locate a query to clean) --
        # the composition never reaches a state where an unvalidated
        # DROP could slip through.
        outer = "DROP TABLE Customer"
        with pytest.raises(ValueError):
            compose_refinement_sql(prev, outer, cap=10_000)

    def test_select_wrapped_forbidden_statement_is_caught_by_validate_sql(self):
        prev = "SELECT TOP 100 Name FROM Customer"
        # This DOES look like a SELECT to clean_sql (starts with the
        # keyword), so composition succeeds -- but it is a stacked
        # statement, and validate_sql on the composed whole still refuses it.
        outer = "SELECT 1; DROP TABLE Customer"
        composed = compose_refinement_sql(prev, outer, cap=10_000)
        with pytest.raises(ValueError):
            validate_sql(composed)


class TestCheckScanTruncated:
    def test_true_when_predicate_exceeds_cap(self):
        calls: list[str] = []

        def execute_fn(sql: str) -> pd.DataFrame:
            calls.append(sql)
            return pd.DataFrame({"Cnt": [3]})  # cap+1 == 3 -> probe filled completely

        truncated = check_scan_truncated(execute_fn, "SELECT TOP 100 Name FROM Customer", cap=2)
        assert truncated is True
        assert len(calls) == 1
        assert "TOP 3" in calls[0]  # cap + 1

    def test_false_when_predicate_is_within_cap(self):
        def execute_fn(sql: str) -> pd.DataFrame:
            return pd.DataFrame({"Cnt": [2]})  # fewer than cap+1

        truncated = check_scan_truncated(execute_fn, "SELECT TOP 100 Name FROM Customer", cap=10)
        assert truncated is False

    def test_false_on_empty_result(self):
        def execute_fn(sql: str) -> pd.DataFrame:
            return pd.DataFrame()

        assert check_scan_truncated(execute_fn, "SELECT TOP 100 Name FROM Customer", cap=10) is False
