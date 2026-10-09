# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Domain knowledge loaded from ``project_config/``.

The five names in ``__all__`` (``RING_ALIASES``, ``BUSINESS_RULES``,
``ENTITIES``, ``EXAMPLES``, ``METRICS``) are re-exported from their
submodules **lazily**, on first attribute access (PEP 562 module
``__getattr__``). ``import knowledge`` and ``import knowledge.config_loader``
therefore read no file and never fail, whether or not ``project_config/``
exists; a missing or invalid file surfaces as
:class:`~knowledge.config_loader.ConfigNotFoundError` / ``ValueError`` when
the name is first used, exactly as each submodule's own docstring says.

This package used to import the five submodules at the top, which read
five YAML files the moment *anything* under ``knowledge`` was imported
(``knowledge.config_loader`` included, since importing a submodule runs the
package first). A fresh checkout could not even import the loaders needed
to report which files were missing.

Server start-up is unaffected: modules the API imports
(``retrieval/*``, ``prompt_engine/static_prefix.py``, ``session/*``) bind
the names they need at import time with ``from knowledge.entities import
ENTITIES`` and the like, so ``import api.server`` still stops on a broken
``project_config/`` before the first request. That is pinned by
``tests/test_knowledge_lazy_import.py``.

Examples
--------
>>> import knowledge
>>> sorted(knowledge.__all__)
['BUSINESS_RULES', 'ENTITIES', 'EXAMPLES', 'METRICS', 'RING_ALIASES']
>>> knowledge.NO_SUCH_NAME
Traceback (most recent call last):
    ...
AttributeError: module 'knowledge' has no attribute 'NO_SUCH_NAME'
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "RING_ALIASES",
    "BUSINESS_RULES",
    "ENTITIES",
    "EXAMPLES",
    "METRICS",
]

#: Public name -> the submodule that owns (and lazily loads) it.
_EXPORTS: dict[str, str] = {
    "RING_ALIASES": "knowledge.aliases",
    "BUSINESS_RULES": "knowledge.business_rules",
    "ENTITIES": "knowledge.entities",
    "EXAMPLES": "knowledge.examples",
    "METRICS": "knowledge.metrics",
}


def __getattr__(name: str) -> Any:
    """Resolve one of the lazily re-exported names on first access.

    Args:
        name: Attribute being looked up on the package.

    Returns:
        The value the owning submodule exposes under *name* (the same
        object ``from knowledge.<module> import <name>`` yields).

    Raises:
        AttributeError: If *name* is not one of ``__all__``.
        knowledge.config_loader.ConfigNotFoundError: If the backing
            ``project_config/`` file is missing.
        ValueError: If the backing file is not valid.

    Examples:
        >>> import knowledge
        >>> knowledge.__getattr__("NO_SUCH_NAME")
        Traceback (most recent call last):
            ...
        AttributeError: module 'knowledge' has no attribute 'NO_SUCH_NAME'
    """
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    """Include the lazy names in ``dir(knowledge)`` without loading them."""
    return sorted({*globals(), *__all__})
