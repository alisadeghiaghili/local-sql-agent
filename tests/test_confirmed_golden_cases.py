# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Analyst confirmations flowing into the golden set.

``scripts/harvest_golden.py`` harvests each confirmed answer ("this answer
is correct", ``appdb.confirmations``) as a ``reviewed`` golden case ahead of
the stratified sample; ``scripts/golden_sheet.py export`` shows those as
already confirmed; ``python -m eval.cli verify --accept`` stays the gate
that activates them.

Real ``appdb`` on a ``tmp_path`` SQLite file, a real audit log written
through :func:`observability.audit.save_audit_record` into ``tmp_path`` and
the real CLIs (``main([...])``) -- no mock at the boundary under test.
Generic names only; the question text is a stand-in that must never reach
the console.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import pandas as pd
import pytest

import config as cfg
from appdb.confirmations import confirm_turn
from appdb.engine import dispose_app_engine
from eval import cli
from eval.runner import load_golden_cases, run_golden_set, make_offline_executor, make_offline_generator
from observability.audit import AuditRecord, save_audit_record
from observability.quality import Ratings
from scripts import golden_sheet
from scripts import harvest_golden as hg
from scripts.harvest_golden import CONFIRMED_TAG, harvest, main

SECRET = "پرسش-محرمانه-۹۹۱"


@pytest.fixture()
def env(tmp_path):
    """A real application database and audit log directory on ``tmp_path``.

    Also what keeps every CLI run in this module off a developer's own
    ``logs/app.db``."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    with cfg.override_settings(
        app_db_url=f"sqlite:///{tmp_path / 'app.db'}", log_dir=str(log_dir),
    ):
        dispose_app_engine()
        yield {"tmp": tmp_path, "log_dir": log_dir, "glob": str(log_dir / "audit_log.jsonl*")}
    dispose_app_engine()


def _audit(
    turn: str, question: str, *, sql: str = "SELECT 1 AS n", session: str = "s_1",
    ts: datetime | None = None, datasource: str | None = "sales", row_count: int = 1,
    error_code: str | None = None, request_id: str | None = None, model: str = "openai:real-model",
    columns: list[str] | None = ("n",),
) -> None:
    save_audit_record(AuditRecord(
        timestamp=ts or datetime(2026, 10, 1, 10, 0, 0),
        request_id=request_id or f"req_{turn}",
        question=question,
        generated_sql=sql,
        guard={"verdict": "allowed"},
        row_count=row_count,
        error_code=error_code,
        llm={"model": model},
        columns=list(columns) if columns is not None else None,
        principal_id="alice-the-analyst",
        session_id=session,
        turn_id=turn,
        datasource=datasource,
    ))


def _sample_noise(count: int) -> None:
    """Unconfirmed, distinct questions the stratified sample can pick from."""
    for i in range(count):
        _audit(f"noise_{i}", f"unrelated question number {i}", session="s_noise",
               ts=datetime(2026, 10, 2, 10, i % 60, 0))


def _run(env, *args):
    out = env["tmp"] / "eval_data" / "candidates.jsonl"
    code = main([env["glob"], "--out", str(out), *args])
    return code, out


# ---------------------------------------------------------------------------
# What a confirmed turn becomes
# ---------------------------------------------------------------------------


class TestAConfirmedTurnBecomesAReviewedCase:
    def test_status_sql_and_provenance(self, env):
        _audit("t_1", "How many customers?", sql="SELECT COUNT(*) AS n FROM Customer",
               request_id="req_conf_1")
        row, _ = confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-7")

        code, out = _run(env)

        assert code == 0
        (case,) = load_golden_cases(out)
        assert case.status == "reviewed", (
            "a person confirmed the SQL's answer; 'pending_review' would ask a second "
            "person to re-judge it and 'active' would skip the verify gate"
        )
        assert not case.is_runnable, "the regression gate must not run it before verify --accept"
        assert case.expected_sql == "SELECT COUNT(*) AS n FROM Customer"
        assert case.expect == "success"
        assert case.datasource == "sales"
        assert case.expected_rows is None and case.expected_fingerprint is None
        assert CONFIRMED_TAG in case.tags and "outcome:success" in case.tags
        assert case.id.startswith("confirmed_")
        assert "analyst-7" in case.notes, "the confirmer's principal id is in the provenance"
        assert row["created_at"] in case.notes, "and so is the confirmation time"
        assert "req_conf_1" in case.notes, "and the audit record it came from"

    def test_a_zero_row_answer_is_an_empty_expectation(self, env):
        _audit("t_1", "Any orders on a holiday?", row_count=0)
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-7")

        _, out = _run(env)

        (case,) = load_golden_cases(out)
        assert case.expect == "empty" and case.status == "reviewed"

    def test_ids_are_stable_across_runs(self, env):
        _audit("t_1", "How many customers?")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-7")
        _, out = _run(env)
        first = [c.id for c in load_golden_cases(out)]
        _, again = _run(env, "--force")
        assert [c.id for c in load_golden_cases(again)] == first


# ---------------------------------------------------------------------------
# Confirmed cases come first and are never dropped by --n
# ---------------------------------------------------------------------------


class TestIncludedBeforeTheSample:
    def test_n_limits_the_sample_only_never_the_confirmed_cases(self, env):
        for i in range(4):
            _audit(f"t_{i}", f"confirmed question {i}", ts=datetime(2026, 10, 1, 9, i, 0))
            confirm_turn(session_id="s_1", turn_id=f"t_{i}", principal_id="analyst-7")
        _sample_noise(20)

        code, out = _run(env, "--n", "2")

        assert code == 0
        cases = load_golden_cases(out)
        confirmed = [c for c in cases if CONFIRMED_TAG in c.tags]
        sampled = [c for c in cases if CONFIRMED_TAG not in c.tags]
        assert len(confirmed) == 4, "--n 2 must not drop a confirmed case"
        assert len(sampled) == 2
        assert [c.status for c in cases[:4]] == ["reviewed"] * 4, "confirmed come before the sample"
        assert {c.status for c in sampled} == {"pending_review"}

    def test_a_confirmed_question_is_not_also_sampled(self, env):
        _audit("t_1", "How many customers?", ts=datetime(2026, 10, 3, 9, 0, 0))
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-7")
        # The same question asked again, later and unconfirmed: still one case.
        _audit("t_again", "how many   customers?", session="s_2", ts=datetime(2026, 10, 4, 9, 0, 0))

        _, out = _run(env, "--n", "10")

        cases = load_golden_cases(out)
        assert len(cases) == 1 and cases[0].status == "reviewed"


# ---------------------------------------------------------------------------
# De-duplication: by normalised question, the most recent confirmation wins
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_two_confirmations_of_one_normalised_question_keep_the_most_recent(self, env):
        _audit("t_old", "How many customers?", sql="SELECT 1 AS n", request_id="req_old",
               ts=datetime(2026, 10, 1, 9, 0, 0))
        confirm_turn(session_id="s_1", turn_id="t_old", principal_id="analyst-old")
        _audit("t_new", "how many   CUSTOMERS?", sql="SELECT 2 AS n", request_id="req_new",
               session="s_2", ts=datetime(2026, 10, 2, 9, 0, 0))
        confirm_turn(session_id="s_2", turn_id="t_new", principal_id="analyst-new")

        _, out = _run(env)

        (case,) = load_golden_cases(out)
        assert case.expected_sql == "SELECT 2 AS n"
        assert "analyst-new" in case.notes and "analyst-old" not in case.notes

    def test_persian_spelling_variants_are_one_question(self, env):
        _audit("t_1", "تعداد سفارش‌های ۱۴۰۲ چقدر است؟", sql="SELECT 1 AS n")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        _audit("t_2", "تعداد سفارشهای 1402 چقدر است؟", sql="SELECT 2 AS n", session="s_2",
               ts=datetime(2026, 10, 2, 9, 0, 0))
        confirm_turn(session_id="s_2", turn_id="t_2", principal_id="analyst-2")

        _, out = _run(env)

        (case,) = load_golden_cases(out)
        assert case.expected_sql == "SELECT 2 AS n"

    def test_the_summary_counts_the_older_duplicate(self, env, capsys):
        for i, session in enumerate(("s_1", "s_2")):
            _audit(f"t_{i}", "How many customers?", session=session,
                   ts=datetime(2026, 10, 1 + i, 9, 0, 0))
            confirm_turn(session_id=session, turn_id=f"t_{i}", principal_id="analyst-1")
        _run(env)
        assert "older duplicates 1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# What is skipped, and said so
# ---------------------------------------------------------------------------


class TestSkippedConfirmations:
    def test_a_confirmation_whose_audit_record_is_gone_is_counted_not_fatal(self, env, capsys):
        _audit("t_1", "How many customers?")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        _audit("t_keep", "another question?", session="s_2")
        # Harvest from a log that no longer holds turn t_1.
        only_other = env["tmp"] / "rotated.jsonl"
        only_other.write_text(
            "\n".join(
                line for log in env["log_dir"].glob("audit_log.jsonl*")
                for line in log.read_text(encoding="utf-8").splitlines() if '"t_keep"' in line
            ) + "\n", encoding="utf-8",
        )
        out = env["tmp"] / "c.jsonl"

        assert main([str(only_other), "--out", str(out)]) == 0

        assert [c.question for c in load_golden_cases(out)] == ["another question?"]
        assert "no audit record 1" in capsys.readouterr().out

    def test_a_question_already_in_the_exclude_set_is_not_harvested_again(self, env, capsys):
        _audit("t_1", "How many customers?")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        golden = env["tmp"] / "golden.jsonl"
        golden.write_text(
            json.dumps({"id": "g", "question": "how MANY customers?", "expected_sql": "SELECT 1"}) + "\n",
            encoding="utf-8",
        )

        _, out = _run(env, "--exclude", str(golden))

        assert out.read_text(encoding="utf-8") == ""
        assert "already in golden set 1" in capsys.readouterr().out

    def test_stub_backend_records_are_not_harvested_even_if_confirmed(self, env):
        _audit("t_1", "How many customers?", model="mock:stub")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        _, out = _run(env)
        assert out.read_text(encoding="utf-8") == ""

    def test_the_pure_function_skips_a_turn_that_was_not_a_successful_answer(self):
        failed = {"question": "q?", "generated_sql": "SELECT 1", "error_code": "QUERY_EXECUTION_ERROR",
                  "session_id": "s", "turn_id": "t", "request_id": "r", "timestamp": "2026-10-01"}
        conf = [{"confirmation_id": 1, "session_id": "s", "turn_id": "t",
                 "principal_id": "a", "created_at": "2026-10-02"}]
        result = harvest([failed], n=5, confirmations=conf)
        assert result.confirmed == 0
        assert result.confirmation_stats["not_successful"] == 1


# ---------------------------------------------------------------------------
# --no-confirmations, and a database that cannot be read
# ---------------------------------------------------------------------------


class TestOptOutAndUnavailable:
    def test_no_confirmations_harvests_only_the_sample(self, env, capsys):
        _audit("t_1", "How many customers?")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")

        _, out = _run(env, "--no-confirmations")

        (case,) = load_golden_cases(out)
        assert case.status == "pending_review" and CONFIRMED_TAG not in case.tags
        assert "skipped (--no-confirmations)" in capsys.readouterr().out

    def test_no_confirmations_does_not_touch_the_application_database(self, env, monkeypatch):
        def boom():
            raise AssertionError("the application database must not be read")

        monkeypatch.setattr("observability.quality.load_ratings", boom)
        _audit("t_1", "How many customers?")
        code, _ = _run(env, "--no-confirmations")
        assert code == 0

    def test_an_unreadable_database_still_writes_the_sample_and_says_why(self, env, monkeypatch, capsys):
        monkeypatch.setattr(
            "observability.quality.load_ratings",
            lambda: Ratings(confirmations=None, flags=None, error="OperationalError"),
        )
        _audit("t_1", "How many customers?")

        code, out = _run(env)

        assert code == 0 and len(load_golden_cases(out)) == 1
        text = capsys.readouterr().out
        assert "unavailable" in text and "OperationalError" in text and "--no-confirmations" in text


# ---------------------------------------------------------------------------
# Privacy: same rules as the script's docstring
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_default_output_is_counts_only(self, env, capsys):
        _audit("t_1", f"{SECRET} one", sql="SELECT TOP 1 LeakedColumn FROM LeakedTable",
               request_id="req_leak")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-secret-id")

        code, _ = _run(env)

        printed = capsys.readouterr()
        everything = printed.out + printed.err
        assert code == 0
        assert SECRET not in everything
        assert "LeakedColumn" not in everything and "SELECT" not in everything
        assert "analyst-secret-id" not in everything, "the confirmer is never printed"
        assert "alice-the-analyst" not in everything and "req_leak" not in everything
        assert "1 confirmed + 0 sampled" in printed.out

    def test_the_file_is_owner_only(self, env):
        if os.name == "nt":
            pytest.skip("POSIX permissions")
        _audit("t_1", "How many customers?")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        _, out = _run(env)
        assert (out.stat().st_mode & 0o777) == 0o600

    def test_the_pure_summary_never_prints_the_confirmer(self):
        rec = {"question": SECRET, "generated_sql": "SELECT 1", "columns": ["n"], "row_count": 1,
               "session_id": "s", "turn_id": "t", "request_id": "r", "timestamp": "2026-10-01",
               "datasource": "sales"}
        conf = [{"confirmation_id": 1, "session_id": "s", "turn_id": "t",
                 "principal_id": "analyst-secret-id", "created_at": "2026-10-02"}]
        text = hg.render_summary(harvest([rec], n=5, confirmations=conf), "out.jsonl")
        assert "analyst-secret-id" not in text and SECRET not in text


# ---------------------------------------------------------------------------
# golden_sheet export shows them as already confirmed; verify --accept gates
# ---------------------------------------------------------------------------


class TestSheetAndGate:
    def test_export_lists_confirmed_cases_with_the_verdict_already_correct(self, env, capsys):
        _audit("t_1", "How many customers?", sql="SELECT COUNT(*) AS n FROM Customer")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-7")
        _sample_noise(2)
        _, cand = _run(env, "--n", "5")
        sheet = env["tmp"] / "review.csv"

        assert golden_sheet.main(["export", "--cases", str(cand), "--out", str(sheet)]) == 0

        rows = {r["question"]: r for r in golden_sheet.read_sheet(sheet)}
        confirmed = rows["How many customers?"]
        assert confirmed["verdict"] == "correct", "already confirmed by an analyst"
        assert confirmed["proposed_sql"] == "SELECT COUNT(*) AS n FROM Customer"
        assert "analyst-7" in confirmed["notes"], "the sheet says who confirmed it"
        unreviewed = [r for q, r in rows.items() if q != "How many customers?"]
        assert unreviewed and all(r["verdict"] == "" for r in unreviewed)
        assert "1 already confirmed" in capsys.readouterr().out

    def test_a_confirmed_case_alone_is_exportable(self, env):
        _audit("t_1", "How many customers?")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-7")
        _, cand = _run(env)
        assert golden_sheet.main(["export", "--cases", str(cand), "--out", str(env["tmp"] / "r.csv")]) == 0

    def test_the_whole_chain_ends_in_active_cases_only_through_verify_accept(
        self, env, monkeypatch,
    ):
        _audit("t_1", "How many customers?", sql="SELECT COUNT(*) AS n FROM Customer")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-7")
        _, cand = _run(env)
        sheet = env["tmp"] / "review.csv"
        assert golden_sheet.main(["export", "--cases", str(cand), "--out", str(sheet)]) == 0
        golden = env["tmp"] / "golden.jsonl"

        # The analyst changes nothing: the pre-filled verdict is enough.
        assert golden_sheet.main(
            ["import", "--sheet", str(sheet), "--candidates", str(cand), "--golden", str(golden)],
        ) == 0
        (imported,) = load_golden_cases(golden)
        assert imported.status == "reviewed" and not imported.is_runnable
        assert "analyst-7" in imported.notes and CONFIRMED_TAG in imported.tags

        # Not active until verify --accept has run the SQL against the database:
        # the regression gate skips it.
        assert run_golden_set(
            [imported], make_offline_generator([imported]), make_offline_executor([imported]),
        ) == []

        monkeypatch.setattr(cli, "_build_executor", lambda: (lambda sql: pd.DataFrame({"n": [9]})))
        assert cli.main(["verify", "--golden", str(golden), "--accept"]) == 0
        (active,) = load_golden_cases(golden)
        assert active.status == "active" and active.expected_rows == [{"n": 9}]
        assert CONFIRMED_TAG in active.tags, "the provenance survives activation"


def test_module_doctests_pass():
    import doctest

    failed, attempted = doctest.testmod(hg, verbose=False)
    assert attempted > 0 and failed == 0
    failed, attempted = doctest.testmod(golden_sheet, verbose=False)
    assert attempted > 0 and failed == 0
