"""Lazy loader for examples config.

Variables are loaded from project_config/examples.yaml on first access.
``import knowledge.examples`` never fails even if project_config/ is absent.
ConfigNotFoundError is only raised when EXAMPLES is accessed.
"""

from __future__ import annotations

from typing import Any

from knowledge.config_loader import load_examples

_cache: dict[str, Any] = {}


def __getattr__(name: str) -> Any:
    if name == "EXAMPLES":
        if "_loaded" not in _cache:
            cfg = load_examples()
            # Expose as plain list matching original structure:
            # [{"tags": [...], "question": "...", "sql": "..."}]
            _cache["EXAMPLES"] = [
                {"tags": ex.tags, "question": ex.question, "sql": ex.sql}
                for ex in cfg.examples
            ]
            _cache["_loaded"] = True
        return _cache[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
