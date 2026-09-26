# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Identify this application to the warehouse in its own connection string.

Finding 5, 2026 warehouse-load audit
-------------------------------------
A DBA looking at ``sys.dm_exec_sessions``/``sys.dm_exec_requests`` (or a
trace) has no way to tell which sessions are this application's without
this: every ODBC connection this codebase opens shows up as whatever the
driver defaults ``program_name`` to (often just the driver name), lost
among every other client hitting the same server. Setting the ODBC
``APP`` keyword — recognised by the Microsoft ODBC Driver for SQL Server
and by FreeTDS — fixes that: it becomes the ``program_name`` a DBA sees.

Only applies when :attr:`config.Settings.db_connection_url` is an
``mssql+pyodbc`` URL (this is a SQL-Server-and-pyodbc-specific ODBC
keyword; every other backend/driver combination, including the
``sqlite`` URLs this codebase's own test suite uses, is returned
untouched) AND that URL does not already set an application name itself
(never overrides an operator's own choice). Two URL shapes are supported,
because SQLAlchemy's ``mssql+pyodbc`` dialect accepts both — see
``docs/deployment-runbook.md``'s connection-string section:

* **Query-string style** — ``mssql+pyodbc://user:pass@host/db?driver=...``.
  Adding ``APP`` here means adding one more query parameter; SQLAlchemy's
  pyodbc dialect folds every query parameter into the ODBC connection
  string it builds.
* **``odbc_connect`` style** — ``mssql+pyodbc:///?odbc_connect=<url-encoded
  ODBC connection string>``, used when the connection string itself needs
  a syntax the query-string form cannot express. Here the whole ODBC
  string is one opaque, URL-encoded blob; adding ``APP`` means decoding
  it, appending ``APP=<name>;`` if nothing already sets ``APP=`` or
  ``Application Name=``, and re-encoding.

Any URL this module cannot confidently parse (or that is not
``mssql+pyodbc`` at all) is returned byte-for-byte unchanged — this is an
attribution nicety, never something that may break a real deployment's
connection string.
"""

from __future__ import annotations

import re

from sqlalchemy.engine import make_url

#: Matches an existing "APP=" or "Application Name=" assignment inside a
#: raw ODBC connection string (odbc_connect style), case-insensitively,
#: at the start of the string or right after a ";" separator -- ODBC
#: connection strings are ";"-delimited key=value pairs (see
#: docs/db-hardening.md's own connection-string examples). Anchored on a
#: separator (rather than a bare substring search) so a value that
#: happens to CONTAIN the text "app=" -- e.g. a password -- is never
#: mistaken for the keyword itself.
_ODBC_APP_KEYWORD_RE = re.compile(r"(?:^|;)\s*(app|application name)\s*=", re.IGNORECASE)


def with_application_name(url_str: str, app_name: str) -> str:
    """Return *url_str* with an ODBC application name set, if applicable.

    See the module docstring for the two URL shapes this handles and the
    "never overrides, never touches a non-mssql+pyodbc URL" guarantees.
    Never raises: a URL this cannot parse is returned unchanged rather
    than failing engine construction over an attribution nicety.
    """
    if not app_name:
        return url_str

    try:
        url = make_url(url_str)
    except Exception:  # noqa: BLE001 - an unparsable URL is not this module's problem
        return url_str

    if url.drivername != "mssql+pyodbc":
        return url_str

    query = dict(url.query)

    odbc_connect = query.get("odbc_connect")
    if odbc_connect is not None:
        # SQLAlchemy stores query values already URL-decoded -- see
        # sqlalchemy.engine.url.URL.query's own contract -- so this is
        # the raw ODBC connection string, not a percent-encoded blob.
        raw = odbc_connect[0] if isinstance(odbc_connect, tuple) else odbc_connect
        if _ODBC_APP_KEYWORD_RE.search(raw):
            return url_str  # an application name is already set
        separator = "" if (not raw or raw.rstrip().endswith(";")) else ";"
        new_query = dict(query)
        new_query["odbc_connect"] = f"{raw}{separator}APP={app_name};"
        return url.set(query=new_query).render_as_string(hide_password=False)

    if any(k.lower() in ("app", "application name") for k in query):
        return url_str  # an application name is already set

    new_query = dict(query)
    new_query["APP"] = app_name
    return url.set(query=new_query).render_as_string(hide_password=False)
