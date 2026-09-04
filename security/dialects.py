# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Per-dialect engine data — catalogues, session setup, schema qualification.

This module is the single place genuinely per-dialect *data* lives, so that
``security/sql_guard.py``, ``database/executor.py``, and ``config.py`` never
need an ``if dialect == "tsql": ... elif dialect == "postgres": ...`` ladder
to know what a given target database calls its system catalogue, how to
bound a query's runtime at the session level, or whether it has a schema
concept at all. SQL *parsing and rendering* is entirely delegated to
`sqlglot <https://sqlglot.com/>`_ (see ``security/sql_guard.py``'s module
docstring for why a second, hand-written dialect layer next to sqlglot is
exactly the mistake this project has already been burned by once); this
module only holds the small amount of per-dialect metadata sqlglot has no
opinion about.

Supported dialects
-------------------
``tsql`` (SQL Server — the only target before this phase), ``postgres``,
``mysql``, ``sqlite``. Each is described by a :class:`DialectProfile` in
:data:`DIALECT_PROFILES`, keyed by the same dialect string
`sqlglot <https://sqlglot.com/>`_ uses for its ``read=`` / ``write=``
parameters — so :data:`config.Settings.sql_dialect` and every sqlglot call
site in this codebase speak the same vocabulary.

Fail closed, not open
-----------------------
:func:`require_dialect_supported` is called once at process start-up (see
``api/server.py``'s ``lifespan`` and ``app.py``) and raises
:class:`UnsupportedDialectError` for any dialect that is not a key of
:data:`DIALECT_PROFILES`, or whose profile has an **empty**
:attr:`DialectProfile.system_schemas` — the module docstring of
``security.sql_guard`` explains why an empty blocklist is refused rather
than silently treated as "nothing to block": a deployment that ships a new
dialect's profile with the catalogue list forgotten must fail loudly at
start-up, not quietly enumerate its own schema to the model on the first
request. A dialect with no *session-level* query-timeout mechanism (see
:attr:`DialectProfile.session_timeout_statement`) is not refused — SQLite
genuinely has none, and it is one of the four dialects this project
supports — but :func:`require_dialect_supported` logs a ``WARNING`` for it
so ``query_timeout_seconds`` silently not being enforced is visible in the
deployment's own logs rather than a fact only this module knows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

#: Schema-qualification styles a dialect can have. ``"schema"`` -- a real
#: schema namespace distinct from the database (SQL Server, PostgreSQL).
#: ``"database"`` -- MySQL has no schema separate from the database itself;
#: "qualifying" a table means naming its database, not a second-level
#: schema. ``"none"`` -- SQLite has neither concept; a table reference is
#: never qualified at all.
SchemaQualificationStyle = Literal["schema", "database", "none"]


@dataclass(frozen=True, slots=True)
class DialectProfile:
    """Per-dialect metadata sqlglot itself has no opinion about.

    Every field here is genuinely per-dialect *data* — see the module
    docstring's "Do not build a dialect layer" cross-reference
    (``docs`` aside: the actual rule lives in the multi-dialect phase
    report). Nothing in this class parses or renders SQL; that stays
    entirely inside sqlglot.
    """

    #: The sqlglot dialect key (``"tsql"``, ``"postgres"``, ``"mysql"``,
    #: ``"sqlite"``) — also what :data:`config.Settings.sql_dialect` and
    #: every sqlglot ``read=``/``write=`` call in this codebase expects.
    name: str

    #: Schema/database qualifiers that name a system catalogue outright
    #: (checked case-insensitively against ``exp.Table.db``, matching the
    #: exact-match half of the original ``_SYSTEM_SCHEMAS`` check). Must be
    #: non-empty for a dialect to be accepted by
    #: :func:`require_dialect_supported` — see that function's docstring.
    system_schemas: frozenset[str]

    #: A ``str.format()`` template (with a ``{timeout_ms}`` placeholder)
    #: for the statement that bounds how long a session will wait/run
    #: before this dialect's own query-timeout mechanism kicks in, executed
    #: once per connection before the query itself — T-SQL's
    #: ``SET LOCK_TIMEOUT``, PostgreSQL's ``SET statement_timeout``,
    #: MySQL's ``SET SESSION MAX_EXECUTION_TIME``. ``None`` when the
    #: dialect has no such session-level mechanism at all (SQLite) — see
    #: :func:`require_dialect_supported` for why that is logged, not
    #: refused.
    session_timeout_statement: str | None

    #: Attribute name to set (to ``query_timeout_seconds``) directly on the
    #: raw DBAPI connection object for a *driver-level* timeout, beyond the
    #: session-level statement above — pyodbc's ``Connection.timeout``, for
    #: SQL Server. ``None`` when the driver this project has actually used
    #: for the dialect exposes no equivalent attribute (verified for
    #: sqlite3; not yet verified for psycopg2/PyMySQL, since no live
    #: PostgreSQL/MySQL server was available to confirm this attribute
    #: does something real rather than silently no-op — see the
    #: multi-dialect phase report). Left ``None`` rather than guessed.
    driver_level_timeout_attr: str | None

    #: How a table reference is qualified for this dialect, for
    #: :func:`~schema_data.registry.get_table_schema_qualifiers` consumers
    #: (``retrieval.value_resolver``, ``retrieval.dimension_vocabulary``)
    #: to decide whether ``schema.yaml``'s per-table ``db_schema`` applies.
    schema_qualification: SchemaQualificationStyle

    #: Whether this dialect accepts a T-SQL national-character literal
    #: (``N'...'``) as valid syntax. ``True`` for tsql (obviously) and
    #: MySQL (which treats ``N'...'`` as a synonym for ``_utf8'...'``);
    #: ``False`` for PostgreSQL and SQLite, which reject it outright as a
    #: syntax error. This is the finding that shapes this whole phase: a
    #: sqlglot transpile leaves ``N'...'`` **completely untouched** in
    #: every target dialect (confirmed by direct execution, not by
    #: reading sqlglot's source) -- it is not converted to a plain
    #: string literal, so a query that filters on a national-character
    #: literal (essentially every query this deployment generates, since
    #: it filters on Persian names) parses "successfully" as any dialect
    #: (sqlglot's own parser accepts ``N'...'`` regardless of the
    #: ``read=`` dialect given) but fails at the real database for a
    #: dialect where this field is ``False`` -- exactly the "looked right,
    #: failed/meant something different at execution" risk this phase's
    #: verification exists to catch. Deliberately has **no default value**
    #: (every profile must set it explicitly): an unset value defaulting
    #: to "assume it works" would be the wrong failure direction, for
    #: exactly the same reason :attr:`system_schemas` must never be empty
    #: by accident.
    supports_national_literal: bool

    #: Identifier *prefixes* (uppercased, no trailing ``*``) that mark a
    #: table/schema as a system catalogue by naming convention rather than
    #: by an exact schema name — PostgreSQL's ``pg_*`` views/tables and
    #: SQLite's ``sqlite_*`` tables both work this way; T-SQL's
    #: ``INFORMATION_SCHEMA``/``sys`` do not need one (empty tuple).
    system_name_prefixes: tuple[str, ...] = field(default_factory=tuple)

    #: Additional remote/file-access function or table-valued-function
    #: names to refuse outright, beyond the dialect-agnostic
    #: ``OPENROWSET``/``OPENQUERY``/``OPENDATASOURCE``/``xp_*``/``sp_*``
    #: set every dialect already refuses (see
    #: ``security.sql_guard._DANGEROUS_FUNCTION_NAMES``). E.g. PostgreSQL's
    #: ``dblink``/``dblink_connect`` (cross-server access) and MySQL's
    #: ``LOAD_FILE`` (local file read). Uppercased. Empty when the dialect
    #: has no well-known equivalent worth naming explicitly (SQLite).
    extra_dangerous_functions: frozenset[str] = frozenset()

    #: One-line human-readable note on the schema-qualification behaviour,
    #: surfaced in start-up logs / error messages rather than repeating the
    #: reasoning at every call site.
    schema_qualification_note: str = ""


DIALECT_PROFILES: dict[str, DialectProfile] = {
    "tsql": DialectProfile(
        name="tsql",
        system_schemas=frozenset({"INFORMATION_SCHEMA", "SYS"}),
        system_name_prefixes=(),
        extra_dangerous_functions=frozenset(),
        session_timeout_statement="SET LOCK_TIMEOUT {timeout_ms}",
        driver_level_timeout_attr="timeout",
        schema_qualification="schema",
        supports_national_literal=True,
        schema_qualification_note=(
            "SQL Server has a real schema namespace distinct from the "
            "database -- schema.yaml's db_schema is rendered as "
            "[db_schema].[table]."
        ),
    ),
    "postgres": DialectProfile(
        name="postgres",
        system_schemas=frozenset({"INFORMATION_SCHEMA", "PG_CATALOG"}),
        system_name_prefixes=("PG_",),
        extra_dangerous_functions=frozenset({"DBLINK", "DBLINK_CONNECT", "DBLINK_EXEC"}),
        session_timeout_statement="SET statement_timeout = {timeout_ms}",
        driver_level_timeout_attr=None,
        schema_qualification="schema",
        supports_national_literal=False,
        schema_qualification_note=(
            "PostgreSQL has a real schema namespace, same shape as T-SQL's "
            "-- schema.yaml's db_schema is rendered as \"db_schema\".\"table\"."
        ),
    ),
    "mysql": DialectProfile(
        name="mysql",
        system_schemas=frozenset({"INFORMATION_SCHEMA", "MYSQL", "PERFORMANCE_SCHEMA", "SYS"}),
        system_name_prefixes=(),
        extra_dangerous_functions=frozenset({"LOAD_FILE"}),
        session_timeout_statement="SET SESSION MAX_EXECUTION_TIME = {timeout_ms}",
        driver_level_timeout_attr=None,
        schema_qualification="database",
        supports_national_literal=True,
        schema_qualification_note=(
            "MySQL has no schema separate from the database -- "
            "schema.yaml's db_schema, if set, names the DATABASE a table "
            "lives in (rendered as `db_schema`.`table`), not a second-level "
            "schema; a deployment with a single database should leave "
            "db_schema empty for MySQL tables."
        ),
    ),
    "sqlite": DialectProfile(
        name="sqlite",
        system_schemas=frozenset({"SQLITE_MASTER", "SQLITE_TEMP_MASTER", "SQLITE_SEQUENCE"}),
        system_name_prefixes=("SQLITE_",),
        extra_dangerous_functions=frozenset(),
        session_timeout_statement=None,
        driver_level_timeout_attr=None,
        schema_qualification="none",
        supports_national_literal=False,
        schema_qualification_note=(
            "SQLite has neither a schema nor a database-qualification "
            "concept for a single-file database -- schema.yaml's "
            "db_schema is never rendered for SQLite tables."
        ),
    ),
}


class UnsupportedDialectError(ValueError):
    """Raised by :func:`get_dialect_profile` / :func:`require_dialect_supported`
    for a dialect this codebase does not (or does not yet safely) support."""


def get_dialect_profile(dialect: str) -> DialectProfile:
    """Return the :class:`DialectProfile` for *dialect*.

    Parameters
    ----------
    dialect:
        A sqlglot dialect key, case-sensitive (``"tsql"``, ``"postgres"``,
        ``"mysql"``, ``"sqlite"``).

    Raises
    ------
    UnsupportedDialectError
        If *dialect* is not a key of :data:`DIALECT_PROFILES`.
    """
    profile = DIALECT_PROFILES.get(dialect)
    if profile is None:
        raise UnsupportedDialectError(
            f"Unsupported SQL dialect {dialect!r} -- this deployment only "
            f"supports {sorted(DIALECT_PROFILES)}. Add a DialectProfile to "
            f"security.dialects.DIALECT_PROFILES before configuring "
            f"SQL_DIALECT={dialect!r}."
        )
    return profile


def require_dialect_supported(dialect: str) -> DialectProfile:
    """Fail-closed start-up check: refuse a dialect with no catalogue list.

    Called once at process start-up (``api/server.py``'s ``lifespan``,
    ``app.py``'s own start-up path) with
    :data:`config.Settings.sql_dialect`. Two independent checks:

    1. *dialect* must be a known :data:`DIALECT_PROFILES` key (delegated to
       :func:`get_dialect_profile`, which raises
       :class:`UnsupportedDialectError` on its own).
    2. Its profile's :attr:`DialectProfile.system_schemas` must be
       **non-empty**. An empty blocklist is indistinguishable from
       "nothing to block" from inside :func:`security.sql_guard.validate_sql`
       -- which is the failure direction that loses (a query could freely
       enumerate the whole warehouse via the target dialect's system
       catalogue). Refusing to start is the only safe response; there is
       no reasonable default to fall back to for a catalogue list nobody
       configured.

    A dialect whose :attr:`DialectProfile.session_timeout_statement` is
    ``None`` (SQLite) is **not** refused here -- that is a real, accepted
    limitation of the dialect itself, not a configuration mistake -- but is
    logged as a ``WARNING`` so ``query_timeout_seconds`` silently not being
    enforced at the session level is visible in the deployment's own
    startup logs, per this project's "a real protection must not silently
    evaporate" rule (see ``config.Settings.query_timeout_seconds``).

    Returns
    -------
    DialectProfile
        The validated profile, for the caller's convenience (so a caller
        that already needs the profile doesn't have to call
        :func:`get_dialect_profile` a second time).

    Raises
    ------
    UnsupportedDialectError
        If *dialect* is unknown, or its profile's ``system_schemas`` is
        empty.
    """
    profile = get_dialect_profile(dialect)
    if not profile.system_schemas:
        raise UnsupportedDialectError(
            f"Dialect {dialect!r} has an empty system_schemas catalogue "
            f"list configured in security.dialects.DIALECT_PROFILES -- "
            f"refusing to start rather than silently allow a query to "
            f"enumerate this warehouse's system catalogue. Configure a "
            f"non-empty system_schemas list for {dialect!r} before "
            f"deploying against it."
        )
    if profile.session_timeout_statement is None:
        logger.warning(
            "SQL dialect %r has no session-level query-timeout mechanism "
            "(DialectProfile.session_timeout_statement is None) -- "
            "query_timeout_seconds=%s is NOT enforced at the database "
            "session level for this dialect. This is a known, accepted "
            "limitation (see security.dialects.DialectProfile docstring), "
            "not a bug, but every deployment on this dialect should know "
            "about it from its own logs rather than from this module's "
            "source.",
            dialect,
            "<unresolved at this call site; see config.settings.query_timeout_seconds>",
        )
    return profile


#: Map a SQLAlchemy engine/URL "backend name" (``Engine.name`` /
#: ``sqlalchemy.engine.url.URL.get_backend_name()``) to the sqlglot dialect
#: key this codebase uses everywhere else. This is the one seam where a
#: SQLAlchemy-specific vocabulary (driver/backend names) meets sqlglot's
#: own -- kept as a single small table here rather than repeated string
#: comparisons at each call site (``database/executor.py``).
SQLALCHEMY_BACKEND_TO_SQLGLOT_DIALECT: dict[str, str] = {
    "mssql": "tsql",
    "postgresql": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
}


def sqlglot_dialect_for_backend(backend_name: str) -> str:
    """Map a SQLAlchemy backend name to this codebase's sqlglot dialect key.

    Parameters
    ----------
    backend_name:
        ``sqlalchemy.engine.Engine.name`` (or
        ``sqlalchemy.engine.url.make_url(...).get_backend_name()``) -- e.g.
        ``"mssql"``, ``"postgresql"``, ``"mysql"``, ``"sqlite"``.

    Returns
    -------
    str
        The corresponding sqlglot dialect key.

    Raises
    ------
    UnsupportedDialectError
        If *backend_name* has no entry in
        :data:`SQLALCHEMY_BACKEND_TO_SQLGLOT_DIALECT`.
    """
    dialect = SQLALCHEMY_BACKEND_TO_SQLGLOT_DIALECT.get(backend_name)
    if dialect is None:
        raise UnsupportedDialectError(
            f"No sqlglot dialect mapping for SQLAlchemy backend "
            f"{backend_name!r} -- this deployment's DB_CONNECTION_URL "
            f"names a database engine this codebase does not yet support. "
            f"Supported backends: {sorted(SQLALCHEMY_BACKEND_TO_SQLGLOT_DIALECT)}."
        )
    return dialect
