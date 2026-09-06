# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The application database — admin panel, phases 2-4.

``docs/admin-panel-architecture.md`` §5: durable storage for the key
store, role grants, versioned ``project_config/`` history, and
wrong-answer feedback/triage — each added by its own phase (§6 named the
last two as originally out of scope for phase 2; :mod:`appdb.config_versions`
and :mod:`appdb.feedback` are where they landed). One schema, one
SQLAlchemy layer, several backends:

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
:mod:`appdb.config_versions`
    The versioned ``project_config/`` bundle — history, diff, rollback,
    and the operations/security propose-and-approve split for
    ``schema.yaml`` (§6, phase 3).
:mod:`appdb.feedback`
    Wrong-answer feedback and its triage (§3 Tier 1, phase 4) — never the
    question or the SQL, which the analyst audit log already carries
    keyed by the same session/turn id.
"""

from __future__ import annotations
