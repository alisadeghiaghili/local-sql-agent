# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The session engine rebuilds the retrieval context field by field.

``TurnEngine`` merges the session's filters into the retrieved context, which
it does by constructing a new :class:`~core.models.RetrievalContext` from the
old one. A field it does not copy silently reverts to its default on the
session path only: the same question then gets a different prompt there than
from ``ContextRetriever`` directly. These tests pin what is carried, and fail
for a field added to the dataclass that nobody decided about.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import pandas as pd

from core.models import RetrievalContext
from llm.providers import MockBackend
from llm.router import LLMRouter
from session import engine as engine_module
from session.engine import TurnEngine
from session.store import SessionStore

_SYSTEM_PROMPT = "You are a T-SQL expert."

#: Every field the rebuild must copy, with a value that differs from its default.
_CARRIED = {
    "entities": ["Customer"],
    "facts": ["Order"],
    "dimensions": ["Customer"],
    "join_tables": ["Bridge"],
    "metric_tables": ["Invoice"],
    "relationships": ["JOIN A ON A.x = B.x"],
    "business_rules": ["Count settled contracts only."],
    "examples": [{"question": "Q", "sql": "SELECT 1", "tags": ["t"]}],
    "resolved_values": {"Customer.Name": ["Acme"]},
}

#: Fields the rebuild does not copy, and why that is right. ``filters`` is
#: merged with the session's, not copied.
_NOT_COPIED = {
    "filters": "merged with the session's filters",
    "warnings": "folded into the turn from the original context, never read by the prompt",
    "token_tier_filters": "read from the original context by the filter-enforcement check",
    "value_clarifications": "not read by the prompt builder",
}


def _rebuilt(context: RetrievalContext) -> RetrievalContext:
    """Ask one question through ``TurnEngine`` and return the context its prompt was built from."""
    seen: list[RetrievalContext] = []
    real = engine_module.build_prompt_segments

    def spy(question, system_prompt, ctx, **kwargs):
        seen.append(ctx)
        return real(question, system_prompt, ctx, **kwargs)

    engine = TurnEngine(
        router=LLMRouter(default_chain=[MockBackend()]),
        execute_fn=lambda sql: pd.DataFrame({"Name": ["A"]}),
    )
    record = SessionStore(ttl_seconds=60, max_size=10, max_turns=10).create()
    with patch("session.engine.ContextRetriever.retrieve", return_value=context), \
            patch("session.engine.build_prompt_segments", spy):
        engine.ask(record, "how many customers do we have", _SYSTEM_PROMPT)
    assert seen, "the question never reached the prompt builder"
    return seen[0]


class TestTheSessionRebuildKeepsTheContext:
    def test_every_field_is_either_carried_or_deliberately_not(self):
        names = {f.name for f in dataclasses.fields(RetrievalContext)}

        undecided = names - set(_CARRIED) - set(_NOT_COPIED)
        assert not undecided, (
            f"RetrievalContext gained {sorted(undecided)}: copy it in session.engine "
            "and add it to _CARRIED, or say in _NOT_COPIED why it is not copied"
        )
        assert set(_CARRIED) | set(_NOT_COPIED) <= names

    def test_a_populated_context_comes_out_with_every_carried_field_unchanged(self):
        context = RetrievalContext(**_CARRIED)

        rebuilt = _rebuilt(context)

        for name in _CARRIED:
            assert getattr(rebuilt, name) == getattr(context, name), name

    def test_the_metric_tables_survive_and_stay_out_of_the_join_tables(self):
        context = RetrievalContext(entities=["Customer"], metric_tables=["Invoice"], join_tables=["Bridge"])

        rebuilt = _rebuilt(context)

        assert rebuilt.metric_tables == ["Invoice"]
        assert rebuilt.join_tables == ["Bridge"]
        assert rebuilt.selected_tables == ["Customer", "Invoice", "Bridge"]
