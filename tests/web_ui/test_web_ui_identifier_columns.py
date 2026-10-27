# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A numeric column named like an identifier is a label, not a measure.

"All trading rings with their codes" returned (Name, ID) rows, and
``web/js/render/table.js::determineShape`` took the one numeric column of a
two-column result as the measure, so the ids were drawn as a bar chart.
``web/js/num.js::isIdentifierName`` now says which names are identifiers, and
the shape picker, ``chart.js`` and ``chart-engine/recommend.js`` (which keeps its
own copy: it may not import) no longer take such a column as the measure.

``run_identifier_columns.mjs`` covers the name rule, the shapes and the
agreement of the two copies; ``run_result_shapes.mjs`` (scenario 6) covers what
is rendered; ``run_shape_pins.mjs`` pins what must not change.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.web_ui import NODE_TIMEOUT_SECONDS
from tests.web_ui.test_chart_engine_boundary import _stage as _stage_engine
from tests.web_ui.test_web_ui_result_shapes import _prepare_copy as _stage_table

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HARNESS = Path(__file__).resolve().parent / "run_identifier_columns.mjs"
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_a_numeric_identifier_is_not_a_measure(tmp_path: Path) -> None:
    table_dir = tmp_path / "table"
    engine_dir = tmp_path / "engine"
    table_dir.mkdir()
    engine_dir.mkdir()
    table_mjs = _stage_table(table_dir)
    engine_mjs = _stage_engine(engine_dir)

    result = subprocess.run(
        [_NODE, str(_HARNESS), str(table_mjs), str(engine_mjs)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=NODE_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"identifier-column check failed.\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion.\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_the_engine_does_not_import_the_shared_rule() -> None:
    """``recommend.js`` repeats ``isIdentifierName`` instead of importing it (spec §4.2)."""
    engine = (_REPO_ROOT / "web" / "js" / "chart-engine" / "recommend.js").read_text(encoding="utf-8")
    assert not re.search(r'from\s+["\'][^"\']*num\.js["\']', engine)
    assert "function isIdentifierName" in engine


def test_every_place_that_picks_a_measure_asks_the_rule() -> None:
    """Shape picker, chart renderer and recommender all consult ``isIdentifierName``."""
    for relative in ("render/table.js", "render/chart.js", "chart-engine/recommend.js"):
        text = (_REPO_ROOT / "web" / "js" / relative).read_text(encoding="utf-8")
        assert "isIdentifierName(" in text, f"{relative} no longer asks isIdentifierName before taking a measure"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
