# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``column_units:`` of ``schema.yaml``: the optional, per-table ``{column: rial | none}`` map.

It is read only by the answer summary (``tests/test_interpret_units.py``) and
kept by the sync (``tests/test_schema_sync_units.py``). Here: what the loader
accepts and refuses.
"""

from __future__ import annotations

import pytest

from schema_data.registry import validate_schema_yaml_text


class TestTheColumnUnitsKeyInSchemaYaml:
    def test_it_is_optional_and_empty_by_default(self):
        config = validate_schema_yaml_text("tables:\n  T:\n    columns:\n      A: a\n")
        assert config.tables["T"].column_units == {}

    def test_the_example_schema_still_loads(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "project_config.example" / "schema.yaml").read_text(
            encoding="utf-8",
        )
        config = validate_schema_yaml_text(text)
        assert config.tables["Order"].column_units == {"TotalAmount": "rial"}
        assert config.tables["Customer"].column_units == {}

    def test_values_are_stored_in_lower_case(self):
        config = validate_schema_yaml_text(
            "tables:\n  T:\n    columns:\n      A: a\n      B: b\n    column_units:\n      A: Rial\n      B: NONE\n",
        )
        assert config.tables["T"].column_units == {"A": "rial", "B": "none"}

    def test_an_empty_key_is_an_empty_map(self):
        config = validate_schema_yaml_text("tables:\n  T:\n    columns:\n      A: a\n    column_units:\n")
        assert config.tables["T"].column_units == {}

    @pytest.mark.parametrize("value", ["toman", "money", "no", "''", "3"])
    def test_an_unknown_unit_is_refused(self, value):
        text = f"tables:\n  T:\n    columns:\n      A: a\n    column_units:\n      A: {value}\n"
        with pytest.raises(ValueError, match=r"column_units\['A'\] must be one of"):
            validate_schema_yaml_text(text)

    def test_a_column_the_table_does_not_have_is_refused(self):
        text = "tables:\n  T:\n    columns:\n      A: a\n    column_units:\n      Ghost: rial\n"
        with pytest.raises(ValueError, match=r"column_units names column\(s\) \['Ghost'\]"):
            validate_schema_yaml_text(text)

    def test_a_list_is_refused(self):
        text = "tables:\n  T:\n    columns:\n      A: a\n    column_units: [A]\n"
        with pytest.raises(ValueError, match="column_units must be a map"):
            validate_schema_yaml_text(text)
