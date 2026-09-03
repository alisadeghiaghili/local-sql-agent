# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Schema auto-discovery module.

Inspects a live database via SQLAlchemy and extracts table metadata,
column info, FK relationships, row counts, and sample values.  The
extracted data can be serialised into draft YAML files that seed
``project_config/entities.yaml``, ``project_config/aliases.yaml``,
``project_config/relationships.yaml``, and ``project_config/schema.yaml``.

Design goals
------------
* **Lazy connection** — importing this module never opens a DB connection.
  A connection is established only when :meth:`SchemaInspector.inspect` is
  called.
* **Dialect-agnostic** — uses ``sqlalchemy.inspect()`` exclusively; no
  raw SQL for schema introspection so it works with MSSQL, PostgreSQL,
  MySQL, and SQLite.
* **Safe sample extraction** — NULLs, binary blobs, and very long values
  are sanitised before being written to YAML.

Typical usage::

    from database.schema_inspector import SchemaInspector

    inspector = SchemaInspector("mssql+pyodbc://...")
    schema = inspector.inspect(include_schemas=["Auction_Dim", "Auction_Fact"])
    entities_yaml      = inspector.draft_entities_yaml(schema)
    aliases_yaml       = inspector.draft_aliases_yaml(schema)
    relationships_yaml = inspector.draft_relationships_yaml(schema)
    schema_yaml        = inspector.draft_schema_yaml(schema)
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ColumnInfo:
    name: str
    type: str          # normalised type string, e.g. "int", "varchar", "decimal"
    is_pk: bool = False
    is_nullable: bool = True
    sample_values: list[str] = field(default_factory=list)


@dataclass
class ForeignKeyInfo:
    column: str                  # local column name
    referred_schema: str | None  # remote schema (may be None)
    referred_table: str          # remote table name
    referred_column: str         # remote column name

    @property
    def reference_str(self) -> str:
        if self.referred_schema:
            return f"{self.referred_schema}.{self.referred_table}.{self.referred_column}"
        return f"{self.referred_table}.{self.referred_column}"


@dataclass
class TableInfo:
    name: str
    schema: str | None
    columns: list[ColumnInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)
    row_count: int | None = None
    classification: str = "unknown"   # "fact" | "dim" | "staging" | "log" | "unknown"

    @property
    def full_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    @property
    def pk_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.is_pk]


@dataclass
class RelationshipInfo:
    from_table: str
    from_schema: str | None
    from_column: str
    to_table: str
    to_schema: str | None
    to_column: str

    @property
    def join_hint(self) -> str:
        from_ref = f"{self.from_schema}.{self.from_table}" if self.from_schema else self.from_table
        to_ref   = f"{self.to_schema}.{self.to_table}"   if self.to_schema   else self.to_table
        return (
            f"JOIN {to_ref} ON "
            f"{from_ref}.{self.from_column} = {to_ref}.{self.to_column}"
        )


@dataclass
class SchemaSnapshot:
    tables: list[TableInfo] = field(default_factory=list)
    relationships: list[RelationshipInfo] = field(default_factory=list)
    source_url: str = ""
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds"))

    @property
    def fact_tables(self) -> list[TableInfo]:
        return [t for t in self.tables if t.classification == "fact"]

    @property
    def dim_tables(self) -> list[TableInfo]:
        return [t for t in self.tables if t.classification == "dim"]


# ---------------------------------------------------------------------------
# Type normalisation
# ---------------------------------------------------------------------------

def _normalise_type(sa_type: Any) -> str:
    """Convert a SQLAlchemy type object to a short lowercase string."""
    raw = str(sa_type).lower()
    for prefix, norm in [
        ("varchar", "varchar"),
        ("nvarchar", "nvarchar"),
        ("char",    "char"),
        ("text",    "text"),
        ("int",     "int"),
        ("bigint",  "bigint"),
        ("smallint","smallint"),
        ("tinyint", "tinyint"),
        ("numeric", "decimal"),
        ("decimal", "decimal"),
        ("float",   "float"),
        ("real",    "float"),
        ("double",  "float"),
        ("money",   "decimal"),
        ("bool",    "boolean"),
        ("bit",     "boolean"),
        ("date",    "date"),
        ("time",    "time"),
        ("uuid",    "uuid"),
        ("blob",    "binary"),
        ("binary",  "binary"),
        ("varbinary","binary"),
        ("image",   "binary"),
    ]:
        if prefix in raw:
            return norm
    return raw.split("(")[0].strip() or "unknown"


def _is_string_type(norm_type: str) -> bool:
    return norm_type in ("varchar", "nvarchar", "char", "text")


def _yaml_str(s: str) -> str:
    """Render *s* as a double-quoted YAML flow scalar.

    ``json.dumps`` produces exactly that (JSON double-quoted strings are a
    valid YAML 1.1/1.2 flow scalar, escapes and all) -- reused here instead
    of a hand-rolled quoting routine so values containing quotes,
    backslashes, or non-ASCII text (e.g. sample values or descriptions
    pulled from live rows) always round-trip through ``yaml.safe_load``.
    """
    return json.dumps(s, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Classification heuristics
# ---------------------------------------------------------------------------

_FACT_NAME_PATTERNS     = re.compile(r"fact|transaction|event|sale|order|contract|offer", re.I)
_DIM_NAME_PATTERNS      = re.compile(r"dim|lookup|ref|type|status|kind|master|list", re.I)
_STAGING_NAME_PATTERNS  = re.compile(r"stag|staging|temp|tmp|stage", re.I)
_LOG_NAME_PATTERNS      = re.compile(r"log|audit|history|archive", re.I)


def _classify_table(table: TableInfo) -> str:
    """Apply name and structure heuristics to classify a table."""
    name = table.name

    # Name-based signals (highest priority)
    if _STAGING_NAME_PATTERNS.search(name):
        return "staging"
    if _LOG_NAME_PATTERNS.search(name):
        return "log"
    if _FACT_NAME_PATTERNS.search(name):
        return "fact"
    if _DIM_NAME_PATTERNS.search(name):
        return "dim"

    # Structure-based heuristics
    col_types   = [c.type for c in table.columns]
    n_numeric   = sum(1 for t in col_types if t in ("int","bigint","decimal","float","smallint","tinyint"))
    n_fk        = len(table.foreign_keys)
    n_cols      = len(table.columns)

    if n_fk >= 3 and n_numeric >= 2:
        return "fact"
    if n_fk <= 1 and n_cols <= 8:
        return "dim"
    if n_fk >= 2:
        return "fact"
    return "unknown"


# ---------------------------------------------------------------------------
# Sample value extraction
# ---------------------------------------------------------------------------

_MAX_SAMPLE_LEN = 50


def _safe_str(val: Any) -> str | None:
    """Convert a DB value to a safe short string, or return None to skip."""
    if val is None:
        return None
    if isinstance(val, (bytes, bytearray)):
        return None  # skip binary
    s = str(val).strip()
    if not s:
        return None
    return s[:_MAX_SAMPLE_LEN]


def _fetch_samples(
    engine: Engine,
    schema: str | None,
    table_name: str,
    column_name: str,
    limit: int,
) -> list[str]:
    """Fetch up to *limit* distinct non-NULL values from a column."""
    if schema:
        full = f"{schema}.{table_name}"
    else:
        full = table_name

    # Quote identifiers with double-quotes (ANSI SQL; works on all dialects)
    col_q  = f'"{column_name}"'
    tbl_q  = ".".join(f'"{p}"' for p in full.split("."))

    sql = text(
        f"SELECT DISTINCT {col_q} "
        f"FROM {tbl_q} "
        f"WHERE {col_q} IS NOT NULL "
        f"ORDER BY {col_q} "
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql).fetchmany(limit)
        values = []
        for (val,) in rows:
            s = _safe_str(val)
            if s is not None:
                values.append(s)
        return values
    except Exception as exc:  # noqa: BLE001
        logger.debug("Sample fetch failed for %s.%s: %s", full, column_name, exc)
        return []


def _fetch_row_count(
    engine: Engine,
    schema: str | None,
    table_name: str,
) -> int | None:
    if schema:
        full = f'"{schema}"."{table_name}"'
    else:
        full = f'"{table_name}"'
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT COUNT(*) FROM {full}"))
            return result.scalar()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Row count failed for %s: %s", full, exc)
        return None


# ---------------------------------------------------------------------------
# Main inspector class
# ---------------------------------------------------------------------------

class SchemaInspector:
    """Inspect a live database and return a :class:`SchemaSnapshot`.

    Parameters
    ----------
    db_url:
        SQLAlchemy connection string.  No connection is made until
        :meth:`inspect` is called.
    sample_rows:
        How many distinct sample values to fetch per string column.
        Set to 0 to skip sampling entirely.
    """

    def __init__(self, db_url: str, sample_rows: int = 10) -> None:
        self._db_url     = db_url
        self._sample_rows = sample_rows
        self._engine: Engine | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_engine(self) -> Engine:
        if self._engine is None:
            logger.info("Connecting to database for schema inspection")
            try:
                self._engine = create_engine(
                    self._db_url,
                    pool_pre_ping=True,
                    pool_size=1,
                    max_overflow=0,
                    echo=False,
                )
                # Verify connectivity
                with self._engine.connect():
                    pass
                logger.info("Connection successful")
            except OperationalError as exc:
                raise ConnectionError(
                    f"Cannot connect to database: {exc.orig or exc}"
                ) from exc
        return self._engine

    def close(self) -> None:
        """Dispose the inspection engine (does not affect the main app engine)."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def inspect(
        self,
        include_schemas: list[str] | None = None,
        exclude_tables: list[str] | None = None,
        fetch_row_counts: bool = True,
    ) -> SchemaSnapshot:
        """Run full schema inspection and return a :class:`SchemaSnapshot`.

        Parameters
        ----------
        include_schemas:
            If given, only tables in these schemas are processed.
        exclude_tables:
            Table names (without schema) to skip.
        fetch_row_counts:
            Whether to issue ``COUNT(*)`` per table (can be slow on large DBs).
        """
        engine    = self._get_engine()
        inspector = sa_inspect(engine)
        exclude   = set(exclude_tables or [])

        snapshot = SchemaSnapshot(source_url=self._redact_url(self._db_url))

        schema_list = include_schemas or inspector.get_schema_names()
        # Filter out system/internal schemas
        schema_list = [
            s for s in schema_list
            if s not in ("information_schema", "sys", "pg_catalog", "pg_toast")
            and not s.startswith("pg_")
        ]

        total = 0
        for schema in schema_list:
            table_names = inspector.get_table_names(schema=schema)
            for tname in table_names:
                if tname in exclude:
                    continue
                total += 1
                print(f"  Inspecting {schema}.{tname} ...", file=sys.stderr)
                table = self._inspect_table(
                    engine, inspector, schema, tname, fetch_row_counts
                )
                table.classification = _classify_table(table)
                snapshot.tables.append(table)

        # Build relationship list from all FKs
        for table in snapshot.tables:
            for fk in table.foreign_keys:
                snapshot.relationships.append(
                    RelationshipInfo(
                        from_table=table.name,
                        from_schema=table.schema,
                        from_column=fk.column,
                        to_table=fk.referred_table,
                        to_schema=fk.referred_schema,
                        to_column=fk.referred_column,
                    )
                )

        print(
            f"  Done. {total} tables | "
            f"{len(snapshot.fact_tables)} fact | "
            f"{len(snapshot.dim_tables)} dim",
            file=sys.stderr,
        )
        return snapshot

    # ------------------------------------------------------------------
    # Table-level inspection
    # ------------------------------------------------------------------

    def _inspect_table(
        self,
        engine: Engine,
        inspector: Any,
        schema: str,
        table_name: str,
        fetch_row_counts: bool,
    ) -> TableInfo:
        table = TableInfo(name=table_name, schema=schema)

        # Primary keys
        try:
            pk_info  = inspector.get_pk_constraint(table_name, schema=schema)
            pk_cols  = set(pk_info.get("constrained_columns", []))
        except Exception:  # noqa: BLE001
            pk_cols = set()

        # Columns
        try:
            raw_cols = inspector.get_columns(table_name, schema=schema)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cannot read columns for %s.%s: %s", schema, table_name, exc)
            raw_cols = []

        for col in raw_cols:
            cname    = col["name"]
            norm_t   = _normalise_type(col["type"])
            samples: list[str] = []
            if self._sample_rows > 0 and _is_string_type(norm_t):
                samples = _fetch_samples(
                    engine, schema, table_name, cname, self._sample_rows
                )
            table.columns.append(
                ColumnInfo(
                    name=cname,
                    type=norm_t,
                    is_pk=(cname in pk_cols),
                    is_nullable=col.get("nullable", True),
                    sample_values=samples,
                )
            )

        # Foreign keys
        try:
            raw_fks = inspector.get_foreign_keys(table_name, schema=schema)
        except Exception:  # noqa: BLE001
            raw_fks = []

        for fk in raw_fks:
            for local_col, ref_col in zip(
                fk.get("constrained_columns", []),
                fk.get("referred_columns", []),
            ):
                table.foreign_keys.append(
                    ForeignKeyInfo(
                        column=local_col,
                        referred_schema=fk.get("referred_schema"),
                        referred_table=fk.get("referred_table", ""),
                        referred_column=ref_col,
                    )
                )

        # Row count
        if fetch_row_counts:
            table.row_count = _fetch_row_count(engine, schema, table_name)

        return table

    # ------------------------------------------------------------------
    # YAML draft generators
    # ------------------------------------------------------------------

    def draft_entities_yaml(self, snapshot: SchemaSnapshot) -> str:
        """Return a draft ``entities.yaml`` string from *snapshot*."""
        lines: list[str] = [
            "# AUTO-GENERATED DRAFT - review and edit before moving to project_config/",
            f"# Generated: {snapshot.generated_at} from {snapshot.source_url}",
            f"# Tables found: {len(snapshot.tables)} | "
            f"Fact tables: {len(snapshot.fact_tables)} | "
            f"Dim tables: {len(snapshot.dim_tables)}",
            "",
            "entities:",
        ]

        for table in snapshot.tables:
            entity_name = table.name
            lines.append(f"  {entity_name}:")
            lines.append(f'    table: "{table.name}"')
            if table.schema:
                lines.append(f'    schema: "{table.schema}"')
            lines.append(f'    classification: "{table.classification}"  # auto-detected')
            if table.row_count is not None:
                lines.append(f"    row_count: {table.row_count}")
            lines.append("    aliases: []  # TO BE FILLED: add natural language aliases")
            lines.append("    columns:")
            for col in table.columns:
                lines.append(f"      - name: \"{col.name}\"")
                lines.append(f"        type: \"{col.type}\"")
                if col.is_pk:
                    lines.append("        is_pk: true")
                if col.sample_values:
                    samples_yaml = ", ".join(f'"{v}"' for v in col.sample_values[:5])
                    lines.append(f"        sample_values: [{samples_yaml}]")
            if table.foreign_keys:
                lines.append("    foreign_keys:")
                for fk in table.foreign_keys:
                    lines.append(f"      - column: \"{fk.column}\"")
                    lines.append(f"        references: \"{fk.reference_str}\"")
            lines.append("")

        return "\n".join(lines)

    def draft_aliases_yaml(self, snapshot: SchemaSnapshot) -> str:
        """Return a draft ``aliases.yaml`` string from *snapshot*.

        The output is intentionally sparse: column names and sample values
        are listed as raw material for a human (or LLM) to curate into
        proper ``ring_aliases`` and ``synonyms`` mappings.
        """
        lines: list[str] = [
            "# AUTO-GENERATED DRAFT - review and edit before moving to project_config/",
            f"# Generated: {snapshot.generated_at} from {snapshot.source_url}",
            "# This file lists raw column names and sample values as synonym seeds.",
            "# Review each entry and move curated terms to aliases.yaml.",
            "",
            "# ring_aliases: canonical entity name -> surface-form list",
            "# Populate from dim table names + aliases section in entities.yaml.",
            "ring_aliases: {}",
            "",
            "# synonyms: surface word -> canonical tokens",
            "# Seeds below are generated from column names and sample values.",
            "synonyms:",
        ]

        seen: set[str] = set()
        for table in snapshot.tables:
            for col in table.columns:
                if not _is_string_type(col.type):
                    continue
                for val in col.sample_values:
                    key = val.lower().strip()
                    if len(key) < 2 or key in seen:
                        continue
                    seen.add(key)
                    # Suggest the table name and column name as canonical tokens
                    tokens = list({
                        table.name.lower(),
                        col.name.lower().replace("_", " "),
                    })
                    tokens_yaml = ", ".join(f'"{t}"' for t in tokens)
                    lines.append(f"  # from {table.full_name}.{col.name}")
                    lines.append(f'  "{key}": [{tokens_yaml}]  # REVIEW')

        return "\n".join(lines)

    def draft_schema_yaml(self, snapshot: SchemaSnapshot) -> str:
        """Return a draft ``schema.yaml`` string from *snapshot*.

        The output mirrors exactly the shape :class:`schema_data.registry.
        SchemaConfig` validates: a top-level ``tables`` map (each entry has
        an optional ``description`` and an optional ``columns`` map) and a
        top-level ``relationships`` list (each entry has ``from_table``,
        ``to_table``, ``join_sql``). This is verified by round-tripping the
        generated text through ``yaml.safe_load`` +
        ``SchemaConfig.model_validate`` (see ``tests/test_schema_inspector.py``)
        rather than merely asserted here.

        A table with no columns detected is emitted WITHOUT a ``columns``
        key at all (not an empty map) — per ``schema_data/registry.py``'s
        documented split, that is what keeps a table described-but-not-
        queryable, the same state a hand-written lookup-table entry would
        be in.

        .. warning::
           Column notes may quote **sample values fetched from live rows**
           (see :attr:`ColumnInfo.sample_values`) to help a human write a
           real description. The returned string therefore may contain
           real warehouse data and must only ever be written to a
           throwaway, git-ignored draft directory — never to
           ``project_config.example/`` or any other path tracked by git.
           This module never writes files itself (see
           :mod:`database.schema_inspector_cli`, which enforces the
           destination-directory guard); it only ever returns a string.
        """
        lines: list[str] = [
            "# AUTO-GENERATED DRAFT - review and edit before moving to project_config/",
            f"# Generated: {snapshot.generated_at} from {snapshot.source_url}",
            f"# Tables found: {len(snapshot.tables)} | "
            f"Fact tables: {len(snapshot.fact_tables)} | "
            f"Dim tables: {len(snapshot.dim_tables)}",
            "#",
            "# WARNING: descriptions below may quote sample values read from",
            "# live rows in this warehouse. Review before sharing this file, and",
            "# NEVER commit it or copy it into project_config.example/.",
            "",
            "tables:",
        ]

        for table in snapshot.tables:
            lines.append(f"  {_yaml_str(table.name)}:")
            desc = f"{table.full_name} — auto-detected {table.classification} table"
            if table.row_count is not None:
                desc += f" (~{table.row_count} rows)"
            lines.append(
                f"    description: {_yaml_str(desc)}  "
                "# TO BE FILLED: refine wording, add native-language synonyms"
            )
            if table.columns:
                lines.append("    columns:")
                for col in table.columns:
                    col_desc = f"{col.type} column"
                    if col.is_pk:
                        col_desc += "; primary key"
                    if col.sample_values:
                        examples = ", ".join(col.sample_values[:3])
                        col_desc += f"; e.g. {examples}"
                    lines.append(f"      {_yaml_str(col.name)}: {_yaml_str(col_desc)}")
            else:
                lines.append(
                    "    # no columns detected -- this table is described in the "
                    "prompt but NOT queryable until a `columns` map is added "
                    "(see schema_data/registry.py)"
                )
            lines.append("")

        lines.append("relationships:")
        for rel in snapshot.relationships:
            lines.append(f"  - from_table: {_yaml_str(rel.from_table)}")
            lines.append(f"    to_table: {_yaml_str(rel.to_table)}")
            lines.append(f"    join_sql: {_yaml_str(rel.join_hint)}")
            lines.append("")

        return "\n".join(lines)

    def draft_relationships_yaml(self, snapshot: SchemaSnapshot) -> str:
        """Return a draft ``relationships.yaml`` string from *snapshot*."""
        lines: list[str] = [
            "# AUTO-GENERATED DRAFT - review and edit before moving to project_config/",
            f"# Generated: {snapshot.generated_at} from {snapshot.source_url}",
            f"# Relationships found: {len(snapshot.relationships)}",
            "",
            "relationships:",
        ]
        for rel in snapshot.relationships:
            lines.append("  - from_table: \"" + rel.from_table + "\"")
            if rel.from_schema:
                lines.append("    from_schema: \"" + rel.from_schema + "\"")
            lines.append("    from_column: \"" + rel.from_column + "\"")
            lines.append("    to_table: \"" + rel.to_table + "\"")
            if rel.to_schema:
                lines.append("    to_schema: \"" + rel.to_schema + "\"")
            lines.append("    to_column: \"" + rel.to_column + "\"")
            lines.append("    join_hint: \"" + rel.join_hint + "\"")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _redact_url(url: str) -> str:
        """Remove password from a connection URL for safe logging."""
        return re.sub(r":[^:@/]+@", ":***@", url)
