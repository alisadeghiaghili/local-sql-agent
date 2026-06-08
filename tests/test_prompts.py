"""Contract tests for prompts/system_prompt.md, few_shots.md, business_glossary.md.

Philosophy
----------
Prompt files are **configuration as code**: a typo or deleted keyword can
silently break LLM behaviour with no Python exception at import time.
These tests act as a compile-time check — they fail immediately in CI if
a critical keyword, section, or structural invariant is removed or misspelled.

Three categories of assertions:

1. **Existence** — file is present and non-empty.
2. **Structural** — required sections / headings exist.
3. **Content contract** — specific keywords / patterns that the SQL guard,
   retriever, or Ollama client depends on must be present.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures — load each file once per session
# ---------------------------------------------------------------------------

PROMPTS_DIR = Path("prompts")


@pytest.fixture(scope="session")
def system_prompt() -> str:
    path = PROMPTS_DIR / "system_prompt.md"
    assert path.exists(), "prompts/system_prompt.md is missing"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def few_shots() -> str:
    path = PROMPTS_DIR / "few_shots.md"
    assert path.exists(), "prompts/few_shots.md is missing"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def business_glossary() -> str:
    path = PROMPTS_DIR / "business_glossary.md"
    assert path.exists(), "prompts/business_glossary.md is missing"
    return path.read_text(encoding="utf-8")


# ===========================================================================
# system_prompt.md
# ===========================================================================

class TestSystemPromptExists:
    def test_file_is_non_empty(self, system_prompt):
        assert len(system_prompt.strip()) > 100, "system_prompt.md is too short"

    def test_no_bom_or_null_bytes(self, system_prompt):
        assert "\x00" not in system_prompt
        assert not system_prompt.startswith("\ufeff")


class TestSystemPromptSqlServerRules:
    """Rules that the SQL guard (sql_guard.py) and LLM prompt depend on."""

    def test_forbids_limit_keyword(self, system_prompt):
        """LIMIT must be explicitly banned — sql_guard.py also rejects it."""
        assert "LIMIT" in system_prompt, "LIMIT prohibition missing from system_prompt"

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
            "Default TOP N value missing from system_prompt"


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
        for topic in ("Customers", "Contracts", "Rings"):
            assert topic in system_prompt, f"Supported topic '{topic}' missing"

    def test_out_of_scope_topics_listed(self, system_prompt):
        for topic in ("politics", "sports", "weather"):
            assert topic in system_prompt.lower(), \
                f"Out-of-scope topic '{topic}' missing from domain restrictions"


class TestSystemPromptRingAliases:
    """Ring business aliases must be present so the LLM maps Persian names."""

    REQUIRED_ALIASES = [
        "\u067e\u062a\u0631\u0648\u0634\u06cc\u0645\u06cc",
        "\u06a9\u06cc\u0634",
        "\u0641\u0644\u0632\u0627\u062a",
        "\u06a9\u0634\u0627\u0648\u0631\u0632\u06cc",
        "\u0646\u0641\u062a\u06cc",
        "\u062e\u0631\u062f",
        "\u0637\u0644\u0627",
        "\u0633\u06cc\u0645\u0627\u0646",
        "\u062e\u0648\u062f\u0631\u0648",
        "\u0645\u0646\u0627\u0642\u0635\u0647",
    ]

    def test_all_required_aliases_present(self, system_prompt):
        for alias in self.REQUIRED_ALIASES:
            assert alias in system_prompt, \
                f"Ring alias '{alias}' missing from system_prompt"

    def test_ring_aliases_section_header_present(self, system_prompt):
        assert "RING" in system_prompt.upper() and "ALIAS" in system_prompt.upper() or \
               "\u0631\u06cc\u0646\u06af" in system_prompt or "\u062a\u0627\u0644\u0627\u0631" in system_prompt, \
               "Ring aliases section header missing"

    def test_petrochemical_maps_to_correct_hall(self, system_prompt):
        assert "\u062a\u0627\u0644\u0627\u0631 \u067e\u062a\u0631\u0648\u0634\u06cc\u0645\u06cc" in system_prompt, \
            "Mapping 'پتروشیمی' → 'تالار پتروشیمی' missing"

    def test_kish_maps_to_correct_hall(self, system_prompt):
        assert "\u062a\u0627\u0644\u0627\u0631 \u06a9\u0627\u0644\u0627\u06cc \u0635\u0627\u062f\u0631\u0627\u062a\u06cc" in system_prompt, \
            "Mapping 'کیش' → 'تالار کالای صادراتی کیش' missing"


# ===========================================================================
# few_shots.md
# ===========================================================================

class TestFewShotsExists:
    def test_file_is_non_empty(self, few_shots):
        assert len(few_shots.strip()) > 50


class TestFewShotsStructure:
    """Each example must follow the Question/SQL block format so the LLM
    can reliably parse the examples."""

    def test_has_question_label(self, few_shots):
        assert "Question:" in few_shots or "Question\n" in few_shots, \
            "'Question:' label missing from few_shots.md"

    def test_has_sql_label(self, few_shots):
        assert "SQL:" in few_shots or "SQL\n" in few_shots, \
            "'SQL:' label missing from few_shots.md"

    def test_question_sql_pairs_balanced(self, few_shots):
        q_count   = len(re.findall(r"^Question[\ :]?", few_shots, re.MULTILINE))
        sql_count = len(re.findall(r"^SQL[\ :]?",      few_shots, re.MULTILINE))
        assert q_count == sql_count, \
            f"Unbalanced Question/SQL pairs: {q_count} questions vs {sql_count} SQL blocks"

    def test_at_least_three_examples(self, few_shots):
        count = len(re.findall(r"^Question[\ :]?", few_shots, re.MULTILINE))
        assert count >= 3, f"Need at least 3 few-shot examples, found {count}"


class TestFewShotsContentContracts:
    """Specific SQL patterns that the examples must demonstrate."""

    def test_uses_bracket_notation(self, few_shots):
        assert "[" in few_shots and "]" in few_shots, \
            "Bracket notation missing from few_shots examples"

    def test_uses_schema_qualified_names(self, few_shots):
        assert re.search(r"\[\w+\]\.\[\w+\]", few_shots), \
            "Schema-qualified bracket notation (e.g. [Schema].[Table]) missing"

    def test_no_select_star(self, few_shots):
        assert "SELECT *" not in few_shots, \
            "'SELECT *' found in few_shots — use explicit column names"

    def test_no_limit_clause(self, few_shots):
        assert not re.search(r"\bLIMIT\b", few_shots, re.IGNORECASE), \
            "LIMIT clause found in few_shots — use TOP instead"

    def test_contains_count_example(self, few_shots):
        assert "COUNT(" in few_shots, \
            "No COUNT() example in few_shots — add at least one aggregation example"

    def test_contains_join_example(self, few_shots):
        assert "JOIN" in few_shots.upper(), \
            "No JOIN example in few_shots — multi-table queries must be demonstrated"

    def test_contains_top_example(self, few_shots):
        assert re.search(r"\bTOP\s+\d+", few_shots), \
            "No TOP N example in few_shots"

    def test_no_markdown_fences(self, few_shots):
        assert "```" not in few_shots, \
            "Markdown code fences found in few_shots — model may mirror them"

    def test_no_prose_explanations(self, few_shots):
        """Each block after 'SQL:' must start with SELECT/WITH, not prose."""
        sql_blocks = re.findall(
            r"SQL[:\s]+([^\n]+)", few_shots
        )
        for block in sql_blocks:
            stripped = block.strip()
            if stripped:
                first_token = stripped.split()[0].upper()
                assert first_token in ("SELECT", "WITH", "INSERT", "--"), \
                    f"few_shots SQL block starts with prose: '{stripped[:60]}'"


# ===========================================================================
# business_glossary.md
# ===========================================================================

class TestBusinessGlossaryExists:
    def test_file_is_non_empty(self, business_glossary):
        assert len(business_glossary.strip()) > 20


class TestBusinessGlossaryContent:
    REQUIRED_TERMS = [
        "Contract",
        "Customer",
        "Ring",
    ]

    def test_required_terms_present(self, business_glossary):
        for term in self.REQUIRED_TERMS:
            assert term in business_glossary, \
                f"Business term '{term}' missing from business_glossary.md"


# ===========================================================================
# Cross-file consistency
# ===========================================================================

class TestCrossFileConsistency:
    """Invariants that must hold across multiple prompt files."""

    def test_out_of_scope_sentinel_only_in_system_prompt(self, few_shots, business_glossary):
        """OUT_OF_SCOPE must NOT appear as an answer in few_shots or glossary."""
        assert "OUT_OF_SCOPE" not in few_shots, \
            "OUT_OF_SCOPE sentinel found in few_shots — remove it"

    def test_system_prompt_bracket_notation_matches_few_shots(self, system_prompt, few_shots):
        """Both files must use the same [Schema].[Table] bracket style."""
        sp_has_brackets = bool(re.search(r"\[\w+\]\.\[\w+\]", system_prompt))
        fs_has_brackets = bool(re.search(r"\[\w+\]\.\[\w+\]", few_shots))
        assert sp_has_brackets and fs_has_brackets, \
            "Bracket notation inconsistency between system_prompt and few_shots"

    def test_auction_schemas_referenced_consistently(self, system_prompt, few_shots):
        """Schema names referenced in few_shots must appear in system_prompt too."""
        schemas_in_fs = re.findall(r"\[(\w+)\]\.\[", few_shots)
        for schema in set(schemas_in_fs):
            assert schema in system_prompt, \
                f"Schema '[{schema}]' used in few_shots but not mentioned in system_prompt"
