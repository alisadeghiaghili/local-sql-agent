# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""How the benchmark importer shows the SQL Server copy to be faithful.

* every table is compared with its SQLite source **by content** (a changed value, a
  changed case, a lost trailing space), and only the questions that read a table that
  differs are excluded;
* the target database must be case- and accent-sensitive (the collation check, with a
  stub for the property query);
* the per-query transpiled check is a diagnostic: a difference on faithful data is
  recorded as ``semantic_difference`` and the case stays in, and a reference that cannot
  be checked is ``fidelity_unverified`` and never removes a case;
* a benchmark report gives the accuracy on all scored cases and without the
  semantic-difference cases.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_bench_fidelity.py -q
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from eval.benchmarks import bird
from eval.benchmarks.bird import tables_referenced
from eval.benchmarks.bird_import import CollationError, ImportOptions, render_import_report, run_import
from eval.benchmarks.bird_run import merge_reports, write_merged
from eval.benchmarks.sqlite_load import (
    ColumnObservation,
    SourceColumn,
    collation_problem,
    compare_contents,
    digest_rows,
    load_sqlite_schema,
    verify_loaded,
)
from eval.models import CaseResult
from eval.report import build_report, render_text, save_json_report
from eval.store import load_golden_cases
from tests._inspector_fixtures import attached

from ._bench_fixtures import build_package, build_target


@pytest.fixture
def package(tmp_path: Path) -> Path:
    return build_package(tmp_path / "download")


@pytest.fixture
def target(tmp_path: Path):
    database = build_target(tmp_path)
    with attached(database):
        engine = sa.create_engine(database.url)
        try:
            yield engine
        finally:
            engine.dispose()


def _import(package: Path, out: Path, engine, **overrides):
    options = ImportOptions(source=package, out=out, skip_config=True, max_rows=100, **overrides)
    return run_import(options, engine, progress=lambda message: None)


def _cases(out: Path) -> dict:
    return {c.id: c for c in load_golden_cases(out / "golden.jsonl")}


def _tamper_source(package: Path, sql: str) -> None:
    connection = sqlite3.connect(package / "MINIDEV" / "dev_databases" / "shop" / "shop.sqlite")
    connection.execute(sql)
    connection.commit()
    connection.close()


COLUMNS = (
    SourceColumn("a", "INTEGER", True, "integer", ColumnObservation()),
    SourceColumn("b", "REAL", True, "float", ColumnObservation()),
    SourceColumn("c", "TEXT", True, "text", ColumnObservation()),
)


class TestRowDigest:
    def test_the_order_of_the_rows_does_not_matter_duplicates_do(self):
        rows = [(1, 1.5, "x"), (2, 2.5, "y")]
        assert digest_rows(rows, COLUMNS) == digest_rows(rows[::-1], COLUMNS)
        assert digest_rows(rows + rows[:1], COLUMNS) != digest_rows(rows, COLUMNS)

    @pytest.mark.parametrize(
        "changed",
        [(1, 1.5, "X"), (1, 1.5, "x "), (1, 1.5, " x"), (1, 1.5001, "x"), (2, 1.5, "x"), (1, 1.5, None)],
    )
    def test_a_value_a_case_or_a_space_changes_the_digest(self, changed):
        assert digest_rows([changed], COLUMNS) != digest_rows([(1, 1.5, "x")], COLUMNS)

    def test_floats_agree_within_twelve_significant_digits_integers_exactly(self):
        assert digest_rows([(1, 0.1 + 0.2, "x")], COLUMNS) == digest_rows([(1, 0.3, "x")], COLUMNS)
        assert digest_rows([(1, 1e-300 * 0.0, "x")], COLUMNS) == digest_rows([(1, -0.0, "x")], COLUMNS)
        assert digest_rows([(10**15 + 1, 1.0, "x")], COLUMNS) != digest_rows([(10**15, 1.0, "x")], COLUMNS)

    def test_blobs_are_compared_by_their_hash(self):
        blobs = (SourceColumn("b", "BLOB", True, "blob", ColumnObservation()),)
        assert digest_rows([(b"\x00\x01",)], blobs) == digest_rows([(b"\x00\x01",)], blobs)
        assert digest_rows([(b"\x00\x01",)], blobs) != digest_rows([(b"\x00\x02",)], blobs)

    def test_per_column_digests_name_the_column(self):
        a = digest_rows([(1, 1.5, "x")], COLUMNS, per_column=True)
        b = digest_rows([(1, 1.5, "y")], COLUMNS, per_column=True)
        assert [x != y for x, y in zip(a.columns, b.columns)] == [False, False, True]


class TestTableContent:
    def test_an_untouched_copy_is_identical(self, package, target):
        source = package / "MINIDEV" / "dev_databases" / "shop" / "shop.sqlite"
        load_sqlite_schema(source, target, "shop")
        report = compare_contents(source, target, "shop")
        assert report.faithful and len(report.tables) == 3

    @pytest.mark.parametrize(
        "source_sql, tamper, column",
        [
            (None, "UPDATE \"shop\".\"Customer\" SET \"Name\" = 'Zed' WHERE \"ID\" = 2", "Name"),
            (None, "UPDATE \"shop\".\"Customer\" SET \"City\" = 'PARIS' WHERE \"ID\" = 1", "City"),
            ("UPDATE Customer SET Name = 'Cy ' WHERE ID = 3",
             "UPDATE \"shop\".\"Customer\" SET \"Name\" = 'Cy' WHERE \"ID\" = 3", "Name"),
        ],
        ids=["changed value", "changed case", "lost trailing space"],
    )
    def test_a_change_that_keeps_the_row_count_is_caught(self, package, target, source_sql, tamper, column):
        if source_sql:
            _tamper_source(package, source_sql)
        source = package / "MINIDEV" / "dev_databases" / "shop" / "shop.sqlite"
        load_sqlite_schema(source, target, "shop")
        with target.begin() as conn:
            conn.exec_driver_sql(tamper)
        assert verify_loaded(source, target, "shop").ok  # the row counts alone see nothing
        report = compare_contents(source, target, "shop")
        assert report.unfaithful == {"Customer": f"column(s) differ: {column}"}

    def test_a_table_that_is_missing_or_short_is_reported(self, package, target):
        source = package / "MINIDEV" / "dev_databases" / "shop" / "shop.sqlite"
        load_sqlite_schema(source, target, "shop")
        with target.begin() as conn:
            conn.exec_driver_sql('DELETE FROM "shop"."Review" WHERE "ID" = 1')
        assert compare_contents(source, target, "shop").unfaithful == {
            "Review": "4 rows in the source, 3 on the target"
        }


class TestOnlyTheTouchedCasesAreExcluded:
    def _tampered(self, package, target, tmp_path, **overrides):
        _import(package, tmp_path / "first", target)
        with target.begin() as conn:
            conn.exec_driver_sql('UPDATE "shop"."Customer" SET "Name" = \'Zed\' WHERE "ID" = 2')
        return _import(package, tmp_path / "second", target, skip_load=True, **overrides)

    def test_cases_that_read_the_table_that_differs_are_excluded_as_load_fidelity(self, package, target, tmp_path):
        report = self._tampered(package, target, tmp_path)
        cases = _cases(tmp_path / "second")
        assert {i for i, c in cases.items() if c.exclusion_reason == "load_fidelity"} == {"bird_0", "bird_1"}
        # the others are not touched by it: another table, another database, or their own reason
        assert cases["bird_2"].is_runnable and cases["bird_5"].is_runnable
        assert all(cases[i].is_runnable for i in ("bird_6", "bird_7", "bird_8"))
        assert cases["bird_3"].exclusion_reason == "reference_failed"
        assert cases["bird_4"].exclusion_reason == "reference_empty"
        assert report.excluded_by_reason() == {
            "load_fidelity": 2, "reference_empty": 1, "reference_failed": 1,
        }
        assert report.contents["shop"].unfaithful == {"Customer": "column(s) differ: Name"}
        assert report.contents["library"].faithful

    def test_the_report_names_the_table_and_the_cases(self, package, target, tmp_path):
        report = self._tampered(package, target, tmp_path)
        text = render_import_report(report)
        assert "1 DIFFER" in text and "Customer: column(s) differ: Name" in text
        data = json.loads((tmp_path / "second" / "import_report.json").read_text(encoding="utf-8"))
        assert data["databases"]["shop"]["content"]["unfaithful"] == {"Customer": "column(s) differ: Name"}
        detail = data["databases"]["shop"]["excluded"]["load_fidelity"][0]["detail"]
        assert "Customer" in detail and "SQLite source" in detail

    def test_a_faithful_copy_excludes_nothing_for_the_load(self, package, target, tmp_path):
        report = _import(package, tmp_path / "out", target)
        assert "load_fidelity" not in report.excluded_by_reason()
        assert all(c.faithful for c in report.contents.values())

    def test_the_tables_a_query_reads_are_found_through_joins_and_ctes(self):
        known = ["Customer", "Order Item", "Review"]
        assert tables_referenced('SELECT 1 FROM "Order Item" AS o JOIN customer c ON c.ID = o.CustomerID', known) == [
            "Customer", "Order Item",
        ]
        assert tables_referenced("WITH review AS (SELECT 1) SELECT * FROM review", known) == []
        assert tables_referenced("SELECT ((", known) == known  # an unparsable query reads everything


class TestCollation:
    CI = "SQL_Latin1_General_CP1_CI_AS"
    CS = "Latin1_General_100_CS_AS_SC_UTF8"

    def _run(self, package, out, engine, collation, **overrides):
        options = ImportOptions(source=package, out=out, skip_config=True, max_rows=100, **overrides)
        return run_import(options, engine, progress=lambda message: None, collation=lambda e: collation)

    @pytest.mark.parametrize("name", ["Latin1_General_100_CS_AS_SC_UTF8", "Latin1_General_BIN2", "Latin1_General_CS_AS"])
    def test_case_and_accent_sensitive_names_pass(self, name):
        assert collation_problem(name) is None

    @pytest.mark.parametrize(
        "name, problem",
        [
            ("SQL_Latin1_General_CP1_CI_AS", "case-insensitive"),
            ("Latin1_General_100_CI_AS", "case-insensitive"),
            ("Latin1_General_CS_AI", "accent-insensitive"),
            ("Latin1_General_100", "not recognisably"),
        ],
    )
    def test_the_other_names_are_a_problem(self, name, problem):
        assert problem in collation_problem(name)

    def test_a_case_insensitive_target_is_refused_before_anything_is_written(self, package, target, tmp_path):
        with pytest.raises(CollationError, match="case-insensitive") as refused:
            self._run(package, tmp_path / "out", target, self.CI)
        assert "COLLATE Latin1_General_100_CS_AS_SC_UTF8" in str(refused.value)
        assert "--allow-ci-collation" in str(refused.value)
        assert not (tmp_path / "out").exists()
        assert sa.inspect(target).get_table_names(schema="shop") == []

    def test_a_sensitive_target_passes_and_is_named_in_the_report(self, package, target, tmp_path):
        report = self._run(package, tmp_path / "out", target, self.CS)
        assert report.collation == {"name": self.CS, "checked": True, "problem": None, "allowed": False}
        assert f"Target collation: {self.CS}" in render_import_report(report)

    def test_allow_imports_anyway_and_flags_it_prominently(self, package, target, tmp_path):
        report = self._run(package, tmp_path / "out", target, self.CI, allow_ci_collation=True)
        assert report.collation["allowed"] is True and report.scored == 7
        text = (tmp_path / "out" / "import_report.txt").read_text(encoding="utf-8")
        assert "*** WARNING: the target database's collation SQL_Latin1_General_CP1_CI_AS" in text
        data = json.loads((tmp_path / "out" / "import_report.json").read_text(encoding="utf-8"))
        assert data["collation"]["problem"] == "is case-insensitive (CI)"

    def test_the_warning_reaches_the_merged_report(self, package, target, tmp_path):
        report = self._run(package, tmp_path / "out", target, self.CI, allow_ci_collation=True)
        result = CaseResult(
            "bird_0", "q", ["bench:bird", "db:shop"], "pass", "SELECT 1", "f", None, 0.1,
        )
        path = tmp_path / "a.json"
        save_json_report(build_report([result], mode="live", reference="stored", pipeline="production"), path)
        merged = merge_reports([path], mode="per_db", evidence=False, import_report=report.to_dict())
        text = render_text(merged)
        assert text.splitlines()[1].startswith("WARNING: the benchmark database's collation")
        assert "lower bound" in text

    def test_a_target_whose_collation_cannot_be_asked_is_not_blocked(self, package, target, tmp_path):
        report = self._run(package, tmp_path / "out", target, None)
        assert report.collation["checked"] is False and report.scored == 7


class TestPerQueryDiagnostic:
    def test_a_semantic_difference_is_recorded_not_excluded(self, package, target, tmp_path):
        report = _import(package, tmp_path / "out", target)
        case = _cases(tmp_path / "out")["bird_5"]
        assert case.is_runnable and "fidelity:semantic_difference" in case.tags
        assert "although every table it reads is identical" in case.notes
        assert report.flagged_by_kind() == {"semantic_difference": 1}
        assert report.contents["shop"].faithful  # the data are faithful: the difference is the semantics
        assert "bird_5" in render_import_report(report)

    def test_the_flag_can_be_made_an_exclusion(self, package, target, tmp_path):
        report = _import(package, tmp_path / "out", target, exclude_semantic_difference=True)
        case = _cases(tmp_path / "out")["bird_5"]
        assert case.status == "excluded" and case.exclusion_reason == "semantic_difference"
        assert report.scored == 6 and report.flagged_by_kind() == {}

    def test_a_difference_on_a_table_that_differs_is_a_load_problem_not_a_semantic_one(self, package, target, tmp_path):
        _import(package, tmp_path / "first", target)
        with target.begin() as conn:
            conn.exec_driver_sql('UPDATE "shop"."Review" SET "Rating" = \'7\' WHERE "ID" = 1')
        _import(package, tmp_path / "second", target, skip_load=True, exclude_semantic_difference=True)
        assert _cases(tmp_path / "second")["bird_5"].exclusion_reason == "load_fidelity"

    def test_an_unverified_reference_never_excludes(self, package, target, tmp_path, monkeypatch):
        def cannot(sql, schema, dialect):
            raise ValueError("cannot be written")

        monkeypatch.setattr(bird, "transpile_reference", cannot)
        report = _import(package, tmp_path / "out", target)
        cases = _cases(tmp_path / "out")
        assert report.scored == 7 and report.excluded_by_reason() == {"reference_empty": 1, "reference_failed": 1}
        scored = [c for c in cases.values() if c.is_runnable]
        assert len(scored) == 7 and all("fidelity:unverified" in c.tags for c in scored)
        assert report.flagged_by_kind() == {"fidelity_unverified": 7}
        assert cases["bird_0"].expected_sql == "SELECT COUNT(*) FROM Customer"  # kept as the SQLite text

    def test_a_reference_that_fails_on_the_copy_is_unverified_too(self, package, target, tmp_path, monkeypatch):
        monkeypatch.setattr(bird, "check_fidelity", lambda *a, **k: ("error", "OperationalError: no such function"))
        report = _import(package, tmp_path / "out", target)
        assert report.scored == 7 and report.flagged_by_kind() == {"fidelity_unverified": 7}


class TestBothAccuraciesAreReported:
    def _results(self):
        tags = ["bench:bird", "db:shop"]
        return [
            CaseResult("bird_1", "q", tags, "pass", "S", "f", None, 0.1),
            CaseResult("bird_2", "q", tags, "pass", "S", "f", None, 0.1),
            CaseResult("bird_3", "q", tags + ["fidelity:semantic_difference"], "fingerprint_mismatch", "S", "f", "x", 0.1),
            CaseResult("bird_4", "q", tags + ["fidelity:unverified"], "pass", "S", "f", None, 0.1),
        ]

    def test_the_text_gives_scored_excluded_and_flagged_counts_and_both_figures(self, tmp_path):
        path = tmp_path / "a.json"
        save_json_report(build_report(self._results(), mode="live", reference="stored", pipeline="production"), path)
        merged = merge_reports(
            [path], mode="per_db", evidence=False,
            import_report={"totals": {"excluded_by_reason": {"load_fidelity": 2, "reference_failed": 1}}},
        )
        text = render_text(merged)
        assert (
            "Cases: scored 4; excluded 3 (load_fidelity 2, reference_failed 1); "
            "semantic-difference cases kept in 1; fidelity unverified (kept in) 1." in text
        )
        assert "Execution accuracy: 75.00% (3/4)" in text
        assert "Without the 1 semantic-difference case(s): 100.00% (3/3)" in text
        assert "fidelity:semantic_difference" in text  # its own stratum, with an interval

    def test_the_json_carries_both(self, tmp_path):
        report = build_report(self._results(), mode="live", reference="stored", pipeline="production")
        assert report.benchmark == {"name": "bird"}
        fidelity = report.to_dict()["fidelity"]
        assert fidelity["semantic_difference_cases"] == 1 and fidelity["unverified_cases"] == 1
        assert (fidelity["all"]["passed"], fidelity["all"]["n"]) == (3, 4)
        assert (fidelity["without_semantic_difference"]["passed"], fidelity["without_semantic_difference"]["n"]) == (3, 3)
        json_path, _ = write_merged(report, tmp_path / "merged")
        assert json.loads(json_path.read_text(encoding="utf-8"))["fidelity"]["all"]["pct"] == 75.0

    def test_a_deployments_own_report_has_no_fidelity_block_unless_it_uses_the_tag(self):
        plain = build_report([CaseResult("a", "q", ["count"], "pass", "S", "f", None, 0.1)], mode="live")
        assert plain.to_dict()["fidelity"] is None
        assert "semantic-difference" not in render_text(plain)
