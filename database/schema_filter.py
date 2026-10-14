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

Discovery only
--------------
The exclusion decides which catalogue tables a tool *proposes, drafts or
reports*. It never hides a table that ``schema.yaml`` already lists: those are
synced exactly as before, and the deployment preflight reports one that sits in
an excluded schema (:func:`core.config_doctor.tables_in_excluded_schemas`) so
the contradiction is visible instead of silent.

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
    "SYSTEM_SCHEMAS",
    "SchemaExclusion",
    "exclusion_patterns",
    "is_system_schema",
    "make_schema_filter",
    "parse_patterns",
    "schema_exclusion",
    "schema_is_excluded",
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

    Examples
    --------
    >>> rule = SchemaExclusion(configured=("Scratch*",), cli=("*_Tmp",))
    >>> rule.reason("INFORMATION_SCHEMA"), rule.reason("scratch1"), rule.reason("a_tmp")
    ('system', 'configured', 'cli')
    >>> rule.reason("sales") is None, rule.excludes("sales")
    (True, False)
    """

    configured: tuple[str, ...] = ()
    cli: tuple[str, ...] = ()

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
    """
    return SchemaExclusion(
        configured=_distinct(_configured_patterns(source, settings)),
        cli=_distinct(extra),
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
