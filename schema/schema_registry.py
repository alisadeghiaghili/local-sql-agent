"""Build the schema context string injected into each prompt.

Example::

    from schema.schema_registry import build_schema_context
    context = build_schema_context(["Customer", "Contract", "Date"])
"""

from __future__ import annotations

from schema.business_rules import BUSINESS_RULES
from schema.relationships import RELATIONSHIPS
from schema.table_schemas import TABLE_SCHEMAS


def build_schema_context(selected_tables: list[str] | None = None) -> str:
    """Return a formatted schema block for *selected_tables*.

    If *selected_tables* is empty or None, all tables are included.
    """
    if not selected_tables:
        selected_tables = list(TABLE_SCHEMAS.keys())

    sections: list[str] = [BUSINESS_RULES, "\nSCHEMA"]

    for table in selected_tables:
        if table in TABLE_SCHEMAS:
            sections.append(TABLE_SCHEMAS[table])

    sections.append("\nRELATIONSHIPS")
    sections.append(RELATIONSHIPS)

    return "\n\n".join(sections)
