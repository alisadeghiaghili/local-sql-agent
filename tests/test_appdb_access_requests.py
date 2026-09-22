# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""ADR-004 part 1 -- ``appdb.access_requests``.

Mirrors ``tests/test_appdb_feedback.py``'s own shape: a real ``appdb.engine``
SQLite file on a real ``tmp_path``, a real audit-log file -- no mock at the
boundary under test.

Two families of column name are used, deliberately kept apart:

* ``_DENIED_COLUMN``/``_OTHER_COLUMN`` are obviously synthetic
  (``SecretColumnXYZ``-style) -- used by every test that only exercises
  the join/dedup/triage logic in :mod:`appdb.access_requests` itself,
  which never consults the real schema at all (the column name comes
  verbatim from the joined audit record's ``guard.subject``).
* ``_ANY_COLUMN`` is a REAL column name, resolved at runtime from
  ``schema_data.columns.TABLE_COLUMNS`` the same ``_ANY_TABLE``/
  ``_ANY_COLUMN`` idiom ``tests/test_sql_guard_schema.py`` uses -- needed
  only by ``TestApprovalUpdatesLiveKeysOnly``, because
  ``appdb.key_store.issue_key``'s restrictive default seeds a fresh key's
  ``denied_columns`` from that same real column set, so a request naming a
  column outside it would trivially find nothing to remove and prove
  nothing about approval actually updating a key.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

import config as cfg
from appdb.access_requests import (
    AlreadyResolvedError,
    NotDeniedColumnError,
    RequestNotFoundError,
    TurnNotAuditedError,
    approve_request,
    deny_request,
    get_request,
    list_requests,
    submit_request,
)
from appdb.engine import dispose_app_engine
from appdb.key_store import issue_key, list_keys, revoke_key, set_disabled
from observability.audit import AuditRecord, save_audit_record

_DENIED_COLUMN = "SecretColumnXYZ"
_OTHER_COLUMN = "OtherSecretColumnXYZ"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"


def _any_real_column() -> str:
    """A real, schema-backed column name -- see module docstring. Resolved
    lazily (not at import time) since ``schema_data.columns.TABLE_COLUMNS``
    requires ``project_config_dir`` to already point at a real schema,
    which only the ``app_env`` fixture guarantees."""
    from schema_data.columns import TABLE_COLUMNS

    any_table = next(iter(TABLE_COLUMNS))
    return next(iter(TABLE_COLUMNS[any_table]))


@pytest.fixture()
def app_env(tmp_path):
    """Real ``appdb`` on a real temp SQLite file, a real (temp) audit log
    directory, and ``project_config.example/`` copied into ``tmp_path`` --
    mirrors ``tests/test_appdb_feedback.py``'s own ``app_env`` fixture.
    ``project_config_dir`` is needed here only because
    ``appdb.key_store.issue_key`` (exercised by ``TestApprovalUpdatesLiveKeysOnly``)
    reads the schema's real column set for its restrictive default -- this
    module's own column names (``_DENIED_COLUMN``/``_OTHER_COLUMN``) are
    never checked against it."""
    db_path = tmp_path / "app.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    project_dir = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE_CONFIG_DIR, project_dir)
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}",
        log_dir=str(log_dir),
        api_keys_json="[]",
        project_config_dir=str(project_dir),
    ):
        dispose_app_engine()
        yield {"db_path": db_path, "log_dir": log_dir}
    dispose_app_engine()


def _write_denied_column_audit(
    session_id: str, turn_id: str, *, column: str = _DENIED_COLUMN, question: str = "پرسش آزمایشی",
) -> None:
    save_audit_record(AuditRecord(
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        request_id="r_1",
        question=question,
        generated_sql="SELECT 1",
        guard={"verdict": "rejected", "reason": "denied_column", "subject": column},
        row_count=0,
        session_id=session_id,
        turn_id=turn_id,
    ))


def _write_allowed_audit(session_id: str, turn_id: str) -> None:
    save_audit_record(AuditRecord(
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        request_id="r_2",
        question="پرسش مجاز",
        generated_sql="SELECT 1",
        guard={"verdict": "allowed"},
        row_count=1,
        session_id=session_id,
        turn_id=turn_id,
    ))


# ---------------------------------------------------------------------------
# The join works, and the client can never forge the column
# ---------------------------------------------------------------------------


class TestTheJoinResolvesTheColumn:
    def test_submit_resolves_column_from_the_audit_records_guard_subject(self, app_env):
        _write_denied_column_audit("s_1", "t_1", column=_DENIED_COLUMN)
        row, created = submit_request(
            session_id="s_1", turn_id="t_1", requester_principal_id="analyst-1",
        )
        assert created is True
        assert row["column_name"] == _DENIED_COLUMN
        assert row["requester_principal_id"] == "analyst-1"
        assert row["status"] == "open"
        # Never carries the question or SQL.
        assert "question" not in row
        assert "generated_sql" not in row

    def test_requesting_an_unaudited_turn_is_refused(self, app_env):
        with pytest.raises(TurnNotAuditedError):
            submit_request(session_id="s_none", turn_id="t_none", requester_principal_id="analyst-1")

    def test_requesting_a_non_denied_column_turn_is_refused(self, app_env):
        _write_allowed_audit("s_2", "t_2")
        with pytest.raises(NotDeniedColumnError):
            submit_request(session_id="s_2", turn_id="t_2", requester_principal_id="analyst-1")

    def test_requesting_a_guard_rejection_with_no_subject_is_refused(self, app_env):
        save_audit_record(AuditRecord(
            timestamp=datetime(2026, 1, 1, 12, 0, 0),
            request_id="r_3", question="q", generated_sql="SELECT 1",
            guard={"verdict": "rejected", "reason": "forbidden_statement", "subject": None},
            row_count=0, session_id="s_3", turn_id="t_3",
        ))
        with pytest.raises(NotDeniedColumnError):
            submit_request(session_id="s_3", turn_id="t_3", requester_principal_id="analyst-1")


# ---------------------------------------------------------------------------
# Dedup: an existing open request wins
# ---------------------------------------------------------------------------


class TestDedup:
    def test_second_submit_for_same_principal_and_column_returns_the_existing_open_request(self, app_env):
        _write_denied_column_audit("s_4", "t_4", column=_DENIED_COLUMN)
        first, created1 = submit_request(session_id="s_4", turn_id="t_4", requester_principal_id="analyst-1")
        assert created1 is True

        _write_denied_column_audit("s_5", "t_5", column=_DENIED_COLUMN)
        second, created2 = submit_request(session_id="s_5", turn_id="t_5", requester_principal_id="analyst-1")
        assert created2 is False
        assert second["request_id"] == first["request_id"]

        assert len(list_requests(requester_principal_id="analyst-1")) == 1

    def test_different_principal_gets_its_own_request_for_the_same_column(self, app_env):
        _write_denied_column_audit("s_6", "t_6", column=_DENIED_COLUMN)
        first, _ = submit_request(session_id="s_6", turn_id="t_6", requester_principal_id="analyst-1")

        _write_denied_column_audit("s_7", "t_7", column=_DENIED_COLUMN)
        second, created = submit_request(session_id="s_7", turn_id="t_7", requester_principal_id="analyst-2")
        assert created is True
        assert second["request_id"] != first["request_id"]

    def test_different_column_gets_its_own_request_for_the_same_principal(self, app_env):
        _write_denied_column_audit("s_8", "t_8", column=_DENIED_COLUMN)
        first, _ = submit_request(session_id="s_8", turn_id="t_8", requester_principal_id="analyst-1")

        _write_denied_column_audit("s_9", "t_9", column=_OTHER_COLUMN)
        second, created = submit_request(session_id="s_9", turn_id="t_9", requester_principal_id="analyst-1")
        assert created is True
        assert second["request_id"] != first["request_id"]

    def test_a_new_request_is_created_once_the_prior_one_is_resolved(self, app_env):
        _write_denied_column_audit("s_10", "t_10", column=_DENIED_COLUMN)
        first, _ = submit_request(session_id="s_10", turn_id="t_10", requester_principal_id="analyst-1")
        deny_request(first["request_id"], actor_principal_id="security-1", reason="Not yet.")

        _write_denied_column_audit("s_11", "t_11", column=_DENIED_COLUMN)
        second, created = submit_request(session_id="s_11", turn_id="t_11", requester_principal_id="analyst-1")
        assert created is True
        assert second["request_id"] != first["request_id"]


# ---------------------------------------------------------------------------
# Approval goes through the existing ACL path
# ---------------------------------------------------------------------------


class TestApprovalUpdatesLiveKeysOnly:
    def _issue(self, principal_id: str, name: str) -> dict:
        _, entry = issue_key(principal_id, name)
        return entry

    def test_approval_removes_the_column_from_every_live_key_of_the_principal(self, app_env):
        column = _any_real_column()
        key1 = self._issue("analyst-1", "Key One")
        key2 = self._issue("analyst-1", "Key Two")
        assert column in key1["denied_columns"]
        assert column in key2["denied_columns"]

        _write_denied_column_audit("s_20", "t_20", column=column)
        row, _ = submit_request(session_id="s_20", turn_id="t_20", requester_principal_id="analyst-1")

        resolved, updated_count = approve_request(row["request_id"], actor_principal_id="security-1")
        assert resolved["status"] == "approved"
        assert updated_count == 2

        rows_by_hash = {r["key_sha256"]: r for r in list_keys()}
        assert column not in rows_by_hash[key1["key_sha256"]]["denied_columns"]
        assert column not in rows_by_hash[key2["key_sha256"]]["denied_columns"]

    def test_approval_never_touches_another_principals_keys(self, app_env):
        column = _any_real_column()
        own_key = self._issue("analyst-1", "Own Key")
        other_key = self._issue("analyst-2", "Other Principal's Key")

        _write_denied_column_audit("s_21", "t_21", column=column)
        row, _ = submit_request(session_id="s_21", turn_id="t_21", requester_principal_id="analyst-1")
        approve_request(row["request_id"], actor_principal_id="security-1")

        rows_by_hash = {r["key_sha256"]: r for r in list_keys()}
        assert column not in rows_by_hash[own_key["key_sha256"]]["denied_columns"]
        assert column in rows_by_hash[other_key["key_sha256"]]["denied_columns"], (
            "approving analyst-1's request must never widen analyst-2's key"
        )

    def test_approval_never_touches_a_revoked_key(self, app_env):
        column = _any_real_column()
        live_key = self._issue("analyst-1", "Live Key")
        revoked_key = self._issue("analyst-1", "Soon-Revoked Key")
        revoke_key(revoked_key["key_sha256"])

        _write_denied_column_audit("s_22", "t_22", column=column)
        row, _ = submit_request(session_id="s_22", turn_id="t_22", requester_principal_id="analyst-1")
        resolved, updated_count = approve_request(row["request_id"], actor_principal_id="security-1")

        assert updated_count == 1
        rows_by_hash = {r["key_sha256"]: r for r in list_keys()}
        assert column not in rows_by_hash[live_key["key_sha256"]]["denied_columns"]
        assert column in rows_by_hash[revoked_key["key_sha256"]]["denied_columns"], (
            "a revoked key must never be touched by an approval"
        )

    def test_approval_still_updates_a_disabled_key(self, app_env):
        """Disabling is reversible -- a re-enabled key must reflect the
        access decision made while it was off."""
        column = _any_real_column()
        disabled_key = self._issue("analyst-1", "Disabled Key")
        set_disabled(disabled_key["key_sha256"], True)

        _write_denied_column_audit("s_23", "t_23", column=column)
        row, _ = submit_request(session_id="s_23", turn_id="t_23", requester_principal_id="analyst-1")
        resolved, updated_count = approve_request(row["request_id"], actor_principal_id="security-1")

        assert updated_count == 1
        rows_by_hash = {r["key_sha256"]: r for r in list_keys()}
        assert column not in rows_by_hash[disabled_key["key_sha256"]]["denied_columns"]

    def test_approving_an_already_resolved_request_is_refused(self, app_env):
        column = _any_real_column()
        self._issue("analyst-1", "Key One")
        _write_denied_column_audit("s_24", "t_24", column=column)
        row, _ = submit_request(session_id="s_24", turn_id="t_24", requester_principal_id="analyst-1")
        approve_request(row["request_id"], actor_principal_id="security-1")

        with pytest.raises(AlreadyResolvedError):
            approve_request(row["request_id"], actor_principal_id="security-2")

    def test_approving_an_unknown_request_raises(self, app_env):
        with pytest.raises(RequestNotFoundError):
            approve_request(999_999, actor_principal_id="security-1")


# ---------------------------------------------------------------------------
# Denial requires a reason
# ---------------------------------------------------------------------------


class TestDenialRequiresAReason:
    def test_blank_reason_is_rejected(self, app_env):
        _write_denied_column_audit("s_30", "t_30", column=_DENIED_COLUMN)
        row, _ = submit_request(session_id="s_30", turn_id="t_30", requester_principal_id="analyst-1")
        with pytest.raises(ValueError):
            deny_request(row["request_id"], actor_principal_id="security-1", reason="   ")
        assert get_request(row["request_id"])["status"] == "open"

    def test_denial_with_a_reason_is_recorded_and_readable_by_the_requester(self, app_env):
        _write_denied_column_audit("s_31", "t_31", column=_DENIED_COLUMN)
        row, _ = submit_request(session_id="s_31", turn_id="t_31", requester_principal_id="analyst-1")
        resolved = deny_request(
            row["request_id"], actor_principal_id="security-1", reason="Column is under legal hold.",
        )
        assert resolved["status"] == "denied"
        assert resolved["resolution_note"] == "Column is under legal hold."

        own = list_requests(requester_principal_id="analyst-1")
        assert own[0]["resolution_note"] == "Column is under legal hold."

    def test_denying_an_already_resolved_request_is_refused(self, app_env):
        _write_denied_column_audit("s_32", "t_32", column=_DENIED_COLUMN)
        row, _ = submit_request(session_id="s_32", turn_id="t_32", requester_principal_id="analyst-1")
        deny_request(row["request_id"], actor_principal_id="security-1", reason="No.")
        with pytest.raises(AlreadyResolvedError):
            deny_request(row["request_id"], actor_principal_id="security-1", reason="No, again.")


# ---------------------------------------------------------------------------
# list_requests scoping
# ---------------------------------------------------------------------------


class TestListScoping:
    def test_requester_scoped_listing_excludes_other_principals(self, app_env):
        _write_denied_column_audit("s_40", "t_40", column=_DENIED_COLUMN)
        submit_request(session_id="s_40", turn_id="t_40", requester_principal_id="analyst-1")
        _write_denied_column_audit("s_41", "t_41", column=_DENIED_COLUMN)
        submit_request(session_id="s_41", turn_id="t_41", requester_principal_id="analyst-2")

        own = list_requests(requester_principal_id="analyst-1")
        assert len(own) == 1
        assert own[0]["requester_principal_id"] == "analyst-1"

    def test_status_filter(self, app_env):
        _write_denied_column_audit("s_42", "t_42", column=_DENIED_COLUMN)
        row, _ = submit_request(session_id="s_42", turn_id="t_42", requester_principal_id="analyst-1")
        deny_request(row["request_id"], actor_principal_id="security-1", reason="No.")

        _write_denied_column_audit("s_43", "t_43", column=_OTHER_COLUMN)
        submit_request(session_id="s_43", turn_id="t_43", requester_principal_id="analyst-1")

        assert len(list_requests(status="open")) == 1
        assert len(list_requests(status="denied")) == 1
        assert len(list_requests()) == 2
