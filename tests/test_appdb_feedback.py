# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 4 -- appdb.feedback. Frozen spec.

Real ``appdb.engine`` SQLite file on a real ``tmp_path``, a real audit-log
file, and real :mod:`appdb.config_versions` -- no mock at the boundary
under test. Mirrors ``tests/test_session_persistence.py``'s own
"distinctive value, check the raw bytes" pattern (spec §7's second
bullet) for proving nothing gets duplicated onto the feedback row.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

import config as cfg
from appdb.engine import dispose_app_engine
from appdb.feedback import (
    FEEDBACK_CATEGORIES,
    RESOLUTION_OUTCOMES,
    AlreadyResolvedError,
    FeedbackNotFoundError,
    TurnNotAuditedError,
    feedback_stats,
    get_feedback,
    list_feedback,
    promote_to_golden_case,
    resolve_feedback,
    submit_flag,
)
from eval.runner import load_golden_cases, make_offline_executor, make_offline_generator, run_golden_set
from observability.audit import AuditRecord, find_record_by_turn, save_audit_record

_DISTINCTIVE_QUESTION = "پرسش-محرمانه-غیرقابل-افشا-۷۷۲۲"
_DISTINCTIVE_SQL = "SELECT TOP 1 SecretColumnXYZ FROM SecretTableXYZ"
_DISTINCTIVE_ROW_VALUE = "ردیف-محرمانه-غیرقابل-افشا-۸۸۳۳"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"


@pytest.fixture()
def app_env(tmp_path):
    """Real ``appdb`` on a real temp SQLite file, a real (temp) audit log
    directory, and -- mirroring ``tests/test_config_versions.py``'s own
    ``project_dir`` fixture -- ``project_config.example/`` copied into
    ``tmp_path`` rather than the real, git-ignored ``project_config/``, so
    ``TestNothingAutoApplies``'s calls into :mod:`appdb.config_versions`
    never touch real deployment data."""
    db_path = tmp_path / "app.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    project_dir = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE_CONFIG_DIR, project_dir)
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}",
        log_dir=str(log_dir),
        project_config_dir=str(project_dir),
    ):
        dispose_app_engine()
        yield {"db_path": db_path, "log_dir": log_dir, "project_dir": project_dir}
    dispose_app_engine()


def _write_audit_record(
    session_id: str, turn_id: str, *, request_id: str = "r_1", config_version_id=1,
    question: str = _DISTINCTIVE_QUESTION, sql: str = _DISTINCTIVE_SQL,
) -> None:
    save_audit_record(AuditRecord(
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        request_id=request_id,
        question=question,
        generated_sql=sql,
        guard={"verdict": "allowed"},
        row_count=1,
        session_id=session_id,
        turn_id=turn_id,
        config_version_id=config_version_id,
    ))


# ---------------------------------------------------------------------------
# §7 bullet 1: a flag references a real turn, and the join returns the
# question and SQL.
# ---------------------------------------------------------------------------


class TestTheJoinWorks:
    def test_submit_flag_resolves_request_id_and_config_version_from_the_audit_log(self, app_env):
        _write_audit_record("s_1", "t_1", request_id="r_abc", config_version_id=7)
        row = submit_flag(
            session_id="s_1", turn_id="t_1", reporter_principal_id="analyst-1",
            category="wrong_number",
        )
        assert row["request_id"] == "r_abc"
        assert row["config_version_id"] == 7

    def test_admin_can_join_a_stored_flag_back_to_question_and_sql(self, app_env):
        _write_audit_record("s_2", "t_2")
        row = submit_flag(
            session_id="s_2", turn_id="t_2", reporter_principal_id="analyst-1",
            category="different_question",
        )
        joined = find_record_by_turn(row["session_id"], row["turn_id"])
        assert joined is not None
        assert joined["question"] == _DISTINCTIVE_QUESTION
        assert joined["generated_sql"] == _DISTINCTIVE_SQL

    def test_flagging_an_unaudited_turn_is_refused(self, app_env):
        with pytest.raises(TurnNotAuditedError):
            submit_flag(
                session_id="s_none", turn_id="t_none", reporter_principal_id="analyst-1",
                category="other",
            )

    def test_category_must_be_in_the_closed_set(self, app_env):
        _write_audit_record("s_3", "t_3")
        with pytest.raises(ValueError):
            submit_flag(
                session_id="s_3", turn_id="t_3", reporter_principal_id="analyst-1",
                category="bogus_category",
            )
        assert "bogus_category" not in FEEDBACK_CATEGORIES


# ---------------------------------------------------------------------------
# §7 bullet 2: feedback rows contain no result rows and no duplicated
# question/SQL -- check the raw database bytes.
# ---------------------------------------------------------------------------


class TestFeedbackRowNeverDuplicatesContentOrRows:
    def test_the_stored_row_carries_no_question_or_sql_fields(self, app_env):
        _write_audit_record("s_4", "t_4")
        row = submit_flag(
            session_id="s_4", turn_id="t_4", reporter_principal_id="analyst-1",
            category="wrong_number", note="یادداشت بی‌خطر تحلیل‌گر",
        )
        assert "question" not in row
        assert "generated_sql" not in row
        assert "sql" not in row
        assert "rows" not in row
        assert "result" not in row

    def test_raw_database_bytes_never_contain_the_question_sql_or_row_value(self, app_env):
        _write_audit_record("s_5", "t_5")
        submit_flag(
            session_id="s_5", turn_id="t_5", reporter_principal_id="analyst-1",
            category="wrong_number", note="یادداشت بی‌خطر",
        )
        dispose_app_engine()  # flush the single shared SQLite connection

        raw = app_env["db_path"].read_bytes()
        assert _DISTINCTIVE_QUESTION.encode("utf-8") not in raw, (
            "the feedback table must never carry the question -- it is "
            "already in the audit log, keyed by the same session/turn id"
        )
        assert _DISTINCTIVE_SQL.encode("utf-8") not in raw, (
            "the feedback table must never carry the generated SQL"
        )
        assert _DISTINCTIVE_ROW_VALUE.encode("utf-8") not in raw, (
            "no result row value may ever reach the application database"
        )


# ---------------------------------------------------------------------------
# §7 bullet 3 (ownership) is enforced at the route layer -- see
# tests/test_v2_feedback_routes.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# §7 bullet 4: triage requires an outcome; a flag cannot leave the queue
# without one.
# ---------------------------------------------------------------------------


class TestTriageRequiresAnOutcome:
    def test_resolve_rejects_an_outcome_outside_the_closed_set(self, app_env):
        _write_audit_record("s_6", "t_6")
        row = submit_flag(
            session_id="s_6", turn_id="t_6", reporter_principal_id="analyst-1", category="other",
        )
        with pytest.raises(ValueError):
            resolve_feedback(
                row["feedback_id"], outcome="silently_ignore", actor_principal_id="ops-1",
            )
        # The row is still open -- an invalid resolution attempt must not
        # half-apply.
        assert get_feedback(row["feedback_id"])["status"] == "open"

    def test_not_a_defect_requires_a_non_blank_reason(self, app_env):
        _write_audit_record("s_7", "t_7")
        row = submit_flag(
            session_id="s_7", turn_id="t_7", reporter_principal_id="analyst-1", category="other",
        )
        with pytest.raises(ValueError):
            resolve_feedback(
                row["feedback_id"], outcome="not_a_defect", actor_principal_id="ops-1", note="   ",
            )
        assert get_feedback(row["feedback_id"])["status"] == "open"

        resolved = resolve_feedback(
            row["feedback_id"], outcome="not_a_defect", actor_principal_id="ops-1",
            note="Question was genuinely ambiguous -- analyst confirmed offline.",
        )
        assert resolved["status"] == "resolved"
        assert resolved["resolution_outcome"] == "not_a_defect"

    def test_a_resolved_flag_leaves_the_open_queue(self, app_env):
        _write_audit_record("s_8", "t_8")
        row = submit_flag(
            session_id="s_8", turn_id="t_8", reporter_principal_id="analyst-1", category="other",
        )
        resolve_feedback(
            row["feedback_id"], outcome="not_a_defect", actor_principal_id="ops-1",
            note="Not a defect -- confirmed correct.",
        )
        assert row["feedback_id"] not in [f["feedback_id"] for f in list_feedback(status="open")]
        assert row["feedback_id"] in [f["feedback_id"] for f in list_feedback(status="resolved")]

    def test_resolving_an_already_resolved_flag_is_refused(self, app_env):
        _write_audit_record("s_9", "t_9")
        row = submit_flag(
            session_id="s_9", turn_id="t_9", reporter_principal_id="analyst-1", category="other",
        )
        resolve_feedback(
            row["feedback_id"], outcome="not_a_defect", actor_principal_id="ops-1",
            note="Already handled.",
        )
        with pytest.raises(AlreadyResolvedError):
            resolve_feedback(
                row["feedback_id"], outcome="not_a_defect", actor_principal_id="ops-2",
                note="Second attempt.",
            )

    def test_resolve_unknown_feedback_raises(self, app_env):
        with pytest.raises(FeedbackNotFoundError):
            resolve_feedback(999_999, outcome="not_a_defect", actor_principal_id="ops-1", note="x")


# ---------------------------------------------------------------------------
# §7 bullet 5: a promoted golden case is written pending_expected, and the
# regression gate ignores it.
# ---------------------------------------------------------------------------


class TestPromotedGoldenCaseIsIgnoredByTheGate:
    def test_promoted_case_is_written_pending_expected(self, app_env, tmp_path):
        golden_path = tmp_path / "golden.jsonl"
        golden_path.write_text(
            '{"id": "existing", "question": "how many customers?", '
            '"expected_sql": "SELECT 1", "expected_fingerprint": "abc"}\n',
            encoding="utf-8",
        )
        with cfg.override_settings(eval_golden_path=str(golden_path)):
            _write_audit_record("s_10", "t_10", question="پرسشی که پاسخ غلط داشت؟")
            row = submit_flag(
                session_id="s_10", turn_id="t_10", reporter_principal_id="analyst-1",
                category="wrong_number",
            )
            promoted = promote_to_golden_case(row["feedback_id"])
            resolved = resolve_feedback(
                row["feedback_id"], outcome="golden_case", actor_principal_id="ops-1",
                golden_case_id=promoted["case_id"],
            )
            assert resolved["resolution_golden_case_id"] == promoted["case_id"]

            cases = load_golden_cases(golden_path)
            new_case = next(c for c in cases if c.id == promoted["case_id"])
            assert new_case.status == "pending_expected"
            assert new_case.expected_sql is None
            assert new_case.question == "پرسشی که پاسخ غلط داشت؟"

    def test_regression_gate_ignores_the_pending_case(self, app_env, tmp_path):
        """Prove the gate ignores it: run the golden set (offline mode, the
        same replay CI uses) and assert the pending case contributes NO
        pass/fail result at all -- not a pass, not a failure -- so a wrong
        placeholder expectation could never make the gate enforce it."""
        golden_path = tmp_path / "golden.jsonl"
        golden_path.write_text(
            '{"id": "existing", "question": "how many customers?", '
            '"expected_sql": "SELECT 1", "expected_fingerprint": "abc"}\n',
            encoding="utf-8",
        )
        with cfg.override_settings(eval_golden_path=str(golden_path)):
            _write_audit_record("s_11", "t_11", question="a flagged, still-unanswered question")
            row = submit_flag(
                session_id="s_11", turn_id="t_11", reporter_principal_id="analyst-1",
                category="wrong_number",
            )
            promote_to_golden_case(row["feedback_id"])

            cases = load_golden_cases(golden_path)
            assert len(cases) == 2, "the pending case must still be visible in the file itself"

            generate_fn = make_offline_generator(cases)
            execute_fn = make_offline_executor(cases)
            results = run_golden_set(cases, generate_fn, execute_fn)

        assert len(results) == 1, (
            "the pending_expected case must produce no CaseResult at all -- "
            "the regression gate must not run it, pass or fail, until "
            "someone supplies its expected_sql"
        )
        assert results[0].case_id == "existing"


# ---------------------------------------------------------------------------
# §7 bullet 6: nothing auto-applies -- a triage outcome never creates or
# applies a configuration version.
# ---------------------------------------------------------------------------


class TestNothingAutoApplies:
    def test_resolving_alias_fix_never_touches_the_active_config_version(self, app_env):
        from appdb.config_versions import get_active_version

        before = get_active_version()

        _write_audit_record("s_12", "t_12")
        row = submit_flag(
            session_id="s_12", turn_id="t_12", reporter_principal_id="analyst-1",
            category="different_question",
        )
        resolve_feedback(
            row["feedback_id"], outcome="alias_fix", actor_principal_id="ops-1",
            note="Model reached for the wrong ring name -- add a synonym.",
            config_version_id=42,  # provenance only -- an id this module never validates
        )

        after = get_active_version()
        assert after["version_id"] == before["version_id"], (
            "resolving a flag must never itself create or apply a "
            "configuration version -- the actual edit goes through the "
            "existing POST /admin/config/versions, unchanged by this phase"
        )
        assert after["content_hash"] == before["content_hash"]


# ---------------------------------------------------------------------------
# Stats (spec §5)
# ---------------------------------------------------------------------------


class TestFeedbackStats:
    def test_counts_reflect_open_and_resolved_flags(self, app_env):
        _write_audit_record("s_13", "t_13")
        row1 = submit_flag(
            session_id="s_13", turn_id="t_13", reporter_principal_id="analyst-1",
            category="wrong_number",
        )
        _write_audit_record("s_14", "t_14")
        submit_flag(
            session_id="s_14", turn_id="t_14", reporter_principal_id="analyst-1",
            category="other",
        )
        resolve_feedback(
            row1["feedback_id"], outcome="not_a_defect", actor_principal_id="ops-1",
            note="Confirmed correct.",
        )

        stats = feedback_stats()
        assert stats["flags_total"] == 2
        assert stats["flags_open"] == 1
        assert stats["outcomes_by_category"]["not_a_defect"] == 1
        for outcome in RESOLUTION_OUTCOMES:
            assert outcome in stats["outcomes_by_category"]
