# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Lazy loader for session-policy config.

Variables are loaded from ``project_config/session_policy.yaml`` on first
access. ``import knowledge.session_policy`` never fails even if
``project_config/`` is absent. ``ConfigNotFoundError`` is only raised when
one of the names below is accessed.

This module externalises the one dimension ``session.ambiguity``,
``session.engine`` and ``session.refinement`` treat as the *default*
scope filter (§5 of ``docs/api-contract-v2.md``: ``source: "default"``,
always user-editable) when a ranking question names no scope of its own
-- a warehouse-specific policy choice (which dimension gets defaulted,
what its fallback label is, what options a clarification offers), not an
engine invariant. It is unrelated to the §5 ``"policy"`` source (a
non-editable system rule, e.g. the §2 scope assumption in
``ambiguity.assumptions_for_cte_refinement``) -- that distinction is about
whether the *user* can override an assumption and stays a fixed property
of the two code paths, not something this config toggles.
"""

from __future__ import annotations

from typing import Any

from knowledge.config_loader import load_session_policy

_cache: dict[str, Any] = {}

_NAMES = (
    "DEFAULT_SCOPE_FILTER_KEY",
    "DEFAULT_SCOPE_FIELD_NAME",
    "DEFAULT_SCOPE_LABEL",
    "DEFAULT_SCOPE_OPTIONS",
    "DEFAULT_SCOPE_CLARIFICATION_PROMPT",
)


def __getattr__(name: str) -> Any:
    if name in _NAMES:
        if "_loaded" not in _cache:
            cfg = load_session_policy().default_scope
            _cache["DEFAULT_SCOPE_FILTER_KEY"] = cfg.filter_key
            _cache["DEFAULT_SCOPE_FIELD_NAME"] = cfg.field_name
            _cache["DEFAULT_SCOPE_LABEL"] = cfg.default_label
            _cache["DEFAULT_SCOPE_OPTIONS"] = tuple(cfg.options)
            _cache["DEFAULT_SCOPE_CLARIFICATION_PROMPT"] = cfg.clarification_prompt
            _cache["_loaded"] = True
        return _cache[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
