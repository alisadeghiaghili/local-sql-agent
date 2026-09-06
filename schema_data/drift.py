# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Schema drift — a read-only comparison of ``schema.yaml`` against the
live warehouse (admin panel phase 6, §2).

The warehouse gains and loses columns; ``schema.yaml`` does not follow
automatically, and because it is the guard's allowlist
(:mod:`security.sql_guard`, via :mod:`schema_data.registry`), the symptom
of drift is *questions that mysteriously stop working*, not an error
anybody can search for. :func:`check_schema_drift` reports three sets:

* **warehouse_only** — a table or column the live warehouse has that
  ``schema.yaml`` does not: currently unqueryable (the guard will refuse
  any reference to it, correctly, since it is not in the allowlist).
* **schema_only** — a table or column ``schema.yaml`` describes as
  queryable that the live warehouse no longer has: a generated query
  referencing it will fail at execution.
* **type_changed** — a column present on both sides whose live type has
  changed since the last time this check ran.

It never writes to ``schema.yaml``, and it never applies anything —
applying a ``schema.yaml`` change is a security-admin action through
phase 3's propose-and-approve flow (:mod:`appdb.config_versions`); this
module's output is at most the input to that, a draft. It reads the
warehouse through the SAME read-only connection every query already uses
(:func:`database.connection.get_engine`, or an injected engine for
testing) — nothing here needs, or accepts, a separate or more privileged
credential.

Why "type_changed" cannot compare against ``schema.yaml`` itself
---------------------------------------------------------------------
``schema.yaml``'s ``columns`` map is ``{column_name: free-text
description}`` (see :mod:`schema_data.registry` and
``project_config.example/schema.yaml``'s own comments) — there is no
structured "type" field anywhere in that file for a live type to be
compared against. Rather than parse free text written by a human for a
different purpose (fragile, and wrong exactly when it matters —
mismatched between a hand-edited description and the real column), this
module keeps its OWN small, read-only-of-schema.yaml baseline: the live
type observed on the *previous* call to this function, persisted to a
tiny JSON file next to the other operational logs
(:attr:`config.Settings.log_dir`). "Type changed" then means "changed
since the last time an operator ran this check" — the same kind of
own-bookkeeping-not-a-copy discipline
``retrieval.dimension_vocabulary``'s freshness tracking already uses.
A first run has no prior baseline to compare against, so it reports zero
type changes and establishes one — this is a fact about the tool's own
history, stated plainly in the result (``baseline_available``), not
concealed as "nothing changed".
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

import config as cfg
from schema_data.registry import get_table_columns, get_table_schema_qualifiers

logger = logging.getLogger(__name__)

# Module-level path variable so tests can patch
# "schema_data.drift._DRIFT_BASELINE_FILE", mirroring
# appdb.admin_audit._ADMIN_ACTION_LOG_FILE's own test seam.
_DRIFT_BASELINE_FILE: str = ""


def _drift_baseline_file() -> str:
    if _DRIFT_BASELINE_FILE:
        return _DRIFT_BASELINE_FILE
    return os.path.join(cfg.settings.log_dir, "schema_drift_baseline.json")


def _normalise_type(sa_type: Any) -> str:
    """A short, stable, lowercase token for a SQLAlchemy column type --
    stable enough to compare across two calls against the same physical
    column, which is all this module needs from it (unlike
    ``database.schema_inspector``'s own normaliser, this one makes no
    claim about matching any particular vocabulary a human would author)."""
    return str(sa_type).split("(")[0].strip().lower() or "unknown"


@dataclass(frozen=True)
class SchemaDriftReport:
    """The outcome of one :func:`check_schema_drift` call."""

    checked_at: str
    schemas_scanned: tuple[str, ...]
    warehouse_only: tuple[str, ...]
    schema_only: tuple[str, ...]
    type_changed: tuple[dict[str, str], ...]
    unverifiable_tables: tuple[str, ...]
    baseline_available: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "schemas_scanned": list(self.schemas_scanned),
            "warehouse_only": list(self.warehouse_only),
            "schema_only": list(self.schema_only),
            "type_changed": list(self.type_changed),
            "unverifiable_tables": list(self.unverifiable_tables),
            "baseline_available": self.baseline_available,
        }


def _load_baseline() -> dict[str, str] | None:
    path = _drift_baseline_file()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("schema_data.drift: could not read baseline at %s: %s", path, exc)
        return None
    types = data.get("types")
    return types if isinstance(types, dict) else None


def _save_baseline(types: dict[str, str]) -> None:
    path = _drift_baseline_file()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "types": types,
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("schema_data.drift: could not write baseline at %s: %s", path, exc)


def check_schema_drift(engine: Engine | None = None, *, persist_baseline: bool = True) -> SchemaDriftReport:
    """Compare ``schema.yaml``'s queryable tables/columns against the live
    warehouse. Read-only in every direction: never writes ``schema.yaml``,
    and reads the warehouse purely through SQLAlchemy's catalogue
    reflection (``inspect(engine).get_table_names()``/``get_columns()``),
    the same metadata-only queries a read-only login already supports.

    Parameters
    ----------
    engine:
        Defaults to :func:`database.connection.get_engine` -- the SAME
        read-only warehouse connection every query already runs through.
        Inject a fixture engine to test against a throwaway database with
        no elevated credentials of any kind (there is no parameter here
        through which one could even be supplied).
    persist_baseline:
        Whether to overwrite this tool's own type baseline with what was
        just observed. ``True`` by default; a caller that wants to run
        the check without moving the baseline forward (this module's own
        tests exercising a live-then-live comparison) passes ``False``.

    Returns
    -------
    SchemaDriftReport
    """
    table_columns = get_table_columns()
    table_schemas = get_table_schema_qualifiers()

    schemas_scanned = sorted({s for t, s in table_schemas.items() if t in table_columns and s})
    # Tables SQLAlchemy sees with no schema qualifier at all (SQLite, or
    # any dialect whose default search path this deployment relies on) --
    # scanned under schema=None whenever at least one queryable table in
    # schema.yaml declares no db_schema either, so a single-schema
    # deployment (schema.yaml's own convention for one) is not silently
    # skipped entirely.
    scan_default_schema = any(t in table_columns and not table_schemas.get(t) for t in table_columns)

    if engine is None:
        from database.connection import get_engine

        engine = get_engine()

    inspector = sa_inspect(engine)

    # {qualified_table_name: {column: type}} -- qualified by schema.yaml's
    # own table name only (never re-prefixed with the schema), matching
    # how schema.yaml itself names tables.
    live: dict[str, dict[str, str]] = {}
    scan_targets = list(schemas_scanned) + ([None] if scan_default_schema else [])
    for schema_name in scan_targets:
        try:
            table_names = inspector.get_table_names(schema=schema_name)
        except Exception as exc:  # noqa: BLE001 - a schema this login cannot see is not a crash
            logger.warning(
                "schema_data.drift: could not list tables for schema %r: %s",
                schema_name, exc,
            )
            continue
        for table_name in table_names:
            try:
                columns = inspector.get_columns(table_name, schema=schema_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "schema_data.drift: could not read columns for %s.%s: %s",
                    schema_name, table_name, exc,
                )
                continue
            live.setdefault(table_name, {})
            for col in columns:
                live[table_name][col["name"]] = _normalise_type(col["type"])

    verifiable_tables = {
        t for t in table_columns if table_schemas.get(t) in schemas_scanned or (
            scan_default_schema and not table_schemas.get(t)
        )
    }
    unverifiable_tables = sorted(set(table_columns) - verifiable_tables)

    schema_col_ids: dict[str, str] = {}  # "Table.Column" -> (no type; presence only)
    for table in verifiable_tables:
        for column in table_columns[table]:
            schema_col_ids[f"{table}.{column}"] = table

    live_col_ids: dict[str, str] = {}
    for table, columns in live.items():
        for column, col_type in columns.items():
            live_col_ids[f"{table}.{column}"] = col_type

    # A whole table the warehouse has that schema.yaml never mentions at
    # all reports here too, one entry per column -- there is nothing in
    # schema_col_ids to diff it against, so every one of its columns
    # already falls out of this set difference on its own.
    warehouse_only = sorted(set(live_col_ids) - set(schema_col_ids))
    schema_only = sorted(set(schema_col_ids) - set(live_col_ids))

    baseline = _load_baseline() or {}
    type_changed: list[dict[str, str]] = []
    for col_id in sorted(set(schema_col_ids) & set(live_col_ids)):
        current_type = live_col_ids[col_id]
        previous_type = baseline.get(col_id)
        if previous_type is not None and previous_type != current_type:
            type_changed.append({
                "column": col_id, "previous_type": previous_type, "current_type": current_type,
            })

    if persist_baseline:
        _save_baseline(live_col_ids)

    return SchemaDriftReport(
        checked_at=datetime.now(timezone.utc).isoformat(),
        schemas_scanned=tuple(schemas_scanned),
        warehouse_only=tuple(warehouse_only),
        schema_only=tuple(schema_only),
        type_changed=tuple(type_changed),
        unverifiable_tables=tuple(unverifiable_tables),
        baseline_available=bool(baseline),
    )
