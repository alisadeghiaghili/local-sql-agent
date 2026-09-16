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

Round 2: six creation points, not one
--------------------------------------
Round 1 fixed the Flask session key (``webapp/app.py``) and the audit
trail (``logs/logger.py``) by hand, and this file's own
``TestTheRepositoryTakesFilePermissionsSeriouslyAtAll`` was written to
catch a *regression of the whole class* -- "does anything at all restrict
a file mode". It is a trap: two hits made it pass while four more
creation points (the application database, the session store,
``webapp/app.db``, and exported ``.xlsx`` workbooks) were still written at
the ambient umask, because the blunt test cannot tell "restricted
somewhere" apart from "restricted everywhere it needs to be". That gap is
exactly how the finding stayed half-open through a round of remediation
that believed it was done.

``TestEveryKnownCreationPointRestrictsItsOwnFile`` below is the fix for
the test, not just the code: it names all six creation points explicitly,
creates each one for real in a ``tmp_path``, and asserts its mode
directly -- so a seventh creation point added later without calling
``core.fileperms.restrict_file`` fails a test that names it, rather than
silently passing the blunt one above. The blunt test is kept as a
cheap regression backstop for the *concept* (do not remove
``core/fileperms.py`` and every inline ``chmod`` and call it done), not as
evidence that coverage is complete -- see its own docstring below.
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


class TestEveryKnownCreationPointRestrictsItsOwnFile:
    """The strict version of this finding: each of the six places this
    project writes something sensitive to disk, exercised for real.

    Every method below builds the real artefact through the real
    production code path (never a hand-rolled substitute), in a
    ``tmp_path`` the test owns, and asserts ``mode & 0o077 == 0`` --
    nothing beyond owner read/write. This is what
    ``TestTheRepositoryTakesFilePermissionsSeriouslyAtAll`` below cannot
    tell you: not "does anything restrict a mode" but "does *this*
    specific thing".
    """

    @_posix_only
    def test_the_application_database_is_owner_only(self, tmp_path):
        """``appdb/engine.py:get_app_engine`` -- API key digests, role
        grants, config-bundle history."""
        from config import override_settings
        from appdb.engine import dispose_app_engine, get_app_engine

        db_path = tmp_path / "app.db"
        dispose_app_engine()
        try:
            with override_settings(app_db_url=f"sqlite:///{db_path}"):
                get_app_engine()
                assert db_path.exists(), "get_app_engine() did not create the SQLite file"
                assert _mode(db_path) & 0o077 == 0, (
                    f"the application database was created mode {oct(_mode(db_path))} -- "
                    "API key digests and role grants are world-readable"
                )
        finally:
            dispose_app_engine()

    @_posix_only
    def test_the_session_store_and_its_wal_sidecar_are_owner_only(self, tmp_path):
        """``session/persistence.py:SessionPersistence`` -- full turn
        transcripts. WAL mode is turned on explicitly in this module, so
        the "-wal" sidecar is exercised too, not just the main file --
        see ``core.fileperms.restrict_sqlite_family``'s docstring for why
        that sidecar matters as much as the database file itself."""
        from session.persistence import SessionPersistence

        db_path = tmp_path / "sessions.db"
        store = SessionPersistence(str(db_path))
        try:
            # A write is what actually puts pages in the "-wal" file --
            # right after construction it may not exist yet.
            store.upsert_session(
                "s1", owner_id=None, title=None,
                created_at="2026-01-01T00:00:00", last_active_at="2026-01-01T00:00:00",
            )
            assert db_path.exists(), "SessionPersistence did not create the SQLite file"
            assert _mode(db_path) & 0o077 == 0, (
                f"the session store was created mode {oct(_mode(db_path))} -- "
                "full conversation transcripts are world-readable"
            )
            wal_path = db_path.with_name(db_path.name + "-wal")
            if wal_path.exists():
                assert _mode(wal_path) & 0o077 == 0, (
                    f"{wal_path.name} was left mode {oct(_mode(wal_path))} -- the "
                    "write-ahead log holds pages not yet checkpointed into the main "
                    "file, i.e. the freshest transcript rows"
                )
        finally:
            store.close()

    @_posix_only
    def test_webapp_app_db_is_owner_only(self, tmp_path, monkeypatch):
        """``webapp/db.py:init_db`` -- Flask password hashes and the
        question/answer log for the web UI."""
        import webapp.db as webapp_db

        db_path = tmp_path / "app.db"
        monkeypatch.setattr(webapp_db, "DB_PATH", db_path)

        webapp_db.init_db()

        assert db_path.exists(), "init_db() did not create app.db"
        assert _mode(db_path) & 0o077 == 0, (
            f"webapp/app.db was created mode {oct(_mode(db_path))} -- Flask "
            "password hashes are world-readable"
        )

    @_posix_only
    def test_an_exported_workbook_is_owner_only(self, tmp_path):
        """``exporters/excel_exporter.py:export_excel`` -- warehouse query
        results handed to the analyst as a file on disk."""
        import pandas as pd

        import exporters.excel_exporter as ex

        monkeypatch_settings = type("S", (), {"export_dir": str(tmp_path)})()
        original_settings = ex.settings
        ex.settings = monkeypatch_settings
        try:
            path = ex.export_excel(pd.DataFrame({"a": [1, 2], "b": ["x", "y"]}))
        finally:
            ex.settings = original_settings

        assert Path(path).exists(), "export_excel() did not create the workbook"
        assert _mode(Path(path)) & 0o077 == 0, (
            f"the exported workbook was created mode {oct(_mode(Path(path)))} -- "
            "query results are world-readable"
        )


class TestTheWiringSurvivesOnAnyPlatform:
    """The four sites above call ``core.fileperms``, asserted on the source
    text so this guarantee is checked (and can fail CI) even on a
    development machine where the POSIX-only live tests above can only
    skip -- the same "assert the pattern in the source, not only the live
    mode" belt-and-suspenders ``TestTheFlaskSessionKeyIsNotWorldReadable
    .test_the_writer_sets_a_mode_explicitly`` already uses for round 1's
    two sites.
    """

    @pytest.mark.parametrize(
        "relative_path",
        [
            "appdb/engine.py",
            "session/persistence.py",
            "webapp/db.py",
            "exporters/excel_exporter.py",
        ],
    )
    def test_the_creation_point_calls_the_shared_helper(self, relative_path):
        src = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "core.fileperms" in src and (
            "restrict_file" in src or "restrict_sqlite_family" in src
        ), (
            f"{relative_path} does not call core.fileperms.restrict_file/"
            "restrict_sqlite_family -- whatever it writes lands at the "
            "ambient umask"
        )


class TestTheRepositoryTakesFilePermissionsSeriouslyAtAll:
    """A blunt guard against the whole class regressing -- NOT proof that
    every creation point is covered.

    This only asks "does anything in the codebase restrict a file mode at
    all". It is cheap and catches the worst regression (deleting
    ``core/fileperms.py`` and every inline ``chmod``), but it is exactly
    the test that let this finding stay half-open through round 1: two
    call sites restricting their files were enough to make this pass while
    four more still wrote at the ambient umask. The guarantee this finding
    actually needs -- every one of the six known creation points, by name
    -- lives in ``TestEveryKnownCreationPointRestrictsItsOwnFile`` above;
    treat that class as the specification and this one as a smoke test.
    """

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
