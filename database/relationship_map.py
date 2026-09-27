# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Runtime relationship registry for JOIN hints.

Load order
----------
1. ``<PROJECT_CONFIG_DIR>/relationships.yaml`` (if it exists) -- the same
   directory (and the same ``PROJECT_CONFIG_DIR`` env var /
   :attr:`config.Settings.project_config_dir` field) every other
   ``project_config``-reading loader in this codebase uses; see
   :mod:`knowledge.config_loader` and :mod:`schema_data.registry`.
2. Live schema inspection via :class:`~database.schema_inspector.SchemaInspector`
   (only when ``AUTO_DISCOVER_SCHEMA=true`` in environment AND no YAML file
   is present)

The registry is populated **lazily** on the first call to
:func:`get_join_path` or :func:`get_relationship_map`.

Public API
----------
:func:`get_join_path`
    Return the list of JOIN hint strings needed to connect two tables.

:func:`get_relationship_map`
    Return the full ``{(from_table, to_table): [join_hint, ...]}`` dict.

:func:`reset`
    Clear the cache (useful in tests or after config reloads).

Example usage::

    from database.relationship_map import get_join_path

    hints = get_join_path("Contract", "Ring")
    # ["JOIN Ring ON Contract.Ring_ID = Ring.ID"]
"""

from __future__ import annotations

import logging
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import MutableMapping

logger = logging.getLogger(__name__)

#: Repository root, used to resolve a *relative* ``PROJECT_CONFIG_DIR``
#: deterministically -- see :func:`_project_config_dir`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCK   = threading.Lock()
_CACHE: dict[tuple[str, str], list[str]] | None = None


def _project_config_dir() -> Path:
    """Return the configured project-config directory, resolved at call time.

    Mirrors ``knowledge.config_loader._project_config_dir`` and
    ``schema_data.registry._project_config_dir`` exactly: reads
    ``cfg.settings.project_config_dir`` fresh on every call (not once at
    import time), and resolves a *relative* value against the repository
    root rather than the current working directory. This module used to
    hardcode the literal path ``project_config/relationships.yaml``,
    ignoring ``PROJECT_CONFIG_DIR`` entirely -- unlike every other loader in
    this codebase, which meant a deployment that set ``PROJECT_CONFIG_DIR``
    (or CI, which points it at ``project_config.example/``) silently never
    reached this module's YAML path at all.
    """
    import config as cfg  # deferred: avoids a hard import-time dependency

    configured = Path(cfg.settings.project_config_dir)
    if configured.is_absolute():
        return configured
    return _REPO_ROOT / configured


def _relationships_yaml_path() -> Path:
    return _project_config_dir() / "relationships.yaml"


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def _load_from_yaml(path: Path) -> dict[tuple[str, str], list[str]]:
    """Parse ``relationships.yaml`` into a join-hint mapping.

    Each entry must have ``from_table``, ``to_table``, and ``join_hint``.
    Missing keys are logged and skipped.
    """
    import yaml  # deferred: keeps module importable without pyyaml at top-level

    mapping: MutableMapping[tuple[str, str], list[str]] = defaultdict(list)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cannot read %s: %s", path, exc)
        return {}

    for entry in raw.get("relationships", []):
        from_t = entry.get("from_table", "").strip()
        to_t   = entry.get("to_table",   "").strip()
        hint   = entry.get("join_hint",  "").strip()
        if not (from_t and to_t and hint):
            logger.debug("Skipping incomplete relationship entry: %s", entry)
            continue
        mapping[(from_t, to_t)].append(hint)
        # Also register the reverse key (useful for symmetric lookups)
        mapping[(to_t, from_t)].append(hint)

    logger.info("Loaded %d relationship pairs from %s", len(mapping), path)
    return dict(mapping)


def _load_from_inspection() -> dict[tuple[str, str], list[str]]:
    """Inspect the live DB and build the mapping from FK constraints."""
    db_url = os.getenv("DB_CONNECTION_URL") or os.getenv("DATABASE_URL", "")
    if not db_url:
        logger.warning(
            "AUTO_DISCOVER_SCHEMA=true but DB_CONNECTION_URL is not set. "
            "Relationship map will be empty."
        )
        return {}

    from database.schema_inspector import SchemaInspector

    logger.info("AUTO_DISCOVER_SCHEMA: running live schema inspection for relationships")
    inspector = SchemaInspector(db_url, sample_rows=0)
    try:
        snapshot = inspector.inspect(fetch_row_counts=False)
    except ConnectionError as exc:
        logger.error("Schema inspection failed: %s", exc)
        return {}
    finally:
        inspector.close()

    mapping: MutableMapping[tuple[str, str], list[str]] = defaultdict(list)
    for rel in snapshot.relationships:
        hint = rel.join_hint
        mapping[(rel.from_table, rel.to_table)].append(hint)
        mapping[(rel.to_table, rel.from_table)].append(hint)

    logger.info("Discovered %d relationship pairs from live inspection", len(mapping))
    return dict(mapping)


def _build_cache() -> dict[tuple[str, str], list[str]]:
    """Build the relationship cache using the configured load strategy."""
    relationships_yaml = _relationships_yaml_path()
    if relationships_yaml.exists():
        logger.debug("Loading relationships from %s", relationships_yaml)
        return _load_from_yaml(relationships_yaml)

    auto_discover = os.getenv("AUTO_DISCOVER_SCHEMA", "false").strip().lower() == "true"
    if auto_discover:
        return _load_from_inspection()

    logger.debug(
        "No relationships.yaml found and AUTO_DISCOVER_SCHEMA is not set. "
        "Relationship map is empty."
    )
    return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_relationship_map() -> dict[tuple[str, str], list[str]]:
    """Return the full ``{(from_table, to_table): [join_hints]}`` mapping.

    The mapping is populated lazily on the first call and cached in memory
    for the lifetime of the process.
    """
    global _CACHE
    if _CACHE is None:
        with _LOCK:
            if _CACHE is None:
                _CACHE = _build_cache()
    return _CACHE


def get_join_path(table_a: str, table_b: str) -> list[str]:
    """Return a list of JOIN hint strings connecting *table_a* to *table_b*.

    Returns an empty list when no direct relationship is known.

    Parameters
    ----------
    table_a:
        Name of the source table (without schema).
    table_b:
        Name of the target table (without schema).

    Examples
    --------
    Building the map needs a live database connection, so the call is
    skipped here — and so is the line that inspects its result, which
    would otherwise raise ``NameError`` on an unbound ``hints``.

    >>> hints = get_join_path("Contract", "Ring")  # doctest: +SKIP
    >>> hints                                      # doctest: +SKIP
    ['JOIN Ring ON Contract.Ring_ID = Ring.ID']
    """
    mapping = get_relationship_map()
    return mapping.get((table_a, table_b), [])


def reset() -> None:
    """Clear the in-memory cache so the next call rebuilds it.

    Useful in tests or after ``<PROJECT_CONFIG_DIR>/relationships.yaml`` has
    been updated without restarting the process.
    """
    global _CACHE
    with _LOCK:
        _CACHE = None
    logger.debug("Relationship map cache cleared")
