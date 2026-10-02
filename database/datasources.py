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
``<PROJECT_CONFIG_DIR>/datasources.yaml`` *describes the connection*: host,
port, database, driver and ODBC options. Only the secret stays in the
environment (``.env``), named by ``password_env``::

    default: sales
    datasources:
      sales:
        description: Sales warehouse
        host: 10.0.0.5
        database: SalesDW
        username: nlq_reader
        password_env: DB_PASSWORD_SALES
        options:
          TrustServerCertificate: true
      reports:
        host: 10.0.0.5
        database: ReportsDW
        trusted_connection: true      # Windows authentication

The environment variable holds the **raw** password. :func:`build_url`
assembles the SQLAlchemy URL with :meth:`sqlalchemy.engine.URL.create`,
which escapes every special character itself, so an operator never
percent-encodes anything. The YAML file is versioned and reviewed like the
rest of ``project_config/``, so it never holds a password; a value that
looks like one is refused when the file is loaded.

A source may instead use the earlier ``url_env`` form (the NAME of a
variable holding a complete SQLAlchemy URL); that form is kept for
deployments written against 6.1 and 6.2. One source is one form or the
other, never both.

Each table in ``schema.yaml`` names its source with ``datasource:``; a
table without one belongs to the default source. The source a query runs
on is derived from the tables it references (see
:mod:`database.routing`), never chosen by the model.

Without ``datasources.yaml``
----------------------------
The file is optional. When it is absent the deployment has exactly one
source, named :data:`DEFAULT_DATASOURCE`, whose connection string is
:attr:`config.Settings.db_connection_url` (``DB_CONNECTION_URL``) -- the
single-connection setup every earlier release used. Its password may be
given raw in ``DB_PASSWORD`` (:attr:`config.Settings.db_password`), which
is set on the parsed URL by :func:`apply_db_password`; without it the
URL's own percent-encoded password is used exactly as before.

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
from typing import TYPE_CHECKING, Any, Collection, Mapping, NamedTuple

import yaml
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

if TYPE_CHECKING:
    from config import Settings

__all__ = [
    "DEFAULT_DATASOURCE",
    "DATASOURCES_FILENAME",
    "DataSource",
    "DataSourceDefinition",
    "DataSourcesConfig",
    "DataSourceConfigError",
    "UnknownDataSourceError",
    "build_url",
    "apply_db_password",
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

#: An environment variable name, as ``url_env``, ``username_env`` and
#: ``password_env`` must give one.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: What each ``*_env`` field must name, and an example variable, for the
#: message that refuses a value written in its place.
_ENV_FIELD_HINTS = {
    "url_env": ("DB_URL_MAIN", "a connection string"),
    "username_env": ("DB_USER_SALES", "a user name"),
    "password_env": ("DB_PASSWORD_SALES", "a password"),
}

#: A key under ``options`` is an ODBC keyword (``Encrypt``,
#: ``Application Name``): letters, digits, ``_`` and spaces.
_OPTION_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*$")

#: Option keys that are not accepted, compared after lower-casing and
#: removing spaces, ``_`` and ``-``. Credentials are never written in this
#: file; the connection's own fields already carry the driver, the
#: authentication mode and the server, and ``odbc_connect`` replaces the
#: whole connection string (credentials included).
_FORBIDDEN_OPTION_KEYS = frozenset({
    "pwd", "password", "uid", "user", "userid", "username",
    "driver", "trustedconnection", "server", "database", "odbcconnect",
})

#: Fields that describe a structured connection. A source that sets any of
#: them is structured; one that also sets ``url_env`` is refused.
_STRUCTURED_FIELDS = (
    "host", "port", "database", "driver", "username", "username_env",
    "password_env", "trusted_connection", "options",
)

#: Keys that would put a secret (or a whole URL) in the versioned file.
_INLINE_SECRET_KEYS = {
    "password": "the password",
    "pwd": "the password",
    "url": "the connection string",
}


class _Backend(NamedTuple):
    """How a sqlglot dialect is reached by a structured source."""

    drivername: str
    default_port: int
    default_driver: str


#: Dialects a structured source can use, keyed by ``SQL_DIALECT``. Adding
#: a dialect is one entry here.
_BACKENDS: Mapping[str, _Backend] = {
    "tsql": _Backend("mssql+pyodbc", 1433, "ODBC Driver 18 for SQL Server"),
}


class UnknownDataSourceError(ValueError):
    """A data source name that is not configured was requested."""


class DataSourceConfigError(ValueError):
    """A data source's connection cannot be built from its configuration.

    Messages name the source and the environment variable concerned, and
    never a secret's value.
    """


# ---------------------------------------------------------------------------
# datasources.yaml model
# ---------------------------------------------------------------------------

def _option_key(key: object) -> str:
    if not isinstance(key, str) or not _OPTION_KEY_RE.match(key):
        raise ValueError(
            f"options key {key!r} must be an ODBC keyword: a letter, then "
            "letters, digits, '_' or spaces"
        )
    if re.sub(r"[ _-]", "", key).lower() in _FORBIDDEN_OPTION_KEYS:
        raise ValueError(
            f"options key {key!r} is not allowed: credentials never go in "
            "datasources.yaml (use username/username_env and password_env), "
            "and driver, trusted_connection, host and database have their "
            "own fields"
        )
    return key


def _option_value(key: str, value: object) -> str:
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError(
            f"options value for {key!r} must be a string, number or "
            f"true/false, not {type(value).__name__}"
        )
    if not text or any(ch in text for ch in ";\r\n\x00"):
        raise ValueError(
            f"options value for {key!r} must be non-empty and must not "
            "contain ';' or a line break"
        )
    return text


class DataSourceDefinition(BaseModel):
    """One entry under ``datasources.yaml``'s ``datasources`` key.

    A source is **structured** (``host`` and ``database`` describe the
    connection, the secret comes from ``password_env``) or **legacy**
    (``url_env`` names a variable holding a complete SQLAlchemy URL).
    Setting ``url_env`` together with any structured field is refused.

    Attributes
    ----------
    description:
        Free text shown to operators (``/health``, the admin panel).
    dialect:
        Optional sqlglot dialect key. Must equal ``SQL_DIALECT`` when set.
    application_name:
        Optional per-source override of ``DB_APPLICATION_NAME``.
    url_env:
        Legacy form. Name of the environment variable holding this
        source's complete SQLAlchemy connection string. Never the
        connection string itself.
    host, port, database:
        Server address and database. ``port`` defaults to the dialect's
        (1433 for ``tsql``), except for a named instance (``host\\INSTANCE``),
        which the SQL Server Browser resolves.
    driver:
        ODBC driver name; defaults to ``ODBC Driver 18 for SQL Server``.
    username:
        Login name as a plain value. Alternative: ``username_env``.
    username_env:
        Name of an environment variable holding the login name.
    password_env:
        Name of the environment variable holding the **raw** password.
    trusted_connection:
        Windows authentication: no login or password is sent, and none of
        ``username``, ``username_env`` or ``password_env`` may be set.
    options:
        Extra ODBC keywords. ``true``/``false`` become ``yes``/``no``,
        numbers become their text.

    Examples
    --------
    >>> d = DataSourceDefinition(host="db1", database="SalesDW",
    ...                          username="nlq", password_env="DB_PASSWORD_SALES",
    ...                          options={"TrustServerCertificate": True})
    >>> d.options
    {'TrustServerCertificate': 'yes'}
    >>> d.trusted_connection, d.port
    (False, None)
    """

    model_config = {"extra": "forbid"}

    description: str = ""
    dialect: str | None = None
    application_name: str | None = None

    url_env: str | None = None

    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = None
    driver: str | None = None
    username: str | None = None
    username_env: str | None = None
    password_env: str | None = None
    trusted_connection: bool = False
    options: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _refuse_inline_secrets(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, what in _INLINE_SECRET_KEYS.items():
                if key in data:
                    raise ValueError(
                        f"{key!r} is not accepted: datasources.yaml is "
                        f"versioned and must not hold {what}. Use "
                        "password_env (the NAME of the variable holding the "
                        "raw password) or username_env"
                    )
        return data

    @field_validator("url_env", "username_env", "password_env")
    @classmethod
    def _is_a_variable_name(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return value
        field = str(info.field_name)
        example, what = _ENV_FIELD_HINTS[field]
        if "://" in value or not _ENV_NAME_RE.match(value):
            raise ValueError(
                f"{field} must be the NAME of an environment variable "
                f"(e.g. {example}), never {what} -- datasources.yaml is "
                "versioned and must not hold credentials"
            )
        return value

    @field_validator("host", "database", "driver", "username")
    @classmethod
    def _is_not_blank(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("host")
    @classmethod
    def _host_is_only_a_host(cls, value: str | None) -> str | None:
        if value is not None and re.search(r"://|[@/,\s]", value):
            raise ValueError(
                "host must be a bare server name or address (no scheme, "
                "login, path or ',port'; the port has its own field)"
            )
        return value

    @field_validator("options", mode="before")
    @classmethod
    def _coerce_options(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value  # the type check reports it
        options = {_option_key(k): _option_value(k, v) for k, v in value.items()}
        if len({k.lower() for k in options}) != len(options):
            raise ValueError("options lists the same keyword twice (case-insensitively)")
        return options

    @model_validator(mode="after")
    def _is_exactly_one_form(self) -> "DataSourceDefinition":
        given = [f for f in _STRUCTURED_FIELDS if f in self.model_fields_set]
        if self.url_env is not None:
            if given:
                raise ValueError(
                    "sets url_env (legacy form) together with structured "
                    f"fields ({', '.join(given)}); a source is one or the other"
                )
            return self
        if not given:
            raise ValueError(
                "needs either url_env (legacy: the NAME of a variable holding "
                "a full SQLAlchemy URL) or host and database (structured)"
            )
        missing = [f for f in ("host", "database") if getattr(self, f) is None]
        if missing:
            raise ValueError(f"structured source is missing {' and '.join(missing)}")
        self._check_authentication()
        return self

    def _check_authentication(self) -> None:
        if self.trusted_connection:
            clash = [
                f for f in ("username", "username_env", "password_env")
                if getattr(self, f) is not None
            ]
            if clash:
                raise ValueError(
                    "trusted_connection: true means Windows authentication, "
                    f"so {', '.join(clash)} must not be set"
                )
            return
        if self.username is not None and self.username_env is not None:
            raise ValueError("set username or username_env, not both")
        if self.username is None and self.username_env is None:
            raise ValueError(
                "needs a login: set username (or username_env) and "
                "password_env, or trusted_connection: true"
            )
        if self.password_env is None:
            raise ValueError(
                "password_env is required: the NAME of the environment "
                "variable holding the raw password (or set "
                "trusted_connection: true)"
            )


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
        message = first["msg"].removeprefix("Value error, ")
        raise ValueError(
            f"[{DATASOURCES_FILENAME}] validation error at '{where}': {message}"
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
    >>> cfg = validate_datasources_yaml_text(
    ...     "datasources:\\n  main:\\n    host: db1\\n    database: Sales\\n"
    ...     "    trusted_connection: true\\n"
    ... )
    >>> cfg.datasources["main"].host
    'db1'
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
# Building the connection URL
# ---------------------------------------------------------------------------

def _read_env(source: str, field: str, variable: str, environ: Mapping[str, str]) -> str:
    """Return the raw value of *variable*, or refuse naming source and variable."""
    value = environ.get(variable, "")
    if not value:
        raise DataSourceConfigError(
            f"data source {source!r}: environment variable {variable} "
            f"(named by {field}) is not set or is empty"
        )
    return value


def build_url(
    name: str,
    definition: DataSourceDefinition,
    dialect: str,
    environ: Mapping[str, str],
) -> URL:
    """Build the SQLAlchemy URL of a structured source.

    This is the only place a structured source's URL is assembled.
    :meth:`sqlalchemy.engine.URL.create` takes the raw password and
    escapes it when the URL is rendered, so no value needs encoding by
    whoever wrote it.

    Parameters
    ----------
    name:
        Source name, used in error messages.
    definition:
        A structured definition (``host`` and ``database`` set).
    dialect:
        sqlglot dialect the source speaks; selects the SQLAlchemy driver.
    environ:
        Environment to read ``username_env`` and ``password_env`` from.

    Returns
    -------
    sqlalchemy.engine.URL
        The URL, password included. Render it with
        ``render_as_string(hide_password=False)`` only to hand it to
        SQLAlchemy.

    Raises
    ------
    DataSourceConfigError
        If the dialect has no structured form, or a variable named by
        ``username_env`` / ``password_env`` is unset or empty.

    Examples
    --------
    >>> d = DataSourceDefinition(host="db1", database="SalesDW", username="nlq",
    ...                          password_env="PW", options={"Encrypt": "yes"})
    >>> url = build_url("sales", d, "tsql", {"PW": "p@ss:w/rd"})
    >>> url.password
    'p@ss:w/rd'
    >>> url.render_as_string()
    'mssql+pyodbc://nlq:***@db1:1433/SalesDW?Encrypt=yes&driver=ODBC+Driver+18+for+SQL+Server'
    """
    backend = _BACKENDS.get(dialect)
    if backend is None:
        raise DataSourceConfigError(
            f"data source {name!r}: host/database form supports dialects "
            f"{sorted(_BACKENDS)}, not {dialect!r}; use url_env for this "
            "source"
        )

    username: str | None = None
    password: str | None = None
    query = {"driver": definition.driver or backend.default_driver, **definition.options}
    if definition.trusted_connection:
        query["trusted_connection"] = "yes"
    else:
        username = definition.username
        if username is None:
            username = _read_env(name, "username_env", str(definition.username_env), environ)
        password = _read_env(name, "password_env", str(definition.password_env), environ)

    host = str(definition.host)
    port = definition.port
    if port is None and "\\" not in host:  # a named instance is found by the Browser
        port = backend.default_port
    return URL.create(
        backend.drivername,
        username=username,
        password=password,
        host=host,
        port=port,
        database=definition.database,
        query=query,
    )


def apply_db_password(url: str, password: str) -> str:
    """Set a raw ``DB_PASSWORD`` on a ``DB_CONNECTION_URL``.

    The password is set on the parsed URL (never concatenated into the
    string), so it needs no encoding. A password already written in the
    URL is not guessed at or re-encoded: both being present is refused.

    Parameters
    ----------
    url:
        The connection string, which then carries a login but no password.
    password:
        The raw password; empty means "not used" and returns *url* as is.

    Returns
    -------
    str
        The URL with the password set, or *url* unchanged when either
        argument is empty.

    Raises
    ------
    DataSourceConfigError
        If *url* cannot be parsed, already has a password, or has no login
        to attach the password to.

    Examples
    --------
    >>> url = apply_db_password("mssql+pyodbc://nlq@db1:1433/Sales", "p@ss/w:rd")
    >>> url
    'mssql+pyodbc://nlq:p%40ss%2Fw%3Ard@db1:1433/Sales'
    >>> make_url(url).password
    'p@ss/w:rd'
    >>> apply_db_password("mssql+pyodbc://nlq@db1/Sales", "")
    'mssql+pyodbc://nlq@db1/Sales'
    """
    if not password or not url:
        return url
    try:
        parsed = make_url(url)
    except ArgumentError:
        raise DataSourceConfigError(
            "DB_PASSWORD is set but DB_CONNECTION_URL is not a valid "
            "SQLAlchemy URL, so the password cannot be applied"
        ) from None
    if parsed.password:
        raise DataSourceConfigError(
            "DB_PASSWORD is set and DB_CONNECTION_URL also contains a "
            "password; it is ambiguous which one applies. Remove the "
            "password from DB_CONNECTION_URL (or unset DB_PASSWORD)"
        )
    if parsed.username is None:
        raise DataSourceConfigError(
            "DB_PASSWORD is set but DB_CONNECTION_URL has no user name to "
            "attach it to (mssql+pyodbc://USER@host/db)"
        )
    return parsed.set(password=password).render_as_string(hide_password=False)


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
        SQLAlchemy connection string, password included, for handing to
        SQLAlchemy only; show :attr:`redacted_url` instead. Empty when a
        ``url_env`` variable is unset; :func:`validate_datasource_urls`
        refuses that at start-up.
    url_env:
        The variable *url* was read from; ``None`` for a structured
        source, whose URL is built by :func:`build_url`.
    dialect:
        sqlglot dialect key (``SQL_DIALECT`` when the file does not set one).
    application_name:
        Value stamped on the connection so a DBA can identify the session.
    description:
        Operator-facing description.

    Examples
    --------
    >>> s = DataSource("sales", "mssql+pyodbc://u:secret@h/db", None, "tsql", "app")
    >>> s.redacted_url
    'mssql+pyodbc://u:***@h/db'
    >>> "secret" in repr(s)
    False
    """

    name: str
    url: str
    url_env: str | None
    dialect: str
    application_name: str
    description: str = ""

    def __repr__(self) -> str:  # never print the connection string
        return f"DataSource(name={self.name!r}, url_env={self.url_env!r}, dialect={self.dialect!r})"

    @property
    def is_structured(self) -> bool:
        """Whether the URL was built from ``host``/``database`` fields."""
        return self.url_env is None

    @property
    def label(self) -> str:
        """What validation messages call this source's connection."""
        return self.url_env or f"data source {self.name!r}"

    @property
    def redacted_url(self) -> str:
        """The URL with its password masked, safe to print or log."""
        if not self.url:
            return "<not set>"
        try:
            return make_url(self.url).render_as_string(hide_password=True)
        except ArgumentError:
            return "<unparsable connection URL>"


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


def _resolve_url(name: str, definition: DataSourceDefinition, dialect: str) -> str:
    """The connection string of a configured source, read from the environment."""
    if definition.url_env is not None:
        return os.environ.get(definition.url_env, "").strip()
    return build_url(name, definition, dialect, os.environ).render_as_string(
        hide_password=False
    )


def get_datasource(name: str | None = None, settings: "Settings | None" = None) -> DataSource:
    """Resolve one source, reading its secrets from the environment.

    Parameters
    ----------
    name:
        Source name; ``None`` means the default source.
    settings:
        See :func:`datasources_path`; also the source of
        ``db_connection_url``, ``db_password``, ``sql_dialect`` and
        ``db_application_name``.

    Returns
    -------
    DataSource
        The resolved source. Read fresh on every call, so a test's
        :func:`config.override_settings` of ``db_connection_url`` applies.

    Raises
    ------
    UnknownDataSourceError
        If *name* is not configured.
    DataSourceConfigError
        If a structured source's credentials cannot be read, or
        ``DB_PASSWORD`` conflicts with the single-source URL.
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
            url=apply_db_password(settings.db_connection_url, settings.db_password),
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
    dialect = definition.dialect or settings.sql_dialect
    return DataSource(
        name=resolved,
        url=_resolve_url(resolved, definition, dialect),
        url_env=definition.url_env,
        dialect=dialect,
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


def _refuse_placeholder_parts(source: DataSource, placeholders: Collection[str]) -> None:
    """Refuse a structured source whose host, database or login is a placeholder.

    A built URL never contains the ``username@server`` text
    :meth:`config.Settings.validate` looks for in a hand-written one, so
    each part is compared with the placeholder tokens instead. Values are
    not echoed: a password is one of the parts.
    """
    tokens = {p.lower() for p in placeholders if p}
    url = make_url(source.url)
    for part, value in (
        ("host", url.host), ("database", url.database),
        ("user name", url.username), ("password", url.password),
    ):
        if value and value.lower() in tokens:
            raise ValueError(
                f"{source.label} still has a placeholder {part} -- replace "
                "it with the real value"
            )


def validate_datasource_urls(
    check_url,
    settings: "Settings | None" = None,
    placeholders: Collection[str] = (),
) -> None:
    """Run *check_url* on every configured source (start-up validation).

    Parameters
    ----------
    check_url:
        ``(label, url, dialect) -> None``; raises :class:`ValueError` for a
        missing, placeholder or mismatched connection string.
        :meth:`config.Settings.validate` passes its own checker so the
        single-source and multi-source paths apply identical rules.
        *label* is the variable name for a ``url_env`` source and
        ``data source '<name>'`` for a structured one.
    settings:
        The settings being validated; see :func:`datasources_path`.
    placeholders:
        Unfilled-token values (``"change_me"``, ...). A structured source
        whose host, database, user name or password equals one is refused.

    Raises
    ------
    ValueError
        From *check_url*, from an invalid ``datasources.yaml``, when a
        source declares a dialect other than ``SQL_DIALECT``, or (as
        :class:`DataSourceConfigError`) when a source's credentials are
        missing or ``DB_PASSWORD`` is ambiguous.
    """
    settings = _settings(settings)
    config = load_datasources_config(settings)
    if config is not None:
        for name, definition in config.datasources.items():
            if definition.dialect not in (None, settings.sql_dialect):
                raise ValueError(
                    f"data source {name!r} declares dialect "
                    f"{definition.dialect!r}, but SQL_DIALECT is "
                    f"{settings.sql_dialect!r}; every data source must use "
                    "the deployment's dialect (mixed dialects are not supported yet)"
                )
    for source in get_datasources(settings):
        if source.is_structured:
            _refuse_placeholder_parts(source, placeholders)
        check_url(source.label, source.url, source.dialect)
