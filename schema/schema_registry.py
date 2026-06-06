"""Build the schema context string injected into each LLM prompt.

Usage::

    from schema.schema_registry import build_schema_context
    context = build_schema_context(["CustomerContract", "Contract", "Date"])
"""

from __future__ import annotations

from functools import lru_cache

from schema.business_rules import BUSINESS_RULES
from schema.relationships import RELATIONSHIPS
from schema.table_schemas import TABLE_SCHEMAS


@lru_cache(maxsize=64)
def build_schema_context(selected_tables: tuple[str, ...] | None = None) -> str:
    """Return a formatted schema block for *selected_tables*.

    Parameters
    ----------
    selected_tables:
        Tuple of logical table names to include.  Pass ``None`` to
        include all registered tables.  A tuple (not list) is required
        for LRU-cache compatibility.
    """
    tables: list[str] = (
        list(selected_tables)
        if selected_tables
        else list(TABLE_SCHEMAS.keys())
    )

    sections: list[str] = [BUSINESS_RULES, "\nSCHEMA"]
    for table in tables:
        if table in TABLE_SCHEMAS:
            sections.append(TABLE_SCHEMAS[table])

    sections += ["\nRELATIONSHIPS", RELATIONSHIPS]
    return "\n\n".join(sections)
