# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regression test: resolving a clarification chip ("which hall did you
mean?") in LIVE mode must really re-run the turn, not just relabel a local
chip.

``web/js/main.js``'s ``turnCtx().onEditAssumption`` has always had a
live-mode branch: ``if (state.mode === "live") { patchLiveAssumption(...);
return; }`` before falling through to the local-simulation code. Its
sibling ``onClarify`` -- used for the "which hall did you mean?" chip
rendered by ``web/js/render/assumptions.js``'s ``renderClarifications`` --
had no such branch: in live mode it still only mutated the in-memory chip
and showed the Persian "local simulation only" notice, so an analyst's
answer to a clarification question never reached the server and the turn's
SQL/result never changed to reflect it. ``PATCH /v2/sessions/{sid}/turns/
{tid}/assumptions`` (api/v2_routes.py) already accepts ``{field, value}``
and ``TurnEngine.ask`` already applies it via ``assumption_overrides`` --
a resolved clarification is exactly that shape -- so only the front-end
wiring was missing.

This test extracts the REAL, unmodified ``turnCtx`` and
``patchLiveAssumption`` function bodies out of ``web/js/main.js`` (a
balanced-brace scan, not a hand-copy -- see ``_extract_function`` below),
wires them to spies standing in for ``state``/``findTurn``/``rerenderTurn``/
``showNotice``/``api``/``handleLiveError``, and drives them under Node (see
``run_onclarify_live_mode.mjs``) to assert:

* in SIMULATED mode, ``onClarify`` behaves exactly as before: it mutates
  the chip locally, never touches ``api.patchAssumptions``, and shows the
  "(شبیه‌سازی محلی)" notice;
* in LIVE mode, ``onClarify`` calls the SAME ``patchLiveAssumption`` code
  path ``onEditAssumption`` already used -- ``api.patchAssumptions`` is
  called with ``(state.sessionId, turnId, [{field, value: option}])``,
  the turn returned by the server replaces the local one, the turn card is
  re-rendered from that server response, and the local-simulation notice is
  never shown;
* a failed live PATCH routes through the same ``handleLiveError`` as
  ``onEditAssumption``'s live path, instead of silently doing nothing.

Because the harness re-extracts ``turnCtx``/``patchLiveAssumption`` from
the real source on every run, reverting the fix in ``web/js/main.js``
(dropping the live-mode branch back out of ``onClarify``) makes this test
fail without touching this file -- exactly the mutation-check this defect
needs.
"""

from __future__ import annotations
from tests.web_ui import NODE_TIMEOUT_SECONDS

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MAIN_JS = _REPO_ROOT / "web" / "js" / "main.js"
_HARNESS = Path(__file__).resolve().parent / "run_onclarify_live_mode.mjs"

_NODE = shutil.which("node")

_TURN_CTX_SIGNATURE = "function turnCtx() {"
_PATCH_LIVE_ASSUMPTION_SIGNATURE = "async function patchLiveAssumption(turnId, field, value) {"


def _extract_function(src: str, signature: str, what: str) -> str:
    """Return the verbatim source of the function starting at *signature*,
    from the signature itself through its matching closing brace -- found
    with a small JS-aware brace counter (tracks line/block comments and
    single-quoted, double-quoted and backtick-quoted strings, so a brace
    inside a string or a comment never miscounts), not a regex that would
    silently stop at the first closing brace.
    """
    start = src.find(signature)
    assert start != -1, (
        f"web/js/main.js no longer contains `{signature}` ({what}) -- "
        "update this test's signature constant to match the real source "
        "instead of silently extracting nothing."
    )
    brace_start = src.index("{", start)
    depth = 0
    i = brace_start
    n = len(src)
    mode = "code"  # code | line_comment | block_comment | sq | dq | bt
    while i < n:
        c = src[i]
        if mode == "code":
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return src[start : i + 1]
            elif c == "/" and i + 1 < n and src[i + 1] == "/":
                mode = "line_comment"
                i += 1
            elif c == "/" and i + 1 < n and src[i + 1] == "*":
                mode = "block_comment"
                i += 1
            elif c == "'":
                mode = "sq"
            elif c == '"':
                mode = "dq"
            elif c == "`":
                mode = "bt"
        elif mode == "line_comment":
            if c == "\n":
                mode = "code"
        elif mode == "block_comment":
            if c == "*" and i + 1 < n and src[i + 1] == "/":
                mode = "code"
                i += 1
        elif mode == "sq":
            if c == "\\":
                i += 1
            elif c == "'":
                mode = "code"
        elif mode == "dq":
            if c == "\\":
                i += 1
            elif c == '"':
                mode = "code"
        elif mode == "bt":
            if c == "\\":
                i += 1
            elif c == "`":
                mode = "code"
        i += 1
    raise AssertionError(f"unbalanced braces while extracting {what} from web/js/main.js")


_HARNESS_MODULE_TEMPLATE = """\
// AUTO-GENERATED by tests/web_ui/test_web_ui_onclarify_live_mode.py -- do
// not edit by hand. Embeds the REAL, unmodified `turnCtx` and
// `patchLiveAssumption` source extracted verbatim from web/js/main.js
// (see that test file's `_extract_function`), wired to spies standing in
// for state/findTurn/rerenderTurn/showNotice/api/handleLiveError so the
// real onClarify/onEditAssumption logic runs under Node without the rest
// of main.js's DOM-heavy boot machinery.

export const state = {{ mode: "simulated", sessionId: null, turns: [] }};

export const calls = {{
  rerenderTurn: [],
  showNotice: [],
  patchAssumptions: [],
  handleLiveError: [],
}};

export function resetCalls() {{
  calls.rerenderTurn.length = 0;
  calls.showNotice.length = 0;
  calls.patchAssumptions.length = 0;
  calls.handleLiveError.length = 0;
}}

let patchAssumptionsImpl = async () => {{
  throw new Error("patchAssumptionsImpl not configured for this scenario");
}};
export function setPatchAssumptionsImpl(fn) {{
  patchAssumptionsImpl = fn;
}}

const api = {{
  patchAssumptions: async (sessionId, turnId, patches) => {{
    calls.patchAssumptions.push({{ sessionId, turnId, patches }});
    return await patchAssumptionsImpl(sessionId, turnId, patches);
  }},
}};

export function findTurn(turnId) {{
  return state.turns.find((t) => t.turn_id === turnId) || null;
}}

export function rerenderTurn(turn) {{
  calls.rerenderTurn.push(turn);
}}

export function showNotice(kind, message) {{
  calls.showNotice.push({{ kind, message }});
}}

export function handleLiveError(err) {{
  calls.handleLiveError.push(err);
}}

// ---- extracted verbatim from web/js/main.js ----------------------------
{patch_live_assumption_src}

{turn_ctx_src}
// ---- end extracted source -----------------------------------------------

export {{ turnCtx, patchLiveAssumption }};
"""


def _write_harness_module(tmp_path: Path) -> Path:
    src = _MAIN_JS.read_text(encoding="utf-8")
    patch_live_assumption_src = _extract_function(
        src, _PATCH_LIVE_ASSUMPTION_SIGNATURE, "patchLiveAssumption"
    )
    turn_ctx_src = _extract_function(src, _TURN_CTX_SIGNATURE, "turnCtx")

    assert "onClarify" in turn_ctx_src, "extracted turnCtx no longer defines onClarify"
    assert "onEditAssumption" in turn_ctx_src, "extracted turnCtx no longer defines onEditAssumption"

    module_src = _HARNESS_MODULE_TEMPLATE.format(
        patch_live_assumption_src=patch_live_assumption_src,
        turn_ctx_src=turn_ctx_src,
    )
    out = tmp_path / "onclarify_harness.mjs"
    out.write_text(module_src, encoding="utf-8")
    return out


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH -- cannot execute web/js/*.js under test")
def test_onclarify_sends_live_patch_like_onEditAssumption() -> None:
    assert _MAIN_JS.exists(), f"expected {_MAIN_JS} to exist"

    with tempfile.TemporaryDirectory() as tmp:
        module_path = _write_harness_module(Path(tmp))
        result = subprocess.run(
            [_NODE, str(_HARNESS), str(module_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=NODE_TIMEOUT_SECONDS,
        )

    assert result.returncode == 0, (
        f"onClarify live-mode check failed.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "ALL_SCENARIOS_PASSED" in result.stdout, (
        f"harness did not report completion (partial run?).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
