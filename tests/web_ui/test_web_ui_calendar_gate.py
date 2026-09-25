# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""D4-calendar-gate: `isSequenceLabel` matched calendar words as a
SUBSTRING of the column name, not a whole word.

`test_web_ui_chart_form_choice.py` and `test_chart_engine_boundary.py`
already pin the incident where a *sorted ranking* got mistaken for a trend
because the "measure is along a sequence" claim was never tested. This file
pins a different, narrower way the same claim came out true when it should
not have: even with that check now real, `isSequenceLabel` decided whether
a label column counted as a sequence by testing
``String(labelKey).toLowerCase().includes(word)`` against a fixed list of
calendar words (``date``, ``day``, ``month``, ``quarter``, ``year`` and
their Persian equivalents). ``.includes`` is a SUBSTRING match, so any
column whose name merely *contains* one of those words as a fragment of a
longer, unrelated word borrowed the sequence treatment it never earned:

* ``"MonthlyCustomer"`` contains ``"month"`` -- a plain ranking of
  customers, string-typed, sorted however the query happened to sort it,
  was offered a **line chart** and assigned the **`trend`** job.
* the Persian ``"تاریخچه_مشتری"`` ("customer history") contains ``"تاریخ"``
  ("date") for the same reason.

No sorted-by-value setup is required to trigger this path, unlike the
original §0 incident -- the column *name alone* is enough, which is what
makes it a distinct defect deserving its own test rather than a variation
folded into the existing suites.

The fix (see the comment on ``isSequenceLabel`` in both
``web/js/render/chart.js`` and ``web/js/chart-engine/recommend.js``, which
must stay textually identical) matches WHOLE TOKENS: the column name is
split at camelCase boundaries and at runs of non-letter separators,
lowercased, and checked for an EXACT match against the calendar-word list.
"Monthly" no longer matches "month" because its only token is "monthly";
"تاریخچه" no longer matches "تاریخ" for the same reason. The
``labelType === "datetime"`` short-circuit -- the stronger, type-based
signal -- is untouched and still runs first.

Two levels, mirroring how the original incident is pinned:

* ``run_calendar_gate.mjs`` drives the chart-recommendation engine
  (``web/js/chart-engine/recommend.js``) through its public
  ``recommend(input, catalog)`` interface, staged the same way
  ``test_chart_engine_boundary.py`` stages it (``_stage``, reused here
  rather than duplicated).
* ``run_calendar_gate_render.mjs`` drives the renderer
  (``web/js/render/chart.js``) through ``chooseFramings`` directly, staged
  the same way ``test_web_ui_chart_form_choice.py`` stages it
  (``test_web_ui_result_shapes._prepare_copy``, reused here too).

Mutation-proof by construction: reverting either ``isSequenceLabel`` to its
old ``.includes(word)`` body makes the "MonthlyCustomer" / "تاریخچه_مشتری"
assertions in the corresponding harness fail immediately (see the manual
revert-and-restore proof described in the task report; not automated here
because these two suites are exactly what that proof exercises).
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.web_ui.test_chart_engine_boundary import _stage as _stage_engine
from tests.web_ui.test_web_ui_result_shapes import _prepare_copy

_ENGINE_HARNESS = Path(__file__).resolve().parent / "run_calendar_gate.mjs"
_RENDER_HARNESS = Path(__file__).resolve().parent / "run_calendar_gate_render.mjs"
_ENGINE_ENTRY = Path(__file__).resolve().parent.parent.parent / "web" / "js" / "chart-engine" / "recommend.js"
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_engine_does_not_treat_a_calendar_word_substring_as_a_sequence() -> None:
    """recommend() must not assign 'trend'/'line' to a label column whose
    NAME merely contains a calendar word inside a longer, unrelated word
    (e.g. "MonthlyCustomer", "تاریخچه_مشتری"), while a genuine calendar
    column ("Month") and a declared datetime column still get their line.
    """
    assert _ENGINE_ENTRY.is_file(), f"expected {_ENGINE_ENTRY} to exist"
    assert _ENGINE_HARNESS.exists(), f"expected {_ENGINE_HARNESS} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        entry = _stage_engine(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_ENGINE_HARNESS), str(entry)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        "calendar-gate engine check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        "harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_renderer_does_not_treat_a_calendar_word_substring_as_a_sequence() -> None:
    """chooseFramings() must not offer 'line'/'split-bar' for a label key
    whose name merely contains a calendar word as a substring
    ("MonthlyCustomer"), while "Month" itself still gets its line.
    """
    assert _RENDER_HARNESS.exists(), f"expected {_RENDER_HARNESS} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        table_mjs = _prepare_copy(Path(tmp))
        chart_mjs = table_mjs.with_name("chart.mjs")
        assert chart_mjs.exists(), "chart.mjs was not staged next to table.mjs"

        result = subprocess.run(
            [_NODE, str(_RENDER_HARNESS), str(chart_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        "calendar-gate renderer check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        "harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_both_issequencelabel_functions_stay_textually_identical() -> None:
    """chart.js and recommend.js deliberately re-derive `isSequenceLabel`
    instead of importing across the render/engine boundary (see
    recommend.js's module docstring). That duplication is only safe as
    long as a fix to one is not silently missed in the other, so this
    pins the function BODY (not the surrounding prose, which legitimately
    differs between the two files' narratives) as identical text.
    """
    chart_js = Path(__file__).resolve().parent.parent.parent / "web" / "js" / "render" / "chart.js"
    recommend_js = Path(__file__).resolve().parent.parent.parent / "web" / "js" / "chart-engine" / "recommend.js"

    import re

    pattern = re.compile(r"function isSequenceLabel\(labelKey, labelType\) \{[\s\S]*?\n\}")

    chart_match = pattern.search(chart_js.read_text(encoding="utf-8"))
    recommend_match = pattern.search(recommend_js.read_text(encoding="utf-8"))

    assert chart_match, "isSequenceLabel not found in web/js/render/chart.js"
    assert recommend_match, "isSequenceLabel not found in web/js/chart-engine/recommend.js"
    assert chart_match.group(0) == recommend_match.group(0), (
        "isSequenceLabel's function body differs between chart.js and "
        "recommend.js. The two modules deliberately re-derive this gate "
        "rather than share an import across the render/engine boundary "
        "(see recommend.js's header comment) -- that is only safe when a "
        "fix to one is applied identically to the other.\n"
        f"--- chart.js ---\n{chart_match.group(0)}\n"
        f"--- recommend.js ---\n{recommend_match.group(0)}"
    )
