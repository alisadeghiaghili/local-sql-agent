# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Round 2 remediation, Task 3 — auditing ``appdb/migrate.py`` for real.

``appdb/migrate.py`` is 624 lines, moves data between two live databases,
and was never opened by either pass of the 2026 audit. Reviewed here
against the checklist the round-2 plan sets for it: identifier/value
interpolation the source database's own catalogue could influence,
credentials written to logs or disk, files it creates, unvalidated paths,
and error messages that carry a connection string to a caller -- the
finding 11 pattern (``tests/security_audit/test_error_message_
sanitization.py``), one layer down.

What the audit found
---------------------
* **String-built SQL for autoincrement reseeding**
  (``_reset_autoincrement``, the ``mssql``/``mysql`` branches) interpolates
  ``table.name`` and an integer ``max_id`` into raw SQL text rather than
  binding them. Read in isolation this looks exactly like the shape
  ``security/sql_guard.py`` exists to refuse -- except ``table`` is always
  one of the four hardcoded ``sqlalchemy.Table`` objects this module
  imports from ``appdb.models`` (:data:`appdb.migrate.
  TABLES_IN_MIGRATION_ORDER`), never a name read from configuration, a
  request, or the source database's own catalogue, and ``max_id`` is
  always the integer result of ``MAX(pk_column)`` cast through ``int()``
  two lines above. Neither value is attacker- or operator-reachable text.
  Table identifiers in the reseed step (``_build_reseed_statement``) are
  now quoted with the dialect's ``identifier_preparer``.
* **No credentials are written to disk anywhere in this module.** The one
  file this tool's pipeline writes -- the temporary export artefact
  (``scripts/migrate_app_db.py``'s ``_make_export_writer``) -- is created
  via ``tempfile.mkstemp()``, which is owner-only (``0o600``) by default
  on POSIX with no code in this project having to ask for that. It holds
  API key hashes and column ACLs, not warehouse or application-database
  *credentials* -- see that file's own ``_SENSITIVITY_WARNING``.
* **Every path this module reads or writes comes from this module's own
  code** (``TABLES_IN_MIGRATION_ORDER``, the Alembic ``script_location``
  built from ``Path(__file__)``) -- never from a request, a config value,
  or the source database's catalogue. There is no path-traversal surface
  here to fix either.
* **The one real finding**: :func:`appdb.migrate.check_not_same_database`
  built its refusal message with ``f"...{source_url!r}...{target_url!r}"``
  -- the complete SQLAlchemy URL, credentials included when the target is
  a networked backend (``postgresql://user:pw@host/db``,
  ``mssql+pyodbc://user:pw@host/db``). :func:`appdb.migrate.run_migration`
  catches :class:`~appdb.migrate.MigrationRefusedError` and copies its
  message verbatim into :attr:`~appdb.migrate.MigrationResult.message`,
  which every caller (today, ``scripts/migrate_app_db.py``, which prints
  it; a future caller could log it, persist it, or surface it in an admin
  UI) is expected to display. This is exactly the finding-11 pattern --
  infrastructure detail that belongs in an operator's own eyes-only
  terminal leaking into a value built to be shown or stored -- one layer
  down from the HTTP response finding 11 was raised against. Fixed by
  redacting through :func:`sqlalchemy.engine.make_url`'s own
  ``render_as_string(hide_password=True)``, the exact mechanism this
  project's own ``scripts/verify_deployment.py`` already uses for the
  warehouse connection ("no credentials are ever printed -- only a
  password-redacted connection target").

What this file deliberately does not fix
------------------------------------------
``scripts/migrate_app_db.py``'s own ``print(f"  from: {args.source_url}")``
still prints the raw URL an operator supplied as their own CLI argument.
That is not the finding-11 pattern -- the "caller" there is the operator
who typed the credential themselves, on their own terminal, not a
separate party a leak would inform -- so it is out of scope for auditing
``appdb/migrate.py`` specifically and is left as a minor, separately
reportable observation rather than bundled into this fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

#: A realistic networked-backend URL, credential included, shaped like
#: what an operator would pass to ``scripts/migrate_app_db.py --to``.
_URL_WITH_CREDENTIAL = "postgresql://appuser:hunter2-the-password@db.internal.example:5432/appdb"


class TestTheRefusalMessageDoesNotCarryTheCredential:
    """``check_not_same_database`` is reachable with attacker-uninfluenced
    but still sensitive input: two URLs an operator configured. The
    refusal must still describe *what* went wrong without repeating the
    password back."""

    def test_check_not_same_database_redacts_the_password(self):
        from appdb.migrate import MigrationRefusedError, check_not_same_database

        with pytest.raises(MigrationRefusedError) as exc_info:
            check_not_same_database(_URL_WITH_CREDENTIAL, _URL_WITH_CREDENTIAL)

        message = str(exc_info.value)
        assert "hunter2-the-password" not in message, (
            f"check_not_same_database's refusal embeds the raw connection "
            f"URL, password included: {message!r}. A caller that displays "
            "or logs this message (scripts/migrate_app_db.py does both) "
            "leaks the credential"
        )
        # Sanitising must not mean deleting -- an operator reading the
        # refusal still needs to recognise which two URLs collided.
        assert "db.internal.example" in message and "appdb" in message, (
            "redacting the password also removed the host/database "
            "identity, which is the whole reason the message names the "
            "URLs at all"
        )

    def test_run_migration_surfaces_the_same_redacted_message(self, monkeypatch):
        """The path an actual caller sees:
        ``run_migration`` -> ``MigrationResult.message``.

        ``run_migration`` fingerprints the source both before and after
        every call (§6, "prove the source was never mutated"), which for
        a real networked URL means an actual connection attempt --
        orthogonal to what this test checks, and not something a unit
        test should depend on a reachable Postgres server for.
        :func:`appdb.migrate.hash_database` is stubbed out so this
        exercises the real ``check_not_same_database`` ->
        ``MigrationRefusedError`` -> ``MigrationResult.message`` path with
        no network involved.
        """
        import appdb.migrate as migrate

        monkeypatch.setattr(migrate, "hash_database", lambda url: "stub-hash")

        result = migrate.run_migration(_URL_WITH_CREDENTIAL, _URL_WITH_CREDENTIAL)
        assert not result.ok
        assert "hunter2-the-password" not in result.message, (
            f"MigrationResult.message carries the raw credential: "
            f"{result.message!r}"
        )
