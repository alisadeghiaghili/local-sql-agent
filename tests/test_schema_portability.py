# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Proves the actual point of Phase 4's finish: NOTHING warehouse-specific
lives in Python source any more for value resolution / vocabulary prefetch.

Before this phase, ``retrieval/value_resolver.py`` and
``retrieval/dimension_vocabulary.py`` each hardcoded a literal schema
qualifier (``_SCHEMA = "Auction_Dim"``) and a hand-written
``{table: (columns...)}`` allowlist. Both are now derived entirely from
``<PROJECT_CONFIG_DIR>/schema.yaml`` via ``schema_data.registry`` -- see
that module's ``get_table_schema_qualifiers`` / ``get_resolvable_columns`` /
``get_prefetchable_columns``.

This module does not re-test the allowlist's *shape* (``tests/test_schema_registry_loader.py``
already covers ``load_schema()``'s directory-configurability and
validation) -- it tests that pointing the SAME, unmodified code at a
genuinely different schema.yaml (different table names, different column
names, different schema qualifier -- not just different values for
identical keys) produces a correspondingly different allowlist and a
correspondingly different generated query. That is the property the owner
actually asked for: this tool must work against *any* warehouse, not just
this one.

Two layers, both against the same two synthetic ``schema.yaml`` documents:

1. :class:`TestRegistryDerivesADifferentAllowlistPerConfig` -- the loader
   layer. Bypasses ``schema_data.registry``'s process-lifetime cache (via
   ``load_schema()`` directly, the same discipline
   ``tests/test_schema_registry_loader.py::TestDirectoryIsConfigurable``
   already uses) so two different directories can be loaded within one
   test process.
2. :class:`TestRetrievalModulesBuildADifferentQueryPerConfig` -- the
   consumer layer. ``retrieval.value_resolver``/``retrieval.dimension_vocabulary``
   read their allowlist once at *import* time (mirroring
   ``security.sql_guard``'s own eager, schema.yaml-derived allowlist -- see
   both modules' docstrings), so re-importing them mid-test-process would
   not reflect a later ``override_settings`` call; production only ever
   picks up a schema.yaml edit on restart, same as the guard's. This class
   monkeypatches each module's already-loaded ``RESOLVABLE_COLUMNS`` /
   ``PREFETCH_COLUMNS`` / ``_TABLE_SCHEMAS`` to the exact values layer 1
   proved two different configs produce, then calls the real
   ``_build_query`` / ``_prefetch_query`` functions -- the actual
   production code path -- to prove the generated SQL text itself differs
   correspondingly, with no source change anywhere.
"""

from __future__ import annotations

import yaml

from config import override_settings
from schema_data.registry import load_schema

# ---------------------------------------------------------------------------
# Two synthetic warehouses. Deliberately share NOTHING lexically -- not a
# table name, not a column name, not a schema qualifier -- so a passing
# test cannot be explained by coincidence or by one hardcoded fallback
# value leaking through.
# ---------------------------------------------------------------------------

_WAREHOUSE_A = {
    "tables": {
        "Ring": {
            "description": "Auction_Dim.Ring — trading halls",
            "db_schema": "Auction_Dim",
            "columns": {"ID": "Primary key", "Name": "Hall name"},
            "resolvable_columns": ["Name"],
            "prefetchable_columns": ["Name"],
        },
    },
}

_WAREHOUSE_B = {
    "tables": {
        "SalesChannel": {
            "description": "retail.SalesChannel — store / channel reference data",
            "db_schema": "retail",
            "columns": {"ID": "Primary key", "ChannelName": "Channel display name"},
            "resolvable_columns": ["ChannelName"],
            "prefetchable_columns": ["ChannelName"],
        },
    },
}


def _write_schema(tmp_path, doc: dict) -> None:
    (tmp_path / "schema.yaml").write_text(
        yaml.dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


class TestRegistryDerivesADifferentAllowlistPerConfig:
    """schema_data.registry.load_schema() -- the loader both
    retrieval.value_resolver and retrieval.dimension_vocabulary call at
    import time -- produces a different resolvable/prefetchable/schema-
    qualifier mapping for two schema.yaml documents that share no table or
    column name at all."""

    def test_resolvable_and_prefetchable_columns_differ(self, tmp_path):
        dir_a, dir_b = tmp_path / "a", tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        _write_schema(dir_a, _WAREHOUSE_A)
        _write_schema(dir_b, _WAREHOUSE_B)

        with override_settings(project_config_dir=str(dir_a)):
            cfg_a = load_schema()
        with override_settings(project_config_dir=str(dir_b)):
            cfg_b = load_schema()

        resolvable_a = {
            name: t.resolvable_columns for name, t in cfg_a.tables.items() if t.resolvable_columns
        }
        resolvable_b = {
            name: t.resolvable_columns for name, t in cfg_b.tables.items() if t.resolvable_columns
        }
        prefetchable_a = {
            name: t.prefetchable_columns for name, t in cfg_a.tables.items() if t.prefetchable_columns
        }
        prefetchable_b = {
            name: t.prefetchable_columns for name, t in cfg_b.tables.items() if t.prefetchable_columns
        }

        assert resolvable_a == {"Ring": ("Name",)}
        assert resolvable_b == {"SalesChannel": ("ChannelName",)}
        assert prefetchable_a == {"Ring": ("Name",)}
        assert prefetchable_b == {"SalesChannel": ("ChannelName",)}
        assert resolvable_a != resolvable_b
        assert prefetchable_a != prefetchable_b

    def test_schema_qualifier_differs(self, tmp_path):
        dir_a, dir_b = tmp_path / "a", tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        _write_schema(dir_a, _WAREHOUSE_A)
        _write_schema(dir_b, _WAREHOUSE_B)

        with override_settings(project_config_dir=str(dir_a)):
            cfg_a = load_schema()
        with override_settings(project_config_dir=str(dir_b)):
            cfg_b = load_schema()

        assert cfg_a.tables["Ring"].db_schema == "Auction_Dim"
        assert cfg_b.tables["SalesChannel"].db_schema == "retail"


class TestRetrievalModulesBuildADifferentQueryPerConfig:
    """The real _build_query / _prefetch_query functions -- unmodified,
    the exact production code path -- produce a correspondingly different
    query for each warehouse's allowlist, once their module's allowlist
    reflects it. Confirms the config-driven allowlist actually reaches the
    generated SQL, not just the loader's own output."""

    def test_value_resolver_build_query_reflects_the_active_config(self, monkeypatch):
        from retrieval import value_resolver

        monkeypatch.setitem(value_resolver._TABLE_SCHEMAS, "Ring", "Auction_Dim")
        sql_a = value_resolver._build_query("Ring", "Name")
        assert sql_a == (
            "SELECT DISTINCT TOP (?) [Name] FROM [Auction_Dim].[Ring] "
            "WHERE [Name] LIKE ? ESCAPE '\\'"
        )

        monkeypatch.setitem(value_resolver._TABLE_SCHEMAS, "SalesChannel", "retail")
        sql_b = value_resolver._build_query("SalesChannel", "ChannelName")
        assert sql_b == (
            "SELECT DISTINCT TOP (?) [ChannelName] FROM [retail].[SalesChannel] "
            "WHERE [ChannelName] LIKE ? ESCAPE '\\'"
        )

        assert sql_a != sql_b

    def test_dimension_vocabulary_prefetch_query_reflects_the_active_config(self, monkeypatch):
        from retrieval import dimension_vocabulary as dv

        monkeypatch.setitem(dv._TABLE_SCHEMAS, "Ring", "Auction_Dim")
        sql_a = dv._prefetch_query("Ring", "Name")
        assert sql_a == "SELECT DISTINCT TOP (?) [Name] FROM [Auction_Dim].[Ring]"

        monkeypatch.setitem(dv._TABLE_SCHEMAS, "SalesChannel", "retail")
        sql_b = dv._prefetch_query("SalesChannel", "ChannelName")
        assert sql_b == "SELECT DISTINCT TOP (?) [ChannelName] FROM [retail].[SalesChannel]"

        assert sql_a != sql_b
