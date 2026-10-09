# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A prompt per data source: what it holds, that it is stable, and that a
deployment with one source is left exactly as it was.

The sources (``sales``, ``inventory``, ``archive``) come from
``tests/_source_fixtures.configured_sources``, which spreads whichever
schema is loaded over them; every expectation below is derived from that
spread, not from table names.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import config as cfg
from config import override_settings
from core.models import RetrievalContext
from knowledge.business_rules import BUSINESS_RULES
from knowledge.examples import EXAMPLES
from llm.router import build_prompt_segments
from prompt_engine import static_prefix as sp_module
from prompt_engine.builder import PromptBuilder
from prompt_engine.source_scope import examples_for_source, scoped_source
from prompt_engine.static_prefix import (
    build_static_prefix,
    estimate_tokens,
    log_prompt_paths,
    should_use_static_prefix,
    static_prefix_token_estimate,
)
from prompt_engine.templates import PROMPT_TEMPLATE, STATIC_PREFIX_TEMPLATE
from schema_data.columns import TABLE_COLUMNS
from schema_data.registry import (
    SchemaRegistry,
    get_relationships_map,
    get_table_columns,
    get_table_schema_qualifiers,
    table_reference_sql,
)
from tests._source_fixtures import SOURCES, configured_sources, tables_of

SYSTEM_PROMPT = "You are a T-SQL expert."

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _table_lines(text: str) -> list[str]:
    return [line[len("Table: "):] for line in text.splitlines() if line.startswith("Table: ")]


def _section(prefix: str, name: str) -> str:
    """The text of one ``===``-fenced section of a static prefix."""
    marker = f"{'=' * 50}\n{name}\n{'=' * 50}\n"
    start = prefix.index(marker) + len(marker)
    end = prefix.find("=" * 50, start)
    return prefix[start:] if end == -1 else prefix[start:end]


def _expected_joins(tables: list[str]) -> list[str]:
    """The join text of every relationship whose two ends are both in *tables*."""
    inside = set(tables)
    joins = []
    for key, join_sql in get_relationships_map().items():
        left, right = key.split(" -> ")
        if left in inside and right in inside:
            joins.append(join_sql)
    return joins


def _example_for(table: str, question: str) -> dict:
    reference = table_reference_sql(table, get_table_schema_qualifiers().get(table, ""))
    return {"tags": ["t"], "question": question, "sql": f"SELECT COUNT(*) FROM {reference}"}


def _using_example_config() -> bool:
    return Path(cfg.settings.project_config_dir).resolve() == (
        _REPO_ROOT / "project_config.example"
    ).resolve()


# ---------------------------------------------------------------------------
# One data source: nothing changes
# ---------------------------------------------------------------------------

#: Captured before per-source prompts existed, by building the prompts the
#: way the code then did (``project_config.example`` and the system prompt
#: above), and kept as hashes so the test cannot drift with the code it
#: guards. See ``TestSingleSourceIsByteForByteUnchanged``.
_GOLDEN = {
    "prefix_sha": "077df029a140b8e4a6a061cebf5221ab044d4c3b4670a69141950adb9a7f4fd8",
    "prefix_len": 3805,
    "estimate": 951,
    "prefix_version": "077df029a140",
    "static_full_sha": "5423c8f6933dab7fcaee280b33a5f3ff15ea91134fd7784d0a63fd3d6d27ca84",
    "seg_question_sha": "1d77a0f1ec95c3df16f07159b30ca9c95988321626d7e89b3458187137fab0a7",
    "retrieval_sha": "04544cc6fa6afa8bfa61d92f0e42b0ab20d085aaafb73f202cf857442626c5ea",
    "retrieval_empty_sha": "8b53521f6a25883b9364ca0f543e0a2e7a5d21d01f20ec2a4d3afb40a6b3539b",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _legacy_static_prefix(system_prompt: str) -> str:
    """The static prefix exactly as it was assembled before sources: every
    table, every relationship, every example, in the one template."""
    return STATIC_PREFIX_TEMPLATE.format(
        system_prompt=system_prompt,
        business_rules="\n\n".join(BUSINESS_RULES.values()),
        metrics=sp_module._all_metrics_text(),
        schema=SchemaRegistry.build_schema_context(None),
        relationships="\n".join(SchemaRegistry.get_relationships(list(TABLE_COLUMNS.keys()))),
        examples="\n\n".join(
            f"Question:\n{ex['question']}\n\nSQL:\n{ex['sql']}" for ex in EXAMPLES
        ),
    )


class TestSingleSourceIsByteForByteUnchanged:
    """With one source, a ``source`` argument changes nothing: the prefix,
    the segments, the estimate and the budget decision are those of the
    code before sources existed."""

    def test_prefix_equals_the_legacy_assembly(self):
        build_static_prefix.cache_clear()
        assert build_static_prefix(SYSTEM_PROMPT) == _legacy_static_prefix(SYSTEM_PROMPT)

    @pytest.mark.parametrize("source", [None, "default", "sales", "anything-at-all"])
    def test_a_source_name_is_ignored(self, source):
        assert build_static_prefix(SYSTEM_PROMPT, source) == _legacy_static_prefix(SYSTEM_PROMPT)
        assert static_prefix_token_estimate(SYSTEM_PROMPT, source) == static_prefix_token_estimate(SYSTEM_PROMPT)
        assert should_use_static_prefix(SYSTEM_PROMPT, source) is should_use_static_prefix(SYSTEM_PROMPT)

    def test_segments_are_identical_with_and_without_a_source(self):
        ctx = RetrievalContext(entities=["Customer"], filters={"PersianYear": 1402})
        plain = build_prompt_segments("how many?", SYSTEM_PROMPT, ctx, session_context="turn 1")
        named = build_prompt_segments(
            "how many?", SYSTEM_PROMPT, ctx, session_context="turn 1", source="sales",
        )
        assert named == plain

    def test_the_retrieval_path_is_identical_with_and_without_a_source(self):
        ctx = RetrievalContext(entities=["Customer"], filters={"PersianYear": 1402})
        with override_settings(prompt_retrieval_token_budget=1):
            plain = build_prompt_segments("how many?", SYSTEM_PROMPT, ctx)
            named = build_prompt_segments("how many?", SYSTEM_PROMPT, ctx, source="sales")
        assert named == plain
        assert plain.static_prefix == ""

    def test_the_retrieval_prompt_equals_the_legacy_assembly(self):
        ctx = RetrievalContext(
            entities=["Customer"], facts=["Order"],
            relationships=["JOIN [sales].[Customer] c ON o.CustomerID = c.ID"],
            business_rules=["A rule."],
            examples=[{"question": "q", "sql": "SELECT 1", "tags": []}],
            filters={"PersianYear": 1402},
        )
        legacy = PROMPT_TEMPLATE.format(
            system_prompt=SYSTEM_PROMPT,
            business_rules="A rule.",
            schema=SchemaRegistry.build_schema_context(ctx.selected_tables),
            relationships="\n".join(ctx.relationships),
            filters="PersianYear: 1402",
            resolved_values="",
            access_notes="",
            examples="Question:\nq\n\nSQL:\nSELECT 1",
            question="how many?",
        )
        with override_settings(prompt_retrieval_token_budget=1):
            built = PromptBuilder.build("how many?", SYSTEM_PROMPT, ctx)
            assert PromptBuilder.build("how many?", SYSTEM_PROMPT, ctx, source="sales") == built
        assert built == legacy

    def test_the_schema_block_has_no_source_heading(self):
        assert "Data source:" not in SchemaRegistry.build_schema_context(None, source="sales")
        assert SchemaRegistry.build_schema_context(None, source="sales") == (
            SchemaRegistry.build_schema_context(None)
        )

    def test_scoped_source_is_none_with_one_source(self):
        assert scoped_source("sales") is None
        assert scoped_source(None) is None

    def test_the_examples_are_not_filtered(self):
        examples = [{"question": "q", "sql": "SELECT * FROM nowhere"}]
        assert examples_for_source(examples, "sales") == examples

    def test_no_prompt_path_is_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger=sp_module.logger.name):
            log_prompt_paths(SYSTEM_PROMPT)
        assert caplog.records == []

    @pytest.mark.skipif(not _using_example_config(), reason="golden values are for project_config.example")
    def test_golden_prompts_captured_before_this_feature_are_unchanged(self):
        build_static_prefix.cache_clear()
        prefix = build_static_prefix(SYSTEM_PROMPT)
        assert (_sha(prefix), len(prefix)) == (_GOLDEN["prefix_sha"], _GOLDEN["prefix_len"])
        assert static_prefix_token_estimate(SYSTEM_PROMPT) == _GOLDEN["estimate"]
        assert sp_module.prefix_version(SYSTEM_PROMPT) == _GOLDEN["prefix_version"]

        ctx = RetrievalContext(entities=["Customer"], facts=["Contract"], filters={"PersianYear": 1402})
        segments = build_prompt_segments("how many?", SYSTEM_PROMPT, ctx, session_context="turn 1")
        assert _sha(segments.static_prefix) == _GOLDEN["prefix_sha"]
        assert _sha(segments.question) == _GOLDEN["seg_question_sha"]
        assert _sha(segments.flatten()) == _GOLDEN["static_full_sha"]

        with override_settings(prompt_retrieval_token_budget=1):
            retrieved = build_prompt_segments("how many?", SYSTEM_PROMPT, ctx)
            empty = build_prompt_segments("q", SYSTEM_PROMPT, RetrievalContext())
        assert _sha(retrieved.flatten()) == _GOLDEN["retrieval_sha"]
        assert _sha(empty.flatten()) == _GOLDEN["retrieval_empty_sha"]

    def test_the_cache_key_prefix_version_is_the_whole_schema_hash(self):
        # api.query_cache keys on prefix_version(system_prompt); it must not
        # move, and it never takes a source.
        expected = hashlib.sha256(_legacy_static_prefix(SYSTEM_PROMPT).encode("utf-8")).hexdigest()[:12]
        assert sp_module.prefix_version(SYSTEM_PROMPT) == expected


# ---------------------------------------------------------------------------
# Several sources: the prefix of one source
# ---------------------------------------------------------------------------


@pytest.fixture()
def sources():
    """Several data sources over the loaded schema; yields ``{table: sources}``."""
    with configured_sources(descriptions={"inventory": "Inventory warehouse"}) as sets:
        yield sets


class TestPrefixHoldsOnlyOneSource:
    def test_exactly_the_tables_of_the_source_appear(self, sources):
        for source in SOURCES:
            prefix = build_static_prefix(SYSTEM_PROMPT, source)
            assert _table_lines(prefix) == tables_of(source, sources), source

    def test_a_shared_table_appears_in_each_of_its_sources_and_in_no_other(self, sources):
        shared = next(t for t, s in sources.items() if len(s) > 1)
        for source in SOURCES:
            present = shared in _table_lines(build_static_prefix(SYSTEM_PROMPT, source))
            assert present is (source in sources[shared])

    def test_tables_are_not_labelled_one_by_one_and_there_is_no_cross_source_rule(self, sources):
        for source in SOURCES:
            schema = _section(build_static_prefix(SYSTEM_PROMPT, source), "DATABASE SCHEMA")
            assert schema.count("Data source:") == 1
            assert "must come from the same data source" not in schema

    def test_the_source_is_announced_once_near_the_top_of_the_schema_block(self, sources):
        inventory = _section(build_static_prefix(SYSTEM_PROMPT, "inventory"), "DATABASE SCHEMA")
        assert inventory.lstrip().startswith("Data source: inventory — Inventory warehouse\n\nTable: ")
        sales = _section(build_static_prefix(SYSTEM_PROMPT, "sales"), "DATABASE SCHEMA")
        assert sales.lstrip().startswith("Data source: sales\n\nTable: ")

    def test_only_relationships_with_both_ends_in_the_source_remain(self, sources):
        for source in SOURCES:
            prefix = build_static_prefix(SYSTEM_PROMPT, source)
            lines = [row for row in _section(prefix, "RELATIONSHIPS").splitlines() if row.strip()]
            assert lines == _expected_joins(tables_of(source, sources)), source

    def test_business_rules_and_metrics_are_not_table_scoped(self, sources):
        whole = build_static_prefix(SYSTEM_PROMPT)
        for source in SOURCES:
            prefix = build_static_prefix(SYSTEM_PROMPT, source)
            assert _section(prefix, "BUSINESS RULES") == _section(whole, "BUSINESS RULES")
            assert _section(prefix, "METRICS") == _section(whole, "METRICS")
            assert prefix.startswith(whole[: whole.index("DATABASE SCHEMA")])

    def test_the_sources_have_different_prefixes(self, sources):
        prefixes = {build_static_prefix(SYSTEM_PROMPT, s) for s in SOURCES}
        assert len(prefixes) == len(SOURCES)

    def test_each_prefix_is_smaller_than_the_whole_schema_prefix(self, sources):
        whole = static_prefix_token_estimate(SYSTEM_PROMPT)
        for source in SOURCES:
            assert static_prefix_token_estimate(SYSTEM_PROMPT, source) < whole

    def test_an_unknown_source_is_refused_not_turned_into_every_table(self, sources):
        with pytest.raises(ValueError, match="unknown data source 'elsewhere'"):
            build_static_prefix(SYSTEM_PROMPT, "elsewhere")
        with pytest.raises(ValueError, match="unknown data source"):
            SchemaRegistry.tables_for_source("elsewhere")

    def test_the_whole_schema_prefix_is_still_available_without_a_source(self, sources):
        # What the query cache's prefix_version and the evaluation harness use.
        whole = build_static_prefix(SYSTEM_PROMPT)
        assert sorted(_table_lines(whole)) == sorted(get_table_columns())
        assert "Data source: " in whole


class TestPrefixIsByteStablePerSource:
    def test_the_same_source_returns_the_same_cached_string(self, sources):
        assert build_static_prefix(SYSTEM_PROMPT, "sales") is build_static_prefix(SYSTEM_PROMPT, "sales")

    def test_requests_for_one_source_share_a_byte_identical_prefix(self, sources):
        prefixes = []
        for question in ("first question", "نمونه پرسش", "a third one"):
            segments = build_prompt_segments(
                question, SYSTEM_PROMPT, RetrievalContext(filters={"PersianYear": 1402}),
                session_context="turn", source="inventory",
            )
            prefixes.append(segments.static_prefix)
            assert question in segments.question
        assert len(set(prefixes)) == 1
        assert prefixes[0] == build_static_prefix(SYSTEM_PROMPT, "inventory")
        assert prefixes[0].encode("utf-8") == build_static_prefix(SYSTEM_PROMPT, "inventory").encode("utf-8")

    def test_a_source_has_its_own_cache_entry(self, sources):
        sales = build_static_prefix(SYSTEM_PROMPT, "sales")
        inventory = build_static_prefix(SYSTEM_PROMPT, "inventory")
        assert sales is not inventory
        assert build_static_prefix(SYSTEM_PROMPT, "sales") is sales
        assert build_static_prefix(SYSTEM_PROMPT, "inventory") is inventory

    def test_rebuilding_after_a_cache_clear_gives_the_same_bytes(self, sources):
        before = build_static_prefix(SYSTEM_PROMPT, "archive")
        build_static_prefix.cache_clear()
        assert build_static_prefix(SYSTEM_PROMPT, "archive") == before

    def test_the_segments_split_exactly_at_the_prefix(self, sources):
        segments = build_prompt_segments("q", SYSTEM_PROMPT, RetrievalContext(), source="sales")
        assert segments.static_prefix == build_static_prefix(SYSTEM_PROMPT, "sales")
        assert segments.flatten().startswith(segments.static_prefix)


class TestExamplesAreFilteredBySource:
    @pytest.fixture()
    def examples(self, sources):
        """One example per source's table, a shared-table one, and two whose
        tables cannot be determined."""
        per_source = {}
        for source in SOURCES:
            table = next(t for t in tables_of(source, sources) if len(sources[t]) == 1)
            per_source[source] = _example_for(table, f"question about {source}")
        shared = next(t for t, s in sources.items() if len(s) > 1)
        crafted = [
            *per_source.values(),
            _example_for(shared, "question about the shared table"),
            {"tags": [], "question": "no table at all", "sql": "SELECT 1"},
            {"tags": [], "question": "unparsable", "sql": "this is not sql"},
        ]
        with patch.object(sp_module, "EXAMPLES", crafted):
            build_static_prefix.cache_clear()
            yield crafted
        build_static_prefix.cache_clear()

    def _questions(self, source):
        return [
            ex["question"] for ex in examples_for_source(sp_module.EXAMPLES, source)
        ]

    def test_the_fixture_examples_resolve_to_the_tables_they_name(self, examples, sources):
        from security.sql_guard import extract_touched_tables

        for example in examples[:3]:
            assert len(extract_touched_tables(example["sql"])) == 1

    def test_an_example_of_another_source_is_dropped(self, examples):
        for source in SOURCES:
            kept = self._questions(source)
            assert f"question about {source}" in kept
            for other in SOURCES:
                if other != source:
                    assert f"question about {other}" not in kept

    def test_a_shared_table_example_is_kept_for_each_of_its_sources(self, examples, sources):
        shared = next(t for t, s in sources.items() if len(s) > 1)
        for source in SOURCES:
            assert ("question about the shared table" in self._questions(source)) is (source in sources[shared])

    def test_an_example_whose_tables_cannot_be_determined_is_kept(self, examples):
        for source in SOURCES:
            kept = self._questions(source)
            assert "no table at all" in kept
            assert "unparsable" in kept

    def test_the_order_of_the_kept_examples_is_unchanged(self, examples):
        kept = self._questions("sales")
        original = [ex["question"] for ex in examples]
        assert kept == [q for q in original if q in kept]

    def test_the_prefix_carries_exactly_the_kept_examples(self, examples):
        for source in SOURCES:
            section = _section(build_static_prefix(SYSTEM_PROMPT, source), "EXAMPLES")
            for example in examples:
                assert (example["question"] in section) is (example["question"] in self._questions(source))

    def test_the_whole_schema_prefix_keeps_every_example(self, examples):
        section = _section(build_static_prefix(SYSTEM_PROMPT), "EXAMPLES")
        assert all(ex["question"] in section for ex in examples)

    def test_filtering_returns_new_dicts_and_leaves_the_input_alone(self, examples):
        before = [dict(e) for e in examples]
        kept = examples_for_source(examples, "sales")
        assert kept and all(k is not e for k in kept for e in examples)
        assert examples == before


# ---------------------------------------------------------------------------
# The path per source, and the retrieval path restricted to a source
# ---------------------------------------------------------------------------


class TestBudgetAppliesPerSource:
    def test_each_source_is_judged_on_its_own_prefix(self, sources):
        estimates = {s: static_prefix_token_estimate(SYSTEM_PROMPT, s) for s in SOURCES}
        smallest, largest = min(estimates, key=estimates.get), max(estimates, key=estimates.get)
        assert estimates[smallest] < estimates[largest]
        budget = estimates[smallest]  # fits the smallest, not the largest
        with override_settings(prompt_retrieval_token_budget=budget):
            assert should_use_static_prefix(SYSTEM_PROMPT, smallest) is True
            assert should_use_static_prefix(SYSTEM_PROMPT, largest) is False
            # The whole schema (no source) is bigger than any one source's.
            assert should_use_static_prefix(SYSTEM_PROMPT) is False

    def test_a_source_that_fits_uses_its_static_prefix_and_one_that_does_not_uses_retrieval(self, sources):
        estimates = {s: static_prefix_token_estimate(SYSTEM_PROMPT, s) for s in SOURCES}
        small, big = min(estimates, key=estimates.get), max(estimates, key=estimates.get)
        ctx = RetrievalContext(entities=tables_of(big, sources)[:1])
        with override_settings(prompt_retrieval_token_budget=estimates[small]):
            fits = build_prompt_segments("q", SYSTEM_PROMPT, ctx, source=small)
            too_big = build_prompt_segments("q", SYSTEM_PROMPT, ctx, source=big)
        assert fits.static_prefix == build_static_prefix(SYSTEM_PROMPT, small)
        assert too_big.static_prefix == ""
        assert "DETECTED FILTERS" in too_big.question

    def test_the_default_budget_is_unchanged(self):
        with patch.dict(os.environ):
            os.environ.pop("PROMPT_RETRIEVAL_TOKEN_BUDGET", None)
            assert cfg.Settings().prompt_retrieval_token_budget == 6000

    def test_a_budget_of_zero_forces_retrieval_for_every_source(self, sources):
        with override_settings(prompt_retrieval_token_budget=0):
            assert not any(should_use_static_prefix(SYSTEM_PROMPT, s) for s in SOURCES)


class TestRetrievalPathRestrictedToTheSource:
    def _prompt(self, source, ctx, **kwargs):
        with override_settings(prompt_retrieval_token_budget=1):
            return build_prompt_segments("q", SYSTEM_PROMPT, ctx, source=source, **kwargs).question

    def test_retrieved_tables_of_other_sources_are_left_out(self, sources):
        mixed = [
            t for source in SOURCES for t in tables_of(source, sources)[:1]
        ]
        ctx = RetrievalContext(entities=list(dict.fromkeys(mixed)))
        for source in SOURCES:
            shown = _table_lines(self._prompt(source, ctx))
            assert shown
            assert set(shown) <= set(tables_of(source, sources))
            assert set(shown) == set(mixed) & set(tables_of(source, sources))

    def test_no_tables_found_shows_every_table_of_the_source_and_no_others(self, sources):
        for source in SOURCES:
            prompt = self._prompt(source, RetrievalContext())
            assert _table_lines(prompt) == tables_of(source, sources)

    def test_tables_found_only_in_other_sources_also_fall_back_to_the_whole_source(self, sources):
        other = next(s for s in SOURCES if s != "sales")
        foreign = [t for t in tables_of(other, sources) if "sales" not in sources[t]][:1]
        assert foreign
        prompt = self._prompt("sales", RetrievalContext(entities=foreign))
        assert _table_lines(prompt) == tables_of("sales", sources)

    def test_the_prompt_announces_the_source(self, sources):
        prompt = self._prompt("inventory", RetrievalContext())
        assert "Data source: inventory — Inventory warehouse" in prompt

    def test_relationships_are_those_between_the_tables_shown(self, sources):
        for source in SOURCES:
            tables = tables_of(source, sources)
            prompt = self._prompt(source, RetrievalContext())
            section = prompt[prompt.index("RELATIONSHIPS") :]
            for join in _expected_joins(tables):
                assert join in section
            # A relationship retrieval found across sources is not carried over.
            all_joins = list(get_relationships_map().values())
            for join in set(all_joins) - set(_expected_joins(tables)):
                assert join not in section

    def test_retrieved_examples_that_cannot_run_on_the_source_are_dropped(self, sources):
        a = next(t for t in tables_of("sales", sources) if len(sources[t]) == 1)
        b = next(t for t in tables_of("archive", sources) if len(sources[t]) == 1)
        ctx = RetrievalContext(examples=[
            {**_example_for(a, "about sales"), "tags": []},
            {**_example_for(b, "about archive"), "tags": []},
        ])
        sales = self._prompt("sales", ctx)
        assert "about sales" in sales and "about archive" not in sales

    def test_filters_session_and_resolved_values_are_carried_as_before(self, sources):
        ctx = RetrievalContext(filters={"PersianYear": 1402})
        table = tables_of("sales", sources)[0]
        prompt = self._prompt(
            "sales", ctx, session_context="turn t_1",
        )
        assert "PersianYear: 1402" in prompt
        with override_settings(prompt_retrieval_token_budget=1):
            fenced = PromptBuilder.build(
                "q", SYSTEM_PROMPT, ctx, resolved_values={f"{table}.Name": ["x"]}, source="sales",
            )
        assert f"{table}.Name: x" in fenced

    def test_the_same_source_gives_the_same_retrieval_prompt_twice(self, sources):
        ctx = RetrievalContext(entities=tables_of("sales", sources)[:2])
        assert self._prompt("sales", ctx) == self._prompt("sales", ctx)


class TestSchemaRegistryPerSource:
    def test_tables_for_source_follow_schema_order(self, sources):
        for source in SOURCES:
            assert SchemaRegistry.tables_for_source(source) == tables_of(source, sources)

    def test_selected_tables_are_narrowed_to_the_source(self, sources):
        everything = list(get_table_columns())
        text = SchemaRegistry.build_schema_context(everything, source="archive")
        assert _table_lines(text) == tables_of("archive", sources)

    def test_a_source_with_none_of_the_selected_tables_renders_only_the_heading(self, sources):
        foreign = [t for t in tables_of("inventory", sources) if "archive" not in sources[t]][:1]
        text = SchemaRegistry.build_schema_context(foreign, source="archive")
        assert _table_lines(text) == []
        assert text.startswith("Data source: archive")

    def test_without_a_source_every_table_is_labelled_as_before(self, sources):
        text = SchemaRegistry.build_schema_context(None)
        assert text.count("Data source: ") >= len(get_table_columns())
        assert "must come from the same data source" in text


# ---------------------------------------------------------------------------
# Logging the path per source
# ---------------------------------------------------------------------------


class TestPromptPathLog:
    def test_each_source_is_logged_once_with_its_estimate_and_path(self, sources, caplog):
        estimates = {s: static_prefix_token_estimate(SYSTEM_PROMPT, s) for s in SOURCES}
        smallest = min(estimates, key=estimates.get)
        with override_settings(prompt_retrieval_token_budget=estimates[smallest]):
            with caplog.at_level(logging.INFO, logger=sp_module.logger.name):
                log_prompt_paths(SYSTEM_PROMPT)
                log_prompt_paths(SYSTEM_PROMPT)  # once per process
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(messages) == len(SOURCES)
        for source in SOURCES:
            line = next(m for m in messages if f"data source '{source}'" in m)
            assert f"estimate {estimates[source]} tokens" in line
            assert f"PROMPT_RETRIEVAL_TOKEN_BUDGET {estimates[smallest]}" in line
            assert ("static prefix (cacheable)" in line) is (source == smallest or estimates[source] <= estimates[smallest])
            assert ("retrieval restricted" in line) is (estimates[source] > estimates[smallest])

    def test_first_use_logs_it_without_a_startup_call(self, sources, caplog):
        with caplog.at_level(logging.INFO, logger=sp_module.logger.name):
            build_prompt_segments("q", SYSTEM_PROMPT, RetrievalContext(), source="sales")
            build_prompt_segments("q", SYSTEM_PROMPT, RetrievalContext(), source="inventory")
        assert len([r for r in caplog.records if "Prompt path" in r.getMessage()]) == len(SOURCES)

    def test_the_estimator_is_the_documented_heuristic(self, sources):
        prefix = build_static_prefix(SYSTEM_PROMPT, "sales")
        assert static_prefix_token_estimate(SYSTEM_PROMPT, "sales") == estimate_tokens(prefix) == len(prefix) // 4


# ---------------------------------------------------------------------------
# The evaluation harness builds prompts the same way
# ---------------------------------------------------------------------------


class TestEvaluationHarnessUsesTheSourcePrompt:
    class _Backend:
        name = "stub"

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def generate(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "SELECT 1"

    def test_the_live_generator_shows_only_the_chosen_sources_tables(self):
        from eval.runner import make_live_generator

        with configured_sources(keywords={"inventory": ["stock level"]}) as sets:
            backend = self._Backend()
            make_live_generator(backend, SYSTEM_PROMPT)("show the stock level")
        (prompt,) = backend.prompts
        assert "Data source: inventory" in prompt
        assert set(_table_lines(prompt)) == set(tables_of("inventory", sets))

    def test_with_one_source_the_prompt_is_the_whole_schema_prompt(self):
        from eval.runner import make_live_generator

        backend = self._Backend()
        make_live_generator(backend, SYSTEM_PROMPT)("show the stock level")
        (prompt,) = backend.prompts
        assert "Data source:" not in prompt
        assert set(_table_lines(prompt)) == set(get_table_columns())
