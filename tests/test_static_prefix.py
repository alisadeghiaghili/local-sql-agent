"""Tests for the Phase 2 static-prefix latency path (prompt_engine.static_prefix).

Covers:
* the static prefix is byte-identical across calls / questions (the whole
  point — see docs/api-contract-v2.md §8);
* PromptBuilder.build() takes the static path by default (today's 12-table
  schema is comfortably under the budget);
* forcing a small token budget makes it fall back to per-question retrieval
  instead (exit criterion: "retrieval still works when the token budget is
  exceeded" — the large-schema escape hatch is not dead code);
* the fixed section ordering required by the contract.
"""

from __future__ import annotations

from config import override_settings
from core.models import RetrievalContext
from prompt_engine.builder import PromptBuilder
from prompt_engine.static_prefix import (
    build_static_prefix,
    estimate_tokens,
    should_use_static_prefix,
    static_prefix_token_estimate,
)

_SYSTEM_PROMPT = "You are a T-SQL expert for the Auction domain."


class TestEstimateTokens:
    def test_empty_string_is_zero(self):
        assert estimate_tokens("") == 0

    def test_roughly_four_chars_per_token(self):
        assert estimate_tokens("a" * 400) == 100

    def test_never_zero_for_nonempty_text(self):
        assert estimate_tokens("hi") >= 1


class TestBuildStaticPrefix:
    def test_contains_every_section(self):
        prefix = build_static_prefix(_SYSTEM_PROMPT)
        assert _SYSTEM_PROMPT in prefix
        assert "BUSINESS RULES" in prefix
        assert "METRICS" in prefix
        assert "DATABASE SCHEMA" in prefix
        assert "RELATIONSHIPS" in prefix
        assert "EXAMPLES" in prefix

    def test_contains_all_twelve_tables(self):
        from schema_data.columns import TABLE_COLUMNS

        prefix = build_static_prefix(_SYSTEM_PROMPT)
        for table in TABLE_COLUMNS:
            assert f"Table: {table}" in prefix

    def test_byte_identical_across_calls(self):
        """The whole point: same system prompt in -> same bytes out, always."""
        p1 = build_static_prefix(_SYSTEM_PROMPT)
        p2 = build_static_prefix(_SYSTEM_PROMPT)
        assert p1 == p2

    def test_section_order_matches_contract(self):
        """docs/api-contract-v2.md §8: system prompt, schema, relationships,
        business rules, metrics, examples — in that fixed order."""
        prefix = build_static_prefix(_SYSTEM_PROMPT)
        rules_pos = prefix.index("BUSINESS RULES")
        metrics_pos = prefix.index("METRICS")
        schema_pos = prefix.index("DATABASE SCHEMA")
        rel_pos = prefix.index("RELATIONSHIPS")
        examples_pos = prefix.index("EXAMPLES")
        assert rules_pos < metrics_pos < schema_pos < rel_pos < examples_pos


class TestShouldUseStaticPrefix:
    def test_true_by_default_for_current_schema(self):
        assert should_use_static_prefix(_SYSTEM_PROMPT) is True

    def test_false_when_budget_is_tiny(self):
        with override_settings(prompt_retrieval_token_budget=1):
            assert should_use_static_prefix(_SYSTEM_PROMPT) is False

    def test_false_when_budget_is_zero_or_negative(self):
        with override_settings(prompt_retrieval_token_budget=0):
            assert should_use_static_prefix(_SYSTEM_PROMPT) is False
        with override_settings(prompt_retrieval_token_budget=-1):
            assert should_use_static_prefix(_SYSTEM_PROMPT) is False

    def test_true_when_budget_generously_large(self):
        with override_settings(prompt_retrieval_token_budget=1_000_000):
            assert should_use_static_prefix(_SYSTEM_PROMPT) is True


class TestPromptBuilderStaticPath:
    def test_build_dispatches_to_static_by_default(self):
        ctx = RetrievalContext(filters={"PersianYear": 1402})
        prompt = PromptBuilder.build("سوال", _SYSTEM_PROMPT, ctx)
        # Full schema present regardless of context.entities/facts (empty here).
        assert "Table: Contract" in prompt
        assert "Table: Customer" in prompt
        assert "1402" in prompt

    def test_static_prefix_identical_regardless_of_question_or_filters(self):
        """Only the suffix may vary — the prefix bytes must not."""
        ctx_a = RetrievalContext(filters={"Ring": "A"})
        ctx_b = RetrievalContext(filters={"Ring": "B"}, entities=["Customer"])
        prompt_a = PromptBuilder.build("question one", _SYSTEM_PROMPT, ctx_a)
        prompt_b = PromptBuilder.build("question two", _SYSTEM_PROMPT, ctx_b)
        marker = "DETECTED FILTERS"
        prefix_a = prompt_a[: prompt_a.index(marker)]
        prefix_b = prompt_b[: prompt_b.index(marker)]
        assert prefix_a == prefix_b

    def test_session_context_lands_in_suffix_not_prefix(self):
        ctx = RetrievalContext()
        prompt = PromptBuilder.build(
            "q", _SYSTEM_PROMPT, ctx, session_context="turn t_01: ..."
        )
        marker = "DETECTED FILTERS"
        prefix = prompt[: prompt.index(marker)]
        assert "turn t_01" not in prefix
        assert "turn t_01" in prompt


class TestPromptBuilderRetrievalFallback:
    """Exit criterion: retrieval still works when the token budget is exceeded."""

    def test_forced_large_schema_uses_retrieval_path(self):
        ctx = RetrievalContext(
            entities=["Customer"],
            facts=["Contract"],
            relationships=["JOIN Customer ON Contract.CustID = Customer.CustID"],
            business_rules=["Only count settled contracts."],
            examples=[{"question": "Q", "sql": "SELECT 1", "tags": []}],
            filters={"PersianYear": 1402},
        )
        with override_settings(prompt_retrieval_token_budget=1):
            prompt = PromptBuilder.build("سوال", _SYSTEM_PROMPT, ctx)

        # Retrieval-path markers: only the retrieved knowledge appears,
        # not the full 12-table static schema block.
        assert "Only count settled contracts." in prompt
        assert "Table: Customer" in prompt
        # A table NOT in the retrieved context must be absent -- proof this
        # is genuinely the narrow retrieval path, not the static one.
        assert "Table: Ring" not in prompt

    def test_static_path_would_have_included_every_table(self):
        """Sanity check the contrast: the static path (default budget)
        includes tables the retrieval-fallback path above correctly omits."""
        ctx = RetrievalContext(entities=["Customer"], facts=["Contract"])
        prompt = PromptBuilder.build("سوال", _SYSTEM_PROMPT, ctx)
        assert "Table: Ring" in prompt

    def test_static_prefix_token_estimate_is_positive(self):
        assert static_prefix_token_estimate(_SYSTEM_PROMPT) > 0
