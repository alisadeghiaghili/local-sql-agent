# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The data sources a setup tool reads, through the application's own engines.

The first-install inspector (:mod:`database.schema_inspector_cli`) and the
setup wizard (``setup_project.py``) both describe a deployment from its
databases. Both must see what the application sees: a draft built under a
wider login (a Windows administrator, say) lists change-capture, temporary and
staging tables the application cannot even query, and they ended up in live
configuration. So each configured source is read through
:func:`database.connection.get_engine`, the engine the server uses for it.

This module resolves *which* sources to read and how to name them. It does not
connect: an engine is opened when :attr:`ConfiguredSource.open_engine` is
called, and it belongs to the application's process-wide cache, so the caller
never disposes it.

Without ``datasources.yaml`` the deployment has one implicit source, ``default``
(``DB_CONNECTION_URL``), and :func:`configured_sources` returns that one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.engine import Engine

__all__ = [
    "ConfiguredSource",
    "SourceSelectionError",
    "configured_sources",
    "datasources_file_in_use",
    "first_line",
]


class SourceSelectionError(ValueError):
    """The requested sources cannot be read; the message is ready to print after ``ERROR: ``."""


@dataclass(frozen=True)
class ConfiguredSource:
    """One configured data source and how to reach it.

    Attributes
    ----------
    name:
        The source name.
    label:
        How a draft header or a message names it: the name and the redacted URL.
    secret_url:
        The connection string with its password, for scrubbing a driver's
        message (:func:`core.redaction.scrub_secrets`); never print it.
    open_engine:
        Returns the application's engine for the source (cached; do not dispose).
    """

    name: str
    label: str
    secret_url: str
    open_engine: Callable[[], Engine]


def first_line(exc: BaseException) -> str:
    """The first line of an exception's message, cut at 160 characters.

    Examples
    --------
    >>> first_line(ValueError("bad value\\nsecond line"))
    'bad value'
    >>> first_line(ValueError(""))
    ''
    """
    return (str(exc).strip().splitlines() or [""])[0][:160]


def _unset_hint(variable: str | None) -> str:
    """Indented lines saying why *variable* is missing, or ``""`` without one.

    A bare "is not set or is empty" sends the reader to the wrong place when
    the variable *is* in their ``.env``: the file was not found (on Windows a
    ``.env.txt``), it has a line python-dotenv skipped, or the name differs.
    See :func:`config.explain_unset_variable`.
    """
    if not variable:
        return ""
    import config

    return "".join(f"\n  {line}" for line in config.explain_unset_variable(variable))


def datasources_file_in_use() -> bool:
    """Whether ``<PROJECT_CONFIG_DIR>/datasources.yaml`` exists (and is valid).

    Returns
    -------
    bool
        ``False`` for the single-source deployment (``DB_CONNECTION_URL``).

    Raises
    ------
    SourceSelectionError
        If the file exists but cannot be used.

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(project_config_dir="/nonexistent"):
    ...     datasources_file_in_use()
    False
    """
    from database.datasources import load_datasources_config

    try:
        return load_datasources_config() is not None
    except ValueError as exc:
        raise SourceSelectionError(f"datasources.yaml cannot be used: {first_line(exc)}") from exc


def configured_sources(
    names: Sequence[str] | None = None,
) -> tuple[list[ConfiguredSource], list[str], str]:
    """The sources to read, every configured source name, and the default one.

    Parameters
    ----------
    names:
        Only these sources (``--datasource NAME``, repeatable); ``None`` for
        every configured source. The result keeps the configuration's order,
        default first.

    Returns
    -------
    tuple[list[ConfiguredSource], list[str], str]
        ``(sources to read, all configured names, default name)``. The
        second item is every source of the deployment, not only those read:
        a run narrowed to one source still writes ``datasource:`` lines when
        the deployment has several.

    Raises
    ------
    SourceSelectionError
        If ``datasources.yaml`` is invalid, a name is not configured, or a
        source's credentials cannot be read (an unset environment variable;
        the message then says which ``.env`` was loaded and why the variable
        is not in it, see :func:`config.explain_unset_variable`).

    Examples
    --------
    >>> import config as cfg
    >>> with cfg.override_settings(project_config_dir="/nonexistent"):
    ...     sources, everything, default = configured_sources()
    >>> [s.name for s in sources], everything, default
    (['default'], ['default'], 'default')
    >>> with cfg.override_settings(project_config_dir="/nonexistent"):
    ...     configured_sources(["sales"])
    Traceback (most recent call last):
        ...
    database.setup_sources.SourceSelectionError: unknown data source sales; configured: default
    """
    from database.datasources import datasource_names, default_datasource_name, get_datasource

    try:
        everything = list(datasource_names())
        default = default_datasource_name()
    except ValueError as exc:
        raise SourceSelectionError(f"datasources.yaml cannot be used: {first_line(exc)}") from exc

    selected = everything
    if names:
        unknown = [n for n in names if n not in everything]
        if unknown:
            raise SourceSelectionError(
                f"unknown data source {', '.join(unknown)}; configured: {', '.join(everything)}"
            )
        selected = [n for n in everything if n in names]

    from database.connection import get_engine

    chosen: list[ConfiguredSource] = []
    for name in selected:
        try:
            source = get_datasource(name)
        except ValueError as exc:  # DataSourceConfigError: a variable that is not set
            raise SourceSelectionError(
                f"data source {name!r} cannot be used: {first_line(exc)}"
                f"{_unset_hint(getattr(exc, 'variable', None))}"
            ) from exc
        if not source.url:
            variable = source.url_env or "DB_CONNECTION_URL"
            raise SourceSelectionError(
                f"data source {name!r} cannot be used: the environment variable "
                f"{variable} that holds its connection string is not set{_unset_hint(variable)}"
            )
        chosen.append(ConfiguredSource(
            name=name,
            label=f"{name} ({source.redacted_url})",
            secret_url=source.url,
            open_engine=lambda n=name: get_engine(n),
        ))
    return chosen, everything, default
