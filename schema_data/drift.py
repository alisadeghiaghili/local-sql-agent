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

A fourth finding concerns data sources, not columns:

* **misplaced_tables** — a table every one of whose ``schema.yaml``
  columns is missing from a data source it is assigned to (its
  ``datasource:``, or the default source when it has none), but which
  another configured source does have. Each entry carries a ``hint``
  such as ``stock_dim.Broker: not in sales, found in inventory — set
  datasource: inventory``. Only with more than one configured source;
  costs one ``INFORMATION_SCHEMA.TABLES`` query per source that has to be
  asked (:func:`database.catalogue.list_tables`), and only when such a
  table exists.

A table listed under several sources (``datasource: [A, B]``) is checked
in EACH of them. A column missing from one copy is reported as
``Table.Column [A]`` so the two copies stay apart.

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
from schema_data.registry import bare_table_name, get_table_columns, get_table_schema_qualifiers

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
    """The outcome of one :func:`check_schema_drift` call.

    ``misplaced_tables`` holds one dict per table found in a different data
    source than the one ``schema.yaml`` assigns it to: ``table``,
    ``assigned`` (its configured sources), ``missing_from`` (the assigned
    sources where every column is missing), ``found_in`` (the sources that
    have it), ``suggested_datasource`` (a name, or a list of names, to put
    under the table's ``datasource:``) and ``hint`` (the one-line sentence
    an operator reads).
    """

    checked_at: str
    schemas_scanned: tuple[str, ...]
    warehouse_only: tuple[str, ...]
    schema_only: tuple[str, ...]
    type_changed: tuple[dict[str, str], ...]
    unverifiable_tables: tuple[str, ...]
    baseline_available: bool
    misplaced_tables: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "schemas_scanned": list(self.schemas_scanned),
            "warehouse_only": list(self.warehouse_only),
            "schema_only": list(self.schema_only),
            "type_changed": list(self.type_changed),
            "unverifiable_tables": list(self.unverifiable_tables),
            "baseline_available": self.baseline_available,
            "misplaced_tables": list(self.misplaced_tables),
        }


@dataclass(frozen=True)
class _SourceScan:
    """What :func:`_scan` found on one data source.

    ``source`` is ``None`` for an injected engine (every table, no source
    name). ``tables`` are the ``schema.yaml`` tables expected on this
    source; ``verifiable`` the subset whose schema was actually scanned.
    """

    source: str | None
    tables: frozenset[str]
    schemas_scanned: tuple[str, ...]
    live: dict[tuple[str, str], dict[str, str]]
    verifiable: frozenset[str]


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



def _scan(
    engine: Engine,
    table_columns: dict[str, dict[str, str]],
    table_schemas: dict[str, str],
) -> tuple[list[str], dict[tuple[str, str], dict[str, str]], set[str]]:
    """Reflect the live tables *engine* can see for the given ``schema.yaml`` tables.

    Parameters
    ----------
    engine:
        Read-only warehouse engine.
    table_columns:
        ``{table: columns}`` for the queryable tables to verify.
    table_schemas:
        ``{table: effective_qualifier}`` (:func:`~schema_data.registry.get_table_schema_qualifiers`);
        a multi-part value (``"OtherDb.dbo"``) is passed to SQLAlchemy
        as-is, which reflects another database on the same SQL Server
        instance.

    Returns
    -------
    tuple
        ``(schemas_scanned, live, verifiable_tables)`` where *live* is
        ``{(schema_name, bare_table_name): {column: normalised type}}`` --
        keyed per SCANNED SCHEMA, not merged across schemas, so two
        same-bare-name tables in different schemas (``sales.Customer``,
        ``ref.Customer``) are kept apart; *schema_name* is ``""`` for a
        table reflected under SQLAlchemy's default search path (no
        qualifier scanned). *verifiable_tables* is the subset of
        *table_columns* whose schema was scanned.
    """
    schemas_scanned = sorted({s for t, s in table_schemas.items() if t in table_columns and s})
    # Tables SQLAlchemy sees with no schema qualifier at all (SQLite, or
    # any dialect whose default search path this deployment relies on) --
    # scanned under schema=None whenever at least one queryable table in
    # schema.yaml declares no db_schema either, so a single-schema
    # deployment (schema.yaml's own convention for one) is not silently
    # skipped entirely.
    scan_default_schema = any(t in table_columns and not table_schemas.get(t) for t in table_columns)

    inspector = sa_inspect(engine)

    # {(schema_name, bare_table_name): {column: type}} -- the BARE name
    # SQLAlchemy itself reports, kept apart per scanned schema; mapped back
    # to a schema.yaml KEY (which may be qualified) in check_schema_drift,
    # the one place that owns that reverse lookup.
    live: dict[tuple[str, str], dict[str, str]] = {}
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
            live_key = (schema_name or "", table_name)
            live.setdefault(live_key, {})
            for col in columns:
                live[live_key][col["name"]] = _normalise_type(col["type"])

    verifiable_tables = {
        t for t in table_columns if table_schemas.get(t) in schemas_scanned or (
            scan_default_schema and not table_schemas.get(t)
        )
    }
    return schemas_scanned, live, verifiable_tables


def _scan_every_datasource(
    table_columns: dict[str, dict[str, str]],
    table_schemas: dict[str, str],
    assignments: dict[str, tuple[str, ...]],
    names: tuple[str, ...],
) -> list[_SourceScan]:
    """Run :func:`_scan` once per data source, on that source's own tables.

    With one data source this is exactly one :func:`_scan` over every
    table. With several, each source's tables are checked against that
    source's own server -- a table listed under several sources is checked
    on each of them -- and ``schemas_scanned`` entries are prefixed
    ``source/`` so a reader can tell the servers apart. A source whose
    engine cannot be built is logged and its tables are reported as
    unverifiable rather than failing the whole report.
    """
    from database.connection import get_engine

    if len(names) == 1:
        scanned, live, verifiable = _scan(get_engine(names[0]), table_columns, table_schemas)
        return [_SourceScan(
            None, frozenset(table_columns), tuple(scanned), live, frozenset(verifiable),
        )]

    scans: list[_SourceScan] = []
    for name in names:
        subset = {t: c for t, c in table_columns.items() if name in assignments[t]}
        if not subset:
            continue
        try:
            engine = get_engine(name)
        except Exception as exc:  # noqa: BLE001 - one unreachable source must not hide the others
            logger.warning("schema_data.drift: data source %r unavailable: %s", name, exc)
            continue
        scanned, source_live, source_verifiable = _scan(engine, subset, table_schemas)
        scans.append(_SourceScan(
            name, frozenset(subset), tuple(f"{name}/{schema}" for schema in scanned),
            source_live, frozenset(source_verifiable),
        ))
    return scans


def _find_misplaced_tables(
    scans: list[_SourceScan],
    table_columns: dict[str, dict[str, str]],
    table_schemas: dict[str, str],
    assignments: dict[str, tuple[str, ...]],
    names: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Tables whose columns are all missing from an assigned source but that exist in another.

    A table is a candidate on source ``s`` when ``s`` is one of its
    assigned sources, its schema was scanned there, and none of its
    ``schema.yaml`` columns exist there. The other sources are then asked
    (one ``INFORMATION_SCHEMA.TABLES`` query each, at most once per call
    and only if there is a candidate) whether they have the table.
    """
    from database.catalogue import default_schema, list_tables, table_location
    from database.connection import get_engine

    # Per table: the assigned sources where its columns are all missing,
    # and those where at least one is present.
    absent: dict[str, list[str]] = {}
    present: dict[str, list[str]] = {}
    by_source = {scan.source: scan for scan in scans}
    for table, columns in table_columns.items():
        for source in assignments[table]:
            scan = by_source.get(source)
            if scan is None or table not in scan.verifiable:
                continue
            live_columns = scan.live.get(
                (table_schemas.get(table, ""), bare_table_name(table)), {},
            )
            if set(columns) & set(live_columns):
                present.setdefault(table, []).append(source)
            else:
                absent.setdefault(table, []).append(source)
    if not absent:
        return []

    catalogues: dict[str, tuple[frozenset[tuple[str, str]], str] | None] = {}

    def _catalogue(source: str) -> tuple[frozenset[tuple[str, str]], str] | None:
        if source not in catalogues:
            try:
                engine = get_engine(source)
                catalogues[source] = (list_tables(engine), default_schema(engine))
            except Exception as exc:  # noqa: BLE001 - a source we cannot read gives no hint
                logger.warning(
                    "schema_data.drift: could not list the tables of data source %r: %s",
                    source, exc,
                )
                catalogues[source] = None
        return catalogues[source]

    misplaced: list[dict[str, Any]] = []
    for table in sorted(absent):
        missing_from = [s for s in names if s in absent[table]]
        found = [s for s in names if s in present.get(table, [])]
        for source in names:
            if source in assignments[table] or source in found:
                continue
            catalogue = _catalogue(source)
            if catalogue is None:
                continue
            tables, default = catalogue
            if table_location(table, table_schemas.get(table, ""), default) in tables:
                found.append(source)
        if not found:
            continue
        found = [s for s in names if s in found]
        suggestion: str | list[str] = found[0] if len(found) == 1 else found
        value = found[0] if len(found) == 1 else "[" + ", ".join(found) + "]"
        misplaced.append({
            "table": table,
            "assigned": list(assignments[table]),
            "missing_from": missing_from,
            "found_in": found,
            "suggested_datasource": suggestion,
            "hint": (
                f"{table}: not in {', '.join(missing_from)}, "
                f"found in {', '.join(found)} — set datasource: {value}"
            ),
        })
    return misplaced


def check_schema_drift(engine: Engine | None = None, *, persist_baseline: bool = True) -> SchemaDriftReport:
    """Compare ``schema.yaml``'s queryable tables/columns against the live
    warehouse. Read-only in every direction: never writes ``schema.yaml``,
    and reads the warehouse purely through SQLAlchemy's catalogue
    reflection (``inspect(engine).get_table_names()``/``get_columns()``),
    the same metadata-only queries a read-only login already supports.

    Parameters
    ----------
    engine:
        Defaults to each data source's :func:`database.connection.get_engine`
        engine -- the SAME read-only connections every query already runs
        through -- with each source's tables checked on that source (a
        table listed under several sources on each of them).
        An injected engine is used for every table, and the
        ``misplaced_tables`` hint (which needs the other sources) is not
        computed.
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

    assignments: dict[str, tuple[str, ...]] = {}
    names: tuple[str, ...] = ()
    if engine is not None:
        scanned, live, verifiable = _scan(engine, table_columns, table_schemas)
        scans = [_SourceScan(
            None, frozenset(table_columns), tuple(scanned), live, frozenset(verifiable),
        )]
    else:
        from database.datasources import datasource_names, table_datasource_sets

        names = datasource_names()
        default = names[0]
        sets = table_datasource_sets()
        assignments = {t: sets.get(t, (default,)) for t in table_columns}
        scans = _scan_every_datasource(table_columns, table_schemas, assignments, names)

    # A table that lives in several sources is reported per source, so the
    # two copies' columns are not mixed up: its ids carry a " [source]" tail.
    shared_tables = {t for t, sources in assignments.items() if len(sources) > 1}

    def _column_id(table_id: str, column: str, scan: _SourceScan, table_key: str | None) -> str:
        suffix = f" [{scan.source}]" if table_key in shared_tables and scan.source else ""
        return f"{table_id}.{column}{suffix}"

    # Map a live (schema, bare_name) pair back to the schema.yaml KEY it
    # matches -- a qualified key's own bare name may differ from the key
    # itself (`"sales.Customer"` -> bare `"Customer"`), so the live scan's
    # bare-named result has to be looked up, not assumed identical to the
    # key. Two schema.yaml keys sharing a bare name in different schemas
    # (the very case this feature exists to support) map back correctly
    # here because the lookup is keyed on (schema, bare_name) together.
    key_by_schema_and_bare = {
        (table_schemas.get(t, ""), bare_table_name(t)): t for t in table_columns
    }

    schema_col_ids: set[str] = set()
    live_col_ids: dict[str, str] = {}
    verified: set[tuple[str | None, str]] = set()
    for scan in scans:
        for table in scan.verifiable:
            verified.add((scan.source, table))
            for column in table_columns[table]:
                schema_col_ids.add(_column_id(table, column, scan, table))
        for (schema_name, bare_name), columns in scan.live.items():
            key = key_by_schema_and_bare.get((schema_name, bare_name))
            if key is not None:
                if key not in scan.tables:
                    # A copy of a table schema.yaml describes for another
                    # source only: nothing routes a query here, so it is
                    # neither drift nor queryable.
                    continue
                # Matches a known table -- report under its schema.yaml key,
                # UNCHANGED from today's output for a bare, non-duplicated key
                # (there, key == bare_name, exactly what this branch already
                # produced before qualified keys existed).
                table_id = key
            elif schema_name:
                # No schema.yaml key names this table at all (a genuine
                # warehouse_only table) -- reported schema-qualified when the
                # schema is known, so two same-bare-name warehouse-only tables
                # in different schemas are not collapsed into one identity.
                table_id = f"{schema_name}.{bare_name}"
            else:
                table_id = bare_name
            for column, col_type in columns.items():
                live_col_ids[_column_id(table_id, column, scan, key)] = col_type

    # Every (source, table) pair that should have been scanned; a table is
    # unverifiable unless it was scanned on each source it lives in.
    if len(names) > 1:
        expected = {(s, t) for t in table_columns for s in assignments[t]}
    else:
        expected = {(None, t) for t in table_columns}
    unverifiable_tables = sorted({table for source, table in expected - verified})

    # A whole table the warehouse has that schema.yaml never mentions at
    # all reports here too, one entry per column -- there is nothing in
    # schema_col_ids to diff it against, so every one of its columns
    # already falls out of this set difference on its own.
    warehouse_only = sorted(set(live_col_ids) - schema_col_ids)
    schema_only = sorted(schema_col_ids - set(live_col_ids))

    baseline = _load_baseline() or {}
    type_changed: list[dict[str, str]] = []
    for col_id in sorted(schema_col_ids & set(live_col_ids)):
        current_type = live_col_ids[col_id]
        previous_type = baseline.get(col_id)
        if previous_type is not None and previous_type != current_type:
            type_changed.append({
                "column": col_id, "previous_type": previous_type, "current_type": current_type,
            })

    misplaced: list[dict[str, Any]] = []
    if len(names) > 1:
        misplaced = _find_misplaced_tables(
            scans, table_columns, table_schemas, assignments, names,
        )

    if persist_baseline:
        _save_baseline(live_col_ids)

    return SchemaDriftReport(
        checked_at=datetime.now(timezone.utc).isoformat(),
        schemas_scanned=tuple(sorted(s for scan in scans for s in scan.schemas_scanned)),
        warehouse_only=tuple(warehouse_only),
        schema_only=tuple(schema_only),
        type_changed=tuple(type_changed),
        unverifiable_tables=tuple(unverifiable_tables),
        baseline_available=bool(baseline),
        misplaced_tables=tuple(misplaced),
    )
