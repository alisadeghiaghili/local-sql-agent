# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A fully denied column named in the prompt, behind ``DENIED_COLUMNS_PROMPT_NOTE`` (issue #263).

The report: with a key whose ``denied_columns`` hides a customer column (the
national ID), «لیست مشتریانی که زعفران خریده‌اند» is refused by the guard.
The schema block of the prompt lists every column of ``sales.Customer``,
national ID included, and nothing in the prompt says the key may not use it,
so a model asked for a list of customers selects it (or writes ``c.*``). A
legacy full-deny entry is a :class:`~security.sql_guard.PolicyRejection`,
terminal by design because "the policy that caused it is not in the prompt":
the request ends on the first answer.

The fix puts the policy in the prompt. With ``DENIED_COLUMNS_PROMPT_NOTE=true``
the ``COLUMN ACCESS`` section (already used for join-only entries) also names
the key's fully denied columns. The restriction itself does not change: the
guard refuses the same statements, terminally, with the setting on or off.

1. **Pins of today's behaviour, with the setting off** (the default): the
   prompt is byte-identical to the one built without the setting, a full deny
   leaves it identical to an unrestricted key's, and the request is refused
   after one call.
2. **With the setting on**: the note, where it goes, what it says, and that
   the question is answered from the first call.
3. **The restriction is not loosened**, with the setting on.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd
import pytest

import config as cfg
from core.models import RetrievalContext
from llm.base import LLMBackend
from llm.router import build_prompt_segments
from llm.sql_agent import SQLAgent
from security.column_policy import denied_prompt_line, join_only_prompt_line
from security.sql_guard import PolicyRejection, validate_sql

_SYSTEM_PROMPT = "You are a T-SQL expert."
_QUESTION = "لیست مشتریانی که زعفران خریده‌اند"
_JOIN_ONLY = "sales.Order.ID"

#: What a model writes for the question when nothing tells it about the policy.
_WITH_NATIONAL_ID = (
    "SELECT DISTINCT c.Name, c.NationalID FROM sales.[Order] o "
    "JOIN sales.Customer c ON o.CustomerID = c.ID "
    "JOIN ref.Symbol s ON o.SymbolID = s.ID WHERE s.Commodity_PersianName = N'زعفران'"
)
_STAR = (
    "SELECT DISTINCT c.* FROM sales.[Order] o JOIN sales.Customer c ON o.CustomerID = c.ID "
    "JOIN ref.Symbol s ON o.SymbolID = s.ID WHERE s.Commodity_PersianName = N'زعفران'"
)
#: What it writes when the prompt says the column may not be used.
_ALLOWED = (
    "SELECT DISTINCT c.ID, c.Name FROM sales.[Order] o JOIN sales.Customer c ON o.CustomerID = c.ID "
    "JOIN ref.Symbol s ON o.SymbolID = s.ID WHERE s.Commodity_PersianName = N'زعفران'"
)

#: Every shape of ``denied_columns`` a key can have.
_POLICIES = {
    "none": None,
    "empty": [],
    "full deny": ["NationalID"],
    "join-only": [_JOIN_ONLY],
    "both": ["NationalID", _JOIN_ONLY],
    "blank entry": ["  "],
}


def _segments(denied: Iterable[str] | None, *, retrieval: bool = False):
    budget = {"prompt_retrieval_token_budget": 1} if retrieval else {}
    with cfg.override_settings(**budget):
        return build_prompt_segments(_QUESTION, _SYSTEM_PROMPT, RetrievalContext(), denied_columns=denied)


class _PolicyAwareModel(LLMBackend):
    """Writes the national-ID query unless the prompt says the key may not use that column."""

    def __init__(self, careless: str = _WITH_NATIONAL_ID) -> None:
        self.careless = careless
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "policy-aware:test"

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        told = "Columns you must not use at all" in prompt and "NationalID" in prompt.split("COLUMN ACCESS", 1)[-1]
        return _ALLOWED if told else self.careless


def _agent(backend: LLMBackend) -> SQLAgent:
    return SQLAgent(backend=backend, execute_fn=lambda sql: pd.DataFrame({"Name": ["x"]}), max_corrections=2)


# ---------------------------------------------------------------------------
# 1. Pins: the setting off
# ---------------------------------------------------------------------------


class TestTheSettingIsOffByDefault:
    def test_the_default_is_off(self):
        assert cfg.Settings().denied_columns_prompt_note is False

    @pytest.mark.parametrize("retrieval", [False, True], ids=["static prefix", "retrieval"])
    @pytest.mark.parametrize("denied", _POLICIES.values(), ids=_POLICIES.keys())
    def test_off_is_byte_identical_to_the_default(self, denied, retrieval):
        default = _segments(denied, retrieval=retrieval)
        with cfg.override_settings(denied_columns_prompt_note=False):
            assert _segments(denied, retrieval=retrieval) == default

    @pytest.mark.parametrize("retrieval", [False, True], ids=["static prefix", "retrieval"])
    def test_off_a_full_deny_leaves_the_prompt_of_an_unrestricted_key(self, retrieval):
        assert _segments(["NationalID"], retrieval=retrieval) == _segments(None, retrieval=retrieval)
        assert _segments(["NationalID", _JOIN_ONLY], retrieval=retrieval) == _segments([_JOIN_ONLY], retrieval=retrieval)

    def test_off_the_reported_question_is_refused_after_one_call(self):
        backend = _PolicyAwareModel()
        with pytest.raises(PolicyRejection) as excinfo:
            _agent(backend).run(_QUESTION, _SYSTEM_PROMPT, denied_columns=("NationalID",))
        assert excinfo.value.reason == "denied_column"
        assert len(backend.prompts) == 1

    def test_off_a_star_over_the_table_is_refused_after_one_call(self):
        backend = _PolicyAwareModel(careless=_STAR)
        with pytest.raises(PolicyRejection):
            _agent(backend).run(_QUESTION, _SYSTEM_PROMPT, denied_columns=("NationalID",))
        assert len(backend.prompts) == 1


# ---------------------------------------------------------------------------
# 2. The setting on
# ---------------------------------------------------------------------------


@pytest.fixture()
def note_on():
    with cfg.override_settings(denied_columns_prompt_note=True):
        yield


class TestTheNote:
    def test_the_line(self):
        assert denied_prompt_line(["NationalID"]) == (
            "Columns you must not use at all (not in SELECT, WHERE, JOIN, GROUP BY or ORDER BY), "
            "and never write * or alias.* over a table that has one; name the other columns instead: "
            "NationalID"
        )

    @pytest.mark.parametrize("denied", [None, [], [_JOIN_ONLY], ["main:ID"], ["  ", "‌"]])
    def test_no_full_deny_no_line(self, denied):
        assert denied_prompt_line(denied) == ""

    def test_entries_as_the_operator_wrote_them_once_each_in_order(self):
        line = denied_prompt_line([" NationalID‏", "Phone", "nationalid", _JOIN_ONLY, "Phone"])
        assert line.endswith(": NationalID, Phone")

    def test_an_entry_that_is_not_text_is_left_out_and_nothing_raises(self):
        # parse_column_policy reports such an entry; the prompt line never raises over one.
        assert denied_prompt_line([None, 7, "NationalID"]).endswith(": NationalID")
        assert denied_prompt_line([None, 7]) == ""

    @pytest.mark.parametrize("retrieval", [False, True], ids=["static prefix", "retrieval"])
    def test_it_is_in_the_column_access_section_of_the_per_question_segment(self, note_on, retrieval):
        segments = _segments(["NationalID"], retrieval=retrieval)
        section = segments.question.split("COLUMN ACCESS", 1)[1]
        assert denied_prompt_line(["NationalID"]) in section
        assert "Columns you must not use at all" not in segments.static_prefix
        text = segments.question
        assert text.index("SESSION CONTEXT") < text.index("COLUMN ACCESS") < text.index("USER QUESTION")

    def test_the_join_only_line_comes_first_and_is_unchanged(self, note_on):
        section = _segments(["NationalID", _JOIN_ONLY]).question
        join_line = join_only_prompt_line([_JOIN_ONLY], None)
        assert f"{join_line}\n{denied_prompt_line(['NationalID'])}\n" in section

    @pytest.mark.parametrize("denied", [None, [], [_JOIN_ONLY], ["  "]], ids=["none", "empty", "join-only", "blank"])
    def test_a_key_without_a_full_deny_gets_the_same_prompt_as_with_the_setting_off(self, denied):
        off = _segments(denied)
        with cfg.override_settings(denied_columns_prompt_note=True):
            assert _segments(denied) == off

    def test_the_static_prefix_is_the_same_for_every_key(self, note_on):
        prefixes = {_segments(denied).static_prefix for denied in _POLICIES.values()}
        assert len(prefixes) == 1

    def test_the_reported_question_is_answered_from_the_first_call(self, note_on):
        backend = _PolicyAwareModel()
        df, result = _agent(backend).run(_QUESTION, _SYSTEM_PROMPT, denied_columns=("NationalID",))
        assert len(backend.prompts) == 1
        assert "NationalID" not in result.sql
        assert list(df["Name"]) == ["x"]

    def test_a_key_that_answers_without_the_column_is_asked_once_either_way(self, note_on):
        backend = _PolicyAwareModel(careless=_ALLOWED)
        _agent(backend).run(_QUESTION, _SYSTEM_PROMPT, denied_columns=("NationalID",))
        assert len(backend.prompts) == 1


# ---------------------------------------------------------------------------
# 3. The restriction is not loosened
# ---------------------------------------------------------------------------


class TestTheRestrictionIsUnchanged:
    @pytest.mark.parametrize("sql", [_WITH_NATIONAL_ID, _STAR], ids=["named", "alias.*"])
    def test_the_guard_refuses_the_same_statements_terminally(self, note_on, sql):
        with pytest.raises(PolicyRejection) as excinfo:
            validate_sql(sql, denied_columns=["NationalID"])
        assert excinfo.value.reason == "denied_column" and excinfo.value.is_refusal

    def test_a_model_that_ignores_the_note_is_still_refused_after_one_call(self, note_on):
        class Ignores(_PolicyAwareModel):
            def generate(self, prompt: str) -> str:
                self.prompts.append(prompt)
                return _WITH_NATIONAL_ID

        backend = Ignores()
        with pytest.raises(PolicyRejection):
            _agent(backend).run(_QUESTION, _SYSTEM_PROMPT, denied_columns=("NationalID",))
        assert len(backend.prompts) == 1
        assert "Columns you must not use at all" in backend.prompts[0]

    def test_the_allowed_statement_passes_the_guard(self):
        validate_sql(_ALLOWED, denied_columns=["NationalID"])
