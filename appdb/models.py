# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The application database's schema — SQLAlchemy Core ``Table`` objects.

Core, not the ORM: this codebase's existing SQLAlchemy usage
(``database/connection.py``, ``database/executor.py``) is Core-only
(``conn.exec_driver_sql``/``text()``), and the key store's access pattern
is simple row lookups and updates with no object graph worth mapping.
:data:`metadata` is what ``appdb/migrations/env.py`` points Alembic's
autogenerate at, and what a fresh SQLite deployment's tables are created
from directly (:func:`create_all`) without a migration tool in the loop at
all, matching ``session/persistence.py``'s own "create tables if missing,
unconditionally" behaviour for the zero-configuration path.

Two tables this phase needs:

``admin_api_keys``
    The key store (§3). One row per issued key, keyed by
    ``key_sha256`` — never the raw key (see ``security/auth.py``'s module
    docstring for why only the digest is ever persisted). Revocation is a
    tombstone (``revoked_at``, never a ``DELETE`` — §3.4): restoring this
    database to a point before a leaked key was revoked must not silently
    un-revoke it.

``admin_principal_roles``
    Role grants (§2) — ``(principal_id, capability)`` pairs. Presence of a
    row means the principal currently holds that capability; a revoke
    deletes the row. This table holds *current state* only — the
    who/when/why history of each grant and revoke lives in the separate,
    append-only admin-action log (:mod:`appdb.admin_audit`), not here,
    for the same reason that log is a file and not a database table (see
    this package's docstring).

Neither table names this deployment's warehouse in any way — both are
pure key/role bookkeeping, unconditionally the same shape regardless of
which warehouse this deployment queries.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Engine,
    MetaData,
    String,
    Table,
)

metadata = MetaData()

#: One row per issued API key. ``key_sha256`` is the primary key (lookups
#: are always by presented-key hash, and it is unique by construction --
#: two principals sharing a hash is exactly the ambiguity
#: ``security.auth._parse_api_keys`` already refuses for the environment
#: bootstrap path). ``principal_id`` is deliberately NOT unique: a
#: principal may hold more than one live key at once (rotation), and a
#: revoked row is never deleted (§3.4), so a principal issued a new key
#: after an old one was revoked would otherwise collide on a uniqueness
#: constraint that has nothing to do with the actual security question.
admin_api_keys = Table(
    "admin_api_keys",
    metadata,
    Column("key_sha256", String(64), primary_key=True),
    Column("principal_id", String(255), nullable=False),
    Column("name", String(255), nullable=False),
    #: JSON-encoded array of column names, matching
    #: ``API_KEYS_JSON``'s own ``denied_columns`` shape. Populated with
    #: *every* column this deployment's schema.yaml knows about at issue
    #: time (:mod:`appdb.key_store`'s restrictive default — see its
    #: docstring for why), never empty for a freshly issued key.
    Column("denied_columns_json", String, nullable=False),
    #: ``"issued"`` for a key minted via ``POST /admin/keys``, or
    #: ``"imported_from_env"`` for a row created once, at start-up, from
    #: an ``API_KEYS_JSON`` entry that predates this table (§3.3).
    #: Bookkeeping only -- both kinds behave identically once resolved.
    Column("source", String(32), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
    #: NULL while enabled. Set/cleared by ``POST /admin/keys/{id}/disable``
    #: and ``.../enable`` -- reversible, unlike ``revoked_at`` below.
    Column("disabled_at", String(64), nullable=True),
    #: NULL until revoked, then never cleared again (§3.4's tombstone --
    #: "never delete a key row"). A restore of this database to a point
    #: before this column was set must not un-revoke a key that leaked.
    Column("revoked_at", String(64), nullable=True),
)

#: Current role-grant state (§2). One row per ``(principal_id,
#: capability)`` pair a principal currently holds; a revoke deletes the
#: row rather than tombstoning it -- see the module docstring for why the
#: history lives in the admin-action log instead.
admin_principal_roles = Table(
    "admin_principal_roles",
    metadata,
    Column("principal_id", String(255), primary_key=True),
    Column("capability", String(32), primary_key=True),
    Column("granted_at", String(64), nullable=False),
    #: The principal id that authorised this grant, or ``"bootstrap"`` for
    #: a role that predates any grant (there is no such row today -- every
    #: environment-bootstrapped role is resolved fresh from
    #: ``API_KEYS_JSON`` on every request, per
    #: ``docs/admin-panel-architecture.md`` §2.3's "never from a web
    #: flow" -- this column exists for the day a migration tool backfills
    #: one, not because this phase writes it).
    Column("granted_by", String(255), nullable=False),
)


def create_all(engine: Engine) -> None:
    """Create every table in :data:`metadata` that does not already exist.

    ``checkfirst=True`` (the default) makes this the SQLAlchemy-Core
    equivalent of ``session/persistence.py``'s pre-Alembic ``CREATE TABLE
    IF NOT EXISTS`` -- the zero-configuration path (unset ``APP_DB_URL``,
    a SQLite file) needs no migration tool at all. A deployment on a
    managed backend that goes through Alembic instead
    (``appdb/migrations/``) never calls this directly; Alembic's own
    ``env.py`` points at this same :data:`metadata` so the two paths can
    never define the schema differently.
    """
    metadata.create_all(engine)
