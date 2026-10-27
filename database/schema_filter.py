# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Which database schemas the setup tools do not scan.

One rule, kept in configuration, shared by every tool that *discovers* tables
in a catalogue: the first-install inspector
(:mod:`database.schema_inspector_cli`), ``scripts/sync_schema.py`` (its
"in the database, not in schema.yaml" report and ``--add-tables``) and the
setup wizard. A schema is excluded when its name matches any of these
``fnmatch`` patterns, compared case-insensitively on every operating system:

1. :data:`SYSTEM_SCHEMAS`, always: the catalogue schemas, the ``guest``
   schema, change-data-capture's ``cdc`` and SQL Server's nine fixed
   database-role schemas. Exact names, not ``db_*``, so a user schema called
   ``db_sales`` is still scanned.
2. ``EXCLUDE_SCHEMAS`` (:attr:`config.Settings.exclude_schemas`), for every
   data source. A deployment without ``datasources.yaml`` (one
   ``DB_CONNECTION_URL``) has the same control as one with several sources.
3. The ``exclude_schemas:`` list of the data source being read
   (:attr:`database.datasources.DataSourceDefinition.exclude_schemas`).
4. Patterns given for one run (``--exclude-schemas`` of a command line).

Tables, and staging schemas that are kept
-----------------------------------------
A schema is often wanted as a whole except for a few tables, or the other way
round, so two more lists sit beside the schema patterns:

* ``EXCLUDE_TABLES`` (:attr:`config.Settings.exclude_tables`) and the source's
  ``exclude_tables:`` are ``fnmatch`` patterns compared, case-insensitively,
  with ``schema.table`` (:func:`table_is_excluded`): ``X_Fact.Order`` is one
  table, ``X_Stg.*_Delete`` the change-capture tables of one schema,
  ``*.Order`` a table of that name anywhere. A pattern with no ``.`` is
  compared with the table name alone. They are used by the same tools, in the
  same places, as the schema patterns, and are reported the same way.
* ``keep_staging_schemas:`` of a source names schemas that the schema patterns
  of ``STAGING_PATTERNS`` (``*_Stg``, ``*stg*``, ``*staging*``) must not drop,
  so a staging schema can be scanned while its change-capture tables are still
  left out by the ``table:`` patterns of ``STAGING_PATTERNS`` or by
  ``exclude_tables:`` (:meth:`SchemaExclusion.keeps_staging`). A schema in
  ``exclude_schemas`` stays excluded: the keep list only lifts the staging rule.

Discovery only
--------------
The exclusion decides which catalogue tables a tool *proposes, drafts or
reports*. It never hides a table that ``schema.yaml`` already lists: those are
synced exactly as before, and the deployment preflight reports one that sits in
an excluded schema or matches an excluded table
(:func:`core.config_doctor.tables_in_excluded_schemas`,
:func:`core.config_doctor.tables_matching_excluded_tables`) so the
contradiction is visible instead of silent.

Why it is recorded
------------------
A flag has to be repeated on every run and by every operator; a line in
``.env`` or ``datasources.yaml`` is reviewed with the rest of the
configuration. The reason it exists: a first draft built under a wide login
listed about 150 change-capture, temporary and staging tables the application's
own login cannot even query, and they ended up in the live ``schema.yaml``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings

__all__ = [
    "RULE_CLI",
    "RULE_CONFIGURED",
    "RULE_SYSTEM",
    "RULE_TABLES",
    "SYSTEM_SCHEMAS",
    "SchemaExclusion",
    "exclude_schemas_env_line",
    "exclude_tables_env_line",
    "exclusion_patterns",
    "is_system_schema",
    "make_schema_filter",
    "parse_patterns",
    "schema_exclusion",
    "schema_is_excluded",
    "table_exclusion_patterns",
    "table_is_excluded",
]

#: Schemas no tool ever scans, as ``fnmatch`` patterns compared without regard
#: to case. SQL Server: ``sys``, ``INFORMATION_SCHEMA``, ``guest``, ``cdc`` and
#: the nine fixed database-role schemas, by exact name (a user schema called
#: ``db_sales`` is not lost). PostgreSQL: ``pg_catalog``, ``pg_toast`` and every
#: ``pg_*`` schema.
SYSTEM_SCHEMAS: tuple[str, ...] = (
    "sys",
    "INFORMATION_SCHEMA",
    "guest",
    "cdc",
    "db_owner",
    "db_accessadmin",
    "db_securityadmin",
    "db_ddladmin",
    "db_backupoperator",
    "db_datareader",
    "db_datawriter",
    "db_denydatareader",
    "db_denydatawriter",
    "pg_catalog",
    "pg_toast",
    "pg_*",
)

#: The names :meth:`SchemaExclusion.reason` returns.
RULE_SYSTEM = "system"
RULE_CONFIGURED = "configured"
RULE_CLI = "cli"
#: What :func:`SchemaExclusion.table_reason` returns: ``EXCLUDE_TABLES`` or the source's ``exclude_tables:``.
RULE_TABLES = "configured-tables"


def schema_is_excluded(schema: str, patterns: Iterable[str] | None) -> bool:
    """Whether *schema* matches any ``fnmatch`` pattern in *patterns*.

    Matching is case-insensitive and does not depend on the operating system
    (``fnmatch.fnmatchcase`` on lower-cased text), so ``*_Stg`` excludes
    ``Sales_Stg`` and ``sales_stg`` alike on every platform.

    Examples
    --------
    >>> schema_is_excluded("Sales_Stg", ["*_stg"])
    True
    >>> schema_is_excluded("sales", ["*_stg", "Replay"])
    False
    >>> schema_is_excluded("replay", ["*_stg", "Replay"])
    True
    >>> schema_is_excluded("sales", None)
    False
    """
    name = schema.lower()
    return any(fnmatch.fnmatchcase(name, p.lower()) for p in patterns or ())


def table_is_excluded(schema: str, table: str, patterns: Iterable[str] | None) -> bool:
    """Whether the table *table* of *schema* matches any ``fnmatch`` pattern in *patterns*.

    A pattern with a ``.`` is compared with ``schema.table``; one without is
    compared with the table name alone, so ``Order`` and ``*_Delete`` apply in
    every schema. Comparison is case-insensitive and does not depend on the
    operating system. A table with no schema (``""``) is compared as its bare name.

    Examples
    --------
    >>> table_is_excluded("X_Fact", "Order", ["X_Fact.Order"])
    True
    >>> table_is_excluded("x_stg", "Trade_Delete", ["X_Stg.*_Delete"])
    True
    >>> table_is_excluded("sales", "Order", ["X_Fact.Order"])
    False
    >>> table_is_excluded("sales", "Order", ["*.Order"]), table_is_excluded("ref", "order", ["Order"])
    (True, True)
    >>> table_is_excluded("sales", "Order_Lines", ["Order"]), table_is_excluded("sales", "Order", None)
    (False, False)
    >>> table_is_excluded("", "Order", ["Order", "x.Order"])
    True
    """
    bare = table.lower()
    qualified = f"{schema}.{table}".lower() if schema else bare
    for pattern in patterns or ():
        low = pattern.lower()
        if fnmatch.fnmatchcase(qualified if "." in low else bare, low):
            return True
    return False


def is_system_schema(schema: str) -> bool:
    """Whether *schema* is one of :data:`SYSTEM_SCHEMAS` (any letter case).

    Examples
    --------
    >>> is_system_schema("INFORMATION_SCHEMA"), is_system_schema("information_schema")
    (True, True)
    >>> is_system_schema("CDC"), is_system_schema("db_datareader"), is_system_schema("pg_temp_3")
    (True, True, True)
    >>> is_system_schema("db_sales"), is_system_schema("dbo"), is_system_schema("sales")
    (False, False, False)
    """
    return schema_is_excluded(schema, SYSTEM_SCHEMAS)


def parse_patterns(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated pattern list (``EXCLUDE_SCHEMAS``, ``--exclude-schemas``).

    Blank items are dropped and the rest trimmed.

    Examples
    --------
    >>> parse_patterns("*_Stg, Replay ,,Alerts")
    ('*_Stg', 'Replay', 'Alerts')
    >>> parse_patterns("") + parse_patterns(None)
    ()
    """
    return tuple(item.strip() for item in (raw or "").split(",") if item.strip())


def _distinct(patterns: Iterable[str]) -> tuple[str, ...]:
    """*patterns* without repeats (compared in lower case), in order."""
    seen: set[str] = set()
    out: list[str] = []
    for pattern in patterns:
        if pattern.lower() not in seen:
            seen.add(pattern.lower())
            out.append(pattern)
    return tuple(out)


def _configured_patterns(source: str | None, settings: "Settings | None") -> tuple[str, ...]:
    """``EXCLUDE_SCHEMAS`` followed by *source*'s ``exclude_schemas:``."""
    import config as cfg
    from database.datasources import datasource_exclude_schemas

    active = cfg.settings if settings is None else settings
    patterns = list(parse_patterns(active.exclude_schemas))
    if source is not None:
        patterns.extend(datasource_exclude_schemas(active).get(source, ()))
    return tuple(patterns)


def table_exclusion_patterns(
    source: str | None,
    settings: "Settings | None" = None,
) -> tuple[str, ...]:
    """The table patterns that keep a table out of discovery for *source*.

    The ``EXCLUDE_TABLES`` setting followed by *source*'s ``exclude_tables:``,
    without repeats (compared in lower case).

    Parameters
    ----------
    source:
        A data source name; ``None`` for tools that read no particular source
        (only the global setting applies).
    settings:
        The settings to read ``EXCLUDE_TABLES`` and ``PROJECT_CONFIG_DIR``
        from; the process-wide ones when ``None``.

    Returns
    -------
    tuple[str, ...]
        ``fnmatch`` patterns, see :func:`table_is_excluded`.

    Raises
    ------
    ValueError
        If ``datasources.yaml`` exists but is invalid.

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(project_config_dir="/nonexistent", exclude_tables="*.Order, X_Stg.*_Delete"):
    ...     table_exclusion_patterns(None)
    ('*.Order', 'X_Stg.*_Delete')
    """
    import config as cfg
    from database.datasources import datasource_exclude_tables

    active = cfg.settings if settings is None else settings
    patterns = list(parse_patterns(active.exclude_tables))
    if source is not None:
        patterns.extend(datasource_exclude_tables(active).get(source, ()))
    return _distinct(patterns)


def _keep_staging_patterns(source: str | None, settings: "Settings | None") -> tuple[str, ...]:
    """*source*'s ``keep_staging_schemas:`` (none without a source)."""
    import config as cfg
    from database.datasources import datasource_keep_staging_schemas

    if source is None:
        return ()
    active = cfg.settings if settings is None else settings
    return _distinct(datasource_keep_staging_schemas(active).get(source, ()))


def exclusion_patterns(
    source: str | None,
    extra: Sequence[str] = (),
    settings: "Settings | None" = None,
) -> tuple[str, ...]:
    """Every pattern that keeps a schema out of discovery for *source*.

    The system schemas, the global ``EXCLUDE_SCHEMAS`` setting, *source*'s own
    ``exclude_schemas:`` and *extra* (the patterns of one command line), in
    that order and without repeats.

    Parameters
    ----------
    source:
        A data source name; ``None`` for tools that read no particular source
        (only the system schemas and the global setting apply).
    extra:
        Patterns for this run only.
    settings:
        The settings to read ``EXCLUDE_SCHEMAS`` and ``PROJECT_CONFIG_DIR``
        from; the process-wide ones when ``None``.

    Returns
    -------
    tuple[str, ...]
        ``fnmatch`` patterns, compared case-insensitively.

    Raises
    ------
    ValueError
        If ``datasources.yaml`` exists but is invalid.

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(project_config_dir="/nonexistent", exclude_schemas="Scratch*"):
    ...     patterns = exclusion_patterns(None, ["*_Tmp"])
    >>> patterns[:2], patterns[-2:]
    (('sys', 'INFORMATION_SCHEMA'), ('Scratch*', '*_Tmp'))
    """
    return _distinct([*SYSTEM_SCHEMAS, *_configured_patterns(source, settings), *extra])


@dataclass(frozen=True)
class SchemaExclusion:
    """The exclusion rule of one data source, with the reason a schema is skipped.

    Attributes
    ----------
    configured:
        ``EXCLUDE_SCHEMAS`` plus the source's ``exclude_schemas:``.
    cli:
        Patterns given for this run.
    tables:
        ``EXCLUDE_TABLES`` plus the source's ``exclude_tables:``: patterns on
        ``schema.table`` (:func:`table_is_excluded`).
    keep_staging:
        The source's ``keep_staging_schemas:``: schemas the schema patterns of
        ``STAGING_PATTERNS`` do not apply to.

    Examples
    --------
    >>> rule = SchemaExclusion(configured=("Scratch*",), cli=("*_Tmp",))
    >>> rule.reason("INFORMATION_SCHEMA"), rule.reason("scratch1"), rule.reason("a_tmp")
    ('system', 'configured', 'cli')
    >>> rule.reason("sales") is None, rule.excludes("sales")
    (True, False)
    >>> rule = SchemaExclusion(tables=("X_Stg.*_Delete",), keep_staging=("X_Stg",))
    >>> rule.table_excluded("x_stg", "Trade_Delete"), rule.table_excluded("x_stg", "Trade")
    (True, False)
    >>> rule.keeps_staging("X_STG"), rule.keeps_staging("Y_Stg")
    (True, False)
    """

    configured: tuple[str, ...] = ()
    cli: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    keep_staging: tuple[str, ...] = ()

    def reason(self, schema: str) -> str | None:
        """Which rule skips *schema*: ``system``, ``configured`` or ``cli`` (``None`` for none).

        A schema several rules match is attributed to the first, in that order.
        """
        if is_system_schema(schema):
            return RULE_SYSTEM
        if schema_is_excluded(schema, self.configured):
            return RULE_CONFIGURED
        if schema_is_excluded(schema, self.cli):
            return RULE_CLI
        return None

    def excludes(self, schema: str) -> bool:
        """Whether *schema* is skipped by any rule."""
        return self.reason(schema) is not None

    def table_excluded(self, schema: str, table: str) -> bool:
        """Whether the configured table patterns (:attr:`tables`) skip *table* of *schema*."""
        return table_is_excluded(schema, table, self.tables)

    def keeps_staging(self, schema: str) -> bool:
        """Whether *schema* is one the schema patterns of ``STAGING_PATTERNS`` must not drop."""
        return schema_is_excluded(schema.rsplit(".", 1)[-1], self.keep_staging)

    @property
    def patterns(self) -> tuple[str, ...]:
        """Every pattern, as :func:`exclusion_patterns` returns them."""
        return _distinct([*SYSTEM_SCHEMAS, *self.configured, *self.cli])


def schema_exclusion(
    source: str | None,
    extra: Sequence[str] = (),
    settings: "Settings | None" = None,
) -> SchemaExclusion:
    """The :class:`SchemaExclusion` of *source* (see :func:`exclusion_patterns`).

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(project_config_dir="/nonexistent", exclude_schemas="Scratch*"):
    ...     rule = schema_exclusion(None, ["*_Tmp"])
    >>> rule.configured, rule.cli
    (('Scratch*',), ('*_Tmp',))
    >>> rule.tables, rule.keep_staging
    ((), ())
    """
    return SchemaExclusion(
        configured=_distinct(_configured_patterns(source, settings)),
        cli=_distinct(extra),
        tables=table_exclusion_patterns(source, settings),
        keep_staging=_keep_staging_patterns(source, settings),
    )


def make_schema_filter(
    source: str | None,
    extra: Sequence[str] = (),
    settings: "Settings | None" = None,
) -> Callable[[str], bool]:
    """A predicate ``schema -> excluded`` for *source*, computed once.

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(project_config_dir="/nonexistent", exclude_schemas=""):
    ...     excluded = make_schema_filter(None, ["*_Stg"])
    >>> excluded("Sales_Stg"), excluded("cdc"), excluded("sales")
    (True, True, False)
    """
    patterns = exclusion_patterns(source, extra, settings)
    return lambda schema: schema_is_excluded(schema, patterns)


def exclude_schemas_env_line(
    extra: Sequence[str],
    settings: "Settings | None" = None,
) -> str:
    """The ``.env`` line that records *extra* next to the current ``EXCLUDE_SCHEMAS``.

    A deployment without ``datasources.yaml`` keeps its schema exclusion in one
    variable. A tool that learns of more schemas to leave out (the setup
    wizard, after an operator names them) prints this line instead of editing
    ``.env``: the patterns already set come first, then *extra*, without
    repeats (compared in lower case).

    Parameters
    ----------
    extra:
        Patterns to add.
    settings:
        The settings to read ``EXCLUDE_SCHEMAS`` from; the process-wide ones
        when ``None``.

    Returns
    -------
    str
        ``EXCLUDE_SCHEMAS=a,b,c``, ready to paste.

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(exclude_schemas="Scratch*"):
    ...     exclude_schemas_env_line(["*_Tmp", "scratch*"])
    'EXCLUDE_SCHEMAS=Scratch*,*_Tmp'
    >>> with cfg.override_settings(exclude_schemas=""):
    ...     exclude_schemas_env_line(["Replay"])
    'EXCLUDE_SCHEMAS=Replay'
    """
    import config as cfg

    active = cfg.settings if settings is None else settings
    patterns = _distinct([*parse_patterns(active.exclude_schemas), *extra])
    return "EXCLUDE_SCHEMAS=" + ",".join(patterns)


def exclude_tables_env_line(
    extra: Sequence[str],
    settings: "Settings | None" = None,
) -> str:
    """The ``.env`` line that records *extra* next to the current ``EXCLUDE_TABLES``.

    The counterpart of :func:`exclude_schemas_env_line` for a deployment
    without ``datasources.yaml``: the patterns already set come first, then
    *extra*, without repeats (compared in lower case).

    Parameters
    ----------
    extra:
        Patterns to add.
    settings:
        The settings to read ``EXCLUDE_TABLES`` from; the process-wide ones
        when ``None``.

    Returns
    -------
    str
        ``EXCLUDE_TABLES=a,b,c``, ready to paste.

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(exclude_tables="*.Order"):
    ...     exclude_tables_env_line(["X_Stg.*_Delete", "*.order"])
    'EXCLUDE_TABLES=*.Order,X_Stg.*_Delete'
    >>> with cfg.override_settings(exclude_tables=""):
    ...     exclude_tables_env_line(["Order"])
    'EXCLUDE_TABLES=Order'
    """
    import config as cfg

    active = cfg.settings if settings is None else settings
    patterns = _distinct([*parse_patterns(active.exclude_tables), *extra])
    return "EXCLUDE_TABLES=" + ",".join(patterns)
