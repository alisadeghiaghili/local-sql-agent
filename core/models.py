"""Core domain models shared across retrieval and prompt layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrievalContext:
    """Aggregated retrieval result passed from ContextRetriever to PromptBuilder.

    Attributes
    ----------
    entities:
        Table names matched via entity/alias lookup (e.g. 'Customer', 'Broker').
    facts:
        Table names matched via TF-IDF fact scoring (e.g. 'Contract', 'Offer').
    dimensions:
        Dimension tables relevant to the question (subset of entities).
    relationships:
        Human-readable JOIN relationship strings for the selected tables.
    business_rules:
        Domain business rules relevant to the question.
    examples:
        Few-shot SQL examples, each a dict with 'question', 'sql', 'tags' keys.
    filters:
        Detected filter values, e.g. {'Ring': 'تالار پتروشیمی', 'PersianYear': 1402}.
    """

    entities: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    business_rules: list[str] = field(default_factory=list)
    examples: list[dict[str, Any]] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
