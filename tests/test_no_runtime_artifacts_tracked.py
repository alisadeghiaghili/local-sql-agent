# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""No file the running engine writes may be tracked by git.

The engine writes three kinds of thing at runtime, and all three carry
real user data on a real deployment: the audit trail (``logs/*.jsonl``
and its rotated ``.1``, ``.2`` siblings), the session store
(``logs/sessions.db``), and that store's SQLite sidecars
(``-wal``/``-shm``).

``.gitignore`` covers them. It did not always: ``*.db`` matches
``sessions.db`` but **not** ``sessions.db-wal``, and the write-ahead log
is where recently-written pages live -- on a deployment that has served
real questions, it is the file most likely to hold questions and
generated SQL that have not been checkpointed into the main database
yet. Both sidecars were committed once, by a ``git add -A`` in a working
copy where the server had run.

The comment in ``.gitignore`` explaining that hazard was already there,
written for the rotated audit logs. It did not stop the same mistake
being made again one line below it, because a comment cannot fail a
build. This test can.

It asserts against git's own index rather than re-implementing pattern
matching, so it is testing what is actually tracked -- not what a
reimplementation of ``.gitignore`` semantics believes should be.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: SQLite artefacts, wherever they appear. The engine writes these and
#: nothing else in the repository legitimately ends this way.
_DATABASE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal")

#: The audit trail lives in logs/ and is matched by location, not by
#: extension: eval_data.example/golden.jsonl is a committed template with
#: placeholder data and has every right to be tracked. A test that flags
#: it would be wrong, and a test that is wrong gets switched off -- which
#: is worse than not having one. Rotated siblings (.jsonl.1, .jsonl.2)
#: count too; they were the near-miss that put the warning in .gitignore.
_AUDIT_LOG_DIR = "logs/"
_AUDIT_LOG_PATTERN = re.compile(r"\.jsonl(\.\d+)?$")


def _is_runtime_artefact(path: str) -> bool:
    if any(path.endswith(suffix) for suffix in _DATABASE_SUFFIXES):
        return True
    return path.startswith(_AUDIT_LOG_DIR) and bool(_AUDIT_LOG_PATTERN.search(path))


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip("not a git working copy")
    return [line for line in result.stdout.splitlines() if line]


class TestNoRuntimeArtefactIsTracked:
    def test_no_database_or_log_artefact_is_in_the_index(self):
        tracked = _tracked_files()
        offenders = [path for path in tracked if _is_runtime_artefact(path)]
        assert offenders == [], (
            "these are written by the running engine and carry real user "
            "questions and generated SQL on a real deployment, so they must "
            "never be committed: " + ", ".join(offenders)
        )

    def test_the_sidecar_patterns_are_actually_ignored(self):
        """Not just absent today -- ignored, so they cannot come back.

        A file can be untracked simply because nobody has run the server
        in this working copy yet. ``git check-ignore`` asks the question
        that matters: would git ignore it if it appeared?
        """
        candidates = [
            "logs/sessions.db",
            "logs/sessions.db-wal",
            "logs/sessions.db-shm",
            "logs/audit_log.jsonl",
        ]
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", *candidates],
            cwd=_REPO_ROOT, capture_output=True, text=True, check=False,
        )
        ignored = set(result.stdout.split())
        missing = [c for c in candidates if c not in ignored]
        assert missing == [], (
            ".gitignore does not cover these, so one `git add -A` in a "
            "working copy where the server has run would commit them: "
            + ", ".join(missing)
        )
