# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test for SQL display formatting and syntax highlighting.

``web/js/sql-display.js`` owns the generated-SQL look; ``turn.js`` calls it:

* display = formatSqlForDisplay(Turn.sql_display || Turn.sql). Multi-line
  strings (backend ``pretty_sql``, scenario SQL) are left alone; one-liners
  go through the vendored ``sql-formatter`` (T-SQL, upper keywords, tab 2);
* highlighting = Prism + a T-SQL patch (bracketed identifiers, ``N'…'``);
* copy = Turn.sql_display || Turn.sql verbatim — never the DOM, never the
  client-prettified variant.

Both decoration layers must be presentation ONLY. The risk this test exists
to catch: a future change that makes the copy button read the SQL back out
of the (now-decorated, now-prettified) DOM, or a highlighting/formatting
failure that blanks or corrupts the visibly rendered SQL.

This drives the REAL ``web/js/render/turn.js`` (and its full real render
dependency chain including ``sql-display.js``) under Node (see
``run_sql_highlight.mjs``) with mocked ``window.Prism`` /
``window.sqlFormatter`` / ``navigator.clipboard``, and asserts:

* Prism highlighting actually runs (real ``.token`` elements) AND reading
  the highlighted element's ``textContent`` back still equals the exact
  string that was highlighted;
* the copy button copies the exact Turn object string for plain ``sql``,
  distinct ``sql_display``, prettified one-liners, and both failure modes;
* client prettify reformats one-liners, never re-prettifies multi-line
  input, and a throwing formatter falls back to the raw text;
* with no ``window.Prism`` at all, and separately with a throwing
  ``Prism.highlight``, the SQL still renders as plain, uncorrupted text
  and copy still works.
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
_SQL_DISPLAY_JS = _WEB_JS / "sql-display.js"
_ICONS_JS = _WEB_JS / "icons.js"
_TURN_JS = _WEB_JS / "render" / "turn.js"
_PIPELINE_JS = _WEB_JS / "render" / "pipeline.js"
_ASSUMPTIONS_JS = _WEB_JS / "render" / "assumptions.js"
_TABLE_JS = _WEB_JS / "render" / "table.js"
_CHART_JS = _WEB_JS / "render" / "chart.js"
_EXPORT_JS = _WEB_JS / "export.js"
_LLM_STATUS_JS = _WEB_JS / "render" / "llm-status.js"
_FEEDBACK_JS = _WEB_JS / "render" / "feedback.js"
_HARNESS = Path(__file__).resolve().parent / "run_sql_highlight.mjs"

_NODE = shutil.which("node")

# The only changes made to the real source before handing it to Node: every
# internal import specifier gets rewritten to its sibling `.mjs` copy
# (Node's ESM loader needs an `.mjs` extension or a controlling package.json
# to parse `export`/`import` syntax, and web/ deliberately ships neither --
# see web/README.md's "no build step, no package.json"). Anything else
# reaching this test is byte-identical to the real source.
_SQL_DISPLAY_IMPORT_IN_TURN = re.compile(
    r'^import \{ copySourceOfTruth, displaySqlForTurn, highlightSql \} from "\.\./sql-display\.js";$',
    re.MULTILINE,
)
_ICONS_IMPORT_IN_ASSUMPTIONS = re.compile(
    r'^import \{ icon \} from "\.\./icons\.js";$', re.MULTILINE,
)
_PIPELINE_IMPORT = re.compile(
    r'^import \{ renderPipeline, renderStageStrip \} from "\./pipeline\.js";$', re.MULTILINE
)
_ASSUMPTIONS_IMPORT_IN_TURN = re.compile(
    r'^import \{ renderBasis, renderAssumptions, renderClarifications \} from "\./assumptions\.js";$', re.MULTILINE,
)
_TABLE_IMPORT_IN_TURN = re.compile(r'^import \{ renderResult, renderWarnings \} from "\./table\.js";$', re.MULTILINE)
_LLM_STATUS_IMPORT = re.compile(
    r'^import \{ renderLlmStatus, answerWasTruncated, renderTruncationQualifier \} from "\./llm-status\.js";$',
    re.MULTILINE,
)
_FEEDBACK_IMPORT_IN_TURN = re.compile(
    r'^import \{ renderFeedbackControl \} from "\./feedback\.js";$', re.MULTILINE,
)
_CHART_IMPORT_IN_TABLE = re.compile(r'^import \{ renderChartAndTable \} from "\./chart\.js";$', re.MULTILINE)
_EXPORT_IMPORT_IN_TABLE = re.compile(r'^import \{ downloadResultAsCsv \} from "\.\./export\.js";$', re.MULTILINE)
_ASSUMPTIONS_IMPORT_IN_TABLE = re.compile(r'^import \{ SOURCE_LABELS \} from "\./assumptions\.js";$', re.MULTILINE)
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
        "update the regex in test_web_ui_sql_highlight.py to match the real source "
        "instead of silently testing stale code."
    )
    return fixed


def _prepare_copies(tmp_path: Path) -> Path:
    """Copy turn.js and its full real render dependency chain into
    *tmp_path* as ESM (``.mjs``), import paths fixed up. Returns the path
    to the copied ``turn.mjs``.
    """
    turn_src = _TURN_JS.read_text(encoding="utf-8")
    pipeline_src = _PIPELINE_JS.read_text(encoding="utf-8")
    assumptions_src = _ASSUMPTIONS_JS.read_text(encoding="utf-8")
    table_src = _TABLE_JS.read_text(encoding="utf-8")
    chart_src = _CHART_JS.read_text(encoding="utf-8")
    export_src = _EXPORT_JS.read_text(encoding="utf-8")
    llm_status_src = _LLM_STATUS_JS.read_text(encoding="utf-8")
    feedback_src = _FEEDBACK_JS.read_text(encoding="utf-8")
    sql_display_src = _SQL_DISPLAY_JS.read_text(encoding="utf-8")
    icons_src = _ICONS_JS.read_text(encoding="utf-8")

    turn_src = _subn_or_fail(
        _SQL_DISPLAY_IMPORT_IN_TURN,
        'import { copySourceOfTruth, displaySqlForTurn, highlightSql } from "./sql-display.mjs";',
        turn_src, "turn.js -> sql-display.js",
    )
    assumptions_src = _subn_or_fail(
        _ICONS_IMPORT_IN_ASSUMPTIONS,
        'import { icon } from "./icons.mjs";',
        assumptions_src, "assumptions.js -> icons.js",
    )
    turn_src = _subn_or_fail(
        _PIPELINE_IMPORT,
        'import { renderPipeline, renderStageStrip } from "./pipeline.mjs";',
        turn_src, "turn.js -> pipeline.js",
    )
    turn_src = _subn_or_fail(
        _ASSUMPTIONS_IMPORT_IN_TURN,
        'import { renderBasis, renderAssumptions, renderClarifications } from "./assumptions.mjs";',
        turn_src, "turn.js -> assumptions.js",
    )
    turn_src = _subn_or_fail(_TABLE_IMPORT_IN_TURN, 'import { renderResult, renderWarnings } from "./table.mjs";', turn_src, "turn.js -> table.js")
    turn_src = _subn_or_fail(
        _LLM_STATUS_IMPORT,
        'import { renderLlmStatus, answerWasTruncated, renderTruncationQualifier } from "./llm-status.mjs";',
        turn_src, "turn.js -> llm-status.js",
    )
    turn_src = _subn_or_fail(
        _FEEDBACK_IMPORT_IN_TURN,
        'import { renderFeedbackControl } from "./feedback.mjs";',
        turn_src, "turn.js -> feedback.js",
    )
    table_src = _subn_or_fail(_CHART_IMPORT_IN_TABLE, 'import { renderChartAndTable } from "./chart.mjs";', table_src, "table.js -> chart.js")
    table_src = _subn_or_fail(_EXPORT_IMPORT_IN_TABLE, 'import { downloadResultAsCsv } from "./export.mjs";', table_src, "table.js -> export.js")
    table_src = _subn_or_fail(_ASSUMPTIONS_IMPORT_IN_TABLE, 'import { SOURCE_LABELS } from "./assumptions.mjs";', table_src, "table.js -> assumptions.js")
    chart_src = _subn_or_fail(_TABLE_IMPORT_IN_CHART, 'import { renderTableOnly, renderExportRow, fmtCell } from "./table.mjs";', chart_src, "chart.js -> table.js")

    turn_src = _rewrite_num_import(turn_src)

    assumptions_src = _rewrite_num_import(assumptions_src)

    table_src = _rewrite_num_import(table_src)

    chart_src = _rewrite_num_import(chart_src)

    export_src = _rewrite_num_import(export_src)

    llm_status_src = _rewrite_num_import(llm_status_src)

    feedback_src = _rewrite_num_import(feedback_src)

    sql_display_src = _rewrite_num_import(sql_display_src)

    (tmp_path / "turn.mjs").write_text(turn_src, encoding="utf-8")
    (tmp_path / "pipeline.mjs").write_text(pipeline_src, encoding="utf-8")
    (tmp_path / "assumptions.mjs").write_text(assumptions_src, encoding="utf-8")
    (tmp_path / "table.mjs").write_text(table_src, encoding="utf-8")
    (tmp_path / "chart.mjs").write_text(chart_src, encoding="utf-8")
    (tmp_path / "export.mjs").write_text(export_src, encoding="utf-8")
    (tmp_path / "llm-status.mjs").write_text(llm_status_src, encoding="utf-8")
    (tmp_path / "feedback.mjs").write_text(feedback_src, encoding="utf-8")
    (tmp_path / "sql-display.mjs").write_text(sql_display_src, encoding="utf-8")
    (tmp_path / "icons.mjs").write_text(icons_src, encoding="utf-8")
    # num.js is shared by every renderer (see web/js/num.js): one place
    # that decides how a number looks, after seven modules each decided
    # separately. Staging it is what lets those imports resolve.
    (tmp_path / "num.mjs").write_text(_NUM_JS.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path / "turn.mjs"


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_sql_highlighting_is_presentation_only_and_copy_stays_exact() -> None:
    for p in (
        _TURN_JS, _SQL_DISPLAY_JS, _PIPELINE_JS, _ASSUMPTIONS_JS, _TABLE_JS, _CHART_JS, _EXPORT_JS,
        _LLM_STATUS_JS, _FEEDBACK_JS,
    ):
        assert p.exists(), f"expected {p} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        turn_mjs = _prepare_copies(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(turn_mjs)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    assert result.returncode == 0, (
        f"SQL highlighting check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
