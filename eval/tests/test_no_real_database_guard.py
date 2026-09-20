# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test: the root conftest's database-safety guards must cover
``eval/tests``, not only ``tests/``.

``eval/tests`` has no ``conftest.py`` of its own. The two autouse fixtures
that keep the combined ``pytest tests/ eval/tests`` run from touching a
real database -- ``_no_real_database`` and
``_no_background_dimension_refresh`` -- used to live in ``tests/conftest.py``
only. A plain ``pytest`` conftest applies solely to the directory tree
rooted where it lives, so that scoped ``eval/tests`` ran with neither guard
active: ``retrieval.dimension_vocabulary``'s background-refresh trigger
stayed at its module default (enabled), and
``database.connection.create_engine`` was never patched. A cold
dimension-vocabulary lookup anywhere in ``eval/tests`` could therefore spin
up a real background thread that reached the real ``pyodbc`` driver and
dialed the configured warehouse host.

Both fixtures now live in the repository-root ``conftest.py`` (an ancestor
of both ``tests/`` and ``eval/tests``), which is what actually fixes the
leak. This test exists only to catch a regression -- if the guards are
ever moved back into ``tests/conftest.py`` alone, or a future
``eval/tests/conftest.py`` locally shadows either one, this test (running
from inside ``eval/tests``) fails.

Deliberately schema-agnostic and fast: it asserts the guard is in effect,
it never touches a real schema, table, or connection string.
"""

from __future__ import annotations

import pytest

import database.connection as db_connection
from retrieval.dimension_vocabulary import is_background_refresh_enabled


def test_background_refresh_is_disabled_in_eval_tests() -> None:
    assert is_background_refresh_enabled() is False


def test_real_engine_construction_is_refused_in_eval_tests() -> None:
    # Asserts the refusal only -- never actually dials anything. If the
    # guard were not in effect here, this call would instead attempt a
    # real connection against the configured (placeholder) warehouse host.
    with pytest.raises(AssertionError, match="real SQLAlchemy engine"):
        db_connection.create_engine("sqlite://")
