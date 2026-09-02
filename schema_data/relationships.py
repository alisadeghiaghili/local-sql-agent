# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Lazy loader for ``RELATIONSHIPS``.

``RELATIONSHIPS`` is loaded from ``<PROJECT_CONFIG_DIR>/schema.yaml`` on
first access (see :mod:`schema_data.registry` and
:attr:`config.Settings.project_config_dir`). ``import
schema_data.relationships`` never fails even if the project-config
directory is absent. ``knowledge.config_loader.ConfigNotFoundError`` is
only raised when ``RELATIONSHIPS`` is actually accessed.
"""

from __future__ import annotations

from typing import Any

from schema_data.registry import get_relationships_map

_cache: dict[str, Any] = {}


def __getattr__(name: str) -> Any:
    if name == "RELATIONSHIPS":
        if "RELATIONSHIPS" not in _cache:
            _cache["RELATIONSHIPS"] = get_relationships_map()
        return _cache["RELATIONSHIPS"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
