"""Lazy loader for entities config.

Variables are loaded from project_config/entities.yaml on first access.
``import knowledge.entities`` never fails even if project_config/ is absent.
ConfigNotFoundError is only raised when ENTITIES is accessed.
"""

from __future__ import annotations

from typing import Any

from knowledge.config_loader import load_entities

_cache: dict[str, Any] = {}


def __getattr__(name: str) -> Any:
    if name == "ENTITIES":
        if "_loaded" not in _cache:
            cfg = load_entities()
            # Expose as plain dict matching original structure:
            # {EntityName: {"aliases": [...], "table": "..."}}
            _cache["ENTITIES"] = {
                k: {"aliases": v.aliases, "table": v.table}
                for k, v in cfg.entities.items()
            }
            _cache["_loaded"] = True
        return _cache[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
