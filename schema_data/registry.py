# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Schema registry — single source of truth for table/column metadata.

Loads ``<PROJECT_CONFIG_DIR>/schema.yaml`` (``PROJECT_CONFIG_DIR`` defaults
to ``project_config``, git-ignored, real warehouse data — see
:attr:`config.Settings.project_config_dir`), the same directory and
"no automatic fallback to project_config.example/" discipline
:mod:`knowledge.config_loader` already applies to the aliases/entities/
business-rules/examples/metrics configs. A missing or invalid
``schema.yaml`` raises :class:`~knowledge.config_loader.ConfigNotFoundError`
or :class:`ValueError` only when the data is actually *accessed* (via
:class:`SchemaRegistry`, or by importing :mod:`schema_data.tables`,
:mod:`schema_data.columns`, or :mod:`schema_data.relationships`) — never
merely by importing this module.

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

``security.sql_guard`` derives its table/column allowlist from this same
data (via :data:`schema_data.columns.TABLE_COLUMNS`) — a table listed under
``schema.yaml``'s ``tables`` key with NO ``columns`` sub-key is described in
the prompt but is not queryable: it will never appear in
:func:`get_table_columns`'s return value, and the guard refuses any query
that references it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from knowledge.config_loader import ConfigNotFoundError, load_yaml

__all__ = [
    "SchemaRegistry",
    "ConfigNotFoundError",
    "TableDefinition",
    "RelationshipDefinition",
    "SchemaConfig",
    "load_schema",
    "get_table_descriptions",
    "get_table_columns",
    "get_relationships_map",
]


# ---------------------------------------------------------------------------
# Pydantic v2 models — mirrors the shape knowledge/config_loader.py uses for
# its own five configs (a validated model per YAML file).
# ---------------------------------------------------------------------------

class TableDefinition(BaseModel):
    """One entry under ``schema.yaml``'s ``tables`` key.

    ``columns`` is optional and, when absent (``None``), means the table is
    described in the prompt's schema block but is deliberately excluded
    from :func:`get_table_columns` and therefore from the SQL guard's table
    allowlist (see the module docstring).
    """

    description: str = ""
    columns: dict[str, str] | None = None


class RelationshipDefinition(BaseModel):
    """One entry under ``schema.yaml``'s ``relationships`` list."""

    from_table: str
    to_table: str
    join_sql: str


class SchemaConfig(BaseModel):
    """Validated, top-level shape of ``schema.yaml``."""

    tables: dict[str, TableDefinition] = Field(default_factory=dict)
    relationships: list[RelationshipDefinition] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _project_config_dir() -> Path:
    """Return the configured project-config directory, resolved at call time.

    Mirrors ``knowledge.config_loader._project_config_dir`` exactly (reads
    ``cfg.settings.project_config_dir`` fresh on every call rather than once
    at import time), so :func:`config.override_settings` and a changed
    ``PROJECT_CONFIG_DIR`` environment variable both take effect
    immediately.
    """
    import config as cfg  # deferred: avoids a hard import-time dependency

    return Path(cfg.settings.project_config_dir)


def load_schema() -> SchemaConfig:
    """Load and validate ``<PROJECT_CONFIG_DIR>/schema.yaml``.

    This is a plain, uncached loader — it re-reads and re-validates the
    file on every call, exactly like ``knowledge.config_loader``'s
    ``load_*`` functions. Callers that want a process-lifetime cache should
    use :func:`get_table_descriptions` / :func:`get_table_columns` /
    :func:`get_relationships_map` (or the lazy module attributes in
    :mod:`schema_data.tables` / :mod:`schema_data.columns` /
    :mod:`schema_data.relationships`) instead.

    Raises
    ------
    ConfigNotFoundError
        If ``schema.yaml`` does not exist under the configured directory.
        There is NO silent fallback to ``project_config.example/``.
    ValueError
        If the file exists but fails Pydantic validation.
    """
    path = _project_config_dir() / "schema.yaml"
    raw = load_yaml(path)
    try:
        return SchemaConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = " -> ".join(str(x) for x in first["loc"])
        raise ValueError(
            f"[schema.yaml] validation error at '{field}': {first['msg']}"
        ) from exc


# ---------------------------------------------------------------------------
# Process-lifetime cache — populated once, on first access, from load_schema().
# Mirrors knowledge/aliases.py's own ``_cache`` pattern.
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {}


def _schema_cache() -> dict[str, Any]:
    if "_loaded" not in _cache:
        cfg = load_schema()
        _cache["table_descriptions"] = {
            name: table.description for name, table in cfg.tables.items()
        }
        _cache["table_columns"] = {
            name: table.columns
            for name, table in cfg.tables.items()
            if table.columns
        }
        _cache["relationships"] = {
            f"{rel.from_table} -> {rel.to_table}": rel.join_sql
            for rel in cfg.relationships
        }
        _cache["_loaded"] = True
    return _cache


def get_table_descriptions() -> dict[str, str]:
    """Return ``{table_name: description}`` for every table in ``schema.yaml``."""
    return _schema_cache()["table_descriptions"]


def get_table_columns() -> dict[str, dict[str, str]]:
    """Return ``{table_name: {column_name: description}}``.

    Only includes tables that have a ``columns`` key in ``schema.yaml`` —
    this is also, by construction, the SQL guard's effective table
    allowlist (see :data:`schema_data.columns.TABLE_COLUMNS`).
    """
    return _schema_cache()["table_columns"]


def get_relationships_map() -> dict[str, str]:
    """Return ``{"FromTable -> ToTable": join_sql}`` for every relationship."""
    return _schema_cache()["relationships"]


class SchemaRegistry:
    """Stateless registry that renders schema and relationship data.

    All methods are static.  The class is a namespace — there is nothing
    to instantiate.

    Data sources
    ------------
    * :func:`get_table_columns` — ``{table: {col: desc}}``
    * :func:`get_table_descriptions` — ``{table: description}``
    * :func:`get_relationships_map` — ``{"A -> B": join_sql}``

    Both are read fresh (through the process-lifetime cache above) on every
    call, not captured at import time — importing this module, or
    :mod:`schema_data`, never requires ``schema.yaml`` to exist; only
    calling one of these two methods does.
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
            :func:`get_table_columns` are silently skipped.

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
            in :func:`get_table_columns`.

        Examples
        --------
        >>> ctx = SchemaRegistry.build_schema_context(["Customer"])
        >>> ctx.startswith("Table: Customer")
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
        table_columns = get_table_columns()
        table_descriptions = get_table_descriptions()

        # None or empty sequence → include everything
        if not selected_tables:
            selected_tables = list(table_columns.keys())

        lines = []

        for table_name in selected_tables:
            if table_name not in table_columns:
                # silently skip unknown tables
                continue

            description = table_descriptions.get(table_name, "")
            columns = table_columns.get(table_name, {})

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

        An edge from :func:`get_relationships_map` is included only when
        **both** its left-side and right-side tables appear in
        ``selected_tables``.  Edges where either endpoint is absent are
        silently omitted.

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

        for name, join_sql in get_relationships_map().items():
            parts = name.split(" -> ")
            left  = parts[0].split(".")[0]
            right = parts[1].split(".")[0]

            if left in selected and right in selected:
                result.append(join_sql)

        return result
