# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Contract tests for the system prompt loaded from PROJECT_CONFIG_DIR.

Philosophy
----------
The system prompt is **configuration as code**: a typo or deleted keyword
can silently break LLM behaviour with no Python exception at import time.
These tests act as a compile-time check — they fail immediately in CI if
a critical keyword, section, or structural invariant is removed or
misspelled.

Two categories of assertions:

1. **Existence** — file is present and non-empty.
2. **Structural / content contract** — specific keywords / patterns that
   the SQL guard, retriever, or LLM client depend on must be present.

``few_shots.md`` and ``business_glossary.md`` used to be covered here too
(``prompts/few_shots.md`` / ``prompts/business_glossary.md``). Both files
were deleted: nothing in first-party code ever read either one --
``prompt_engine/static_prefix.py`` builds its few-shot block from
``knowledge.examples.EXAMPLES`` (``project_config/examples.yaml``) and its
business-rules block from ``knowledge.business_rules.BUSINESS_RULES``
(``business_rules.yaml``), both already domain-data-driven -- so their only
readers were this module's own contract tests. Removed along with the
files they tested.
"""

from __future__ import annotations

import re

import pytest

from knowledge.config_loader import resolve_system_prompt_path

# ---------------------------------------------------------------------------
# Fixture — load the system prompt once per session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def system_prompt() -> str:
    path = resolve_system_prompt_path()
    assert path.exists(), f"system prompt not found at {path}"
    return path.read_text(encoding="utf-8")


# ===========================================================================
# system prompt
# ===========================================================================

class TestSystemPromptExists:
    def test_file_is_non_empty(self, system_prompt):
        assert len(system_prompt.strip()) > 100, "system prompt is too short"

    def test_no_bom_or_null_bytes(self, system_prompt):
        assert "\x00" not in system_prompt
        assert not system_prompt.startswith("﻿")


class TestSystemPromptSqlServerRules:
    """Rules that the SQL guard (sql_guard.py) and LLM prompt depend on."""

    def test_forbids_limit_keyword(self, system_prompt):
        """LIMIT must be explicitly banned — sql_guard.py also rejects it."""
        assert "LIMIT" in system_prompt, "LIMIT prohibition missing from system prompt"

    def test_instructs_use_top(self, system_prompt):
        assert "TOP" in system_prompt, "TOP instruction missing"

    def test_forbids_select_star(self, system_prompt):
        assert "SELECT *" in system_prompt, "SELECT * prohibition missing"

    def test_forbids_dml_keywords(self, system_prompt):
        for kw in ("DELETE", "UPDATE", "INSERT", "DROP", "ALTER"):
            assert kw in system_prompt, f"DML keyword '{kw}' prohibition missing"

    def test_bracket_notation_instructed(self, system_prompt):
        """Bracket notation is required for SQL Server schema-qualified names."""
        assert "[" in system_prompt and "]" in system_prompt, \
            "Bracket notation example missing"

    def test_no_markdown_in_output_instruction(self, system_prompt):
        """Model must be told not to wrap SQL in markdown fences."""
        assert "markdown" in system_prompt.lower() or "```" in system_prompt, \
            "No-markdown instruction missing"

    def test_instructs_no_explanation(self, system_prompt):
        lowered = system_prompt.lower()
        assert "no explanation" in lowered or "never explain" in lowered, \
            "'No explanation' instruction missing"

    def test_schema_qualified_names_mentioned(self, system_prompt):
        assert "schema" in system_prompt.lower() or "schema-qualified" in system_prompt.lower(), \
            "Schema-qualified name instruction missing"

    def test_row_number_cte_pattern_present(self, system_prompt):
        """Ranking queries must use ROW_NUMBER() CTE — not subquery."""
        assert "ROW_NUMBER()" in system_prompt, "ROW_NUMBER() CTE instruction missing"
        assert "WITH" in system_prompt or "CTE" in system_prompt.upper(), \
            "CTE instruction missing for ranking queries"

    def test_distinct_top_order_correct(self, system_prompt):
        """DISTINCT before TOP is the correct SQL Server syntax."""
        assert "SELECT DISTINCT TOP" in system_prompt, \
            "'SELECT DISTINCT TOP' correct-order example missing"

    def test_incorrect_top_distinct_order_flagged(self, system_prompt):
        """The prompt must show TOP DISTINCT as the *incorrect* form."""
        assert "SELECT TOP" in system_prompt and "DISTINCT" in system_prompt, \
            "TOP/DISTINCT ordering example missing"

    def test_forbidden_dialects_listed(self, system_prompt):
        """Non-SQL-Server syntax must be explicitly banned."""
        for term in ("QUALIFY", "ILIKE", "SERIAL"):
            assert term in system_prompt, f"Forbidden dialect term '{term}' missing"

    def test_default_top_n_specified(self, system_prompt):
        """Model must have a default TOP N when user doesn't specify."""
        assert re.search(r"TOP\s+\d+", system_prompt), \
            "Default TOP N value missing from system prompt"


class TestSystemPromptOutOfScope:
    """OUT_OF_SCOPE sentinel must be spelled exactly right — sql_guard and
    app.py both check for the exact string 'OUT_OF_SCOPE'."""

    def test_sentinel_present(self, system_prompt):
        assert "OUT_OF_SCOPE" in system_prompt

    def test_sentinel_spelled_correctly(self, system_prompt):
        """Common typos: out_of_scope, OUT-OF-SCOPE, out-of-scope."""
        bad_variants = ["OUT-OF-SCOPE", "out-of-scope",
                        "OUTOFSCOPE", "OUT OF SCOPE"]
        for bad in bad_variants:
            assert bad not in system_prompt, \
                f"Misspelled sentinel found: '{bad}'"

    def test_sentinel_return_instruction_present(self, system_prompt):
        """The prompt must instruct the model to RETURN the sentinel."""
        lowered = system_prompt.lower()
        assert "return" in lowered and "OUT_OF_SCOPE" in system_prompt, \
            "'return OUT_OF_SCOPE' instruction missing"

    def test_out_of_scope_examples_present(self, system_prompt):
        """At least one concrete out-of-scope example question required."""
        assert "OUT_OF_SCOPE" in system_prompt
        # Must appear at least twice: instruction + at least one example
        assert system_prompt.count("OUT_OF_SCOPE") >= 3, \
            "Need at least 3 occurrences: instruction + examples"

    def test_supported_topics_listed(self, system_prompt):
        for topic in ("Customers", "Orders", "Rings"):
            assert topic in system_prompt, f"Supported topic '{topic}' missing"

    def test_out_of_scope_topics_listed(self, system_prompt):
        for topic in ("politics", "sports", "weather"):
            assert topic in system_prompt.lower(), \
                f"Out-of-scope topic '{topic}' missing from domain restrictions"
