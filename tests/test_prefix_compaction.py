# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The static prefix was compacted by formatting only: nothing the model reads was dropped.

The prefix is the largest part of every SQL-generation prompt and the part a
cold model server prefills token by token, so its size is latency. Two
formatting changes shrank it without touching what it says:

* a column line is ``- Name: description``, not ``  - Name: description``
  (the indent carried no meaning);
* each business rule is stripped before the rules are joined, so a rule's
  trailing newline no longer adds a blank line between rules.

What this file pins is the claim that matters -- *formatting only*:

* every table, description, column, column description, relationship, rule,
  metric and example of the loaded configuration is in the prefix;
* reversing the two formatting changes gives back, byte for byte, the prefix
  the previous release built (a hash captured before the change; the
  synthetic ``project_config.example`` and a fixed system prompt, so no
  deployment's text is in this repository);
* the prefix is smaller, and the savings are what they were measured to be.

Anything that removes or rewords content belongs in the configuration files,
not in how they are rendered, and would fail the first group of tests.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

import config as cfg
from knowledge.business_rules import BUSINESS_RULES
from knowledge.examples import EXAMPLES
from knowledge.metrics import METRICS
from prompt_engine.static_prefix import (
    build_static_prefix,
    estimate_tokens,
    static_prefix_token_estimate,
)
from schema_data.columns import TABLE_COLUMNS
from schema_data.registry import SchemaRegistry, get_table_descriptions

SYSTEM_PROMPT = "You are a T-SQL expert."

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The prefix of ``project_config.example`` + ``SYSTEM_PROMPT`` as the release
#: before this compaction built it (also the first row of
#: ``tests/test_source_prompts.py``'s golden values, until they were
#: re-captured).
_PRE_COMPACTION = {"sha12": "077df029a140", "chars": 3805, "estimate": 951}


def _using_example_config() -> bool:
    return Path(cfg.settings.project_config_dir).resolve() == (
        _REPO_ROOT / "project_config.example"
    ).resolve()


@pytest.fixture()
def prefix() -> str:
    build_static_prefix.cache_clear()
    return build_static_prefix(SYSTEM_PROMPT)


def _squash(text: str) -> str:
    """*text* with every run of whitespace as one space: what survives any pure re-formatting."""
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Nothing the model reads was dropped
# ---------------------------------------------------------------------------

class TestNothingWasDropped:
    def test_every_table_with_its_description_is_present(self, prefix):
        descriptions = get_table_descriptions()
        for table in TABLE_COLUMNS:
            assert f"Table: {table}\n" in prefix
            if descriptions.get(table):
                assert f"Description: {descriptions[table]}\n" in prefix

    def test_every_column_with_its_description_is_present(self, prefix):
        for table, columns in TABLE_COLUMNS.items():
            block = prefix[prefix.index(f"Table: {table}\n"):]
            block = block.split("\n\n", 1)[0]
            for column, description in columns.items():
                assert f"\n- {column}: {description}" in block, (table, column)

    def test_the_columns_of_a_table_keep_their_order_and_count(self, prefix):
        for table, columns in TABLE_COLUMNS.items():
            block = prefix[prefix.index(f"Table: {table}\n"):].split("\n\n", 1)[0]
            listed = [line[2:].split(":", 1)[0] for line in block.splitlines() if line.startswith("- ")]
            assert listed == list(columns), table

    def test_every_relationship_is_present(self, prefix):
        for join in SchemaRegistry.get_relationships(list(TABLE_COLUMNS)):
            assert join in prefix

    def test_every_business_rule_is_present_whole(self, prefix):
        for rule in BUSINESS_RULES.values():
            assert rule.strip() in prefix

    def test_business_rules_keep_their_order(self, prefix):
        positions = [prefix.index(rule.strip()) for rule in BUSINESS_RULES.values()]
        assert positions == sorted(positions)

    def test_every_metric_is_present(self, prefix):
        for key, spec in METRICS.items():
            assert f"{key}: {spec.get('expression', '')}" in prefix

    def test_every_example_is_present_whole(self, prefix):
        for example in EXAMPLES:
            assert f"Question:\n{example['question']}\n\nSQL:\n{example['sql']}" in prefix

    def test_the_system_prompt_is_present_verbatim(self, prefix):
        assert SYSTEM_PROMPT in prefix

    def test_the_sections_keep_their_order(self, prefix):
        titles = ["BUSINESS RULES", "METRICS", "DATABASE SCHEMA", "RELATIONSHIPS", "EXAMPLES"]
        positions = [prefix.index(f"\n{title}\n") for title in titles]
        assert positions == sorted(positions)

    def test_only_whitespace_separates_a_rule_from_the_next(self, prefix):
        rules = [rule.strip() for rule in BUSINESS_RULES.values()]
        joined = "\n\n".join(rules)
        assert joined in prefix
        assert _squash(joined) == _squash("\n".join(BUSINESS_RULES.values()))


# ---------------------------------------------------------------------------
# What changed is the two formatting changes, and only those
# ---------------------------------------------------------------------------

class TestFormattingOnly:
    def test_column_lines_carry_no_indent(self, prefix):
        assert "\n  - " not in prefix
        assert "\n- " in prefix

    def test_no_blank_line_pile_up_between_rules(self, prefix):
        rules = _squash("\n".join(BUSINESS_RULES.values()))
        assert rules
        section = prefix.split("BUSINESS RULES\n", 1)[1].split("\n====", 1)[0]
        assert "\n\n\n" not in section

    @pytest.mark.skipif(not _using_example_config(), reason="hash is of project_config.example")
    def test_reversing_the_formatting_gives_back_the_previous_prefix(self, prefix):
        old_rules = "\n\n".join(BUSINESS_RULES.values())
        new_rules = "\n\n".join(rule.strip() for rule in BUSINESS_RULES.values())
        assert new_rules in prefix
        restored = prefix.replace(new_rules, old_rules, 1)
        restored = re.sub(r"(?m)^- (?=\S)", "  - ", restored)
        assert len(restored) == _PRE_COMPACTION["chars"]
        assert hashlib.sha256(restored.encode("utf-8")).hexdigest()[:12] == _PRE_COMPACTION["sha12"]

    @pytest.mark.skipif(not _using_example_config(), reason="numbers are for project_config.example")
    def test_it_is_smaller_by_the_measured_amount(self, prefix):
        assert static_prefix_token_estimate(SYSTEM_PROMPT) == estimate_tokens(prefix)
        assert len(prefix) == _PRE_COMPACTION["chars"] - 67
        assert estimate_tokens(prefix) < _PRE_COMPACTION["estimate"]

    def test_the_cache_identity_follows_the_text(self, prefix):
        from prompt_engine.static_prefix import prefix_version

        build_static_prefix.cache_clear()
        prefix_version.cache_clear()
        assert prefix_version(SYSTEM_PROMPT) == hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:12]
