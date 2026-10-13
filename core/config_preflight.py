# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Refuse to start on an unreadable ``project_config/`` with one clear line.

``security.sql_guard`` (and through it everything the API imports) binds
``schema_data.columns.TABLE_COLUMNS`` at import time, so a missing or broken
``schema.yaml`` -- or any other file the import chain reads -- used to end
``uvicorn api.server:app`` and ``scripts/prompt_budget.py`` in a forty-line
traceback whose last line is the only useful one. This module runs the real
loaders for those files *before* the imports that read them and turns a
failure into a single line naming the file and the error.

It reads the files exactly as the application does (the same loaders, the
same strict YAML parser), so it can never accept a file the application
would refuse, nor refuse one it would accept. It imports only what the
caller is about to import anyway -- ``knowledge.config_loader`` and
``schema_data.registry`` -- and nothing heavy (no web framework, no
database driver).

Public API
----------
SERVER_IMPORT_FILES
    The files ``import api.server`` reads.
PROMPT_BUDGET_IMPORT_FILES
    The files ``scripts/prompt_budget.py`` reads while importing.
project_config_problem(filenames) -> str | None
    The first problem found, as one line, or ``None``.
exit_if_project_config_unusable(filenames, *, hints=(), status=2) -> None
    Print the problem to stderr and raise ``SystemExit``; do nothing when
    every file loads.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable

__all__ = [
    "PROMPT_BUDGET_IMPORT_FILES",
    "SERVER_IMPORT_FILES",
    "exit_if_project_config_unusable",
    "project_config_problem",
]

#: What ``import api.server`` reads: ``retrieval/*``, ``session/*``,
#: ``prompt_engine/static_prefix.py`` and ``security/sql_guard.py`` bind
#: these at import time. (Files read later -- the system prompt, the memory
#: policy -- fail at start-up with their own message.) Pinned to a real
#: import by ``tests/test_config_preflight.py``.
SERVER_IMPORT_FILES: tuple[str, ...] = (
    "entities.yaml",
    "schema.yaml",
    "retrieval_hints.yaml",
    "aliases.yaml",
    "business_rules.yaml",
    "examples.yaml",
    "metrics.yaml",
    "session_policy.yaml",
)

#: What ``scripts/prompt_budget.py`` reads while importing.
PROMPT_BUDGET_IMPORT_FILES: tuple[str, ...] = (
    "business_rules.yaml",
    "examples.yaml",
    "metrics.yaml",
    "schema.yaml",
)


def _loaders() -> dict[str, Callable[[], object]]:
    """File name -> the function the application reads that file with."""
    from knowledge.config_loader import (
        load_aliases,
        load_business_rules,
        load_entities,
        load_examples,
        load_metrics,
        load_retrieval_hints,
        load_session_policy,
    )
    from schema_data.registry import load_schema

    return {
        "aliases.yaml": load_aliases,
        "entities.yaml": load_entities,
        "business_rules.yaml": load_business_rules,
        "examples.yaml": load_examples,
        "metrics.yaml": load_metrics,
        "schema.yaml": load_schema,
        "retrieval_hints.yaml": load_retrieval_hints,
        "session_policy.yaml": load_session_policy,
    }


def project_config_problem(filenames: Iterable[str]) -> str | None:
    """Return the first problem with *filenames* under ``project_config/``.

    Args:
        filenames: Names from :func:`_loaders`, checked in the order given.

    Returns:
        ``None`` when every file loads. Otherwise the loader's own message,
        which names the file (``[schema.yaml] duplicate key ...`` or
        ``<path> not found. ...``); a message that does not mention the
        file is prefixed with ``[<name>]``.

    Raises:
        ImportError: If the loaders themselves cannot be imported (a broken
            install, not a configuration problem).

    Examples:
        >>> import tempfile
        >>> from config import override_settings
        >>> with tempfile.TemporaryDirectory() as empty:
        ...     with override_settings(project_config_dir=empty):
        ...         message = project_config_problem(["schema.yaml"])
        >>> "schema.yaml" in message and "not found" in message
        True
    """
    from knowledge.config_loader import ConfigNotFoundError

    loaders = _loaders()
    for name in filenames:
        try:
            loaders[name]()
        except (ConfigNotFoundError, ValueError, OSError) as exc:
            message = " ".join(str(exc).split())
            return message if name in message else f"[{name}] {message}"
    return None


def exit_if_project_config_unusable(
    filenames: Iterable[str],
    *,
    hints: Iterable[str] = (),
    status: int = 2,
) -> None:
    """Print ``cannot load project_config: <problem>`` and exit, or return.

    Nothing happens when every file in *filenames* loads, so a valid
    deployment starts exactly as before.

    Args:
        filenames: Files to check, in order; see
            :func:`project_config_problem`.
        hints: Extra lines printed after the first one.
        status: The ``SystemExit`` status; ``2`` like ``scripts/sync_schema.py``.

    Raises:
        SystemExit: With *status*, after writing to stderr, when a file
            cannot be loaded.
    """
    problem = project_config_problem(filenames)
    if problem is None:
        return
    print(f"cannot load project_config: {problem}", file=sys.stderr)
    for line in hints:
        print(line, file=sys.stderr)
    raise SystemExit(status)
