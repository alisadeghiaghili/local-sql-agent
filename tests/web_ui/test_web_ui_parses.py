# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Every shipped ``web/`` JavaScript file must actually parse.

Why this exists
---------------
``web/js/main.js`` carried a syntax error through four releases. Line 170
wrote a newline separator as a literal line break inside a ``"..."``
string:

.. code-block:: javascript

    setHealth(h.api, h.llm, h.db, lines.join("
    "));

A JavaScript string literal cannot span lines (a template literal can),
so the whole module failed to parse — and ``main.js`` is the analyst UI's
entry point, so nothing in the page ran at all.

Nothing caught it. The ``tests/web_ui/`` harnesses stage the modules they
exercise **explicitly**, importing ``state.js`` and ``config.js`` and
re-implementing ``main.js``'s boot order in the harness rather than
importing it — ``main.js`` appears in ``run_live_default.mjs`` only
inside a comment. A file no harness imports is never parsed, and a
regex assertion over its source text is not parsing: it matches broken
code exactly as happily as working code.

So this test asserts the weakest possible property — *it is syntactically
valid JavaScript* — over **every** file, discovered by walking the tree
rather than from a list someone has to remember to extend. The behaviour
of any individual module stays the business of the harness that
exercises it; this only guarantees there is something there to exercise.

The ``.mjs`` detail is not cosmetic
-----------------------------------
``node --check`` decides how to parse from the file extension. Handed a
``.js`` file containing ``import``/``export``, it does not report the
module syntax as an error — it also does not reliably report a *real*
error inside one. The first version of this check ran against ``.js``
paths, passed cleanly, and was worthless; the same run against a
``.mjs`` copy found the bug immediately. So each file is copied to a
``.mjs`` temporary path before checking, and
:func:`test_the_checker_itself_rejects_broken_syntax` pins that the
checker can still fail — a green check from a checker that cannot fail
is worse than no check, because it is mistaken for coverage.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WEB_DIR = _REPO_ROOT / "web"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH; the web UI harnesses need it too")
    return node


def _check(source: str) -> subprocess.CompletedProcess:
    """Run ``node --check`` over *source*, as a module."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "candidate.mjs"
        target.write_text(source, encoding="utf-8")
        return subprocess.run(
            [_node(), "--check", str(target)],
            capture_output=True,
            text=True,
            timeout=60,
        )


def _js_files() -> list[Path]:
    return sorted(_WEB_DIR.rglob("*.js"))


def test_there_are_js_files_to_check():
    """Guards against the whole suite passing because the glob broke."""
    files = _js_files()
    assert len(files) >= 10, (
        f"expected the web UI's JavaScript modules under {_WEB_DIR}, found "
        f"{len(files)} — has the tree moved?"
    )


def test_the_checker_itself_rejects_broken_syntax():
    """The check must be able to fail.

    Specifically it must reject the exact shape that shipped: a literal
    newline inside a plain string literal.
    """
    broken = 'export const s = ["a", "b"].join("\n");\n'
    result = _check(broken)
    assert result.returncode != 0, (
        "node --check accepted a literal newline inside a string literal — "
        "the checker cannot fail, so every other assertion in this module "
        "is meaningless"
    )
    assert "SyntaxError" in result.stderr


@pytest.mark.parametrize(
    "path", _js_files(), ids=lambda p: str(p.relative_to(_REPO_ROOT)).replace("\\", "/")
)
def test_every_web_js_file_parses(path: Path):
    result = _check(path.read_text(encoding="utf-8"))
    assert result.returncode == 0, (
        f"{path.relative_to(_REPO_ROOT)} is not syntactically valid "
        f"JavaScript:\n{result.stderr}"
    )
