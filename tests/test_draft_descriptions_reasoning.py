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

1. **Pins of today's behaviour** (they passed before the fix and must keep
   passing): ``WizardLLM.generate`` without the new argument, which is how the
   setup wizard calls it, and every non-reasoning answer even with it.
2. **The reported shapes**, reproduced end to end through ``main()``; they
   failed before the fix.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import config as cfg
import setup_project
from llm.wizard_llm import WizardLLM
from scripts.draft_descriptions import EXIT_OK, OUTPUT_FILENAME, main
from tests.test_draft_descriptions import SCHEMA

_MODEL = "Qwen/Qwen3.8-27B-FP8"
_REPAIR_HEAD = "The text below must be a valid JSON object or array but failed to parse."

#: The reasoning text the report quotes, as the server's reasoning parser returns it.
_THINKING = (
    "We need answer user's request. Need produce JSON only, Persian descriptions. "
    "Need analyze table ActionType schema Auction_Dim data source Auction_DM."
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
STARVED_NULL = _body(None, "length", reasoning=_THINKING)
#: The same, with the JSON started but cut off.
STARVED_PARTIAL = _body('{"table": "هر ردیف یک نوع', "length", reasoning=_THINKING)
#: No reasoning parser on the server: the thinking arrives in ``content`` itself.
STARVED_INLINE = _body("<think>" + _THINKING, "length")
#: The server counts the reasoning tokens but returns no reasoning text.
STARVED_COUNTED = _body("", "length", reasoning_tokens=600)
#: The model's answer to a repair prompt that had nothing to repair, as in the report.
REPAIR_OF_NOTHING = _body("{}", reasoning=_THINKING_ON_REPAIR)
#: A reasoning model's complete answer.
ANSWERED_AFTER_THINKING = _body(json.dumps(_ANSWER), reasoning=_THINKING)


class Wire:
    """Stands for ``requests.post``: answers with the next scripted body and records every request."""

    def __init__(self, *bodies: dict[str, Any]) -> None:
        self.bodies = list(bodies)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, *, headers: dict, json: dict, timeout: float) -> MagicMock:  # noqa: A002, ARG002
        self.requests.append(copy.deepcopy(json))
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = self.bodies.pop(0)
        response.raise_for_status = MagicMock()
        return response

    @property
    def prompts(self) -> list[str]:
        return [r["messages"][0]["content"] for r in self.requests]

    @property
    def max_tokens(self) -> list[int]:
        return [r["max_tokens"] for r in self.requests]


@pytest.fixture()
def wizard(monkeypatch) -> WizardLLM:
    """The wizard's model on a local OpenAI-compatible endpoint."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return WizardLLM(provider="openai", model=_MODEL, base_url="http://localhost:8080/v1")


def _run(wizard: WizardLLM, wire: Wire, **kwargs: Any) -> Any:
    with patch("requests.post", side_effect=wire), cfg.override_settings(llm_num_predict=512):
        return wizard.generate("PROMPT", expect_json=True, **kwargs)


# ---------------------------------------------------------------------------
# 1. Pins: today's behaviour
# ---------------------------------------------------------------------------


class TestTodaysContractWithoutTheNewArgument:
    """How the setup wizard calls ``generate``: unchanged, whatever the answer."""

    def test_a_parsable_first_answer_is_one_call(self, wizard):
        wire = Wire(_body('{"x": 1}'))
        assert _run(wizard, wire) == {"x": 1}
        assert wire.prompts == ["PROMPT"]

    def test_an_unparsable_answer_gets_one_repair_prompt_with_its_text(self, wizard):
        wire = Wire(_body("not json at all"), _body('{"x": 2}'))
        assert _run(wizard, wire) == {"x": 2}
        assert wire.prompts[1].startswith(_REPAIR_HEAD)
        assert wire.prompts[1].endswith("not json at all")
        assert wire.max_tokens == [512, 512]

    def test_a_reasoning_model_cut_off_still_gets_the_repair_prompt(self, wizard):
        # The issue's sequence, kept as it is for the callers that do not opt in.
        wire = Wire(STARVED_NULL, REPAIR_OF_NOTHING)
        assert _run(wizard, wire) == {}
        assert [p.startswith(_REPAIR_HEAD) for p in wire.prompts] == [False, True]
        assert wire.max_tokens == [512, 512]

    def test_two_unparsable_answers_raise_the_same_error(self, wizard):
        wire = Wire(_body("still not json"), _body("still not json"))
        with pytest.raises(ValueError, match=r"\[WizardLLM\] Could not extract valid JSON after retry"):
            _run(wizard, wire)
        assert len(wire.requests) == 2


#: Non-reasoning answers, and reasoning answers that were not cut off: with the
#: new argument, the requests must be exactly the ones sent without it.
_NOT_STARVED = {
    "parsable": [_body('{"x": 1}')],
    "prose, then repaired": [_body("Sure! Here it is."), _body('{"x": 1}')],
    "cut off, no reasoning": [_body('{"table": "One row', "length"), _body('{"x": 1}')],
    "empty, no reasoning": [_body("", "length"), _body('{"x": 1}')],
    "null content, stop": [_body(None), _body('{"x": 1}')],
    "reasoning, answered": [ANSWERED_AFTER_THINKING],
    "reasoning, prose, not cut off": [_body("I cannot.", reasoning=_THINKING), _body('{"x": 1}')],
    "reasoning counted as zero, cut off": [_body("", "length", reasoning_tokens=0), _body('{"x": 1}')],
}


class TestNothingChangesForAnAnswerThatWasNotStarved:
    @pytest.mark.parametrize("bodies", _NOT_STARVED.values(), ids=_NOT_STARVED.keys())
    def test_the_requests_and_the_result_are_the_same_with_and_without_the_argument(self, wizard, bodies):
        before, after = Wire(*copy.deepcopy(bodies)), Wire(*copy.deepcopy(bodies))
        assert _run(wizard, before) == _run(wizard, after, reasoning_max_tokens=8192)
        assert after.requests == before.requests

    def test_two_unparsable_answers_raise_the_same_error_with_the_argument(self, wizard):
        wire = Wire(_body("still not json"), _body("still not json"))
        with pytest.raises(ValueError, match=r"\[WizardLLM\] Could not extract valid JSON after retry"):
            _run(wizard, wire, reasoning_max_tokens=8192)
        assert len(wire.requests) == 2

    def test_no_more_room_than_the_first_call_had_means_todays_path(self, wizard):
        before, after = Wire(STARVED_NULL, REPAIR_OF_NOTHING), Wire(STARVED_NULL, REPAIR_OF_NOTHING)
        assert _run(wizard, before) == _run(wizard, after, reasoning_max_tokens=512)
        assert after.requests == before.requests


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
    @pytest.mark.parametrize("starved", _STARVED.values(), ids=_STARVED.keys())
    def test_the_original_prompt_is_sent_again_with_the_room(self, wizard, starved):
        wire = Wire(starved, ANSWERED_AFTER_THINKING)
        assert _run(wizard, wire, reasoning_max_tokens=8192) == _ANSWER
        assert wire.prompts == ["PROMPT", "PROMPT"]       # never the repair prompt
        assert wire.max_tokens == [512, 8192]

    def test_the_first_request_is_the_one_sent_today(self, wizard):
        today, now = Wire(STARVED_NULL, REPAIR_OF_NOTHING), Wire(STARVED_NULL, ANSWERED_AFTER_THINKING)
        _run(wizard, today)
        _run(wizard, now, reasoning_max_tokens=8192)
        assert now.requests[0] == today.requests[0]

    def test_the_room_is_for_that_call_only(self, wizard):
        wire = Wire(STARVED_NULL, ANSWERED_AFTER_THINKING, _body('{"x": 1}'))
        _run(wizard, wire, reasoning_max_tokens=8192)
        _run(wizard, wire)
        assert wire.max_tokens == [512, 8192, 512]

    def test_a_retry_that_answers_prose_gets_the_repair_prompt_with_the_room(self, wizard):
        wire = Wire(STARVED_NULL, _body("Here is the answer.", reasoning=_THINKING), ANSWERED_AFTER_THINKING)
        assert _run(wizard, wire, reasoning_max_tokens=8192) == _ANSWER
        assert wire.prompts[2].startswith(_REPAIR_HEAD) and wire.prompts[2].endswith("Here is the answer.")
        assert wire.max_tokens == [512, 8192, 8192]

    def test_starved_again_falls_back_to_todays_repair_and_names_the_two_remedies(self, wizard, caplog):
        wire = Wire(STARVED_NULL, STARVED_NULL, REPAIR_OF_NOTHING)
        with caplog.at_level(logging.WARNING, logger="llm.wizard_llm"):
            assert _run(wizard, wire, reasoning_max_tokens=8192) == {}
        assert [p.startswith(_REPAIR_HEAD) for p in wire.prompts] == [False, False, True]
        assert wire.max_tokens == [512, 8192, 512]                  # the repair as today
        warning = " ".join(r.getMessage() for r in caplog.records if r.name == "llm.wizard_llm")
        assert "8192" in warning and "--max-tokens" in warning
        assert '{"chat_template_kwargs": {"enable_thinking": false}}' in warning

    def test_the_argument_must_be_positive(self, wizard):
        with pytest.raises(ValueError, match="reasoning_max_tokens"):
            _run(wizard, Wire(), reasoning_max_tokens=0)


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8", newline="\n")
    with cfg.override_settings(project_config_dir=str(tmp_path), llm_num_predict=512):
        yield tmp_path


class TestTheReportedRun:
    """``main()`` with the real model connection (``_connect``), as the operator ran it."""

    def _main(self, wizard, monkeypatch, wire, argv):
        monkeypatch.setattr(setup_project, "setup_wizard_llm", lambda *a, **k: wizard)
        with patch("requests.post", side_effect=wire):
            return main(argv)

    def test_a_table_the_model_reasoned_too_long_on_is_drafted(self, wizard, monkeypatch, project, capsys):
        wire = Wire(STARVED_NULL, ANSWERED_AFTER_THINKING)
        assert self._main(wizard, monkeypatch, wire, ["--tables", "sales.Trade"]) == EXIT_OK
        assert "One row per trade made with a customer." in (project / OUTPUT_FILENAME).read_text(encoding="utf-8")
        assert "FAILED" not in capsys.readouterr().out
        assert wire.max_tokens == [600, 8192]       # sized for 5 descriptions, then --max-tokens

    def test_the_room_is_the_max_tokens_flag(self, wizard, monkeypatch, project):
        wire = Wire(STARVED_PARTIAL, ANSWERED_AFTER_THINKING)
        argv = ["--tables", "sales.Trade", "--max-tokens", "3000"]
        assert self._main(wizard, monkeypatch, wire, argv) == EXIT_OK
        assert wire.max_tokens == [600, 3000]

    def test_a_model_that_answers_at_once_is_asked_once(self, wizard, monkeypatch, project):
        wire = Wire(ANSWERED_AFTER_THINKING)
        assert self._main(wizard, monkeypatch, wire, ["--tables", "sales.Trade"]) == EXIT_OK
        assert wire.max_tokens == [600]
