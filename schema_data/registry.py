"""Schema registry — single source of truth for table/column metadata.

Provides :class:`SchemaRegistry` with two public methods:

* :meth:`~SchemaRegistry.build_schema_context` / :meth:`~SchemaRegistry.build_context`
  (alias) — render a human-readable schema block for the given tables.
* :meth:`~SchemaRegistry.get_relationships` — return JOIN SQL for FK edges
  between the selected tables.

Both methods are pure functions (no side-effects) and are safe to call
from multiple threads simultaneously.

Typical usage::

    from schema_data.registry import SchemaRegistry

    # All tables
    full_schema = SchemaRegistry.build_schema_context(None)

    # Specific tables
    ctx = SchemaRegistry.build_schema_context(["Contract", "Customer"])

    # JOIN clauses between selected tables
    joins = SchemaRegistry.get_relationships(["Contract", "Customer"])
"""

from __future__ import annotations

from schema_data.columns import TABLE_COLUMNS
from schema_data.tables import TABLE_DESCRIPTIONS
from schema_data.relationships import RELATIONSHIPS


class SchemaRegistry:
    """Stateless registry that renders schema and relationship data.

    All methods are static.  The class is a namespace — there is nothing
    to instantiate.

    Data sources
    ------------
    * :data:`~schema_data.columns.TABLE_COLUMNS` — ``{table: {col: desc}}``
    * :data:`~schema_data.tables.TABLE_DESCRIPTIONS` — ``{table: description}``
    * :data:`~schema_data.relationships.RELATIONSHIPS` — ``{"A -> B": join_sql}``
    """

    @staticmethod
    def build_schema_context(selected_tables) -> str:
        """Render a structured schema block for the given tables.

        Parameters
        ----------
        selected_tables:
            An iterable of table-name strings, **or** ``None``, **or** an
            empty sequence (``()``, ``[]``).  When the value is falsy
            (``None``, empty list, empty tuple), *all* known tables are
            included.  Table names not present in
            :data:`~schema_data.columns.TABLE_COLUMNS` are silently skipped.

        Returns
        -------
        str
            Multi-line string with one section per table::

                Table: Contract
                Description: Records every completed trade on the exchange.
                Columns:
                  - ContractID: Surrogate primary key
                  - Volume: Number of lots traded
                  ...

            Sections are separated by a blank line.  Returns an empty string
            when ``selected_tables`` is non-empty but none of the names exist
            in :data:`~schema_data.columns.TABLE_COLUMNS`.

        Examples
        --------
        >>> ctx = SchemaRegistry.build_schema_context(["Contract"])
        >>> ctx.startswith("Table: Contract")
        True

        >>> # None → include all tables
        >>> all_ctx = SchemaRegistry.build_schema_context(None)
        >>> "Table: Customer" in all_ctx
        True

        >>> # Empty tuple → same as None
        >>> SchemaRegistry.build_schema_context(()) == all_ctx
        True

        >>> # Unknown table silently skipped → empty string
        >>> SchemaRegistry.build_schema_context(["NonExistentTable"])
        ''
        """
        # None or empty sequence → include everything
        if not selected_tables:
            selected_tables = list(TABLE_COLUMNS.keys())

        lines = []

        for table_name in selected_tables:
            if table_name not in TABLE_COLUMNS:
                # silently skip unknown tables
                continue

            description = TABLE_DESCRIPTIONS.get(table_name, "")
            columns = TABLE_COLUMNS.get(table_name, {})

            lines.append(f"Table: {table_name}")

            if description:
                lines.append(f"Description: {description}")

            if columns:
                lines.append("Columns:")
                for col_name, col_desc in columns.items():
                    lines.append(f"  - {col_name}: {col_desc}")

            lines.append("")

        return "\n".join(lines)

    # Alias so tests and callers that use build_context() still work.
    build_context = build_schema_context

    @staticmethod
    def get_relationships(selected_tables: list[str]) -> list[str]:
        """Return JOIN SQL clauses for FK edges between *selected_tables*.

        An edge from :data:`~schema_data.relationships.RELATIONSHIPS` is
        included only when **both** its left-side and right-side tables
        appear in ``selected_tables``.  Edges where either endpoint is
        absent are silently omitted.

        Relationship keys follow the format ``"LeftTable -> RightTable"``
        (with optional schema prefix, e.g. ``"Contract.ContractID ->
        CustomerContract.ContractID"``).

        Parameters
        ----------
        selected_tables:
            List of table names that will be used in the query.  Order
            does not matter.  May be empty.

        Returns
        -------
        list[str]
            SQL JOIN snippets (one per relevant FK edge), e.g.::

                [
                    "JOIN [Auction_Dim].[Customer] ON "
                    "[Auction_Fact].[Contract].[CustomerID] = "
                    "[Auction_Dim].[Customer].[CustomerID]",
                    ...
                ]

            Returns an empty list when ``selected_tables`` is empty or
            when no registered edges connect the given tables.

        Examples
        --------
        >>> joins = SchemaRegistry.get_relationships(["Contract", "Customer"])
        >>> all(isinstance(j, str) for j in joins)
        True

        >>> # Single table → no edges
        >>> SchemaRegistry.get_relationships(["Contract"])
        []

        >>> # Empty list → no edges
        >>> SchemaRegistry.get_relationships([])
        []
        """
        selected = set(selected_tables)
        result = []

        for name, join_sql in RELATIONSHIPS.items():
            parts = name.split(" -> ")
            left  = parts[0].split(".")[0]
            right = parts[1].split(".")[0]

            if left in selected and right in selected:
                result.append(join_sql)

        return result
