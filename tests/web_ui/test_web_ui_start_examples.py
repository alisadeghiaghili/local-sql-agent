# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The analyst page's start questions (``GET /v2/examples``).

An empty live conversation now offers the deployment's ``examples.yaml``
questions as one-click starts, under the existing hint. The server decides
what is offered (``tests/test_v2_examples_route.py``); these checks pin the
page's side of it:

* only in live mode, only with a key, and fetched once per page load (a
  failed fetch is retried next time, not remembered);
* hidden until the questions arrive, so an older server, an empty list or an
  error leaves the empty state exactly as it was;
* each question is set with ``textContent`` (it is configuration text, but
  never markup), and a click asks it through the same ``ask()`` as the box;
* both languages have the label.
"""

from __future__ import annotations

from pathlib import Path

_WEB = Path(__file__).resolve().parent.parent.parent / "web"
_MAIN_JS = _WEB / "js" / "main.js"
_API_JS = _WEB / "js" / "api.js"
_I18N_JS = _WEB / "js" / "i18n.js"
_STYLE = _WEB / "styles" / "style.css"


def _block(src: str, start: str, end: str) -> str:
    return src[src.index(start):src.index(end, src.index(start))]


def test_the_empty_state_asks_for_start_questions_only_in_live_mode_with_a_key() -> None:
    src = _MAIN_JS.read_text(encoding="utf-8")
    empty = _block(src, "function renderTranscriptEmptyState()", "function clearTranscriptEmptyState()")
    assert 'state.mode === "simulated"' in empty, "the demo's story chips are unchanged"
    assert 'state.mode === "live" && hasApiKey()' in empty
    assert "wrap.hidden = true;" in empty, "hidden until the questions arrive"
    assert "renderStartExamples(wrap)" in empty


def test_start_questions_are_fetched_once_and_rendered_as_text() -> None:
    src = _MAIN_JS.read_text(encoding="utf-8")
    body = _block(src, "async function renderStartExamples(", "function clearTranscriptEmptyState()")
    assert "api.listExamples(6)" in body
    assert "startExamplesPromise = null; return [];" in body, "a failed fetch is retried next time"
    assert "if (!wrap.isConnected || !examples.length) return;" in body
    assert "btn.textContent = question;" in body
    assert "innerHTML" not in body
    assert '$("question").value = question;' in body and "ask();" in body
    assert "wrap.hidden = false;" in body


def test_the_api_client_and_the_labels() -> None:
    api = _API_JS.read_text(encoding="utf-8")
    assert "async listExamples(limit = 6)" in api
    assert "/v2/examples?limit=" in api
    i18n = _I18N_JS.read_text(encoding="utf-8")
    assert i18n.count("startExamples:") == 2, "a label in both languages"


def test_hidden_start_questions_take_no_space() -> None:
    css = _STYLE.read_text(encoding="utf-8")
    assert ".start-examples[hidden] { display: none; }" in css
