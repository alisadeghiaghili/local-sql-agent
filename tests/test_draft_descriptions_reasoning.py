# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``draft_descriptions.py`` against a reasoning model that spends its budget thinking (issue #251).

The report: ``Qwen/Qwen3.8-27B-FP8`` behind vLLM, almost every table logged
"appears to carry reasoning-channel text rather than a final answer" twice and
then ``FAILED (ValueError: the answer holds no description that can be used)``.
A table of 17 columns succeeded; the small ones failed. Each call is sized by
:func:`~schema_data.describe_prompt.max_tokens_for` (300 tokens plus 60 per
column, never less than ``LLM_NUM_PREDICT``), so a small table got about 512 to
600 tokens, the model spent them on its reasoning channel and was cut off
(``finish_reason: "length"``) before it wrote the JSON. The wizard then sent
that empty answer back with "Return ONLY the corrected JSON"; the model,
finding no text to correct, answered ``{}``, which holds no description.

Every request here goes through the real :class:`~llm.providers.OpenAIBackend`
with ``requests.post`` replaced, so what is pinned is what goes on the wire.

1. **Pins of today's behaviour.** ``WizardLLM.generate`` without the new
   argument, which is how the setup wizard calls it; every answer that was not
   cut off while reasoning, with the argument; and the ``main()`` runs where no
   retry has room. Those that do not use the new argument passed before the
   fix; all must keep passing.
2. **The reported shapes.** :class:`Server` answers like the reported one:
   it spends any budget below :data:`_ROOM_NEEDED` on reasoning and answers a
   repair prompt with ``{}``. Through ``main()`` this failed before the fix
   exactly as reported (``FAILED ... holds no description``).
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import config as cfg
import setup_project
from llm.wizard_llm import WizardLLM
from scripts.draft_descriptions import EXIT_FAILED_TABLES, EXIT_OK, OUTPUT_FILENAME, main
from tests.test_draft_descriptions import SCHEMA

_MODEL = "Qwen/Qwen3.8-27B-FP8"
_REPAIR_HEAD = "The text below must be a valid JSON object or array but failed to parse."
_PROMPT = "PROMPT"
_CUT = "length"
_OK_TEXT = '{"x": 1}'
_NOT_JSON = "still not json"
_PROSE = "Here is the answer."
_TODAYS_ERROR = r"\[WizardLLM\] Could not extract valid JSON after retry"
_NO_DESCRIPTION = "holds no description that can be used"
_ROOM = 8192
_BUDGET = 512
_TRADE = ["--tables", "sales.Trade"]
_MAX_TOKENS = "--max-tokens"

#: The reasoning text the report quotes, as the server's reasoning parser returns it.
_THINKING = (
    "We need answer user's request. Need produce JSON only, Persian descriptions. "
    "Need analyze table Trade schema sales data source default."
)
_THINKING_ON_REPAIR = (
    "We need answer user's request. They say: \"The text below must be a valid JSON "
    "object or array but failed to parse.\" But there is no text below."
)
_ANSWER = {"table": "One row per trade made with a customer.", "columns": {"Status": "Lifecycle state"}}


def _body(
    content: str | None,
    finish: str = "stop",
    *,
    reasoning: str | None = None,
    reasoning_tokens: int | None = None,
) -> dict[str, Any]:
    """One ``/chat/completions`` response body of the given shape."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    body: dict[str, Any] = {"choices": [{"message": message, "finish_reason": finish}]}
    if reasoning_tokens is not None:
        body["usage"] = {
            "completion_tokens": reasoning_tokens,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        }
    return body


#: The first answer of the report: the budget was spent on the reasoning channel.
STARVED_NULL = _body(None, _CUT, reasoning=_THINKING)
#: The same, with the JSON started but cut off.
STARVED_PARTIAL = _body('{"table": "هر ردیف یک نوع', _CUT, reasoning=_THINKING)
#: No reasoning parser on the server: the thinking arrives in ``content`` itself.
STARVED_INLINE = _body("<think>" + _THINKING, _CUT)
#: The server counts the reasoning tokens but returns no reasoning text.
STARVED_COUNTED = _body("", _CUT, reasoning_tokens=600)
#: The model's answer to a repair prompt that had nothing to repair, as in the report.
REPAIR_OF_NOTHING = _body("{}", reasoning=_THINKING_ON_REPAIR)
#: A reasoning model's complete answer.
ANSWERED_AFTER_THINKING = _body(json.dumps(_ANSWER), reasoning=_THINKING)
#: A plain, complete answer.
OK = _body(_OK_TEXT)


class Wire:
    """Stands for ``requests.post``: answers with the next scripted body and records every request."""

    def __init__(self, *bodies: dict[str, Any]) -> None:
        self.bodies = list(bodies)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, *_args: object, json: dict[str, Any], **_kwargs: object) -> MagicMock:
        self.requests.append(copy.deepcopy(json))
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = self.bodies.pop(0)
        response.raise_for_status = MagicMock()
        return response

    @property
    def prompts(self) -> list[str]:
        """The prompt of every request, in order."""
        return [r["messages"][0]["content"] for r in self.requests]

    @property
    def max_tokens(self) -> list[int]:
        """The ``max_tokens`` of every request, in order."""
        return [r["max_tokens"] for r in self.requests]


#: The completion budget the reasoning of :class:`Server` needs before it answers.
_ROOM_NEEDED = 2000


class Server(Wire):
    """Behaves like the reported endpoint instead of following a script.

    A repair prompt is answered with ``{}`` (there is nothing to repair). Any
    other prompt is answered completely when ``max_tokens`` leaves
    :data:`_ROOM_NEEDED` for the reasoning, and is otherwise cut off while
    still reasoning.
    """

    def __call__(self, *_args: object, json: dict[str, Any], **_kwargs: object) -> MagicMock:
        prompt = json["messages"][0]["content"]
        if prompt.startswith(_REPAIR_HEAD):
            self.bodies.append(REPAIR_OF_NOTHING)
        elif json["max_tokens"] >= _ROOM_NEEDED:
            self.bodies.append(ANSWERED_AFTER_THINKING)
        else:
            self.bodies.append(STARVED_NULL)
        return super().__call__(json=json)


@pytest.fixture()
def wizard(monkeypatch: pytest.MonkeyPatch) -> WizardLLM:
    """The wizard's model on a local OpenAI-compatible endpoint."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return WizardLLM(provider="openai", model=_MODEL, base_url="http://localhost:8080/v1")


def _run(wizard: WizardLLM, wire: Wire, **kwargs: int) -> object:
    with patch("requests.post", side_effect=wire), cfg.override_settings(llm_num_predict=_BUDGET):
        return wizard.generate(_PROMPT, expect_json=True, **kwargs)


def _wizard_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "llm.wizard_llm" and r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# 1. Pins: today's behaviour
# ---------------------------------------------------------------------------


class TestTodaysContractWithoutTheNewArgument:
    """How the setup wizard calls ``generate``: unchanged, whatever the answer."""

    def test_a_parsable_first_answer_is_one_call(self, wizard: WizardLLM) -> None:
        wire = Wire(OK)
        assert _run(wizard, wire) == {"x": 1}
        assert wire.prompts == [_PROMPT]

    def test_an_unparsable_answer_gets_one_repair_prompt_with_its_text(self, wizard: WizardLLM) -> None:
        wire = Wire(_body("not json at all"), _body('{"x": 2}'))
        assert _run(wizard, wire) == {"x": 2}
        assert wire.prompts[1].startswith(_REPAIR_HEAD)
        assert wire.prompts[1].endswith("not json at all")
        assert wire.max_tokens == [_BUDGET, _BUDGET]

    def test_a_reasoning_model_cut_off_still_gets_the_repair_prompt(self, wizard: WizardLLM) -> None:
        # The issue's sequence, kept as it is for the callers that do not opt in.
        wire = Wire(STARVED_NULL, REPAIR_OF_NOTHING)
        assert _run(wizard, wire) == {}
        assert [p.startswith(_REPAIR_HEAD) for p in wire.prompts] == [False, True]
        assert wire.max_tokens == [_BUDGET, _BUDGET]


#: Non-reasoning answers, and reasoning answers that were not cut off: with the
#: new argument, the requests must be exactly the ones sent without it.
_NOT_STARVED = {
    "parsable": [OK],
    "prose, then repaired": [_body("Sure! Here it is."), OK],
    "cut off, no reasoning": [_body('{"table": "One row', _CUT), OK],
    "empty, no reasoning": [_body("", _CUT), OK],
    "null content, stop": [_body(None), OK],
    "reasoning, answered": [ANSWERED_AFTER_THINKING],
    "reasoning, cut off after a complete answer": [_body(_OK_TEXT, _CUT, reasoning=_THINKING)],
    "reasoning, prose, not cut off": [_body("I cannot.", reasoning=_THINKING), OK],
    "reasoning counted as zero, cut off": [_body("", _CUT, reasoning_tokens=0), OK],
}


class TestNothingChangesForAnAnswerThatWasNotStarved:
    """With the new argument, every answer that was not cut off while reasoning sends today's requests."""

    @pytest.mark.parametrize("bodies", _NOT_STARVED.values(), ids=_NOT_STARVED.keys())
    def test_the_requests_and_the_result_are_the_same_with_and_without_the_argument(
        self, wizard: WizardLLM, bodies: list[dict[str, Any]], caplog: pytest.LogCaptureFixture,
    ) -> None:
        before, after = Wire(*copy.deepcopy(bodies)), Wire(*copy.deepcopy(bodies))
        assert _run(wizard, before) == _run(wizard, after, reasoning_max_tokens=_ROOM)
        assert after.requests == before.requests
        assert _wizard_warnings(caplog) == []

    @pytest.mark.parametrize("kwargs", [{}, {"reasoning_max_tokens": _ROOM}], ids=["without", "with"])
    def test_two_unparsable_answers_raise_todays_error(self, wizard: WizardLLM, kwargs: dict[str, int]) -> None:
        wire = Wire(_body(_NOT_JSON), _body(_NOT_JSON))
        with pytest.raises(ValueError, match=_TODAYS_ERROR):
            _run(wizard, wire, **kwargs)
        assert len(wire.requests) == 2

    def test_no_more_room_than_the_first_call_had_means_todays_path(self, wizard: WizardLLM) -> None:
        before, after = Wire(STARVED_NULL, REPAIR_OF_NOTHING), Wire(STARVED_NULL, REPAIR_OF_NOTHING)
        assert _run(wizard, before) == _run(wizard, after, reasoning_max_tokens=_BUDGET)
        assert after.requests == before.requests

    def test_raw_text_is_one_call_and_the_same_request(self, wizard: WizardLLM) -> None:
        before, after = Wire(STARVED_NULL), Wire(STARVED_NULL)
        with patch("requests.post", side_effect=before), cfg.override_settings(llm_num_predict=_BUDGET):
            raw_before = wizard.generate(_PROMPT, expect_json=False)
        with patch("requests.post", side_effect=after), cfg.override_settings(llm_num_predict=_BUDGET):
            raw_after = wizard.generate(_PROMPT, expect_json=False, reasoning_max_tokens=_ROOM)
        assert raw_after == raw_before == ""
        assert after.requests == before.requests

    def test_the_mock_provider_answers_its_stub(self) -> None:
        llm = WizardLLM(provider="mock", model="mock")
        assert llm.generate("anything", reasoning_max_tokens=_ROOM) == llm.generate("anything")


# ---------------------------------------------------------------------------
# 2. The reported shapes
# ---------------------------------------------------------------------------

_STARVED = {
    "content null, reasoning parser": STARVED_NULL,
    "JSON cut off, reasoning parser": STARVED_PARTIAL,
    "no reasoning parser, <think> in content": STARVED_INLINE,
    "reasoning tokens counted, no text": STARVED_COUNTED,
}


class TestAStarvedAnswerIsAskedAgainWithRoom:
    """An answer cut off while the model was reasoning is asked again with the room, once."""

    @pytest.mark.parametrize("starved", _STARVED.values(), ids=_STARVED.keys())
    def test_the_original_prompt_is_sent_again_with_the_room(
        self, wizard: WizardLLM, starved: dict[str, Any], caplog: pytest.LogCaptureFixture,
    ) -> None:
        wire = Wire(starved, ANSWERED_AFTER_THINKING)
        assert _run(wizard, wire, reasoning_max_tokens=_ROOM) == _ANSWER
        # Never the repair prompt.
        assert wire.prompts == [_PROMPT, _PROMPT]
        assert wire.max_tokens == [_BUDGET, _ROOM]
        assert _wizard_warnings(caplog) == []

    def test_the_first_request_is_the_one_sent_today(self, wizard: WizardLLM) -> None:
        today, now = Wire(STARVED_NULL, REPAIR_OF_NOTHING), Wire(STARVED_NULL, ANSWERED_AFTER_THINKING)
        _run(wizard, today)
        _run(wizard, now, reasoning_max_tokens=_ROOM)
        assert now.requests[0] == today.requests[0]

    def test_the_room_is_for_that_call_only(self, wizard: WizardLLM) -> None:
        wire = Wire(STARVED_NULL, ANSWERED_AFTER_THINKING, OK)
        _run(wizard, wire, reasoning_max_tokens=_ROOM)
        _run(wizard, wire)
        assert wire.max_tokens == [_BUDGET, _ROOM, _BUDGET]

    def test_a_retry_that_answers_prose_gets_the_repair_prompt_with_the_room(self, wizard: WizardLLM) -> None:
        wire = Wire(STARVED_NULL, _body(_PROSE, reasoning=_THINKING), ANSWERED_AFTER_THINKING)
        assert _run(wizard, wire, reasoning_max_tokens=_ROOM) == _ANSWER
        assert wire.prompts[2].startswith(_REPAIR_HEAD)
        assert wire.prompts[2].endswith(_PROSE)
        assert wire.max_tokens == [_BUDGET, _ROOM, _ROOM]

    def test_a_retry_and_a_repair_that_both_fail_raise_todays_error(self, wizard: WizardLLM) -> None:
        wire = Wire(STARVED_NULL, _body(_PROSE, reasoning=_THINKING), _body(_NOT_JSON))
        with pytest.raises(ValueError, match=_TODAYS_ERROR):
            _run(wizard, wire, reasoning_max_tokens=_ROOM)
        assert wire.max_tokens == [_BUDGET, _ROOM, _ROOM]

    def test_starved_again_falls_back_to_todays_repair_of_the_first_answer(
        self, wizard: WizardLLM, caplog: pytest.LogCaptureFixture,
    ) -> None:
        first_text = STARVED_PARTIAL["choices"][0]["message"]["content"]
        wire = Wire(STARVED_PARTIAL, STARVED_NULL, REPAIR_OF_NOTHING)
        with caplog.at_level(logging.WARNING, logger="llm.wizard_llm"):
            assert _run(wizard, wire, reasoning_max_tokens=_ROOM) == {}
        assert [p.startswith(_REPAIR_HEAD) for p in wire.prompts] == [False, False, True]
        # The first answer is repaired, as today, under today's budget.
        assert wire.prompts[2].endswith(first_text)
        assert wire.max_tokens == [_BUDGET, _ROOM, _BUDGET]
        warnings = _wizard_warnings(caplog)
        assert len(warnings) == 1
        assert str(_ROOM) in warnings[0]
        assert _MAX_TOKENS in warnings[0]
        assert '{"chat_template_kwargs": {"enable_thinking": false}}' in warnings[0]

    @pytest.mark.parametrize("bad", [0, -1])
    @pytest.mark.parametrize("expect_json", [True, False])
    def test_the_argument_must_be_positive_and_is_checked_before_anything_is_sent(
        self, wizard: WizardLLM, bad: int, expect_json: bool,
    ) -> None:
        wire = Wire()
        with patch("requests.post", side_effect=wire), pytest.raises(ValueError, match="reasoning_max_tokens"):
            wizard.generate(_PROMPT, expect_json=expect_json, reasoning_max_tokens=bad)
        assert wire.requests == []


@pytest.fixture()
def project(tmp_path: Path):
    """A ``schema.yaml`` with tables to describe, and ``LLM_NUM_PREDICT`` at its default."""
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8", newline="\n")
    with cfg.override_settings(project_config_dir=str(tmp_path), llm_num_predict=_BUDGET):
        yield tmp_path


def _main(wizard: WizardLLM, monkeypatch: pytest.MonkeyPatch, wire: Wire, argv: list[str]) -> int:
    """``main(argv)`` with the real model connection (``_connect``) talking to *wire*."""
    monkeypatch.setattr(setup_project, "setup_wizard_llm", lambda *_a, **_k: wizard)
    with patch("requests.post", side_effect=wire):
        return main(argv)


@pytest.mark.usefixtures("project")
class TestTheReportedRun:
    """``main()`` with the real model connection (``_connect``), against :class:`Server`."""

    def test_a_table_the_model_reasoned_too_long_on_is_drafted(
        self, wizard: WizardLLM, monkeypatch: pytest.MonkeyPatch, project: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Before the fix: "FAILED (ValueError: the answer holds no description that can be used)".
        server = Server()
        assert _main(wizard, monkeypatch, server, _TRADE) == EXIT_OK
        assert "One row per trade made with a customer." in (project / OUTPUT_FILENAME).read_text(encoding="utf-8")
        assert "FAILED" not in capsys.readouterr().out
        # Sized for 5 descriptions, then --max-tokens.
        assert server.max_tokens == [600, _ROOM]
        assert server.prompts[0] == server.prompts[1]

    def test_the_room_is_the_max_tokens_flag(self, wizard: WizardLLM, monkeypatch: pytest.MonkeyPatch) -> None:
        server = Server()
        assert _main(wizard, monkeypatch, server, [*_TRADE, _MAX_TOKENS, "3000"]) == EXIT_OK
        assert server.max_tokens == [600, 3000]

    def test_a_model_that_answers_at_once_is_asked_once(
        self, wizard: WizardLLM, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        wire = Wire(ANSWERED_AFTER_THINKING)
        assert _main(wizard, monkeypatch, wire, _TRADE) == EXIT_OK
        assert wire.max_tokens == [600]

    @pytest.mark.parametrize(
        ("flag", "budgets"),
        [("600", [600, 600]), ("100", [_BUDGET, _BUDGET])],
        ids=["ceiling equal to the first call", "ceiling below LLM_NUM_PREDICT"],
    )
    def test_a_ceiling_with_no_room_above_the_first_call_is_todays_run(
        self, wizard: WizardLLM, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        flag: str, budgets: list[int],
    ) -> None:
        server = Server()
        assert _main(wizard, monkeypatch, server, [*_TRADE, _MAX_TOKENS, flag]) == EXIT_FAILED_TABLES
        assert _NO_DESCRIPTION in capsys.readouterr().out
        assert server.max_tokens == budgets
        assert server.prompts[1].startswith(_REPAIR_HEAD)

    def test_room_that_is_still_too_small_ends_as_today_after_one_more_call(
        self, wizard: WizardLLM, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    ) -> None:
        server = Server()
        assert _main(wizard, monkeypatch, server, [*_TRADE, _MAX_TOKENS, "1500"]) == EXIT_FAILED_TABLES
        assert _NO_DESCRIPTION in capsys.readouterr().out
        assert server.max_tokens == [600, 1500, 600]
