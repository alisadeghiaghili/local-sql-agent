# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for database/relationship_map.py's PROJECT_CONFIG_DIR resolution.

This module's ``_RELATIONSHIPS_YAML`` used to be a hardcoded
``Path("project_config") / "relationships.yaml"`` constant, evaluated once
at import time -- ignoring ``PROJECT_CONFIG_DIR`` entirely, unlike every
other ``project_config``-reading loader in this codebase
(:mod:`knowledge.config_loader`, :mod:`schema_data.registry`). A
deployment (or CI) that pointed ``PROJECT_CONFIG_DIR`` anywhere else --
including at ``project_config.example/``, which is exactly what CI does --
silently never reached this module's YAML file at all: ``get_relationship_map()``
fell straight through to "no relationships.yaml found", returning an empty
mapping with no error, no matter what the file on disk actually contained.

These tests use synthetic table/column names for the unit-level behaviour
(database-agnostic, per this repo's testing convention) and separately
pin the one integration-level *behaviour change*: under CI's own
``PROJECT_CONFIG_DIR=project_config.example``, the module now actually
loads that directory's ``relationships.yaml`` instead of silently finding
nothing.
"""

from __future__ import annotations


import pytest

import database.relationship_map as relationship_map
from config import override_settings


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts and ends with a cold cache -- this module's cache
    is a bare module-level global, not scoped to a test's own override."""
    relationship_map.reset()
    yield
    relationship_map.reset()


class TestProjectConfigDirResolution:
    def test_relative_project_config_dir_resolves_against_repo_root(self):
        with override_settings(project_config_dir="project_config.example"):
            path = relationship_map._relationships_yaml_path()
        assert path.is_absolute()
        assert path.name == "relationships.yaml"

    def test_absolute_project_config_dir_used_as_is(self, tmp_path):
        with override_settings(project_config_dir=str(tmp_path)):
            path = relationship_map._relationships_yaml_path()
        assert path == tmp_path / "relationships.yaml"


class TestLoadFromConfiguredDirectory:
    """Synthetic, database-agnostic fixture -- table/column names are
    arbitrary strings, not real or example schema data."""

    _YAML = """
relationships:
  - from_table: "Widget"
    from_schema: "app"
    from_column: "GadgetID"
    to_table: "Gadget"
    to_schema: "app"
    to_column: "ID"
    join_hint: "JOIN [app].[Gadget] ON [app].[Widget].[GadgetID] = [app].[Gadget].[ID]"
"""

    def test_relationship_loaded_from_configured_project_config_dir(self, tmp_path):
        (tmp_path / "relationships.yaml").write_text(self._YAML, encoding="utf-8")
        with override_settings(project_config_dir=str(tmp_path)):
            hints = relationship_map.get_join_path("Widget", "Gadget")
        assert hints == [
            "JOIN [app].[Gadget] ON [app].[Widget].[GadgetID] = [app].[Gadget].[ID]"
        ]

    def test_reverse_direction_registered_automatically(self, tmp_path):
        (tmp_path / "relationships.yaml").write_text(self._YAML, encoding="utf-8")
        with override_settings(project_config_dir=str(tmp_path)):
            hints = relationship_map.get_join_path("Gadget", "Widget")
        assert hints == [
            "JOIN [app].[Gadget] ON [app].[Widget].[GadgetID] = [app].[Gadget].[ID]"
        ]

    def test_no_file_in_configured_directory_is_empty_not_an_error(self, tmp_path):
        with override_settings(project_config_dir=str(tmp_path)):
            assert relationship_map.get_relationship_map() == {}

    def test_reset_forces_a_reload_from_the_currently_configured_directory(self, tmp_path):
        with override_settings(project_config_dir=str(tmp_path)):
            assert relationship_map.get_relationship_map() == {}

            (tmp_path / "relationships.yaml").write_text(self._YAML, encoding="utf-8")
            # Without reset(), the process-lifetime cache would still hold
            # the earlier, empty result.
            relationship_map.reset()
            assert relationship_map.get_join_path("Widget", "Gadget") != []


class TestExampleConfigIsActuallyReached:
    """Pins the behaviour change: CI (and any deployment) that sets
    PROJECT_CONFIG_DIR now has that directory's relationships.yaml
    actually loaded, instead of the pre-fix code silently looking at a
    hardcoded ``project_config/`` regardless of the setting.

    Derives its expectation from schema_data.registry's own
    get_relationships_map() (the example config's already-loaded, public
    template data) rather than hardcoding a table name here -- consistent
    with this repo's "don't assert real/example domain values redundantly"
    convention.
    """

    def test_relationships_yaml_under_configured_dir_is_loaded(self):
        import schema_data.registry as registry

        with override_settings(project_config_dir="project_config.example"):
            relationship_map.reset()
            mapping = relationship_map.get_relationship_map()

        assert mapping, (
            "expected a non-empty relationship map once PROJECT_CONFIG_DIR "
            "is actually honoured -- project_config.example/relationships.yaml "
            "is non-empty"
        )

        # Every pair schema.yaml's own registry knows about a JOIN edge for
        # must be resolvable through this module too (both files describe
        # the same example schema).
        example_pairs = {
            tuple(key.split(" -> ")) for key in registry.get_relationships_map()
        }
        for from_table, to_table in example_pairs:
            assert relationship_map.get_join_path(from_table, to_table), (
                f"schema.yaml declares a {from_table}->{to_table} relationship "
                f"that relationships.yaml does not"
            )
