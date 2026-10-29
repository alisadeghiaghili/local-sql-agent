# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""What a curated ``aliases.yaml`` does today, pinned before ``ring_aliases`` is guarded.

``ring_aliases`` maps the exact value of a ring dimension's name column to the
surface forms analysts use for it. ``ValueRetriever.extract_ring`` finds a
surface form in the question and returns the key, which becomes the ring filter.
A file whose keys are real ring names must go on behaving exactly as it does
now, whatever the tables of ``schema.yaml`` are called, so this module pins:

* ``extract_ring``: the key of the first entry (in file order) with a form in the
  question, the folding of letter forms, and a ring named like ``North Hall``
  although a table is called ``Hall``;
* the loader: the entries as written, in order, from a file with LF or CRLF lines;
* the preflight: a curated file passes ``Aliases are curated`` with the same text;
* the wizard: a curated ``ring_aliases`` and ``synonyms`` survive a run untouched.

Nothing here may be edited when the guard lands; it is the proof that the guard
changes nothing for a curated file. Every name is invented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config as cfg
import knowledge.aliases as aliases_module
import retrieval.value_retriever as value_retriever_module
import setup_project as sp
from core.yaml_loading import safe_load_strict
from knowledge.config_loader import load_aliases
from retrieval.value_retriever import ValueRetriever
from scripts import verify_deployment as verify
from tests.test_setup_wizard_drafts import RecordingLLM, read_yaml, run_wizard, snapshot_of, table

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Real ring names (the exact values of a name column), with the forms analysts type.
CURATED_RINGS = {
    "Region A": ["region a", "alpha region", "alpha"],
    "Region B": ["region b", "beta"],
    "North Hall": ["north hall", "northern hall", "north"],
    "Export Hall": ["export hall", "export"],
}

CURATED_TEXT = """\
ring_aliases:
  "Region A":
    - "region a"
    - "alpha region"
    - "alpha"
  "Region B":
    - "region b"
    - "beta"
  "North Hall":
    - "north hall"
    - "northern hall"
    - "north"
  "Export Hall":
    - "export hall"
    - "export"

synonyms:
  "annual": ["year", "date"]
  "buyer": ["customer", "purchase"]
"""

#: A schema whose tables are called like words inside the ring names above.
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


@pytest.fixture()
def config_dir(tmp_path):
    """A ``project_config/`` in effect whose files the test writes; the aliases cache is fresh."""
    with cfg.override_settings(project_config_dir=str(tmp_path)):
        yield tmp_path


@pytest.fixture()
def fresh_aliases(monkeypatch):
    """``knowledge.aliases`` reloads from the configured directory on the next access."""
    monkeypatch.setattr(aliases_module, "_cache", {})


def _write(directory: Path, name: str, text: str, newline: str = "\n") -> None:
    (directory / name).write_bytes(text.replace("\n", newline).encode("utf-8"))


# ---------------------------------------------------------------------------
# 1. extract_ring
# ---------------------------------------------------------------------------

class TestExtractRingOnACuratedFile:
    @pytest.fixture(autouse=True)
    def _rings(self, monkeypatch):
        monkeypatch.setattr(value_retriever_module, "RING_ALIASES", CURATED_RINGS)

    @pytest.mark.parametrize(("question", "ring"), [
        ("trades in region a last year", "Region A"),
        ("Trades in the ALPHA region", "Region A"),
        ("show beta totals", "Region B"),
        ("sales in the northern hall", "North Hall"),
        ("sales at the export hall in 1402", "Export Hall"),
        ("export totals", "Export Hall"),
    ])
    def test_the_key_of_the_ring_a_form_names_is_returned(self, question, ring):
        assert ValueRetriever.extract_ring(question) == ring

    def test_a_question_that_names_no_ring_gives_none(self):
        assert ValueRetriever.extract_ring("list the customers") is None
        assert ValueRetriever.extract_ring("") is None

    def test_a_ring_named_like_a_table_word_still_matches(self):
        # A table is called `Hall` and `Region`; the rings `North Hall` and `Region A`
        # merely contain those words and must be found exactly as before.
        assert ValueRetriever.extract_ring("north hall trades") == "North Hall"
        assert ValueRetriever.extract_ring("region b volumes") == "Region B"

    def test_the_first_entry_in_file_order_wins_when_two_rings_are_named(self):
        assert ValueRetriever.extract_ring("region b and region a") == "Region A"
        assert ValueRetriever.extract_ring("north hall and export hall") == "North Hall"

    def test_retrieve_puts_the_key_in_the_ring_filter(self):
        assert ValueRetriever.retrieve("north hall trades in 1402") == {"Ring": "North Hall", "PersianYear": 1402}


class TestExtractRingFoldsLetterForms:
    def test_arabic_letter_forms_and_zwnj_do_not_change_the_answer(self, monkeypatch):
        monkeypatch.setattr(value_retriever_module, "RING_ALIASES", {
            "تالار الف": ["تالار یک", "می‌خواهم"],
        })
        for question in ("تالار يك", "ميخواهم گزارش"):
            assert ValueRetriever.extract_ring(question) == "تالار الف"


# ---------------------------------------------------------------------------
# 2. The loader
# ---------------------------------------------------------------------------

class TestTheLoaderGivesTheEntriesAsWritten:
    @pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
    def test_a_curated_file_loads_whole_and_in_order(self, config_dir, fresh_aliases, newline):
        _write(config_dir, "aliases.yaml", CURATED_TEXT, newline)
        _write(config_dir, "schema.yaml", SCHEMA_TEXT, newline)

        loaded = aliases_module.RING_ALIASES

        assert loaded == CURATED_RINGS
        assert list(loaded) == list(CURATED_RINGS)
        assert aliases_module.SYNONYMS == {"annual": ["year", "date"], "buyer": ["customer", "purchase"]}

    def test_load_aliases_reads_the_same_entries(self, config_dir):
        _write(config_dir, "aliases.yaml", CURATED_TEXT)

        assert load_aliases().ring_aliases == CURATED_RINGS

    def test_a_file_without_a_schema_loads_whole(self, config_dir, fresh_aliases):
        _write(config_dir, "aliases.yaml", CURATED_TEXT)

        assert aliases_module.RING_ALIASES == CURATED_RINGS

    def test_a_schema_that_does_not_parse_leaves_the_entries_alone(self, config_dir, fresh_aliases):
        _write(config_dir, "aliases.yaml", CURATED_TEXT)
        _write(config_dir, "schema.yaml", "tables: [unclosed\n")

        assert aliases_module.RING_ALIASES == CURATED_RINGS

    def test_the_shipped_template_loads_whole(self, fresh_aliases):
        example = _REPO_ROOT / "project_config.example"
        written = safe_load_strict((example / "aliases.yaml").read_bytes().decode("utf-8"))["ring_aliases"]
        with cfg.override_settings(project_config_dir=str(example)):
            loaded = aliases_module.RING_ALIASES

        assert loaded == written
        assert list(loaded) == ["Hall Industrial", "Hall Export", "Hall Example3"]

    def test_the_loaded_template_still_names_its_halls(self, fresh_aliases, monkeypatch):
        example = _REPO_ROOT / "project_config.example"
        with cfg.override_settings(project_config_dir=str(example)):
            monkeypatch.setattr(value_retriever_module, "RING_ALIASES", aliases_module.RING_ALIASES)
        assert ValueRetriever.extract_ring("export ring trades") == "Hall Export"
        assert ValueRetriever.extract_ring("the industrial market") == "Hall Industrial"


# ---------------------------------------------------------------------------
# 3. The preflight
# ---------------------------------------------------------------------------

class TestAliasesAreCuratedOnACuratedFile:
    NAME = "Aliases are curated"

    def test_real_ring_names_pass_with_the_same_detail_whatever_the_tables_are_called(self, config_dir):
        _write(config_dir, "aliases.yaml", CURATED_TEXT)
        without_schema = verify.check_aliases_are_curated()
        _write(config_dir, "schema.yaml", SCHEMA_TEXT)
        with_schema = verify.check_aliases_are_curated()

        assert without_schema.name == with_schema.name == self.NAME
        assert without_schema.status == with_schema.status == "PASS"
        assert without_schema.detail == "2 synonym key(s), 4 ring alias(es)"
        assert with_schema.detail == without_schema.detail

    def test_a_crlf_file_gives_the_same_verdict(self, config_dir):
        _write(config_dir, "aliases.yaml", CURATED_TEXT, "\r\n")
        _write(config_dir, "schema.yaml", SCHEMA_TEXT, "\r\n")

        result = verify.check_aliases_are_curated()

        assert (result.status, result.detail) == ("PASS", "2 synonym key(s), 4 ring alias(es)")

    def test_the_shipped_template_passes_with_its_counts(self):
        with cfg.override_settings(project_config_dir=str(_REPO_ROOT / "project_config.example")):
            result = verify.check_aliases_are_curated()

        assert (result.status, result.detail) == ("PASS", "8 synonym key(s), 3 ring alias(es)")

    def test_the_entity_key_check_still_accepts_real_ring_names(self, config_dir):
        _write(config_dir, "schema.yaml", SCHEMA_TEXT)
        _write(config_dir, "entities.yaml", "entities:\n  Hall:\n    aliases: [hall]\n    table: Hall\n")
        _write(config_dir, "aliases.yaml", CURATED_TEXT)

        assert verify.check_entities_match_schema_keys().status == "PASS"


# ---------------------------------------------------------------------------
# 4. The wizard keeps a curated aliases.yaml
# ---------------------------------------------------------------------------

class TestTheWizardKeepsACuratedAliasesFile:
    def _run(self, out: Path, *tables) -> int:
        return run_wizard(out, snapshot_of(*tables), RecordingLLM())

    def test_curated_ring_entries_and_synonyms_survive_a_run_unchanged(self, tmp_path):
        out = tmp_path / "draft"
        out.mkdir()
        _write(out, "aliases.yaml", CURATED_TEXT)

        assert self._run(out, table("ref", "Hall"), table("ref", "Region")) == 0

        written = read_yaml(out / "aliases.yaml")
        for ring, forms in CURATED_RINGS.items():
            assert written["ring_aliases"][ring] == forms
        assert written["synonyms"] == {"annual": ["year", "date"], "buyer": ["customer", "purchase"]}

    def test_the_order_of_the_curated_entries_is_kept(self, tmp_path):
        out = tmp_path / "draft"
        out.mkdir()
        _write(out, "aliases.yaml", CURATED_TEXT)

        self._run(out, table("ref", "Hall"))

        kept = [key for key in read_yaml(out / "aliases.yaml")["ring_aliases"] if key in CURATED_RINGS]
        assert kept == list(CURATED_RINGS)

    def test_a_ring_that_merely_contains_a_table_word_is_not_taken_for_the_table(self, tmp_path):
        out = tmp_path / "draft"
        out.mkdir()
        _write(out, "aliases.yaml", 'ring_aliases:\n  "North Hall": ["north hall"]\nsynonyms: {}\n')

        self._run(out, table("ref", "Hall"))

        assert read_yaml(out / "aliases.yaml")["ring_aliases"]["North Hall"] == ["north hall"]

    def test_the_aliases_of_a_table_are_written_to_entities(self, tmp_path):
        out = tmp_path / "draft"

        self._run(out, table("ref", "Hall"), table("ref", "Region"))

        entities = read_yaml(out / "entities.yaml")["entities"]
        assert entities["Hall"]["aliases"] == ["alias of ref.Hall"]
        assert entities["Region"]["aliases"] == ["alias of ref.Region"]

    def test_the_file_renderer_keeps_the_given_entries(self):
        text = sp._aliases_yaml(CURATED_RINGS, {"annual": ["year"]}, "2026-01-01T00:00:00")

        parsed = safe_load_strict(text)
        assert parsed["ring_aliases"] == CURATED_RINGS and list(parsed["ring_aliases"]) == list(CURATED_RINGS)
        assert parsed["synonyms"] == {"annual": ["year"]}
