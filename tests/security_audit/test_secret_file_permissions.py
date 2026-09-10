# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Finding 18 — nothing this project writes gets restrictive permissions.

The audit grepped the whole repository for ``chmod``, ``0o600`` and
``umask`` and found zero occurrences. Every file the application creates is
therefore written at the process umask, which on a normal Linux server
means ``0644`` -- readable by every local account.

The sharpest instance is ``webapp/app.py``::

    key = secrets.token_hex(32)
    SECRET_KEY_FILE.write_text(key)     # no chmod

That is the Flask session-signing key. Anyone with a shell on the host --
another service account, an operator, a compromised process -- reads it and
forges a session cookie for the admin user. No password, no brute force,
nothing in the auth logs. Combined with finding 13 (the Flask path passes
no principal, so the column ACL does not apply), that forged session has
unrestricted access to every column in the warehouse.

The same exposure covers the audit trail (real questions and generated
SQL), the session store (full transcripts), the Flask user table (password
hashes) and the export directory (query results).

Note on scope: this test asserts the *code* restricts what it creates. It
says nothing about the permissions of files already sitting on the
production server, which only an operator can inspect -- the audit's
"what was not tested" list says so explicitly.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: POSIX-only: Windows has no umask/mode model these assertions can read.
_posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="file mode bits are a POSIX concept; the deployment target for "
           "this finding is the Linux server, and CI covers it there",
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestTheFlaskSessionKeyIsNotWorldReadable:
    """The single highest-value secret this project writes to disk."""

    @_posix_only
    def test_a_freshly_generated_key_file_is_owner_only(self, tmp_path, monkeypatch):
        import webapp.app as webapp_app

        key_file = tmp_path / ".secret_key"
        monkeypatch.setattr(webapp_app, "SECRET_KEY_FILE", key_file)
        monkeypatch.delenv("SECRET_KEY", raising=False)

        webapp_app._secret_key()

        assert key_file.exists(), "_secret_key() no longer persists a key file"
        assert _mode(key_file) & 0o077 == 0, (
            f"{key_file.name} was created mode {oct(_mode(key_file))}. Any local "
            "account can read the Flask session-signing key and forge the "
            "admin's cookie -- no password required"
        )

    def test_the_writer_sets_a_mode_explicitly(self):
        """Asserted on the source too, so the guarantee survives on hosts
        where the test above is skipped."""
        src = (_REPO_ROOT / "webapp" / "app.py").read_text(encoding="utf-8")
        assert "chmod" in src, (
            "webapp/app.py writes the session key without ever calling chmod, "
            "so its mode is whatever the process umask happens to be"
        )


class TestOtherSecretsAndSensitiveDataAreRestricted:
    """The audit trail carries real questions and real generated SQL; the
    session store carries whole transcripts. Neither is a secret in the
    credential sense, and both are exactly what a data-governance review
    would object to finding world-readable on a shared host."""

    @_posix_only
    def test_the_audit_log_is_not_world_readable(self, tmp_path):
        """``append_jsonl`` takes a full path, not a name relative to
        ``LOG_DIR`` -- an earlier draft of this test assumed otherwise and
        could not have passed on any platform."""
        from logs.logger import append_jsonl

        target = tmp_path / "audit_probe.jsonl"
        append_jsonl(str(target), {"question": "q", "sql": "SELECT 1"})

        assert target.exists(), "append_jsonl wrote nothing at the path it was given"
        assert _mode(target) & 0o007 == 0, (
            f"{target.name} is world-readable ({oct(_mode(target))}). It holds "
            "verbatim analyst questions and the SQL they produced"
        )


class TestTheRepositoryTakesFilePermissionsSeriouslyAtAll:
    """A blunt guard against the whole class regressing. The audit's finding
    was not one bad line -- it was that the concept was absent everywhere."""

    def test_something_in_the_codebase_restricts_a_file_mode(self):
        hits = []
        for path in _REPO_ROOT.rglob("*.py"):
            parts = set(path.parts)
            if parts & {".venv", "__pycache__", "tests", "node_modules"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "chmod" in text or "S_IRUSR" in text:
                hits.append(path.relative_to(_REPO_ROOT).as_posix())
        assert hits, (
            "no module in this project restricts the mode of anything it "
            "writes. Secrets, the audit trail, the session store and the "
            "Flask password table are all created at the ambient umask"
        )
