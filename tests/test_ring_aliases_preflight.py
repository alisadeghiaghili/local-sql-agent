# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``Aliases are curated`` fails on ``ring_aliases`` keys that are table keys, and advises on stale ones.

A ``ring_aliases`` key is the exact value of the ring dimension's name column.
The setup wizard of 6.13 to 6.16.2 wrote a key per *table* instead, and the
preflight passed that file (``0 synonym key(s), 75 ring alias(es)``). The check
now fails when a key is, as a whole and ignoring letter case, a key of
``schema.yaml`` -- exactly the entries the application ignores -- and says how to
get out. When the application has cached the ring dimension (the running
server; the command line has no cache), a PASS also counts the keys that are
not among the cached values. A curated file keeps passing with the same detail
(``tests/test_ring_aliases_pins.py``). Every name is invented.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config as cfg
from scripts import verify_deployment as verify

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE = _REPO_ROOT / "project_config.example"

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

NAME = "Aliases are curated"


@pytest.fixture()
def project(tmp_path):
    with cfg.override_settings(project_config_dir=str(tmp_path)):
        yield tmp_path


def _write(directory: Path, name: str, text: str) -> None:
    directory.joinpath(name).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))


def _rings(*keys: str) -> str:
    return "ring_aliases:\n" + "".join(f'  "{key}": ["{key.lower()}"]\n' for key in keys) + 'synonyms:\n  "annual": ["year"]\n'


class TestTableKeyedRingAliasesFail:
    def test_the_wizard_s_file_fails_with_the_count_the_keys_and_the_way_out(self, project):
        _write(project, "schema.yaml", SCHEMA_TEXT)
        _write(project, "aliases.yaml", _rings("Hall", "Region", "sales.Trade"))

        result = verify.check_aliases_are_curated()

        assert result.name == NAME and result.status == "FAIL"
        assert "3 of 3 ring_aliases key(s) are keys of schema.yaml tables: Hall, Region, sales.Trade" in result.detail
        assert "exact value of the ring dimension's name column" in result.detail
        assert "python setup_project.py" in result.detail and "restore the curated ring names" in result.detail
        assert "docs/deployment-runbook.md" in result.detail
        assert result.render().isascii()

    def test_real_rings_beside_table_keys_are_counted_out_of_all_entries(self, project):
        _write(project, "schema.yaml", SCHEMA_TEXT)
        _write(project, "aliases.yaml", _rings("Hall", "North Hall", "Region A", "Region"))

        result = verify.check_aliases_are_curated()

        assert result.status == "FAIL"
        assert "2 of 4 ring_aliases key(s)" in result.detail and ": Hall, Region --" in result.detail
        assert "North Hall" not in result.detail and "Region A" not in result.detail

    def test_the_comparison_ignores_letter_case_and_nothing_else(self, project):
        _write(project, "schema.yaml", SCHEMA_TEXT)
        _write(project, "aliases.yaml", _rings("HALL", "Halls", "Region B"))

        result = verify.check_aliases_are_curated()

        assert result.status == "FAIL"
        assert "1 of 3 ring_aliases key(s) are keys of schema.yaml tables: HALL --" in result.detail

    def test_a_long_list_is_capped(self, project):
        _write(project, "schema.yaml", "tables:\n" + "".join(f"  T{n}: {{}}\n" for n in range(15)))
        _write(project, "aliases.yaml", _rings(*[f"T{n}" for n in range(15)]))

        detail = verify.check_aliases_are_curated().detail

        assert "15 of 15 ring_aliases key(s)" in detail and "T9, and 5 more" in detail and "T10" not in detail

    def test_a_file_with_only_real_rings_passes_whatever_the_tables_are_called(self, project):
        _write(project, "schema.yaml", SCHEMA_TEXT)
        _write(project, "aliases.yaml", _rings("North Hall", "Region A", "Export Hall"))

        result = verify.check_aliases_are_curated()

        assert (result.status, result.detail) == ("PASS", "1 synonym key(s), 3 ring alias(es)")

    @pytest.mark.parametrize("schema", [None, "tables: [unclosed\n"], ids=["absent", "broken"])
    def test_without_a_readable_schema_there_is_nothing_to_compare_and_it_passes(self, project, schema):
        if schema is not None:
            _write(project, "schema.yaml", schema)
        _write(project, "aliases.yaml", _rings("Hall", "Region"))

        assert verify.check_aliases_are_curated().status == "PASS"

    def test_the_seed_failure_keeps_its_own_message(self, project):
        _write(project, "schema.yaml", SCHEMA_TEXT)
        _write(project, "aliases.yaml", (
            'ring_aliases:\n  "Hall": ["hall"]\nsynonyms:\n'
            '  "2024-05-01 10:00:00": ["t"]\n  "2024-05-02 10:00:00": ["t"]\n  "word": ["w"]\n'
        ))

        result = verify.check_aliases_are_curated()

        assert result.status == "FAIL"
        assert "2 of 3 synonym keys are not words" in result.detail
        assert "ring_aliases key(s) are keys" not in result.detail

    def test_the_shipped_template_still_passes_with_the_same_detail(self):
        with cfg.override_settings(project_config_dir=str(_EXAMPLE)):
            result = verify.check_aliases_are_curated()

        assert (result.status, result.detail) == ("PASS", "8 synonym key(s), 3 ring alias(es)")


class TestKeysOutsideTheCachedRingVocabulary:
    @pytest.fixture()
    def configured(self, project):
        for name in ("session_policy.yaml", "memory_policy.yaml"):
            # a checkout may hold CRLF lines: take the text with LF so _write adds them once
            _write(project, name, (_EXAMPLE / name).read_bytes().decode("utf-8").replace("\r\n", "\n"))
        _write(project, "schema.yaml", SCHEMA_TEXT.replace("  Region:", "  Ring:"))
        return project

    def test_stale_keys_are_counted_in_the_pass_detail_without_naming_them(self, configured):
        _write(configured, "aliases.yaml", _rings("Region A", "Region B", "Region C"))

        with patch.object(verify, "_cached_dimension_values", return_value=["Region A", "Region B"]):
            result = verify.check_aliases_are_curated()

        assert result.status == "PASS"
        assert result.detail.startswith("1 synonym key(s), 3 ring alias(es); advice: 1 of 3 ring_aliases key(s)")
        assert "not among the 2 cached value(s) of Ring.Name" in result.detail
        assert "Region C" not in result.detail and "Region A" not in result.detail

    def test_keys_that_are_all_cached_add_nothing(self, configured):
        _write(configured, "aliases.yaml", _rings("Region A", "Region B"))

        with patch.object(verify, "_cached_dimension_values", return_value=["Region B", "Region A", "Region X"]):
            result = verify.check_aliases_are_curated()

        assert result.detail == "1 synonym key(s), 2 ring alias(es)"

    def test_the_comparison_folds_letter_forms(self, configured):
        _write(configured, "aliases.yaml", _rings("تالار یک"))

        with patch.object(verify, "_cached_dimension_values", return_value=["تالار يك"]):
            assert "advice" not in verify.check_aliases_are_curated().detail

    def test_with_nothing_cached_there_is_no_advice(self, configured):
        _write(configured, "aliases.yaml", _rings("Region A"))

        with patch.object(verify, "_cached_dimension_values", return_value=None):
            assert verify.check_aliases_are_curated().detail == "1 synonym key(s), 1 ring alias(es)"

    def test_without_policy_files_there_is_no_advice_and_no_error(self, project):
        _write(project, "schema.yaml", SCHEMA_TEXT)
        _write(project, "aliases.yaml", _rings("Region A"))

        with patch.object(verify, "_cached_dimension_values", return_value=["Other"]):
            assert verify.check_aliases_are_curated().detail == "1 synonym key(s), 1 ring alias(es)"

    def test_the_advice_reads_the_cache_and_never_the_database(self, configured):
        _write(configured, "aliases.yaml", _rings("Region A"))

        with patch("database.connection.get_engine", side_effect=AssertionError("database touched")), \
                patch.object(verify, "_cached_dimension_values", return_value=["Other"]):
            assert "advice" in verify.check_aliases_are_curated().detail
