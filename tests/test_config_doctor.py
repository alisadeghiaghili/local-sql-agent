# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The preflight checks that tell a curated ``project_config/`` from a raw draft.

``core.config_doctor`` reads the files and ``scripts/verify_deployment.py``
turns what it finds into five checks. The inspector's own drafts and the
curated warehouse-shape configuration are exercised in
``tests/test_setup_on_warehouse_shape.py``; this module covers the edges with
small hand-written files: the thresholds, empty and unreadable files, a single
source, the ``STAGING_PATTERNS`` setting, and what a message may and may not
contain. Every name below is invented.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config as cfg
from core import config_doctor as doctor
from scripts import verify_deployment as verify

DRAFT = "# AUTO-GENERATED DRAFT - review and edit before moving to project_config/\n"


def _write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text, encoding="utf-8", newline="\n")


def _schema(*tables: tuple[str, str, str], extra: str = "") -> str:
    """A ``schema.yaml`` text: ``(key, db_schema, description)`` per table."""
    lines = ["tables:"]
    for key, schema, description in tables:
        lines.append(f"  {key}:")
        if schema:
            lines.append(f"    db_schema: {schema}")
        lines.append(f"    description: {description!r}")
        lines.append("    columns:")
        lines.append("      ID: 'Primary key'")
    lines.append(extra)
    return "\n".join(lines) + "\n"


PLACEHOLDER = "s.T \u2014 auto-detected fact table (~10 rows)"
CURATED_ROW = "Sales invoices, one row per invoice"

TWO_SOURCES = (
    "default: main\n"
    "datasources:\n"
    "  main:\n    url_env: DB_URL_MAIN\n"
    "  archive:\n    url_env: DB_URL_ARCHIVE\n"
)
ONE_SOURCE = "datasources:\n  main:\n    url_env: DB_URL_MAIN\n"


@pytest.fixture()
def project(tmp_path):
    """An empty ``project_config/`` in effect for the test; the test writes the files."""
    with cfg.override_settings(project_config_dir=str(tmp_path)):
        yield tmp_path


# ---------------------------------------------------------------------------
# 1. project_config/ is curated, not a raw draft
# ---------------------------------------------------------------------------

class TestProjectConfigIsCurated:
    NAME = "project_config/ is curated, not a raw draft"

    def test_a_curated_file_passes_with_the_counts(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW), ("B", "s", "Payments")))

        result = verify.check_project_config_is_curated()

        assert result.name == self.NAME
        assert result.status == "PASS"
        assert result.detail == (
            "no file carries the AUTO-GENERATED DRAFT header; 0 of 2 table description(s) "
            "are the inspector's placeholder"
        )

    def test_the_header_on_any_yaml_file_fails_and_the_detail_names_it(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW)))
        _write(project, "entities.yaml", DRAFT + "entities: {}\n")
        _write(project, "relationships.yml", "\n\n" + DRAFT + "relationships: []\n")

        result = verify.check_project_config_is_curated()

        assert result.status == "FAIL"
        assert "2 file(s) carry the AUTO-GENERATED DRAFT header: entities.yaml, relationships.yml" in result.detail
        assert "schema.yaml:" not in result.detail
        assert "restore the curated files" in result.detail.lower()
        assert "python scripts/sync_schema.py --add-tables 'Schema.*'" in result.detail
        assert "docs/deployment-runbook.md" in result.detail

    def test_a_header_below_the_first_lines_or_in_another_kind_of_file_is_not_a_draft(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW)))
        _write(project, "entities.yaml", "# note\n" * 12 + DRAFT + "entities: {}\n")
        _write(project, "notes.txt", DRAFT)
        (project / "_test_fixtures").mkdir()
        _write(project / "_test_fixtures", "sample.yaml", DRAFT)

        assert verify.check_project_config_is_curated().status == "PASS"

    def test_the_header_text_in_a_value_is_not_a_header(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", "About the AUTO-GENERATED DRAFT header")))

        assert verify.check_project_config_is_curated().status == "PASS"

    def test_a_reviewed_file_is_told_how_to_clear_the_flag(self, project):
        _write(project, "schema.yaml", DRAFT + _schema(("A", "s", CURATED_ROW)))

        result = verify.check_project_config_is_curated()

        assert result.status == "FAIL"
        assert "delete its header line" in result.detail

    @pytest.mark.parametrize(
        ("placeholders", "total", "status"),
        [(0, 4, "PASS"), (1, 4, "PASS"), (2, 4, "FAIL"), (4, 4, "FAIL"), (1, 3, "FAIL"), (1, 1, "FAIL"), (2, 9, "PASS")],
    )
    def test_the_threshold_is_more_than_a_quarter(self, project, placeholders, total, status):
        rows = [(f"T{i}", "s", PLACEHOLDER if i < placeholders else CURATED_ROW) for i in range(total)]
        _write(project, "schema.yaml", _schema(*rows))

        result = verify.check_project_config_is_curated()

        assert doctor.DRAFT_DESCRIPTION_SHARE == 0.25
        assert result.status == status, result.detail
        if status == "FAIL":
            assert f"schema.yaml: {placeholders} of {total} table descriptions" in result.detail

    def test_the_failing_percentage_is_in_the_detail(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", PLACEHOLDER), ("B", "s", PLACEHOLDER), ("C", "s", CURATED_ROW)))

        assert "2 of 3 table descriptions (66%)" in verify.check_project_config_is_curated().detail

    def test_to_be_filled_in_a_description_counts_and_in_a_comment_does_not(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", "TO BE FILLED"), ("B", "s", "TO BE FILLED")))
        assert verify.check_project_config_is_curated().status == "FAIL"

        # sync_schema.py leaves this comment after a table it appended; the description is just the name.
        _write(
            project, "schema.yaml",
            "tables:\n  A:\n    description: 's.A'  # TO BE FILLED (added by sync_schema.py)\n"
            "  B:\n    description: 's.B'  # TO BE FILLED (added by sync_schema.py)\n",
        )
        assert verify.check_project_config_is_curated().status == "PASS"

    def test_repeated_table_keys_are_all_counted(self, project):
        # Before 6.9.3 the inspector wrote one key per table name, whatever its schema, and the
        # strict loader refuses the file; the doctor must still count every table.
        text = "tables:\n" + "".join(
            f"  Invoice:\n    db_schema: s{i}\n    description: {PLACEHOLDER!r}\n" for i in range(5)
        )
        _write(project, "schema.yaml", text)

        view = doctor.read_schema(project)
        result = verify.check_project_config_is_curated()

        assert view.parsed and len(view.tables) == 5
        assert result.status == "FAIL" and "5 of 5 table descriptions (100%)" in result.detail

    def test_a_schema_that_is_not_yaml_is_counted_from_its_lines(self, project):
        text = (
            "tables:\n"
            "  A:\n    description: \"s.A \u2014 auto-detected dim table\"  # TO BE FILLED: refine\n"
            "  B:\n    description: \"s.B \u2014 auto-detected dim table\"  # TO BE FILLED: refine\n"
            "    columns: [unclosed\n"
        )
        _write(project, "schema.yaml", text)

        view = doctor.read_schema(project)
        result = verify.check_project_config_is_curated()

        assert not view.parsed and view.raw_tables == 2 and view.raw_placeholders == 2
        assert result.status == "FAIL" and "2 of 2 table descriptions" in result.detail

    def test_an_empty_schema_file_has_nothing_to_count(self, project):
        _write(project, "schema.yaml", "")

        result = verify.check_project_config_is_curated()

        assert result.status == "PASS"
        assert "0 of 0" in result.detail

    def test_no_schema_file_skips(self, project):
        result = verify.check_project_config_is_curated()

        assert result.status == "SKIP"
        assert "project_config/ loads" in result.detail

    def test_a_byte_order_mark_and_crlf_line_endings_do_not_matter(self, project):
        (project / "schema.yaml").write_bytes(
            "\ufeff".encode("utf-8")
            + (DRAFT + _schema(("A", "s", PLACEHOLDER))).replace("\n", "\r\n").encode("utf-8")
        )

        result = verify.check_project_config_is_curated()

        assert result.status == "FAIL"
        assert "schema.yaml" in result.detail and "1 of 1" in result.detail


# ---------------------------------------------------------------------------
# 2. Aliases are curated
# ---------------------------------------------------------------------------

def _aliases(rings: str = "{}", synonyms: str = "") -> str:
    return f"ring_aliases: {rings}\nsynonyms:\n{synonyms}"


class TestAliasesAreCurated:
    def test_a_curated_vocabulary_passes(self, project):
        _write(project, "aliases.yaml", _aliases(
            '\n  "Zone A": ["a", "zone a"]', '  "annual": ["year"]\n  "monthly": ["month"]\n',
        ))

        result = verify.check_aliases_are_curated()

        assert result.name == "Aliases are curated"
        assert result.status == "PASS"
        assert result.detail == "2 synonym key(s), 1 ring alias(es)"

    def test_empty_ring_aliases_alone_is_advice_not_a_failure(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW)))
        _write(project, "aliases.yaml", _aliases("{}", '  "annual": ["year"]\n'))

        result = verify.check_aliases_are_curated()

        assert result.status == "PASS"
        assert "advice: ring_aliases is empty" in result.detail
        assert "only the ring filter reads it" in result.detail

    def test_empty_ring_aliases_with_nothing_described_has_no_advice(self, project):
        _write(project, "aliases.yaml", _aliases("{}", '  "annual": ["year"]\n'))

        assert "advice" not in verify.check_aliases_are_curated().detail

    def test_an_aliases_file_with_nothing_in_it_passes(self, project):
        _write(project, "aliases.yaml", "")

        result = verify.check_aliases_are_curated()

        assert result.status == "PASS"
        assert result.detail.startswith("0 synonym key(s), 0 ring alias(es)")

    @pytest.mark.parametrize(
        ("words", "stamps", "status"),
        [(3, 1, "PASS"), (1, 1, "PASS"), (1, 2, "FAIL"), (0, 3, "FAIL"), (2, 3, "FAIL"), (3, 3, "PASS")],
    )
    def test_the_threshold_is_more_than_half_non_words(self, project, words, stamps, status):
        keys = [f'  "word{chr(97 + i)}x": ["t"]\n' for i in range(words)]
        keys += [f'  "2024-05-0{i + 1} 10:00:00": ["t"]\n' for i in range(stamps)]
        _write(project, "aliases.yaml", _aliases("{}", "".join(keys)))

        result = verify.check_aliases_are_curated()

        assert doctor.NON_WORD_SYNONYM_SHARE == 0.5
        assert result.status == status, result.detail
        if status == "FAIL":
            assert f"{stamps} of {words + stamps} synonym keys are not words" in result.detail
            assert "ring_aliases is empty" in result.detail

    def test_numbers_that_yaml_reads_as_numbers_are_not_words(self, project):
        _write(project, "aliases.yaml", _aliases("{}", "  1402: [year]\n  2024: [year]\n  invoice: [bill]\n"))

        assert verify.check_aliases_are_curated().status == "FAIL"

    def test_persian_and_single_letter_keys(self, project):
        assert doctor.non_word_synonym_keys(["\u0641\u0631\u0648\u0634", "x", "2024-05-01T10:00:00Z"]) == 2

    def test_the_review_marker_fails_even_when_the_keys_are_words(self, project):
        seeds = "".join(f'  "name{i}x": ["t", "c"]  # REVIEW\n' for i in range(4))
        _write(project, "aliases.yaml", _aliases("{}", seeds + '  "annual": ["year"]\n'))

        result = verify.check_aliases_are_curated()

        assert result.status == "FAIL"
        assert "4 of 5 synonym entries still end in the inspector's `# REVIEW` marker" in result.detail

    def test_a_few_marked_entries_are_not_a_draft(self, project):
        seeds = '  "name1x": ["t"]  # REVIEW\n  "annual": ["year"]\n  "monthly": ["month"]\n'
        _write(project, "aliases.yaml", _aliases("{}", seeds))

        assert verify.check_aliases_are_curated().status == "PASS"

    def test_an_aliases_draft_that_is_not_yaml_is_read_from_its_lines(self, project):
        # Before 6.9.3 a synonym key holding a quote made the draft unreadable.
        seeds = 'ring_aliases: {}\nsynonyms:\n  "Pipe 5" steel": ["t", "c"]  # REVIEW\n  "plain": ["t", "c"]  # REVIEW\n'
        _write(project, "aliases.yaml", seeds)

        view = doctor.read_aliases(project)
        result = verify.check_aliases_are_curated()

        assert view is not None and not view.parsed
        assert view.ring_aliases == 0 and len(view.synonym_keys) == 2
        assert result.status == "FAIL" and "2 of 2" in result.detail

    def test_the_lines_of_an_unreadable_file_still_count_ring_entries(self, project):
        text = 'ring_aliases:\n  "Zone A": ["a"]\n  "Zone B":\n    - "b"\nsynonyms:\n  "annual": ["year"]\n  bad: [\n'
        _write(project, "aliases.yaml", text)

        view = doctor.read_aliases(project)

        assert view is not None and not view.parsed
        assert view.ring_aliases == 2 and view.synonym_keys == ("annual",)

    def test_no_aliases_file_skips(self, project):
        assert verify.check_aliases_are_curated().status == "SKIP"

    def test_an_aliases_file_that_is_not_a_mapping_has_no_vocabulary(self, project):
        _write(project, "aliases.yaml", "- just\n- a list\n")

        assert verify.check_aliases_are_curated().status == "PASS"


# ---------------------------------------------------------------------------
# 3. Every data source is described
# ---------------------------------------------------------------------------

class TestEveryDataSourceIsDescribed:
    NAME = "Every data source is described"

    def test_without_datasources_yaml_the_default_source_has_every_table(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW), ("B", "s", CURATED_ROW)))

        result = verify.check_every_data_source_is_described()

        assert result.name == self.NAME
        assert result.status == "PASS"
        assert result.detail == "all 1 data source(s) have tables (2 table(s) in schema.yaml)"

    def test_a_single_source_needs_no_datasource_lines(self, project):
        _write(project, "datasources.yaml", ONE_SOURCE)
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW)))

        assert verify.check_every_data_source_is_described().status == "PASS"

    def test_a_lost_database_is_named(self, project):
        _write(project, "datasources.yaml", TWO_SOURCES)
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW), ("B", "s", CURATED_ROW)))

        result = verify.check_every_data_source_is_described()

        assert result.status == "FAIL"
        assert "1 of 2 data source(s) have no table in schema.yaml: archive" in result.detail
        assert "a whole database is missing" in result.detail
        assert "sync_schema.py --add-tables" in result.detail

    def test_tables_without_a_line_belong_to_the_default_not_to_every_source(self, project):
        _write(project, "datasources.yaml", TWO_SOURCES.replace("default: main", "default: archive"))
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW)))

        result = verify.check_every_data_source_is_described()

        assert result.status == "FAIL"
        assert "no table in schema.yaml: main" in result.detail

    def test_a_datasource_line_assigns_a_table(self, project):
        _write(project, "datasources.yaml", TWO_SOURCES)
        _write(
            project, "schema.yaml",
            "tables:\n  A:\n    description: 'a'\n  B:\n    datasource: archive\n    description: 'b'\n",
        )

        assert verify.check_every_data_source_is_described().status == "PASS"

    def test_a_list_counts_for_each_source_it_names(self, project):
        _write(project, "datasources.yaml", TWO_SOURCES)
        _write(project, "schema.yaml", "tables:\n  A:\n    datasource: [main, archive]\n    description: 'a'\n")

        assert verify.check_every_data_source_is_described().status == "PASS"

    def test_a_line_naming_an_unknown_source_assigns_nothing(self, project):
        _write(project, "datasources.yaml", TWO_SOURCES)
        _write(project, "schema.yaml", "tables:\n  A:\n    datasource: elsewhere\n    description: 'a'\n")

        result = verify.check_every_data_source_is_described()

        assert result.status == "FAIL"
        assert "2 of 2 data source(s) have no table in schema.yaml: main, archive" in result.detail

    def test_a_schema_with_no_table_fails(self, project):
        _write(project, "schema.yaml", "tables: {}\n")

        result = verify.check_every_data_source_is_described()

        assert result.status == "FAIL"
        assert result.detail == "schema.yaml describes no table"

    def test_a_schema_that_does_not_load_skips(self, project):
        _write(project, "schema.yaml", "tables: [unclosed\n")

        assert verify.check_every_data_source_is_described().status == "SKIP"

    def test_no_schema_file_skips(self, project):
        assert verify.check_every_data_source_is_described().status == "SKIP"

    def test_an_invalid_datasources_file_skips_and_the_detail_has_no_text_from_it(self, project):
        _write(project, "datasources.yaml", "datasources: {main: {url_env: 'mssql://user:hunter2@host/db'}}\n")
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW)))

        result = verify.check_every_data_source_is_described()

        assert result.status == "SKIP"
        assert "hunter2" not in result.detail
        assert "Tables map to data sources" in result.detail


# ---------------------------------------------------------------------------
# 4. No staging or change-capture tables
# ---------------------------------------------------------------------------

STAGING_SCHEMA = _schema(
    ("Invoice", "sales", CURATED_ROW),
    ("Invoice_Stg", "sales_Stg", "copy"),
    ("Payment", "sales_STG", "copy"),
    ("Receipt", "stg_sales", "copy"),
    ("Refund", "Staging", "copy"),
    ("Order_Inserts", "dbo", "cdcrows"),
    ("Order_Updates", "dbo", "cdcrows"),
    ("Order_DELETES", "Replay", "cdcrows"),
    ("Stg_Notes", "dbo", "a table whose name only looks like staging"),
    ("Return", "", "no schema"),
)


class TestNoStagingTables:
    NAME = "No staging or change-capture tables"

    def test_the_default_patterns_count_tables_per_schema(self, project):
        _write(project, "schema.yaml", STAGING_SCHEMA)

        result = verify.check_no_staging_tables()

        assert result.name == self.NAME
        assert result.status == "PASS"
        assert result.detail.startswith("advice: 7 of 10 table(s) look like staging or change-capture tables (")
        # Largest first; dbo holds two change-capture tables.
        assert "(dbo: 2, " in result.detail
        for schema in ("Replay", "Staging", "sales_Stg", "sales_STG", "stg_sales"):
            assert f"{schema}: 1" in result.detail
        assert "STAGING_PATTERNS" in result.detail

    def test_the_table_prefix_matches_the_table_name_only(self, project):
        _write(project, "schema.yaml", STAGING_SCHEMA)

        with cfg.override_settings(staging_patterns="table:*_inserts"):
            result = verify.check_no_staging_tables()

        assert result.detail.startswith("advice: 1 of 10 table(s)")
        assert "dbo: 1" in result.detail

    def test_a_bare_pattern_matches_the_schema_name_only(self, project):
        _write(project, "schema.yaml", STAGING_SCHEMA)

        with cfg.override_settings(staging_patterns="Stg_*"):
            result = verify.check_no_staging_tables()

        # "Stg_Notes" is a table name; the schema stg_sales is the one match.
        assert result.detail.startswith("advice: 1 of 10 table(s)")
        assert "stg_sales: 1" in result.detail

    def test_a_setting_replaces_the_default_and_whitespace_is_ignored(self, project):
        _write(project, "schema.yaml", STAGING_SCHEMA)

        with cfg.override_settings(staging_patterns=" Replay ,, table:Return "):
            result = verify.check_no_staging_tables()

        assert result.detail.startswith("advice: 2 of 10 table(s)")
        assert "Replay: 1" in result.detail and "(no schema): 1" in result.detail

    def test_an_empty_setting_switches_the_check_off(self, project):
        _write(project, "schema.yaml", STAGING_SCHEMA)

        with cfg.override_settings(staging_patterns=""):
            result = verify.check_no_staging_tables()

        assert result.status == "SKIP"
        assert "STAGING_PATTERNS is empty" in result.detail

    def test_no_match_passes_without_advice(self, project):
        _write(project, "schema.yaml", _schema(("A", "sales", CURATED_ROW), ("B", "ledger", CURATED_ROW)))

        result = verify.check_no_staging_tables()

        assert result.status == "PASS"
        assert result.detail == "none of the 2 table(s) matches STAGING_PATTERNS"

    def test_a_qualified_key_gives_the_schema_when_there_is_no_db_schema(self, project):
        _write(project, "schema.yaml", "tables:\n  sales_Stg.Invoice:\n    description: 'copy'\n  Invoice:\n    description: 'x'\n")

        result = verify.check_no_staging_tables()

        assert result.detail.startswith("advice: 1 of 2 table(s)") and "sales_Stg: 1" in result.detail

    def test_the_schema_of_a_draft_from_before_6_9_3_is_read_from_its_description(self, project):
        staged = "sales_Stg.Invoice \u2014 auto-detected fact table (~3 rows)"
        plain = "sales.Payment \u2014 auto-detected fact table (~3 rows)"
        text = f"tables:\n  Invoice:\n    description: {staged!r}\n  Payment:\n    description: {plain!r}\n"
        _write(project, "schema.yaml", text)

        result = verify.check_no_staging_tables()

        assert result.detail.startswith("advice: 1 of 2 table(s)") and "sales_Stg: 1" in result.detail

    def test_a_database_qualified_schema_is_matched_on_its_last_part(self, project):
        _write(project, "schema.yaml", "tables:\n  A:\n    db_schema: OtherDb.sales_Stg\n    description: 'x'\n")

        assert "OtherDb.sales_Stg: 1" in verify.check_no_staging_tables().detail

    def test_the_detail_lists_schemas_and_counts_and_never_a_description(self, project):
        _write(project, "schema.yaml", STAGING_SCHEMA)

        detail = verify.check_no_staging_tables().detail

        assert "cdcrows" not in detail and "a table whose name" not in detail
        assert "Order_Inserts" not in detail

    def test_the_schema_list_is_capped(self, project):
        rows = [(f"T{i}", f"load_Stg{i}", "x") for i in range(14)]
        _write(project, "schema.yaml", _schema(*rows))

        detail = verify.check_no_staging_tables().detail

        assert "and 4 more" in detail

    def test_a_schema_that_does_not_load_skips(self, project):
        _write(project, "schema.yaml", "tables: [unclosed\n")

        assert verify.check_no_staging_tables().status == "SKIP"

    def test_the_setting_reads_the_environment_and_has_the_documented_default(self, monkeypatch):
        monkeypatch.delenv("STAGING_PATTERNS", raising=False)
        assert cfg.Settings().staging_patterns == "*_Stg,*stg*,*staging*,table:*_Inserts,table:*_Updates,table:*_Deletes"
        monkeypatch.setenv("STAGING_PATTERNS", "Replay")
        assert cfg.Settings().staging_patterns == "Replay"
        monkeypatch.setenv("STAGING_PATTERNS", "")
        assert cfg.Settings().staging_patterns == ""

    def test_the_default_in_env_example_is_the_default_in_code(self):
        from dotenv import dotenv_values

        values = dotenv_values(Path(__file__).resolve().parent.parent / ".env.example")

        assert values["STAGING_PATTERNS"] == "*_Stg,*stg*,*staging*,table:*_Inserts,table:*_Updates,table:*_Deletes"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("*_Stg", [("*_stg", False)]),
            ("table:*_Inserts", [("*_inserts", True)]),
            ("TABLE:x", [("x", True)]),
            ("a, ,b", [("a", False), ("b", False)]),
            ("table:", []),
            ("", []),
        ],
    )
    def test_the_pattern_syntax(self, raw, expected):
        assert [(p.pattern, p.on_table) for p in doctor.parse_staging_patterns(raw)] == expected


# ---------------------------------------------------------------------------
# 5. No live sample values in descriptions
# ---------------------------------------------------------------------------

def _notes(*notes: str, description: str = CURATED_ROW) -> str:
    """One table ``A`` whose columns carry *notes*."""
    lines = ["tables:", "  A:", "    db_schema: s", f"    description: {description!r}", "    columns:"]
    lines += [f"      C{i}: {note!r}" for i, note in enumerate(notes)]
    return "\n".join(lines) + "\n"


class TestNoSampleValuesInDescriptions:
    NAME = "No live sample values in descriptions"

    def _detail(self, project, *notes: str, **kwargs) -> verify.CheckResult:
        _write(project, "schema.yaml", _notes(*notes, **kwargs))
        return verify.check_no_sample_values_in_descriptions()

    def test_a_curated_file_passes_plainly(self, project):
        result = self._detail(project, "Primary key", "Invoice date")

        assert result.name == self.NAME
        assert result.status == "PASS"
        assert result.detail == "no description quotes phone-, id- or address-like sample values"

    @pytest.mark.parametrize(
        ("note", "kinds"),
        [
            ("varchar column; e.g. 09120000001, 09120000002", "phone-like: 1, national-id-like: 0, address-like: 0"),
            ("varchar column; e.g. 02112345678", "phone-like: 1, national-id-like: 0, address-like: 0"),
            ("varchar column; e.g. 1234567", "phone-like: 1, national-id-like: 0, address-like: 0"),
            ("varchar column; e.g. 0012345678", "phone-like: 0, national-id-like: 1, address-like: 0"),
            ("varchar column; e.g. \u06f0\u06f9\u06f1\u06f2\u06f0\u06f0\u06f0\u06f0\u06f0\u06f0\u06f1", "phone-like: 1, national-id-like: 0, address-like: 0"),
            ("varchar column; e.g. 0912 000 0001", "phone-like: 1, national-id-like: 0, address-like: 0"),
            ("varchar column; e.g. +98 912 000 0001", "phone-like: 1, national-id-like: 0, address-like: 0"),
            ("varchar column; e.g. 12 Example Street, Building 4, Floor 2", "phone-like: 0, national-id-like: 0, address-like: 1"),
            ("varchar column; e.g. Unit 9 of the Sample Tower on the main avenue", "phone-like: 0, national-id-like: 0, address-like: 1"),
        ],
    )
    def test_what_counts(self, project, note, kinds):
        result = self._detail(project, note)

        assert result.status == "PASS"
        assert result.detail.startswith("advice: 1 note(s) in 1 table(s) seem to quote live sample values")
        assert f"({kinds})" in result.detail
        assert "tables: s.A" not in result.detail and "tables: A" in result.detail

    @pytest.mark.parametrize(
        "note",
        [
            "varchar column; e.g. Open, Closed, Held",
            "varchar column; e.g. 123456",                       # six digits
            "varchar column; e.g. 12345.678901",                 # a decimal number
            "varchar column; e.g. 2024-01-31, 1402/05/01",
            "varchar column; e.g. 20240131, 14020501",           # a date without separators
            "varchar column; e.g. 20240131103000",               # a timestamp
            "varchar column; e.g. 5455313d, ba5b5011",           # a hash holding a long digit run
            "A code, usually 8 digits (e.g. 12345678). Used to join",
            "No examples here",
        ],
    )
    def test_what_does_not_count(self, project, note):
        assert self._detail(project, note).detail.startswith("no description quotes")

    def test_a_table_description_is_read_too(self, project):
        result = self._detail(project, "Primary key", description="Contacts, e.g. 09120000001")

        assert result.detail.startswith("advice: 1 note(s) in 1 table(s)")

    def test_a_note_with_two_kinds_is_one_note(self, project):
        result = self._detail(project, "varchar column; e.g. 09120000001, 0012345678")

        assert result.detail.startswith("advice: 1 note(s) in 1 table(s)")
        assert "phone-like: 1, national-id-like: 1" in result.detail

    def test_the_detail_has_counts_and_keys_and_never_a_value(self, project):
        result = self._detail(
            project,
            "varchar column; e.g. 09120000001, 09120000002",
            "varchar column; e.g. 0012345678",
            "varchar column; e.g. 12 Example Street, Building 4, Floor 2, Sample City",
        )

        for value in ("09120000001", "09120000002", "0012345678", "Example Street", "Sample City"):
            assert value not in result.detail
        assert "3 note(s) in 1 table(s)" in result.detail

    def test_the_tables_listed_are_capped(self, project):
        text = "tables:\n" + "".join(
            f"  T{i}:\n    description: 'x'\n    columns:\n      C: 'varchar column; e.g. 09120000001'\n"
            for i in range(13)
        )
        _write(project, "schema.yaml", text)

        detail = verify.check_no_sample_values_in_descriptions().detail

        assert "13 note(s) in 13 table(s)" in detail
        assert "T0, T1, T2" in detail and "and 3 more" in detail

    def test_a_schema_that_does_not_load_skips(self, project):
        _write(project, "schema.yaml", "tables: [unclosed\n")

        assert verify.check_no_sample_values_in_descriptions().status == "SKIP"

    def test_an_empty_schema_file_skips(self, project):
        _write(project, "schema.yaml", "")

        assert verify.check_no_sample_values_in_descriptions().status == "SKIP"


# ---------------------------------------------------------------------------
# All of them: cheap, in the report, and the same on the admin card
# ---------------------------------------------------------------------------

CURATION_CHECK_NAMES = (
    "check_project_config_is_curated",
    "check_aliases_are_curated",
    "check_every_data_source_is_described",
    "check_no_staging_tables",
    "check_no_sample_values_in_descriptions",
)


class TestTheChecksInTheReport:
    def test_they_are_part_of_the_checks_after_project_config_loads(self):
        names = [c.__name__ for c in verify.build_checks()]

        for name in CURATION_CHECK_NAMES:
            assert names.count(name) == 1
        loads = names.index("check_project_config_loads")
        assert names[loads + 1: loads + 6] == list(CURATION_CHECK_NAMES)

    def test_none_is_a_deep_check_because_each_only_reads_files(self):
        from api.admin_routes import _DEEP_CHECK_NAMES

        assert not set(CURATION_CHECK_NAMES) & set(_DEEP_CHECK_NAMES)

    def test_they_make_no_database_call(self, project):
        _write(project, "schema.yaml", STAGING_SCHEMA)
        _write(project, "aliases.yaml", _aliases("{}", '  "annual": ["year"]\n'))
        _write(project, "datasources.yaml", TWO_SOURCES)

        with patch("database.connection.get_engine", side_effect=AssertionError("database touched")), \
                patch("sqlalchemy.create_engine", side_effect=AssertionError("database touched")):
            results = [getattr(verify, name)() for name in CURATION_CHECK_NAMES]

        # The second source has no table, which is the one finding here.
        assert [r.status for r in results] == ["PASS", "PASS", "FAIL", "PASS", "PASS"]

    def test_a_message_never_carries_a_non_ascii_character_the_console_cannot_print(self, project):
        _write(project, "schema.yaml", DRAFT + _schema(("A", "s", PLACEHOLDER)))
        _write(project, "aliases.yaml", DRAFT + _aliases("{}", '  "x": ["y"]  # REVIEW\n'))

        for name in CURATION_CHECK_NAMES:
            assert getattr(verify, name)().render().isascii()

    def test_every_check_documents_what_it_catches(self):
        for name in CURATION_CHECK_NAMES:
            doc = " ".join((getattr(verify, name).__doc__ or "").split())
            assert "File reads only" in doc, name

    def test_the_cli_exit_code_follows_the_fail_status_and_advice_does_not_fail(self, project, capsys):
        _write(project, "schema.yaml", STAGING_SCHEMA)
        _write(project, "aliases.yaml", _aliases("{}", '  "annual": ["year"]\n'))
        checks = [getattr(verify, name) for name in CURATION_CHECK_NAMES]

        with patch.object(verify, "build_checks", lambda: checks):
            assert verify.main() == 0
        out = capsys.readouterr().out
        assert "[PASS] No staging or change-capture tables -- advice: " in out
        assert "5 passed, 0 failed, 0 skipped" in out

        _write(project, "schema.yaml", DRAFT + STAGING_SCHEMA)
        with patch.object(verify, "build_checks", lambda: checks):
            assert verify.main() == 1
        assert "[FAIL] project_config/ is curated, not a raw draft" in capsys.readouterr().out

    def test_a_check_that_cannot_read_anything_does_not_raise(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(tmp_path / "missing")):
            statuses = [getattr(verify, name)().status for name in CURATION_CHECK_NAMES]

        assert statuses == ["SKIP"] * 5


# ---------------------------------------------------------------------------
# 6. No table in an excluded schema
# ---------------------------------------------------------------------------

class TestNoTableInAnExcludedSchema:
    NAME = "No table in an excluded schema"

    @staticmethod
    def schema(*tables: tuple[str, str]) -> str:
        return _schema(*[(key, schema, CURATED_ROW) for key, schema in tables])

    def test_it_is_a_global_check_after_the_other_advice_and_reads_files_only(self):
        names = [c.__name__ for c in verify.build_checks()]

        assert names.count("check_no_table_in_excluded_schema") == 1
        assert names.index("check_no_table_in_excluded_schema") == names.index("check_no_sample_values_in_descriptions") + 1

    def test_it_is_not_a_deep_check(self):
        from api.admin_routes import _DEEP_CHECK_NAMES

        assert "check_no_table_in_excluded_schema" not in _DEEP_CHECK_NAMES

    def test_pass_when_the_configured_exclusion_matches_no_table(self, project):
        _write(project, "schema.yaml", self.schema(("A", "sales"), ("B", "ref")))

        with cfg.override_settings(exclude_schemas="*_Stg"):
            result = verify.check_no_table_in_excluded_schema()

        assert result.name == self.NAME
        assert (result.status, result.detail) == ("PASS", "none of the 2 table(s) is in an excluded schema")

    def test_skip_when_nothing_is_excluded_beyond_the_system_schemas_and_nothing_matches(self, project):
        _write(project, "schema.yaml", self.schema(("A", "sales")))

        result = verify.check_no_table_in_excluded_schema()

        assert result.status == "SKIP"
        assert "no schema is excluded beyond the system schemas" in result.detail

    def test_a_listed_table_in_an_excluded_schema_is_advice_with_its_key_and_both_fixes(self, project):
        _write(project, "schema.yaml", self.schema(("A", "sales"), ("Batch", "Load_Stg"), ("sales_Stg.Order", "")))

        with cfg.override_settings(exclude_schemas="*_Stg"):
            result = verify.check_no_table_in_excluded_schema()

        assert result.status == "PASS"                        # advice, like the other doctor advice
        assert result.detail.startswith("advice: 2 of 3 table(s) are in a schema the setup tools are told not to scan: ")
        assert "Batch" in result.detail and "sales_Stg.Order" in result.detail
        assert "remove them from schema.yaml, or narrow the exclusion" in result.detail
        assert "EXCLUDE_SCHEMAS" in result.detail and "exclude_schemas:" in result.detail

    def test_a_system_schema_counts_even_with_nothing_configured(self, project):
        _write(project, "schema.yaml", self.schema(("Audit", "cdc"), ("A", "sales")))

        result = verify.check_no_table_in_excluded_schema()

        assert result.detail.startswith("advice: 1 of 2 table(s)") and "Audit" in result.detail

    def test_matching_ignores_case(self, project):
        _write(project, "schema.yaml", self.schema(("A", "Load_STG")))

        with cfg.override_settings(exclude_schemas="*_stg"):
            assert verify.check_no_table_in_excluded_schema().detail.startswith("advice: 1 of 1")

    def test_a_sources_own_list_applies_to_the_tables_assigned_to_it_only(self, project):
        _write(project, "datasources.yaml", TWO_SOURCES.replace(
            "url_env: DB_URL_ARCHIVE", "url_env: DB_URL_ARCHIVE\n    exclude_schemas: [old]"))
        _write(
            project, "schema.yaml",
            "tables:\n"
            "  InMain:\n    db_schema: old\n    datasource: main\n    columns: {ID: k}\n"
            "  InArchive:\n    db_schema: old\n    datasource: archive\n    columns: {ID: k}\n"
            "  InBoth:\n    db_schema: old\n    datasource: [main, archive]\n    columns: {ID: k}\n",
        )
        from database.datasources import reset_datasources_cache

        reset_datasources_cache()
        try:
            result = verify.check_no_table_in_excluded_schema()
        finally:
            reset_datasources_cache()

        # Only the table assigned to `archive` alone is excluded everywhere it lives.
        assert result.detail.startswith("advice: 1 of 3 table(s)") and "InArchive" in result.detail
        assert "InMain" not in result.detail and "InBoth" not in result.detail

    def test_a_schema_that_does_not_load_is_skipped(self, project):
        _write(project, "schema.yaml", "tables: [not, a, mapping]\n")

        assert verify.check_no_table_in_excluded_schema().status == "SKIP"

    def test_an_invalid_datasources_yaml_is_skipped_not_a_crash(self, project):
        _write(project, "schema.yaml", self.schema(("A", "sales")))
        _write(project, "datasources.yaml", "datasources:\n  a:\n    url_env: X\n    exclude_schemas: 1\n")
        from database.datasources import reset_datasources_cache

        reset_datasources_cache()
        try:
            result = verify.check_no_table_in_excluded_schema()
        finally:
            reset_datasources_cache()

        assert result.status == "SKIP" and "datasources.yaml did not load" in result.detail

    def test_the_message_is_ascii_and_holds_keys_only(self, project):
        _write(project, "schema.yaml", self.schema(("A", "Load_Stg")))

        with cfg.override_settings(exclude_schemas="*_Stg"):
            assert verify.check_no_table_in_excluded_schema().render().isascii()

    def test_the_doctor_helper_reports_tables_excluded_in_every_source_they_live_in(self):
        view = doctor.SchemaView(parsed=True, tables=(
            doctor.SchemaTable("A", "old", "A", "", ("m",), ()),
            doctor.SchemaTable("B", "old", "B", "", ("m", "n"), ()),
            doctor.SchemaTable("C", "", "C", "", (), ()),
        ))

        found = doctor.tables_in_excluded_schemas(view, {"m": ["old"], "n": []}, "m")

        assert [t.key for t in found] == ["A"]

    def test_is_staging_matches_what_the_staging_count_matches(self):
        patterns = doctor.parse_staging_patterns("*_Stg, table:*_Inserts")

        assert doctor.is_staging("Sales_Stg", "Invoice", patterns)
        assert doctor.is_staging("sales", "INVOICE_INSERTS", patterns)
        assert not doctor.is_staging("sales", "Invoice", patterns)


# ---------------------------------------------------------------------------
# 7. Drafts of the sync format, and the wizard's header
# ---------------------------------------------------------------------------

WIZARD_HEADER = "# AUTO-GENERATED by setup_project.py \u2014 review before use\n"


def _sync_schema(*rows: tuple[str, str, str], notes: tuple[str, ...] = ("bigint column", "nvarchar(100) column")) -> str:
    """A ``schema.yaml`` in the format ``sync_schema.py --add-tables`` writes: ``(key, db_schema, description)``."""
    lines = ["tables:"]
    for key, schema, description in rows:
        lines.append(f"  {key}:")
        lines.append(f"    description: {description!r}  # TO BE FILLED (added by sync_schema.py)")
        if schema:
            lines.append(f"    db_schema: {schema!r}")
        lines.append("    columns:")
        for number, note in enumerate(notes):
            lines.append(f"      Col{number}: {note!r}  # TO BE FILLED (added by sync_schema.py)")
    return "\n".join(lines) + "\n"


def _named(count: int, *, note: str = "bigint column") -> str:
    return _sync_schema(*[(f"T{n}", "Sales_Fact", f"Sales_Fact.T{n}") for n in range(count)], notes=(note,))


class TestTheSyncFormatDraftIsCaught:
    """The 6.13 drafts: no header, `description: 'Sales_Fact.Trade'`, `bigint column`."""

    NAME = "project_config/ is curated, not a raw draft"

    def test_a_draft_whose_header_line_was_deleted_fails_with_the_counts_and_the_way_back(self, project):
        _write(project, "schema.yaml", _named(10))

        result = verify.check_project_config_is_curated()

        assert result.status == "FAIL"
        assert "schema.yaml: 10 of 10 table descriptions (100%) are only the table's own name" in result.detail
        assert "schema.yaml: 10 of 10 column descriptions (100%) are only `<type> column`" in result.detail
        assert "Restore the curated files" in result.detail
        assert "python scripts/sync_schema.py --add-tables 'Schema.*'" in result.detail
        assert "header:" not in result.detail.split(" -- ")[0]

    @pytest.mark.parametrize(
        "description", ["Sales_Fact.Trade", "Trade", "[Sales_Fact].[Trade]", "Sales_Fact.Trade (view)", "trade"],
    )
    def test_a_description_that_is_the_key_the_bare_name_or_schema_dot_name_is_the_name_only(self, project, description):
        _write(project, "schema.yaml", _sync_schema(("Trade", "Sales_Fact", description), notes=("Invoice date",)))

        result = verify.check_project_config_is_curated()

        assert result.status == "FAIL", result.detail
        assert "1 of 1 table descriptions (100%)" in result.detail
        assert "column descriptions" not in result.detail

    def test_a_qualified_key_that_is_its_own_description_counts(self, project):
        _write(project, "schema.yaml", _sync_schema(("Sales_Fact.Trade", "", "Sales_Fact.Trade"), notes=("Invoice date",)))

        assert verify.check_project_config_is_curated().status == "FAIL"

    @pytest.mark.parametrize(
        ("named", "total", "status"), [(0, 10, "PASS"), (1, 10, "PASS"), (5, 10, "PASS"), (6, 10, "FAIL"), (10, 10, "FAIL")],
    )
    def test_the_threshold_for_table_descriptions_is_more_than_half(self, project, named, total, status):
        rows = [
            (f"T{n}", "s", f"s.T{n}" if n < named else "Sales invoices, one row per invoice")
            for n in range(total)
        ]
        _write(project, "schema.yaml", _sync_schema(*rows, notes=("Invoice date",)))

        result = verify.check_project_config_is_curated()

        assert doctor.SYNC_DRAFT_SHARE == 0.5
        assert result.status == status, result.detail
        if status == "PASS" and named:
            assert f"{named} of {total} only repeat the table's name" in result.detail

    @pytest.mark.parametrize(
        ("typed", "total", "status"), [(0, 4, "PASS"), (2, 4, "PASS"), (3, 4, "FAIL"), (4, 4, "FAIL")],
    )
    def test_the_threshold_for_column_descriptions_is_more_than_half(self, project, typed, total, status):
        notes = tuple("int column" if n < typed else "Invoice date" for n in range(total))
        _write(project, "schema.yaml", _sync_schema(("T", "s", "Sales invoices"), notes=notes))

        result = verify.check_project_config_is_curated()

        assert result.status == status, result.detail
        if status == "PASS" and typed:
            assert f"{typed} of {total} column description(s) are only `<type> column`" in result.detail

    @pytest.mark.parametrize(
        "note", ["Invoice column", "the amount column", "column", "int", "bigint column of the amount"],
    )
    def test_a_hand_written_note_is_not_a_type_note(self, project, note):
        _write(project, "schema.yaml", _sync_schema(("T", "s", "Sales invoices"), notes=(note,)))

        assert verify.check_project_config_is_curated().status == "PASS"

    @pytest.mark.parametrize(
        "note", ["bigint column", "NVARCHAR(100) column", "decimal(18,2) column", "datetime2(7) column", "uniqueidentifier column"],
    )
    def test_every_sql_type_note_counts(self, project, note):
        _write(project, "schema.yaml", _sync_schema(("T", "s", "Sales invoices"), notes=(note,)))

        assert verify.check_project_config_is_curated().status == "FAIL"

    def test_a_curated_file_with_a_few_added_tables_is_still_curated(self, project):
        curated = [(f"C{n}", "s", f"Curated description number {n}") for n in range(8)]
        added = [(f"A{n}", "s", f"s.A{n}") for n in range(2)]
        _write(project, "schema.yaml", _sync_schema(*curated, *added, notes=("Invoice date",)))

        result = verify.check_project_config_is_curated()

        assert result.status == "PASS"
        assert "2 of 10 only repeat the table's name" in result.detail

    def test_an_empty_file_has_no_share_to_exceed(self, project):
        _write(project, "schema.yaml", "tables: {}\n")

        assert verify.check_project_config_is_curated().status == "PASS"

    def test_the_helpers_count_what_the_check_reports(self, project):
        _write(project, "schema.yaml", _named(3))
        view = doctor.read_schema(project)

        assert doctor.name_only_descriptions(view) == (3, 3)
        assert doctor.type_only_column_notes(view) == (3, 3)
        assert doctor.name_only_descriptions(doctor.SchemaView()) == (0, 0)


class TestTheWizardHeaderIsADraftHeader:
    NAME = "project_config/ is curated, not a raw draft"

    def test_a_file_the_wizard_wrote_is_a_draft_until_the_line_is_deleted(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW)))
        _write(project, "entities.yaml", WIZARD_HEADER + "entities: {}\n")
        _write(project, "aliases.yaml", WIZARD_HEADER + "ring_aliases: {}\nsynonyms: {}\n")

        result = verify.check_project_config_is_curated()

        assert result.status == "FAIL"
        assert "2 file(s) carry the AUTO-GENERATED by setup_project.py header: aliases.yaml, entities.yaml" in result.detail
        assert "delete its header line" in result.detail
        assert result.render().isascii()

    def test_both_headers_are_named_each_with_its_files(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW)))
        _write(project, "entities.yaml", WIZARD_HEADER + "entities: {}\n")
        _write(project, "relationships.yaml", DRAFT + "relationships: []\n")

        detail = verify.check_project_config_is_curated().detail

        assert "1 file(s) carry the AUTO-GENERATED DRAFT header: relationships.yaml" in detail
        assert "1 file(s) carry the AUTO-GENERATED by setup_project.py header: entities.yaml" in detail

    def test_the_doctor_says_which_header_each_file_has(self, project):
        _write(project, "a.yaml", WIZARD_HEADER)
        _write(project, "b.yml", DRAFT)
        _write(project, "c.yaml", "# a curated file\n")
        _write(project, "d.txt", WIZARD_HEADER)

        assert doctor.find_draft_header_kinds(project) == {
            "a.yaml": doctor.WIZARD_DRAFT_HEADER, "b.yml": doctor.DRAFT_HEADER,
        }
        assert doctor.find_draft_headers(project) == ["a.yaml", "b.yml"]

    def test_a_header_below_the_first_lines_or_in_a_value_is_not_a_header(self, project):
        _write(project, "schema.yaml", _schema(("A", "s", CURATED_ROW)))
        _write(project, "entities.yaml", "# note\n" * 12 + WIZARD_HEADER + "entities: {}\n")
        _write(project, "business_rules.yaml", "rules:\n  A:\n    rule_text: 'AUTO-GENERATED by setup_project.py'\n")

        assert verify.check_project_config_is_curated().status == "PASS"

    def test_the_wizard_writes_the_line_the_doctor_looks_for(self):
        import setup_project as sp

        assert sp._FILE_HEADER.startswith(f"# {doctor.WIZARD_DRAFT_HEADER} ")


# ---------------------------------------------------------------------------
# 8. Aliases hold no raw data values
# ---------------------------------------------------------------------------

#: Invented values of the shapes a row holds: none is a real date, number or person.
RAW_KEYS = {
    "date": ["1387/03/24", "01/01/2010", "2024-05-01 10:00:00"],
    "time": ["10:30:05"],
    "phone": ["09120000001", "+1 (555) 010-0100"],
    "ip": ["10.0.12.200", "192.168.0.7"],
    "masked": ["ab xxxxx cd", "****1234"],
    "digits": ["0012345001", "1234567890123"],
    "address": ["12 Example Street, Building 4, Floor 2"],
}


def _raw_synonyms(*keys: str) -> str:
    return _aliases("{}", "".join(f"  {key!r}: ['t']\n" for key in keys))


class TestAliasesHoldNoRawValues:
    NAME = "Aliases hold no raw data values"

    def test_a_draft_of_values_fails_with_the_count_per_shape_and_never_a_value(self, project):
        every = [key for keys in RAW_KEYS.values() for key in keys]
        _write(project, "aliases.yaml", _raw_synonyms("invoice", "payment", *every))

        result = verify.check_aliases_hold_no_raw_values()

        assert result.name == self.NAME
        assert result.status == "FAIL"
        assert f"{len(every)} of {len(every) + 2} synonym keys look like raw data values" in result.detail
        for shape, count in (("date", 3), ("phone", 2), ("ip", 2), ("masked", 2), ("digits", 2), ("address", 1), ("time", 1)):
            assert f"<{shape}>: {count}" in result.detail
        for value in every:
            assert value not in result.detail
        assert "--value-synonyms" in result.detail and "docs/deployment-runbook.md" in result.detail
        assert result.render().isascii()

    def test_a_curated_vocabulary_passes(self, project):
        _write(project, "aliases.yaml", _raw_synonyms("annual", "monthly", "1402", "q1", "ring a", "\u0641\u0631\u0648\u0634", "\u0646\u0631\u062e \u0627\u0631\u0632"))

        result = verify.check_aliases_hold_no_raw_values()

        assert result.status == "PASS"
        assert result.detail == "0 of 7 synonym key(s) look like a date, a number or an address"

    @pytest.mark.parametrize(("raw", "status"), [(0, "PASS"), (5, "PASS"), (6, "FAIL"), (40, "FAIL")])
    def test_a_few_are_allowed_and_more_than_five_fail(self, project, raw, status):
        _write(project, "aliases.yaml", _raw_synonyms("annual", *[f"2024-05-{n + 1:02d}" for n in range(raw)]))

        result = verify.check_aliases_hold_no_raw_values()

        assert doctor.RAW_VALUE_SYNONYM_LIMIT == 5
        assert result.status == status, result.detail

    @pytest.mark.parametrize("key", [
        "\u06f1\u06f3\u06f8\u06f7/\u06f0\u06f3/\u06f2\u06f4",                      # a date in Persian digits
        "\u06f0\u06f9\u06f1\u06f2\u06f0\u06f0\u06f0\u06f0\u06f0\u06f0\u06f1",     # a phone number in Persian digits
        "user@example.test", "https://example.test/path", "fe80::1",
        "5f4dcc3b5aa765d61d8327deb882cf99", "Sam ***", "x xxx y",
    ])
    def test_each_shape_is_counted_whatever_the_script(self, key):
        assert sum(doctor.raw_value_synonym_keys([key]).values()) == 1

    def test_the_count_works_on_an_aliases_file_that_is_not_yaml(self, project):
        lines = "".join(f'  "2024-05-{n + 1:02d} 10:00:00": ["t"]  # REVIEW\n' for n in range(8))
        _write(project, "aliases.yaml", DRAFT + "ring_aliases: {}\nsynonyms:\n" + lines + '  "bad"quote": ["t"]\n')

        assert verify.check_aliases_hold_no_raw_values().status == "FAIL"

    def test_no_aliases_file_skips(self, project):
        assert verify.check_aliases_hold_no_raw_values().status == "SKIP"

    def test_an_aliases_file_with_nothing_in_it_passes(self, project):
        _write(project, "aliases.yaml", "")

        assert verify.check_aliases_hold_no_raw_values().status == "PASS"

    def test_the_helper_names_shapes_and_nothing_else(self):
        found = doctor.raw_value_synonym_keys(["1387/03/24", "invoice", "09120000001"])

        assert found == {"date": 1, "phone": 1}
        assert doctor.raw_value_synonym_keys([]) == {}


# ---------------------------------------------------------------------------
# 9. Entities match schema keys
# ---------------------------------------------------------------------------

def _keyed_schema(*tables: tuple[str, str]) -> str:
    """``(key, db_schema)`` per table, with curated descriptions."""
    return _schema(*[(key, schema, CURATED_ROW) for key, schema in tables])


def _entities(*rows: tuple[str, str, str]) -> str:
    """``(key, table, schema_name)`` per entity."""
    lines = ["entities:"]
    for key, table, schema in rows:
        lines += [f"  {key}:", "    aliases: ['x']", f"    table: {table}"]
        if schema:
            lines.append(f"    schema_name: {schema}")
    return "\n".join(lines) + "\n"


class TestEntitiesMatchSchemaKeys:
    NAME = "Entities match schema keys"

    SCHEMA = _keyed_schema(("ActionType", "Sales_Dim"), ("Date", "Ref_Dim"), ("sales.Trade", ""), ("ref.Trade", ""))

    def test_the_6_14_wizard_s_pairs_fail_with_the_key_to_use_and_the_duplicate(self, project):
        _write(project, "schema.yaml", self.SCHEMA)
        _write(project, "entities.yaml", _entities(
            ("ActionType", "ActionType", ""), ("Sales_Dim.ActionType", "ActionType", "Sales_Dim"),
            ("Date", "Date", ""), ("Ref_Dim.Date", "Date", "Ref_Dim"),
            ("sales.Trade", "Trade", "sales"),
        ))
        _write(project, "aliases.yaml", _aliases(
            '\n  "Sales_Dim.ActionType": ["a"]\n  "Ref_Dim.Date": ["d"]\n  "Hall A": ["a hall"]', "",
        ))

        result = verify.check_entities_match_schema_keys()

        assert result.name == self.NAME
        assert result.status == "FAIL"
        assert (
            "2 entity key(s) are not a schema.yaml table key: Sales_Dim.ActionType (use ActionType), Ref_Dim.Date (use Date)"
            in result.detail
        )
        assert (
            "2 table(s) have two entities: Sales_Dim.ActionType (ActionType and Sales_Dim.ActionType), "
            "Ref_Dim.Date (Date and Ref_Dim.Date)" in result.detail
        )
        assert (
            "2 ring_aliases key(s) name a table but not by its schema.yaml key: "
            "Sales_Dim.ActionType (use ActionType), Ref_Dim.Date (use Date)" in result.detail
        )
        assert "Hall A" not in result.detail
        assert "Rename the key to the schema.yaml key" in result.detail
        assert result.render().isascii()

    def test_entities_keyed_as_schema_yaml_keys_them_pass(self, project):
        _write(project, "schema.yaml", self.SCHEMA)
        _write(project, "entities.yaml", _entities(
            ("ActionType", "ActionType", "Sales_Dim"), ("Date", "Date", "Ref_Dim"),
            ("sales.Trade", "Trade", "sales"), ("ref.Trade", "Trade", "ref"),
        ))
        _write(project, "aliases.yaml", _aliases('\n  "Hall A": ["a"]\n  "ActionType": ["a"]', ""))

        result = verify.check_entities_match_schema_keys()

        assert result.status == "PASS", result.detail
        assert result.detail == "all 4 entity key(s) name a schema.yaml table and no table has two entities"

    def test_a_bare_name_that_retrieval_resolves_to_one_key_is_a_table_key(self, project):
        # `Customer` for `sales.Customer`: written before names were qualified, resolved at run time.
        _write(project, "schema.yaml", _keyed_schema(("sales.Customer", ""), ("ref.Region", "")))
        _write(project, "entities.yaml", _entities(("Customer", "Customer", ""), ("ref.Region", "Region", "")))

        assert verify.check_entities_match_schema_keys().status == "PASS"

    def test_a_key_that_names_no_table_fails_and_says_so_without_a_suggestion(self, project):
        _write(project, "schema.yaml", self.SCHEMA)
        _write(project, "entities.yaml", _entities(("Entity1", "your_dim_table", ""), ("Date", "Date", "")))

        result = verify.check_entities_match_schema_keys()

        assert result.status == "FAIL"
        assert "1 entity key(s) are not a schema.yaml table key: Entity1" in result.detail
        assert "Entity1 (use" not in result.detail

    def test_two_entities_for_one_table_fail_even_when_both_keys_resolve(self, project):
        _write(project, "schema.yaml", self.SCHEMA)
        _write(project, "entities.yaml", _entities(("ActionType", "ActionType", ""), ("actiontype", "ActionType", "")))

        result = verify.check_entities_match_schema_keys()

        assert result.status == "FAIL"
        assert "1 table(s) have two entities: Sales_Dim.ActionType (ActionType and actiontype)" in result.detail
        assert "entity key(s) are not" not in result.detail

    def test_the_inspectors_schema_spelling_is_read_like_schema_name(self, project):
        _write(project, "schema.yaml", self.SCHEMA)
        text = "entities:\n  ActionType:\n    aliases: []\n    table: ActionType\n    schema: Sales_Dim\n"
        _write(project, "entities.yaml", text + "  Sales_Dim.ActionType:\n    aliases: [x]\n    table: ActionType\n    schema: Sales_Dim\n")

        assert "1 table(s) have two entities" in verify.check_entities_match_schema_keys().detail

    def test_a_ring_alias_that_is_a_hall_name_is_not_a_table_reference(self, project):
        _write(project, "schema.yaml", self.SCHEMA)
        _write(project, "entities.yaml", _entities(("Date", "Date", "")))
        _write(project, "aliases.yaml", _aliases('\n  "Hall Industrial": ["industrial"]\n  "Ring": ["ring"]', ""))

        assert verify.check_entities_match_schema_keys().status == "PASS"

    def test_a_ring_alias_that_names_a_table_by_the_wrong_key_fails_alone(self, project):
        _write(project, "schema.yaml", self.SCHEMA)
        _write(project, "entities.yaml", _entities(("Date", "Date", "")))
        _write(project, "aliases.yaml", _aliases('\n  "Ref_Dim.Date": ["d"]', ""))

        result = verify.check_entities_match_schema_keys()

        assert result.status == "FAIL"
        assert result.detail.startswith(
            "1 ring_aliases key(s) name a table but not by its schema.yaml key: Ref_Dim.Date (use Date)"
        )

    def test_it_skips_when_there_is_nothing_to_compare(self, project):
        assert verify.check_entities_match_schema_keys().status == "SKIP"
        _write(project, "schema.yaml", self.SCHEMA)
        assert verify.check_entities_match_schema_keys().status == "SKIP"            # no entities.yaml
        _write(project, "entities.yaml", "entities: [not, a, mapping]\n")
        assert verify.check_entities_match_schema_keys().status == "SKIP"
        _write(project, "entities.yaml", _entities(("Date", "Date", "")))
        _write(project, "schema.yaml", "tables: [unclosed\n")
        assert verify.check_entities_match_schema_keys().status == "SKIP"

    def test_a_long_list_is_capped(self, project):
        rows = [(f"S.T{n}", f"T{n}", "S") for n in range(15)]
        _write(project, "schema.yaml", _keyed_schema(*[(f"T{n}", "S") for n in range(15)]))
        _write(project, "entities.yaml", _entities(*rows))

        detail = verify.check_entities_match_schema_keys().detail

        assert "15 entity key(s) are not a schema.yaml table key" in detail and "and 5 more" in detail

    def test_the_doctor_function_works_on_rows(self):
        view = doctor.SchemaView(parsed=True, tables=(doctor.SchemaTable("T", "s", "T", "", (), ()),))
        rows = [doctor.EntityRow("T", "T", ""), doctor.EntityRow("s.T", "T", "s"), doctor.EntityRow("Other", "Other", "")]

        found = doctor.entity_key_findings(view, rows)

        assert found.unknown_entities == [("s.T", "T"), ("Other", "")]
        assert found.duplicates == [("s.T", ("T", "s.T"))]
        assert found.found == 3


# ---------------------------------------------------------------------------
# 10. The new checks in the report
# ---------------------------------------------------------------------------

NEW_CHECK_NAMES = ("check_aliases_hold_no_raw_values", "check_entities_match_schema_keys")


class TestTheNewChecksInTheReport:
    def test_they_follow_the_other_file_checks_and_each_is_registered_once(self):
        names = [c.__name__ for c in verify.build_checks()]

        for name in NEW_CHECK_NAMES:
            assert names.count(name) == 1
        after = names.index("check_no_table_in_excluded_schema")
        assert names[after + 1: after + 3] == list(NEW_CHECK_NAMES)

    def test_none_is_a_deep_check_and_each_documents_that_it_only_reads_files(self):
        from api.admin_routes import _DEEP_CHECK_NAMES

        assert not set(NEW_CHECK_NAMES) & set(_DEEP_CHECK_NAMES)
        for name in NEW_CHECK_NAMES:
            assert "File reads only" in " ".join((getattr(verify, name).__doc__ or "").split())

    def test_they_make_no_database_call_and_do_not_raise_on_a_missing_directory(self, tmp_path):
        with cfg.override_settings(project_config_dir=str(tmp_path / "missing")), \
                patch("database.connection.get_engine", side_effect=AssertionError("database touched")):
            assert [getattr(verify, name)().status for name in NEW_CHECK_NAMES] == ["SKIP", "SKIP"]

    def test_the_example_configuration_has_nothing_to_report_about_its_aliases(self):
        example = Path(__file__).resolve().parent.parent / "project_config.example"
        with cfg.override_settings(project_config_dir=str(example)):
            assert verify.check_aliases_hold_no_raw_values().status == "PASS"
