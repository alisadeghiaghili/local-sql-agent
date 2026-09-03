# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pins schema_data's loaded registry to a known-good snapshot.

Phase 4 moved the IME warehouse schema out of
``schema_data/{tables,columns,relationships}.py`` (Python literals, tracked
by git) into ``project_config/schema.yaml`` (git-ignored, loaded through
:mod:`schema_data.registry`). ``security.sql_guard`` derives its table and
column allowlist directly from :data:`schema_data.columns.TABLE_COLUMNS` --
so a ``schema.yaml`` edit that silently drops a column, renames a table, or
adds a new one changes the guard's effective security posture with no
Python-level code change at all to review.

This module pins the loaded data so that kind of drift fails a test
instead of silently changing what the guard accepts:

1. The exact set of tables that are part of the guard's allowlist (i.e.
   carry a ``columns`` key in ``schema.yaml``) -- ``TestGuardAllowlistTables``.
2. The exact column count per allowlisted table, and overall -- ``87``
   columns across ``12`` tables today -- ``TestGuardAllowlistColumns``.
3. The full set of described tables (23: the 12 queryable ones plus 11
   prompt-only lookup/status dimensions with no ``columns`` key) --
   ``TestFullSchemaTableSet``.
4. The relationship count -- ``TestRelationshipCount``.
5. A content hash of every table's and relationship's full text, so even a
   *wording* change to ``schema.yaml`` is visible here -- not
   security-relevant on its own, but this module's job is "did the loaded
   registry change at all", not just "did the allowlist shrink or grow" --
   ``TestSchemaContentHash``.

If this fails after an intentional, reviewed ``schema.yaml`` edit, that is
expected -- recompute and update the pinned values below (see
``_hash``'s docstring for how).

Every class above this module's ``TestAllowlistStructuralInvariants`` pins
*exact* real values, so each is marked ``domain_data`` and is auto-skipped
whenever ``PROJECT_CONFIG_DIR`` points at ``project_config.example/`` (CI,
a fresh clone) -- see the repo-root ``conftest.py``. Against the real
``project_config/`` (every developer's normal local run) these run exactly
as before. ``TestAllowlistStructuralInvariants`` at the bottom of this
module is NOT marked ``domain_data`` -- see its own docstring for the
schema-agnostic allowlist check that keeps running in CI regardless.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from schema_data.columns import TABLE_COLUMNS
from schema_data.relationships import RELATIONSHIPS
from schema_data.tables import TABLE_DESCRIPTIONS

# ---------------------------------------------------------------------------
# Known-good snapshot, captured from project_config/schema.yaml at the time
# schema_data/{tables,columns,relationships}.py were retired (Phase 4).
# ---------------------------------------------------------------------------

#: Tables that carry a `columns` key in schema.yaml -- these, and only
#: these, are queryable per security.sql_guard's table allowlist.
_EXPECTED_ALLOWLIST_TABLES = (
    "Broker", "Contract", "Currency", "Customer", "CustomerContract",
    "Date", "DeliveryPlace", "Offer", "Order", "Ring", "Supplier", "Symbol",
)

_EXPECTED_COLUMNS_PER_TABLE = {
    "Contract": 14,
    "CustomerContract": 15,
    "Offer": 15,
    "Order": 8,
    "Customer": 4,
    "Supplier": 3,
    "Broker": 3,
    "Symbol": 4,
    "Ring": 3,
    "Date": 13,
    "Currency": 3,
    "DeliveryPlace": 2,
}

_EXPECTED_TOTAL_COLUMNS = 87

#: Every table described in schema.yaml, including the 11 prompt-only
#: lookup/status dimensions that have no `columns` key (Bank, BuyMethod,
#: Carrier, ClearingKind, ContractKind, ContractStatus, GeneralStatus,
#: OfferKind, OfferStatus, PaymentDelivery, TalarLog) and are therefore
#: NOT part of the guard's allowlist.
_EXPECTED_TABLE_NAMES = (
    "Bank", "Broker", "BuyMethod", "Carrier", "ClearingKind", "Contract",
    "ContractKind", "ContractStatus", "Currency", "Customer",
    "CustomerContract", "Date", "DeliveryPlace", "GeneralStatus", "Offer",
    "OfferKind", "OfferStatus", "Order", "PaymentDelivery", "Ring",
    "Supplier", "Symbol", "TalarLog",
)

_EXPECTED_RELATIONSHIP_COUNT = 25

# sha256 of the canonical (sort_keys=True) JSON form of each data source --
# any change anywhere in it (a description edit, a column rename, an added
# or removed entry) changes the hash. Recompute with:
#
#   python -c "
#   import hashlib, json
#   from schema_data.columns import TABLE_COLUMNS
#   print(hashlib.sha256(
#       json.dumps(TABLE_COLUMNS, sort_keys=True, ensure_ascii=False)
#           .encode('utf-8')
#   ).hexdigest())"
#
# (swap in TABLE_DESCRIPTIONS / RELATIONSHIPS for the other two hashes).
_EXPECTED_COLUMNS_HASH = (
    "761fb345d73842f97fb0e541001a05c07360c43963aa74a9973e799b8fb11972"
)
_EXPECTED_DESCRIPTIONS_HASH = (
    "f8afbf92edaa7854d913972175ee4fa3483b1c44a77c268db2037b85c31b2299"
)
_EXPECTED_RELATIONSHIPS_HASH = (
    "4df07f669989bb086f156f6ade4439df2c0add83f0145c72d92a8417de827999"
)


def _hash(obj: object) -> str:
    """sha256 hex digest of *obj*'s canonical (sorted-key) JSON form."""
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TestGuardAllowlistTables:
    """The exact set of tables the SQL guard accepts must not drift silently."""

    pytestmark = pytest.mark.domain_data

    def test_allowlisted_table_names(self):
        assert sorted(TABLE_COLUMNS) == sorted(_EXPECTED_ALLOWLIST_TABLES)

    def test_table_count(self):
        assert len(TABLE_COLUMNS) == 12


class TestGuardAllowlistColumns:
    """The exact column count per table, and overall, must not drift silently."""

    pytestmark = pytest.mark.domain_data

    def test_column_count_per_table(self):
        actual = {table: len(cols) for table, cols in TABLE_COLUMNS.items()}
        assert actual == _EXPECTED_COLUMNS_PER_TABLE

    def test_total_column_count(self):
        total = sum(len(cols) for cols in TABLE_COLUMNS.values())
        assert total == _EXPECTED_TOTAL_COLUMNS


class TestFullSchemaTableSet:
    """All 23 described tables (12 queryable + 11 prompt-only), not just
    the guard's allowlist -- catches drift in schema.yaml's `tables` key
    even for a table that carries no `columns` sub-key."""

    pytestmark = pytest.mark.domain_data

    def test_all_table_names(self):
        assert sorted(TABLE_DESCRIPTIONS) == sorted(_EXPECTED_TABLE_NAMES)


class TestRelationshipCount:
    pytestmark = pytest.mark.domain_data

    def test_relationship_count(self):
        assert len(RELATIONSHIPS) == _EXPECTED_RELATIONSHIP_COUNT


class TestSchemaContentHash:
    """Content hashes catch a *wording* change even when no name/count
    changes -- e.g. someone edits a column's description in schema.yaml.
    Not security-relevant on its own, but this module's job is "did the
    loaded registry change at all" -- if one of these fails after a
    deliberate, reviewed schema.yaml edit, recompute and update the pinned
    hash above (see ``_hash``'s docstring)."""

    pytestmark = pytest.mark.domain_data

    def test_columns_hash(self):
        assert _hash(TABLE_COLUMNS) == _EXPECTED_COLUMNS_HASH

    def test_descriptions_hash(self):
        assert _hash(TABLE_DESCRIPTIONS) == _EXPECTED_DESCRIPTIONS_HASH

    def test_relationships_hash(self):
        assert _hash(RELATIONSHIPS) == _EXPECTED_RELATIONSHIPS_HASH


class TestAllowlistStructuralInvariants:
    """Schema-agnostic checks on the guard's allowlist SHAPE, not its exact
    values -- these run in every configuration, including CI's
    ``project_config.example/`` (unlike every class above, which is marked
    ``domain_data`` and skips there). This is the answer to "what does CI
    still verify about the guard's allowlist once the exact real snapshot
    can no longer run there": not the specific 12 table names, but that
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
    """

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
        table name (e.g. the real schema's "Contract -> BuyerBroker" /
        "Contract -> SellerBroker" both resolve to the "Broker" table, one
        per FK role), which SchemaRegistry.get_relationships's own
        docstring already documents as a supported key shape. That is a
        pre-existing property of schema_data/relationships.py's data
        model, not a regression this test should flag. Every from_table in
        both the real schema and project_config.example/schema.yaml is a
        literal table name, so that side is safe to check strictly."""
        for key in RELATIONSHIPS:
            left = key.split(" -> ")[0]
            left_table = left.split(".")[0]
            assert left_table in TABLE_DESCRIPTIONS, f"{key}: unknown left table {left_table!r}"
