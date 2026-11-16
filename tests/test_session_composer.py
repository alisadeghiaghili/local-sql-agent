# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
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


_PG_PREV = 'SELECT c."Name" FROM "Customer" AS c LIMIT 5'
_PG_PREDICATE = (
    "SELECT c.ID AS c_ID, c.Name AS c_Name, c.NationalID AS c_NationalID, "
    "c.IsActive AS c_IsActive FROM [Customer] AS c"
)


class TestPostgresDialect:
    """The previous turn's stored SQL may be in the target dialect; the guard SQL is T-SQL."""

    def test_strip_display_cap_returns_tsql(self):
        from session.composer import strip_display_cap
        assert strip_display_cap(_PG_PREV, dialect="postgres") == _PG_PREDICATE

    def test_build_capped_predicate_uses_top(self):
        from session.composer import build_capped_predicate
        result = build_capped_predicate(_PG_PREV, cap=10, dialect="postgres")
        assert result.startswith("SELECT TOP 10 ")
        assert "LIMIT" not in result

    def test_predicate_columns(self):
        from session.composer import predicate_columns
        assert predicate_columns(_PG_PREV, dialect="postgres") == [
            "c_ID", "c_Name", "c_NationalID", "c_IsActive",
        ]

    def test_composed_postgres_refinement_passes_guard(self):
        prev = 'SELECT c."Name" FROM "Customer" AS c WHERE c."IsActive" = 1 LIMIT 5'
        composed = compose_refinement_sql(
            prev, "SELECT TOP 3 c_Name FROM _prev", cap=50, dialect="postgres",
        )
        assert composed.startswith("WITH _prev AS (")
        validate_sql(composed)  # must not raise


class TestComposeRefinementOuterDecisions:
    """The outer query is judged on its parsed tree, and kept byte-for-byte."""

    _PREV = "SELECT TOP 10 Name FROM Customer"

    @pytest.mark.parametrize(
        "outer",
        [
            "with x as (select 1 as a) select a from x",
            "\n\n   WITH x AS (SELECT 1 AS a) SELECT a FROM x",
            "```sql\nWITH x AS (SELECT 1 AS a) SELECT a FROM x\n```",
        ],
    )
    def test_outer_with_clause_is_rejected(self, outer: str):
        with pytest.raises(CompositionError):
            compose_refinement_sql(self._PREV, outer, cap=1000)

    def test_set_operation_over_prev_composes_with_single_cte(self):
        import sqlglot
        from sqlglot import exp

        outer = "SELECT c_Name FROM _prev UNION SELECT c_Name FROM _prev"
        composed = compose_refinement_sql(self._PREV, outer, cap=1000)
        statements = sqlglot.parse(composed, read="tsql")
        assert len(statements) == 1
        ctes = list(statements[0].find_all(exp.CTE))
        assert [cte.alias for cte in ctes] == ["_prev"]

    @pytest.mark.parametrize(
        "outer",
        ["SELECT c_Name FROM _prev; SELECT 1", "SELECT FROM WHERE"],
    )
    def test_unparseable_outer_is_composed_and_left_to_the_guard(self, outer: str):
        composed = compose_refinement_sql(self._PREV, outer, cap=1000)
        assert composed.startswith("WITH _prev AS (")
        with pytest.raises(ValueError):
            validate_sql(composed)

    def test_outer_text_is_kept_byte_for_byte(self):
        outer = "SELECT   TOP 3  c_Name\n  FROM _prev   ORDER BY c_Name"
        composed = compose_refinement_sql(self._PREV, outer, cap=1000)
        assert composed.endswith(outer)


class TestVerifyComposition:
    _PREV = "SELECT TOP 10 Name FROM Customer"
    _OUTER = "SELECT TOP 3 c_Name FROM _prev"

    def _parts(self):
        from session.composer import _single_query, build_capped_predicate
        predicate = build_capped_predicate(self._PREV, 1000)
        return predicate, _single_query(self._OUTER, "tsql")

    def test_genuine_composition_passes(self):
        from session.composer import _verify_composition
        predicate, outer = self._parts()
        composed = f"WITH _prev AS (\n    {predicate}\n)\n{self._OUTER}"
        _verify_composition(composed, predicate, outer)  # must not raise

    def test_second_cte_is_refused(self):
        from session.composer import _verify_composition
        predicate, outer = self._parts()
        composed = f"WITH _prev AS ({predicate}), x AS (SELECT 1 AS a) {self._OUTER}"
        with pytest.raises(CompositionError):
            _verify_composition(composed, predicate, outer)

    def test_differently_named_cte_is_refused(self):
        from session.composer import _verify_composition
        predicate, outer = self._parts()
        composed = f"WITH other AS ({predicate}) {self._OUTER}"
        with pytest.raises(CompositionError):
            _verify_composition(composed, predicate, outer)

    def test_different_predicate_body_is_refused(self):
        from session.composer import _verify_composition
        predicate, outer = self._parts()
        composed = f"WITH _prev AS (SELECT TOP 5 ID FROM Customer) {self._OUTER}"
        with pytest.raises(CompositionError):
            _verify_composition(composed, predicate, outer)

    def test_different_outer_query_is_refused(self):
        from session.composer import _verify_composition
        predicate, outer = self._parts()
        composed = f"WITH _prev AS ({predicate}) SELECT TOP 9 c_ID FROM _prev"
        with pytest.raises(CompositionError):
            _verify_composition(composed, predicate, outer)


class TestTruncationProbeSql:
    def test_tsql_exact_text(self):
        from session.composer import strip_display_cap, truncation_probe_sql
        prev = "SELECT TOP 100 Name FROM Customer"
        expected = (
            "SELECT COUNT(*) AS Cnt FROM (SELECT TOP 4 1 AS _x FROM "
            f"({strip_display_cap(prev)}) AS _base) AS _check"
        )
        assert truncation_probe_sql(prev, 3) == expected

    @pytest.mark.parametrize(
        ("dialect", "prev"),
        [
            ("postgres", _PG_PREV),
            ("sqlite", 'SELECT c."Name" FROM "Customer" AS c LIMIT 5'),
        ],
    )
    def test_non_tsql_dialect_uses_limit(self, dialect: str, prev: str):
        import sqlglot
        from session.composer import truncation_probe_sql
        probe = truncation_probe_sql(prev, 3, dialect=dialect)
        assert "LIMIT 4" in probe
        assert "TOP" not in probe
        assert len(sqlglot.parse(probe, read=dialect)) == 1


class TestCheckScanTruncatedProbe:
    _PREV = "SELECT TOP 100 Name FROM Customer"

    def test_passes_exactly_the_probe_to_execute_fn(self):
        from session.composer import truncation_probe_sql
        seen: list[str] = []

        def execute_fn(sql: str) -> pd.DataFrame:
            seen.append(sql)
            return pd.DataFrame({"Cnt": [1]})

        check_scan_truncated(execute_fn, self._PREV, cap=5)
        assert seen == [truncation_probe_sql(self._PREV, 5)]

    def test_postgres_probe_is_passed_through(self):
        from session.composer import truncation_probe_sql
        seen: list[str] = []

        def execute_fn(sql: str) -> pd.DataFrame:
            seen.append(sql)
            return pd.DataFrame({"Cnt": [6]})

        assert check_scan_truncated(execute_fn, _PG_PREV, cap=5, dialect="postgres") is True
        assert seen == [truncation_probe_sql(_PG_PREV, 5, dialect="postgres")]

    def test_false_when_count_equals_cap(self):
        def execute_fn(sql: str) -> pd.DataFrame:
            return pd.DataFrame({"Cnt": [5]})

        assert check_scan_truncated(execute_fn, self._PREV, cap=5) is False

    def test_false_on_none(self):
        def execute_fn(sql: str) -> None:
            return None

        assert check_scan_truncated(execute_fn, self._PREV, cap=5) is False  # type: ignore[arg-type]

    def test_execute_fn_exception_propagates(self):
        def execute_fn(sql: str) -> pd.DataFrame:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            check_scan_truncated(execute_fn, self._PREV, cap=5)
