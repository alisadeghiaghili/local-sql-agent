# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The setup wizard no longer writes a table's aliases into ``ring_aliases``.

A ``ring_aliases`` key is the exact stored value of a ring; the wizard of 6.13 to
6.16.2 wrote one entry per *table* (key = the table's key, values = its aliases),
which made a question that named a table filter the ring by the table's name.
Table aliases belong in ``entities.yaml``, where the wizard has always written
them. A run now

* writes no ``ring_aliases`` entry of its own;
* drops, from an earlier ``aliases.yaml``, the entries whose key names a table,
  keeping their aliases in the table's entity, and says how many and which in the
  summary and in ``.setup_log.json``;
* touches no other entry, whatever words a ring's name shares with a table.

What a curated file keeps through a run is pinned in
``tests/test_ring_aliases_pins.py``. Every name is invented.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import setup_project as sp
from core import ring_names
from core.wizard_drafts import drop_table_rings
from tests.test_setup_wizard_drafts import RecordingLLM, read_yaml, run_wizard, snapshot_of, table
from tests.test_setup_wizard_drafts import SilentLLM


@pytest.fixture(autouse=True)
def _unwrapped_console(monkeypatch):
    """Keep the wizard's console from wrapping its lines (see test_setup_wizard.py)."""
    if hasattr(sp.console, "_width"):
        monkeypatch.setattr(sp.console, "_width", 10_000)


def _write(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


#: An aliases.yaml of 6.16.2: a key per table, then two rings the operator added by hand.
WIZARD_ALIASES = (
    "ring_aliases:\n"
    "  Hall: [hall, trading hall]\n"
    "  Region: [region]\n"
    "  sales.Trade: [trade, deal]\n"
    '  "North Hall": ["north hall", "north"]\n'
    '  "Region A": ["region a", "alpha"]\n'
    "synonyms:\n"
    '  "annual": ["year", "date"]\n'
)

REAL_RINGS = {"North Hall": ["north hall", "north"], "Region A": ["region a", "alpha"]}


def _tables():
    return (table("ref", "Hall"), table("ref", "Region"), table("sales", "Trade", kind="fact"), table("ref", "Trade"))


def _log(out: Path) -> dict:
    return json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))


class TestAFreshRunWritesNoTableAliasesIntoRingAliases:
    def test_ring_aliases_is_empty_and_the_aliases_are_in_entities(self, tmp_path):
        out = tmp_path / "draft"

        assert run_wizard(out, snapshot_of(table("ref", "Hall"), table("ref", "Region"))) == 0

        written = read_yaml(out / "aliases.yaml")
        assert written["ring_aliases"] == {}
        entities = read_yaml(out / "entities.yaml")["entities"]
        assert entities["Hall"]["aliases"] == ["alias of ref.Hall"]
        assert entities["Region"]["aliases"] == ["alias of ref.Region"]

    def test_the_log_keeps_the_shape_with_no_ring_aliases(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("ref", "Hall")))

        result = _log(out)["step3_aliases"]["result"]

        assert result["ring_aliases"] == {}
        assert result["entities"]["Hall"]["aliases"] == ["alias of ref.Hall"]

    def test_a_run_that_has_nothing_to_drop_says_nothing_about_it(self, tmp_path, capsys):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("ref", "Hall")))

        assert "ring_aliases entr" not in capsys.readouterr().out
        assert _log(out)["step6_write"]["dropped_table_rings"]["count"] == 0


class TestAMergeDropsTheTableKeyedEntriesOnly:
    def _earlier(self, out: Path) -> None:
        _write(out, "aliases.yaml", WIZARD_ALIASES)
        _write(out, "entities.yaml", (
            "entities:\n"
            "  Hall: {table: Hall, schema_name: ref, aliases: [hall, trading hall]}\n"
            "  Region: {table: Region, schema_name: ref, aliases: [region]}\n"
            "  sales.Trade: {table: Trade, schema_name: sales, aliases: [trade, deal]}\n"
        ))

    def test_table_keyed_entries_go_and_the_rings_and_synonyms_stay_as_written(self, tmp_path):
        out = tmp_path / "draft"
        self._earlier(out)

        assert run_wizard(out, snapshot_of(*_tables()), RecordingLLM()) == 0

        written = read_yaml(out / "aliases.yaml")
        assert written["ring_aliases"] == REAL_RINGS
        assert list(written["ring_aliases"]) == ["North Hall", "Region A"]
        assert written["synonyms"] == {"annual": ["year", "date"]}

    def test_the_summary_and_the_log_count_them_and_name_the_keys(self, tmp_path, capsys):
        out = tmp_path / "draft"
        self._earlier(out)

        run_wizard(out, snapshot_of(*_tables()), RecordingLLM())

        summary = capsys.readouterr().out
        assert "aliases.yaml: 3 ring_aliases entries from an earlier run were keyed by a table" in summary
        assert "dropped: Hall, Region, sales.Trade" in summary
        assert "Entries that are ring names were not touched." in summary
        done = _log(out)["step6_write"]["dropped_table_rings"]
        assert done["count"] == 3 and done["keys"] == ["Hall", "Region", "sales.Trade"]
        assert done["unplaced"] == []
        assert "North Hall" not in json.dumps(done)

    def test_the_aliases_of_a_dropped_entry_that_only_it_held_land_in_the_entity(self, tmp_path):
        out = tmp_path / "draft"
        _write(out, "aliases.yaml", 'ring_aliases:\n  Hall: [hall, "the old hall name"]\nsynonyms: {}\n')
        _write(out, "entities.yaml", "entities:\n  Hall: {table: Hall, schema_name: ref, aliases: [hall]}\n")

        # The model has nothing for Hall this time, so the entity keeps what it had and gains the ring entry's aliases.
        run_wizard(out, snapshot_of(table("ref", "Hall")), SilentLLM(aliases=("ref.Hall",)))

        assert read_yaml(out / "entities.yaml")["entities"]["Hall"]["aliases"] == ["hall", "the old hall name"]
        assert read_yaml(out / "aliases.yaml")["ring_aliases"] == {}
        assert _log(out)["step6_write"]["dropped_table_rings"]["aliases_added_to_entities"] == 1

    def test_a_table_with_no_entity_gets_one_from_the_dropped_entry(self, tmp_path):
        out = tmp_path / "draft"
        _write(out, "aliases.yaml", 'ring_aliases:\n  Old: [old]\nsynonyms: {}\n')
        _write(out, "schema.yaml", "tables:\n  Old:\n    description: 'An old table'\n    db_schema: legacy\n")

        run_wizard(out, snapshot_of(table("ref", "Hall")))

        entities = read_yaml(out / "entities.yaml")["entities"]
        assert entities["Old"] == {"aliases": ["old"], "table": "Old", "schema_name": "legacy"}
        assert read_yaml(out / "aliases.yaml")["ring_aliases"] == {}
        assert _log(out)["step6_write"]["dropped_table_rings"]["entities_created"] == 1

    def test_a_ring_that_contains_a_table_word_is_kept_and_not_counted(self, tmp_path, capsys):
        out = tmp_path / "draft"
        _write(out, "aliases.yaml", 'ring_aliases:\n  "North Hall": ["north hall"]\n  "Hall Export": ["export"]\nsynonyms: {}\n')

        run_wizard(out, snapshot_of(table("ref", "Hall")))

        assert read_yaml(out / "aliases.yaml")["ring_aliases"] == {"North Hall": ["north hall"], "Hall Export": ["export"]}
        assert _log(out)["step6_write"]["dropped_table_rings"]["count"] == 0
        assert "ring_aliases entr" not in capsys.readouterr().out

    def test_a_dry_run_reports_and_writes_nothing(self, tmp_path, capsys):
        out = tmp_path / "draft"
        self._earlier(out)
        before = (out / "aliases.yaml").read_bytes()

        run_wizard(out, snapshot_of(*_tables()), RecordingLLM(), "--dry-run")

        assert (out / "aliases.yaml").read_bytes() == before
        assert "3 ring_aliases entries from an earlier run were keyed by a table" in capsys.readouterr().out

    def test_the_key_of_a_6_14_run_that_qualifies_every_table_is_dropped_too(self, tmp_path):
        out = tmp_path / "draft"
        _write(out, "aliases.yaml", 'ring_aliases:\n  Ref_Dim.Date: [day]\n  "Region A": ["a"]\nsynonyms: {}\n')

        run_wizard(out, snapshot_of(table("Ref_Dim", "Date"), table("Sales_Dim", "Region")))

        assert read_yaml(out / "aliases.yaml")["ring_aliases"] == {"Region A": ["a"]}


class TestResume:
    def test_a_log_of_6_16_2_does_not_bring_its_table_keyed_ring_aliases_back(self):
        legacy = {"step3_aliases": {
            "completed_at": "t", "language": "en", "provider": "x",
            "result": {
                "entities": {"Hall": {"table": "Hall", "schema": "ref", "aliases": ["hall"]}},
                "ring_aliases": {"Hall": ["hall"]},
            },
        }}

        aliases, _, _ = sp._restorable_results(legacy, "en")

        assert aliases["entities"]["Hall"]["aliases"] == ["hall"]
        assert aliases["ring_aliases"] == {}


class TestDropTableRingsOnItsOwn:
    VIEW = ring_names.table_view([
        ("Hall", "ref", "Hall"), ("sales.Trade", "sales", "Trade"), ("ref.Trade", "ref", "Trade"), ("Date", "Ref_Dim", "Date"),
    ])

    def test_a_key_is_a_table_by_its_key_a_bare_name_or_a_schema_spelling(self):
        rings = {"hall": ["a"], "sales.Trade": ["b"], "Ref_Dim.Date": ["c"], "Hall Export": ["d"], "Region A": ["e"]}

        cleaned = drop_table_rings(rings, {}, self.VIEW)

        assert cleaned.dropped == ("hall", "sales.Trade", "Ref_Dim.Date")
        assert cleaned.ring_aliases == {"Hall Export": ["d"], "Region A": ["e"]}

    def test_nothing_to_drop_returns_the_entities_untouched(self):
        entities = {"Hall": {"table": "Hall", "schema": "ref", "aliases": ["h"]}}

        cleaned = drop_table_rings({"North Hall": ["n"]}, entities, self.VIEW)

        assert cleaned.entities is entities and cleaned.dropped == () and cleaned.ring_aliases == {"North Hall": ["n"]}

    def test_a_bare_name_that_fits_two_tables_has_no_single_entity_to_go_to(self):
        cleaned = drop_table_rings({"Trade": ["t"]}, {}, self.VIEW)

        assert cleaned.dropped == ("Trade",) and cleaned.unplaced == ("Trade",) and cleaned.created == 0

    def test_aliases_are_united_with_the_entitys_without_repeats_in_letter_case(self):
        entities = {"Hall": {"table": "Hall", "schema": "ref", "aliases": ["Hall"]}}

        cleaned = drop_table_rings({"Hall": ["hall", "main hall"]}, entities, self.VIEW)

        assert cleaned.entities["Hall"]["aliases"] == ["Hall", "main hall"] and cleaned.carried == 1
        assert entities["Hall"]["aliases"] == ["Hall"]             # the argument is not changed
