# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""What the answer-summary prompt and its post-processing do today, pinned.

The summary prompt is rewritten so that Rial/ریال is attached to money only
(``tests/test_interpret_units.py`` covers that). These tests are written
first and pass before and after the rewrite: they hold what must not move.

* A money column still gets a Rial/ریال instruction, and toman/تومان is still
  forbidden in the prompt.
* The question, the rows (at most :data:`~llm.interpret.MAX_PREVIEW_ROWS` of
  them) and the empty-result marker are still in the prompt.
* The model's answer still goes through the toman -> Rial replacement and the
  thousands separators, and an empty or failing answer is still ``""``.
"""

from __future__ import annotations

import pytest

from llm.interpret import MAX_PREVIEW_ROWS, interpret_rows
from llm.providers import MockBackend
from llm.router import LLMRouter


class _Capture(MockBackend):
    """Answers with *summary* and keeps every prompt it was given."""

    def __init__(self, summary: str = "summary") -> None:
        super().__init__(response="SELECT 1")
        self.summary = summary
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.summary


def _prompt_for(question: str, rows: list[dict]) -> str:
    backend = _Capture()
    interpret_rows(LLMRouter(default_chain=[backend]), question, rows)
    assert len(backend.prompts) == 1
    return backend.prompts[0]


class TestThePromptStillCarriesTheMoneyRule:
    def test_a_money_column_still_asks_for_rial(self):
        prompt = _prompt_for("What is the total sales value?", [{"TotalValue": 120_000_000}])
        assert "Rial/ریال" in prompt
        assert "Iranian Rials" in prompt

    def test_toman_is_still_forbidden(self):
        prompt = _prompt_for("What is the total sales value?", [{"TotalValue": 120_000_000}])
        assert "never use toman/تومان" in prompt

    def test_the_question_and_the_rows_are_in_the_prompt(self):
        prompt = _prompt_for("How many orders?", [{"OrderCount": 7}])
        assert "How many orders?" in prompt
        assert "{'OrderCount': 7}" in prompt
        assert "up to 20 rows shown" in prompt

    def test_only_the_preview_rows_are_sent(self):
        rows = [{"n": i} for i in range(MAX_PREVIEW_ROWS + 5)]
        prompt = _prompt_for("q", rows)
        assert f"{{'n': {MAX_PREVIEW_ROWS - 1}}}" in prompt
        assert f"{{'n': {MAX_PREVIEW_ROWS}}}" not in prompt

    def test_an_empty_result_is_marked(self):
        prompt = _prompt_for("q", [])
        assert "(empty result set)" in prompt
        assert "If the result is empty, say so clearly." in prompt


class TestTheAnswerIsStillPostProcessed:
    @staticmethod
    def _answer(summary: str) -> str:
        router = LLMRouter(default_chain=[_Capture(summary)])
        return interpret_rows(router, "q", [{"a": 1}])

    def test_persian_toman_becomes_rial(self):
        assert self._answer("مبلغ ۵ تومان بود") == "مبلغ ۵ ریال بود"

    def test_latin_toman_becomes_rial_in_any_case(self):
        assert self._answer("Total 5 Toman and 6 TOMAN") == "Total 5 Rial and 6 Rial"

    def test_large_numbers_get_thousands_separators(self):
        assert self._answer("total 33493503232 units") == "total 33,493,503,232 units"

    def test_a_year_is_left_alone(self):
        assert self._answer("in 1402") == "in 1402"

    def test_a_blank_answer_is_empty(self):
        assert self._answer("   ") == ""

    def test_a_failing_backend_gives_an_empty_summary(self):
        class _Down(MockBackend):
            def generate(self, prompt: str) -> str:
                raise RuntimeError("down")

        assert interpret_rows(LLMRouter(default_chain=[_Down()]), "q", [{"a": 1}]) == ""


def test_the_runner_template_and_the_shared_template_agree_on_the_money_rule():
    """Two prompts used to carry the same wording; neither may lose it."""
    import api.runner as runner
    from llm.interpret import INTERPRET_TEMPLATE

    for template in (INTERPRET_TEMPLATE, runner._INTERPRET_TEMPLATE):
        assert "Rial/ریال" in template
        assert "toman/تومان" in template
        assert "{question}" in template and "{rows}" in template


@pytest.mark.parametrize("rows", [[{"a": 1}], [{"a": 1, "b": "x"}], []])
def test_the_prompt_can_always_be_built(rows):
    assert _prompt_for("q", rows)
