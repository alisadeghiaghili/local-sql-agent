# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The API-key lifecycle — issue/disable/enable/revoke, read at call time.

``docs/admin-panel-architecture.md`` §5.5/§5.6 and the phase 2 spec §3:
``API_KEYS_JSON`` is read once, at start-up, so a "disable" button could
never take effect before the next restart. Keys move into the application
database (:mod:`appdb.models`'s ``admin_api_keys`` table) precisely so a
revoked or disabled key stops authenticating on the *very next request* —
no restart, no sleep-and-hope.

The cost, and the mitigation (§3.2)
------------------------------------
Read-at-call-time plus keys-in-a-database means a network round trip to
the application database on every single request, unless that is
mitigated. :func:`get_active_principals` is cached in memory for
``cfg.settings.key_cache_ttl_seconds`` (default 5s) **and** every mutation
in this module calls :func:`invalidate_cache` explicitly — so revocation
is immediate regardless of the TTL (the cache is thrown away the instant a
key is revoked, not merely allowed to expire), while an unchanged key set
serves every request in between from memory, never re-querying the
database per request. Both halves are load-bearing: without the explicit
invalidation, revocation would only take effect after the TTL elapses
(not "the very next request"); without the TTL/cache at all, every
request pays the round trip the whole point of caching exists to avoid.

Environment keys keep working, and the ambiguity refusal (§3.3)
-------------------------------------------------------------------
``API_KEYS_JSON`` remains supported indefinitely, not just as a one-time
migration path. :func:`bootstrap_from_env` imports its entries into the
(then-empty) key table on the very first start against a fresh
application database. After that, both sources keep being read on every
request (:func:`get_active_principals` merges them) — but if the *same*
principal id ever resolves to two *different* key hashes across the two
sources, that is an ambiguous identity (which key is really "analyst-1"?)
and :func:`bootstrap_from_env` refuses to let the server start rather than
silently picking one, per the same reasoning
``security.auth._parse_api_keys`` already applies to two environment
entries sharing a hash.

Restrictive default, structurally (§2.1's escalation path #1)
-------------------------------------------------------------------
:func:`issue_key`'s signature has no ``denied_columns`` parameter at all —
the only caller of this function is the operations-gated
``POST /admin/keys`` route (:mod:`api.admin_write_routes`), so there is
structurally no path for an operations admin to request a specific ACL,
regardless of what the HTTP request body claims. Every newly issued key
instead gets :func:`_maximally_restrictive_denied_columns` — every column
name this deployment's ``schema.yaml`` currently knows about — so a freshly
issued key can authenticate but cannot select a single column of warehouse
data until a security admin loosens it via ``PATCH /admin/keys/{id}/acl``
(:func:`update_denied_columns`, the ``security``-gated sibling to this
module's operations-gated functions).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import config as cfg
import security.auth as auth
from appdb.engine import get_app_engine
from appdb.models import admin_api_keys, admin_principal_roles
from security.auth import ApiKeyConfigError, Principal

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KeyNotFoundError(LookupError):
    """No ``admin_api_keys`` row matches the given key id."""


class AmbiguousKeyIdentityError(RuntimeError):
    """The same principal id resolves to two different key hashes across
    ``API_KEYS_JSON`` and the application database (§3.3)."""


def _maximally_restrictive_denied_columns() -> list[str]:
    """Every column name this deployment's ``schema.yaml`` currently
    describes, across every table — the restrictive default a freshly
    issued key's ``denied_columns`` is set to (see module docstring).

    Reusing the schema registry (rather than a hardcoded engine-level
    notion of "sensitive columns", which would require this
    domain-agnostic engine to know a real warehouse's column names) means
    this default is correct for *any* deployment's schema, not just the
    one this codebase happens to ship example data for — the same reason
    ``api/admin_routes.py``'s ``/admin/config`` endpoint counts
    ``schema.yaml``'s tables instead of hardcoding a count.
    """
    from schema_data.registry import get_table_columns

    columns: set[str] = set()
    for column_map in get_table_columns().values():
        columns.update(column_map.keys())
    return sorted(columns)


# ---------------------------------------------------------------------------
# Start-up: import env keys once, and refuse an ambiguous identity
# ---------------------------------------------------------------------------

def bootstrap_from_env() -> None:
    """Import ``API_KEYS_JSON`` into the (if empty) key table, and refuse
    to proceed if any principal id is claimed by two different key hashes
    across the environment and the database. See module docstring.

    Called once from ``api/server.py``'s ``lifespan``, after
    :func:`appdb.engine.raise_if_same_database` has already passed.

    Raises
    ------
    security.auth.ApiKeyConfigError
        Propagated unchanged if ``API_KEYS_JSON`` itself is malformed —
        the existing fail-closed behaviour ``api/server.py`` already has
        for this, unchanged by this phase.
    AmbiguousKeyIdentityError
        If a principal id resolves to two different key hashes across the
        two sources.
    """
    env_principals = auth.load_api_keys()  # {key_sha256: Principal}; raises ApiKeyConfigError

    engine = get_app_engine()
    with engine.begin() as conn:
        existing = conn.execute(select(admin_api_keys)).mappings().all()

        if not existing and env_principals:
            now = _now_iso()
            for key_hash, principal in env_principals.items():
                conn.execute(
                    admin_api_keys.insert().values(
                        key_sha256=key_hash,
                        principal_id=principal.id,
                        name=principal.name,
                        denied_columns_json=json.dumps(list(principal.denied_columns)),
                        source="imported_from_env",
                        created_at=now,
                        updated_at=now,
                        disabled_at=None,
                        revoked_at=None,
                    )
                )
            # Re-read so the ambiguity check below sees the rows just
            # imported (which, being copies of env_principals itself,
            # can never trigger it -- but re-reading keeps this function
            # correct even if a future change makes the import partial).
            existing = conn.execute(select(admin_api_keys)).mappings().all()

    db_hashes_by_id: dict[str, set[str]] = {}
    for row in existing:
        db_hashes_by_id.setdefault(row["principal_id"], set()).add(row["key_sha256"])

    for key_hash, principal in env_principals.items():
        db_hashes = db_hashes_by_id.get(principal.id)
        if db_hashes and key_hash not in db_hashes:
            raise AmbiguousKeyIdentityError(
                f"Principal id {principal.id!r} is claimed by both an "
                "API_KEYS_JSON entry and a different application-database "
                "key (different key_sha256 in each) -- refusing to start "
                "rather than silently picking one. Either remove this id "
                "from API_KEYS_JSON, or revoke the conflicting database "
                "key and re-issue it under a different id."
            )

    invalidate_cache()


# ---------------------------------------------------------------------------
# The in-memory cache -- see module docstring's "The cost, and the
# mitigation" section.
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached_principals: dict[str, Principal] | None = None
_cached_at: float = 0.0


def invalidate_cache() -> None:
    """Discard the cached key set. Called by every mutating function in
    this module (and :mod:`appdb.roles`) so a revocation, disable, ACL
    change, or role grant/revoke is visible on the very next request,
    never merely "eventually, once the TTL elapses"."""
    global _cached_principals, _cached_at
    with _cache_lock:
        _cached_principals = None
        _cached_at = 0.0


def _role_capabilities_by_principal(conn) -> dict[str, frozenset[str]]:
    rows = conn.execute(
        select(admin_principal_roles.c.principal_id, admin_principal_roles.c.capability)
    ).all()
    out: dict[str, set[str]] = {}
    for principal_id, capability in rows:
        out.setdefault(principal_id, set()).add(capability)
    return {pid: frozenset(caps) for pid, caps in out.items()}


def _load_db_rows() -> dict[str, dict]:
    """Every ``admin_api_keys`` row (including disabled/revoked ones --
    callers decide usability), each carrying its role-derived
    capabilities. Empty (not an error) if the application database is
    momentarily unreachable -- see :func:`get_active_principals`'s
    docstring for why a DB outage degrades rather than blocks every
    caller, including ones authenticating with a pure-environment key."""
    try:
        engine = get_app_engine()
        with engine.connect() as conn:
            key_rows = conn.execute(select(admin_api_keys)).mappings().all()
            role_map = _role_capabilities_by_principal(conn)
    except SQLAlchemyError as exc:
        logger.warning(
            "Application database unreachable while loading the key store "
            "-- serving environment-configured keys only until it recovers: %s",
            exc,
        )
        return {}

    out: dict[str, dict] = {}
    for row in key_rows:
        out[row["key_sha256"]] = {
            "principal_id": row["principal_id"],
            "name": row["name"],
            "denied_columns": tuple(json.loads(row["denied_columns_json"])),
            "capabilities": role_map.get(row["principal_id"], frozenset()),
            "disabled_at": row["disabled_at"],
            "revoked_at": row["revoked_at"],
        }
    return out


def get_active_principals() -> dict[str, Principal]:
    """``{key_sha256: Principal}`` for every currently-usable key, merged
    from ``API_KEYS_JSON`` and the application database, cached for
    ``cfg.settings.key_cache_ttl_seconds``.

    Merge rule when a key hash exists in both sources (only possible for a
    hash imported at bootstrap, or one an operator has not yet removed
    from ``API_KEYS_JSON`` after importing): the **database row is
    authoritative** for ``denied_columns`` and for whether the key
    authenticates at all (``disabled_at``/``revoked_at``) — this is what
    makes revocation of an originally environment-sourced key immediate,
    even though its hash may still be sitting in an un-restarted
    process's ``API_KEYS_JSON``. The environment entry's own capability
    flags (``admin``/``operations``/``security``) are still unioned in,
    since those may only ever be granted from the environment (never
    through a web flow — ``docs/admin-panel-architecture.md`` §2.3), and
    must never be lost just because the same key was also imported.

    A key hash present **only** in ``API_KEYS_JSON`` (never imported, or
    added after the one-time import already ran) resolves from the
    environment alone, exactly as it did before this phase.
    """
    global _cached_principals, _cached_at

    ttl = cfg.settings.key_cache_ttl_seconds
    now = time.monotonic()
    with _cache_lock:
        if _cached_principals is not None and (ttl <= 0 or now - _cached_at < ttl):
            return _cached_principals

    db_rows = _load_db_rows()
    try:
        env_principals = auth.load_api_keys()
    except ApiKeyConfigError as exc:
        logger.error("API_KEYS_JSON could not be parsed while resolving keys: %s", exc)
        env_principals = {}

    merged: dict[str, Principal] = {}
    for key_hash, row in db_rows.items():
        if row["revoked_at"] is not None or row["disabled_at"] is not None:
            continue
        env_extra_caps = (
            env_principals[key_hash].capabilities if key_hash in env_principals else frozenset()
        )
        merged[key_hash] = Principal(
            id=row["principal_id"],
            name=row["name"],
            denied_columns=row["denied_columns"],
            capabilities=row["capabilities"] | env_extra_caps,
        )
    for key_hash, principal in env_principals.items():
        if key_hash not in db_rows:
            merged[key_hash] = principal

    with _cache_lock:
        _cached_principals = merged
        _cached_at = now
    return merged


# ---------------------------------------------------------------------------
# Lifecycle mutations -- each invalidates the cache before returning.
# ---------------------------------------------------------------------------

def issue_key(principal_id: str, name: str) -> tuple[str, dict]:
    """Mint a new key for *principal_id*, with the restrictive default ACL.

    Returns ``(raw_key, entry)`` — the raw key is returned **once**, never
    stored (only its SHA-256 digest is persisted), matching
    ``scripts/issue_api_key.py``'s existing behaviour. *entry* is the
    persisted row's data (never the raw key).

    Deliberately no ``denied_columns`` parameter — see module docstring's
    "Restrictive default, structurally" section.
    """
    raw_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    denied_columns = _maximally_restrictive_denied_columns()
    now = _now_iso()

    engine = get_app_engine()
    with engine.begin() as conn:
        conn.execute(
            admin_api_keys.insert().values(
                key_sha256=key_hash,
                principal_id=principal_id,
                name=name,
                denied_columns_json=json.dumps(denied_columns),
                source="issued",
                created_at=now,
                updated_at=now,
                disabled_at=None,
                revoked_at=None,
            )
        )
    invalidate_cache()
    return raw_key, {
        "key_sha256": key_hash,
        "principal_id": principal_id,
        "name": name,
        "denied_columns": denied_columns,
        "created_at": now,
    }


def _get_row(conn, key_sha256: str) -> dict:
    row = conn.execute(
        select(admin_api_keys).where(admin_api_keys.c.key_sha256 == key_sha256)
    ).mappings().first()
    if row is None:
        raise KeyNotFoundError(f"No key with key_sha256={key_sha256!r}")
    return dict(row)


def set_disabled(key_sha256: str, disabled: bool) -> None:
    """Toggle ``disabled_at`` for the key identified by *key_sha256*
    (operations action — reversible, unlike :func:`revoke_key`)."""
    now = _now_iso()
    engine = get_app_engine()
    with engine.begin() as conn:
        _get_row(conn, key_sha256)  # raises KeyNotFoundError if absent
        conn.execute(
            admin_api_keys.update()
            .where(admin_api_keys.c.key_sha256 == key_sha256)
            .values(disabled_at=(now if disabled else None), updated_at=now)
        )
    invalidate_cache()


def revoke_key(key_sha256: str) -> None:
    """Tombstone the key identified by *key_sha256* — ``revoked_at`` is set
    and never cleared again (§3.4). The row itself is never deleted:
    restoring this database to a point before the revocation must not
    silently un-revoke a key that leaked."""
    now = _now_iso()
    engine = get_app_engine()
    with engine.begin() as conn:
        _get_row(conn, key_sha256)
        conn.execute(
            admin_api_keys.update()
            .where(admin_api_keys.c.key_sha256 == key_sha256)
            .values(revoked_at=now, updated_at=now)
        )
    invalidate_cache()


def update_denied_columns(key_sha256: str, denied_columns: list[str]) -> None:
    """Set the key identified by *key_sha256*'s ``denied_columns`` — the
    security-gated ACL loosening/tightening endpoint
    (``PATCH /admin/keys/{id}/acl``). An empty list means "no column
    restriction", exactly as it does for an ``API_KEYS_JSON`` entry."""
    now = _now_iso()
    engine = get_app_engine()
    with engine.begin() as conn:
        _get_row(conn, key_sha256)
        conn.execute(
            admin_api_keys.update()
            .where(admin_api_keys.c.key_sha256 == key_sha256)
            .values(denied_columns_json=json.dumps(list(denied_columns)), updated_at=now)
        )
    invalidate_cache()


def list_keys() -> list[dict]:
    """Every key row, most recently created first — never the raw key,
    never the hash's originating raw material, only the persisted
    metadata. Used by the admin panel's key list."""
    engine = get_app_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(admin_api_keys).order_by(admin_api_keys.c.created_at.desc())
        ).mappings().all()
    return [
        {
            "key_sha256": row["key_sha256"],
            "principal_id": row["principal_id"],
            "name": row["name"],
            "denied_columns": json.loads(row["denied_columns_json"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "disabled_at": row["disabled_at"],
            "revoked_at": row["revoked_at"],
        }
        for row in rows
    ]
