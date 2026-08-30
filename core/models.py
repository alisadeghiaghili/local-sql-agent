"""Shared domain models for the retrieval pipeline.

RetrievalContext
----------------
Immutable dataclass produced by ContextRetriever and consumed by
PromptBuilder.  Every field is optional-safe: callers may receive
an empty list / dict when a sub-retriever finds nothing.

Fields
------
entities       : dimension table names matched to the question
                 (e.g. ["Ring", "Customer", "Symbol"])
facts          : fact table names matched to the question
                 (e.g. ["Contract", "CustomerContract"])
dimensions     : alias for *entities* — kept for PromptBuilder compatibility
relationships  : JOIN SQL clauses relevant to the selected tables
business_rules : domain rules injected into the prompt as plain text
examples       : few-shot {"question": ..., "sql": ..., "tags": [...]} dicts
filters        : concrete filter values extracted from the question
                 (e.g. {"Ring": "تالار پتروشیمی", "PersianYear": 1402})
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetrievalContext:
    """All context a PromptBuilder needs to construct a grounded SQL prompt."""

    # ── table selection ───────────────────────────────────────────────────────
    entities: list[str] = field(default_factory=list)
    """Dimension tables (Ring, Customer, Symbol, …) relevant to the question."""

    facts: list[str] = field(default_factory=list)
    """Fact tables (Contract, CustomerContract, Offer, …) relevant to the question."""

    dimensions: list[str] = field(default_factory=list)
    """Alias of *entities*, populated by ``ContextRetriever`` for backward
    compatibility. **Not** read by ``PromptBuilder`` — the builder derives
    its table list from ``entities``/``facts``/``selected_tables`` only.
    This field is a historical leftover kept so existing callers that read
    ``context.dimensions`` directly are unaffected; new code should prefer
    ``entities`` or ``selected_tables``."""

    # ── join layer ────────────────────────────────────────────────────────────
    relationships: list[str] = field(default_factory=list)
    """JOIN SQL snippets for every FK edge between selected_tables."""

    # ── knowledge injection ───────────────────────────────────────────────────
    business_rules: list[str] = field(default_factory=list)
    """Domain business rules (plain text) relevant to the question."""

    examples: list[dict] = field(default_factory=list)
    """Few-shot {question, sql, tags} dicts ranked by tag overlap."""

    # ── value filters ─────────────────────────────────────────────────────────
    filters: dict = field(default_factory=dict)
    """Concrete filter values extracted from the question.

    Examples
    --------
    {"Ring": "تالار پتروشیمی", "PersianYear": 1402}
    """

    # ── convenience ──────────────────────────────────────────────────────────
    @property
    def selected_tables(self) -> list[str]:
        """Deduplicated union of entities + facts — the full table set."""
        seen: set[str] = set()
        result: list[str] = []
        for t in self.entities + self.facts:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result

    def is_empty(self) -> bool:
        """True when no tables were matched — signals a fallback is needed."""
        return not self.entities and not self.facts
