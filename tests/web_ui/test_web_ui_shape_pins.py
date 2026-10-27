# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pins for the result-shape and chart-measure decisions, written before the
identifier rule changes how a measure is picked.

``run_identifier_columns.mjs`` makes a numeric column whose name is an
identifier (``ID``, ``RingID``, ``BrokerCode``, ``کد``) stop being a measure.
Everything else the UI decides must stay as it is, and this test says what that
is: ``determineShape`` for ordinary measures (including names that merely end
in "id" or "code", such as ``Paid`` and ``Barcode``), the row-count and
column-count rules, and the chart engine's label/measure split. It passes
before and after the rule.

Staged the way ``test_web_ui_result_shapes`` stages ``table.js`` and its
imports, plus ``chart-engine/recommend.js`` as ``test_chart_engine_boundary``
stages it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.web_ui import NODE_TIMEOUT_SECONDS
from tests.web_ui.test_chart_engine_boundary import _stage as _stage_engine
from tests.web_ui.test_web_ui_result_shapes import _prepare_copy as _stage_table

_HARNESS = Path(__file__).resolve().parent / "run_shape_pins.mjs"
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_shape_and_measure_decisions_for_ordinary_columns_are_unchanged(tmp_path: Path) -> None:
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
        f"shape pins failed.\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion.\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
