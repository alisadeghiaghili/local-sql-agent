# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 6, §9 (resolved) -- admin-action log retention.

Two facts this covers, both stated in appdb/admin_audit.py's own module
docstring: the log is exempt from size-based rotation (retained by time
instead), and a config'd retention window actually discards what it
should while leaving everything newer untouched.
"""

from __future__ import annotations

import json

import pytest

import appdb.admin_audit as admin_audit
from appdb.admin_audit import (
    iter_admin_actions,
    purge_expired_admin_actions,
    record_admin_action,
)
from config import override_settings


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path):
    path = tmp_path / "admin_action_log.jsonl"
    admin_audit._ADMIN_ACTION_LOG_FILE = str(path)
    yield path
    admin_audit._ADMIN_ACTION_LOG_FILE = ""


class TestNeverSizeRotated:
    def test_a_large_volume_of_actions_never_rotates_the_file(self, _isolate_log):
        """The whole point of the anti-forensic fix: generating a lot of
        noise must never roll the oldest record off the end -- so a tiny
        log_max_bytes (which WOULD rotate an ordinary log almost
        immediately) must have no effect here at all."""
        with override_settings(log_max_bytes=200, log_backup_count=1):
            record_admin_action("actor-1", "operations", "key.issue", "target-0")
            for i in range(1, 200):
                record_admin_action("actor-1", "operations", "key.issue", f"target-{i}")

        assert not _isolate_log.with_suffix(_isolate_log.suffix + ".1").exists()
        actions = iter_admin_actions()
        assert len(actions) == 200
        assert actions[0]["target"] == "target-0"  # the oldest record survived


class TestTimeBasedPurge:
    def _write_raw(self, path, records):
        with path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def test_purge_discards_only_records_older_than_the_retention_window(self, _isolate_log):
        old = {
            "timestamp": "2020-01-01T00:00:00+00:00", "actor_principal_id": "a",
            "authorised_by": "operations", "action": "key.issue", "target": "old-key",
            "detail": {},
        }
        recent = {
            "timestamp": "2099-01-01T00:00:00+00:00", "actor_principal_id": "a",
            "authorised_by": "operations", "action": "key.issue", "target": "recent-key",
            "detail": {},
        }
        self._write_raw(_isolate_log, [old, recent])

        with override_settings(admin_action_log_retention_days=365):
            removed = purge_expired_admin_actions()

        assert removed == 1
        remaining = iter_admin_actions()
        assert [r["target"] for r in remaining] == ["recent-key"]

    def test_retention_days_zero_disables_the_purge(self, _isolate_log):
        old = {
            "timestamp": "2020-01-01T00:00:00+00:00", "actor_principal_id": "a",
            "authorised_by": "operations", "action": "key.issue", "target": "old-key",
            "detail": {},
        }
        self._write_raw(_isolate_log, [old])

        with override_settings(admin_action_log_retention_days=0):
            removed = purge_expired_admin_actions()

        assert removed == 0
        assert [r["target"] for r in iter_admin_actions()] == ["old-key"]

    def test_purge_is_a_no_op_when_the_log_does_not_exist_yet(self, tmp_path):
        admin_audit._ADMIN_ACTION_LOG_FILE = str(tmp_path / "does_not_exist.jsonl")
        with override_settings(admin_action_log_retention_days=30):
            assert purge_expired_admin_actions() == 0
