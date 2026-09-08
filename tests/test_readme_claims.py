# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The README's coverage badge cannot outrun the gate that enforces it.

A coverage badge is a claim made to everyone who opens the repository, and
it is the kind of claim that rots silently: the number is written once, by
hand, and nothing afterwards refers back to it. Before this file, the
README carried ``Tests-2573`` and ``Version-4.6.1`` — the first was never
right and the second was three releases behind, and neither was noticed
because nothing was watching.

``tests/test_version.py`` already applies exactly this idea to the version
number, cross-checking ``core.__version__`` against ``CHANGELOG.md``'s
newest heading. This is the same protection for the badge whose truth
depends on a setting somewhere else entirely.

What is checkable here, and what is not
---------------------------------------
The real coverage percentage cannot be measured from inside a test run
that is itself the thing being measured. What *can* be checked is the
relationship between the advertised figure and the gate that keeps it
honest: if the badge says 92% while ``fail_under`` is 85, coverage can
fall seven points with every build still green and the README still
claiming otherwise. That gap is the failure mode, and it is a pure
configuration fact.

The test-count badge is deliberately not checked. Pytest's collected count
cannot be derived without re-running collection, and a check that fired on
every added test would train people to edit the README without reading it
— which is how ``2573`` got there.
"""

from __future__ import annotations

import configparser
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README = _REPO_ROOT / "README.md"
_SETUP_CFG = _REPO_ROOT / "setup.cfg"

#: How far the advertised figure may sit above the enforced gate. Coverage
#: genuinely drifts a little as code lands, so the badge is not required to
#: track it exactly -- but the distance has to stay small enough that the
#: gate is still holding the claim up rather than standing well below it.
_MAX_BADGE_OVER_GATE = 5.0

_COVERAGE_BADGE_RE = re.compile(r"!\[Coverage\]\(https://img\.shields\.io/badge/coverage-(\d+)%25")


def _advertised_coverage() -> int:
    text = _README.read_text(encoding="utf-8")
    match = _COVERAGE_BADGE_RE.search(text)
    assert match, (
        "no coverage badge found in README.md -- if it was removed on "
        "purpose, remove this test with it; if the badge's URL shape "
        "changed, update the pattern here so the claim stays watched"
    )
    return int(match.group(1))


def _enforced_gate() -> float:
    parser = configparser.ConfigParser()
    parser.read(_SETUP_CFG, encoding="utf-8")
    return parser.getfloat("coverage:report", "fail_under")


def test_the_badge_does_not_promise_more_than_the_gate_holds():
    advertised = _advertised_coverage()
    gate = _enforced_gate()
    assert advertised - gate <= _MAX_BADGE_OVER_GATE, (
        f"README advertises {advertised}% coverage but setup.cfg only "
        f"enforces {gate}%. Coverage could fall {advertised - gate:.0f} "
        "points with every build still green and the badge still claiming "
        f"{advertised}%. Either raise fail_under, or lower the badge to "
        "something the gate actually holds up."
    )


def test_the_badge_is_not_quietly_below_the_gate():
    """The inverse mistake: a gate raised without the badge following it
    understates the project and, worse, means the two numbers are once
    again unrelated."""
    advertised = _advertised_coverage()
    gate = _enforced_gate()
    assert advertised >= gate, (
        f"setup.cfg enforces {gate}% but the README advertises only "
        f"{advertised}% -- the gate moved and the badge did not follow"
    )


def test_no_hardcoded_version_badge_remains():
    """The version badge was static and went three releases stale. It is
    now served from GitHub's own release list, which cannot drift; this
    fails if a hand-written one ever comes back."""
    text = _README.read_text(encoding="utf-8")
    assert not re.search(r"shields\.io/badge/[Vv]ersion-\d", text), (
        "a hardcoded version badge is back in README.md. Use the dynamic "
        "one (img.shields.io/github/v/release/...) so it cannot go stale."
    )

# ---------------------------------------------------------------------------
# The enforcement badges
# ---------------------------------------------------------------------------

#: ``(badge label, the thing that has to still be true, how to check it)``.
#:
#: These three badges are a different kind of claim from the coverage
#: percentage: each says "a build step enforces this", and each links to
#: the step. That makes them checkable in a way a number is not -- the
#: failure mode is not drift, it is the backing quietly going away while
#: the badge stays. A guard deleted in a refactor leaves the README
#: promising something no longer true, and nothing else would notice.
_ENFORCEMENT_BADGES = (
    ("SQL guard", "security/sql_guard.py", "sqlglot"),
    ("Domain-free engine", "tests/test_no_domain_literals.py", None),
    ("Doctests", ".github/workflows/ci.yml", "--doctest-modules"),
)


@pytest.mark.parametrize("label, target, must_contain", _ENFORCEMENT_BADGES)
def test_each_enforcement_badge_links_to_something_that_still_exists(
    label, target, must_contain,
):
    readme = _README.read_text(encoding="utf-8")
    assert f"[![{label}]" in readme, (
        f"the {label!r} badge is gone from README.md -- if that was "
        "deliberate, drop its row from _ENFORCEMENT_BADGES too"
    )
    assert f"]({target})" in readme, (
        f"the {label!r} badge no longer links to {target} -- a badge that "
        "does not point at its own evidence is just a sticker"
    )

    path = _REPO_ROOT / target
    assert path.exists(), (
        f"README advertises {label!r} and links to {target}, which does "
        "not exist. The claim outlived the thing enforcing it."
    )
    if must_contain is not None:
        assert must_contain in path.read_text(encoding="utf-8"), (
            f"{target} no longer contains {must_contain!r}, so the "
            f"{label!r} badge is claiming an enforcement that is gone"
        )
