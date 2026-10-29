# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A ``ring_aliases`` entry keyed by a table is ignored when ``aliases.yaml`` is loaded.

The setup wizard of 6.13 to 6.16.2 wrote one entry per *table* into
``ring_aliases`` (key = the table's ``schema.yaml`` key, values = the table's
aliases), where a key must be the exact stored value of a ring. A question that
named a table by one of its aliases filtered the ring by the table's name
(``WHERE r.[Name] = N'Hall'``) and came back empty. The loader now leaves such
an entry out and logs one WARNING; the entries that are ring names, and a file
whose ``schema.yaml`` cannot be read, are loaded exactly as before (that is
pinned in ``tests/test_ring_aliases_pins.py``). Every name is invented.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import config as cfg
import knowledge.aliases as aliases_module
import retrieval.value_retriever as value_retriever_module
from core import ring_names
from retrieval.value_retriever import ValueRetriever

SCHEMA_TEXT = """\
tables:
  Hall:
    description: 'Halls of the exchange'
    columns:
      ID: 'Primary key'
      Name: 'Name of the hall'
  Region:
    description: 'Regions'
    columns:
      ID: 'Primary key'
      Name: 'Name of the region'
  sales.Trade:
    description: 'Trades'
    db_schema: sales
    columns:
      ID: 'Primary key'
"""

#: What the wizard of 6.13 to 6.16.2 wrote: one entry per table, then the operator's two rings.
WIZARD_TEXT = """\
ring_aliases:
  Hall:
    - "hall"
    - "trading hall"
  Region:
    - "region"
  sales.Trade:
    - "trade"
    - "deal"
  "North Hall":
    - "north hall"
    - "north"
  "Region A":
    - "region a"
    - "alpha"
synonyms: {}
"""

REAL_RINGS = {"North Hall": ["north hall", "north"], "Region A": ["region a", "alpha"]}


@pytest.fixture()
def config_dir(tmp_path, monkeypatch):
    """A ``project_config/`` in effect for the test, and a ``knowledge.aliases`` cache that is empty."""
    monkeypatch.setattr(aliases_module, "_cache", {})
    with cfg.override_settings(project_config_dir=str(tmp_path)):
        yield tmp_path


def _write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))   # CRLF: as a Windows checkout


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "core.ring_names" and r.levelno == logging.WARNING]


class TestTheLoaderIgnoresTableKeyedEntries:
    def test_the_entries_keyed_by_a_table_are_left_out_and_the_rings_stay(self, config_dir):
        _write(config_dir, "aliases.yaml", WIZARD_TEXT)
        _write(config_dir, "schema.yaml", SCHEMA_TEXT)

        loaded = aliases_module.RING_ALIASES

        assert loaded == REAL_RINGS
        assert list(loaded) == ["North Hall", "Region A"]

    def test_one_warning_names_the_count_and_the_keys(self, config_dir, caplog):
        _write(config_dir, "aliases.yaml", WIZARD_TEXT)
        _write(config_dir, "schema.yaml", SCHEMA_TEXT)

        with caplog.at_level(logging.WARNING, logger="core.ring_names"):
            aliases_module.RING_ALIASES
            aliases_module.RING_ALIASES
            aliases_module.SYNONYMS

        (warning,) = _warnings(caplog)
        text = warning.getMessage()
        assert "3 ring_aliases key(s)" in text
        assert "Hall, Region, sales.Trade" in text
        assert "North Hall" not in text and "trading hall" not in text   # no real ring, no alias
        assert "setup wizard" in text and "run it again" in text

    def test_the_match_is_the_whole_key_and_ignores_letter_case(self, config_dir, caplog):
        _write(config_dir, "aliases.yaml", (
            'ring_aliases:\n  "HALL": ["hall"]\n  "SALES.trade": ["trade"]\n  "Halls": ["halls"]\n'
            '  "Hall B": ["hall b"]\n  "Region": ["region"]\nsynonyms: {}\n'
        ))
        _write(config_dir, "schema.yaml", SCHEMA_TEXT)

        with caplog.at_level(logging.WARNING, logger="core.ring_names"):
            loaded = aliases_module.RING_ALIASES

        assert list(loaded) == ["Halls", "Hall B"]
        assert "3 ring_aliases key(s)" in _warnings(caplog)[0].getMessage()
        assert "HALL, SALES.trade, Region" in _warnings(caplog)[0].getMessage()

    def test_a_ring_that_merely_contains_a_table_word_is_kept(self, config_dir, caplog):
        _write(config_dir, "aliases.yaml", 'ring_aliases:\n  "North Hall": ["north hall"]\nsynonyms: {}\n')
        _write(config_dir, "schema.yaml", SCHEMA_TEXT)

        with caplog.at_level(logging.WARNING, logger="core.ring_names"):
            loaded = aliases_module.RING_ALIASES

        assert loaded == {"North Hall": ["north hall"]}
        assert not _warnings(caplog)

    def test_a_file_of_real_rings_is_the_very_object_the_loader_made(self, config_dir, caplog):
        _write(config_dir, "aliases.yaml", 'ring_aliases:\n  "Region A": ["a"]\nsynonyms: {}\n')
        _write(config_dir, "schema.yaml", SCHEMA_TEXT)
        rings = {"Region A": ["a"]}

        assert ring_names.guard_ring_aliases(rings, config_dir) is rings
        assert not _warnings(caplog)

    @pytest.mark.parametrize("schema", [None, "tables: [unclosed\n", "tables: 3\n", ""], ids=["absent", "broken", "no-map", "empty"])
    def test_without_a_readable_schema_every_entry_is_kept(self, config_dir, caplog, schema):
        _write(config_dir, "aliases.yaml", WIZARD_TEXT)
        if schema is not None:
            _write(config_dir, "schema.yaml", schema)

        with caplog.at_level(logging.WARNING, logger="core.ring_names"):
            loaded = aliases_module.RING_ALIASES

        assert list(loaded) == ["Hall", "Region", "sales.Trade", "North Hall", "Region A"]
        assert not _warnings(caplog)

    def test_synonyms_are_not_touched(self, config_dir):
        _write(config_dir, "aliases.yaml", WIZARD_TEXT.replace("synonyms: {}", 'synonyms:\n  "hall": ["hall", "exchange"]'))
        _write(config_dir, "schema.yaml", SCHEMA_TEXT)

        assert aliases_module.SYNONYMS == {"hall": ["hall", "exchange"]}

    def test_a_guard_that_fails_leaves_the_entries_alone(self, config_dir, monkeypatch):
        _write(config_dir, "aliases.yaml", WIZARD_TEXT)
        _write(config_dir, "schema.yaml", SCHEMA_TEXT)

        def boom(*_args, **_kwargs):
            raise RuntimeError("schema cannot be read")

        monkeypatch.setattr(ring_names, "schema_table_keys", boom)

        assert len(aliases_module.RING_ALIASES) == 5


class TestAQuestionNamingAHallNoLongerFiltersByATableName:
    """The reproduction: a wizard-written file, a question that names a hall."""

    @pytest.fixture()
    def retriever(self, config_dir, monkeypatch):
        _write(config_dir, "aliases.yaml", WIZARD_TEXT)
        _write(config_dir, "schema.yaml", SCHEMA_TEXT)
        monkeypatch.setattr(value_retriever_module, "RING_ALIASES", aliases_module.RING_ALIASES)
        return ValueRetriever

    def test_a_question_that_names_a_hall_gives_the_hall(self, retriever):
        assert retriever.extract_ring("trades in the north hall last year") == "North Hall"

    def test_a_question_about_the_halls_gives_no_ring_instead_of_a_table_name(self, retriever):
        # "halls" contains the table Hall's alias "hall"; before the guard the answer was 'Hall'.
        assert retriever.extract_ring("list the halls") is None

    def test_a_question_that_names_a_trade_gives_no_ring(self, retriever):
        assert retriever.extract_ring("show the latest deal") is None
        assert retriever.retrieve("show the latest deal in 1402") == {"PersianYear": 1402}
