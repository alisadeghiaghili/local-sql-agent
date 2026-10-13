# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``appdb.confirmations`` -- "this answer is correct" -- and its migration.

Real ``appdb.engine`` SQLite file on a real ``tmp_path`` and a real audit
log file, no mock at the boundary under test (the same posture as
``tests/test_appdb_feedback.py``). Covers:

* the storage rules: at most one confirmation per (turn, principal),
  re-confirming is idempotent, a principal withdraws only their own;
* identifiers only: the question, the SQL and a result value never reach
  the database;
* the gate: only a turn whose audit record shows a successful execution can
  be confirmed;
* the flag half of "the later action wins" (``withdraw_open_flags``);
* the Alembic migration ``0005_turn_confirmations`` in both directions, and
  that ``appdb.migrate`` carries the table to another database.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

import config as cfg
from appdb.confirmations import (
    TurnNotConfirmableError,
    confirm_turn,
    get_confirmation,
    list_confirmations,
    withdraw_confirmation,
)
from appdb.engine import build_engine, dispose_app_engine, get_app_engine
from appdb.feedback import list_feedback, resolve_feedback, submit_flag, withdraw_open_flags
from appdb.migrate import run_migration
from appdb.models import create_all, turn_confirmations
from observability.audit import AuditRecord, save_audit_record, turn_executed_successfully

_DISTINCTIVE_QUESTION = "پرسش-محرمانه-غیرقابل-افشا-۴۴۱۱"
_DISTINCTIVE_SQL = "SELECT TOP 1 SecretColumnXYZ FROM SecretTableXYZ"

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app_env(tmp_path):
    """Real ``appdb`` on a temp SQLite file and a real (temp) audit log dir."""
    db_path = tmp_path / "app.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    with cfg.override_settings(app_db_url=f"sqlite:///{db_path}", log_dir=str(log_dir)):
        dispose_app_engine()
        yield {"db_path": db_path, "log_dir": log_dir}
    dispose_app_engine()


def _audit(
    session_id: str, turn_id: str, *, request_id: str = "r_1", sql: str = _DISTINCTIVE_SQL,
    error_code: str | None = None, guard: dict | None = None,
    columns: list[str] | None = ("n",), row_count: int = 1,
) -> None:
    save_audit_record(AuditRecord(
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        request_id=request_id,
        question=_DISTINCTIVE_QUESTION,
        generated_sql=sql,
        guard=guard or {"verdict": "allowed"},
        row_count=row_count,
        error_code=error_code,
        columns=list(columns) if columns is not None else None,
        session_id=session_id,
        turn_id=turn_id,
    ))


# ---------------------------------------------------------------------------
# Storage rules
# ---------------------------------------------------------------------------


class TestStorageRules:
    def test_confirming_stores_identifiers_and_resolves_request_id_from_the_audit_log(self, app_env):
        _audit("s_1", "t_1", request_id="r_abc")
        row, created = confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        assert created is True
        assert row["session_id"] == "s_1"
        assert row["turn_id"] == "t_1"
        assert row["principal_id"] == "analyst-1"
        assert row["request_id"] == "r_abc"
        assert row["created_at"]

    def test_reconfirming_is_idempotent(self, app_env):
        _audit("s_1", "t_1")
        first, created_first = confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        again, created_again = confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        assert (created_first, created_again) == (True, False)
        assert again["confirmation_id"] == first["confirmation_id"]
        assert again["created_at"] == first["created_at"], "the first click's time is kept"
        assert len(list_confirmations()) == 1

    def test_one_confirmation_per_principal_but_two_principals_may_both_confirm(self, app_env):
        _audit("s_1", "t_1")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-2")
        assert sorted(r["principal_id"] for r in list_confirmations()) == ["analyst-1", "analyst-2"]

    def test_the_database_itself_refuses_a_second_row_for_the_same_turn_and_principal(self, app_env):
        """The unique constraint, not only the module's check, enforces it
        -- so two concurrent clicks cannot store two rows."""
        _audit("s_1", "t_1")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        with pytest.raises(IntegrityError):
            with get_app_engine().begin() as conn:
                conn.execute(turn_confirmations.insert().values(
                    session_id="s_1", turn_id="t_1", principal_id="analyst-1",
                    request_id="r_x", created_at="2026-01-01T00:00:00+00:00",
                ))

    def test_withdrawing_deletes_only_the_callers_own_confirmation(self, app_env):
        _audit("s_1", "t_1")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-2")

        assert withdraw_confirmation(session_id="s_1", turn_id="t_1", principal_id="analyst-1") is True
        assert get_confirmation(session_id="s_1", turn_id="t_1", principal_id="analyst-1") is None
        assert get_confirmation(session_id="s_1", turn_id="t_1", principal_id="analyst-2") is not None

    def test_withdrawing_twice_is_not_an_error(self, app_env):
        _audit("s_1", "t_1")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        assert withdraw_confirmation(session_id="s_1", turn_id="t_1", principal_id="analyst-1") is True
        assert withdraw_confirmation(session_id="s_1", turn_id="t_1", principal_id="analyst-1") is False

    def test_a_withdrawn_turn_can_be_confirmed_again(self, app_env):
        _audit("s_1", "t_1")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        withdraw_confirmation(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        _row, created = confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        assert created is True

    def test_list_filters(self, app_env):
        for turn in ("t_1", "t_2"):
            _audit("s_1", turn)
        _audit("s_2", "t_9")
        confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        confirm_turn(session_id="s_1", turn_id="t_2", principal_id="analyst-2")
        confirm_turn(session_id="s_2", turn_id="t_9", principal_id="analyst-1")

        assert len(list_confirmations()) == 3
        assert {r["turn_id"] for r in list_confirmations(session_id="s_1")} == {"t_1", "t_2"}
        assert {r["turn_id"] for r in list_confirmations(principal_id="analyst-1")} == {"t_1", "t_9"}
        assert {
            r["turn_id"]
            for r in list_confirmations(session_id="s_1", principal_id="analyst-1")
        } == {"t_1"}
        assert list_confirmations(since="9999-01-01") == []


# ---------------------------------------------------------------------------
# Identifiers only
# ---------------------------------------------------------------------------


class TestIdentifiersOnly:
    def test_no_question_sql_or_data_reaches_the_database(self, app_env):
        _audit("s_1", "t_1")
        row, _ = confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")

        assert set(row) == {
            "confirmation_id", "session_id", "turn_id", "principal_id", "request_id", "created_at",
        }
        engine = get_app_engine()
        with engine.connect() as conn:
            dumped = " ".join(
                str(value)
                for r in conn.execute(text("SELECT * FROM turn_confirmations")).fetchall()
                for value in r
            )
        assert _DISTINCTIVE_QUESTION not in dumped
        assert "SecretColumnXYZ" not in dumped
        # ...and the raw database file's bytes, too (WAL included).
        raw = b"".join(p.read_bytes() for p in app_env["db_path"].parent.glob("app.db*"))
        assert _DISTINCTIVE_QUESTION.encode("utf-8") not in raw
        assert b"SecretColumnXYZ" not in raw

    def test_the_table_has_no_column_that_could_hold_content(self):
        assert {c.name for c in turn_confirmations.columns} == {
            "confirmation_id", "session_id", "turn_id", "principal_id", "request_id", "created_at",
        }


# ---------------------------------------------------------------------------
# The gate: only a successful answer
# ---------------------------------------------------------------------------


class TestOnlyASuccessfulAnswerCanBeConfirmed:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"error_code": "OUT_OF_SCOPE", "sql": ""},
            {"error_code": "FORBIDDEN_SQL", "guard": {"verdict": "rejected", "rule": "FORBIDDEN_SQL"}},
            {"error_code": "QUERY_EXECUTION_ERROR"},
            {"guard": {"verdict": "rejected"}},
            {"sql": ""},
            {"columns": None},
        ],
        ids=["out-of-scope", "guard-rejected", "execution-error", "rejected-no-code", "no-sql", "no-result"],
    )
    def test_an_unsuccessful_turn_is_refused_with_reason_not_successful(self, app_env, kwargs):
        _audit("s_1", "t_1", **kwargs)
        with pytest.raises(TurnNotConfirmableError) as excinfo:
            confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        assert excinfo.value.reason == "not_successful"
        assert list_confirmations() == []

    def test_a_turn_with_no_audit_record_is_refused_with_reason_not_audited(self, app_env):
        with pytest.raises(TurnNotConfirmableError) as excinfo:
            confirm_turn(session_id="s_none", turn_id="t_none", principal_id="analyst-1")
        assert excinfo.value.reason == "not_audited"

    def test_an_empty_result_is_still_a_successful_answer(self, app_env):
        _audit("s_1", "t_1", row_count=0)
        _row, created = confirm_turn(session_id="s_1", turn_id="t_1", principal_id="analyst-1")
        assert created is True

    def test_the_predicate_tolerates_a_record_from_an_older_version(self):
        assert turn_executed_successfully({"generated_sql": "SELECT 1"}) is True
        assert turn_executed_successfully({"generated_sql": "SELECT 1", "guard": None}) is True
        assert turn_executed_successfully({}) is False


# ---------------------------------------------------------------------------
# The flag half of "the later action wins"
# ---------------------------------------------------------------------------


class TestWithdrawOpenFlags:
    def _flag(self, principal: str, turn: str = "t_1") -> dict:
        return submit_flag(
            session_id="s_1", turn_id=turn, reporter_principal_id=principal, category="wrong_number",
        )

    def test_removes_only_the_principals_open_flags_on_that_turn(self, app_env):
        for turn in ("t_1", "t_2"):
            _audit("s_1", turn)
        self._flag("analyst-1")
        self._flag("analyst-2")
        self._flag("analyst-1", turn="t_2")

        removed = withdraw_open_flags(
            session_id="s_1", turn_id="t_1", reporter_principal_id="analyst-1",
        )

        assert removed == 1
        left = {(f["turn_id"], f["reporter_principal_id"]) for f in list_feedback()}
        assert left == {("t_1", "analyst-2"), ("t_2", "analyst-1")}

    def test_a_resolved_flag_is_kept_because_triage_recorded_a_decision_on_it(self, app_env):
        _audit("s_1", "t_1")
        flag = self._flag("analyst-1")
        resolve_feedback(
            flag["feedback_id"], outcome="not_a_defect", actor_principal_id="admin-1",
            note="the number was right",
        )

        removed = withdraw_open_flags(
            session_id="s_1", turn_id="t_1", reporter_principal_id="analyst-1",
        )

        assert removed == 0
        assert len(list_feedback()) == 1


# ---------------------------------------------------------------------------
# Migration 0005 and the cross-backend copy
# ---------------------------------------------------------------------------


def _alembic_config() -> Config:
    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", str(_REPO_ROOT / "appdb" / "migrations"))
    return alembic_cfg


class TestMigration0005:
    def test_upgrade_creates_the_table_with_the_unique_constraint(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'app.db'}"
        with cfg.override_settings(app_db_url=db_url):
            command.upgrade(_alembic_config(), "0005_turn_confirmations")

        engine = create_engine(db_url)
        try:
            insp = inspect(engine)
            assert insp.has_table("turn_confirmations")
            assert {c["name"] for c in insp.get_columns("turn_confirmations")} == {
                "confirmation_id", "session_id", "turn_id", "principal_id", "request_id", "created_at",
            }
            uniques = insp.get_unique_constraints("turn_confirmations")
            assert any(
                set(u["column_names"]) == {"session_id", "turn_id", "principal_id"} for u in uniques
            )
        finally:
            engine.dispose()

    def test_downgrade_drops_only_this_table(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'app.db'}"
        with cfg.override_settings(app_db_url=db_url):
            command.upgrade(_alembic_config(), "head")
            command.downgrade(_alembic_config(), "0004_access_requests")

        engine = create_engine(db_url)
        try:
            insp = inspect(engine)
            assert not insp.has_table("turn_confirmations")
            assert insp.has_table("access_requests")
            assert insp.has_table("turn_feedback")
        finally:
            engine.dispose()

    def test_the_migrated_schema_matches_the_models(self, tmp_path):
        """The hand-written migration and ``metadata.create_all`` (the
        zero-configuration path) must define the same table."""
        migrated_url = f"sqlite:///{tmp_path / 'migrated.db'}"
        created_url = f"sqlite:///{tmp_path / 'created.db'}"
        with cfg.override_settings(app_db_url=migrated_url):
            command.upgrade(_alembic_config(), "head")
        created = create_engine(created_url)
        create_all(created)
        migrated = create_engine(migrated_url)
        try:
            def shape(engine):
                insp = inspect(engine)
                return sorted(
                    (c["name"], str(c["type"]), c["nullable"])
                    for c in insp.get_columns("turn_confirmations")
                )
            assert shape(migrated) == shape(created)
        finally:
            created.dispose()
            migrated.dispose()


class TestCopiedByTheMigrationTool:
    def test_confirmations_survive_a_move_between_databases(self, tmp_path):
        source_url = f"sqlite:///{tmp_path / 'source.db'}"
        target_url = f"sqlite:///{tmp_path / 'target.db'}"
        engine = build_engine(source_url)
        create_all(engine)
        with engine.begin() as conn:
            conn.execute(turn_confirmations.insert().values(
                confirmation_id=7, session_id="s_1", turn_id="t_1", principal_id="analyst-1",
                request_id="r_1", created_at="2020-01-01T00:00:00+00:00",
            ))
        engine.dispose()

        result = run_migration(source_url, target_url)

        assert result.ok, result.message
        assert result.export_row_counts["turn_confirmations"] == 1
        target = build_engine(target_url)
        try:
            with target.connect() as conn:
                rows = conn.execute(select(turn_confirmations)).mappings().all()
        finally:
            target.dispose()
        assert [(r["confirmation_id"], r["principal_id"]) for r in rows] == [(7, "analyst-1")]
