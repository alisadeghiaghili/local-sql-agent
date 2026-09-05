# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test for result-shape selection: `web/js/render/table.js`'s
``determineShape`` and the chart framing/focus logic in
``web/js/render/chart.js``.

Before this change, ``table.js`` had no shape logic beyond an empty check
-- every result rendered as a plain table regardless of its actual shape
(a single scalar total looked exactly like a 300-row detail dump), and
there was no chart renderer at all. This test drives the REAL source
under Node against contract-shaped ``TurnResult`` objects (matching
``session/models.py::TurnResult`` / ``session/engine.py::_infer_type``'s
type vocabulary: ``"number" | "string" | "boolean" | "datetime"``) and
asserts the chosen presentation for each shape -- not merely that a
function named ``determineShape`` exists, which would not catch a wrong
shape decision (e.g. a 1x1 numeric result rendered as a one-row table, or
a guard-rejected 0-row result wrongly blamed on an assumption).

See ``run_result_shapes.mjs`` in this directory for the full scenario
list and the minimal in-Node DOM shim it uses (web/ ships no
package.json / node_modules by design, so this brings no dependency on
jsdom or any other package -- same spirit as
``run_auth_boundary.mjs``'s mocked fetch/localStorage).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WEB_JS = _REPO_ROOT / "web" / "js"
_NUM_JS = _WEB_JS / "num.js"
_TABLE_JS = _WEB_JS / "render" / "table.js"
_CHART_JS = _WEB_JS / "render" / "chart.js"
_ASSUMPTIONS_JS = _WEB_JS / "render" / "assumptions.js"
_EXPORT_JS = _WEB_JS / "export.js"
_HARNESS = Path(__file__).resolve().parent / "run_result_shapes.mjs"

_NODE = shutil.which("node")

# The only changes made to the real source before handing it to Node: the
# three internal import specifiers get rewritten to their sibling `.mjs`
# copies (Node's ESM loader needs an `.mjs` extension or a controlling
# package.json to parse `export`/`import` syntax, and web/ deliberately
# ships neither -- see web/README.md's "no build step, no package.json").
# Anything else reaching this test is byte-identical to the real source.
_CHART_IMPORT = re.compile(r'^import \{ renderChartAndTable \} from "\./chart\.js";$', re.MULTILINE)
_EXPORT_IMPORT = re.compile(r'^import \{ downloadResultAsCsv \} from "\.\./export\.js";$', re.MULTILINE)
_ASSUMPTIONS_IMPORT = re.compile(r'^import \{ SOURCE_LABELS \} from "\./assumptions\.js";$', re.MULTILINE)
_TABLE_IMPORT_IN_CHART = re.compile(
    r'^import \{ renderTableOnly, renderExportRow, fmtCell \} from "\./table\.js";$', re.MULTILINE,
)


def _rewrite_num_import(src: str) -> str:
    """Point a staged module's `num.js` import at the staged `num.mjs`.

    Every renderer imports the shared number formatter (see web/js/num.js),
    and the staged copies all sit flat in one directory, so both the
    "../num.js" and "./num.js" forms resolve to the same sibling here.
    Unlike the other rewrites this one is not asserted to match: not every
    staged module imports it, and requiring one would break the moment a
    module legitimately has no numbers in it.
    """
    return src.replace('from "../num.js"', 'from "./num.mjs"').replace(
        'from "./num.js"', 'from "./num.mjs"'
    )


def _subn_or_fail(pattern: re.Pattern[str], replacement: str, text: str, what: str) -> str:
    fixed, n = pattern.subn(replacement, text)
    assert n == 1, (
        f"web/js source no longer matches the import this test expects ({what}) -- "
        "update the regex in test_web_ui_result_shapes.py to match the real source "
        "instead of silently testing stale code."
    )
    return fixed


def _prepare_copy(tmp_path: Path) -> Path:
    """Copy table.js/chart.js/assumptions.js/export.js into *tmp_path* as
    ESM (``.mjs``), import paths fixed up. Returns the path to the copied
    ``table.mjs``.
    """
    table_src = _TABLE_JS.read_text(encoding="utf-8")
    chart_src = _CHART_JS.read_text(encoding="utf-8")
    assumptions_src = _ASSUMPTIONS_JS.read_text(encoding="utf-8")
    export_src = _EXPORT_JS.read_text(encoding="utf-8")

    table_src = _subn_or_fail(_CHART_IMPORT, 'import { renderChartAndTable } from "./chart.mjs";', table_src, "table.js -> chart.js")
    table_src = _subn_or_fail(_EXPORT_IMPORT, 'import { downloadResultAsCsv } from "./export.mjs";', table_src, "table.js -> export.js")
    table_src = _subn_or_fail(_ASSUMPTIONS_IMPORT, 'import { SOURCE_LABELS } from "./assumptions.mjs";', table_src, "table.js -> assumptions.js")
    chart_src = _subn_or_fail(_TABLE_IMPORT_IN_CHART, 'import { renderTableOnly, renderExportRow, fmtCell } from "./table.mjs";', chart_src, "chart.js -> table.js")

    table_mjs = tmp_path / "table.mjs"
    chart_mjs = tmp_path / "chart.mjs"
    assumptions_mjs = tmp_path / "assumptions.mjs"
    export_mjs = tmp_path / "export.mjs"
    table_src = _rewrite_num_import(table_src)
    chart_src = _rewrite_num_import(chart_src)
    assumptions_src = _rewrite_num_import(assumptions_src)
    export_src = _rewrite_num_import(export_src)
    table_mjs.write_text(table_src, encoding="utf-8")
    chart_mjs.write_text(chart_src, encoding="utf-8")
    assumptions_mjs.write_text(assumptions_src, encoding="utf-8")
    export_mjs.write_text(export_src, encoding="utf-8")
    # num.js is shared by every renderer (see web/js/num.js): one place
    # that decides how a number looks, after seven modules each decided
    # separately. Staging it here is what lets those imports resolve.
    num_mjs = tmp_path / "num.mjs"
    num_mjs.write_text(_NUM_JS.read_text(encoding="utf-8"), encoding="utf-8")
    return table_mjs


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_result_shape_selection_matches_the_contract_driven_shape_table() -> None:
    assert _TABLE_JS.exists(), f"expected {_TABLE_JS} to exist"
    assert _CHART_JS.exists(), f"expected {_CHART_JS} to exist"
    assert _ASSUMPTIONS_JS.exists(), f"expected {_ASSUMPTIONS_JS} to exist"
    assert _EXPORT_JS.exists(), f"expected {_EXPORT_JS} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        table_mjs = _prepare_copy(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(table_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    assert result.returncode == 0, (
        f"result-shape selection check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
