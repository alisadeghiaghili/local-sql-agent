# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The sync and ``column_units:`` of ``schema.yaml``.

``column_units:`` (``{column: rial | none}``, see
:class:`schema_data.registry.TableDefinition`) is written by hand and read only
by the answer summary. The sync must therefore leave the block exactly as it
is -- except that ``--prune`` removes a column from ``columns:`` and has to
take that column's unit with it, or the file would name a column it no longer
has and stop validating.
"""

from __future__ import annotations

from schema_data.registry import validate_schema_yaml_text
from schema_data.sync import SyncOptions, sync_schema_text, verify_structural_only
from tests._sync_fixtures import catalogue, tbl

SCHEMA = '''\
tables:
  sales.Trade:
    description: "One row per trade"
    columns:
      TradeID: "Primary key"
      Volume: "Units traded"      # no longer in the database
      Value: "Traded value"
    column_units:
      Volume: none
      # the value is in Rials
      Value: rial
    resolvable_columns: []
'''

DB = catalogue(tbl("sales", "Trade", [("TradeID", "int"), ("Value", "decimal(18,2)")], pk=["TradeID"]))


def _sync(text: str = SCHEMA, **options):
    return sync_schema_text(text, ["main"], "main", {"main": DB}, SyncOptions(**options))


class TestAPlainSyncLeavesTheUnitsAlone:
    def test_the_block_is_kept_byte_for_byte(self):
        block = (
            "    column_units:\n      Volume: none\n      # the value is in Rials\n      Value: rial\n"
        )
        assert block in _sync().text

    def test_the_result_still_validates_and_keeps_the_units(self):
        parsed = validate_schema_yaml_text(_sync().text)
        assert parsed.tables["sales.Trade"].column_units == {"Volume": "none", "Value": "rial"}

    def test_the_stale_column_is_only_marked(self):
        assert "# not in database" in _sync().text


class TestPruneTakesTheUnitWithTheColumn:
    def test_the_pruned_columns_unit_is_removed_and_the_rest_kept(self):
        result = _sync(prune=True)
        parsed = validate_schema_yaml_text(result.text)
        table = parsed.tables["sales.Trade"]
        assert list(table.columns) == ["TradeID", "Value"]
        assert table.column_units == {"Value": "rial"}
        assert "# the value is in Rials\n      Value: rial" in result.text

    def test_pruning_the_only_unit_leaves_a_valid_file(self):
        text = SCHEMA.replace(
            "      Volume: none\n      # the value is in Rials\n      Value: rial\n",
            "      Volume: none\n",
        )
        parsed = validate_schema_yaml_text(_sync(text, prune=True).text)
        assert parsed.tables["sales.Trade"].column_units == {}

    def test_the_structural_check_accepts_the_pruned_file(self):
        original = validate_schema_yaml_text(SCHEMA)
        result = _sync(prune=True)
        verify_structural_only(original, validate_schema_yaml_text(result.text), result.plan)

    def test_a_file_without_units_prunes_as_before(self):
        text = SCHEMA.replace(
            "    column_units:\n      Volume: none\n      # the value is in Rials\n      Value: rial\n", "",
        )
        parsed = validate_schema_yaml_text(_sync(text, prune=True).text)
        assert list(parsed.tables["sales.Trade"].columns) == ["TradeID", "Value"]
