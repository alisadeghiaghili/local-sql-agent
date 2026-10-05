# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Reading and atomically rewriting golden-set files.

The tools that build the evaluation set (``eval.cli verify``,
``scripts/harvest_golden.py``, ``scripts/golden_sheet.py``) all end by
writing a ``.jsonl`` of :class:`~eval.models.GoldenCase` lines into the
git-ignored ``eval_data/`` directory. Those files hold real questions and,
once verified, real result rows, so every write here

* goes to a temporary file in the same directory and is moved over the
  target with :func:`os.replace`, so a reader (or a crash) never sees a
  half-written set;
* keeps the previous version next to it as ``<name>.bak`` when asked, so a
  bad run is one ``mv`` away from undone;
* leaves the file owner-only (:func:`core.fileperms.restrict_file`).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

from core.fileperms import restrict_file
from eval.models import GoldenCase
from eval.runner import load_golden_cases


def dump_cases(cases: Iterable[GoldenCase]) -> str:
    """Serialise *cases* as ``.jsonl`` text, one case per line.

    Parameters
    ----------
    cases:
        The cases to write.

    Returns
    -------
    str
        UTF-8 safe text (non-ASCII kept as is, so Persian stays readable),
        ending with a newline unless *cases* is empty.

    Examples
    --------
    >>> text = dump_cases([GoldenCase(id="a", question="چند؟", expected_sql="SELECT 1")])
    >>> text.endswith("\\n"), "چند" in text
    (True, True)
    >>> dump_cases([])
    ''
    """
    return "".join(json.dumps(c.to_dict(), ensure_ascii=False) + "\n" for c in cases)


def write_text_atomic(path: str | Path, text: str, *, backup: bool = False) -> None:
    """Write *text* to *path* atomically (temp file in the same directory, then replace).

    Parameters
    ----------
    path:
        Destination. Parent directories are created.
    text:
        File content, written as UTF-8 with ``\\n`` line endings.
    backup:
        When true and *path* already exists, copy it to ``<path>.bak``
        first (replacing an older backup).

    Examples
    --------
    >>> import tempfile
    >>> d = Path(tempfile.mkdtemp())
    >>> target = d / "golden.jsonl"
    >>> write_text_atomic(target, "one\\n", backup=True)
    >>> write_text_atomic(target, "two\\n", backup=True)
    >>> target.read_text(encoding="utf-8"), (d / "golden.jsonl.bak").read_text(encoding="utf-8")
    ('two\\n', 'one\\n')
    >>> sorted(p.name for p in d.iterdir())
    ['golden.jsonl', 'golden.jsonl.bak']
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        restrict_file(tmp)
        if backup and target.exists():
            bak = target.with_name(target.name + ".bak")
            shutil.copy2(target, bak)
            restrict_file(bak)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_golden_cases(
    path: str | Path, cases: Iterable[GoldenCase], *, backup: bool = True
) -> None:
    """Atomically write *cases* to *path* as ``.jsonl`` (see the module docstring).

    Examples
    --------
    >>> import tempfile
    >>> d = Path(tempfile.mkdtemp())
    >>> write_golden_cases(d / "g.jsonl", [GoldenCase(id="a", question="q", expected_sql="SELECT 1")])
    >>> [c.id for c in load_golden_cases(d / "g.jsonl")]
    ['a']
    """
    write_text_atomic(path, dump_cases(cases), backup=backup)


def load_cases_or_empty(path: str | Path) -> list[GoldenCase]:
    """Load *path*, or ``[]`` when it does not exist or has no cases.

    Unlike :func:`eval.runner.load_golden_cases` an empty or missing file
    is not an error: it is how a golden set starts.

    Examples
    --------
    >>> load_cases_or_empty("/nonexistent/golden.jsonl")
    []
    """
    p = Path(path)
    if not p.exists() or not p.read_text(encoding="utf-8").strip():
        return []
    return load_golden_cases(p)
