# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The examples call has room for the pairs it asks for, and keeps a cut-off answer's complete pairs.

Issue #252: ``examples.yaml`` came out empty after ``setup_project.py
--language fa``. Step 5 asks for ten question and SQL pairs in one call, and
that call ran under ``LLM_NUM_PREDICT`` (default 512 tokens), which is sized for
one SQL statement. Ten pairs need about three times that, so the answer was
cut off, did not parse, and the wizard sent the truncated text back to be
"repaired" under the same limit, which was cut off again. Every pair was lost,
including the ones written completely before the cut. Pinned here:

* ``salvage_object_list``: the complete objects of a cut-off list of objects;
* ``example_token_budget``: the ``max_tokens`` for an examples call;
* ``WizardLLM.generate_examples``: that budget on the wire, salvage instead of
  a repair prompt, and a clear error when nothing is complete;
* ``step5_examples``: a cut-off answer still writes its complete pairs, and the
  run says the answer was cut off.
"""

from __future__ import annotations

import pytest

import config as cfg
import setup_project as sp
from llm.base import LLMBackend
from llm.providers import salvage_object_list
from llm.wizard_llm import (
    EXAMPLE_TOKENS_PER_PAIR,
    ExampleAnswer,
    WizardLLM,
    example_token_budget,
)
from tests.test_setup_wizard_drafts import snapshot_of, table

_PAIR_1 = '{"tags": ["count"], "question": "تعداد کل قراردادها چقدر است؟", "sql": "SELECT COUNT(*) FROM s.Contract"}'
_PAIR_2 = '{"tags": ["sum"], "question": "جمع مبلغ قراردادها؟", "sql": "SELECT SUM(Amount) FROM s.Contract"}'
#: Two complete pairs, then the third cut off inside its SQL, as max_tokens leaves it.
_CUT_ARRAY = f'[{_PAIR_1}, {_PAIR_2}, {{"tags": ["top-n"], "question": "۱۰ تالار برتر", "sql": "SELECT TOP 10 r.Na'


class ScriptedBackend(LLMBackend):
    """Answers with the next scripted ``(text, finish_reason)`` and records every call."""

    def __init__(self, *replies: tuple[str, str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, int | None]] = []

    def generate(self, prompt: str) -> str:
        return self.generate_with_meta(prompt)[0]

    def generate_with_meta(self, prompt: str, *, max_tokens: int | None = None):
        self.calls.append((prompt, max_tokens))
        text, finish = self.replies.pop(0)
        return text, {"finish_reason": finish}


def _wizard(*replies: tuple[str, str]) -> tuple[WizardLLM, ScriptedBackend]:
    llm = WizardLLM(provider="mock", model="m")
    backend = ScriptedBackend(*replies)
    llm._backend = backend
    return llm, backend


class TestSalvageObjectList:
    def test_complete_objects_before_the_cut_are_kept_and_the_cut_one_is_not(self):
        items = salvage_object_list(_CUT_ARRAY)
        assert [i["tags"] for i in items] == [["count"], ["sum"]]

    def test_a_list_nested_under_a_known_key_is_found(self):
        text = f'{{"note": "here they are", "examples": [{_PAIR_1}, {{"question": "cut'
        assert len(salvage_object_list(text, ("examples",))) == 1

    def test_the_key_list_is_preferred_over_an_earlier_list(self):
        text = f'{{"tables": [{{"name": "x"}}], "examples": [{_PAIR_1}, {{"q'
        assert salvage_object_list(text, ("examples",))[0]["tags"] == ["count"]

    def test_fenced_answers_and_prose_in_front_are_read(self):
        assert len(salvage_object_list(f"Here you go:\n```json\n{_CUT_ARRAY}")) == 2

    def test_nothing_complete_gives_an_empty_list(self):
        assert salvage_object_list('[{"question": "cut in the first') == []
        assert salvage_object_list("") == []


class TestExampleTokenBudget:
    def test_the_default_sql_budget_is_raised_to_fit_the_pairs(self):
        assert example_token_budget(10, 512) == 10 * EXAMPLE_TOKENS_PER_PAIR + 64
        assert example_token_budget(10, 512) > 3 * 512

    def test_the_smaller_retry_still_gets_more_than_the_sql_budget(self):
        assert example_token_budget(5, 512) > 512

    def test_a_larger_configured_budget_is_never_lowered(self):
        assert example_token_budget(10, 8192) == 8192


class TestGenerateExamples:
    def test_the_call_carries_the_example_budget_not_llm_num_predict(self):
        llm, backend = _wizard((f"[{_PAIR_1}]", "stop"))
        with cfg.override_settings(llm_num_predict=512):
            llm.generate_examples("PROMPT", 10)
        assert backend.calls == [("PROMPT", example_token_budget(10, 512))]

    def test_a_complete_answer_is_returned_as_parsed(self):
        llm, _ = _wizard((f'{{"examples": [{_PAIR_1}, {_PAIR_2}]}}', "stop"))
        answer = llm.generate_examples("PROMPT", 10)
        assert answer == ExampleAnswer(parsed={"examples": [
            {"tags": ["count"], "question": "تعداد کل قراردادها چقدر است؟", "sql": "SELECT COUNT(*) FROM s.Contract"},
            {"tags": ["sum"], "question": "جمع مبلغ قراردادها؟", "sql": "SELECT SUM(Amount) FROM s.Contract"},
        ]})

    def test_a_cut_off_answer_is_salvaged_in_one_call(self):
        llm, backend = _wizard((_CUT_ARRAY, "length"))
        answer = llm.generate_examples("PROMPT", 10)
        assert answer.salvaged and answer.cut_off
        assert len(answer.parsed) == 2
        assert [prompt for prompt, _ in backend.calls] == ["PROMPT"]   # the truncated text was not sent back

    def test_an_answer_with_nothing_complete_raises_and_says_it_was_cut_off(self):
        llm, backend = _wizard(('[{"question": "cut in the fir', "length"))
        with pytest.raises(ValueError, match="cut off at the token limit"):
            llm.generate_examples("PROMPT", 10)
        assert len(backend.calls) == 1


class TestStepFiveKeepsACutOffAnswer:
    def test_the_complete_pairs_of_a_cut_off_answer_are_written(self, capsys):
        llm, backend = _wizard((_CUT_ARRAY, "length"))
        log: dict = {}

        examples = sp.step5_examples(None, snapshot_of(table("s", "Contract", "ID", "Amount", kind="fact")), llm, log, "fa")

        assert [e["question"] for e in examples] == ["تعداد کل قراردادها چقدر است؟", "جمع مبلغ قراردادها؟"]
        assert len(backend.calls) == 1                     # usable pairs: no smaller retry
        assert log["step5_examples"]["cut_off"] == ["all tables"]
        out = capsys.readouterr().out
        assert "Generated 2 examples" in out and "stopped at the token limit" in out

    def test_with_the_sql_budget_the_answer_would_have_been_lost(self):
        """Regression guard for the old path: the generic ``generate`` raises on this answer."""
        llm, _ = _wizard((_CUT_ARRAY, "length"), (_CUT_ARRAY, "length"))
        with pytest.raises(ValueError):
            llm.generate("PROMPT", expect_json=True)
