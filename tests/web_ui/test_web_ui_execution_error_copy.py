# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``QUERY_EXECUTION_ERROR`` must never show raw backend English under its
Persian lead.

Before this task, ``web/js/render/turn.js``'s ``QUERY_EXECUTION_ERROR`` case
passed ``turn.error.message`` straight through as the banner's "why" line.
That message is either the database's own sentence for a bad statement, or
``database/errors.py``'s generic fallback (``_GENERIC_STATEMENT_MESSAGE``,
``"The database rejected the query."``) -- either way, plain English shown
directly under an otherwise entirely Persian sentence. This task introduces
``DB_GENERIC_REJECTION`` (checked against the backend by
``tests/test_failure_copy_parity.py``) to suppress the fallback case
entirely, and an optional ``detail`` parameter on ``buildFailureBanner`` to
render a database-specific message as a labelled, ``dir="ltr"`` technical
detail instead of the "why" line.

This drives the REAL ``web/js/render/turn.js`` (and its full real render
dependency chain, same staging as ``test_web_ui_failure_sentences.py``)
under Node (see ``run_execution_error_copy.mjs``) and asserts, for each
scenario:

* (a) the generic fallback message: the card shows the existing Persian
      lead and neither the English fallback text nor any detail line;
* (b) a database-specific message (e.g. an invalid-column error): the
      Persian label is exactly the expected 17-character string, contains
      no ASCII letters, and the message sits inside a ``dir="ltr"``
      element;
* (c) an HTML-injection attempt as the message: it renders as inert text,
      never causes an element to be constructed from it, and never reaches
      ``innerHTML``/``outerHTML``/``insertAdjacentHTML`` anywhere in the
      rendered tree;
* (d) ``DATABASE_UNAVAILABLE`` and ``QUERY_TIMEOUT`` (codes this task's
      detail-line logic never touches) still show none of their own
      backend English.

Database-agnostic by construction: every fixture value (turn ids, the
question text, the synthetic column name in the injection-safe message) is
either synthetic or copied verbatim from ``database/errors.py``'s own
message constants -- never a real schema/table identifier.
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_WEB_JS = _REPO / "web" / "js"
_HARNESS = Path(__file__).resolve().parent / "run_execution_error_copy.mjs"
_NODE = shutil.which("node")


def _rewrite_imports(src: str, mapping: dict[str, str]) -> str:
    for old, new in mapping.items():
        src = src.replace(old, new)
    return src


def _stage(tmp: Path) -> Path:
    """Copy turn.js and its full real render dependency chain into *tmp*
    as ESM (``.mjs``), import paths fixed up. Returns the path to the
    copied ``turn.mjs``. Mirrors ``test_web_ui_failure_sentences.py``'s
    ``_stage`` -- same file set, same rewrite mapping, kept as its own
    copy per this test directory's existing one-staging-helper-per-harness
    convention."""
    render = _WEB_JS / "render"
    files = {
        "turn.js": render / "turn.js",
        "pipeline.js": render / "pipeline.js",
        "assumptions.js": render / "assumptions.js",
        "table.js": render / "table.js",
        "chart.js": render / "chart.js",
        "export.js": _WEB_JS / "export.js",
        "llm-status.js": render / "llm-status.js",
        "feedback.js": render / "feedback.js",
        "num.js": _WEB_JS / "num.js",
        "sql-display.js": _WEB_JS / "sql-display.js",
        "icons.js": _WEB_JS / "icons.js",
    }
    for name, path in files.items():
        assert path.exists(), path
        src = path.read_text(encoding="utf-8")
        src = _rewrite_imports(
            src,
            {
                'from "../num.js"': 'from "./num.mjs"',
                'from "./num.js"': 'from "./num.mjs"',
                'from "../sql-display.js"': 'from "./sql-display.mjs"',
                'from "../icons.js"': 'from "./icons.mjs"',
                'from "../export.js"': 'from "./export.mjs"',
                'from "./pipeline.js"': 'from "./pipeline.mjs"',
                'from "./assumptions.js"': 'from "./assumptions.mjs"',
                'from "./table.js"': 'from "./table.mjs"',
                'from "./llm-status.js"': 'from "./llm-status.mjs"',
                'from "./feedback.js"': 'from "./feedback.mjs"',
                'from "./chart.js"': 'from "./chart.mjs"',
            },
        )
        (tmp / name.replace(".js", ".mjs")).write_text(src, encoding="utf-8")
    return tmp / "turn.mjs"


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_execution_error_copy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        turn_mjs = _stage(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(turn_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        pytest.fail(f"run_execution_error_copy.mjs exited with code {result.returncode}")

    assert "ALL_EXECUTION_ERROR_COPY_TESTS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
