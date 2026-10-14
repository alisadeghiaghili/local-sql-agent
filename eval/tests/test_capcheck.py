# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``eval.capcheck``: the count query that confirms a ``match_capped`` result.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_capcheck.py -q
"""

from __future__ import annotations

import pandas as pd
import pytest
import sqlglot

import config as cfg
from eval.capcheck import count_query, verify_capped_match
from eval.models import MismatchDiagnosis
from schema_data.columns import TABLE_COLUMNS

TABLE = next(iter(TABLE_COLUMNS))
MODEL_SQL = f"SELECT ID FROM [{TABLE}] WHERE ID > 1"


def _capped(expected_rows=5, cap=3) -> MismatchDiagnosis:
    return MismatchDiagnosis(
        "match_capped", expected_rows, cap, ["ID"], ["ID"], capped=True, row_cap=cap
    )


def _counts(value, seen=None):
    def run(sql):
        if seen is not None:
            seen.append(sql)
        return pd.DataFrame({"n": [value]})

    return run


class TestCountQuery:
    def test_an_order_by_without_a_row_limit_is_removed(self):
        text = count_query("SELECT ID FROM T WHERE a = 1 ORDER BY ID DESC")
        assert "ORDER BY" not in text and text.startswith("SELECT COUNT(*) FROM (")

    @pytest.mark.parametrize("sql", [
        "SELECT TOP 5 ID FROM T ORDER BY ID",
        "SELECT ID FROM T ORDER BY ID OFFSET 2 ROWS FETCH NEXT 3 ROWS ONLY",
    ])
    def test_an_order_by_that_a_row_limit_needs_is_kept(self, sql):
        assert "ORDER BY" in count_query(sql)

    def test_the_order_by_of_a_union_is_removed_from_its_last_branch(self):
        text = count_query("SELECT a FROM x UNION ALL SELECT a FROM y ORDER BY a")
        assert "ORDER BY" not in text and "UNION ALL" in text

    def test_a_with_clause_is_hoisted_out_of_the_derived_table(self):
        text = count_query("WITH c AS (SELECT 1 AS a) SELECT a FROM c ORDER BY a")
        assert text.startswith("WITH c AS (SELECT 1 AS a) SELECT COUNT(*) FROM (SELECT a")
        assert "ORDER BY" not in text
        assert sqlglot.parse_one(text, read="tsql")  # still one statement

    def test_unnamed_and_repeated_columns_get_names(self):
        text = count_query("SELECT COUNT(*), a.x, b.x FROM a JOIN b ON 1 = 1")
        names = [p.alias_or_name for p in sqlglot.parse_one(text, read="tsql")
                 .find(sqlglot.exp.Subquery).this.expressions]
        assert len(names) == len(set(names)) == 3

    def test_distinct_and_the_filter_are_untouched(self):
        text = count_query("SELECT DISTINCT a FROM T WHERE b = 'x' AND c > 3")
        assert "DISTINCT" in text and "b = 'x'" in text and "c > 3" in text

    def test_it_is_a_sqlite_runnable_count_too(self):
        # the shape is valid SQL, not just parseable: run it against a real engine
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE T (ID INTEGER, a INTEGER)")
        conn.executemany("INSERT INTO T VALUES (?, ?)", [(i, i % 2) for i in range(1, 8)])
        for sql, expected in [
            ("SELECT ID FROM T WHERE a = 1 ORDER BY ID", 4),
            ("SELECT TOP 3 ID FROM T ORDER BY ID", 3),
            ("SELECT COUNT(*), MAX(ID) FROM T", 1),
            ("SELECT DISTINCT a FROM T", 2),
        ]:
            native = sqlglot.transpile(count_query(sql), read="tsql", write="sqlite")[0]
            assert conn.execute(native).fetchone()[0] == expected

    @pytest.mark.parametrize("sql", ["SELECT ( FROM", "SELECT 1; SELECT 2", "DELETE FROM T", ""])
    def test_what_cannot_be_counted_raises(self, sql):
        with pytest.raises(ValueError, match="cannot build a count query"):
            count_query(sql)


class TestVerify:
    def test_a_count_equal_to_the_reference_confirms_the_match(self):
        seen: list[str] = []
        result = verify_capped_match(_capped(), MODEL_SQL, _counts(5, seen))
        assert (result.mismatch_class, result.capped, result.uncapped_rows) == ("match_capped", True, 5)
        assert result.capped_unverified is False
        assert len(seen) == 1 and seen[0].startswith("SELECT COUNT(*) FROM (")

    def test_a_smaller_count_demotes_it_to_a_subset(self):
        result = verify_capped_match(_capped(), MODEL_SQL, _counts(4))
        assert (result.mismatch_class, result.capped, result.uncapped_rows) == ("actual_is_subset", False, 4)
        assert result.capped_unverified is False

    def test_a_larger_count_is_not_the_reference_either(self):
        result = verify_capped_match(_capped(), MODEL_SQL, _counts(9))
        assert result.mismatch_class == "actual_is_subset" and result.uncapped_rows == 9

    @pytest.mark.parametrize("failure", [RuntimeError("timeout"), ValueError("x"), OSError("y")])
    def test_a_count_that_cannot_run_fails_unverified(self, failure):
        def broken(sql):
            raise failure

        result = verify_capped_match(_capped(), MODEL_SQL, broken)
        assert result.mismatch_class == "actual_is_subset"
        assert result.capped_unverified is True and result.capped is False
        assert result.uncapped_rows is None

    def test_an_unparseable_or_unrecorded_statement_fails_unverified(self):
        for sql in ("SELECT ( FROM", None, ""):
            result = verify_capped_match(_capped(), sql, _counts(5))
            assert result.mismatch_class == "actual_is_subset" and result.capped_unverified

    def test_an_empty_count_result_fails_unverified(self):
        result = verify_capped_match(_capped(), MODEL_SQL, lambda sql: pd.DataFrame())
        assert result.capped_unverified is True

    def test_a_count_the_guard_refuses_fails_unverified(self):
        result = verify_capped_match(_capped(), "SELECT ID FROM NoSuchTable", _counts(5))
        assert result.mismatch_class == "actual_is_subset" and result.capped_unverified

    def test_nothing_else_is_touched(self):
        other = MismatchDiagnosis("actual_is_subset", 5, 2, ["ID"], ["ID"])
        assert verify_capped_match(other, MODEL_SQL, _counts(1)) is other

    REFERENCE_SQL = f"SELECT ID FROM [{TABLE}]"

    @staticmethod
    def _by_query(model_count, reference_count, seen=None):
        """Answers the model's count and the reference's count differently (by the WHERE)."""

        def run(sql):
            if seen is not None:
                seen.append(sql)
            value = model_count if "ID > 1" in sql else reference_count
            if isinstance(value, Exception):
                raise value
            return pd.DataFrame({"n": [value]})

        return run

    def test_a_cut_reference_is_counted_too_and_equal_counts_pass(self):
        seen: list[str] = []
        with cfg.override_settings(max_rows_returned=5):
            result = verify_capped_match(
                _capped(expected_rows=5), MODEL_SQL, self._by_query(40, 40, seen),
                reference_sql=self.REFERENCE_SQL)
        assert result.mismatch_class == "match_capped" and result.capped is True
        assert (result.uncapped_rows, result.reference_uncapped_rows) == (40, 40)
        assert len(seen) == 2  # one count for each side

    def test_a_cut_reference_with_a_different_count_fails_even_when_the_model_count_is_larger(self):
        with cfg.override_settings(max_rows_returned=5):
            smaller = verify_capped_match(_capped(expected_rows=5), MODEL_SQL,
                                          self._by_query(30, 40), reference_sql=self.REFERENCE_SQL)
            larger = verify_capped_match(_capped(expected_rows=5), MODEL_SQL,
                                         self._by_query(50, 40), reference_sql=self.REFERENCE_SQL)
        for result, counts in ((smaller, (30, 40)), (larger, (50, 40))):
            assert result.mismatch_class == "actual_is_subset" and result.capped is False
            assert (result.uncapped_rows, result.reference_uncapped_rows) == counts
            assert result.capped_unverified is False

    def test_the_old_at_least_rule_is_gone(self):
        # 40 >= 5 used to pass; the reference's true size is 7, so 40 is not its answer
        with cfg.override_settings(max_rows_returned=5):
            result = verify_capped_match(_capped(expected_rows=5), MODEL_SQL,
                                         self._by_query(40, 7), reference_sql=self.REFERENCE_SQL)
        assert result.mismatch_class == "actual_is_subset"

    @pytest.mark.parametrize("reference_sql", [None, "", "SELECT ( FROM"])
    def test_a_cut_reference_that_cannot_be_counted_is_unverified_and_fails(self, reference_sql):
        with cfg.override_settings(max_rows_returned=5):
            result = verify_capped_match(_capped(expected_rows=5), MODEL_SQL,
                                         self._by_query(40, 40), reference_sql=reference_sql)
        assert result.mismatch_class == "actual_is_subset"
        assert result.capped_unverified is True and result.capped is False

    def test_a_cut_reference_whose_count_query_fails_is_unverified_and_fails(self):
        with cfg.override_settings(max_rows_returned=5):
            result = verify_capped_match(
                _capped(expected_rows=5), MODEL_SQL,
                self._by_query(40, RuntimeError("timeout")), reference_sql=self.REFERENCE_SQL)
        assert result.mismatch_class == "actual_is_subset"
        assert result.capped_unverified is True
        assert result.reference_uncapped_rows is None

    def test_an_uncut_reference_is_not_counted(self):
        seen: list[str] = []
        result = verify_capped_match(_capped(expected_rows=5), MODEL_SQL, _counts(5, seen),
                                     reference_sql=self.REFERENCE_SQL)
        assert result.mismatch_class == "match_capped" and result.reference_uncapped_rows is None
        assert len(seen) == 1

    def test_only_counts_are_serialised(self):
        text = str(verify_capped_match(_capped(), MODEL_SQL, _counts(5)).to_dict())
        assert "uncapped_rows': 5" in text and "capped_unverified': False" in text
        assert "reference_uncapped_rows': None" in text
