# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The application database — admin panel, phase 2.

``docs/admin-panel-architecture.md`` §5 and the phase 2 spec: durable
storage for the key store and role grants (feedback and configuration
versioning are out of scope for this phase — see the spec's §6). One
schema, one SQLAlchemy layer, several backends:

* unset ``APP_DB_URL`` — SQLite at :attr:`config.Settings.app_db_sqlite_path`,
  created automatically;
* a configured ``APP_DB_URL`` — tables created inside a database that
  already exists (never ``CREATE DATABASE`` — see :mod:`appdb.engine`).

Deliberately **not** the warehouse connection
(:data:`config.Settings.db_connection_url`), which ``docs/db-hardening.md``
specifies as read-only and ``database/executor.py`` always rolls back:
:func:`appdb.engine.raise_if_same_database` is the start-up check that
refuses to conflate the two.

Submodules
----------
:mod:`appdb.engine`
    Engine construction, the same-database refusal, and the SQLAlchemy
    ``MetaData``/``Table`` definitions.
:mod:`appdb.key_store`
    The key lifecycle — issue/disable/enable/revoke, the environment-key
    bootstrap/import, and the short-TTL cache with explicit invalidation
    that keeps revocation immediate without a database round trip per
    request (§5.5/§5.6).
:mod:`appdb.roles`
    The two-role grant/revoke, and the last-admin-of-either-kind
    protection (§2.2).
:mod:`appdb.admin_audit`
    The separate, append-only admin-action log (§5) — a JSONL stream,
    not a database table, for the same tamper-evidence reason
    ``docs/admin-panel-architecture.md`` §4.2 keeps the analyst audit log
    out of the application database: a row in this database is editable
    by anyone with a connection to it (including a DBA), which would
    defeat the one property this record exists for.
"""

from __future__ import annotations
