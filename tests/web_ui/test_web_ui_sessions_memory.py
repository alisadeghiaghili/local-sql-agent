# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test for multi-session and memory support in the web UI:
``web/js/render/sessions.js`` (the conversation rail), ``web/js/render/
memory.js`` (the memory panel and its client-side value validation),
``web/js/state.js``'s session-id resolution/persistence, ``web/js/api.js``'s
new v2 call sites, and ``web/js/render/table.js``'s `rows_omitted` handling.

Before this change none of this existed: the UI showed exactly one
conversation with no way to list, rename, delete, or return to another one,
and there was no way for the analyst to pin a value to cross-conversation
memory at all. This test drives the REAL source under Node (see
``run_sessions_memory.mjs`` in this directory for the full scenario list and
the minimal DOM shim it uses -- web/ ships no package.json / node_modules by
design, so this brings no dependency on jsdom or any other package, the same
spirit as ``run_auth_boundary.mjs``'s mocked fetch/localStorage and
``run_result_shapes.mjs``'s DOM shim) and asserts at the boundary that would
actually break:

* every new v2 call this feature adds (``GET /v2/sessions``,
  ``PATCH /v2/sessions/{sid}``, ``GET /v2/memory``, ``PUT /v2/memory/{key}``,
  ``DELETE /v2/memory/{key}``, ``DELETE /v2/memory``) attaches
  ``Authorization: Bearer <key>`` -- the exact class of bug PR #44 fixed for
  the original v2 routes, now extended to this feature's routes;
* ``rows_omitted: true`` never renders as an empty table or "0 rows", and
  surfaces the row count that was actually returned;
* a stored session id absent from the index falls back to the newest
  session without throwing, and an empty index resolves to ``null``
  (first-run state) rather than an error;
* ``localStorage`` throwing on every read does not break start-up;
* a memory value containing a newline is refused before the request is
  ever made -- both the pure validator and the wired save-button path;
* an ``applicable: false`` memory entry renders visibly inactive -- never
  hidden, never indistinguishable from an active entry;
* the conversation rail renders newest-``last_active_at``-first regardless
  of the order the index array itself was given in.
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

_API_JS = _WEB_JS / "api.js"
_APIKEY_JS = _WEB_JS / "apikey.js"
_STATE_JS = _WEB_JS / "state.js"
_TABLE_JS = _WEB_JS / "render" / "table.js"
_CHART_JS = _WEB_JS / "render" / "chart.js"
_ASSUMPTIONS_JS = _WEB_JS / "render" / "assumptions.js"
_EXPORT_JS = _WEB_JS / "export.js"
_SESSIONS_JS = _WEB_JS / "render" / "sessions.js"
_MEMORY_JS = _WEB_JS / "render" / "memory.js"

_HARNESS = Path(__file__).resolve().parent / "run_sessions_memory.mjs"

_NODE = shutil.which("node")

# The only changes made to the real source before handing it to Node: every
# internal import specifier gets rewritten to its sibling `.mjs` copy
# (Node's ESM loader needs an `.mjs` extension or a controlling package.json
# to parse `export`/`import` syntax, and web/ deliberately ships neither --
# see web/README.md's "no build step, no package.json"). Anything else
# reaching this test is byte-identical to the real source.
_APIKEY_IMPORT = re.compile(r'^import \{ getApiKey \} from "\./apikey\.js";$', re.MULTILINE)
_CHART_IMPORT = re.compile(r'^import \{ renderChartAndTable \} from "\./chart\.js";$', re.MULTILINE)
_EXPORT_IMPORT = re.compile(r'^import \{ downloadResultAsCsv \} from "\.\./export\.js";$', re.MULTILINE)
_ASSUMPTIONS_IMPORT = re.compile(r'^import \{ SOURCE_LABELS \} from "\./assumptions\.js";$', re.MULTILINE)
_TABLE_IMPORT_IN_CHART = re.compile(
    r'^import \{ renderTableOnly, renderExportRow, fmtCell \} from "\./table\.js";$', re.MULTILINE,
)


def _subn_or_fail(pattern: re.Pattern[str], replacement: str, text: str, what: str) -> str:
    fixed, n = pattern.subn(replacement, text)
    assert n == 1, (
        f"web/js source no longer matches the import this test expects ({what}) -- "
        "update the regex in test_web_ui_sessions_memory.py to match the real source "
        "instead of silently testing stale code."
    )
    return fixed


def _prepare_copies(tmp_path: Path) -> dict[str, Path]:
    """Copies every module this suite drives into *tmp_path* as ESM
    (``.mjs``), import paths fixed up. Returns a dict of the paths the
    harness needs (see run_sessions_memory.mjs's argv contract).
    """
    api_src = _API_JS.read_text(encoding="utf-8")
    apikey_src = _APIKEY_JS.read_text(encoding="utf-8")
    state_src = _STATE_JS.read_text(encoding="utf-8")
    table_src = _TABLE_JS.read_text(encoding="utf-8")
    chart_src = _CHART_JS.read_text(encoding="utf-8")
    assumptions_src = _ASSUMPTIONS_JS.read_text(encoding="utf-8")
    export_src = _EXPORT_JS.read_text(encoding="utf-8")
    sessions_src = _SESSIONS_JS.read_text(encoding="utf-8")
    memory_src = _MEMORY_JS.read_text(encoding="utf-8")

    api_src = _subn_or_fail(_APIKEY_IMPORT, 'import { getApiKey } from "./apikey.mjs";', api_src, "api.js -> apikey.js")
    table_src = _subn_or_fail(_CHART_IMPORT, 'import { renderChartAndTable } from "./chart.mjs";', table_src, "table.js -> chart.js")
    table_src = _subn_or_fail(_EXPORT_IMPORT, 'import { downloadResultAsCsv } from "./export.mjs";', table_src, "table.js -> export.js")
    table_src = _subn_or_fail(_ASSUMPTIONS_IMPORT, 'import { SOURCE_LABELS } from "./assumptions.mjs";', table_src, "table.js -> assumptions.js")
    chart_src = _subn_or_fail(_TABLE_IMPORT_IN_CHART, 'import { renderTableOnly, renderExportRow, fmtCell } from "./table.mjs";', chart_src, "chart.js -> table.js")

    # state.js, sessions.js and memory.js have no internal import
    # specifiers to rewrite -- copied byte-for-byte (extension aside).

    paths = {
        "api": tmp_path / "api.mjs",
        "apikey": tmp_path / "apikey.mjs",
        "state": tmp_path / "state.mjs",
        "table": tmp_path / "table.mjs",
        "chart": tmp_path / "chart.mjs",
        "assumptions": tmp_path / "assumptions.mjs",
        "export": tmp_path / "export.mjs",
        "sessions": tmp_path / "sessions.mjs",
        "memory": tmp_path / "memory.mjs",
    }
    paths["api"].write_text(api_src, encoding="utf-8")
    paths["apikey"].write_text(apikey_src, encoding="utf-8")
    paths["state"].write_text(state_src, encoding="utf-8")
    paths["table"].write_text(table_src, encoding="utf-8")
    paths["chart"].write_text(chart_src, encoding="utf-8")
    paths["assumptions"].write_text(assumptions_src, encoding="utf-8")
    paths["export"].write_text(export_src, encoding="utf-8")
    paths["sessions"].write_text(sessions_src, encoding="utf-8")
    paths["memory"].write_text(memory_src, encoding="utf-8")
    return paths


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_sessions_and_memory_boundaries() -> None:
    for p in (_API_JS, _APIKEY_JS, _STATE_JS, _TABLE_JS, _CHART_JS, _ASSUMPTIONS_JS, _EXPORT_JS, _SESSIONS_JS, _MEMORY_JS):
        assert p.exists(), f"expected {p} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        paths = _prepare_copies(Path(tmp))
        result = subprocess.run(
            [
                _NODE, str(_HARNESS),
                str(paths["api"]), str(paths["table"]), str(paths["state"]),
                str(paths["sessions"]), str(paths["memory"]),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    assert result.returncode == 0, (
        f"sessions/memory boundary check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
