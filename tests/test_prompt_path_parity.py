# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Which knowledge both prompt paths carry, and which one may narrow.

The prompt is built on one of two paths. The static path puts the whole
knowledge base in a cached prefix; the retrieval path shows a per-question
subset. The retrieval path exists for a knowledge base too large for the
static prefix, so it may show *fewer tables* -- but a business rule, a metric
or the session context is not scoped to a table, and losing it there is a
silent wrong answer. This module is the executable contract between the two:

* the **same sections** appear on both paths (their order differs: the static
  path puts ``EXAMPLES`` before the variable suffix, the retrieval path puts it
  after ``COLUMN ACCESS``; both orders are pinned below, so a reorder is a
  deliberate change);
* what is **not table-scoped is identical**: the ``METRICS`` section, every
  always-on business rule, every triggered rule whose keyword is in the
  question, the session context, the resolved warehouse values, the column
  access notes and the detected filters;
* what **is table-scoped may only narrow**: the schema's tables, the
  relationship lines and the examples on the retrieval path are subsets of the
  static ones;
* a metric the question names brings the tables its expression uses;
* an empty retrieval shows the whole schema, as the static path does.

A section added to one template and forgotten on the other fails here.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from config import override_settings
from core.models import RetrievalContext
from knowledge.business_rules import BUSINESS_RULES
from prompt_engine import static_prefix
from prompt_engine.builder import PromptBuilder
from prompt_engine.static_prefix import METRICS, build_static_prefix
from retrieval.context_retriever import ContextRetriever
from retrieval.metric_retriever import expression_tables
from retrieval.rule_retriever import RuleRetriever
from tests.test_retrieval_prompt_knowledge import _table_missing_from, _yield_metric

_SYSTEM_PROMPT = "You are a T-SQL expert for the Auction domain."
_RULE = "=" * 50
_HEADING = re.compile(rf"^{_RULE}\n([A-Z][A-Z ]*[A-Z])\n{_RULE}\n", re.MULTILINE)
_TABLE = re.compile(r"^Table: (.+)$", re.MULTILINE)
_EXAMPLE_QUESTION = re.compile(r"^Question:\n(.+)$", re.MULTILINE)

#: A question that selects some tables but not all, and that names no
#: trigger keyword of any rule, so only the always-on rules apply to it.
_NEUTRAL = "zzzz"
#: A question whose tables, relationships and example are a strict subset of
#: the schema's in the example configuration, and that triggers the
#: ``purchase`` and ``date`` rules.
_QUESTION = "total purchase of each customer by month"

_SESSION_CONTEXT = "Previous question: how many customers?"
_RESOLVED = {"Customer.Name": ["Acme Trading"]}
_ACCESS_NOTES = "Use Customer.NationalID only in JOIN ... ON."

#: Headings in the order the static path (cached prefix, then the variable
#: suffix) renders them.
_STATIC_ORDER = [
    "BUSINESS RULES", "METRICS", "DATABASE SCHEMA", "RELATIONSHIPS", "EXAMPLES",
    "DETECTED FILTERS", "RESOLVED WAREHOUSE VALUES", "SESSION CONTEXT",
    "COLUMN ACCESS", "USER QUESTION",
]
#: The same headings in the order the retrieval path renders them: the
#: examples follow the session notes there, because the whole prompt is one
#: per-question text with no cache boundary to protect.
_RETRIEVAL_ORDER = [
    "BUSINESS RULES", "METRICS", "DATABASE SCHEMA", "RELATIONSHIPS",
    "DETECTED FILTERS", "RESOLVED WAREHOUSE VALUES", "SESSION CONTEXT",
    "COLUMN ACCESS", "EXAMPLES", "USER QUESTION",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sections(prompt: str) -> list[tuple[str, str]]:
    """Split *prompt* into ``(heading, body)`` pairs, in prompt order.

    A heading is a line of capitals between two 50-character ``=`` rules. A
    body runs to the next heading and is stripped of surrounding whitespace,
    so the blank lines a template puts around it do not matter. Text before
    the first heading (the system prompt) belongs to no section.
    """
    found = list(_HEADING.finditer(prompt))
    return [
        (
            match.group(1),
            prompt[match.end(): found[i + 1].start() if i + 1 < len(found) else len(prompt)].strip(),
        )
        for i, match in enumerate(found)
    ]


def _headings(prompt: str) -> list[str]:
    """The section headings of *prompt*, in prompt order."""
    return [heading for heading, _ in _sections(prompt)]


def _body(prompt: str, heading: str) -> str:
    """The stripped body of the *heading* section of *prompt*."""
    bodies = [body for name, body in _sections(prompt) if name == heading]
    assert len(bodies) == 1, f"expected exactly one {heading!r} section, found {len(bodies)}"
    return bodies[0]


def _tables(prompt: str) -> set[str]:
    """The tables listed in the ``DATABASE SCHEMA`` section of *prompt*."""
    return set(_TABLE.findall(_body(prompt, "DATABASE SCHEMA")))


def _lines(prompt: str, heading: str) -> set[str]:
    """The non-empty lines of the *heading* section of *prompt*."""
    return {line for line in _body(prompt, heading).splitlines() if line}


def _example_questions(prompt: str) -> set[str]:
    """The few-shot questions listed in the ``EXAMPLES`` section of *prompt*."""
    return set(_EXAMPLE_QUESTION.findall(_body(prompt, "EXAMPLES")))


def _build(question: str, context: RetrievalContext, path: str, **kwargs: object) -> str:
    """Build the prompt on *path* (``"static"`` or ``"retrieval"``) via the budget setting."""
    budget = 10**9 if path == "static" else 1
    with override_settings(prompt_retrieval_token_budget=budget):
        return PromptBuilder.build(question, _SYSTEM_PROMPT, context, **kwargs)


def _both(question: str, context: RetrievalContext | None = None, **kwargs: object) -> tuple[str, str]:
    """The ``(static, retrieval)`` prompts for one question and one retrieved context."""
    context = ContextRetriever.retrieve(question) if context is None else context
    return _build(question, context, "static", **kwargs), _build(question, context, "retrieval", **kwargs)


def _full_context(question: str) -> RetrievalContext:
    """The retrieved context for *question*, with a detected filter added."""
    return dataclasses.replace(ContextRetriever.retrieve(question), filters={"PersianYear": 1402})


def _trigger(rule_key: str) -> str:
    """A question that triggers *rule_key*, or one that triggers nothing if it is always-on."""
    keywords = RuleRetriever.RULE_MAPPING.get(rule_key)
    return f"zzzz {keywords[0]} zzzz" if keywords else _NEUTRAL


def _position_in(text: str, snippets: list[str]) -> list[int]:
    """The index of each of *snippets* in *text*."""
    return [text.index(snippet) for snippet in snippets]


@pytest.fixture()
def configure_metrics(monkeypatch):
    """Replace the configured metrics for the prompt renderer and the retriever alike."""
    import retrieval.metric_retriever as metric_retriever

    def apply(metrics: dict) -> None:
        monkeypatch.setattr(static_prefix, "METRICS", metrics)
        monkeypatch.setattr(metric_retriever, "METRICS", metrics)
        static_prefix.build_static_prefix.cache_clear()

    yield apply
    monkeypatch.undo()
    static_prefix.build_static_prefix.cache_clear()


# ---------------------------------------------------------------------------
# The paths really are the two paths
# ---------------------------------------------------------------------------


class TestThePathsUnderComparison:
    def test_the_static_prompt_is_the_cached_prefix_followed_by_the_suffix(self):
        context = _full_context(_QUESTION)
        static, _ = _both(_QUESTION, context, session_context=_SESSION_CONTEXT)

        prefix = build_static_prefix(_SYSTEM_PROMPT)
        suffix = PromptBuilder.build_static_suffix(_QUESTION, context, session_context=_SESSION_CONTEXT)
        assert static == prefix + suffix
        assert static == PromptBuilder.build_static(
            _QUESTION, _SYSTEM_PROMPT, context, session_context=_SESSION_CONTEXT,
        )

    def test_the_retrieval_prompt_is_the_per_question_template(self):
        context = _full_context(_QUESTION)
        _, retrieval = _both(_QUESTION, context, session_context=_SESSION_CONTEXT)

        assert retrieval == PromptBuilder._build_retrieval(
            _QUESTION, _SYSTEM_PROMPT, context, session_context=_SESSION_CONTEXT,
        )


# ---------------------------------------------------------------------------
# Section headings
# ---------------------------------------------------------------------------


class TestSectionHeadings:
    def test_both_paths_have_the_same_set_of_headings(self):
        static, retrieval = _both(_QUESTION, access_notes=_ACCESS_NOTES)

        assert set(_headings(static)) == set(_headings(retrieval))

    def test_the_headings_are_each_rendered_once(self):
        for prompt in _both(_QUESTION, access_notes=_ACCESS_NOTES):
            headings = _headings(prompt)
            assert len(headings) == len(set(headings))

    def test_the_static_order_is_pinned(self):
        static, _ = _both(_QUESTION, access_notes=_ACCESS_NOTES)

        assert _headings(static) == _STATIC_ORDER

    def test_the_retrieval_order_is_pinned(self):
        # The order differs from the static one in exactly one place: EXAMPLES
        # sits after the session notes here, and in the cached prefix there.
        _, retrieval = _both(_QUESTION, access_notes=_ACCESS_NOTES)

        assert _headings(retrieval) == _RETRIEVAL_ORDER
        assert sorted(_STATIC_ORDER) == sorted(_RETRIEVAL_ORDER)

    def test_a_prompt_without_access_notes_has_no_such_section_on_either_path(self):
        for prompt in _both(_QUESTION):
            assert "COLUMN ACCESS" not in _headings(prompt)


# ---------------------------------------------------------------------------
# Identical on both paths
# ---------------------------------------------------------------------------


class TestKnowledgeThatIsNotTableScopedIsIdentical:
    @pytest.fixture()
    def prompts(self) -> tuple[str, str]:
        return _both(
            _QUESTION, _full_context(_QUESTION),
            session_context=_SESSION_CONTEXT, resolved_values=_RESOLVED, access_notes=_ACCESS_NOTES,
        )

    def test_the_metrics_section(self, prompts):
        static, retrieval = prompts

        assert METRICS, "the loaded configuration declares no metric to compare"
        assert _body(static, "METRICS") == _body(retrieval, "METRICS")

    def test_the_session_context(self, prompts):
        static, retrieval = prompts

        assert _body(static, "SESSION CONTEXT") == _body(retrieval, "SESSION CONTEXT") == _SESSION_CONTEXT

    def test_the_resolved_warehouse_values(self, prompts):
        static, retrieval = prompts

        body = _body(static, "RESOLVED WAREHOUSE VALUES")
        assert body == _body(retrieval, "RESOLVED WAREHOUSE VALUES")
        assert "Acme Trading" in body

    def test_the_column_access_notes(self, prompts):
        static, retrieval = prompts

        assert _body(static, "COLUMN ACCESS") == _body(retrieval, "COLUMN ACCESS") == _ACCESS_NOTES

    def test_the_detected_filters(self, prompts):
        static, retrieval = prompts

        body = _body(static, "DETECTED FILTERS")
        assert body == _body(retrieval, "DETECTED FILTERS")
        assert "PersianYear: 1402" in body

    def test_the_question(self, prompts):
        static, retrieval = prompts

        assert _body(static, "USER QUESTION") == _body(retrieval, "USER QUESTION")

    def test_every_always_on_rule_is_on_both_paths(self):
        always_on = [
            text.strip() for key, text in BUSINESS_RULES.items()
            if key not in RuleRetriever.RULE_MAPPING and text.strip()
        ]
        if not always_on:
            pytest.skip("the loaded configuration has no rule outside RULE_MAPPING")

        static, retrieval = _both(_NEUTRAL)

        for text in always_on:
            assert text in _body(static, "BUSINESS RULES")
            assert text in _body(retrieval, "BUSINESS RULES")

    def test_every_rule_whose_keyword_is_in_the_question_is_on_both_paths(self):
        keyword_of = {
            key: RuleRetriever.RULE_MAPPING[key][0]
            for key in BUSINESS_RULES if key in RuleRetriever.RULE_MAPPING
        }
        if not keyword_of:
            pytest.skip("the loaded configuration has no rule with a trigger keyword")
        question = " ".join(keyword_of.values())

        static, retrieval = _both(question)

        for key in keyword_of:
            text = BUSINESS_RULES[key].strip()
            assert text in _body(static, "BUSINESS RULES"), key
            assert text in _body(retrieval, "BUSINESS RULES"), key

    def test_the_rules_keep_the_configured_order_on_both_paths(self):
        keyword_of = [
            RuleRetriever.RULE_MAPPING[key][0] for key in BUSINESS_RULES if key in RuleRetriever.RULE_MAPPING
        ]
        texts = [text.strip() for text in BUSINESS_RULES.values() if text.strip()]
        static, retrieval = _both(" ".join(keyword_of) or _NEUTRAL)

        for prompt in (static, retrieval):
            positions = _position_in(_body(prompt, "BUSINESS RULES"), texts)
            assert positions == sorted(positions)

    def test_a_rule_whose_keyword_is_absent_is_the_one_thing_the_retrieval_path_drops(self):
        triggered = [key for key in BUSINESS_RULES if key in RuleRetriever.RULE_MAPPING]
        if not triggered:
            pytest.skip("the loaded configuration has no rule with a trigger keyword")

        static, retrieval = _both(_NEUTRAL)

        for key in BUSINESS_RULES:
            text = BUSINESS_RULES[key].strip()
            assert text in _body(static, "BUSINESS RULES")
            on_retrieval = text in _body(retrieval, "BUSINESS RULES")
            assert on_retrieval is (key not in RuleRetriever.RULE_MAPPING), key


# ---------------------------------------------------------------------------
# Every configured entry
# ---------------------------------------------------------------------------


class TestEveryConfiguredEntry:
    @pytest.mark.parametrize("key", list(METRICS))
    def test_a_metric_is_on_both_paths_and_brings_its_tables(self, key):
        spec = METRICS[key]
        aliases = spec.get("aliases") or []
        line = f"{key}: {spec.get('expression', '')}"
        if aliases:
            line += f" (aliases: {', '.join(aliases)})"
        question = f"show the {(aliases or [key])[0]} per month"

        static, retrieval = _both(question)

        assert line in _lines(static, "METRICS")
        assert line in _lines(retrieval, "METRICS")
        # The expression's tables are in the static schema by construction
        # and must be in the retrieval one once the question names the metric.
        for table in expression_tables(spec.get("expression", "")):
            assert table in _tables(static)
            assert table in _tables(retrieval)

    @pytest.mark.parametrize("key", list(BUSINESS_RULES))
    def test_a_rule_is_on_both_paths_when_the_question_triggers_it(self, key):
        text = BUSINESS_RULES[key].strip()
        if not text:
            pytest.skip("a blank rule is rendered on neither path")

        static, retrieval = _both(_trigger(key))

        assert text in _body(static, "BUSINESS RULES")
        assert text in _body(retrieval, "BUSINESS RULES")

    @pytest.mark.parametrize("key", [k for k in BUSINESS_RULES if k not in RuleRetriever.RULE_MAPPING])
    def test_an_always_on_rule_is_on_the_retrieval_path_for_any_question(self, key):
        text = BUSINESS_RULES[key].strip()
        if not text:
            pytest.skip("a blank rule is rendered on neither path")

        _, retrieval = _both(_NEUTRAL)

        assert text in _body(retrieval, "BUSINESS RULES")


# ---------------------------------------------------------------------------
# May differ, in one direction
# ---------------------------------------------------------------------------


class TestTableScopedKnowledgeOnlyNarrows:
    @pytest.fixture()
    def prompts(self) -> tuple[str, str]:
        return _both(_QUESTION)

    def test_the_retrieval_schema_is_a_strict_subset_of_the_static_one(self, prompts):
        static, retrieval = prompts

        assert _tables(retrieval) < _tables(static)
        assert _tables(retrieval)

    def test_the_retrieval_relationships_are_a_subset_of_the_static_ones(self, prompts):
        static, retrieval = prompts

        assert _lines(retrieval, "RELATIONSHIPS") <= _lines(static, "RELATIONSHIPS")

    def test_the_retrieval_examples_are_a_subset_of_the_static_ones(self, prompts):
        static, retrieval = prompts

        assert _example_questions(retrieval) <= _example_questions(static)

    def test_the_static_prompt_lists_every_table_whatever_was_retrieved(self, prompts):
        from schema_data.columns import TABLE_COLUMNS

        static, _ = prompts

        assert _tables(static) == set(TABLE_COLUMNS)


# ---------------------------------------------------------------------------
# Metric dependency
# ---------------------------------------------------------------------------


class TestAMentionedMetricBringsItsTable:
    _QUESTION = "show the zyxwv yield per month"

    def test_the_table_is_in_the_retrieval_schema_and_in_the_static_one(self, configure_metrics):
        configure_metrics({})
        table = _table_missing_from(self._QUESTION)
        configure_metrics(_yield_metric(table))

        static, retrieval = _both(self._QUESTION)

        assert table in _tables(static)
        assert table in _tables(retrieval)
        assert _body(static, "METRICS") == _body(retrieval, "METRICS")

    def test_a_question_that_does_not_name_the_metric_gets_the_same_schema(self, configure_metrics):
        question = "how many customers?"
        configure_metrics({})
        table = _table_missing_from(self._QUESTION)
        _, before = _both(question)
        configure_metrics(_yield_metric(table))

        _, after = _both(question)

        assert _tables(after) == _tables(before)


# ---------------------------------------------------------------------------
# Empty retrieval
# ---------------------------------------------------------------------------


class TestAnEmptyRetrievalShowsWhatTheStaticPathShows:
    def test_the_schema_and_the_relationships_are_the_static_ones(self):
        static, retrieval = _both("anything", RetrievalContext())

        assert _body(retrieval, "DATABASE SCHEMA") == _body(static, "DATABASE SCHEMA")
        assert _body(retrieval, "RELATIONSHIPS") == _body(static, "RELATIONSHIPS")
        assert _body(static, "RELATIONSHIPS"), "the loaded schema declares no relationship to compare"
