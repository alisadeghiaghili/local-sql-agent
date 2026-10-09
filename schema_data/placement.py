# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Which data source(s) each ``schema.yaml`` table lives in, and the
``datasource:`` line that says so.

Shared by ``scripts/assign_datasources.py`` (which writes only these lines)
and ``scripts/sync_schema.py`` (which writes them as one part of a wider
structural sync), so both apply exactly the same rules:

* found in one source: ``datasource: sales``;
* found in several (a replicated date dimension, say): ``datasource:
  [sales, inventory]``, in ``datasources.yaml`` order;
* found in none: the table is left as it is and the comment
  :data:`NOT_FOUND_COMMENT` is added directly under its key;
* a table whose ``datasource:`` already names exactly the sources it was
  found in is left untouched (a stale :data:`NOT_FOUND_COMMENT` on it is
  dropped).

Tables are matched to catalogues by schema and name, ignoring case, with a
multi-part ``db_schema`` (``OtherDb.dbo``) matched on its last part
(:func:`database.catalogue.table_location`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from database.catalogue import table_location
from schema_data.registry import SchemaConfig
from schema_data.yaml_text import Edit, TableBlock, datasource_span, trailing_comment

__all__ = [
    "NOT_FOUND_COMMENT",
    "Placement",
    "SourceCatalogue",
    "datasource_edits",
    "format_datasource",
    "locate_tables",
]

#: ``{(schema, table): {column, ...}}``, all lower-cased: one source's
#: tables and views with their columns.
SourceCatalogue = Mapping[tuple[str, str], frozenset[str]]

#: The comment put under the key of a table found in no data source.
NOT_FOUND_COMMENT = "# not found in any data source"

#: Schema used for a table with no qualifier at all (SQL Server).
_DEFAULT_SCHEMA = "dbo"


@dataclass(frozen=True)
class Placement:
    """Where one ``schema.yaml`` table was found.

    Attributes
    ----------
    key:
        The table key as written under ``tables:``.
    found_in:
        Every source whose catalogue has the table, in ``datasources.yaml``
        order; empty when it is nowhere.
    current:
        The table's ``datasource:`` as written (``()`` when it has none).
    effective:
        *current*, or ``(default,)`` when the table has none.
    missing_columns:
        ``{source: columns listed in schema.yaml that the source's table
        does not have}``, only for sources where the table was found and
        only where something is missing.
    """

    key: str
    found_in: tuple[str, ...]
    current: tuple[str, ...]
    effective: tuple[str, ...]
    missing_columns: Mapping[str, tuple[str, ...]]

    @property
    def disagrees(self) -> bool:
        """Found somewhere, and not in exactly the sources it is assigned to."""
        return bool(self.found_in) and set(self.found_in) != set(self.effective)

    @property
    def wanted(self) -> tuple[str, ...]:
        """The ``datasource:`` value the table should have afterwards.

        The value it has now when it is found nowhere or already matches;
        otherwise the sources it was found in.
        """
        if not self.found_in or set(self.found_in) == set(self.current):
            return self.current
        return self.found_in


def locate_tables(
    schema: SchemaConfig,
    sources: Sequence[str],
    default: str,
    catalogues: Mapping[str, SourceCatalogue],
) -> list[Placement]:
    """Match every ``schema.yaml`` table against every source's catalogue.

    Parameters
    ----------
    schema:
        The validated ``schema.yaml``.
    sources:
        Source names in ``datasources.yaml`` order (default first).
    default:
        The default source, where a table with no ``datasource:`` lives.
    catalogues:
        ``{source: catalogue}`` for every name in *sources*.

    Returns
    -------
    list[Placement]
        One per table, in ``schema.yaml`` order.

    Examples
    --------
    >>> from schema_data.registry import validate_schema_yaml_text
    >>> schema = validate_schema_yaml_text(
    ...     "tables:\\n  Date:\\n    db_schema: dim\\n    columns: {ID: x, Gone: y}\\n"
    ... )
    >>> found = {("dim", "date"): frozenset({"id"})}
    >>> [(p.key, p.found_in, dict(p.missing_columns)) for p in locate_tables(
    ...     schema, ["a", "b"], "a", {"a": found, "b": {}})]
    [('Date', ('a',), {'a': ('Gone',)})]
    """
    placements: list[Placement] = []
    for key, table in schema.tables.items():
        location = table_location(key, table.db_schema, _DEFAULT_SCHEMA)
        found = tuple(name for name in sources if location in catalogues[name])
        missing: dict[str, tuple[str, ...]] = {}
        if table.columns:
            for name in found:
                have = catalogues[name][location]
                gone = tuple(col for col in table.columns if col.lower() not in have)
                if gone:
                    missing[name] = gone
        placements.append(Placement(
            key=key,
            found_in=found,
            current=table.datasource,
            effective=table.datasource or (default,),
            missing_columns=missing,
        ))
    return placements


def format_datasource(sources: Sequence[str]) -> str:
    """A ``datasource:`` value: a name, or a flow list of several.

    Examples
    --------
    >>> format_datasource(["sales"]), format_datasource(["sales", "inventory"])
    ('sales', '[sales, inventory]')
    """
    return sources[0] if len(sources) == 1 else "[" + ", ".join(sources) + "]"


def datasource_edits(
    lines: Sequence[str], block: TableBlock, placement: Placement, cr: str = "",
) -> list[Edit]:
    """The line edits that give one table its ``datasource:``.

    Parameters
    ----------
    lines:
        The file split on ``"\\n"``.
    block:
        The table's place in the text.
    placement:
        Where the table was found.
    cr:
        ``"\\r"`` for a file with Windows line endings, else ``""``.

    Returns
    -------
    list[Edit]
        Empty when the table needs nothing. An existing ``datasource:``
        (inline or a multi-line list) is replaced in place, keeping a
        trailing comment on its first line; otherwise one line is inserted
        directly under the table key.

    Examples
    --------
    >>> lines = ["  Date:  # dim", "    description: d"]
    >>> block = TableBlock("Date", 0, 2, 2, 4)
    >>> datasource_edits(lines, block, Placement("Date", ("a", "b"), (), ("a",), {}))
    [(1, 1, ['    datasource: [a, b]'])]
    """
    first, end = block.line + 1, block.end
    pad = " " * block.child_indent
    marker = first < end and lines[first].strip() == NOT_FOUND_COMMENT
    if not placement.found_in:
        return [] if marker else [(first, first, [f"{pad}{NOT_FOUND_COMMENT}{cr}"])]
    if set(placement.found_in) == set(placement.current):
        return [(first, first + 1, [])] if marker else []
    new_value = format_datasource(placement.found_in)
    span = datasource_span(lines, first, end, block.child_indent)
    if span is None:
        replaced = 1 if marker else 0
        return [(first, first + replaced, [f"{pad}datasource: {new_value}{cr}"])]
    old_first, old_stop = span
    head = lines[old_first].rstrip("\r")
    comment = trailing_comment(head.split(":", 1)[1])
    tail = f"  {comment}" if comment else ""
    edits: list[Edit] = [(old_first, old_stop, [f"{pad}datasource: {new_value}{tail}{cr}"])]
    if marker:
        edits.append((first, first + 1, []))
    return edits
