# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The files a deployment's ``project_config/`` must contain.

One list, importable without reading any of them. ``import knowledge`` loads
five of the files the moment it is imported, so anything that wants to say
*which* file is missing (the setup wizard, ``scripts/verify_deployment.py``)
cannot ask the loaders: it would die on the first absent file with a
traceback instead of naming all of them.

Public API
----------
REQUIRED_PROJECT_CONFIG_FILES
    The ten required file names.
missing_project_config_files(directory) -> list[str]
    Which of them a directory lacks.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["REQUIRED_PROJECT_CONFIG_FILES", "missing_project_config_files"]

#: Every file the application cannot start or answer without: the nine
#: ``*.yaml`` files ``knowledge.config_loader`` and ``schema_data.registry``
#: raise ``ConfigNotFoundError`` for (the same nine ``appdb.config_versions``
#: versions as one bundle), and ``system_prompt.md``. Optional files
#: (``relationships.yaml``, ``datasources.yaml``, ``api_keys.json``) are not
#: listed. ``tests/test_project_config_files.py`` pins this list to the
#: loaders, so a file added to one and not here fails a test.
REQUIRED_PROJECT_CONFIG_FILES: tuple[str, ...] = (
    "aliases.yaml",
    "entities.yaml",
    "business_rules.yaml",
    "examples.yaml",
    "metrics.yaml",
    "schema.yaml",
    "retrieval_hints.yaml",
    "session_policy.yaml",
    "memory_policy.yaml",
    "system_prompt.md",
)


def missing_project_config_files(directory: Path) -> list[str]:
    """Return the required files that *directory* does not contain.

    Args:
        directory: A ``project_config``-style directory. It need not exist.

    Returns:
        The names from :data:`REQUIRED_PROJECT_CONFIG_FILES` that are not
        regular files in *directory*, in that tuple's order. Empty when the
        directory is complete.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     Path(tmp, "aliases.yaml").write_text("{}")
        ...     missing = missing_project_config_files(Path(tmp))
        2
        >>> "aliases.yaml" in missing, "metrics.yaml" in missing
        (False, True)
        >>> len(missing_project_config_files(Path("/no/such/directory")))
        10
    """
    return [name for name in REQUIRED_PROJECT_CONFIG_FILES if not (directory / name).is_file()]
