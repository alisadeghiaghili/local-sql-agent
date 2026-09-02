# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Lazy loader for ``TABLE_DESCRIPTIONS``.

``TABLE_DESCRIPTIONS`` is loaded from ``<PROJECT_CONFIG_DIR>/schema.yaml``
on first access (see :mod:`schema_data.registry` and
:attr:`config.Settings.project_config_dir`). ``import schema_data.tables``
never fails even if the project-config directory is absent.
``knowledge.config_loader.ConfigNotFoundError`` is only raised when
``TABLE_DESCRIPTIONS`` is actually accessed.
"""

from __future__ import annotations

from typing import Any

from schema_data.registry import get_table_descriptions

_cache: dict[str, Any] = {}


def __getattr__(name: str) -> Any:
    if name == "TABLE_DESCRIPTIONS":
        if "TABLE_DESCRIPTIONS" not in _cache:
            _cache["TABLE_DESCRIPTIONS"] = get_table_descriptions()
        return _cache["TABLE_DESCRIPTIONS"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
