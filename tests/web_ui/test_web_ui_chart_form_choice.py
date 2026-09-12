# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The chart type must follow the data's job, not the call order.

A real result — ten customers ranked by traded volume — rendered as a **line
chart** with company names along the x axis, under the headline
«مقدار … رو به کاهش بود» ("the amount was declining"). Nothing was declining.
The rows arrive sorted by value, so a *ranking* drew the shape of a downward
trend, and the headline then described that shape as though time had passed.

``chooseFramings()`` offers a ``line`` framing first and unconditionally. Its
own stated reason is «سنجه در طول یک توالی است» — "the measure is along a
sequence" — an assertion the function never tests. Company names are not a
sequence. A line drawn between them asserts order and continuity the data does
not have, and that is the most consequential kind of chart mistake available:
it does not mislabel the answer, it invents a different one.

The same unchecked assumption produces the «نیمهٔ دوم/اول» figure. Halves are a
property of an ordered series; ten customers have no first half.

Why this is a harness test and not a CSS check
----------------------------------------------
Unlike the direction and contrast checks in this directory, the defect is not
a declaration — it is a *decision* made in JavaScript from the data. So this
runs the real ``chooseFramings``/``chooseFocus`` against real row shapes
through the existing Node harness (``run_chart_form_choice.mjs``), reusing
``test_web_ui_result_shapes._prepare_copy`` rather than duplicating its
import-rewriting, which is intricate and already carries its own reasoning.

The fifth scenario covers something separate that the same screenshot
exposed: when the focus lands on the **first** point — which, for a
descending ranking, it always does — the context segment is a single point,
the ``length > 1`` guard skips the context polyline, and the whole line
renders in the focus colour. The emphasis colours and weights corrected
earlier could never be seen for that entire class of result. The fix may go
either way; what it may not do is leave the outcome to an off-by-one.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.web_ui.test_web_ui_result_shapes import _prepare_copy

_HARNESS = Path(__file__).resolve().parent / "run_chart_form_choice.mjs"
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_the_chart_form_follows_the_datas_job() -> None:
    """Ranking data gets a bar; a sequence gets a line; neither borrows the
    other's headline."""
    assert _HARNESS.exists(), f"expected {_HARNESS} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        # _prepare_copy returns table.mjs, but every symbol this harness
        # needs (chooseFramings, chooseFocus) is exported from chart.mjs,
        # which it stages alongside.
        table_mjs = _prepare_copy(Path(tmp))
        chart_mjs = table_mjs.with_name("chart.mjs")
        assert chart_mjs.exists(), "chart.mjs was not staged next to table.mjs"

        result = subprocess.run(
            [_NODE, str(_HARNESS), str(chart_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    assert result.returncode == 0, (
        "chart form-choice check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        "harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_the_reason_string_no_longer_claims_an_unchecked_sequence() -> None:
    """The line framing's own justification said the measure runs along a
    sequence while nothing verified that. Whatever wording replaces it, the
    claim must either be true by construction or gone — a reason that
    asserts what the code does not check is how this defect stayed
    invisible through several reviews of the same file.
    """
    chart_js = Path(__file__).resolve().parent.parent.parent / "web" / "js" / "render" / "chart.js"
    src = chart_js.read_text(encoding="utf-8")

    line_block = src.split('kind: "line"', 1)
    assert len(line_block) == 2, "the line framing is gone from chooseFramings"
    nearby = line_block[1][:600]

    if "توالی" in nearby:
        assert "labelType" in src or "isSequence" in src or "isTemporal" in src, (
            "the line framing still claims the measure is along a sequence "
            "(«توالی») while chooseFramings tests nothing about the label "
            "axis. Either check it or stop claiming it"
        )
