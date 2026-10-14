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
redact_error_text(text) -> str
    A database or guard error message with every quoted string literal
    replaced by ``'…'`` and every run of four or more digits by ``#``, so it
    can be written to a file that leaves the server.
redact_sql_literals(sql, dialect="tsql") -> str
    SQL text with every string and numeric literal replaced by a placeholder,
    found by parsing (never by pattern).

Examples
--------
>>> redact_db_url("mssql+pyodbc://nlq:s3cret@db1/Sales")
'mssql+pyodbc://nlq:***@db1/Sales'
>>> redact_error_text("Conversion failed for the value 'Acme Ltd' (id 1234567)")
"Conversion failed for the value '…' (id #)"
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "MASK",
    "SQL_WITHHELD",
    "UNPARSEABLE_URL",
    "redact_db_url",
    "redact_error_text",
    "redact_sql_literals",
    "scrub_secrets",
    "url_secrets",
]

#: Placeholder shown when a connection string cannot be parsed at all.
UNPARSEABLE_URL = "<unparseable connection URL>"

#: Mask substituted for every credential.
MASK = "***"

#: What :func:`redact_sql_literals` returns for text it cannot parse: a
#: literal can only be found by parsing, so nothing of an unparseable
#: statement is kept.
SQL_WITHHELD = "<SQL withheld: could not be parsed to redact its literals>"

#: A single-quoted string literal, ``''`` being an escaped quote. An
#: unterminated one (a message cut short) runs to the end of the text, so a
#: truncated value is masked too.
_QUOTED_LITERAL_RE = re.compile(r"'(?:[^']|'')*(?:'|$)")

#: Four or more digits in a row (``\d`` also matches Persian and Arabic-Indic
#: digits): identifiers, amounts, national codes, dates without separators.
_LONG_DIGITS_RE = re.compile(r"\d{4,}")

#: Longest error text kept after redaction; driver messages can carry the
#: whole statement.
_ERROR_TEXT_LIMIT = 600

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


def redact_error_text(text: str | None) -> str:
    """Return *text* made safe to write into a file that leaves the server.

    A database driver or the SQL guard may quote a real value in its
    message (``Conversion failed when converting the varchar value 'Acme' to
    data type int``, a duplicate-key value, an echoed statement). The rule is
    deliberately blunt, because nothing here can tell a value from an
    identifier inside quotes:

    * every single-quoted string becomes ``'…'`` (the quotes stay, so the
      shape of the message is still readable);
    * every run of four or more digits (Latin, Persian or Arabic-Indic)
      becomes ``#``;
    * an ODBC ``PWD=``/``Password=`` value and the ``user:password@`` part of
      a connection URL are masked first, as :func:`redact_db_url` would;
    * the result is cut at 600 characters.

    The message is otherwise unchanged, so the error class and the words
    around the value survive.

    Args:
        text: An error message, or ``None``.

    Returns:
        The redacted text; ``""`` for ``None``.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> redact_error_text("Invalid column name 'Foo'.")
        "Invalid column name '…'."
        >>> redact_error_text("value 'it''s' and 'x' at row 12345")
        "value '…' and '…' at row #"
        >>> redact_error_text("national code ۱۲۳۴۵۶۷۸۹۰ not found")
        'national code # not found'
        >>> redact_error_text("year 2024 limit 999 rows 12")
        'year # limit 999 rows 12'
        >>> redact_error_text("cut off 'Acme Lt")
        "cut off '…"
        >>> redact_error_text("login: UID=a;PWD=s3cret;")
        'login: UID=a;PWD=***;'
        >>> redact_error_text(None)
        ''
    """
    if not text:
        return ""
    out = _ODBC_SECRET_RE.sub(rf"\1{MASK}", str(text))
    out = _RAW_USERINFO_RE.sub(rf"\1:{MASK}@", out)
    out = _QUOTED_LITERAL_RE.sub(_mask_quoted, out)
    out = _LONG_DIGITS_RE.sub("#", out)
    if len(out) > _ERROR_TEXT_LIMIT:
        out = out[:_ERROR_TEXT_LIMIT] + "…"
    return out


def _mask_quoted(match: re.Match[str]) -> str:
    """The replacement for one quoted literal: ``'…'``, unterminated or not."""
    return "'…'" if match.group(0).endswith("'") and len(match.group(0)) > 1 else "'…"


def redact_sql_literals(sql: str | None, dialect: str = "tsql") -> str:
    """Return *sql* with every string and numeric literal replaced by a placeholder.

    The statement is parsed with sqlglot and the literals are found in the
    syntax tree, never by pattern: a regular expression cannot tell the
    ``'`` of a string from the ``'`` inside a comment or a bracketed name.
    A string literal (including ``N'...'``, hex and bit strings) becomes
    ``:str`` and a number becomes ``:num``. The one exception is an ordinal
    in ``ORDER BY`` / ``GROUP BY`` (``ORDER BY 2``), which names a column
    position and is kept. Comments are dropped. The text is regenerated by
    sqlglot, so its spacing and keyword case are normalised.

    Text that does not parse is not guessed at: it is replaced whole by
    :data:`SQL_WITHHELD`.

    Args:
        sql: A SQL statement (or several), or ``None``.
        dialect: The sqlglot dialect to read and write; ``tsql`` by default.

    Returns:
        The redacted SQL; ``""`` for ``None`` or blank input.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> redact_sql_literals("SELECT TOP 5 Name FROM T WHERE City = 'Oslo' AND Year = 2024 ORDER BY 1")
        'SELECT TOP (:num) Name FROM T WHERE City = :str AND Year = :num ORDER BY 1'
        >>> redact_sql_literals("SELECT N'x' AS a, 0x1F AS b FROM T")
        'SELECT :str AS a, :str AS b FROM T'
        >>> redact_sql_literals("SELECT ( FROM")
        '<SQL withheld: could not be parsed to redact its literals>'
        >>> redact_sql_literals(None)
        ''
    """
    if sql is None or not str(sql).strip():
        return ""
    try:
        import sqlglot
        from sqlglot import exp

        trees = [t for t in sqlglot.parse(str(sql), dialect=dialect) if t is not None]
        if not trees:
            return SQL_WITHHELD
        string_like = (
            exp.National, exp.HexString, exp.BitString, exp.ByteString, exp.RawString,
        )

        def _replace(node: "exp.Expression") -> "exp.Expression":
            if isinstance(node, string_like):
                return exp.Placeholder(this="str")
            if isinstance(node, exp.Literal):
                if node.is_string:
                    return exp.Placeholder(this="str")
                parent = node.parent
                if isinstance(parent, (exp.Ordered, exp.Group)):
                    return node  # a column position, not a value
                return exp.Placeholder(this="num")
            return node

        return "; ".join(
            tree.transform(_replace).sql(dialect=dialect, comments=False) for tree in trees
        )
    except Exception:  # noqa: BLE001 - never fall back to the raw text
        return SQL_WITHHELD
