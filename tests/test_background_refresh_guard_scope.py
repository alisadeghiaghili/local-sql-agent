# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test: the root conftest's ``_no_background_dimension_refresh``
guard must NOT be path-scoped to ``tests/`` and ``eval/tests/``.

That scoping is correct, and deliberate, for its sibling fixture
``_no_real_database`` -- see :func:`conftest._item_needs_database_guard`
and ``conftest._no_real_database``'s own docstring: CI's separate
``pytest --doctest-modules -q api llm security session database core
knowledge prompt_engine retrieval schema_data logs exporters observability
eval config.py`` step collects ``database/connection.py``'s
``dispose_engine`` doctest, which legitimately builds and disposes a real
engine, and unconditionally patching ``create_engine`` out from under it
breaks that doctest.

It was wrong for ``_no_background_dimension_refresh``. That same doctest
step also collects doctests straight out of ``eval/runner.py`` -- a module
that sits in ``eval/``, not ``eval/tests/``, so the old scope check (shared
between both fixtures) treated it as outside the guarded trees and left the
background-refresh trigger at its module default (enabled). Those
doctests exercise ``run_case``/``run_golden_set``, which construct a real
``retrieval.context_retriever.ContextRetriever``: with the trigger enabled,
every configured dimension spawned its own background-refresh thread (six
real daemon threads named ``dim-vocab-bg-refresh-Broker.PersianName``,
``…Currency.PersianName``, ``…DeliveryPlace.PersianName``, ``…Ring.Name``,
``…Symbol.Commodity_PersianName`` and ``…Symbol.Commodity_Symbol``), each
attempting a real ``pyodbc`` connection to the configured warehouse host.
The doctest step still exited 0 -- nothing in it asserts on background
thread state -- so this was invisible in an ordinarily green CI run.

No collected item, anywhere in this repository, doctest or test-suite,
legitimately needs a live background-refresh thread (unlike
``_no_real_database``'s one real exception above), so
``_no_background_dimension_refresh`` must apply unconditionally.

This test is deliberately placed in ``tests/`` rather than alongside
``eval/tests/test_no_real_database_guard.py``: CI's doctest step above
passes bare ``eval`` (not ``eval/tests``) as one of its collection roots,
so pytest's ordinary test discovery -- independent of ``--doctest-modules``
-- picks up any ``test_*.py`` module anywhere under ``eval/``, including
``eval/tests/``. A regression test asserting this scope living in that
directory would itself add a passing test to that doctest run's count.
``tests/`` is never passed to that command, so a module here is exercised
only by the ordinary test-suite runs (``pytest tests/ eval/tests``, or
``pytest tests/`` alone) and never inflates the doctest step's summary
line.

Deliberately schema-agnostic and fast: it builds a synthetic
``request``-like object, drives the two fixture generators directly, and
never dials anything real.
"""

from __future__ import annotations

from pathlib import Path

import conftest
import database.connection as db_connection
from retrieval.dimension_vocabulary import is_background_refresh_enabled


class _FakeNode:
    """Just enough of a pytest ``Item`` for ``_item_needs_database_guard``
    to read ``request.node.path`` off of."""

    def __init__(self, path: Path) -> None:
        self.path = path


class _FakeRequest:
    """Just enough of a ``pytest.FixtureRequest`` for ``_no_real_database``
    to read ``request.node.path`` off of."""

    def __init__(self, path: Path) -> None:
        self.node = _FakeNode(path)


def test_background_refresh_guard_is_not_path_scoped() -> None:
    """Pins the asymmetry between the two root-conftest guards.

    Builds a synthetic request whose node path is ``eval/runner.py`` --
    outside both ``tests/`` and ``eval/tests/`` -- standing in for a
    doctest collected straight out of that module by CI's
    ``pytest --doctest-modules`` step, and exercises the actual fixture
    functions from the root ``conftest.py`` against it (via
    ``__wrapped__``, since pytest refuses to call a
    ``@pytest.fixture``-decorated callable directly).

    Asserts that, for that path:

    * ``is_background_refresh_enabled()`` reads ``False`` --
      ``_no_background_dimension_refresh`` applied even though the path is
      outside both guarded trees.
    * ``database.connection.create_engine`` is left completely unpatched --
      ``_no_real_database`` correctly stayed a no-op for that same path,
      the one case (``dispose_engine``'s doctest) that needs a real
      engine.

    If the background-refresh guard is ever made path-scoped again (e.g.
    by reusing ``_item_needs_database_guard`` for it, matching
    ``_no_real_database``'s shape), the first assertion fails here instead
    of silently reopening the six-thread leak the next time CI's doctest
    step runs.
    """
    fake_request = _FakeRequest(
        Path(conftest.__file__).resolve().parent / "eval" / "runner.py"
    )
    unpatched_create_engine = db_connection.create_engine

    bg_refresh_gen = conftest._no_background_dimension_refresh.__wrapped__()
    next(bg_refresh_gen)
    try:
        assert is_background_refresh_enabled() is False

        no_real_db_gen = conftest._no_real_database.__wrapped__(fake_request)
        next(no_real_db_gen)
        try:
            # Never actually calling create_engine() here: the point is
            # only that it was left untouched for this path, not to
            # exercise it (which could attempt a real connection).
            assert db_connection.create_engine is unpatched_create_engine
        finally:
            try:
                next(no_real_db_gen)
            except StopIteration:
                pass
    finally:
        try:
            next(bg_refresh_gen)
        except StopIteration:
            pass
