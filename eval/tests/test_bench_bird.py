# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The BIRD benchmark importer on a synthetic, BIRD-shaped package.

A SQLite target with ``ATTACH``ed schemas stands in for SQL Server (as the
inspector's tests do); the SQL Server DDL is checked by compiling it with the
mssql dialect. No real benchmark data is involved.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_bench_bird.py -q
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql
from sqlalchemy.schema import CreateTable

from eval.benchmarks.bird import (
    BENCH_TAG,
    EVIDENCE_MARKER,
    BirdQuestion,
    Decision,
    golden_case,
    load_questions,
    locate_source,
    read_descriptions,
    run_reference,
    transpile_reference,
)
from eval.benchmarks.bird_import import ImportOptions, LoadMismatch, run_import
from eval.benchmarks.sqlite_load import (
    ColumnObservation,
    SchemaNotEmpty,
    column_kind,
    load_sqlite_schema,
    open_sqlite_readonly,
    post_load_statements,
    reflect_source,
    target_table,
    verify_loaded,
)
from eval.models import GoldenCase
from eval.runner import run_case
from eval.store import load_golden_cases
from tests._inspector_fixtures import attached

from ._bench_fixtures import QUESTIONS, build_package, build_target


@pytest.fixture
def package(tmp_path: Path) -> Path:
    return build_package(tmp_path / "download")


@pytest.fixture
def target(tmp_path: Path):
    database = build_target(tmp_path)
    with attached(database):
        engine = sa.create_engine(database.url)
        try:
            yield engine, database
        finally:
            engine.dispose()


def _import(package: Path, out: Path, engine, **overrides):
    options = ImportOptions(source=package, out=out, skip_config=True, max_rows=100, **overrides)
    return run_import(options, engine, progress=lambda message: None)


class TestPackage:
    def test_the_nested_package_is_found(self, package):
        source = locate_source(package)
        assert source.questions_file.name == "mini_dev_sqlite.json"
        assert source.database_ids() == ["library", "shop"]
        assert source.sqlite_path("shop").name == "shop.sqlite"

    def test_a_directory_without_the_json_is_refused(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="no mini_dev_sqlite.json"):
            locate_source(tmp_path)

    def test_the_questions_parse_with_every_field(self, package):
        questions = load_questions(locate_source(package).questions_file)
        assert len(questions) == len(QUESTIONS) == 9
        second = questions[1]
        assert (second.question_id, second.db_id, second.difficulty) == ("1", "shop", "simple")
        assert second.evidence == "Paris refers to City = 'Paris'"
        assert questions[-1].evidence == ""  # null evidence

    @pytest.mark.parametrize(
        "mutation, message",
        [
            (lambda q: q.pop("SQL"), "lacks SQL"),
            (lambda q: q.update(question="  "), "lacks question"),
        ],
    )
    def test_an_entry_without_a_required_key_is_refused(self, tmp_path, mutation, message):
        entry = dict(QUESTIONS[0])
        mutation(entry)
        path = tmp_path / "q.json"
        path.write_text(json.dumps([entry]), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_questions(path)

    def test_a_repeated_question_id_is_refused(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text(json.dumps([QUESTIONS[0], QUESTIONS[0]]), encoding="utf-8")
        with pytest.raises(ValueError, match="appears twice"):
            load_questions(path)

    def test_the_description_csv_is_read(self, package):
        descriptions = read_descriptions(locate_source(package).description_dir("shop"))
        assert descriptions["customer"]["city"] == "city of residence (text). e.g. Paris"
        assert read_descriptions(locate_source(package).description_dir("library")) == {}


class TestTypeMappingAndDdl:
    def test_the_storage_class_of_the_data_decides_the_type(self, package):
        tables = {t.name: t for t in reflect_source(locate_source(package).sqlite_path("shop"))}
        kinds = {c.name: c.kind for c in tables["Customer"].columns}
        assert kinds == {"ID": "integer", "Name": "text", "City": "text", "Joined": "text"}
        order = {c.name: c.kind for c in tables["Order Item"].columns}
        assert order["Total"] == "float" and order["Free Qty (K-12)"] == "integer"
        # a column with no declared type that holds 5, 3, 'n/a' and 4.5 is text, never a number
        assert {c.name: c.kind for c in tables["Review"].columns}["Rating"] == "text"
        assert column_kind("integer", ColumnObservation(rows=1, has_int=True, has_text=True)) == "text"

    def test_a_nullable_column_stays_nullable_and_a_key_is_not_null_only_when_declared(self, package):
        tables = {t.name: t for t in reflect_source(locate_source(package).sqlite_path("shop"))}
        free = next(c for c in tables["Order Item"].columns if c.name == "Free Qty (K-12)")
        assert free.nullable is True and free.observed.nulls == 1

    def test_create_table_for_sql_server_keeps_and_quotes_the_names(self, package):
        tables = {t.name: t for t in reflect_source(locate_source(package).sqlite_path("shop"))}
        ddl = str(CreateTable(target_table(tables["Order Item"], "shop")).compile(dialect=mssql.dialect()))
        assert "CREATE TABLE [shop].[Order Item]" in ddl
        assert "[Free Qty (K-12)] BIGINT NULL" in ddl
        assert "[Total] FLOAT(53) NULL" in ddl
        ddl = str(CreateTable(target_table(tables["Customer"], "shop")).compile(dialect=mssql.dialect()))
        assert "[Name] NVARCHAR(16) NULL" in ddl
        assert "[Joined] NVARCHAR(16) NULL" in ddl  # a date stays exactly the text it was
        assert "PRIMARY KEY" not in ddl  # keys come after the data

    def test_keys_are_added_after_the_data_and_only_for_sql_server(self, package):
        tables = reflect_source(locate_source(package).sqlite_path("shop"))
        statements = [ddl for _what, ddl in post_load_statements(tables, "shop", mssql.dialect())]
        assert "ALTER TABLE [shop].[Customer] ADD CONSTRAINT [PK_Customer] PRIMARY KEY ([ID])" in statements
        assert any(
            s.startswith("ALTER TABLE [shop].[Order Item] WITH NOCHECK ADD CONSTRAINT")
            and "FOREIGN KEY ([CustomerID]) REFERENCES [shop].[Customer] ([ID])" in s
            for s in statements
        )
        pks = [i for i, s in enumerate(statements) if "PRIMARY KEY" in s]
        fks = [i for i, s in enumerate(statements) if "FOREIGN KEY" in s]
        assert max(pks) < min(fks)
        assert post_load_statements(tables, "shop", sa.create_engine("sqlite://").dialect) == []

    def test_a_key_that_sql_server_cannot_hold_is_not_attempted(self):
        long_text = ColumnObservation(rows=1, has_text=True, max_text_len=2000)
        from eval.benchmarks.sqlite_load import SourceColumn, SourceTable

        table = SourceTable("Wide", (SourceColumn("Code", "TEXT", False, "text", long_text, 1),), rows=1)
        assert post_load_statements([table], "s", mssql.dialect()) == []

    def test_the_source_is_opened_read_only(self, package):
        path = locate_source(package).sqlite_path("library")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            open_sqlite_readonly(path).execute("DELETE FROM Book")


class TestLoad:
    def test_row_counts_are_equal_on_the_target(self, package, target):
        engine, _ = target
        report = load_sqlite_schema(locate_source(package).sqlite_path("shop"), engine, "shop")
        counts = {t.name: (t.source_rows, t.target_rows) for t in report.tables}
        assert counts == {"Customer": (3, 3), "Order Item": (3, 3), "Review": (4, 4)}
        assert report.ok
        with engine.connect() as conn:
            rows = conn.exec_driver_sql('SELECT "Name" FROM "shop"."Customer" ORDER BY "ID"').fetchall()
        assert [r[0] for r in rows] == ["Ada", "Bo", "Cy"]

    def test_a_non_empty_schema_is_refused_unless_replace_is_given(self, package, target):
        engine, _ = target
        source = locate_source(package).sqlite_path("library")
        load_sqlite_schema(source, engine, "library")
        with pytest.raises(SchemaNotEmpty, match="--replace"):
            load_sqlite_schema(source, engine, "library")
        assert load_sqlite_schema(source, engine, "library", replace=True).ok
        with engine.connect() as conn:
            assert conn.exec_driver_sql('SELECT COUNT(*) FROM "library"."Book"').scalar() == 3

    def test_verify_loaded_notices_a_missing_row(self, package, target):
        engine, _ = target
        source = locate_source(package).sqlite_path("library")
        load_sqlite_schema(source, engine, "library")
        assert verify_loaded(source, engine, "library").ok
        with engine.begin() as conn:
            conn.exec_driver_sql('DELETE FROM "library"."Book" WHERE "ID" = 1')
        assert [t.name for t in verify_loaded(source, engine, "library").mismatched] == ["Book"]


class TestReferenceAndTranspile:
    def test_the_reference_runs_on_the_original_file(self, package):
        conn = open_sqlite_readonly(locate_source(package).sqlite_path("shop"))
        assert run_reference(conn, "SELECT COUNT(*) FROM Customer", max_rows=10).frame.iloc[0, 0] == 3
        assert run_reference(conn, "SELECT Rank FROM Customer", max_rows=10).outcome == "failed"
        assert run_reference(conn, "SELECT Name FROM Customer WHERE City = 'x'", max_rows=10).outcome == "empty"
        assert run_reference(conn, "SELECT Name FROM Customer", max_rows=3).outcome == "too_large"
        conn.close()

    def test_a_slow_reference_is_stopped(self, package):
        conn = open_sqlite_readonly(locate_source(package).sqlite_path("shop"))
        slow = (
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) "
            "SELECT COUNT(*) FROM n"
        )
        result = run_reference(conn, slow, max_rows=10, timeout=0.2)
        assert result.outcome == "failed" and "stopped after" in result.detail
        conn.close()

    def test_every_table_is_qualified_by_the_schema_and_ctes_are_not(self):
        sql = transpile_reference("SELECT a FROM t JOIN u ON t.k = u.k", "db1", "tsql")
        assert "[db1].[t]" in sql and "[db1].[u]" in sql
        cte = transpile_reference("WITH c AS (SELECT 1 AS n) SELECT n FROM c", "db1", "tsql")
        assert "[db1].[c]" not in cte and "FROM [c]" in cte

    def test_limit_becomes_top_for_sql_server(self):
        assert transpile_reference("SELECT a FROM t ORDER BY a LIMIT 2", "d", "tsql").startswith("SELECT TOP 2 ")


class TestImport:
    def test_scored_and_excluded_cases_with_their_reasons(self, package, target, tmp_path):
        engine, _ = target
        report = _import(package, tmp_path / "out", engine)
        cases = {c.id: c for c in load_golden_cases(tmp_path / "out" / "golden.jsonl")}
        assert len(cases) == 9
        assert {i: c.status for i, c in cases.items() if c.status == "excluded"} == {
            "bird_3": "excluded", "bird_4": "excluded", "bird_5": "excluded",
        }
        assert cases["bird_3"].exclusion_reason == "reference_failed"
        assert cases["bird_4"].exclusion_reason == "reference_empty"
        assert cases["bird_5"].exclusion_reason == "load_fidelity"
        assert all(c.is_runnable for i, c in cases.items() if i not in ("bird_3", "bird_4", "bird_5"))
        assert report.scored == 6 and report.excluded == 3
        assert report.excluded_by_reason() == {
            "load_fidelity": 1, "reference_empty": 1, "reference_failed": 1,
        }

    def test_the_golden_format(self, package, target, tmp_path):
        engine, _ = target
        _import(package, tmp_path / "out", engine)
        first = json.loads((tmp_path / "out" / "golden.jsonl").read_text(encoding="utf-8").splitlines()[1])
        assert first["id"] == "bird_1"
        assert first["tags"] == [BENCH_TAG, "db:shop", "difficulty:simple"]
        assert first["match"] == "execution" and first["status"] == "active"
        assert first["expected_rows"] == [{"Name": "Ada"}, {"Name": "Cy"}]
        assert first["expected_fingerprint"]
        assert first["question"] == "List the names of customers in Paris."  # no evidence by default
        assert "SQLite SQL: SELECT Name FROM Customer WHERE City = 'Paris'" in first["notes"]
        assert '"shop"."Customer"' in first["expected_sql"]
        assert (tmp_path / "out" / "shop" / "golden.jsonl").is_file()
        assert (tmp_path / "out" / "library" / "golden.jsonl").is_file()
        assert not (tmp_path / "out" / "golden.with_evidence.jsonl").exists()

    def test_per_database_files_hold_only_that_database(self, package, target, tmp_path):
        engine, _ = target
        _import(package, tmp_path / "out", engine)
        library = load_golden_cases(tmp_path / "out" / "library" / "golden.jsonl")
        assert [c.id for c in library] == ["bird_6", "bird_7", "bird_8"]
        assert all("db:library" in c.tags for c in library)

    def test_the_reference_result_comes_from_sqlite_not_from_the_copy(self, package, target, tmp_path):
        engine, _ = target
        _import(package, tmp_path / "out", engine)
        excluded = next(c for c in load_golden_cases(tmp_path / "out" / "golden.jsonl") if c.id == "bird_5")
        # the original holds 5 and 3 as integers; the copy holds them as text, so it would say 0
        with engine.connect() as conn:
            assert conn.exec_driver_sql(excluded.expected_sql).scalar() == 0
        scored = next(c for c in load_golden_cases(tmp_path / "out" / "golden.jsonl") if c.id == "bird_2")
        assert scored.expected_rows == [{"SUM(Total)": 21.75}]

    def test_the_evidence_variant(self, package, target, tmp_path):
        engine, _ = target
        _import(package, tmp_path / "out", engine, with_evidence=True)
        plain = {c.id: c for c in load_golden_cases(tmp_path / "out" / "golden.jsonl")}
        hinted = {c.id: c for c in load_golden_cases(tmp_path / "out" / "golden.with_evidence.jsonl")}
        assert plain.keys() == hinted.keys()
        assert hinted["bird_1"].question == (
            f"List the names of customers in Paris.\n\n{EVIDENCE_MARKER}\nParis refers to City = 'Paris'"
        )
        assert hinted["bird_0"].question == plain["bird_0"].question  # no evidence: unchanged
        assert hinted["bird_8"].question == plain["bird_8"].question  # null evidence: unchanged
        assert hinted["bird_1"].expected_rows == plain["bird_1"].expected_rows
        assert hinted["bird_1"].tags == plain["bird_1"].tags
        assert [c.id for c in load_golden_cases(tmp_path / "out" / "shop" / "golden.with_evidence.jsonl")][0] == "bird_0"

    def test_a_second_import_is_refused_before_anything_is_written(self, package, target, tmp_path):
        engine, _ = target
        _import(package, tmp_path / "out", engine)
        before = (tmp_path / "out" / "golden.jsonl").read_bytes()
        with pytest.raises(SchemaNotEmpty, match="--replace"):
            _import(package, tmp_path / "out", engine)
        assert (tmp_path / "out" / "golden.jsonl").read_bytes() == before
        assert _import(package, tmp_path / "out", engine, replace=True).scored == 6

    def test_skip_load_reuses_the_loaded_schemas_and_checks_their_counts(self, package, target, tmp_path):
        engine, _ = target
        _import(package, tmp_path / "out", engine)
        assert _import(package, tmp_path / "again", engine, skip_load=True).scored == 6
        with engine.begin() as conn:
            conn.exec_driver_sql('DELETE FROM "shop"."Customer" WHERE "ID" = 3')
        with pytest.raises(LoadMismatch, match="shop.Customer"):
            _import(package, tmp_path / "third", engine, skip_load=True)
        assert not (tmp_path / "third").exists()

    def test_dbs_selects_databases(self, package, target, tmp_path):
        engine, _ = target
        report = _import(package, tmp_path / "out", engine, dbs=("library",))
        assert list(report.loads) == ["library"]
        assert {c.id for c in load_golden_cases(tmp_path / "out" / "golden.jsonl")} == {"bird_6", "bird_7", "bird_8"}
        with pytest.raises(ValueError, match="no question is about: nope"):
            _import(package, tmp_path / "x", engine, dbs=("nope",), replace=True)

    def test_unverified_questions_are_excluded_unless_kept(self, package, target, tmp_path, monkeypatch):
        engine, _ = target
        import eval.benchmarks.bird as bird

        def broken(sql, schema, dialect):
            raise ValueError("cannot be written")

        monkeypatch.setattr(bird, "transpile_reference", broken)
        report = _import(package, tmp_path / "out", engine)
        assert report.excluded_by_reason()["fidelity_unverified"] == 7  # all that ran on SQLite
        kept = _import(package, tmp_path / "kept", engine, replace=True, keep_unverified=True)
        assert kept.scored == 7 and "fidelity_unverified" not in kept.excluded_by_reason()

    def test_the_import_report_lists_every_excluded_case(self, package, target, tmp_path):
        engine, _ = target
        _import(package, tmp_path / "out", engine)
        data = json.loads((tmp_path / "out" / "import_report.json").read_text(encoding="utf-8"))
        assert data["totals"]["excluded_by_reason"] == {
            "load_fidelity": 1, "reference_empty": 1, "reference_failed": 1,
        }
        listed = {
            item["case_id"]: reason
            for db in data["databases"].values()
            for reason, items in db["excluded"].items() for item in items
        }
        assert listed == {"bird_3": "reference_failed", "bird_4": "reference_empty", "bird_5": "load_fidelity"}
        assert data["databases"]["shop"]["load"]["tables"]["Customer"] == {
            "source_rows": 3, "target_rows": 3, "columns": 4, "match": True,
        }
        text = (tmp_path / "out" / "import_report.txt").read_text(encoding="utf-8")
        assert "NOT this deployment's accuracy" in text and "CC BY-SA 4.0" in text
        assert "bird_5" in text and "load_fidelity" in text
        assert "CC BY-SA" in (tmp_path / "out" / "NOTICE.txt").read_text(encoding="utf-8")

    def test_nothing_of_the_benchmark_is_written_inside_the_package(self, package, target, tmp_path):
        engine, _ = target
        before = sorted(p.relative_to(package) for p in package.rglob("*"))
        _import(package, tmp_path / "out", engine)
        assert sorted(p.relative_to(package) for p in package.rglob("*")) == before


class TestStoredReference:
    """``run --reference stored`` compares with the SQLite result, not with a fingerprint."""

    def _case(self, package, target, tmp_path, case_id):
        engine, _ = target
        _import(package, tmp_path / "out", engine)
        return next(c for c in load_golden_cases(tmp_path / "out" / "golden.jsonl") if c.id == case_id)

    def test_aliases_and_row_order_do_not_matter_a_wrong_value_does(self, package, target, tmp_path):
        case = self._case(package, target, tmp_path, "bird_7")  # authors and their book counts
        sql = "SELECT COUNT(*) AS n FROM Customer"  # any SQL the example guard accepts
        right = pd.DataFrame({"who": [2, 1], "books": [1, 2]})
        wrong = pd.DataFrame({"who": [2, 1], "books": [1, 3]})
        assert run_case(case, lambda q: sql, lambda s: right).status == "pass"
        mismatch = run_case(case, lambda q: sql, lambda s: wrong)
        assert mismatch.status == "fingerprint_mismatch" and "recorded reference rows" in mismatch.error

    def test_an_excluded_case_is_not_run(self, package, target, tmp_path):
        case = self._case(package, target, tmp_path, "bird_5")
        assert case.status == "excluded" and not case.is_runnable

    def test_the_ordering_of_the_reference_is_kept(self, package, target, tmp_path):
        case = self._case(package, target, tmp_path, "bird_6")  # longest book: ORDER BY ... LIMIT 1
        sql = "SELECT TOP 1 Name FROM Customer ORDER BY Name"
        assert run_case(case, lambda q: sql, lambda s: pd.DataFrame({"x": ["Fay"]})).status == "pass"
        assert run_case(case, lambda q: sql, lambda s: pd.DataFrame({"x": ["Eve"]})).status != "pass"


class TestGoldenCaseUnit:
    def test_an_excluded_case_keeps_its_reason_and_detail_in_the_notes(self):
        question = BirdQuestion("9", "db", "q?", "", "SELECT 1", "simple")
        case = golden_case(question, Decision("reference_failed", "OperationalError: x", "SELECT 1"))
        assert isinstance(case, GoldenCase)
        assert case.exclusion_reason == "reference_failed" and "OperationalError: x" in case.notes
