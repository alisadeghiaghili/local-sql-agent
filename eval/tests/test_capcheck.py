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

    def test_a_reference_cut_by_max_rows_only_needs_the_count_to_reach_it(self):
        with cfg.override_settings(max_rows_returned=5):
            ok = verify_capped_match(_capped(expected_rows=5), MODEL_SQL, _counts(40))
            short = verify_capped_match(_capped(expected_rows=5), MODEL_SQL, _counts(4))
        assert ok.mismatch_class == "match_capped" and ok.uncapped_rows == 40
        assert short.mismatch_class == "actual_is_subset"

    def test_only_counts_are_serialised(self):
        text = str(verify_capped_match(_capped(), MODEL_SQL, _counts(5)).to_dict())
        assert "uncapped_rows': 5" in text and "capped_unverified': False" in text
