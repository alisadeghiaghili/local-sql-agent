# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Keeps the SPDX header and the repository licence files from rotting away.

This codebase has been copied without attribution before. The
``SPDX-License-Identifier`` header on every first-party Python file is the
part of the licence that survives a single file being lifted out of the
repository -- ``NOTICE`` and ``LICENSE`` do not travel with it. A header
nobody checks drifts: new files get added without it, and someone can quietly
delete ``NOTICE`` to make the code look unencumbered. This module fails loudly
in both cases.

The file list is discovered via ``git ls-files``, not hardcoded, so a newly
added ``.py`` file without the header fails here by name instead of the check
silently going stale.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

_HEADER_LINES = (
    "# SPDX-License-Identifier: BUSL-1.1",
    "# Copyright (c) 2024-2026 Ali Sadeghi Aghili",
)

_LICENSOR = "Ali Sadeghi Aghili"


def _git_tracked_python_files() -> list[str]:
    """Return every ``*.py`` path tracked by git, relative to the repo root.

    Returns an empty list (never raises) when git or the repository is
    unavailable, so callers can skip cleanly instead of erroring out in
    environments where ``git`` isn't on PATH.
    """
    if shutil.which("git") is None:
        return []
    try:
        result = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


_TRACKED_PY_FILES = _git_tracked_python_files()


def test_git_ls_files_is_available() -> None:
    """Sanity-check the discovery step itself.

    Without this, a broken ``git`` invocation would make the parametrized
    header check below vacuously pass with zero cases -- exactly the kind
    of silent, always-green failure this file exists to prevent.
    """
    if not _TRACKED_PY_FILES:
        pytest.skip("git is unavailable or this checkout is not a git repository")
    assert len(_TRACKED_PY_FILES) > 100, (
        f"expected the repository's full set of tracked .py files, "
        f"found {len(_TRACKED_PY_FILES)}"
    )


@pytest.mark.parametrize("rel_path", _TRACKED_PY_FILES or ["<git unavailable>"])
def test_python_file_carries_spdx_header(rel_path: str) -> None:
    """Every tracked ``.py`` file must open with the two-line SPDX header.

    A shebang or a PEP 263 encoding comment, when present, is allowed to
    precede the header (both must stay on/near line 1 to be honoured by the
    interpreter) -- so this checks that the header lines appear, in order,
    among the first four lines rather than requiring them to be exactly
    lines 1-2 of every file.
    """
    if not _TRACKED_PY_FILES:
        pytest.skip("git is unavailable or this checkout is not a git repository")

    full_path = _REPO_ROOT / rel_path
    text = full_path.read_text(encoding="utf-8")
    lines = [line.rstrip("\r\n") for line in text.splitlines()[:4]]

    assert _HEADER_LINES[0] in lines, (
        f"{rel_path} is missing the SPDX header line {_HEADER_LINES[0]!r} "
        f"in its first four lines."
    )
    id_index = lines.index(_HEADER_LINES[0])
    assert lines[id_index + 1 : id_index + 2] == [_HEADER_LINES[1]], (
        f"{rel_path} must have {_HEADER_LINES[1]!r} on the line immediately "
        f"after the SPDX-License-Identifier line."
    )


@pytest.mark.parametrize(
    "filename",
    ["LICENSE", "NOTICE", "AGENTS.md", "llms.txt"],
)
def test_repository_licence_file_exists(filename: str) -> None:
    """The four licensing files this project relies on must all be present.

    If someone deletes ``NOTICE`` (or one of its companions) to make the
    code look unencumbered, this should go red rather than staying quiet.
    """
    path = _REPO_ROOT / filename
    assert path.is_file(), f"expected {filename!r} at the repository root"


@pytest.mark.parametrize(
    "filename",
    ["LICENSE", "NOTICE", "AGENTS.md"],
)
def test_repository_licence_file_names_the_licensor(filename: str) -> None:
    """``LICENSE``, ``NOTICE`` and ``AGENTS.md`` must all name the licensor.

    ``llms.txt`` is intentionally excluded: it is written for crawlers/LLM
    agents and may summarise the licence without repeating the licensor's
    name verbatim.
    """
    path = _REPO_ROOT / filename
    content = path.read_text(encoding="utf-8")
    assert _LICENSOR in content, (
        f"{filename} no longer names the licensor ({_LICENSOR!r}); "
        f"this looks like an attempt to obscure attribution."
    )
