# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Findings 5 and 9 — nothing pins what this project installs.

``requirements.txt`` is floors only::

    sqlalchemy>=2.0.0
    sqlglot>=23.0.0
    fastapi>=0.111.0

with no lockfile anywhere. Two consequences.

*Reproducibility.* Installing today and installing next month produce
different dependency trees, so "CI is green" says less than it appears to
about what is running on the exchange's server.

*Supply chain.* If any of these publishes a compromised release, the next
`pip install` or container rebuild takes it, with no review and no signal.

``sqlglot`` deserves singling out. The entire SQL security model of this
product rests on that parser producing a faithful AST -- the guard's
allowlist, the denied-column ACL and the statement-type refusal are all
decisions made about parse output. It is the one dependency whose silent
upgrade can change security behaviour without changing a line of this
repository's code.

An earlier external report checked for known CVEs, found none, and stopped
there. That answers "is anything vulnerable today", which is the less
useful question. The useful one is "what can change tomorrow without my
consent", and pinning is what answers it. Auditing (finding 9) is the
other half: pinning tells you nothing new arrives, auditing tells you when
what you pinned turns out to be bad.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_LOCK_CANDIDATES = [
    "requirements.lock",
    "requirements.txt.lock",
    "constraints.txt",
    "uv.lock",
    "poetry.lock",
]

#: A pinned line looks like `name==1.2.3`; `>=`, `~=` and bare names do not.
_PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]+\])?==\S+")


def _lockfile() -> Path | None:
    for name in _LOCK_CANDIDATES:
        candidate = _REPO_ROOT / name
        if candidate.exists():
            return candidate
    return None


class TestTheDependencyTreeIsPinned:
    def test_a_lockfile_is_committed(self):
        assert _lockfile() is not None, (
            "no lockfile in the repository. requirements.txt states floors, "
            f"so every install resolves differently. Expected one of: "
            f"{_LOCK_CANDIDATES}"
        )

    def test_the_lockfile_pins_exact_versions(self):
        lock = _lockfile()
        assert lock is not None, "no lockfile to check"
        lines = [
            ln.strip() for ln in lock.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith(("#", "-", "--"))
        ]
        unpinned = [ln for ln in lines if not _PINNED.match(ln)]
        assert not unpinned, (
            f"{lock.name} contains lines that are not exact pins: {unpinned[:5]}"
        )

    def test_the_parser_the_guard_depends_on_is_pinned(self):
        """Singled out because a silent upgrade here changes security
        behaviour with no change to this repository."""
        lock = _lockfile()
        assert lock is not None, "no lockfile to check"
        text = lock.read_text(encoding="utf-8").lower()
        assert re.search(r"^sqlglot==", text, re.M), (
            "sqlglot is not pinned to an exact version. security/sql_guard.py "
            "makes every one of its decisions from that parser's AST"
        )


class TestDependenciesAreAudited:
    """Pinning stops silent change; auditing tells you when what you pinned
    turns out to be bad. Neither substitutes for the other."""

    def test_pip_audit_is_available_to_developers(self):
        dev = (_REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
        assert "pip-audit" in dev, (
            "pip-audit is not in requirements-dev.txt, so 'run the audit' is "
            "an instruction nobody can follow without extra setup"
        )

    def test_ci_runs_a_dependency_audit(self):
        workflows = list((_REPO_ROOT / ".github" / "workflows").glob("*.yml"))
        assert workflows, "no CI workflows found"
        combined = "\n".join(w.read_text(encoding="utf-8") for w in workflows).lower()
        assert "pip-audit" in combined or "safety" in combined, (
            "no CI workflow performs a dependency audit. A vulnerability "
            "disclosed after this commit will never be noticed by this repo"
        )


class TestTheDocumentedInstallUsesThePins:
    """A lockfile the install instructions ignore protects nobody."""

    def test_the_readme_or_runbook_mentions_the_lockfile(self):
        lock = _lockfile()
        assert lock is not None, "no lockfile to check"
        docs = [_REPO_ROOT / "README.md", _REPO_ROOT / "docs" / "deployment-runbook.md"]
        text = "\n".join(p.read_text(encoding="utf-8") for p in docs if p.exists())
        assert lock.name in text, (
            f"{lock.name} exists but no install instruction references it, so "
            "deployments keep resolving from the unpinned requirements.txt"
        )
