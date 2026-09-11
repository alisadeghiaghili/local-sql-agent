# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Owner-only file permissions for everything this project writes to disk.

Why this module exists
-----------------------
Finding 18 (2026 audit) started as "nothing this codebase writes restricts
its own permissions" -- a repo-wide grep for ``chmod``/``0o600``/``umask``
came back empty, so every file this application ever created landed at
whatever the process umask happened to be (``0644`` on a typical Linux
host -- world-readable). Round 1 of remediation fixed the two sharpest
instances (``logs/logger.py``'s audit trail, ``webapp/app.py``'s Flask
session-signing key) by hand, each with its own inline
``if os.name != "nt": os.chmod(path, 0o600)``. That closed the finding's
most dangerous example but left it open everywhere else: the application
database (API key digests, role grants), the session store (full
conversation transcripts), ``webapp/app.db`` (password hashes), and
``exports/*.xlsx`` (query results) were all still created at the ambient
umask, because the fix was never factored into something a fifth call
site could just call.

This module is that shared call. Every creation point in the codebase
that writes something sensitive now calls :func:`restrict_file` (or
:func:`restrict_sqlite_family` for a SQLite database, which also has to
cover the ``-wal``/``-shm`` sidecars -- see that function's docstring) the
moment the file exists, instead of re-deriving the same three lines with
a slightly different guard or a forgotten sidecar.

Design decisions, and why each one is fail-closed rather than fail-loud
------------------------------------------------------------------------
* **Windows is a no-op, not a crash.** File mode bits are a POSIX concept;
  Windows has no equivalent this module can map onto, and the deployment
  target this finding describes is a Linux server. The ``os.chmod`` call
  itself stays present unconditionally in the *source* (guarded only at
  the call, with ``if os.name != "nt":``) rather than being conditionally
  imported or wrapped in a platform shim, because
  ``tests/security_audit/test_secret_file_permissions.py`` greps the
  source text for exactly this pattern as a backstop that survives on a
  platform where the live-permission assertion itself has to skip.
* **A ``chmod`` failure never propagates.** An exotic filesystem (a FUSE
  mount, some NFS configurations, a container filesystem with a
  restrictive mount option) can refuse a ``chmod`` for reasons that have
  nothing to do with whether the file itself was written successfully.
  Failing the caller's actual operation -- a query that ran fine, a
  session that saved fine -- over a permissions call that is defense in
  depth on top of that write would be a worse outcome than the
  world-readable file this module exists to prevent. Every failure is
  therefore caught, logged at ``WARNING`` (loud enough that an operator
  monitoring logs sees it, quiet enough that it never becomes a user-facing
  error), and swallowed -- the same "fail open but loud" shape
  ``observability/audit.py``'s audit-counter fallback uses for the same
  reason (round 1 remediation).
* **Idempotent.** Calling this on a file that already has the right mode
  (or that another process already restricted) is a no-op in effect --
  ``os.chmod`` unconditionally sets the mode rather than reading it back
  first, which is simpler and no slower than a read-then-maybe-write, and
  is safe to call on every write rather than only the write that first
  creates the file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: Owner read/write only. Applied to every plain file this project writes
#: that is not meant to be group- or world-readable -- the application
#: database, the session store, the Flask user table, exported workbooks.
_FILE_MODE = 0o600

#: Owner read/write/execute only. Applied to directories this project
#: creates to hold the files above, so a restrictive file mode is not
#: undermined by a world-readable parent directory an attacker could still
#: list (even if the file inside cannot be opened, its existence, size and
#: mtime are already information a shared host should not hand out).
_DIR_MODE = 0o700

#: SQLite sidecar suffixes that can hold data the main file does not (yet).
#: See :func:`restrict_sqlite_family` for why this list is exactly these
#: two and not, say, ``-journal`` as well.
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")


def _chmod_quietly(path: Path, mode: int) -> None:
    """Apply *mode* to *path*, never letting the attempt fail the caller.

    A no-op on Windows (see module docstring). On POSIX, any ``OSError``
    -- permission denied because the process does not own the file, an
    unsupported filesystem, the path having vanished between creation and
    this call -- is logged and swallowed rather than raised, because a
    permissions tightening step must never be the reason a query, a login,
    or an export fails for the user in front of it.
    """
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError as exc:
        logger.warning(
            "Could not restrict permissions on %s to %o: %s. The file may "
            "be readable by other local accounts.", path, mode, exc,
        )


def restrict_file(path: str | Path) -> None:
    """Restrict *path* to owner read/write only (``0o600``).

    Call this immediately after a sensitive file is first created --
    the application database, the session store, ``webapp/app.db``, an
    exported ``.xlsx`` workbook, or anything else this project writes
    that is not meant to be readable by another local account. Safe to
    call on a file that does not exist (a no-op: there is nothing to
    ``chmod``) so a caller does not need its own existence check first,
    and safe to call repeatedly on the same file (idempotent -- see
    module docstring).
    """
    p = Path(path)
    if not p.exists():
        return
    _chmod_quietly(p, _FILE_MODE)


def restrict_directory(path: str | Path) -> None:
    """Restrict *path* to owner read/write/execute only (``0o700``).

    Call this on a directory this project creates to hold sensitive
    files (an export directory, a session-store parent) -- a
    ``0o600``-restricted file inside a world-readable directory still
    leaks the file's name, size and modification time to anyone who can
    list that directory, which is exactly the kind of exposure this
    finding exists to close. Safe to call on a path that does not exist
    or is not a directory (a no-op).
    """
    p = Path(path)
    if not p.is_dir():
        return
    _chmod_quietly(p, _DIR_MODE)


def restrict_sqlite_family(db_path: str | Path) -> None:
    """Restrict a SQLite database file and its ``-wal``/``-shm`` sidecars.

    Why the sidecars matter as much as the main file
    --------------------------------------------------
    A database opened in write-ahead-logging mode (every SQLite database
    this project opens is -- see ``session/persistence.py`` and
    ``appdb/engine.py``) does not write committed pages straight into the
    main file. They land first in ``<db_path>-wal``, and stay there,
    readable by anyone who can open that file, until SQLite next
    checkpoints them back into the main database. On a live, frequently
    written database (the session store logging every turn, the key store
    recording every issued key) the ``-wal`` file is not a transient
    artefact -- it is routinely where the freshest rows actually live.
    Restricting only ``db_path`` and leaving ``db_path-wal`` at the
    ambient umask would protect the stale copy and expose the current
    one. ``.gitignore`` in this repository carries a comment about this
    exact lesson, learned the hard way, for the same reason.

    ``-shm`` (the shared-memory index SQLite uses to coordinate WAL
    readers across processes) holds no row data of its own, but is
    restricted alongside the other two anyway -- it is trivial to include
    and there is no argument for leaving any file this project's own
    database writes at a looser mode than the database itself.

    Deliberately not ``-journal``: unlike ``-wal``, a rollback-journal
    sidecar exists only *during* an in-flight transaction and is removed
    by SQLite itself the moment that transaction commits or rolls back
    (``.gitignore`` still lists ``*.db-journal`` -- ``appdb``'s database
    is opened at whatever SQLite's default journal mode is, since nothing
    in ``appdb/engine.py`` sets ``PRAGMA journal_mode`` the way
    ``session/persistence.py`` sets WAL -- but that mode's sidecar is
    exactly the file a caller here would race to ``chmod`` between two
    already-atomic operations it does not control the timing of, not a
    file it could ever observe sitting at rest with unwanted permissions
    the way an ``-wal`` file routinely does).

    Each of the three paths is restricted independently and only if it
    exists -- a database not yet under active write load may have no
    ``-wal``/``-shm`` sidecar at all (SQLite creates them lazily, on the
    first write in WAL mode), and that is a normal state, not an error.
    """
    p = Path(db_path)
    restrict_file(p)
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        restrict_file(p.with_name(p.name + suffix))
