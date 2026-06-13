"""Lazy loader for metrics config.

Variables are loaded from project_config/metrics.yaml on first access.
``import knowledge.metrics`` never fails even if project_config/ is absent.
ConfigNotFoundError is only raised when METRICS is accessed.
"""

from __future__ import annotations

from typing import Any

from knowledge.config_loader import load_metrics

_cache: dict[str, Any] = {}


def __getattr__(name: str) -> Any:
    if name == "METRICS":
        if "_loaded" not in _cache:
            cfg = load_metrics()
            # Expose as plain dict matching original structure:
            # {metric_key: {"aliases": [...], "expression": "..."}}
            _cache["METRICS"] = {
                k: {"aliases": v.aliases, "expression": v.expression}
                for k, v in cfg.metrics.items()
            }
            _cache["_loaded"] = True
        return _cache[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
