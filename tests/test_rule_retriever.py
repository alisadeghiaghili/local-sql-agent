# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for retrieval/rule_retriever.py (triggered and always-on rules).

``RuleRetriever`` reads ``BUSINESS_RULES`` through the module global it
imported, so each test replaces that global with a small in-memory mapping
and leaves the real configuration untouched.
"""

from __future__ import annotations

import pytest

from retrieval import rule_retriever
from retrieval.rule_retriever import RuleRetriever


@pytest.fixture
def set_rules(monkeypatch: pytest.MonkeyPatch):
    """Return a helper that installs a ``BUSINESS_RULES`` mapping for one test."""

    def _set(rules: dict[str, str]) -> None:
        monkeypatch.setattr(rule_retriever, "BUSINESS_RULES", rules)

    return _set


def test_unmapped_rule_is_returned_for_every_question(set_rules) -> None:
    """A rule whose key is not in RULE_MAPPING has no trigger and always applies."""
    set_rules({"custom_policy": "Always exclude test accounts."})

    assert RuleRetriever.retrieve("how many widgets were sold") == [
        "Always exclude test accounts."
    ]
    assert RuleRetriever.retrieve("purchase volume") == [
        "Always exclude test accounts."
    ]


def test_mapped_rule_is_returned_when_keyword_matches(set_rules) -> None:
    """A mapped rule is returned when one of its trigger keywords is in the question."""
    set_rules({"purchase": "Purchase queries use FactTable."})

    assert RuleRetriever.retrieve("total purchase value") == [
        "Purchase queries use FactTable."
    ]


def test_mapped_rule_matches_persian_keyword(set_rules) -> None:
    """Persian trigger keywords match the same way as English ones."""
    set_rules({"purchase": "Purchase queries use FactTable."})

    assert RuleRetriever.retrieve("مجموع خرید ماه گذشته") == [
        "Purchase queries use FactTable."
    ]


def test_mapped_rule_is_not_returned_without_keyword_match(set_rules) -> None:
    """A mapped rule stays out of the prompt when no trigger keyword occurs."""
    set_rules({"purchase": "Purchase queries use FactTable."})

    assert RuleRetriever.retrieve("how many widgets were sold") == []


def test_order_follows_business_rules_not_rule_mapping(set_rules) -> None:
    """Output follows the BUSINESS_RULES (YAML) order, not RULE_MAPPING order.

    ``purchase`` is listed before ``trade`` in RULE_MAPPING but comes after
    ``trade`` here; the unmapped keys sit between and around the mapped ones.
    """
    set_rules(
        {
            "trade": "Trade text.",
            "zeta": "Zeta text.",
            "purchase": "Purchase text.",
            "alpha": "Alpha text.",
        }
    )

    assert RuleRetriever.retrieve("purchase and trade") == [
        "Trade text.",
        "Zeta text.",
        "Purchase text.",
        "Alpha text.",
    ]


def test_blank_rule_text_is_skipped_and_text_is_stripped(set_rules) -> None:
    """Whitespace-only and empty rule texts are dropped; others are stripped."""
    set_rules(
        {
            "zeta": "   \n  ",
            "alpha": "  Keep this.\n",
            "purchase": "",
        }
    )

    assert RuleRetriever.retrieve("purchase") == ["Keep this."]


def test_no_duplicates_when_two_keywords_of_one_key_match(set_rules) -> None:
    """Two matching keywords of the same key still yield the rule once."""
    set_rules({"purchase": "Purchase queries use FactTable."})

    assert RuleRetriever.retrieve("purchase خرید") == [
        "Purchase queries use FactTable."
    ]


def test_mapped_key_without_rule_text_contributes_nothing(set_rules) -> None:
    """A RULE_MAPPING key that is absent from BUSINESS_RULES is simply ignored."""
    set_rules({"zeta": "Zeta text."})

    assert RuleRetriever.retrieve("purchase") == ["Zeta text."]


def test_no_rules_configured_returns_empty_list(set_rules) -> None:
    """With no business rules at all the result is an empty list."""
    set_rules({})

    assert RuleRetriever.retrieve("purchase volume") == []
