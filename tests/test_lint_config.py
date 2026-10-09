# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The lint gate is pinned: exact ruff version, explicit rule set, CI job.

A lint gate whose verdict can change without a commit in this repository
(a ruff release re-tuning its defaults, a job nobody wired up) is not a
gate. These tests fail if any of the three pins is loosened.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_ruff_is_pinned_to_an_exact_version_in_dev_requirements() -> None:
    dev = (_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert re.search(r"^ruff==\d+\.\d+\.\d+\s*$", dev, re.M), (
        "ruff must be pinned with == in requirements-dev.txt"
    )


def test_rule_set_is_explicit_and_not_widened() -> None:
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lint = config["tool"]["ruff"]["lint"]
    assert lint["select"] == ["E4", "E7", "E9", "F"]
    assert "ignore" not in lint and "per-file-ignores" not in lint, (
        "findings are fixed, not ignored; see the commit that introduced this gate"
    )


def test_ci_has_a_lint_job_that_runs_ruff_check() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"^  lint:\s*$", workflow, re.M)
    assert "ruff check ." in workflow
    assert "requirements-dev.txt" in workflow, (
        "the lint job must take its ruff version from requirements-dev.txt"
    )
