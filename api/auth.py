# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""ASGI-layer wiring for API-key authentication — Phase 8.

:mod:`security.auth` is framework-agnostic (``Principal``, key parsing,
key resolution). This module is the thin FastAPI/Starlette adapter on
top of it:

* :class:`AuthMiddleware` resolves the caller's :class:`~security.auth.Principal`
  from the ``Authorization`` header on *every* request and stashes it on
  ``request.state.principal`` — but never itself rejects a request. It
  must run before ``RateLimitMiddleware`` (see ``api/server.py``'s
  middleware ordering) so the rate limiter can read
  ``request.state.principal`` and bucket on principal id instead of IP
  for an authenticated caller.
* :func:`require_principal` is the FastAPI dependency protected routes
  declare (``Depends(require_principal)``). It is what actually enforces
  ``AUTH_REQUIRED``: a missing/invalid principal becomes a 401
  (:class:`~api.errors.UnauthenticatedError`) only when
  ``cfg.settings.auth_required`` is true; otherwise it degrades to
  :data:`~security.auth.ANONYMOUS` so the server keeps answering exactly
  as it did before this phase while the escape hatch is engaged.
* :func:`require_admin` is the further-restricted dependency every
  ``/admin/*`` route declares (admin panel phase 1 —
  ``docs/admin-panel-architecture.md``). It layers on top of
  :func:`require_principal`, so ``AUTH_REQUIRED=false`` is resolved to
  :data:`~security.auth.ANONYMOUS` *first* and only then checked for the
  admin capability — which ``ANONYMOUS`` never carries, so that escape
  hatch can never confer it.

``GET /health`` deliberately uses neither: it stays open unconditionally
and reads ``request.state.principal`` directly to decide whether to
include ``model`` in its response (see ``api/server.py``).
"""

from __future__ import annotations

import logging

from fastapi import Depends, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

import config as cfg
from api.errors import (
    AdminRequiredError,
    OperationsRequiredError,
    SecurityRequiredError,
    UnauthenticatedError,
)
from security.auth import (
    ANONYMOUS,
    ApiKeyConfigError,
    Principal,
    load_all_principals,
    resolve_principal,
)

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """Resolve ``request.state.principal`` from ``Authorization: Bearer <key>``.

    Never rejects a request itself — see module docstring. A malformed
    ``API_KEYS_JSON`` (only reachable if the fail-closed startup check in
    ``api/server.py``'s ``lifespan`` was bypassed, e.g. a test that
    exercises this middleware directly) is logged and treated as "no
    keys configured" rather than raising mid-request.

    Also stamps ``request.state.auth_failed`` (admin panel phase 6) --
    ``True`` only when an ``Authorization`` header was presented but did
    not resolve to a principal, never for a request with no header at all.
    ``api.middleware.RateLimitMiddleware`` reads this to bucket auth
    failures separately from the shared unauthenticated budget, and every
    such failure is also recorded via
    :func:`security.auth_failures.record_auth_failure` for the admin
    panel's operational-tier visibility.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        principal = self._resolve(request)
        request.state.principal = principal
        # An Authorization header was presented but did not resolve to a
        # real principal -- an auth FAILURE, distinct from a request that
        # never presented one at all (ordinary unauthenticated traffic,
        # e.g. GET /health from a probe). Recorded for the admin panel's
        # operational-tier visibility (security.auth_failures) and flagged
        # on request.state for RateLimitMiddleware's separate, smaller
        # auth-failure bucket (see that module's docstring and
        # docs/admin-panel-architecture.md §9). Never raised from here --
        # this middleware still never itself rejects a request (module
        # docstring).
        header_present = bool(request.headers.get("authorization"))
        request.state.auth_failed = header_present and principal is None
        if request.state.auth_failed:
            from security.auth_failures import record_auth_failure

            source_ip = request.client.host if request.client else "unknown"
            record_auth_failure(source_ip, request.url.path)
        return await call_next(request)

    @staticmethod
    def _resolve(request: Request) -> Principal | None:
        try:
            keys = load_all_principals()
        except ApiKeyConfigError as exc:
            logger.error("API_KEYS_JSON could not be parsed at request time: %s", exc)
            return None
        return resolve_principal(request.headers.get("authorization"), keys)


def require_principal(request: Request) -> Principal:
    """FastAPI dependency: the authenticated caller for a protected route.

    Reads ``request.state.principal`` — set by :class:`AuthMiddleware`,
    which runs earlier in the middleware stack — rather than re-parsing
    the ``Authorization`` header itself, so the two layers can never
    disagree about who the caller is.

    Raises
    ------
    api.errors.UnauthenticatedError
        (401, ``WWW-Authenticate: Bearer``) when ``cfg.settings.auth_required``
        is true and no valid principal was resolved for this request.
        When it is false (the deliberate ``AUTH_REQUIRED=false`` escape
        hatch), a missing/invalid principal degrades to
        :data:`~security.auth.ANONYMOUS` instead of failing the request.
    """
    principal: Principal | None = getattr(request.state, "principal", None)
    if not cfg.settings.auth_required:
        return principal or ANONYMOUS
    if principal is None:
        raise UnauthenticatedError("Missing or invalid API key.")
    return principal


def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    """FastAPI dependency for every ``/admin/*`` route (admin panel phase 1).

    Depends on :func:`require_principal` rather than re-reading
    ``request.state`` itself, so the two dependencies can never disagree
    about who the caller is, and so ``AUTH_REQUIRED``'s own enforcement
    (401 for no/invalid credentials) always runs first: an admin route
    must reject a missing key with 401, not 403, and only ask "is this
    key an admin key" once a real principal is established.

    Raises
    ------
    api.errors.AdminRequiredError
        (403) when the resolved principal does not carry the ``admin``
        capability (:attr:`~security.auth.Principal.is_admin`) — whether
        that principal is a real, non-admin key or
        :data:`~security.auth.ANONYMOUS` (the ``AUTH_REQUIRED=false``
        escape hatch, which must never confer it — see
        ``docs/admin-panel-architecture.md`` §2.3).
    """
    if not principal.is_admin:
        raise AdminRequiredError("This API key does not have admin access.")
    return principal


def require_operations(principal: Principal = Depends(require_principal)) -> Principal:
    """FastAPI dependency for every operations-gated ``/admin/*`` write
    route (admin panel phase 2 — ``docs/admin-panel-architecture.md`` §2:
    key lifecycle, and everything else that does not change who can see
    what data).

    Same shape as :func:`require_admin`: depends on :func:`require_principal`
    so ``AUTH_REQUIRED``'s own 401 always runs first, and the
    ``AUTH_REQUIRED=false`` escape hatch resolves to
    :data:`~security.auth.ANONYMOUS` — which carries no capabilities at
    all — *before* the capability check, so that escape hatch can never
    confer this capability either (§2.3).

    Raises
    ------
    api.errors.OperationsRequiredError
        (403) when the resolved principal does not carry
        :data:`~security.auth.OPERATIONS_CAPABILITY`.
    """
    if not principal.is_operations:
        raise OperationsRequiredError(
            "This API key does not have the operations admin capability."
        )
    return principal


def require_security(principal: Principal = Depends(require_principal)) -> Principal:
    """FastAPI dependency for every security-gated ``/admin/*`` write route
    (admin panel phase 2 §2: ``denied_columns`` ACL changes, granting
    either role — "anything that changes who can see what data").

    Same shape as :func:`require_operations` — see that function's
    docstring for why ``AUTH_REQUIRED=false`` can never confer this
    capability either.

    Raises
    ------
    api.errors.SecurityRequiredError
        (403) when the resolved principal does not carry
        :data:`~security.auth.SECURITY_CAPABILITY`.
    """
    if not principal.is_security:
        raise SecurityRequiredError(
            "This API key does not have the security admin capability."
        )
    return principal


def require_operations_or_security(
    principal: Principal = Depends(require_principal),
) -> Principal:
    """FastAPI dependency for a route either admin role may reach —
    mutual visibility with no mutual authority (§2.4): an operations
    admin can read that a security admin changed something, and a
    security admin can read that an operations admin did, without either
    being able to act as the other. Used only by read routes (the admin
    action log, the role-holders listing) — never by a route that
    mutates anything, which always declares exactly one of
    :func:`require_operations` / :func:`require_security` instead.

    Raises
    ------
    api.errors.AdminRequiredError
        (403) when the resolved principal carries neither capability.
        Reuses phase 1's error code rather than inventing a third one
        for "neither role" — the caller-facing fact is the same one
        :func:`require_admin` already reports (no admin surface at all
        for this principal).
    """
    if not (principal.is_operations or principal.is_security):
        raise AdminRequiredError(
            "This API key has neither the operations nor the security "
            "admin capability."
        )
    return principal


def get_principal_if_any(request: Request) -> Principal | None:
    """The resolved principal for *request*, or ``None`` — never raises.

    Used by routes that stay open regardless of ``AUTH_REQUIRED`` (``GET
    /health``) but still want to know whether the caller authenticated,
    without pulling in :func:`require_principal`'s enforcement.
    """
    return getattr(request.state, "principal", None)
