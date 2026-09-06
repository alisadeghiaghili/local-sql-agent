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

Per-table schema qualifier and resolver/prefetch flags (Phase 4 finish)
-------------------------------------------------------------------------
Two more pieces of warehouse-specific metadata used to live as Python
literals in :mod:`retrieval.value_resolver` and
:mod:`retrieval.dimension_vocabulary` -- a hardcoded ``_SCHEMA`` constant
and two hand-maintained ``{table: (columns...)}`` dicts (``RESOLVABLE_COLUMNS``,
``PREFETCH_COLUMNS``) that had to be kept in sync with this file by hand.
Both are now per-table fields on :class:`TableDefinition`, read here instead:

* ``db_schema`` -- the schema/database qualifier a query must use for this
  table (e.g. ``"Auction_Dim"``), via :func:`get_table_schema_qualifiers`.
  A per-*table* field, not one global constant, because a real warehouse
  routinely has more than one schema (this one has at least ``Auction_Dim``
  and ``Auction_Fact``) -- a single shared literal would be the wrong shape
  even before portability is considered.
* ``resolvable_columns`` -- columns :func:`~retrieval.value_resolver.resolve_value`
  is allowed to query for this table, via :func:`get_resolvable_columns`.
* ``prefetchable_columns`` -- columns
  :mod:`retrieval.dimension_vocabulary` is allowed to prefetch the entire
  vocabulary of, via :func:`get_prefetchable_columns`.

Both flags live on the column's own table entry (next to ``columns``, the
same map they are validated against), not as a second, separately-authored
list elsewhere -- see :class:`SchemaConfig`'s validator. A column named in
either list that is not also a key of that table's ``columns`` map fails
``schema.yaml`` validation outright; a table that flags either list non-empty
without also giving ``db_schema`` fails the same way (there is no schema
qualifier to build a query with otherwise). This is what keeps the derived
allowlists from ever drifting out of sync with ``schema.yaml``'s own
``columns`` map -- there is exactly one place a column is declared to exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from knowledge.config_loader import ConfigNotFoundError, load_yaml

__all__ = [
    "SchemaRegistry",
    "ConfigNotFoundError",
    "TableDefinition",
    "RelationshipDefinition",
    "SchemaConfig",
    "load_schema",
    "validate_schema_yaml_text",
    "get_table_descriptions",
    "get_table_columns",
    "get_relationships_map",
    "get_table_schema_qualifiers",
    "get_resolvable_columns",
    "get_prefetchable_columns",
    "check_allowlist_structural_invariants",
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

    ``db_schema``, ``resolvable_columns``, and ``prefetchable_columns`` are
    all optional and default to "not used by that feature" (``""`` / empty
    tuple) -- a table needs none of them merely to be described or
    queryable. See the module docstring's "Per-table schema qualifier and
    resolver/prefetch flags" section, and :class:`SchemaConfig`'s validator
    for the consistency rule tying them to ``columns``.
    """

    description: str = ""
    columns: dict[str, str] | None = None
    db_schema: str = ""
    resolvable_columns: tuple[str, ...] = Field(default_factory=tuple)
    prefetchable_columns: tuple[str, ...] = Field(default_factory=tuple)


class RelationshipDefinition(BaseModel):
    """One entry under ``schema.yaml``'s ``relationships`` list."""

    from_table: str
    to_table: str
    join_sql: str


class SchemaConfig(BaseModel):
    """Validated, top-level shape of ``schema.yaml``."""

    tables: dict[str, TableDefinition] = Field(default_factory=dict)
    relationships: list[RelationshipDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _resolvable_and_prefetchable_columns_are_consistent(self) -> "SchemaConfig":
        """Ties ``resolvable_columns``/``prefetchable_columns`` to ``columns``.

        Two rules, checked per table:

        1. Every name in either list must also be a key of that table's
           ``columns`` map -- a column cannot be flagged resolvable or
           prefetchable without first existing as a real, described column.
           This is the mechanism that keeps the derived allowlists
           (:func:`get_resolvable_columns` / :func:`get_prefetchable_columns`)
           from ever drifting out of sync with ``schema.yaml``'s own column
           list: there is exactly one place a column is declared, and these
           flags can only ever narrow it, never extend it.
        2. A table that flags either list non-empty must also give a
           non-empty ``db_schema`` -- without it there is no schema
           qualifier to build a ``[schema].[table]`` reference with, and
           :mod:`retrieval.value_resolver` / :mod:`retrieval.dimension_vocabulary`
           would have nothing to look up at query-build time.

        Raising ``ValueError`` here (rather than a plain assertion) is
        deliberate -- Pydantic wraps it into the same
        :class:`~pydantic.ValidationError` :func:`load_schema` already
        catches and reformats as ``"[schema.yaml] validation error at ...: ..."``,
        so a schema.yaml author sees exactly the same error shape for this
        mistake as for any other validation failure in this file.
        """
        for name, table in self.tables.items():
            flagged = set(table.resolvable_columns) | set(table.prefetchable_columns)
            if not flagged:
                continue
            known_columns = set(table.columns or {})
            unknown = sorted(flagged - known_columns)
            if unknown:
                raise ValueError(
                    f"table '{name}': resolvable_columns/prefetchable_columns "
                    f"name column(s) {unknown} that are not in this table's "
                    f"`columns` map"
                )
            if not table.db_schema:
                raise ValueError(
                    f"table '{name}': resolvable_columns/prefetchable_columns "
                    f"is set but `db_schema` is empty -- a schema qualifier is "
                    f"required to build a query for this table"
                )
        return self


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
    return _validate_schema_raw(raw)


def _validate_schema_raw(raw: dict) -> SchemaConfig:
    try:
        return SchemaConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = " -> ".join(str(x) for x in first["loc"])
        raise ValueError(
            f"[schema.yaml] validation error at '{field}': {first['msg']}"
        ) from exc


def validate_schema_yaml_text(text: str) -> SchemaConfig:
    """Validate ``schema.yaml`` *text* directly, raising the exact same
    ``"[schema.yaml] validation error at ...'"`` :class:`ValueError`
    :func:`load_schema` would for the same content on disk -- without
    touching the filesystem or ``cfg.settings.project_config_dir`` at all.

    Mirrors :func:`knowledge.config_loader.validate_yaml_text` exactly --
    see that function's docstring for why ``appdb.config_versions`` needs
    this in-memory validation path rather than
    :func:`config.override_settings` plus a temp file: that context
    manager mutates a single process-wide setting and is documented as a
    test-only tool, unsafe to use from concurrent request-handling code.

    Examples
    --------
    >>> validate_schema_yaml_text("tables: {}").tables
    {}

    >>> validate_schema_yaml_text(
    ...     "relationships: [{from_table: A, to_table: B}]"
    ... )
    Traceback (most recent call last):
        ...
    ValueError: [schema.yaml] validation error at 'relationships -> 0 -> join_sql': Field required
    """
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"[schema.yaml] {exc}") from exc
    return _validate_schema_raw(raw)


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
        _cache["table_schemas"] = {
            name: table.db_schema for name, table in cfg.tables.items() if table.db_schema
        }
        _cache["resolvable_columns"] = {
            name: table.resolvable_columns
            for name, table in cfg.tables.items()
            if table.resolvable_columns
        }
        _cache["prefetchable_columns"] = {
            name: table.prefetchable_columns
            for name, table in cfg.tables.items()
            if table.prefetchable_columns
        }
        _cache["_loaded"] = True
    return _cache


def get_table_descriptions() -> dict[str, str]:
    """Return ``{table_name: description}`` for every table in ``schema.yaml``."""
    return _schema_cache()["table_descriptions"]


def get_table_schema_qualifiers() -> dict[str, str]:
    """Return ``{table_name: db_schema}`` for every table that sets one.

    A table with no ``db_schema`` in ``schema.yaml`` (the common case for a
    table that is only ever described, not resolved/prefetched against) is
    simply absent here rather than mapped to ``""`` -- callers that need a
    schema qualifier for a specific table (:mod:`retrieval.value_resolver`,
    :mod:`retrieval.dimension_vocabulary`) only ever look up tables that
    :func:`get_resolvable_columns` / :func:`get_prefetchable_columns` already
    guarantee have one (see :class:`SchemaConfig`'s validator).
    """
    return _schema_cache()["table_schemas"]


def get_resolvable_columns() -> dict[str, tuple[str, ...]]:
    """Return ``{table_name: (column, ...)}`` for every table's
    ``resolvable_columns`` -- ``retrieval.value_resolver.resolve_value``'s
    allowlist. A table with no ``resolvable_columns`` in ``schema.yaml`` is
    absent here (not mapped to ``()``), matching :func:`get_table_columns`'s
    own "absent, not empty" convention for a table with no ``columns`` key.
    """
    return _schema_cache()["resolvable_columns"]


def get_prefetchable_columns() -> dict[str, tuple[str, ...]]:
    """Return ``{table_name: (column, ...)}`` for every table's
    ``prefetchable_columns`` -- :mod:`retrieval.dimension_vocabulary`'s
    prefetch set. A table with no ``prefetchable_columns`` in ``schema.yaml``
    is absent here (not mapped to ``()``), same convention as
    :func:`get_resolvable_columns`.
    """
    return _schema_cache()["prefetchable_columns"]


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


def check_allowlist_structural_invariants(
    table_columns: dict[str, dict[str, str]],
    table_descriptions: dict[str, str],
    relationships: dict[str, str],
) -> list[str]:
    """Schema-agnostic invariants the guard's allowlist must hold, regardless
    of which real ``schema.yaml`` produced it.

    The single source of truth for the checks
    ``tests/test_schema_registry_snapshot.py``'s ``TestAllowlistStructuralInvariants``
    pins against the process's own loaded registry -- that test class
    calls this function rather than duplicating the checks, and
    ``appdb.config_versions`` calls it a second time, against a
    *candidate* ``schema.yaml`` that has not been applied yet, before a
    security admin's edit can ever reach the guard (admin panel phase 3,
    spec §4 item 2). Neither caller reimplements the other's logic.

    Parameters
    ----------
    table_columns:
        ``{table: {column: description}}`` -- normally
        :func:`get_table_columns`'s return value, or the equivalent
        derived from a candidate, not-yet-applied ``schema.yaml``.
    table_descriptions:
        ``{table: description}`` for every *described* table, allowlisted
        or not -- normally :func:`get_table_descriptions`.
    relationships:
        ``{"FromTable -> ToTable": join_sql}`` -- normally
        :func:`get_relationships_map`.

    Returns
    -------
    list[str]
        One human-readable violation description per problem found, empty
        when every invariant holds.

    Examples
    --------
    >>> check_allowlist_structural_invariants(
    ...     {"Widget": {"ID": "primary key"}},
    ...     {"Widget": "a test table"},
    ...     {},
    ... )
    []

    A table with an empty ``columns`` map has no business carrying the key
    at all:

    >>> check_allowlist_structural_invariants(
    ...     {"Widget": {}}, {"Widget": "a test table"}, {},
    ... )
    ["table 'Widget' has a `columns` key but no columns"]

    An allowlisted table must also be a described one:

    >>> check_allowlist_structural_invariants(
    ...     {"Widget": {"ID": "pk"}}, {}, {},
    ... )
    ["allowlisted table(s) not described: ['Widget']"]
    """
    violations: list[str] = []

    if len(table_columns) == 0:
        violations.append("the guard allowlist is empty -- no table carries a `columns` key")

    for table, columns in table_columns.items():
        if len(columns) == 0:
            violations.append(f"table '{table}' has a `columns` key but no columns")
            continue
        names = list(columns)
        if not all(name.strip() for name in names):
            violations.append(f"table '{table}' has a blank column name")
        if len(names) != len(set(names)):
            violations.append(f"table '{table}' has a duplicate column name")

    undescribed = sorted(set(table_columns) - set(table_descriptions))
    if undescribed:
        violations.append(f"allowlisted table(s) not described: {undescribed}")

    for key in relationships:
        left_table = key.split(" -> ")[0].split(".")[0]
        if left_table not in table_descriptions:
            violations.append(f"relationship {key!r}: unknown left table {left_table!r}")

    return violations


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
