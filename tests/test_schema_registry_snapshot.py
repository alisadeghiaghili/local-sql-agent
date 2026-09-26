# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pins schema_data's loaded registry to a known-good snapshot.

Phase 4 moved the real warehouse schema out of
``schema_data/{tables,columns,relationships}.py`` (Python literals, tracked
by git) into ``project_config/schema.yaml`` (git-ignored, loaded through
:mod:`schema_data.registry`). ``security.sql_guard`` derives its table and
column allowlist directly from :data:`schema_data.columns.TABLE_COLUMNS` --
so a ``schema.yaml`` edit that silently drops a column, renames a table, or
adds a new one changes the guard's effective security posture with no
Python-level code change at all to review.

This module pins the loaded data so that kind of drift fails a test
instead of silently changing what the guard accepts. The exact pinned
values (table names, column counts, relationship count, content hashes)
are real, deployment-specific data -- they say nothing useful about
``project_config.example/``'s generic schema, so they are NOT hardcoded
here. Instead they live in an optional, deployment-owned fixture file,
``<PROJECT_CONFIG_DIR>/_test_fixtures/schema_registry_snapshot.json`` (see
``project_config.example/_test_fixtures/README.md`` for the format), loaded
through :mod:`tests._domain_fixtures`. Every class below except
``TestAllowlistStructuralInvariants`` is additionally marked
``domain_data`` and auto-skips whenever ``PROJECT_CONFIG_DIR`` points at
``project_config.example/`` (CI, a fresh clone) -- see the repo-root
``conftest.py``; the fixture-file skip in ``tests._domain_fixtures``
separately covers "a real ``project_config/`` is in effect, but this one
optional fixture hasn't been created yet."

If a check fails after an intentional, reviewed ``schema.yaml`` edit, that
is expected -- recompute and update the deployment's own fixture file (see
the module-level ``_hash`` docstring below for how to recompute a hash).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from schema_data.columns import TABLE_COLUMNS
from schema_data.registry import check_allowlist_structural_invariants
from schema_data.relationships import RELATIONSHIPS
from schema_data.tables import TABLE_DESCRIPTIONS
from tests._domain_fixtures import load_json_fixture


def _hash(obj: object) -> str:
    """sha256 hex digest of *obj*'s canonical (sorted-key) JSON form.

    Recompute with:

        python -c "
        import hashlib, json
        from schema_data.columns import TABLE_COLUMNS
        print(hashlib.sha256(
            json.dumps(TABLE_COLUMNS, sort_keys=True, ensure_ascii=False)
                .encode('utf-8')
        ).hexdigest())"

    (swap in TABLE_DESCRIPTIONS / RELATIONSHIPS for the other two hashes)
    and paste the new value into the fixture file.
    """
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def snapshot():
    return load_json_fixture("schema_registry_snapshot.json")


class TestGuardAllowlistTables:
    """The exact set of tables the SQL guard accepts must not drift silently."""

    pytestmark = pytest.mark.domain_data

    def test_allowlisted_table_names(self, snapshot):
        assert sorted(TABLE_COLUMNS) == sorted(snapshot["allowlist_tables"])

    def test_table_count(self, snapshot):
        assert len(TABLE_COLUMNS) == snapshot["allowlist_table_count"]


class TestGuardAllowlistColumns:
    """The exact column count per table, and overall, must not drift silently."""

    pytestmark = pytest.mark.domain_data

    def test_column_count_per_table(self, snapshot):
        actual = {table: len(cols) for table, cols in TABLE_COLUMNS.items()}
        assert actual == snapshot["columns_per_table"]

    def test_total_column_count(self, snapshot):
        total = sum(len(cols) for cols in TABLE_COLUMNS.values())
        assert total == snapshot["total_columns"]


class TestFullSchemaTableSet:
    """All described tables (queryable + prompt-only ones with no
    ``columns`` sub-key), not just the guard's allowlist -- catches drift
    in schema.yaml's ``tables`` key even for a table with no ``columns``
    key of its own."""

    pytestmark = pytest.mark.domain_data

    def test_all_table_names(self, snapshot):
        assert sorted(TABLE_DESCRIPTIONS) == sorted(snapshot["all_table_names"])


class TestRelationshipCount:
    pytestmark = pytest.mark.domain_data

    def test_relationship_count(self, snapshot):
        assert len(RELATIONSHIPS) == snapshot["relationship_count"]


class TestSchemaContentHash:
    """Content hashes catch a *wording* change even when no name/count
    changes -- e.g. someone edits a column's description in schema.yaml.
    Not security-relevant on its own, but this module's job is "did the
    loaded registry change at all" -- if one of these fails after a
    deliberate, reviewed schema.yaml edit, recompute and update the
    deployment's fixture file (see ``_hash``'s docstring)."""

    pytestmark = pytest.mark.domain_data

    def test_columns_hash(self, snapshot):
        assert _hash(TABLE_COLUMNS) == snapshot["columns_hash"]

    def test_descriptions_hash(self, snapshot):
        assert _hash(TABLE_DESCRIPTIONS) == snapshot["descriptions_hash"]

    def test_relationships_hash(self, snapshot):
        assert _hash(RELATIONSHIPS) == snapshot["relationships_hash"]


class TestAllowlistStructuralInvariants:
    """Schema-agnostic checks on the guard's allowlist SHAPE, not its exact
    values -- these run in every configuration, including CI's
    ``project_config.example/`` (unlike every class above, which is marked
    ``domain_data`` and skips there). This is the answer to "what does CI
    still verify about the guard's allowlist once the exact real snapshot
    can no longer run there": not the specific table names, but that
    whatever schema.yaml IS loaded is internally consistent -- every
    allowlisted table has at least one column, every allowlisted table is
    also a described table, and every relationship connects two tables that
    actually exist. A schema.yaml edit that broke one of these would be a
    real bug regardless of which project_config directory is in effect.

    ``tests/test_sql_guard_schema.py``'s ``TestRealSchemaTablesValidate``
    and ``TestRealSchemaColumnsValidate`` classes already do the same job
    one level down (they parametrize over whatever ``TABLE_COLUMNS``
    actually is and prove each entry validates through the real
    ``security.sql_guard.validate_sql`` pipeline) -- this class covers the
    loader's own output shape, that one covers the guard's behaviour on it.

    Every check below is delegated to
    ``schema_data.registry.check_allowlist_structural_invariants`` rather
    than reimplemented here -- admin panel phase 3's
    ``appdb.config_versions`` calls that same function against a
    *candidate*, not-yet-applied ``schema.yaml`` before a security admin's
    edit can reach the guard at all, so this class and that module share
    one source of truth for what "structurally sound" means.
    """

    def test_structural_invariants_hold(self):
        violations = check_allowlist_structural_invariants(
            TABLE_COLUMNS, TABLE_DESCRIPTIONS, RELATIONSHIPS
        )
        assert violations == []

    def test_allowlist_is_not_empty(self):
        assert len(TABLE_COLUMNS) > 0

    def test_every_allowlisted_table_has_at_least_one_column(self):
        for table, columns in TABLE_COLUMNS.items():
            assert len(columns) > 0, f"{table} has a `columns` key but no columns"

    def test_every_column_name_is_non_empty_and_unique_per_table(self):
        for table, columns in TABLE_COLUMNS.items():
            names = list(columns)
            assert all(name.strip() for name in names), f"{table} has a blank column name"
            assert len(names) == len(set(names)), f"{table} has a duplicate column name"

    def test_every_allowlisted_table_is_also_a_described_table(self):
        """A table cannot be queryable without also being described -- the
        `columns` key lives under the same `tables` entry as `description`
        in schema.yaml, so this should be structurally impossible; this
        test exists so a future loader change that broke that link would
        still be caught here rather than only downstream."""
        assert set(TABLE_COLUMNS).issubset(set(TABLE_DESCRIPTIONS))

    def test_every_relationship_left_side_is_a_described_table(self):
        """Only the LEFT side is checked here -- the right side of a
        relationship key is sometimes a *role* name rather than a literal
        table name (e.g. two relationship entries can both resolve to the
        same physical table, one per FK role), which
        SchemaRegistry.get_relationships's own docstring already documents
        as a supported key shape. That is a pre-existing property of
        schema_data/relationships.py's data model, not a regression this
        test should flag. Every from_table in both the real schema and
        project_config.example/schema.yaml is a literal table name, so
        that side is safe to check strictly."""
        for key in RELATIONSHIPS:
            left = key.split(" -> ")[0]
            left_table = left.split(".")[0]
            assert left_table in TABLE_DESCRIPTIONS, f"{key}: unknown left table {left_table!r}"
