# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Guard against tests that exist but are never collected.

This file exists because of a real incident, not a hypothetical. Two
branches independently added a test class named
``TestSqlOnlyModeUsesRouter`` to ``tests/test_runner_audit.py``, a few
lines apart. Git rebased them cleanly — there was no textual conflict to
resolve — and Python did exactly what Python does: the second definition
shadowed the first. Four tests silently stopped being collected while the
suite still reported green, and the total test count stayed put instead of
moving by the expected delta.

That is the worst shape a test failure can take. A red test tells you
something. A test that quietly stops existing tells you nothing, and the
green badge actively argues that everything is fine.

A clean rebase of a test file is not evidence that its tests survived.
This module turns that lesson into a check, so the next occurrence fails
in milliseconds instead of hiding behind a passing suite.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Directories whose ``test_*.py`` modules this check walks.
_TEST_DIRS = ("tests", "eval/tests")


def _test_modules() -> list[Path]:
    """Return every ``test_*.py`` under :data:`_TEST_DIRS`, recursively."""
    found: list[Path] = []
    for rel in _TEST_DIRS:
        found.extend(sorted((_REPO_ROOT / rel).rglob("test_*.py")))
    return found


def _duplicate_top_level_names(tree: ast.Module) -> dict[str, int]:
    """Return ``{name: count}`` for top-level defs declared more than once.

    Only module-level ``class`` and ``def`` statements count: those are
    what pytest collects by name, and therefore what shadowing silently
    removes from the run.
    """
    counts = collections.Counter(
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    )
    return {name: n for name, n in counts.items() if n > 1}


def _duplicate_methods(tree: ast.Module) -> dict[str, dict[str, int]]:
    """Return ``{class_name: {method_name: count}}`` for shadowed methods.

    Same failure mode one level down: a second ``def test_x`` inside one
    class replaces the first, so the earlier assertions never run.
    """
    out: dict[str, dict[str, int]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        counts = collections.Counter(
            child.name for child in node.body if isinstance(child, ast.FunctionDef)
        )
        dupes = {name: n for name, n in counts.items() if n > 1}
        if dupes:
            out[node.name] = dupes
    return out


def test_test_modules_were_found() -> None:
    """Sanity-check the walk itself, so a silent zero cannot pass this file.

    Without this, a broken glob would make every check below vacuously
    true — the same class of failure the module exists to prevent.
    """
    modules = _test_modules()
    assert len(modules) > 20, f"expected the suite's test modules, found {len(modules)}"


@pytest.mark.parametrize("path", _test_modules(), ids=lambda p: p.name)
def test_no_shadowed_top_level_definitions(path: Path) -> None:
    """No test module may define the same class or function name twice."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    dupes = _duplicate_top_level_names(tree)
    assert not dupes, (
        f"{path.relative_to(_REPO_ROOT).as_posix()} defines these names more than "
        f"once: {dupes}. The later definition shadows the earlier one, so those "
        f"tests are silently not collected. Merge the bodies under one name."
    )


@pytest.mark.parametrize("path", _test_modules(), ids=lambda p: p.name)
def test_no_shadowed_methods_within_a_class(path: Path) -> None:
    """No test class may define the same method name twice."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    dupes = _duplicate_methods(tree)
    assert not dupes, (
        f"{path.relative_to(_REPO_ROOT).as_posix()} has shadowed methods: {dupes}. "
        f"The later definition replaces the earlier one, so its assertions never run."
    )
