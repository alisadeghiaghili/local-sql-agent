# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for ``python -m eval.cli verify`` (eval/verify.py) and eval/store.py.

A fake executor stands in for the database; the golden file is a real file
in ``tmp_path`` so the atomic write-back, the ``.bak`` copy and the
``--accept`` flip are exercised end to end.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_verify.py -q
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

import config as cfg
from eval import cli, store
from eval.fingerprint import fingerprint_dataframe
from eval.models import GoldenCase
from eval.runner import load_golden_cases, make_offline_executor, make_offline_generator, run_golden_set
from eval.verify import frame_to_rows, render_verify_text, verify_cases
from schema_data.columns import TABLE_COLUMNS

_TABLE = next(iter(TABLE_COLUMNS))
SQL_A = f"SELECT COUNT(*) AS n FROM {_TABLE}"
SQL_B = f"SELECT TOP 2 * FROM {_TABLE}"
SECRET_QUESTION = "رمز-محرمانه question text"


def _case(case_id="a", sql=SQL_A, status="reviewed", **kw) -> GoldenCase:
    return GoldenCase(id=case_id, question=f"{SECRET_QUESTION} {case_id}", expected_sql=sql,
                      status=status, **kw)


def _executor(answers):
    calls = []

    def run(sql):
        calls.append(sql)
        answer = answers[sql]
        if isinstance(answer, Exception):
            raise answer
        return answer

    run.calls = calls
    return run


class TestFrameToRows:
    @pytest.mark.parametrize(
        "df",
        [
            pd.DataFrame({"n": [1, 2, 3]}),
            pd.DataFrame({"n": [1, None], "s": ["a", None]}),
            pd.DataFrame({"d": [Decimal("1.50"), Decimal("2.25")]}),
            pd.DataFrame({"t": [pd.Timestamp("2026-01-02 03:04:05"), pd.NaT]}),
            pd.DataFrame({"d": [date(2026, 1, 2)], "dt": [datetime(2026, 1, 2, 3, 4)]}),
            pd.DataFrame({"b": [True, False], "f": [0.1 + 0.2, 1e-9]}),
            pd.DataFrame({"fa": ["مشتری", "تامین‌کننده"]}),
            pd.DataFrame({"n": pd.Series([], dtype="int64")}),
        ],
    )
    def test_replayed_rows_have_the_same_fingerprint(self, df):
        rows = frame_to_rows(df)
        replay = pd.DataFrame(rows, columns=[str(c) for c in df.columns])
        assert fingerprint_dataframe(replay) == fingerprint_dataframe(df)

    def test_rows_are_json_serialisable(self):
        df = pd.DataFrame({"d": [Decimal("1.5")], "t": [pd.Timestamp("2026-01-02")], "n": [None]})
        json.dumps(frame_to_rows(df))


class TestVerifyCases:
    def test_reviewed_case_gets_rows_and_fingerprint_and_stays_reviewed_without_accept(self):
        df = pd.DataFrame({"n": [3]})
        result = verify_cases([_case()], _executor({SQL_A: df}))
        case = result.cases[0]
        assert case.status == "reviewed"
        assert case.expected_rows == [{"n": 3}]
        assert case.expected_fingerprint == fingerprint_dataframe(df)
        assert result.changed and not result.problems

    def test_accept_activates_only_cases_without_problems(self):
        ok, bad = _case("ok"), _case("bad", SQL_B)
        wh = _executor({SQL_A: pd.DataFrame({"n": [3]}), SQL_B: RuntimeError("Database error: nope")})
        result = verify_cases([ok, bad], wh, accept=True)
        assert [c.status for c in result.cases] == ["active", "reviewed"]
        assert [(o.case_id, o.problem) for o in result.problems] == [("bad", "database_error")]
        assert result.outcomes[0].accepted and not result.outcomes[1].accepted

    def test_guard_rejection_is_reported_and_nothing_is_executed(self):
        wh = _executor({})
        result = verify_cases([_case(sql="DROP TABLE x")], wh, accept=True)
        assert result.problems[0].problem == "guard_rejected"
        assert wh.calls == []
        assert result.cases[0].status == "reviewed" and not result.changed

    def test_empty_result_where_success_is_expected(self):
        empty = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        result = verify_cases([_case()], _executor({SQL_A: empty}), accept=True)
        assert result.problems[0].problem == "empty_result"
        assert result.cases[0].status == "reviewed"
        assert result.cases[0].expected_rows is None

    def test_empty_expectation_accepts_an_empty_result(self):
        empty = pd.DataFrame({"n": pd.Series([], dtype="int64")})
        result = verify_cases([_case(expect="empty")], _executor({SQL_A: empty}), accept=True)
        assert not result.problems
        assert result.cases[0].status == "active"
        assert result.cases[0].expected_rows == []

    def test_empty_expectation_with_rows_is_a_problem(self):
        result = verify_cases(
            [_case(expect="empty")], _executor({SQL_A: pd.DataFrame({"n": [1]})}), accept=True
        )
        assert result.problems[0].problem == "unexpected_rows"
        assert result.cases[0].status == "reviewed"

    def test_out_of_scope_case_is_accepted_without_executing_anything(self):
        case = GoldenCase(id="o", question="weather?", expect="out_of_scope", status="reviewed")
        wh = _executor({})
        result = verify_cases([case], wh, accept=True)
        assert result.cases[0].status == "active"
        assert wh.calls == []

    def test_pending_cases_are_left_alone(self):
        pending = GoldenCase(id="p", question="q", expected_sql=SQL_A, status="pending_review")
        wh = _executor({})
        result = verify_cases([pending], wh, accept=True)
        assert result.skipped == 1 and result.outcomes == [] and not result.changed
        assert result.cases == [pending]

    def test_active_case_keeps_its_recorded_answer_unless_refreshed(self):
        old = _case(status="active", expected_rows=[{"n": 1}], expected_fingerprint="old")
        wh = _executor({SQL_A: pd.DataFrame({"n": [2]})})
        kept = verify_cases([old], wh).cases[0]
        assert (kept.expected_rows, kept.expected_fingerprint) == ([{"n": 1}], "old")
        refreshed = verify_cases([old], wh, refresh=True).cases[0]
        assert refreshed.expected_rows == [{"n": 2}]

    def test_active_case_missing_its_answer_is_filled(self):
        active = _case(status="active")
        filled = verify_cases([active], _executor({SQL_A: pd.DataFrame({"n": [2]})})).cases[0]
        assert filled.expected_rows == [{"n": 2}] and filled.status == "active"

    def test_result_at_the_row_cap_is_flagged_as_truncated_but_not_blocked(self):
        df = pd.DataFrame({"n": [1, 2, 3]})
        with cfg.override_settings(max_rows_returned=3):
            result = verify_cases([_case()], _executor({SQL_A: df}), accept=True)
        assert result.outcomes[0].truncated and result.outcomes[0].problem is None
        assert result.cases[0].status == "active"

    def test_duplicate_column_names_are_a_problem_not_a_crash(self):
        df = pd.DataFrame([[1, 2]], columns=["x", "x"])
        result = verify_cases([_case()], _executor({SQL_A: df}), accept=True)
        assert result.problems[0].problem == "duplicate_columns"
        assert result.cases[0].status == "reviewed"

    def test_verified_cases_replay_offline_and_pass(self):
        df = pd.DataFrame({"n": [3], "d": [Decimal("1.5")]})
        sql = f"SELECT COUNT(*) AS n FROM {_TABLE}"
        cases = verify_cases([_case(sql=sql)], _executor({sql: df}), accept=True).cases
        results = run_golden_set(cases, make_offline_generator(cases), make_offline_executor(cases))
        assert [r.status for r in results] == ["pass"]


class TestRenderVerifyText:
    def test_report_has_counts_and_ids_but_no_question_or_row_text(self):
        bad = _case("bad", SQL_B)
        wh = _executor({SQL_A: pd.DataFrame({"n": [424242]}), SQL_B: RuntimeError("Database error: x")})
        result = verify_cases([_case("ok"), bad], wh)
        text = render_verify_text(result, accept=False, written=True)
        assert "PROBLEM bad: database_error" in text
        assert "re-run with --accept" in text
        assert SECRET_QUESTION not in text and "424242" not in text


class TestCliVerify:
    def _golden(self, tmp_path, cases):
        path = tmp_path / "golden.jsonl"
        path.write_text(store.dump_cases(cases), encoding="utf-8")
        return path

    def _run(self, monkeypatch, args, answers):
        wh = _executor(answers)
        monkeypatch.setattr(cli, "_build_executor", lambda: wh)
        return cli.main(["verify", *args]), wh

    def test_accept_writes_back_atomically_and_keeps_a_bak(self, tmp_path, monkeypatch, capsys):
        original = [_case("a"), _case("b", SQL_B, status="pending_review")]
        path = self._golden(tmp_path, original)
        before = path.read_text(encoding="utf-8")
        code, _ = self._run(
            monkeypatch, ["--golden", str(path), "--accept"], {SQL_A: pd.DataFrame({"n": [7]})}
        )
        assert code == 0
        written = {c.id: c for c in load_golden_cases(path)}
        assert written["a"].status == "active" and written["a"].expected_rows == [{"n": 7}]
        assert written["b"].status == "pending_review"
        assert (tmp_path / "golden.jsonl.bak").read_text(encoding="utf-8") == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ["golden.jsonl", "golden.jsonl.bak"]
        out = capsys.readouterr().out
        assert SECRET_QUESTION not in out and "7" not in out.replace("1 case", "")

    def test_without_accept_rows_are_recorded_and_status_stays(self, tmp_path, monkeypatch):
        path = self._golden(tmp_path, [_case("a")])
        code, _ = self._run(monkeypatch, ["--golden", str(path)], {SQL_A: pd.DataFrame({"n": [7]})})
        assert code == 0
        case = load_golden_cases(path)[0]
        assert case.status == "reviewed" and case.expected_rows == [{"n": 7}]

    def test_problem_gives_exit_code_one_and_does_not_touch_other_cases(self, tmp_path, monkeypatch):
        path = self._golden(tmp_path, [_case("a"), _case("b", SQL_B)])
        code, _ = self._run(
            monkeypatch, ["--golden", str(path), "--accept"],
            {SQL_A: pd.DataFrame({"n": [1]}), SQL_B: RuntimeError("Database error: no")},
        )
        assert code == 1
        statuses = {c.id: c.status for c in load_golden_cases(path)}
        assert statuses == {"a": "active", "b": "reviewed"}

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch, capsys):
        path = self._golden(tmp_path, [_case("a")])
        before = path.read_bytes()
        code, _ = self._run(
            monkeypatch, ["--golden", str(path), "--accept", "--dry-run"],
            {SQL_A: pd.DataFrame({"n": [1]})},
        )
        assert code == 0 and path.read_bytes() == before
        assert not (tmp_path / "golden.jsonl.bak").exists()
        assert "not modified" in capsys.readouterr().out

    def test_no_change_means_no_rewrite_and_no_bak(self, tmp_path, monkeypatch):
        pending = GoldenCase(id="p", question="q", expected_sql=SQL_A, status="pending_review")
        path = self._golden(tmp_path, [pending])
        code, _ = self._run(monkeypatch, ["--golden", str(path)], {})
        assert code == 0 and not (tmp_path / "golden.jsonl.bak").exists()

    def test_running_it_twice_is_stable(self, tmp_path, monkeypatch):
        path = self._golden(tmp_path, [_case("a")])
        answers = {SQL_A: pd.DataFrame({"n": [7]})}
        self._run(monkeypatch, ["--golden", str(path), "--accept"], answers)
        first = path.read_bytes()
        self._run(monkeypatch, ["--golden", str(path), "--accept"], answers)
        assert path.read_bytes() == first

    def test_the_default_executor_is_the_applications_own(self, monkeypatch):
        import database.executor as real

        assert cli._build_executor() is real.execute_sql


class TestStore:
    def test_a_failed_replace_leaves_the_original_and_no_temp_file(self, tmp_path, monkeypatch):
        target = tmp_path / "golden.jsonl"
        target.write_text("original\n", encoding="utf-8")

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            store.write_text_atomic(target, "new\n", backup=True)
        assert target.read_text(encoding="utf-8") == "original\n"
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def test_written_file_is_owner_only(self, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX permissions")
        target = tmp_path / "golden.jsonl"
        store.write_text_atomic(target, "x\n", backup=True)
        store.write_text_atomic(target, "y\n", backup=True)
        assert (target.stat().st_mode & 0o777) == 0o600
        assert ((tmp_path / "golden.jsonl.bak").stat().st_mode & 0o777) == 0o600

    def test_load_cases_or_empty(self, tmp_path):
        assert store.load_cases_or_empty(tmp_path / "missing.jsonl") == []
        blank = tmp_path / "blank.jsonl"
        blank.write_text("\n  \n", encoding="utf-8")
        assert store.load_cases_or_empty(blank) == []

    def test_dump_round_trips_persian(self, tmp_path):
        case = GoldenCase(id="a", question="تعداد سفارش‌ها", expected_sql=SQL_A)
        path = tmp_path / "g.jsonl"
        store.write_golden_cases(path, [case])
        assert "تعداد سفارش‌ها" in path.read_text(encoding="utf-8")  # not \u-escaped
        assert load_golden_cases(path) == [case]
