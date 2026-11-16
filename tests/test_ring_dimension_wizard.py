# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``setup_project.py --ring-dimension TABLE.COLUMN``: ``ring_aliases`` from the stored ring names.

A ``ring_aliases`` key is the exact stored value of the ring dimension's name
column. The wizard of 6.13 to 6.16.2 wrote table keys there (#206, #207) and
has written nothing since 6.16.3, because it cannot know which table holds the
rings. With the option the operator names the column, and the wizard reads it
once and adds an entry for each name that has none.

What is pinned: the keys are stored values and never tables, a curated entry is
never changed, a second run adds nothing, no value reaches the log, and every
way the option can be wrong stops the run before a model is asked. Every name
is invented.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import setup_project as sp
from core.ring_names import table_keyed_ring_keys
from database.schema_inspector import ColumnInfo, TableInfo
from knowledge.config_loader import AliasesConfig
from tests.test_setup_wizard_drafts import RecordingLLM, read_yaml, run_wizard, snapshot_of, table
from tests.test_setup_wizard_output import lab  # noqa: F401 - the two-source fixture with rows


@pytest.fixture(autouse=True)
def _unwrapped_console(monkeypatch):
    """Keep the wizard's console from wrapping its lines (see test_setup_wizard.py)."""
    if hasattr(sp.console, "_width"):
        monkeypatch.setattr(sp.console, "_width", 10_000)


def _dimension(schema: str, name: str, *columns: tuple[str, str]) -> TableInfo:
    cols = [ColumnInfo("ID", "int", is_pk=True), *(ColumnInfo(c, kind) for c, kind in columns)]
    return TableInfo(name=name, schema=schema, columns=cols, classification="dim")


SNAP = snapshot_of(
    _dimension("ref", "Hall", ("Name", "varchar"), ("Rank", "int"), ("Phone", "varchar")),
    _dimension("ref", "Zone", ("Name", "varchar")),
    table("mkt", "Deal", "ID", "Amount", kind="fact"),
)


def run_with_values(out: Path, values: list, *extra: str, llm: RecordingLLM | None = None, snapshot=SNAP) -> int:
    """Run the wizard on the fake database with *values* as what the ring column stores."""
    asked: list[tuple[str, str, int | None]] = []

    def read(self, table_info, column, limit=None):
        asked.append((table_info.full_name, column.name, limit))
        return list(values)

    with patch.object(sp._ValueReader, "__call__", read):
        code = run_wizard(out, snapshot, llm, *extra)
    run_with_values.asked = asked  # type: ignore[attr-defined]
    return code


def _rings(out: Path) -> dict:
    return read_yaml(out / "aliases.yaml")["ring_aliases"]


def _log(out: Path) -> dict:
    return json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. From real rows
# ---------------------------------------------------------------------------

class TestFromRows:
    def test_the_stored_names_become_the_keys_and_data_shaped_values_do_not(self, lab):  # noqa: F811
        run = lab.run("--ring-dimension", "ref.Status.Name")

        assert run.code == 0, run.out
        rings = read_yaml(run.directory / "aliases.yaml")["ring_aliases"]
        # longest first: the first entry with a form in the question wins
        assert list(rings.items()) == [("Pending review", ["Pending review"]), ("Closed", ["Closed"]), ("Open", ["Open"])]
        AliasesConfig(**read_yaml(run.directory / "aliases.yaml"))
        assert "3 ring name(s) from ref.Status.Name (8 read" in run.out
        assert "3 ring_aliases entries added from the stored ring names" in run.out

    def test_the_log_has_counts_and_names_of_columns_but_no_value(self, lab):  # noqa: F811
        run = lab.run("--ring-dimension", "ref.Status.Name")
        log = run.log()
        assert log["ring_dimension"]["table"] == "Status" and log["ring_dimension"]["column"] == "Name"
        assert log["ring_dimension"]["read"] == 8 and log["ring_dimension"]["usable"] == 3
        assert log["step6_write"]["drafted_rings"] == {"added": 3, "already_there": 0, "named_like_a_table": 0}
        text = (run.directory / ".setup_log.json").read_text(encoding="utf-8")
        for value in ("Pending review", "Closed", "09120000009", "10.0.0.7"):
            assert value not in text

    def test_without_the_option_nothing_is_read_and_ring_aliases_stays_empty(self, lab):  # noqa: F811
        run = lab.run()
        assert read_yaml(run.directory / "aliases.yaml")["ring_aliases"] == {}
        assert "ring_dimension" not in run.log() and run.log()["step6_write"]["drafted_rings"] == {}


# ---------------------------------------------------------------------------
# 2. Which entries are written
# ---------------------------------------------------------------------------

class TestEntries:
    def test_one_bounded_read_of_the_named_column(self, tmp_path):
        assert run_with_values(tmp_path / "d", ["North"], "--ring-dimension", "hall.name") == 0
        assert run_with_values.asked == [("ref.Hall", "Name", sp._RING_MAX_VALUES + 1)]

    def test_a_generic_word_adds_the_short_form_unless_two_names_would_share_it(self, tmp_path):
        out = tmp_path / "d"
        values = ["Hall Export", "Hall Spot", "Ring Spot", "Hall"]
        assert run_with_values(out, values, "--ring-dimension", "Zone.Name",
                               "--ring-generic-word", "Hall", "--ring-generic-word", "Ring") == 0
        assert _rings(out) == {
            "Hall Export": ["Hall Export", "Export"], "Hall Spot": ["Hall Spot"], "Ring Spot": ["Ring Spot"],
        }                                               # "Hall" is the table ref.Hall: left out
        assert _log(out)["step6_write"]["drafted_rings"] == {"added": 3, "already_there": 0, "named_like_a_table": 1}

    def test_no_key_is_a_table(self, tmp_path, capsys):
        out = tmp_path / "d"
        assert run_with_values(out, ["Deal", "zone", "North"], "--ring-dimension", "Hall.Name") == 0
        assert list(_rings(out)) == ["North"]
        assert table_keyed_ring_keys(_rings(out), ["Hall", "Zone", "Deal"]) == []
        assert "2 name(s) are also the name of a table and were left out" in capsys.readouterr().out

    def test_a_curated_entry_is_never_changed_and_only_missing_names_are_added(self, tmp_path):
        out = tmp_path / "d"
        out.mkdir()
        (out / "aliases.yaml").write_text(
            'ring_aliases:\n  "North": ["north", "شمال"]\n  "Old Ring": ["old"]\nsynonyms: {}\n', encoding="utf-8",
        )
        assert run_with_values(out, ["north", "South"], "--ring-dimension", "Hall.Name") == 0
        assert _rings(out) == {"North": ["north", "شمال"], "Old Ring": ["old"], "South": ["South"]}
        assert _log(out)["step6_write"]["drafted_rings"] == {"added": 1, "already_there": 1, "named_like_a_table": 0}

    def test_a_second_run_adds_nothing_and_a_run_without_the_option_keeps_the_entries(self, tmp_path):
        out = tmp_path / "d"
        run_with_values(out, ["North", "South"], "--ring-dimension", "Hall.Name")
        first = _rings(out)
        run_with_values(out, ["North", "South"], "--ring-dimension", "Hall.Name")
        assert _rings(out) == first and _log(out)["step6_write"]["drafted_rings"]["added"] == 0
        run_wizard(out, SNAP)
        assert _rings(out) == first

    def test_the_two_spellings_of_one_name_give_one_entry(self, tmp_path):
        out = tmp_path / "d"
        run_with_values(out, ["تالار علي", "تالار علی"], "--ring-dimension", "Hall.Name")
        assert list(_rings(out)) == ["تالار علی"]


# ---------------------------------------------------------------------------
# 3. A wrong option stops the run before a model is asked
# ---------------------------------------------------------------------------

class TestRefusals:
    @pytest.mark.parametrize(("reference", "values", "message"), [
        ("Hall", ["North"], "must be TABLE.COLUMN"),
        ("Nowhere.Name", ["North"], "'Nowhere' is not among the 3 table(s)"),
        ("Hall.Title", ["North"], "'Title' is not a column of ref.Hall (columns: ID, Name, Rank, Phone)"),
        ("Hall.Rank", ["North"], "ref.Hall.Rank is a int column"),
        ("Hall.Phone", ["North"], "named like personal or contact data"),
        ("Hall.Name", [f"Name {i:03d} x" for i in range(201)], "more than 200 distinct values"),
        ("Hall.Name", ["2024-05-01", "09120000009"], "gave no usable name (2 value(s) read"),
        ("Hall.Name", [], "gave no usable name (0 value(s) read)"),
    ])
    def test_it_exits_2_says_why_and_asks_no_model(self, tmp_path, capsys, reference, values, message):
        llm = RecordingLLM()
        assert run_with_values(tmp_path / "d", values, "--ring-dimension", reference, llm=llm) == 2
        assert message in capsys.readouterr().out
        assert llm.calls == 0
        assert not (tmp_path / "d" / "aliases.yaml").exists()

    def test_a_name_two_schemas_share_must_be_qualified(self, tmp_path, capsys):
        snap = snapshot_of(_dimension("a", "Hall", ("Name", "varchar")), _dimension("b", "Hall", ("Name", "varchar")))
        assert run_with_values(tmp_path / "d", ["North"], "--ring-dimension", "Hall.Name", snapshot=snap) == 2
        assert "fits 2 tables (a.Hall, b.Hall)" in capsys.readouterr().out
        assert run_with_values(tmp_path / "d", ["North"], "--ring-dimension", "b.Hall.Name", snapshot=snap) == 0
        assert run_with_values.asked == [("b.Hall", "Name", 201)]

    def test_a_generic_word_without_the_dimension_is_refused(self, tmp_path, capsys):
        assert run_wizard(tmp_path / "d", SNAP, None, "--ring-generic-word", "Hall") == 2
        assert "--ring-generic-word needs --ring-dimension" in capsys.readouterr().out
