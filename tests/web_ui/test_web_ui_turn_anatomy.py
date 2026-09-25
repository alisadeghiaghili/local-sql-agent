# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Turn anatomy contract: trust surface above the result (DESIGN.md §5.2).

Assumption chips and resolved_question must appear BEFORE the result node
in the DOM — api-contract v2 §5/§7. Pipeline becomes a slim stage strip;
the full list lives in the details drawer. Guard verdict is on the outcome
line and SQL header.
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_WEB_JS = _REPO / "web" / "js"
_HARNESS = Path(__file__).resolve().parent / "run_turn_anatomy.mjs"
_NODE = shutil.which("node")


def _rewrite_imports(src: str, mapping: dict[str, str]) -> str:
    for old, new in mapping.items():
        src = src.replace(old, new)
    return src


def _stage(tmp: Path) -> Path:
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


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH")
def test_turn_anatomy_keeps_trust_surface_above_result() -> None:
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
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL_ANATOMY_PASSED" in result.stdout, result.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
