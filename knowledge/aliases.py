# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Lazy loader for aliases config.

Variables are loaded from project_config/aliases.yaml on first access.
``import knowledge.aliases`` never fails even if project_config/ is absent.
ConfigNotFoundError is only raised when RING_ALIASES or SYNONYMS is accessed.
"""

from __future__ import annotations

from typing import Any

from knowledge.config_loader import load_aliases

_cache: dict[str, Any] = {}


def __getattr__(name: str) -> Any:
    if name in ("RING_ALIASES", "SYNONYMS"):
        if "_loaded" not in _cache:
            cfg = load_aliases()
            _cache["RING_ALIASES"] = cfg.ring_aliases
            _cache["SYNONYMS"] = cfg.synonyms
            _cache["_loaded"] = True
        return _cache[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
