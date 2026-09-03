# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Lazy loader for ``TABLE_COLUMNS``.

``TABLE_COLUMNS`` is loaded from ``<PROJECT_CONFIG_DIR>/schema.yaml`` on
first access (see :mod:`schema_data.registry` and
:attr:`config.Settings.project_config_dir`). ``import schema_data.columns``
never fails even if the project-config directory is absent.
``knowledge.config_loader.ConfigNotFoundError`` is only raised when
``TABLE_COLUMNS`` is actually accessed.

``security.sql_guard`` builds its table/column allowlist directly from
``TABLE_COLUMNS`` — only tables listed here (i.e. tables that carry a
``columns`` key in ``schema.yaml``) are queryable; a table described in
``schema.yaml`` with no ``columns`` key never appears here and is refused
by the guard as an unknown table.
"""

from __future__ import annotations

from typing import Any

from schema_data.registry import get_table_columns

_cache: dict[str, Any] = {}


def __getattr__(name: str) -> Any:
    if name == "TABLE_COLUMNS":
        if "TABLE_COLUMNS" not in _cache:
            _cache["TABLE_COLUMNS"] = get_table_columns()
        return _cache["TABLE_COLUMNS"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
