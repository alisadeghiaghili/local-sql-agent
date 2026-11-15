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


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

_RULE = "=" * 50


def _section(prompt: str, title: str) -> str:
    """The body of the *title* section of a prompt."""
    after = prompt.split(f"{title}\n{_RULE}\n\n", 1)[1]
    return after.split(f"\n\n{_RULE}", 1)[0]


def _table_missing_from(question: str) -> str:
    """A table the retrievers do not select for *question* and whose bare name is unambiguous."""
    from retrieval.context_retriever import ContextRetriever
    from retrieval.table_keys import resolve_table_ref
    from schema_data.registry import bare_table_name

    chosen = set(ContextRetriever.retrieve(question).selected_tables)
    for table in TABLE_COLUMNS:
        if table not in chosen and resolve_table_ref(bare_table_name(table)) == (table,):
            return table
    pytest.skip("every table of the loaded schema is retrieved for the question")


def _yield_metric(table: str) -> dict:
    """A metric named ``zyxwv yield`` whose expression uses *table*."""
    from schema_data.registry import bare_table_name

    return {
        "zyx_yield": {
            "aliases": ["zyxwv yield"],
            "expression": f"SUM({bare_table_name(table)}.Amount)",
        },
    }


@pytest.fixture()
def configure_metrics(monkeypatch):
    """Replace the configured metrics, for the prompt renderer and the retriever alike."""
    import prompt_engine.static_prefix as static_prefix
    import retrieval.metric_retriever as metric_retriever

    def apply(metrics: dict) -> None:
        monkeypatch.setattr(static_prefix, "METRICS", metrics)
        monkeypatch.setattr(metric_retriever, "METRICS", metrics)
        static_prefix.build_static_prefix.cache_clear()

    yield apply
    monkeypatch.undo()
    static_prefix.build_static_prefix.cache_clear()


@pytest.fixture()
def fixed_selection(monkeypatch):
    """Make the table retrievers return a chosen selection, and pruning keep it as is."""
    import retrieval.context_retriever as module

    def apply(entities: list[str]) -> None:
        monkeypatch.setattr(module.EntityRetriever, "retrieve", staticmethod(lambda q: list(entities)))
        monkeypatch.setattr(module.FactRetriever, "retrieve", staticmethod(lambda q: []))
        monkeypatch.setattr(module, "prune_selection", lambda q, e, f: (e, f))

    return apply


_TWO_METRICS = {
    "total_value": {"aliases": ["total value", "sum of value"], "expression": "SUM(T.Amount)"},
    "average_price": {"aliases": ["average price"], "expression": "AVG(T.Price)"},
}


class TestEveryMetricIsInTheRetrievalPrompt:
    def test_each_configured_metric_line_appears(self, configure_metrics):
        configure_metrics(_TWO_METRICS)

        prompt = _retrieval_prompt("how many customers?", RetrievalContext())

        assert "total_value: SUM(T.Amount) (aliases: total value, sum of value)" in prompt
        assert "average_price: AVG(T.Price) (aliases: average price)" in prompt

    def test_the_section_sits_between_business_rules_and_the_schema(self, configure_metrics):
        configure_metrics(_TWO_METRICS)

        prompt = _retrieval_prompt("q", RetrievalContext(business_rules=["Only settled contracts."]))

        assert (
            prompt.index("BUSINESS RULES")
            < prompt.index("Only settled contracts.")
            < prompt.index("METRICS")
            < prompt.index("total_value:")
            < prompt.index("DATABASE SCHEMA")
        )

    def test_the_metrics_do_not_depend_on_what_the_question_mentions(self, configure_metrics):
        configure_metrics(_TWO_METRICS)

        about_one = _retrieval_prompt("total value by month", RetrievalContext())
        about_none = _retrieval_prompt("how many customers?", RetrievalContext())

        assert _section(about_one, "METRICS") == _section(about_none, "METRICS")
        assert "average_price:" in _section(about_one, "METRICS")

    def test_the_section_is_the_one_the_static_prefix_renders(self, configure_metrics):
        from prompt_engine.static_prefix import build_static_prefix

        configure_metrics(_TWO_METRICS)

        retrieval = _retrieval_prompt("q", RetrievalContext())
        static = build_static_prefix(_SYSTEM_PROMPT)

        assert _section(retrieval, "METRICS") == _section(static, "METRICS")

    def test_no_configured_metric_renders_an_empty_section(self, configure_metrics):
        configure_metrics({})

        prompt = _retrieval_prompt("q", RetrievalContext())

        assert _section(prompt, "METRICS") == ""
        assert prompt.index("METRICS") < prompt.index("DATABASE SCHEMA")


class TestMentionedMetricBringsItsTables:
    _QUESTION = "show the zyxwv yield per month"

    def test_the_schema_block_covers_a_table_only_the_expression_names(self, configure_metrics):
        from retrieval.context_retriever import ContextRetriever

        configure_metrics({})
        table = _table_missing_from(self._QUESTION)
        configure_metrics(_yield_metric(table))

        context = ContextRetriever.retrieve(self._QUESTION)
        prompt = _retrieval_prompt(self._QUESTION, context)

        assert table in context.selected_tables
        assert table in context.metric_tables
        assert table not in context.join_tables
        assert table not in context.entities + context.facts
        assert f"Table: {table}" in _schema_block(prompt)

    def test_a_question_that_does_not_mention_the_metric_is_unaffected(self, configure_metrics):
        from retrieval.context_retriever import ContextRetriever

        configure_metrics({})
        table = _table_missing_from(self._QUESTION)
        before = ContextRetriever.retrieve("how many customers?")
        configure_metrics(_yield_metric(table))

        after = ContextRetriever.retrieve("how many customers?")

        assert after.selected_tables == before.selected_tables
        assert after.metric_tables == before.metric_tables == []
        assert after.join_tables == before.join_tables

    def test_the_tables_are_added_after_pruning_and_before_the_join_expansion(
        self, configure_metrics, fixed_selection, monkeypatch,
    ):
        import retrieval.context_retriever as module

        first, second = list(TABLE_COLUMNS)[:2]
        configure_metrics(_yield_metric(second))
        fixed_selection([first])
        seen: list[list[str]] = []

        def record(selected, **kwargs):
            seen.append(list(selected))
            return []

        monkeypatch.setattr(module, "expand_join_paths", record)

        context = module.ContextRetriever.retrieve(self._QUESTION)

        assert seen == [[first, second]]
        assert context.entities == [first]
        assert context.metric_tables == [second]
        assert context.join_tables == []
        assert context.selected_tables == [first, second]

    def test_a_metric_table_the_question_already_named_is_not_repeated(
        self, configure_metrics, fixed_selection, monkeypatch,
    ):
        import retrieval.context_retriever as module

        first = next(iter(TABLE_COLUMNS))
        configure_metrics(_yield_metric(first))
        fixed_selection([first])
        monkeypatch.setattr(module, "expand_join_paths", lambda selected, **kwargs: [])

        context = module.ContextRetriever.retrieve(self._QUESTION)

        assert context.selected_tables == [first]
        assert context.metric_tables == []
        assert context.join_tables == []

    def test_the_join_lines_cover_the_metric_table(self, configure_metrics, fixed_selection):
        import retrieval.context_retriever as module

        names = list(TABLE_COLUMNS)
        pairs = [
            (a, b) for a in names for b in names
            if a != b and SchemaRegistry.get_relationships([a, b])
        ]
        if not pairs:
            pytest.skip("the loaded schema declares no relationship")
        first, second = pairs[0]
        configure_metrics(_yield_metric(second))
        fixed_selection([first])

        context = module.ContextRetriever.retrieve(self._QUESTION)
        prompt = _retrieval_prompt(self._QUESTION, context)

        for clause in SchemaRegistry.get_relationships([first, second]):
            assert clause in context.relationships
            assert clause in prompt
