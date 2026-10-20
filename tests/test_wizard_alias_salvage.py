# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The alias call survives a model that loops until ``max_tokens`` cuts it off.

Seen on a deployment with a Qwen3-class model behind an OpenAI-compatible
gateway: for many tables the answer was ``{"aliases": ["a", "b", "a", "b", ...``
repeated until the token limit, the JSON never closed, and the wizard "repaired"
it by sending the truncated text back, which looped again. The table ended with
no aliases although the first few strings were usable. Pinned here:

* ``salvage_string_list``: the complete strings of a cut-off flat list, and only
  of a flat list of strings;
* ``WizardLLM.generate_aliases``: salvage, deduplication and the cap, no repair
  prompt that carries the truncated text, one stricter retry only when nothing
  was salvaged;
* the per-call ``max_tokens``: on the alias call only, never on the SQL path,
  and out of the way of a reasoning model;
* what ``step3_aliases`` tells the operator.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import config as cfg
import setup_project as sp
from core.wizard_drafts import ALIAS_LIMIT
from llm.base import LLMBackend
from llm.providers import OpenAIBackend, salvage_string_list
from llm.wizard_llm import (
    ALIAS_MAX_TOKENS,
    AliasAnswer,
    UnusableAnswerError,
    WizardLLM,
    alias_token_cap,
)

@pytest.fixture(autouse=True)
def _unwrapped_console(monkeypatch):
    """Keep the wizard's console from wrapping its lines (see test_setup_wizard.py)."""
    if hasattr(sp.console, "_width"):
        monkeypatch.setattr(sp.console, "_width", 10_000)


# ---------------------------------------------------------------------------
# salvage_string_list
# ---------------------------------------------------------------------------


class TestSalvageStringList:
    @pytest.mark.parametrize(("text", "expected"), [
        ('{"aliases": ["a", "b", "a", "c', ["a", "b", "a"]),
        ('{"aliases": ["a", "b", "a", "c"', ["a", "b", "a", "c"]),   # the string is whole; only "]" is missing
        ('{"aliases": ["a", "b",', ["a", "b"]),
        ('{"aliases": ["a", "b", ', ["a", "b"]),
        ('{"aliases": [', []),
        ('{"aliases": ["one', []),                                  # cut inside the first string
        ('{"aliases": [], "description": "x"}', []),                # an empty list is an empty list
        ('["x", "y", "z', ["x", "y"]),                              # a bare array
        ('{"aliases": ["a", "b"], "description": "cut off in', ["a", "b"]),   # list closed, rest cut
        ('```json\n{"aliases": ["a", "b", "c', ["a", "b"]),         # an unclosed fence
        ('Here you go: {"aliases": ["a", "b", "c', ["a", "b"]),     # prose in front
        ('{"aliases":["a","b","c', ["a", "b"]),                     # no spaces
        ('{\n  "aliases": [\n    "a",\n    "b",\n    "c', ["a", "b"]),
        ('{"aliases": ["a", "a", "a", "a', ["a", "a", "a"]),        # repeats are kept for the caller to count
    ])
    def test_the_complete_strings_before_the_cut(self, text, expected):
        assert salvage_string_list(text, "aliases") == expected

    def test_escaped_quotes_and_backslashes_are_read_not_cut_at(self):
        text = r'{"aliases": ["say \"hi\"", "back\\slash", "tab\there", "cut \"off'
        assert salvage_string_list(text, "aliases") == ['say "hi"', "back\\slash", "tab\there"]

    def test_unicode_escapes_are_decoded(self):
        text = r'{"aliases": ["café", "مشتری", "ک'
        assert salvage_string_list(text, "aliases") == ["café", "مشتری"]

    def test_a_cut_inside_a_unicode_escape_drops_only_that_string(self):
        assert salvage_string_list(r'{"aliases": ["a", "b\u06', "aliases") == ["a"]

    def test_persian_text(self):
        text = '{"aliases": ["مشتری", "فهرست مشتریان", "مشتری", "فهر'
        assert salvage_string_list(text, "aliases") == ["مشتری", "فهرست مشتریان", "مشتری"]

    @pytest.mark.parametrize("text", [
        '{"aliases": ["a", {"x": 1}, "b"',              # a nested object
        '{"aliases": ["a", ["b"], "c"',                 # a nested list
        '{"aliases": ["a", 3, "c"',                     # a number
        '{"aliases": ["a", null, "c"',
        '[{"question": "How many?", "sql": "SELECT COUNT(*) FROM t"}, {"question": "Top 5", "sql": "SELECT TOP 5 *',
        '{"examples": [{"question": "q", "sql": "SELECT 1"}, {"question": "r", "sql": "SELECT a FROM',
    ])
    def test_only_a_flat_list_of_strings_is_ever_salvaged(self, text):
        assert salvage_string_list(text, "aliases") == []
        assert salvage_string_list(text) == []

    def test_half_a_sql_statement_is_never_returned(self):
        assert salvage_string_list('{"sql": "SELECT a, b FROM t WHERE x = ') == []
        assert salvage_string_list('{"rules": {"value_col": "Amount", "rule_text": "Use the') == []
        assert salvage_string_list('SELECT a FROM t WHERE x IN (') == []

    @pytest.mark.parametrize("text", ["", "   ", "no json at all", "```", '{"aliases"', '{"aliases":', "{", "["])
    def test_nothing_to_salvage(self, text):
        assert salvage_string_list(text, "aliases") == []

    def test_the_key_selects_the_list(self):
        text = '{"other": ["p", "q"], "aliases": ["a", "b", "c'
        assert salvage_string_list(text, "aliases") == ["a", "b"]
        assert salvage_string_list(text) == ["p", "q"]                     # no key: the first list
        assert salvage_string_list(text, "missing") == []

    def test_a_list_after_a_nested_value_is_found(self):
        text = '{"meta": {"n": [1, 2]}, "aliases": ["a", "b", "c'
        assert salvage_string_list(text, "aliases") == ["a", "b"]

    def test_a_complete_answer_gives_the_same_list(self):
        assert salvage_string_list('{"aliases": ["a", "b"], "description": "d"}', "aliases") == ["a", "b"]


# ---------------------------------------------------------------------------
# The alias call
# ---------------------------------------------------------------------------

#: What the deployment sent back, shortened: the same two phrases until the limit.
def _looping(*phrases: str, cut: str = "ph") -> str:
    body = ", ".join(f'"{p}"' for p in list(phrases) * 12)
    return f'{{"aliases": [{body}, "{cut}'


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


class TestGenerateAliases:
    def test_a_truncated_repeating_answer_is_salvaged_deduplicated_and_not_asked_again(self):
        llm, backend = _wizard((_looping("customer list", "client"), "length"))

        answer = llm.generate_aliases("PROMPT")

        assert answer.aliases == ["customer list", "client"]
        assert answer.salvaged and answer.cut_off and answer.repeated and not answer.retried
        assert len(answer.answered) == 24                      # what the model wrote, before the cleaning
        assert [prompt for prompt, _ in backend.calls] == ["PROMPT"]   # one call: nothing was "repaired"

    def test_the_salvaged_list_is_capped(self):
        phrases = [f"phrase {chr(97 + n)}" for n in range(12)]
        text = '{"aliases": [' + ", ".join(f'"{p}"' for p in phrases) + ', "phra'
        llm, _ = _wizard((text, "length"))

        answer = llm.generate_aliases("PROMPT")

        assert len(answer.aliases) == ALIAS_LIMIT
        assert answer.aliases == phrases[:ALIAS_LIMIT]
        assert answer.salvaged and not answer.repeated

    def test_a_clean_answer_is_one_call_and_not_salvaged(self):
        llm, backend = _wizard(('{"aliases": ["a", "A", "b"], "description": "d"}', "stop"))

        answer = llm.generate_aliases("PROMPT")

        assert (answer.aliases, answer.description) == (["a", "b"], "d")
        assert not (answer.salvaged or answer.retried or answer.cut_off)
        assert len(backend.calls) == 1

    def test_an_empty_list_is_an_answer_and_is_not_asked_again(self):
        llm, backend = _wizard(('{"aliases": [], "description": "d"}', "stop"))
        assert llm.generate_aliases("PROMPT").aliases == []
        assert len(backend.calls) == 1

    def test_the_mock_provider_still_gives_an_empty_list(self):
        assert WizardLLM(provider="mock", model="mock").generate_aliases("anything").aliases == []

    def test_nothing_salvaged_gets_one_stricter_retry_with_the_original_prompt(self):
        first = '{"aliases": ["unfinished phrase that loops and loops'
        llm, backend = _wizard((first, "length"), ('{"aliases": ["x", "y"], "description": "d"}', "stop"))

        answer = llm.generate_aliases("ORIGINAL PROMPT")

        assert answer.aliases == ["x", "y"] and answer.retried and not answer.salvaged
        assert len(backend.calls) == 2
        retry_prompt = backend.calls[1][0]
        assert retry_prompt.startswith("ORIGINAL PROMPT")
        assert f"at most {ALIAS_LIMIT} aliases" in retry_prompt
        assert "Do not repeat a phrase" in retry_prompt
        # The truncated text is never sent back, and the prompt is not a "repair this" prompt.
        assert "unfinished phrase" not in retry_prompt
        assert "corrected JSON" not in retry_prompt

    def test_the_retry_may_itself_be_salvaged(self):
        llm, backend = _wizard(("not json at all", "stop"), (_looping("a b", "c d"), "length"))
        answer = llm.generate_aliases("PROMPT")
        assert answer.aliases == ["a b", "c d"] and answer.retried and answer.salvaged
        assert len(backend.calls) == 2

    def test_no_third_call_and_a_short_error_when_both_answers_are_unusable(self):
        garbage = '{"aliases": ["' + "x" * 3000
        llm, backend = _wizard((garbage, "length"), (garbage, "length"))

        with pytest.raises(UnusableAnswerError) as caught:
            llm.generate_aliases("PROMPT")

        assert len(backend.calls) == 2
        message = str(caught.value)
        assert isinstance(caught.value, ValueError)
        assert len(message) < 500                                # an excerpt, never the answer
        assert "x" * 100 not in message
        assert "cut off at the token limit" in message and "stricter prompt" in message
        assert "cut off at the token limit" in caught.value.reason

    def test_a_reasoning_model_that_spent_its_tokens_is_named_in_the_error(self):
        class Reasoning(ScriptedBackend):
            def generate_with_meta(self, prompt, *, max_tokens=None):
                text, meta = super().generate_with_meta(prompt, max_tokens=max_tokens)
                return text, {**meta, "reasoning_detected": True, "reasoning_tokens": 300}

        llm = WizardLLM(provider="mock", model="m")
        llm._backend = Reasoning(("", "length"), ("", "length"))
        with pytest.raises(UnusableAnswerError, match="LLM_EXTRA_BODY"):
            llm.generate_aliases("PROMPT")

    def test_a_bare_list_is_read_as_the_aliases(self):
        llm, _ = _wizard(('["a", "b"]', "stop"))
        assert llm.generate_aliases("PROMPT").aliases == ["a", "b"]

    def test_from_parsed_ignores_an_answer_of_another_shape(self):
        assert AliasAnswer.from_parsed({"aliases": "x"}).aliases == []
        assert AliasAnswer.from_parsed({"rules": {}}).aliases == []
        assert AliasAnswer.from_parsed("text").aliases == []


# ---------------------------------------------------------------------------
# The per-call max_tokens
# ---------------------------------------------------------------------------

def _reply(content: str, finish: str = "stop") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    return resp


@pytest.fixture
def openai_wizard(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    return WizardLLM(provider="openai", model="m", base_url="http://fake/v1")


class TestAliasTokenCap:
    def test_a_small_cap_for_a_short_answer(self):
        assert alias_token_cap(512) == ALIAS_MAX_TOKENS == 320

    def test_never_above_what_the_operator_configured(self):
        assert alias_token_cap(128) == 128

    def test_a_raised_budget_means_room_for_reasoning_so_no_cap(self):
        assert alias_token_cap(2048) is None
        assert alias_token_cap(513) is None

    def test_a_reasoning_switch_in_the_extra_body_means_no_cap(self):
        assert alias_token_cap(512, '{"chat_template_kwargs": {"enable_thinking": false}}') is None
        assert alias_token_cap(512, "  ") == 320


class TestMaxTokensReachesThePayload:
    def _posted(self, post) -> list[int]:
        return [call.kwargs["json"]["max_tokens"] for call in post.call_args_list]

    def test_the_alias_call_sends_its_own_cap_and_the_sql_call_does_not(self, openai_wizard):
        with cfg.override_settings(llm_num_predict=512, llm_extra_body_json=""), \
                patch("requests.post", return_value=_reply('{"aliases": ["a"], "description": "d"}')) as post:
            openai_wizard.generate_aliases("PROMPT")
            openai_wizard.generate("PROMPT")                                  # the wizard's other calls
            openai_wizard._backend.generate("SELECT-ish prompt")              # the SQL path's call
        assert self._posted(post) == [320, 512, 512]

    def test_a_raised_llm_num_predict_is_left_alone_by_the_alias_call(self, openai_wizard):
        with cfg.override_settings(llm_num_predict=2048, llm_extra_body_json=""), \
                patch("requests.post", return_value=_reply('{"aliases": ["a"]}')) as post:
            openai_wizard.generate_aliases("PROMPT")
        assert self._posted(post) == [2048]

    def test_the_override_is_not_remembered(self):
        backend = OpenAIBackend(model="m", api_key="k", base_url="http://fake/v1")
        with cfg.override_settings(llm_num_predict=512, llm_extra_body_json=""), \
                patch("requests.post", return_value=_reply("x")) as post:
            backend.generate("p", max_tokens=64)
            backend.generate("p")
            backend.generate_with_meta("p", max_tokens=32)
            backend.generate_with_meta("p")
        assert self._posted(post) == [64, 512, 32, 512]

    def test_the_other_fields_of_the_request_are_unchanged(self):
        backend = OpenAIBackend(model="m", api_key="k", base_url="http://fake/v1")
        with cfg.override_settings(llm_num_predict=512, llm_extra_body_json=""), \
                patch("requests.post", return_value=_reply("x")) as post:
            backend.generate("p")
            backend.generate("p", max_tokens=64)
        plain, capped = (call.kwargs["json"] for call in post.call_args_list)
        assert {k: v for k, v in capped.items() if k != "max_tokens"} == {k: v for k, v in plain.items() if k != "max_tokens"}

    @pytest.mark.parametrize("bad", [0, -5])
    def test_a_cap_below_one_is_refused(self, bad):
        backend = OpenAIBackend(model="m", api_key="k", base_url="http://fake/v1")
        with patch("requests.post") as post, pytest.raises(ValueError, match="positive"):
            backend.generate("p", max_tokens=bad)
        post.assert_not_called()

    def test_the_finish_reason_of_a_cut_off_answer_reaches_the_wizard(self, openai_wizard):
        with cfg.override_settings(llm_num_predict=512, llm_extra_body_json=""), \
                patch("requests.post", return_value=_reply(_looping("a b", "c d"), "length")):
            answer = openai_wizard.generate_aliases("PROMPT")
        assert answer.cut_off and answer.salvaged and answer.aliases == ["a b", "c d"]


# ---------------------------------------------------------------------------
# What the wizard says
# ---------------------------------------------------------------------------

def _snapshot(*names: str) -> SimpleNamespace:
    def table(name: str) -> SimpleNamespace:
        column = SimpleNamespace(name="Title", type="varchar", sample_values=[], is_pk=False)
        return SimpleNamespace(
            name=name, schema="sales", full_name=f"sales.{name}", classification="dim",
            columns=[column], foreign_keys=[], datasources=(),
        )

    tables = [table(n) for n in names]
    return SimpleNamespace(tables=tables, fact_tables=[], dim_tables=tables, relationships=[])


class _Aliasing:
    """A wizard model whose ``generate_aliases`` answers per table from a script."""

    provider = "fake"
    model = "fake"

    def __init__(self, script) -> None:
        self.script = script

    def generate_aliases(self, prompt: str, *, limit: int = ALIAS_LIMIT):
        name = prompt.split("Table name: sales.")[1].splitlines()[0]
        result = self.script[name]
        if isinstance(result, Exception):
            raise result
        return result


def _run_step3(llm, *names: str, log: dict | None = None):
    args = SimpleNamespace(non_interactive=True, dry_run=False, allow_repeated_answers=False)
    log = {} if log is None else log
    result = sp.step3_aliases(args, _snapshot(*names), llm, log, "en")
    return result, log


class TestStepThreeReports:
    def test_a_salvaged_list_is_said_to_be_salvaged_in_the_step_line_and_the_summary(self, capsys):
        llm, _ = _wizard((_looping("customer list", "client"), "length"))
        # A real WizardLLM on a scripted backend, through the real prompt.
        result, log = _run_step3(llm, "Customer")

        out = capsys.readouterr().out
        assert "sales.Customer ... 2 aliases (salvaged 2 from a truncated answer)" in out
        assert "1 alias list(s) were salvaged from a truncated answer" in out
        assert result["entities"]["Customer"]["aliases"] == ["customer list", "client"]
        assert log["step3_aliases"]["aliases_salvaged"] == ["sales.Customer"]
        assert log["step3_aliases"]["aliases_failed"] == []

    def test_a_table_with_no_usable_answer_says_why_in_the_step_line_not_only_in_a_log_line(self, capsys, caplog):
        garbage = '{"aliases": ["' + "x" * 600
        llm, backend = _wizard((garbage, "length"), (garbage, "length"))

        result, log = _run_step3(llm, "Customer")

        out = capsys.readouterr().out
        assert "sales.Customer ... 0 aliases (no usable answer: cut off at the token limit" in out
        assert "1 table(s) got no aliases because the model call failed or its answer was unusable: sales.Customer" in out
        assert result["entities"]["Customer"]["aliases"] == []
        assert log["step3_aliases"]["aliases_failed"] == ["sales.Customer"]
        assert len(backend.calls) == 2
        assert "x" * 100 not in out and "x" * 100 not in caplog.text          # never the answer itself

    def test_a_failed_call_is_named_by_its_kind(self, capsys):
        llm = _Aliasing({"Customer": RuntimeError("endpoint down")})
        _run_step3(llm, "Customer")
        assert "0 aliases (the model call failed: RuntimeError)" in capsys.readouterr().out

    def test_a_clean_run_prints_none_of_it(self, capsys):
        llm = _Aliasing({"Customer": AliasAnswer.from_parsed({"aliases": ["a", "b"], "description": "d"})})
        _, log = _run_step3(llm, "Customer")
        out = capsys.readouterr().out
        assert "sales.Customer ... 2 aliases" in out
        assert "salvaged" not in out and "no aliases" not in out
        assert log["step3_aliases"]["aliases_salvaged"] == [] and log["step3_aliases"]["aliases_failed"] == []

    def test_the_final_summary_repeats_both(self, capsys):
        log = {"step3_aliases": {"aliases_salvaged": ["sales.A", "sales.B"], "aliases_failed": ["sales.C"]}}
        sp._print_summary(log, Path("out"), None, SimpleNamespace(non_interactive=True, dry_run=False))
        out = capsys.readouterr().out
        assert "Aliases: 2 table(s) got a list salvaged from a truncated answer (sales.A, sales.B)" in out
        assert "Aliases: 1 table(s) got none because the model call failed or its answer was unusable (sales.C)" in out

    def test_the_alias_prompt_asks_for_distinct_aliases_and_a_stop(self):
        prompt = sp._prompt_aliases("sales.Customer", ["Title"], [], "en")
        assert f"at most {ALIAS_LIMIT} aliases" in prompt
        assert "do not repeat a phrase" in prompt
        assert "Stop after the list" in prompt
