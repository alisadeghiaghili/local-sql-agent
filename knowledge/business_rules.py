"""Lazy loader for business rules config.

Variables are loaded from project_config/business_rules.yaml on first access.
``import knowledge.business_rules`` never fails even if project_config/ is absent.
ConfigNotFoundError is only raised when BUSINESS_RULES is accessed.
"""

from __future__ import annotations

from typing import Any

from knowledge.config_loader import load_business_rules

_cache: dict[str, Any] = {}


def __getattr__(name: str) -> Any:
    if name == "BUSINESS_RULES":
        if "_loaded" not in _cache:
            cfg = load_business_rules()
            # Expose as plain dict matching original structure:
            # {rule_key: rule_text_string}
            _cache["BUSINESS_RULES"] = {
                k: v.rule_text
                for k, v in cfg.rules.items()
            }
            _cache["_loaded"] = True
        return _cache[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
