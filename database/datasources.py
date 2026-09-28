# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Registry of the warehouse data sources this deployment can query.

A *data source* is one warehouse connection: one server login, one
connection pool, one set of query limits. Several databases on the same
SQL Server instance are one data source (a query names the other
database with a three-part ``[Db].[Schema].[Table]`` reference, and
cross-database joins on that server keep working). Databases on
different servers are different data sources.

Where the configuration lives
-----------------------------
``<PROJECT_CONFIG_DIR>/datasources.yaml`` describes *what* exists; the
connection strings themselves stay in the environment (``.env``), named
by ``url_env``::

    default: main
    datasources:
      main:
        url_env: DB_URL_MAIN
        description: Main warehouse server
      archive:
        url_env: DB_URL_ARCHIVE

The YAML file is versioned and reviewed like the rest of
``project_config/``, so it never holds a password. A connection string
does, so it lives in ``.env`` or a secret store and is only referenced
here by the variable's name.

Each table in ``schema.yaml`` names its source with ``datasource:``; a
table without one belongs to the default source. The source a query runs
on is derived from the tables it references (see
:mod:`database.routing`), never chosen by the model.

Without ``datasources.yaml``
----------------------------
The file is optional. When it is absent the deployment has exactly one
source, named :data:`DEFAULT_DATASOURCE`, whose connection string is
:attr:`config.Settings.db_connection_url` (``DB_CONNECTION_URL``) -- the
single-connection setup every earlier release used, unchanged.

Scope
-----
Every source must use the deployment's :attr:`config.Settings.sql_dialect`.
A per-source ``dialect`` key is accepted so a file written today stays
valid later, but a value different from ``SQL_DIALECT`` is refused at
start-up until mixed dialects are supported. One query runs on one
source; a query that needs tables from two sources is refused (see
``docs/design/DATASOURCES.md`` for both roadmap items).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

if TYPE_CHECKING:
    from config import Settings

__all__ = [
    "DEFAULT_DATASOURCE",
    "DATASOURCES_FILENAME",
    "DataSource",
    "DataSourceDefinition",
    "DataSourcesConfig",
    "UnknownDataSourceError",
    "datasources_path",
    "load_datasources_config",
    "validate_datasources_yaml_text",
    "default_datasource_name",
    "datasource_names",
    "get_datasource",
    "get_datasources",
    "table_datasources",
    "check_table_datasources",
    "validate_datasource_urls",
    "reset_datasources_cache",
]

#: Name of the single source used when ``datasources.yaml`` is absent.
DEFAULT_DATASOURCE = "default"

#: File name under ``PROJECT_CONFIG_DIR``.
DATASOURCES_FILENAME = "datasources.yaml"

#: A source name appears in logs, the audit trail, ``/health`` and error
#: messages, so it is kept to a plain identifier.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

#: An environment variable name, as ``url_env`` must give one.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UnknownDataSourceError(ValueError):
    """A data source name that is not configured was requested."""


# ---------------------------------------------------------------------------
# datasources.yaml model
# ---------------------------------------------------------------------------

class DataSourceDefinition(BaseModel):
    """One entry under ``datasources.yaml``'s ``datasources`` key.

    Attributes
    ----------
    url_env:
        Name of the environment variable holding this source's SQLAlchemy
        connection string. Never the connection string itself.
    description:
        Free text shown to operators (``/health``, the admin panel).
    dialect:
        Optional sqlglot dialect key. Must equal ``SQL_DIALECT`` when set.
    application_name:
        Optional per-source override of ``DB_APPLICATION_NAME``.
    """

    model_config = {"extra": "forbid"}

    url_env: str
    description: str = ""
    dialect: str | None = None
    application_name: str | None = None

    @field_validator("url_env")
    @classmethod
    def _url_env_is_a_variable_name(cls, value: str) -> str:
        if "://" in value or not _ENV_NAME_RE.match(value):
            raise ValueError(
                "url_env must be the NAME of an environment variable "
                "(e.g. DB_URL_MAIN), never a connection string -- "
                "datasources.yaml is versioned and must not hold credentials"
            )
        return value


class DataSourcesConfig(BaseModel):
    """The whole ``datasources.yaml`` file.

    Attributes
    ----------
    default:
        Source used by a table with no ``datasource:`` key and by a query
        that references no configured table. Optional when exactly one
        source is listed; required otherwise.
    datasources:
        Source name to definition. At least one entry.
    """

    model_config = {"extra": "forbid"}

    default: str | None = None
    datasources: dict[str, DataSourceDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_and_default_are_consistent(self) -> "DataSourcesConfig":
        for name in self.datasources:
            if not _NAME_RE.match(name):
                raise ValueError(
                    f"data source name {name!r} must start with a letter and "
                    "contain only letters, digits, '_' or '-' (max 64 chars)"
                )
        if self.default is None:
            if len(self.datasources) > 1:
                raise ValueError(
                    "more than one data source is listed, so `default` must "
                    "name the one used by tables without a `datasource` key"
                )
        elif self.default not in self.datasources:
            raise ValueError(
                f"default {self.default!r} is not one of the listed data "
                f"sources: {sorted(self.datasources)}"
            )
        return self

    @property
    def default_name(self) -> str:
        """The resolved default source name."""
        return self.default or next(iter(self.datasources))


def _validate_raw(raw: object) -> DataSourcesConfig:
    try:
        return DataSourcesConfig.model_validate(raw or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = " -> ".join(str(x) for x in first["loc"]) or "(top level)"
        raise ValueError(
            f"[{DATASOURCES_FILENAME}] validation error at '{where}': {first['msg']}"
        ) from exc


def validate_datasources_yaml_text(text: str) -> DataSourcesConfig:
    """Parse and validate *text* as the contents of ``datasources.yaml``.

    Parameters
    ----------
    text:
        YAML text.

    Returns
    -------
    DataSourcesConfig
        The validated model.

    Raises
    ------
    ValueError
        If *text* is not valid YAML or does not match the schema.

    Examples
    --------
    >>> cfg = validate_datasources_yaml_text(
    ...     "datasources:\\n  main:\\n    url_env: DB_URL_MAIN\\n"
    ... )
    >>> cfg.default_name
    'main'
    >>> validate_datasources_yaml_text(
    ...     "datasources:\\n  main:\\n    url_env: mssql://u:p@h/db\\n"
    ... )
    Traceback (most recent call last):
        ...
    ValueError: [datasources.yaml] validation error at 'datasources -> main -> url_env': ...
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"[{DATASOURCES_FILENAME}] is not valid YAML: {exc}") from exc
    return _validate_raw(raw)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

#: Repository root, against which a relative ``PROJECT_CONFIG_DIR`` is
#: resolved -- the same rule :func:`knowledge.config_loader._project_config_dir`
#: applies.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _settings(settings: "Settings | None") -> "Settings":
    import config as cfg

    return cfg.settings if settings is None else settings


def datasources_path(settings: "Settings | None" = None) -> Path:
    """Return ``<PROJECT_CONFIG_DIR>/datasources.yaml``, resolved at call time.

    Parameters
    ----------
    settings:
        Settings to read ``project_config_dir`` from; the process-wide
        :data:`config.settings` when ``None``. :meth:`config.Settings.validate`
        passes its own instance, so validating a candidate configuration
        never reads the global one.

    Returns
    -------
    pathlib.Path
        The path, whether or not the file exists. A relative
        ``PROJECT_CONFIG_DIR`` is resolved against the repository root.
    """
    configured = Path(_settings(settings).project_config_dir)
    base = configured if configured.is_absolute() else _REPO_ROOT / configured
    return base / DATASOURCES_FILENAME


@lru_cache(maxsize=8)
def _load_cached(path: str, mtime_ns: int) -> DataSourcesConfig:
    with open(path, encoding="utf-8") as fh:
        return validate_datasources_yaml_text(fh.read())


def load_datasources_config(settings: "Settings | None" = None) -> DataSourcesConfig | None:
    """Load ``datasources.yaml``, or return ``None`` when it does not exist.

    The parsed file is cached per path and modification time, so a routed
    query does not re-read YAML, and an edited file is picked up without
    a manual cache reset.

    Parameters
    ----------
    settings:
        See :func:`datasources_path`.

    Returns
    -------
    DataSourcesConfig | None
        ``None`` means the single-source fallback applies.

    Raises
    ------
    ValueError
        If the file exists but is invalid.
    """
    path = datasources_path(settings)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    return _load_cached(str(path), mtime_ns)


def reset_datasources_cache() -> None:
    """Drop the cached ``datasources.yaml`` parse (for tests)."""
    _load_cached.cache_clear()


# ---------------------------------------------------------------------------
# Resolved sources
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DataSource:
    """A configured source with its connection string resolved.

    Attributes
    ----------
    name:
        The source name.
    url:
        SQLAlchemy connection string, read from the environment. Empty
        when the variable is unset; :func:`validate_datasource_urls`
        refuses that at start-up.
    url_env:
        The variable *url* was read from.
    dialect:
        sqlglot dialect key (``SQL_DIALECT`` when the file does not set one).
    application_name:
        Value stamped on the connection so a DBA can identify the session.
    description:
        Operator-facing description.
    """

    name: str
    url: str
    url_env: str
    dialect: str
    application_name: str
    description: str = ""

    def __repr__(self) -> str:  # never print the connection string
        return f"DataSource(name={self.name!r}, url_env={self.url_env!r}, dialect={self.dialect!r})"


def default_datasource_name(settings: "Settings | None" = None) -> str:
    """Return the default source name.

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(project_config_dir="/nonexistent"):
    ...     default_datasource_name()
    'default'
    """
    config = load_datasources_config(settings)
    return DEFAULT_DATASOURCE if config is None else config.default_name


def datasource_names(settings: "Settings | None" = None) -> tuple[str, ...]:
    """Return every configured source name, default first."""
    config = load_datasources_config(settings)
    if config is None:
        return (DEFAULT_DATASOURCE,)
    default = config.default_name
    return (default, *(n for n in config.datasources if n != default))


def get_datasource(name: str | None = None, settings: "Settings | None" = None) -> DataSource:
    """Resolve one source, reading its connection string from the environment.

    Parameters
    ----------
    name:
        Source name; ``None`` means the default source.
    settings:
        See :func:`datasources_path`; also the source of
        ``db_connection_url``, ``sql_dialect`` and ``db_application_name``.

    Returns
    -------
    DataSource
        The resolved source. Read fresh on every call, so a test's
        :func:`config.override_settings` of ``db_connection_url`` applies.

    Raises
    ------
    UnknownDataSourceError
        If *name* is not configured.
    """
    settings = _settings(settings)
    config = load_datasources_config(settings)
    if config is None:
        if name not in (None, DEFAULT_DATASOURCE):
            raise UnknownDataSourceError(
                f"unknown data source {name!r}: no {DATASOURCES_FILENAME} is "
                f"configured, so the only source is {DEFAULT_DATASOURCE!r}"
            )
        return DataSource(
            name=DEFAULT_DATASOURCE,
            url=settings.db_connection_url,
            url_env="DB_CONNECTION_URL",
            dialect=settings.sql_dialect,
            application_name=settings.db_application_name,
        )

    resolved = config.default_name if name is None else name
    definition = config.datasources.get(resolved)
    if definition is None:
        raise UnknownDataSourceError(
            f"unknown data source {resolved!r}; configured: {sorted(config.datasources)}"
        )
    return DataSource(
        name=resolved,
        url=os.environ.get(definition.url_env, "").strip(),
        url_env=definition.url_env,
        dialect=definition.dialect or settings.sql_dialect,
        application_name=(
            definition.application_name
            if definition.application_name is not None
            else settings.db_application_name
        ),
        description=definition.description,
    )


def get_datasources(settings: "Settings | None" = None) -> tuple[DataSource, ...]:
    """Resolve every configured source, default first."""
    return tuple(get_datasource(name, settings) for name in datasource_names(settings))


# ---------------------------------------------------------------------------
# Tables -> sources
# ---------------------------------------------------------------------------

def table_datasources() -> dict[str, str]:
    """Return ``{table_name: source_name}`` for every table in ``schema.yaml``.

    A table without a ``datasource:`` key belongs to the default source.
    Names are returned as written; :func:`check_table_datasources` is what
    refuses an unknown one.
    """
    from schema_data.registry import get_table_datasource_names

    default = default_datasource_name()
    return {
        table: (source or default)
        for table, source in get_table_datasource_names().items()
    }


def check_table_datasources(assignments: Mapping[str, str] | None = None) -> None:
    """Refuse a ``schema.yaml`` table that names an unconfigured source.

    Parameters
    ----------
    assignments:
        ``{table: source}`` to check; defaults to :func:`table_datasources`.
        The admin panel passes a candidate ``schema.yaml``'s own mapping
        here before a draft is saved.

    Raises
    ------
    ValueError
        Naming every offending table and the configured sources.
    """
    if assignments is None:
        assignments = table_datasources()
    known = set(datasource_names())
    default = default_datasource_name()
    bad = sorted(
        f"{table} -> {source}"
        for table, source in assignments.items()
        if (source or default) not in known
    )
    if bad:
        raise ValueError(
            "schema.yaml assigns tables to data sources that are not "
            f"configured: {', '.join(bad)}. Configured sources: {sorted(known)}"
        )


def validate_datasource_urls(check_url, settings: "Settings | None" = None) -> None:
    """Run *check_url* on every configured source (start-up validation).

    Parameters
    ----------
    check_url:
        ``(label, url, dialect) -> None``; raises :class:`ValueError` for a
        missing, placeholder or mismatched connection string.
        :meth:`config.Settings.validate` passes its own checker so the
        single-source and multi-source paths apply identical rules.
    settings:
        The settings being validated; see :func:`datasources_path`.

    Raises
    ------
    ValueError
        From *check_url*, from an invalid ``datasources.yaml``, or when a
        source declares a dialect other than ``SQL_DIALECT``.
    """
    settings = _settings(settings)
    for source in get_datasources(settings):
        if source.dialect != settings.sql_dialect:
            raise ValueError(
                f"data source {source.name!r} declares dialect "
                f"{source.dialect!r}, but SQL_DIALECT is "
                f"{settings.sql_dialect!r}; every data source must use "
                "the deployment's dialect (mixed dialects are not supported yet)"
            )
        check_url(source.url_env, source.url, source.dialect)
