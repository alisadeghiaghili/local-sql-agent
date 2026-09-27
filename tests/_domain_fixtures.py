# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Small loader for optional, deployment-owned domain-value test fixtures.

A handful of tests need a REAL deployment's real values (table/column
names, exact SQL, exact row counts, real Persian alias mappings) to mean
anything -- they are marked ``@pytest.mark.domain_data`` (or carry a
module-level ``pytestmark = pytest.mark.domain_data``) and already
auto-skip whenever ``PROJECT_CONFIG_DIR`` resolves to
``project_config.example/`` (see the repo-root ``conftest.py``). That
solves the *runtime* half of the problem.

The *static* half -- keeping the real values themselves out of tracked
test **source** -- is solved the same way every other piece of
deployment-specific knowledge in this codebase is solved: externalised to
a file under ``<PROJECT_CONFIG_DIR>/_test_fixtures/``, loaded through this
module, never hardcoded in a ``tests/*.py`` literal. This mirrors
:mod:`knowledge.config_loader`'s own "resolve the directory at call time,
skip/raise cleanly when the specific file is missing" shape, one layer
further -- test fixtures instead of runtime config.

Two distinct kinds of "missing":

* The whole ``PROJECT_CONFIG_DIR`` points at ``project_config.example/``
  -- handled entirely by the existing ``domain_data`` marker/skip
  mechanism in ``conftest.py``; this module is never even reached in that
  case for a properly-marked test.
* A real ``PROJECT_CONFIG_DIR`` is in effect, but this *particular*,
  optional fixture file has not been created yet -- handled here, by
  skipping the individual test (or the whole module, for a module-level
  fixture load) with a message that names the expected path and points at
  ``project_config.example/_test_fixtures/README.md``.

Either way, a missing fixture skips cleanly; it never fails the build and
never falls back to a generic/synthetic value that would silently stop
testing the real thing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import config as cfg

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _project_config_dir() -> Path:
    """Same resolution rule as knowledge.config_loader._project_config_dir
    and schema_data.registry._project_config_dir: read the setting fresh
    on every call, resolve a relative value against the repository root."""
    configured = Path(cfg.settings.project_config_dir)
    if configured.is_absolute():
        return configured
    return _REPO_ROOT / configured


def fixture_path(name: str) -> Path:
    """The path a fixture named *name* would live at, whether or not it
    currently exists."""
    return _project_config_dir() / "_test_fixtures" / name


def _skip(name: str, *, allow_module_level: bool) -> None:
    path = fixture_path(name)
    pytest.skip(
        f"optional domain-value test fixture not found: {path} "
        f"-- see project_config.example/_test_fixtures/README.md",
        allow_module_level=allow_module_level,
    )


def load_json_fixture(name: str, *, allow_module_level: bool = False) -> Any:
    """Load ``<PROJECT_CONFIG_DIR>/_test_fixtures/<name>`` as JSON, or skip
    the calling test (or the whole module, with ``allow_module_level=True``,
    for a fixture loaded at import time) cleanly if that file is absent."""
    path = fixture_path(name)
    if not path.exists():
        _skip(name, allow_module_level=allow_module_level)
    return json.loads(path.read_text(encoding="utf-8"))
