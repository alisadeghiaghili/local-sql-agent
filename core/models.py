"""Core domain models shared across the entire application."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrievalContext:
    """All context retrieved for a single user question.

    Produced by ``ContextRetriever.retrieve()`` and consumed by
    ``PromptBuilder.build()``.
    """

    entities: list[str] = field(default_factory=list)
    """Dimension table names matched from the question (e.g. ['Customer', 'Ring'])."""

    facts: list[str] = field(default_factory=list)
    """Fact table names matched from the question (e.g. ['CustomerContract'])."""

    dimensions: list[str] = field(default_factory=list)
    """Alias for entities — kept separate so PromptBuilder can distinguish."""

    relationships: list[str] = field(default_factory=list)
    """JOIN condition strings relevant to the selected tables."""

    business_rules: list[str] = field(default_factory=list)
    """Business rule paragraphs relevant to the question."""

    examples: list[dict[str, Any]] = field(default_factory=list)
    """Few-shot SQL examples selected by ExampleRetriever."""

    filters: dict[str, Any] = field(default_factory=dict)
    """Concrete filter values extracted from the question (e.g. {'Ring': 'تالار پتروشیمی', 'PersianYear': 1403})."""

    @property
    def selected_tables(self) -> list[str]:
        """Union of entity and fact tables — de-duplicated, order preserved."""
        seen: set[str] = set()
        result: list[str] = []
        for t in self.entities + self.facts:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result
