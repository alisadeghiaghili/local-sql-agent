# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Role grant/revoke, and the last-admin-of-either-kind protection (§2.2).

Only a security admin may call :func:`grant` / :func:`revoke` — enforced
by ``api/admin_write_routes.py``'s ``require_security`` dependency on
``POST /admin/roles/{principal_id}``, not by anything in this module,
which does no authorisation of its own (the same split
``session/persistence.py`` keeps from ``session/store.py``: this module is
the storage, the route is the gate).

Bootstrap and lockout
----------------------
Both roles bootstrap from ``API_KEYS_JSON`` (never from a web flow —
``docs/admin-panel-architecture.md`` §2.3), and this module cannot touch
that: it only ever writes/deletes rows in
:data:`appdb.models.admin_principal_roles`. :func:`revoke` therefore
counts an environment-bootstrapped holder of a capability as a *permanent*
holder for the purpose of the last-admin check below — the API can never
demote them, so they can never be the thing standing between "one admin
left" and "zero". This is also precisely why an environment-bootstrapped
deployment can never be locked out by this module alone: recovery through
``.env`` plus a restart always remains possible, per the spec.

The last-admin check
----------------------
:func:`revoke` refuses when the principal being revoked is the **only**
current holder of that capability, counting both environment-bootstrapped
holders (:func:`security.auth.load_api_keys`'s own capability flags) and
database-granted holders (this table). Demoting the *only* remaining
holder of a capability would make recovery from a mistake require editing
``.env`` and restarting — exactly what §2.2 exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import security.auth as auth
from appdb.engine import get_app_engine
from appdb.key_store import invalidate_cache
from appdb.models import admin_principal_roles


class LastAdminError(RuntimeError):
    """Refusing to revoke the last remaining holder of a capability."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_holders(capability: str) -> set[str]:
    """Principal ids that hold *capability* via ``API_KEYS_JSON`` alone."""
    try:
        env_principals = auth.load_api_keys()
    except auth.ApiKeyConfigError:
        return set()
    return {p.id for p in env_principals.values() if capability in p.capabilities}


def _db_holders(capability: str) -> set[str]:
    engine = get_app_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(admin_principal_roles.c.principal_id).where(
                    admin_principal_roles.c.capability == capability
                )
            ).all()
    except SQLAlchemyError:
        return set()
    return {r[0] for r in rows}


def holders(capability: str) -> set[str]:
    """Every principal id currently holding *capability*, from either
    source. Used by the last-admin check, and by the roles read endpoint."""
    return _env_holders(capability) | _db_holders(capability)


def grant(principal_id: str, capability: str, granted_by: str) -> None:
    """Grant *capability* to *principal_id*. Idempotent — granting a
    capability a principal already holds (from either source) is a no-op
    that still invalidates the cache, so a caller never has to check
    first."""
    engine = get_app_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            select(admin_principal_roles).where(
                (admin_principal_roles.c.principal_id == principal_id)
                & (admin_principal_roles.c.capability == capability)
            )
        ).first()
        if existing is None:
            conn.execute(
                admin_principal_roles.insert().values(
                    principal_id=principal_id,
                    capability=capability,
                    granted_at=_now_iso(),
                    granted_by=granted_by,
                )
            )
    invalidate_cache()


def revoke(principal_id: str, capability: str) -> None:
    """Revoke *capability* from *principal_id*.

    Raises
    ------
    LastAdminError
        If *principal_id* is the only current holder of *capability*
        (counting both environment-bootstrapped and database-granted
        holders — see module docstring). The row (if any) is left
        untouched in this case.
    """
    current_holders = holders(capability)
    if current_holders == {principal_id}:
        raise LastAdminError(
            f"Refusing to revoke {capability!r} from {principal_id!r} -- "
            "they are the only remaining holder of this capability. "
            "Grant it to another principal first, or restore access "
            "through API_KEYS_JSON and a restart if this was a mistake."
        )

    engine = get_app_engine()
    with engine.begin() as conn:
        conn.execute(
            admin_principal_roles.delete().where(
                (admin_principal_roles.c.principal_id == principal_id)
                & (admin_principal_roles.c.capability == capability)
            )
        )
    invalidate_cache()
