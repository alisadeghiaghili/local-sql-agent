from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class RetrievalContext:
    """
    Holds all retrieved context for a single user question.
    Built by ContextRetriever and consumed by PromptBuilder.
    """

    # Dimension tables detected (e.g. Customer, Broker, Symbol)
    entities: List[str] = field(default_factory=list)

    # Fact tables detected (e.g. Contract, Offer, Order)
    facts: List[str] = field(default_factory=list)

    # Alias for entities — kept separate for future use
    dimensions: List[str] = field(default_factory=list)

    # Relevant JOIN clauses
    relationships: List[str] = field(default_factory=list)

    # Matched business rules (plain text)
    business_rules: List[str] = field(default_factory=list)

    # Few-shot SQL examples: [{"question": ..., "sql": ...}]
    examples: List[Dict[str, str]] = field(default_factory=list)

    # Extracted filter values: {"Ring": "تالار پتروشیمی", ...}
    filters: Dict[str, str] = field(default_factory=dict)

    @property
    def selected_tables(self) -> List[str]:
        """Union of dimension and fact tables."""
        return list(set(self.entities + self.facts))

    def is_empty(self) -> bool:
        """Returns True if no useful context was retrieved."""
        return not (self.entities or self.facts)
