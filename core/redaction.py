# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Credential redaction for database connection strings.

One implementation, used wherever a connection string is shown, logged or
written: the setup wizard (``setup_project.py``), the schema inspector
(``database/schema_inspector.py``) and the application-database refusal
messages (``appdb/engine.py``). It used to live in the wizard, while the
inspector carried a regular expression that left a password containing
``@`` or ``:`` half visible and never touched ``PWD=`` in an
``odbc_connect`` query value.

Public API
----------
redact_db_url(url) -> str
    The URL with every credential masked; never raises.
url_secrets(url) -> list[str]
    The credential values found in a URL, longest first.
scrub_secrets(text, db_url) -> str
    *text* with the URL and any credential from it masked.

Examples
--------
>>> redact_db_url("mssql+pyodbc://nlq:s3cret@db1/Sales")
'mssql+pyodbc://nlq:***@db1/Sales'
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "MASK",
    "UNPARSEABLE_URL",
    "redact_db_url",
    "scrub_secrets",
    "url_secrets",
]

#: Placeholder shown when a connection string cannot be parsed at all.
UNPARSEABLE_URL = "<unparseable connection URL>"

#: Mask substituted for every credential.
MASK = "***"

#: Query-string keys whose value is a credential, compared lower-cased.
_SECRET_QUERY_KEYS = frozenset(
    {"pwd", "password", "passwd", "secret", "token", "access_token", "api_key", "apikey"}
)

#: ``PWD=...`` / ``Password=...`` inside an ODBC connection string (the
#: ``odbc_connect`` query value of an ``mssql+pyodbc`` URL carries the whole
#: string, password included).
_ODBC_SECRET_RE = re.compile(r"(?i)(\b(?:pwd|password|passwd)\s*=\s*)[^;&]*")

#: ``://user:password@`` in a string that could not be parsed unambiguously.
#: Greedy up to the last ``@`` before the query string, because a password
#: may itself contain ``@``, ``:`` or ``/``; a host, port or database name
#: never contains ``@``. Over-masking (an ``@`` inside a query value) is
#: the safe direction.
_RAW_USERINFO_RE = re.compile(r"(?<=://)([^:/@\s]*):([^?]*)@(?=[^@?]*(?:\?|$))")


def redact_db_url(url: str) -> str:
    """Return *url* with every credential masked, safe to print or write.

    The URL is parsed by SQLAlchemy (``make_url(...).render_as_string(
    hide_password=True)``) rather than matched with a pattern, so a password
    containing ``@``, ``:`` or ``/`` is still masked. Credentials carried in
    the query string (``?PWD=...``, or inside ``odbc_connect``) are masked as
    well. A string SQLAlchemy cannot parse is never returned as it came: a
    string that does not look like a URL becomes a placeholder, and one that
    does has its ``user:password@`` and ``PWD=`` parts masked by pattern.

    Args:
        url: A SQLAlchemy connection string, possibly with credentials.

    Returns:
        The same URL with the password (and any secret query value) replaced
        by ``***``, or a placeholder when nothing safe can be shown.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> redact_db_url("mssql+pyodbc://nlq:s3cret@db1/Sales")
        'mssql+pyodbc://nlq:***@db1/Sales'
        >>> "s3cret" in redact_db_url("mssql+pyodbc://nlq:p@s3cret@db1/Sales")
        False
        >>> "s3cret" in redact_db_url("mssql+pyodbc:///?odbc_connect=UID=a;PWD=s3cret")
        False
        >>> redact_db_url("not a url")
        '<unparseable connection URL>'
    """
    from sqlalchemy.engine import make_url

    if not isinstance(url, str):
        return UNPARSEABLE_URL
    try:
        if url.split("?", 1)[0].count("@") > 1:
            # An unescaped "@" inside the password: SQLAlchemy ends the
            # password at the first "@" and puts the rest of it in the host
            # or the database name, where render_as_string does not mask
            # it. Mask by pattern, up to the last "@", instead.
            raise ValueError("ambiguous user information")
        parsed = make_url(url)
        query: dict[str, Any] = {}
        for key, value in parsed.query.items():
            lowered = key.lower()
            values = value if isinstance(value, tuple) else (value,)
            if lowered in _SECRET_QUERY_KEYS:
                masked = tuple(MASK for _ in values)
            else:
                masked = tuple(_ODBC_SECRET_RE.sub(rf"\1{MASK}", v) for v in values)
            query[key] = masked if isinstance(value, tuple) else masked[0]
        return parsed.set(query=query).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - never fall back to the raw string
        if "://" not in url:
            return UNPARSEABLE_URL
        masked_raw = _RAW_USERINFO_RE.sub(rf"\1:{MASK}@", url)
        return _ODBC_SECRET_RE.sub(rf"\1{MASK}", masked_raw)


def url_secrets(url: str) -> list[str]:
    """Return every credential value found in *url*, longest first.

    Used to scrub third-party error text (a driver may echo part of the
    connection string). Includes the URL-encoded spelling of the password,
    because SQLAlchemy renders it that way.

    Args:
        url: A SQLAlchemy connection string.

    Returns:
        Distinct non-empty secret strings, longest first. Empty when *url*
        cannot be parsed or carries no credential.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> url_secrets("mssql+pyodbc://nlq:s3cret@db1/Sales")
        ['s3cret']
        >>> url_secrets("sqlite:///x.db")
        []
    """
    from urllib.parse import quote, quote_plus

    from sqlalchemy.engine import make_url

    found: set[str] = {m.group(2) for m in _RAW_USERINFO_RE.finditer(url)}
    try:
        parsed = make_url(url)
        if parsed.password and "@" not in (parsed.host or ""):
            found.update({parsed.password, quote(parsed.password, safe=""),
                          quote_plus(parsed.password)})
        for key, value in parsed.query.items():
            for item in (value if isinstance(value, tuple) else (value,)):
                if key.lower() in _SECRET_QUERY_KEYS:
                    found.add(item)
                else:
                    found.update(m.group(0).split("=", 1)[1].strip()
                                 for m in _ODBC_SECRET_RE.finditer(item))
    except Exception:  # noqa: BLE001
        pass
    return sorted((s for s in found if s), key=len, reverse=True)


def scrub_secrets(text: str, db_url: str) -> str:
    """Return *text* with *db_url* and any credential from it masked.

    Args:
        text: Text about to be printed, e.g. a driver's exception message.
        db_url: The connection string the text may have come from.

    Returns:
        *text* with the raw URL replaced by its redacted form and every
        credential value replaced by ``***``.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> scrub_secrets("login failed for nlq:s3cret", "mssql+pyodbc://nlq:s3cret@db1/Sales")
        'login failed for nlq:***'
    """
    if db_url:
        text = text.replace(db_url, redact_db_url(db_url))
    for secret in url_secrets(db_url):
        text = text.replace(secret, MASK)
    return text
