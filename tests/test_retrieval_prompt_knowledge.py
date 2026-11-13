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
