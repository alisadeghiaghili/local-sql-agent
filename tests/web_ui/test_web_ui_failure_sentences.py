# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Every failure the analyst sees gets a Persian, analyst-facing sentence.

``docs/design/DESIGN-INVARIANTS.md`` §8 requires "what happened, in the
analyst's terms, not the system's" for every failure state. Before this
task, ``web/js/render/turn.js`` broke that rule twice over:

* the ``MODEL_UNAVAILABLE`` banner's "why" line was ``turn.error.message``
  -- raw English straight from ``session/engine.py`` -- inside an
  otherwise entirely Persian UI;
* every error code without its own bespoke ``case`` fell through to
  ``default:``, whose LEAD sentence *was* ``turn.error.message``
  (``MODEL_TIMEOUT``, ``OUT_OF_SCOPE``, ``NO_PREVIOUS_TURN``,
  ``EMPTY_SQL_RESPONSE``, ...);
* every guard rejection except ``denied_column`` got one generic sentence
  regardless of ``turn.guard.reason``.

This drives the REAL ``web/js/render/turn.js`` (and its full real render
dependency chain, same staging as ``test_web_ui_turn_anatomy.py``) under
Node (see ``run_failure_sentences.mjs``) and asserts, for every code in
the owner-approved mapping table: the rendered banner's lead is the exact
Persian sentence, the English backend message a fixture supplies is
ABSENT from the card's text, and the expected action buttons are present
(and only when their callback was actually wired). It also asserts an
unrecognised code falls back to the ``INTERNAL_ERROR`` sentence, that
``QUERY_EXECUTION_ERROR``/``LLM_OUTPUT_TRUNCATED``/``FORBIDDEN_SQL`` are
untouched, and that every closed-set ``GuardVerdict.reason`` renders its
own sentence.

Database-agnostic by construction: every fixture value (turn ids,
questions, the guard's ``subject`` column name, the guard's rejected-table
name) is synthetic text invented for this test, never a real schema/table
identifier.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_WEB_JS = _REPO / "web" / "js"
_HARNESS = Path(__file__).resolve().parent / "run_failure_sentences.mjs"
_NODE = shutil.which("node")


def _rewrite_imports(src: str, mapping: dict[str, str]) -> str:
    for old, new in mapping.items():
        src = src.replace(old, new)
    return src


def _stage(tmp: Path) -> Path:
    """Copy turn.js and its full real render dependency chain into *tmp*
    as ESM (``.mjs``), import paths fixed up. Returns the path to the
    copied ``turn.mjs``. Mirrors ``test_web_ui_turn_anatomy.py``'s
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
def test_every_failure_code_gets_its_persian_sentence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        turn_mjs = _stage(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(turn_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    assert result.returncode == 0, (
        f"Failure-sentence check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_FAILURE_SENTENCES_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
