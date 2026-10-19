# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Small, pure helpers for talking about CORS origins.

Two places need the same few facts about an origin and must not drift apart:
the middleware that names a rejected origin in the server log
(:mod:`api.cors_diagnostics`) and the preflight check
``Web UI origin is allowed by CORS`` (:mod:`scripts.verify_deployment`). Both
print the same ``.env`` line, both ask whether an origin is loopback, and both
treat an origin as text to be cleaned before it is shown. Nothing here reads
settings, the environment or a file.

Public API
----------
MAX_ORIGIN_LENGTH
    How many characters of an origin are ever shown.
clean_origin(origin, limit) -> str
env_line(allowed, origin) -> str
is_loopback_origin(origin) -> bool
parse_origin(raw) -> str | None
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from urllib.parse import urlsplit

__all__ = [
    "MAX_ORIGIN_LENGTH",
    "clean_origin",
    "env_line",
    "is_loopback_origin",
    "parse_origin",
]

#: Characters of an origin that are ever shown. A real origin is a scheme, a
#: host and a port; anything longer is not one, and a request can put up to
#: the server's header limit into the header.
MAX_ORIGIN_LENGTH = 200


def clean_origin(origin: str, limit: int = MAX_ORIGIN_LENGTH) -> str:
    """Make a request-supplied origin safe to print.

    Drops every character that is not printable (control characters, line and
    paragraph separators, zero-width and direction marks) so a request cannot
    start a second log line or hide text, then cuts the result to *limit*
    characters, marking the cut with ``...``.

    Parameters
    ----------
    origin : str
        The ``Origin`` header value, as received.
    limit : int, optional
        Characters kept, default :data:`MAX_ORIGIN_LENGTH`.

    Returns
    -------
    str
        The cleaned text; ``""`` when nothing printable is left.

    Examples
    --------
    >>> clean_origin("http://host:8077")
    'http://host:8077'
    >>> clean_origin("http://a\\r\\nWARNING forged\\x00")
    'http://aWARNING forged'
    >>> clean_origin("x" * 300, limit=5)
    'xxxxx...'
    """
    printable = "".join(ch for ch in origin if ch.isprintable())
    if len(printable) > limit:
        return printable[:limit] + "..."
    return printable


def env_line(allowed: Sequence[str], origin: str) -> str:
    """The ``.env`` line that adds *origin* to the allowed list.

    ``CORS_ALLOWED_ORIGINS`` replaces the default list instead of extending
    it, so the line has to carry every origin that is already allowed.

    Parameters
    ----------
    allowed : Sequence[str]
        The origins allowed now, in order.
    origin : str
        The origin to add.

    Returns
    -------
    str
        ``CORS_ALLOWED_ORIGINS=<allowed...>,<origin>``; just the origin when
        nothing is allowed yet.

    Examples
    --------
    >>> env_line(["http://172.16.101.42:8077"], "http://localhost:8077")
    'CORS_ALLOWED_ORIGINS=http://172.16.101.42:8077,http://localhost:8077'
    >>> env_line([], "http://localhost:8077")
    'CORS_ALLOWED_ORIGINS=http://localhost:8077'
    """
    return "CORS_ALLOWED_ORIGINS=" + ",".join([*allowed, origin])


def is_loopback_origin(origin: str) -> bool:
    """Whether an origin names this machine (``localhost``, ``127.x.x.x``, ``::1``).

    Parameters
    ----------
    origin : str
        An origin such as ``http://localhost:8080``.

    Returns
    -------
    bool
        ``False`` for anything that does not parse, so an odd entry is never
        mistaken for a loopback one.

    Examples
    --------
    >>> is_loopback_origin("http://localhost:8080")
    True
    >>> is_loopback_origin("http://127.0.0.1:8080")
    True
    >>> is_loopback_origin("http://[::1]:8080")
    True
    >>> is_loopback_origin("http://172.16.101.42:8077")
    False
    >>> is_loopback_origin("not an origin")
    False
    """
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_origin(raw: str) -> str | None:
    """Reduce what an operator typed to an origin: ``scheme://host[:port]``.

    Accepts the address as copied from a browser's address bar, so a trailing
    slash or a path is dropped. The scheme and host are lower-cased, as a
    browser sends them.

    Parameters
    ----------
    raw : str
        For example ``http://172.16.101.42:8077/index.html``.

    Returns
    -------
    str | None
        The origin, or ``None`` when *raw* is not an ``http``/``https``
        address with a host.

    Examples
    --------
    >>> parse_origin(" http://172.16.101.42:8077/ ")
    'http://172.16.101.42:8077'
    >>> parse_origin("HTTP://Example.org:8080/a/b?c=1")
    'http://example.org:8080'
    >>> parse_origin("172.16.101.42:8077") is None
    True
    >>> parse_origin("ftp://host") is None
    True
    """
    try:
        parts = urlsplit(raw.strip())
        netloc = parts.netloc.lower()
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or "@" in netloc:
        return None
    return f"{parts.scheme.lower()}://{netloc}"
