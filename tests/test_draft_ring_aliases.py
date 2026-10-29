# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``scripts/draft_policies.py --ring-aliases``: real ``ring_aliases`` entries from the stored ring values.

A ``ring_aliases`` key is the exact stored value of the ring dimension's name
column. The draft adds an entry for each value that has none: the value itself
and, if the operator names a generic leading word, the value without it. What is
pinned: nothing is drafted without the option, existing entries and ``synonyms``
and comments and line endings stay byte for byte, a value that already has an
entry is not touched, the forms are conservative, the drafts load with the
application's validator, nothing is written into the live directory without
``--overwrite-live``, and no stored value is printed. Every name is invented.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import config as cfg
from core.policy_drafts import draft_ring_aliases, ring_forms
from knowledge.config_loader import AliasesConfig, validate_yaml_text
from schema_data.yaml_text import LayoutError
from scripts import draft_policies as command
from tests.test_draft_policies import MEMORY, SESSION, Reader

_EXAMPLE = Path(__file__).resolve().parent.parent / "project_config.example"

ALIASES = """\
# curated by hand
ring_aliases:
  # the first ring
  "Hall Alpha":
    - "alpha"      # what analysts say
    - "hall alpha"

synonyms:
  "annual": ["year", "date"]
"""

VALUES = ["Hall Alpha", "Hall Export", "Hall Industrial", "Spot"]


def _load(text: str):
    return validate_yaml_text("aliases.yaml", text, AliasesConfig)


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    target = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE, target)
    (target / "memory_policy.yaml").write_text(MEMORY, encoding="utf-8")
    (target / "session_policy.yaml").write_text(SESSION, encoding="utf-8")
    (target / "aliases.yaml").write_text(ALIASES, encoding="utf-8", newline="\n")
    return target


def _run(config_dir: Path, tmp_path: Path, *extra: str, values=VALUES):
    out = tmp_path / "draft"
    code = command.main(["--config-dir", str(config_dir), "--output", str(out), *extra], read_values=Reader(values))
    return code, out


class TestForms:
    def test_the_value_itself_is_always_a_form(self):
        assert ring_forms("Spot")[0] == "Spot"
        assert ring_forms("Spot", ["Hall"]) == ["Spot"]

    def test_a_leading_generic_word_is_cut_as_a_whole_word_only(self):
        assert ring_forms("Hall Export", ["hall"]) == ["Hall Export", "Export"]
        assert ring_forms("Hallway Export", ["Hall"]) == ["Hallway Export"]
        assert ring_forms("Export Hall", ["Hall"]) == ["Export Hall"]

    def test_a_value_that_is_only_the_generic_word_or_leaves_one_is_not_cut(self):
        assert ring_forms("Hall", ["Hall"]) == ["Hall"]
        assert ring_forms("Hall Ring", ["Hall", "Ring"]) == ["Hall Ring"]

    def test_persian_letter_forms_are_folded_when_the_word_is_compared(self):
        assert ring_forms("تالار صادراتي", ["تالار"])[1] == "صادراتي"


class TestTheDraftOfTheFile:
    def test_new_entries_are_added_after_the_last_one_and_everything_else_is_byte_for_byte(self):
        drafted = draft_ring_aliases(ALIASES, VALUES, ["Hall"])

        before, after = ALIASES.split("\n"), drafted.text.split("\n")
        assert after[: len(before) - 4] == before[:-4]                       # up to the end of the last entry
        assert drafted.text.endswith('\nsynonyms:\n  "annual": ["year", "date"]\n')
        assert drafted.text.startswith("# curated by hand\nring_aliases:\n  # the first ring\n")
        assert '    - "alpha"      # what analysts say\n' in drafted.text
        assert drafted.added == ("Hall Industrial", "Hall Export", "Spot")
        assert drafted.kept == 1 and drafted.stale == 0

    def test_the_result_is_the_input_plus_exactly_the_new_entries(self):
        drafted = draft_ring_aliases(ALIASES, VALUES, ["Hall"])

        loaded = _load(drafted.text)
        assert loaded.ring_aliases == {
            "Hall Alpha": ["alpha", "hall alpha"],
            "Hall Industrial": ["Hall Industrial", "Industrial"],
            "Hall Export": ["Hall Export", "Export"],
            "Spot": ["Spot"],
        }
        assert list(loaded.ring_aliases) == ["Hall Alpha", "Hall Industrial", "Hall Export", "Spot"]
        assert loaded.synonyms == {"annual": ["year", "date"]}

    def test_a_value_that_has_an_entry_is_not_touched_even_when_spelled_differently(self):
        drafted = draft_ring_aliases(ALIASES, ["hall  alpha", "Spot"])

        assert drafted.added == ("Spot",)
        assert _load(drafted.text).ring_aliases["Hall Alpha"] == ["alpha", "hall alpha"]

    def test_longest_value_first_so_a_longer_name_is_found_before_its_prefix(self):
        drafted = draft_ring_aliases("ring_aliases: {}\nsynonyms: {}\n", ["Zone A", "Zone AB", "Zone ABC"])

        assert drafted.added == ("Zone ABC", "Zone AB", "Zone A")

    def test_a_shortened_form_that_two_values_share_is_left_out(self):
        drafted = draft_ring_aliases("ring_aliases: {}\nsynonyms: {}\n", ["Hall Export", "Ring Export", "Hall Spot"], ["Hall", "Ring"])

        forms = _load(drafted.text).ring_aliases
        assert forms["Hall Export"] == ["Hall Export"] and forms["Ring Export"] == ["Ring Export"]
        assert forms["Hall Spot"] == ["Hall Spot", "Spot"]

    def test_an_empty_inline_section_becomes_a_block_and_keeps_its_comment(self):
        drafted = draft_ring_aliases("ring_aliases: {}   # fill in\nsynonyms: {}\n", ["Zone A"])

        assert drafted.text.startswith("ring_aliases: # fill in\n  \"Zone A\":\n")
        assert _load(drafted.text).ring_aliases == {"Zone A": ["Zone A"]}

    def test_no_file_gives_a_new_one_that_loads(self):
        drafted = draft_ring_aliases("", ["Zone A"])

        assert _load(drafted.text).ring_aliases == {"Zone A": ["Zone A"]} and _load(drafted.text).synonyms == {}
        assert drafted.kept == 0

    def test_crlf_line_endings_and_a_byte_order_mark_are_kept(self):
        text = "﻿" + ALIASES.replace("\n", "\r\n")

        drafted = draft_ring_aliases(text, VALUES)

        assert drafted.text.startswith("﻿# curated by hand\r\n")
        assert "\r\n" in drafted.text and drafted.text.replace("\r\n", "").count("\n") == 0
        assert _load(drafted.text.lstrip("﻿")).ring_aliases["Spot"] == ["Spot"]

    def test_nothing_to_add_returns_the_text_unchanged(self):
        drafted = draft_ring_aliases(ALIASES, ["Hall Alpha"])

        assert drafted.text == ALIASES and drafted.added == ()

    def test_existing_keys_that_are_not_stored_values_stay_and_are_counted(self):
        drafted = draft_ring_aliases(ALIASES, ["Spot"])

        assert drafted.stale == 1 and "Hall Alpha" in _load(drafted.text).ring_aliases

    def test_a_section_written_inline_is_refused_not_rewritten(self):
        with pytest.raises(LayoutError, match="written inline"):
            draft_ring_aliases('ring_aliases: {"Zone A": ["a"]}\nsynonyms: {}\n', ["Spot"])

    def test_a_file_the_application_cannot_load_is_refused(self):
        with pytest.raises(ValueError, match="aliases.yaml"):
            draft_ring_aliases("ring_aliases: [not, a, mapping]\nsynonyms: {}\n", ["Spot"])


class TestTheCommand:
    def test_without_the_option_no_aliases_file_is_drafted(self, config_dir, tmp_path):
        code, out = _run(config_dir, tmp_path)

        assert code == 0 and not (out / "aliases.yaml").exists()

    def test_the_option_drafts_aliases_yaml_beside_the_two_policies(self, config_dir, tmp_path, capsys):
        code, out = _run(config_dir, tmp_path, "--ring-aliases", "--generic-word", "Hall")

        assert code == 0
        loaded = _load((out / "aliases.yaml").read_text(encoding="utf-8"))
        assert loaded.ring_aliases["Hall Export"] == ["Hall Export", "Export"]
        assert loaded.ring_aliases["Hall Alpha"] == ["alpha", "hall alpha"]
        assert (out / "memory_policy.yaml").is_file() and (out / "session_policy.yaml").is_file()
        shown = capsys.readouterr().out
        assert "aliases.yaml: 3 ring_aliases entries added; 1 already there and left as they are" in shown
        assert "Hall Export" not in shown and "Industrial" not in shown          # no stored value is printed

    def test_the_live_aliases_file_is_not_touched(self, config_dir, tmp_path):
        before = (config_dir / "aliases.yaml").read_bytes()

        _run(config_dir, tmp_path, "--ring-aliases")

        assert (config_dir / "aliases.yaml").read_bytes() == before

    def test_a_dry_run_writes_nothing_and_still_counts(self, config_dir, tmp_path, capsys):
        code, out = _run(config_dir, tmp_path, "--ring-aliases", "--dry-run")

        assert code == 0 and not out.exists()
        assert "aliases.yaml: 3 ring_aliases entries added" in capsys.readouterr().out

    def test_the_live_directory_is_refused_without_overwrite_live_and_backed_up_with_it(self, config_dir, tmp_path):
        with cfg.override_settings(project_config_dir=str(config_dir)):
            refused = command.main(["--config-dir", str(config_dir), "--output", str(config_dir), "--ring-aliases"], read_values=Reader(VALUES))
            assert refused == 2
            done = command.main(
                ["--config-dir", str(config_dir), "--output", str(config_dir), "--ring-aliases", "--overwrite-live"],
                read_values=Reader(VALUES),
            )

        assert done == 0
        assert len(list(config_dir.glob("aliases.yaml.bak-*"))) == 1
        assert "Spot" in _load((config_dir / "aliases.yaml").read_text(encoding="utf-8")).ring_aliases

    def test_an_existing_draft_is_not_replaced_without_force(self, config_dir, tmp_path):
        _run(config_dir, tmp_path, "--ring-aliases")
        (tmp_path / "draft" / "aliases.yaml").write_text("mine", encoding="utf-8")

        code, out = _run(config_dir, tmp_path, "--ring-aliases")

        assert code == 2 and (out / "aliases.yaml").read_text(encoding="utf-8") == "mine"

    def test_entries_keyed_by_a_table_are_left_in_and_counted(self, config_dir, tmp_path, capsys):
        (config_dir / "schema.yaml").write_text(
            "tables:\n  Ring:\n    description: 'Rings'\n    columns:\n      ID: 'Primary key'\n      Name: 'Name of the ring'\n"
            "  Currency:\n    description: 'Currencies'\n",
            encoding="utf-8",
        )
        (config_dir / "aliases.yaml").write_text(
            'ring_aliases:\n  "Ring": ["ring"]\n  "Currency": ["currency"]\n  "Hall Alpha": ["alpha"]\nsynonyms: {}\n',
            encoding="utf-8",
        )

        code, out = _run(config_dir, tmp_path, "--ring-aliases")

        assert code == 0
        assert "2 existing key(s) are keys of schema.yaml tables (Ring, Currency): the application ignores them" in capsys.readouterr().out
        assert {"Ring", "Currency", "Hall Alpha"} <= set(_load((out / "aliases.yaml").read_text(encoding="utf-8")).ring_aliases)

    def test_a_layout_that_cannot_be_edited_is_an_error_and_nothing_is_written(self, config_dir, tmp_path, capsys):
        (config_dir / "aliases.yaml").write_text('ring_aliases: {"Zone A": ["a"]}\nsynonyms: {}\n', encoding="utf-8")

        code, out = _run(config_dir, tmp_path, "--ring-aliases")

        assert code == 2 and not out.exists()
        assert "cannot draft aliases.yaml" in capsys.readouterr().err

    def test_generic_word_without_the_option_is_an_error(self, config_dir, tmp_path):
        code, out = _run(config_dir, tmp_path, "--generic-word", "Hall")

        assert code == 2 and not out.exists()
