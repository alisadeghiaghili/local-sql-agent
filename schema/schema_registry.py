"""Build the schema context string injected into each LLM prompt.

Usage::

    from schema.schema_registry import build_schema_context
    context = build_schema_context(("CustomerContract", "Contract", "Date"))
"""

from __future__ import annotations

from functools import lru_cache

from schema.business_rules import BUSINESS_RULES
from schema.relationships import RELATIONSHIPS
from schema.table_schemas import TABLE_SCHEMAS


def _filter_relationships(selected_tables: tuple[str, ...]) -> str:
    """Return only relationship lines that mention at least one selected table."""
    if not selected_tables:
        return RELATIONSHIPS
    lines = []
    for line in RELATIONSHIPS.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if any(f".{table}." in line or f".{table}]" in line
               for table in selected_tables):
            lines.append(line)
    return "\n".join(lines)


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

    rel_block = (
        _filter_relationships(selected_tables)
        if selected_tables
        else RELATIONSHIPS
    )
    sections += ["\nRELATIONSHIPS", rel_block]
    return "\n\n".join(sections)
