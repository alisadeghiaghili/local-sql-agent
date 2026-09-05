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
from api.errors import AdminRequiredError, UnauthenticatedError
from security.auth import ANONYMOUS, ApiKeyConfigError, Principal, load_api_keys, resolve_principal

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """Resolve ``request.state.principal`` from ``Authorization: Bearer <key>``.

    Never rejects a request itself — see module docstring. A malformed
    ``API_KEYS_JSON`` (only reachable if the fail-closed startup check in
    ``api/server.py``'s ``lifespan`` was bypassed, e.g. a test that
    exercises this middleware directly) is logged and treated as "no
    keys configured" rather than raising mid-request.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        request.state.principal = self._resolve(request)
        return await call_next(request)

    @staticmethod
    def _resolve(request: Request) -> Principal | None:
        try:
            keys = load_api_keys()
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


def get_principal_if_any(request: Request) -> Principal | None:
    """The resolved principal for *request*, or ``None`` — never raises.

    Used by routes that stay open regardless of ``AUTH_REQUIRED`` (``GET
    /health``) but still want to know whether the caller authenticated,
    without pulling in :func:`require_principal`'s enforcement.
    """
    return getattr(request.state, "principal", None)
