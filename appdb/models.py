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
    Integer,
    MetaData,
    String,
    Table,
    Text,
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

#: Phase 3: the ``project_config/`` bundle's version history (§1, §6 of
#: ``docs/admin-panel-architecture.md`` and the phase 3 spec). One row per
#: bundle version -- a full snapshot of all nine ``project_config/*.yaml``
#: files, never a diff, so restoring any depth is a read rather than a
#: replay (spec §1). ``version_id`` is a plain autoincrementing integer:
#: the *order* versions were created in is exactly the information a
#: rollback and an audit trail both need, and an integer sequence is the
#: simplest thing that can never disagree with itself about that order
#: (unlike a timestamp, which two versions created within the same clock
#: tick could tie on).
#:
#: ``status`` is one of ``"applied"``, ``"draft"``, or ``"rejected"``
#: (spec §3.1's propose-and-approve flow). The *active* version is always
#: the highest ``version_id`` whose ``status`` is ``"applied"`` -- there is
#: deliberately no separate "is this the active one" flag to keep in sync;
#: see :mod:`appdb.config_versions` for why that single rule is sufficient
#: even across restores and approvals.
#:
#: ``content_json`` holds the full bundle as a JSON object
#: (``{filename: yaml_text}``, one entry per file) -- the same "JSON text
#: in a String/Text column" shape :data:`admin_api_keys`'s own
#: ``denied_columns_json`` already uses, chosen for the same reason: every
#: backend this application supports (SQLite, PostgreSQL, SQL Server,
#: MySQL) has a text column, and a bespoke JSON column type would be one
#: more thing :mod:`appdb.migrations` would need a backend-specific
#: mapping for (see the architecture's §5.4 on why type mapping across
#: backends must be explicit rather than incidental).
config_bundle_versions = Table(
    "config_bundle_versions",
    metadata,
    Column("version_id", Integer, primary_key=True, autoincrement=True),
    Column("status", String(16), nullable=False),
    Column("content_json", Text, nullable=False),
    #: sha256 hex digest of the canonical (sorted-key) JSON form of the
    #: content -- a cheap identity check (e.g. "did this restore actually
    #: change anything") without re-parsing every file.
    Column("content_hash", String(64), nullable=False),
    #: The version this row was optimistically based on (spec §8) -- the
    #: version the editor/reviewer had open when they saved. NULL only for
    #: the very first, bootstrap version, which has no prior version to be
    #: based on.
    Column("based_on_version", Integer, nullable=True),
    #: Set when this version's content is a revert of an earlier one
    #: (spec §2/§6.1's "revert, never reset") -- the version *number* that
    #: was restored from, never mutated itself.
    Column("restored_from_version", Integer, nullable=True),
    #: The single filename that was restored, when this version is a
    #: *per-file* restore (spec §1's "a single file may be restored from
    #: any version") rather than a restore of the whole bundle. NULL for a
    #: whole-bundle restore or an ordinary multi-file edit.
    Column("restored_file", String(64), nullable=True),
    Column("created_at", String(64), nullable=False),
    Column("created_by", String(255), nullable=False),
    #: Which capability authorised creating this version -- "operations"
    #: or "security" (never omitted, mirroring
    #: :attr:`appdb.admin_audit.AdminActionRecord.authorised_by`'s own
    #: reasoning: one principal may hold both roles at once).
    Column("created_by_capability", String(32), nullable=False),
    #: NULL until a draft is approved or rejected; the security admin who
    #: did so, thereafter (spec §3.1).
    Column("reviewed_by", String(255), nullable=True),
    Column("reviewed_at", String(64), nullable=True),
    Column("review_note", Text, nullable=True),
    #: Precomputed at creation time (never recomputed later, so a
    #: subsequent version's own diff/dry-run can never retroactively
    #: change what an earlier draft showed its reviewer): the structural
    #: diff (spec §4.3) and the offline dry-run summary (spec §5), each as
    #: a JSON object. NULL only for the bootstrap version, which has no
    #: prior version to diff or dry-run against.
    Column("diff_json", Text, nullable=True),
    Column("dry_run_json", Text, nullable=True),
)


#: Phase 4: wrong-answer feedback and its triage (spec §2, §3;
#: ``docs/admin-panel-architecture.md`` §3's tier-1 "closes a loop nothing
#: else closes"). One row per flag an analyst raised on one of their own
#: turns.
#:
#: Deliberately does NOT carry the question, the generated SQL, or any
#: result data -- spec §2.2: those already exist, keyed by the same
#: ``session_id``/``turn_id``, in the analyst audit log
#: (``observability/audit.py``, whose ``session_id``/``turn_id`` fields
#: exist precisely so this join is possible). Duplicating them here would
#: create a second place for the same content to leak from and a second
#: thing to keep consistent, and would put row-adjacent question text in
#: yet another store. ``request_id`` and ``config_version_id`` are resolved
#: from that same audit record at flag time (see :mod:`appdb.feedback`) and
#: stored here only as identifiers -- the same "an id, never the content it
#: names" category ``observability.audit.AuditRecord.session_id`` already
#: is.
turn_feedback = Table(
    "turn_feedback",
    metadata,
    Column("feedback_id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(64), nullable=False),
    Column("turn_id", String(64), nullable=False),
    #: The audit record's own request id, resolved by joining on
    #: (session_id, turn_id) at flag time -- never supplied by the client.
    #: NULL only if no audit record could be found for this turn (e.g. the
    #: audit log has since rotated past it).
    Column("request_id", String(64), nullable=True),
    Column("reporter_principal_id", String(255), nullable=False),
    #: One of :data:`appdb.feedback.FEEDBACK_CATEGORIES`.
    Column("category", String(32), nullable=False),
    #: The analyst's optional free-text note (spec §2.1) -- never the
    #: question or the SQL, which the join above already supplies.
    Column("note", Text, nullable=True),
    #: The configuration version (:mod:`appdb.config_versions`) active when
    #: this turn's answer was produced, resolved from the audit record --
    #: never the version active at flag time, which may already differ.
    #: NULL when the audit record predates this field or carries no value.
    Column("config_version_id", Integer, nullable=True),
    Column("created_at", String(64), nullable=False),
    #: ``"open"`` until triaged, then ``"resolved"`` -- spec §3's "a triage
    #: queue that lets items vanish without a decision becomes an inbox
    #: nobody opens": there is no third state and no delete.
    Column("status", String(16), nullable=False),
    #: One of :data:`appdb.feedback.RESOLUTION_OUTCOMES`, set only once
    #: ``status`` becomes ``"resolved"``.
    Column("resolution_outcome", String(32), nullable=True),
    #: Free-text reason -- required for ``"not_a_defect"`` (spec §3.1:
    #: "recorded with a reason, not silently dropped").
    Column("resolution_note", Text, nullable=True),
    #: For ``"alias_fix"``/``"rule_fix"`` outcomes: the id of an existing
    #: ``config_bundle_versions`` row the admin made the actual edit
    #: through (``POST /admin/config/versions`` -- phase 3, unchanged by
    #: this phase) and is linking here for provenance. This table never
    #: creates or applies a configuration version itself -- see
    #: :mod:`appdb.feedback`'s module docstring for why that is what makes
    #: "nothing auto-applies" (spec §3.2) trivially true rather than merely
    #: asserted.
    Column("resolution_config_version_id", Integer, nullable=True),
    #: For the ``"golden_case"`` outcome: the id assigned to the case
    #: written to the golden-set file (spec §4). NULL for every other
    #: outcome.
    Column("resolution_golden_case_id", String(255), nullable=True),
    Column("resolved_by", String(255), nullable=True),
    Column("resolved_at", String(64), nullable=True),
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
