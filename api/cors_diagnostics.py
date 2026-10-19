# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Name the origin CORS refused, in the server log.

The incident this exists for: the web UI showed every light red and the
server log held only ``OPTIONS /health``, ``OPTIONS /v2/sessions`` and
``OPTIONS /v2/access-requests``, each ``400 Bad Request``. The allowlist
held ``http://172.16.101.42:8077`` and the page had been opened as
``http://localhost:8077``. Starlette's ``CORSMiddleware`` answers a preflight
from an origin it does not allow with a bare 400 and says nothing; the browser
tells the page only "Failed to fetch". Nothing named the origin, so the
operator had to work it out.

:class:`RejectedOriginLogMiddleware` sits outside ``CORSMiddleware`` (it is
registered after it in ``api/server.py``, and Starlette makes the last
registered layer the outermost), so it sees the request before the CORS layer
answers. It never answers anything itself and never changes a request or a
response: the CORS policy is exactly what it was.

What it logs
------------
One ``WARNING`` per distinct rejected origin for the life of the process, for
a preflight and for an ordinary request alike (the response to the latter
reaches the browser without the CORS headers and the browser discards it). The
line names the origin, the allowed list, the exact ``.env`` line that would
allow it, and that the page must be opened with exactly an allowed address.

A request whose origin is the server's own address (the ``Origin`` equals the
``Host`` the request was sent to) is same-origin and needs no CORS, so it is
not reported.

Bounds
------
The origin is attacker-supplied text. It is stripped of non-printable
characters and cut to about 200 characters before it is stored or printed, at
most :data:`MAX_REMEMBERED_ORIGINS` distinct origins are remembered (after
which one line says that further ones are not logged), and it is never put
into a response.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from threading import Lock
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from core.cors_origins import clean_origin, env_line

__all__ = ["MAX_REMEMBERED_ORIGINS", "RejectedOriginLogMiddleware"]

logger = logging.getLogger(__name__)

#: Distinct rejected origins remembered (and so logged) per process.
MAX_REMEMBERED_ORIGINS = 64

#: An origin longer than this is never parsed for the same-origin test: it is
#: not a real origin, and parsing is work a request should not be able to pick.
_MAX_PARSED_ORIGIN = 2048


def _header(scope: Scope, name: bytes) -> str | None:
    """The first value of request header *name* (lower-case bytes), or ``None``."""
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _is_same_origin(origin: str, host: str | None) -> bool:
    """Whether *origin* names the server the request was sent to.

    Compares the origin's host and port with the ``Host`` header; the scheme
    is not compared because a TLS terminator in front of this process hides
    it.
    """
    if not host or len(origin) > _MAX_PARSED_ORIGIN:
        return False
    try:
        return urlsplit(origin).netloc.lower() == host.strip().lower()
    except ValueError:
        return False


class RejectedOriginLogMiddleware:
    """Log, once per origin, a request whose ``Origin`` CORS does not allow.

    Pure ASGI, so it never reads a body and adds nothing to a response.

    Parameters
    ----------
    app : ASGIApp
        The wrapped application (``CORSMiddleware`` in ``api/server.py``).
    allowed_origins : Sequence[str]
        The list ``CORSMiddleware`` was given. Pass the same list, so what is
        reported is what is enforced; ``"*"`` allows every origin, as it does
        there.
    max_origins : int, optional
        Distinct origins remembered, default :data:`MAX_REMEMBERED_ORIGINS`.

    Examples
    --------
    >>> import asyncio
    >>> async def inner(scope, receive, send):
    ...     pass
    >>> mw = RejectedOriginLogMiddleware(inner, ["http://a.example:1"])
    >>> mw.rejected_count
    0
    >>> scope = {"type": "http", "headers": [(b"origin", b"http://b.example:2")]}
    >>> asyncio.run(mw(scope, None, None))
    >>> mw.rejected_count
    1
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed_origins: Sequence[str],
        max_origins: int = MAX_REMEMBERED_ORIGINS,
    ) -> None:
        self.app = app
        self._allowed = tuple(allowed_origins)
        self._max = max_origins
        self._seen: set[str] = set()
        self._cap_reported = False
        self._lock = Lock()

    @property
    def rejected_count(self) -> int:
        """Distinct rejected origins remembered so far (never above the cap)."""
        with self._lock:
            return len(self._seen)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self._observe(scope)
        await self.app(scope, receive, send)

    def _observe(self, scope: Scope) -> None:
        origin = _header(scope, b"origin")
        if origin is None or "*" in self._allowed or origin in self._allowed:
            return
        if _is_same_origin(origin, _header(scope, b"host")):
            return

        shown = clean_origin(origin) or "(empty)"
        with self._lock:
            if shown in self._seen:
                return
            if len(self._seen) < self._max:
                self._seen.add(shown)
                report_cap = False
            elif self._cap_reported:
                return
            else:
                self._cap_reported = True
                report_cap = True

        if report_cap:
            logger.warning(
                "CORS: more than %d distinct rejected origins seen; "
                "further rejected origins are not logged.",
                self._max,
            )
            return
        allowed = [clean_origin(o) for o in self._allowed]
        logger.warning(
            "CORS: origin '%s' is not allowed (allowed: %s). Browsers show this as "
            "'Failed to fetch'. To allow it add this line to .env and restart: %s "
            "-- or open the page with exactly an allowed address (scheme, host and port). "
            "Logged once per origin.",
            shown,
            ", ".join(allowed) or "(none)",
            env_line(allowed, shown),
        )
