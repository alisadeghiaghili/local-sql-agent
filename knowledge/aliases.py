# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Lazy loader for aliases config.

Variables are loaded from project_config/aliases.yaml on first access.
``import knowledge.aliases`` never fails even if project_config/ is absent.
ConfigNotFoundError is only raised when RING_ALIASES or SYNONYMS is accessed.

A ``ring_aliases`` key is the exact value of the ring dimension's name column
(``ValueRetriever.extract_ring`` turns it into the ring filter). The setup
wizard of 6.13 to 6.16.2 wrote one entry per *table* there, keyed by the
table's ``schema.yaml`` key; such an entry made a question that named a table
filter the ring by the table's name. ``RING_ALIASES`` leaves out an entry whose
key is, as a whole and ignoring letter case, a key of ``schema.yaml``'s
``tables``, and logs one WARNING naming them (``core.ring_names``). A file whose
keys are real ring names -- including a ring that merely contains a table word,
``North Hall`` for a table ``Hall`` -- is loaded exactly as written, and so is a
file when ``schema.yaml`` cannot be read.
"""

from __future__ import annotations

from typing import Any

from knowledge.config_loader import _project_config_dir, load_aliases

_cache: dict[str, Any] = {}


def _ring_aliases(loaded: dict[str, list[str]]) -> dict[str, list[str]]:
    """*loaded* without the entries named like a table of ``schema.yaml``.

    Never raises: whatever goes wrong while the tables are read, the entries
    are used as written (the behaviour before the guard existed).
    """
    try:
        from core.ring_names import guard_ring_aliases

        return guard_ring_aliases(loaded, _project_config_dir())  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 - a guard that cannot read the schema must not stop the server
        return loaded


def __getattr__(name: str) -> Any:
    if name in ("RING_ALIASES", "SYNONYMS"):
        if "_loaded" not in _cache:
            cfg = load_aliases()
            _cache["RING_ALIASES"] = _ring_aliases(cfg.ring_aliases)
            _cache["SYNONYMS"] = cfg.synonyms
            _cache["_loaded"] = True
        return _cache[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
