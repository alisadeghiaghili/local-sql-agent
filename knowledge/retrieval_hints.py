# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Lazy loader for retrieval-hints config.

Variables are loaded from ``project_config/retrieval_hints.yaml`` on first
access. ``import knowledge.retrieval_hints`` never fails even if
``project_config/`` is absent. ``ConfigNotFoundError`` is only raised when
``FACT_TABLES``, ``ALWAYS_INCLUDE`` or ``FACT_PATTERNS`` is accessed.

These three names replace what used to be domain vocabulary hardcoded in
Python:

* ``FACT_TABLES`` -- the set of table names this warehouse treats as
  fact/transaction tables, previously duplicated verbatim in both
  ``retrieval/entity_retriever.py`` and ``retrieval/fact_retriever.py``.
* ``ALWAYS_INCLUDE`` -- ``schema_data/retriever.py``'s forced-match table
  -> trigger-phrase map (a retrieval heuristic, not schema metadata --
  it can name a table independently of whichever ``schema.yaml`` happens
  to be loaded; see that module's docstring).
* ``FACT_PATTERNS`` -- ``retrieval/fact_retriever.py``'s fast keyword ->
  fact-table map, tried before the TF-IDF fallback.
"""

from __future__ import annotations

from typing import Any

from knowledge.config_loader import load_retrieval_hints

_cache: dict[str, Any] = {}

_NAMES = ("FACT_TABLES", "ALWAYS_INCLUDE", "FACT_PATTERNS")


def __getattr__(name: str) -> Any:
    if name in _NAMES:
        if "_loaded" not in _cache:
            cfg = load_retrieval_hints()
            _cache["FACT_TABLES"] = set(cfg.fact_tables)
            _cache["ALWAYS_INCLUDE"] = cfg.always_include
            _cache["FACT_PATTERNS"] = cfg.fact_patterns
            _cache["_loaded"] = True
        return _cache[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
