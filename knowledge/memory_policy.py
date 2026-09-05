# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Lazy loader for cross-session memory config — ``docs/api-contract-v2.md`` §5.

Mirrors ``knowledge.session_policy``'s pattern exactly: variables (here, a
whole ``{key: MemoryKey}`` mapping) are loaded from
``project_config/memory_policy.yaml`` on first access, cached for the life
of the process, and ``import knowledge.memory_policy`` never fails even if
``project_config/`` is absent — ``ConfigNotFoundError`` only surfaces the
first time :func:`get_memory_keys` is actually called.

This is the ONE place the closed set of rememberable keys lives. Neither
``session.memory`` nor ``session.engine`` name a key, a warehouse column,
or a permitted value as a Python literal — every one of those comes back
out of the mapping this module returns, which is exactly what
``tests/test_no_domain_literals.py`` would otherwise catch if a key/column
name leaked into engine source as a hardcoded string.
"""

from __future__ import annotations

from typing import NamedTuple

from knowledge.config_loader import load_memory_policy

_cache: dict[str, "dict[str, MemoryKey]"] = {}


class MemoryKey(NamedTuple):
    """One declared, rememberable preference — one row of ``memory_policy.yaml``.

    filter_key:
        The merged-filters / :class:`~session.store.TurnMemory.filters`
        dict key this entry contributes when applied.
    field_name:
        The generic :class:`~session.models.Assumption.field` / UI field
        name this entry's value is shown under.
    column:
        The warehouse column this value constrains — re-checked against
        the requesting principal's ``denied_columns`` on every turn that
        would apply it (§5, "re-check the ACL at read time").
    options:
        Closed set of permitted values. Empty means "any value up to
        ``max_length``", not "no values permitted".
    max_length:
        Per-key cap on the stored value's length, never wider than
        ``config.Settings.memory_value_max_length``.
    """

    filter_key: str
    field_name: str
    column: str
    options: tuple[str, ...]
    max_length: int


def get_memory_keys() -> dict[str, MemoryKey]:
    """Return ``{key: MemoryKey}`` — the closed set an analyst may pin via
    ``PUT /v2/memory/{key}``, loaded from
    ``project_config/memory_policy.yaml`` and cached after first access.

    Raises
    ------
    knowledge.config_loader.ConfigNotFoundError
        If ``memory_policy.yaml`` is missing from the configured
        ``project_config_dir``.
    """
    if "keys" not in _cache:
        loaded = load_memory_policy()
        _cache["keys"] = {
            name: MemoryKey(
                filter_key=entry.filter_key,
                field_name=entry.field_name,
                column=entry.column,
                options=tuple(entry.options),
                max_length=entry.max_length,
            )
            for name, entry in loaded.keys.items()
        }
    return _cache["keys"]
