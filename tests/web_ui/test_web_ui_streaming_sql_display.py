# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The streamed ``sql`` event's ``sql_display`` reaches the turn being drawn.

``api/v2_routes.py`` sends ``{sql, sql_display, guard}`` in the ``sql`` SSE
event. ``web/js/main.js``'s ``askLive`` used to keep only ``sql`` and
``guard``, so until the ``done`` event arrived the card showed the SQL
through the client formatter, a different layout from the one ``done`` then
swapped in.

This extracts the real ``askLive`` from ``web/js/main.js`` (same technique as
``test_web_ui_onclarify_live_mode.py``) and runs it under Node against spies
for the card and the API client (see ``run_streaming_sql_display.mjs``),
together with the real ``web/js/sql-display.js``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.web_ui import NODE_TIMEOUT_SECONDS
from tests.web_ui.test_web_ui_onclarify_live_mode import _extract_function

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MAIN_JS = _REPO_ROOT / "web" / "js" / "main.js"
_SQL_DISPLAY_JS = _REPO_ROOT / "web" / "js" / "sql-display.js"
_HARNESS = Path(__file__).resolve().parent / "run_streaming_sql_display.mjs"

_NODE = shutil.which("node")

_HARNESS_MODULE_TEMPLATE = """\
// Written at test time by tests/web_ui/test_web_ui_streaming_sql_display.py -- do
// not edit by hand. Embeds the REAL, unmodified `askLive` extracted verbatim
// from web/js/main.js, wired to spies for everything it touches.

export const renders = [];
let events = [];
export function setEvents(list) {{ events = list; }}

class V2NotSupportedError extends Error {{}}
class UnauthorizedError extends Error {{}}
class RateLimitError extends Error {{}}
class ApiError extends Error {{}}

const state = {{ interpret: false, sessionId: "s_test" }};

function hasApiKey() {{ return true; }}
function promptForApiKey() {{}}
async function ensureLiveSession() {{ return "s_test"; }}
function emptyTurn(question, sessionId) {{
  return {{ turn_id: "live_1", session_id: sessionId, index: 0, question, sql: null, guard: null }};
}}
function clearTranscriptEmptyState() {{}}
function turnCtx() {{ return {{}}; }}
function $() {{ return {{ appendChild() {{}} }}; }}
const document = {{ getElementById: () => null }};
function scrollIntoViewMaybeSmooth() {{}}
function scrollSettledResultAboveComposer() {{}}
function bumpActiveSessionMeta() {{}}
function addTurn() {{}}
function handleLiveError(err) {{ throw err; }}

// Records what each card is built from, as a snapshot: the turn object is
// mutated in place between events.
function createTurnCard(turn) {{
  renders.push(JSON.parse(JSON.stringify(turn)));
  return {{
    el: {{ replaceWith() {{}} }},
    pipeline: {{ setStage() {{}} }},
    revealEarly() {{}}, revealLate() {{}},
    result: null,
  }};
}}

const api = {{
  askTurnStreaming: async (sessionId, question, onEvent) => {{
    for (const [event, data] of events) onEvent(event, data);
  }},
}};

// ---- extracted verbatim from web/js/main.js ----------------------------
{ask_live_src}
// ---- end extracted source -----------------------------------------------

export {{ askLive }};
"""


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_streamed_sql_event_display_form_reaches_the_first_paint() -> None:
    src = _MAIN_JS.read_text(encoding="utf-8")
    ask_live_src = _extract_function(src, "async function askLive(q) {", "askLive")
    assert 'case "sql":' in ask_live_src, "extracted askLive no longer handles the sql event"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        harness_module = tmp_path / "streaming_harness.mjs"
        harness_module.write_text(
            _HARNESS_MODULE_TEMPLATE.format(ask_live_src=ask_live_src), encoding="utf-8",
        )
        sql_display = tmp_path / "sql-display.mjs"
        shutil.copyfile(_SQL_DISPLAY_JS, sql_display)

        result = subprocess.run(
            [_NODE, str(_HARNESS), str(harness_module), str(sql_display)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        f"streaming sql_display check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
