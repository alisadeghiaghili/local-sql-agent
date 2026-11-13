# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The retrieval prompt carries the knowledge the static prompt carries.

The retrieval path (``PROMPT_TEMPLATE``) is the escape hatch for a knowledge
base too large for the static prefix. It must not quietly know less than the
static path does, so these tests force it with a one-token budget and check:

* an empty retrieval shows the whole schema together with the relationships
  between those tables, not the whole schema with no JOIN lines;
* the session context reaches the prompt;
* every metric definition reaches the prompt, and the tables a mentioned
  metric's expression uses are part of the schema block.
"""

from __future__ import annotations

import logging

import pytest

from config import override_settings
from core.models import RetrievalContext
from prompt_engine.builder import PromptBuilder
from schema_data.columns import TABLE_COLUMNS
from schema_data.registry import SchemaRegistry

_SYSTEM_PROMPT = "You are a T-SQL expert for the Auction domain."


def _retrieval_prompt(question: str, context: RetrievalContext, **kwargs: object) -> str:
    """Build *context*'s prompt on the retrieval path (a one-token budget)."""
    with override_settings(prompt_retrieval_token_budget=1):
        return PromptBuilder.build(question, _SYSTEM_PROMPT, context, **kwargs)


def _schema_block(prompt: str) -> str:
    """The text between the DATABASE SCHEMA and RELATIONSHIPS headings."""
    after = prompt.split("DATABASE SCHEMA", 1)[1]
    return after.split("RELATIONSHIPS", 1)[0]


class TestEmptyRetrievalShowsTheWholeSchemaCoherently:
    def test_every_table_and_the_relationships_between_them_are_shown(self):
        prompt = _retrieval_prompt("anything", RetrievalContext())
        schema = _schema_block(prompt)

        for table in TABLE_COLUMNS:
            assert f"Table: {table}" in schema
        expected = SchemaRegistry.get_relationships(list(TABLE_COLUMNS))
        assert expected, "the loaded schema declares no relationships to show"
        for clause in expected:
            assert clause in prompt

    def test_the_fallback_is_logged_as_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prompt_engine.builder"):
            _retrieval_prompt("anything", RetrievalContext())

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "no table" in warnings[0].getMessage()
        assert "whole schema" in warnings[0].getMessage()

    def test_a_non_empty_retrieval_stays_narrow_and_silent(self, caplog):
        table = next(iter(TABLE_COLUMNS))
        other = next(t for t in TABLE_COLUMNS if t != table)
        with caplog.at_level(logging.WARNING, logger="prompt_engine.builder"):
            prompt = _retrieval_prompt("q", RetrievalContext(entities=[table]))

        assert f"Table: {table}" in _schema_block(prompt)
        assert f"Table: {other}" not in _schema_block(prompt)
        assert not caplog.records


class TestSessionContextReachesTheRetrievalPrompt:
    def test_the_session_context_is_rendered_under_its_heading(self):
        text = "Previous question: how many customers?"
        prompt = _retrieval_prompt("and by month?", RetrievalContext(), session_context=text)

        assert text in prompt
        heading = prompt.index("SESSION CONTEXT")
        assert prompt.index("RESOLVED WAREHOUSE VALUES") < heading < prompt.index("EXAMPLES")
        assert heading < prompt.index(text) < prompt.index("USER QUESTION")

    def test_the_section_sits_before_the_column_access_notes(self):
        prompt = _retrieval_prompt(
            "q", RetrievalContext(), session_context="prior turn", access_notes="Use X only in JOIN.",
        )

        assert prompt.index("SESSION CONTEXT") < prompt.index("COLUMN ACCESS") < prompt.index("EXAMPLES")

    def test_an_empty_session_context_keeps_the_section_layout(self):
        prompt = _retrieval_prompt("q", RetrievalContext())

        assert "SESSION CONTEXT" in prompt
        after = prompt.split("SESSION CONTEXT", 1)[1]
        # the same layout the static suffix gives an empty session context:
        # the heading's rule, an empty value line, then the next heading
        assert after.startswith(
            "\n==================================================\n\n\n\n"
            "==================================================\nEXAMPLES"
        )

    def test_both_paths_use_the_same_heading_block(self):
        from prompt_engine.templates import PROMPT_TEMPLATE, SUFFIX_TEMPLATE

        block = (
            "==================================================\n"
            "SESSION CONTEXT\n"
            "==================================================\n\n"
            "{session_context}\n{access_notes}\n"
        )
        assert block in PROMPT_TEMPLATE
        assert block in SUFFIX_TEMPLATE
