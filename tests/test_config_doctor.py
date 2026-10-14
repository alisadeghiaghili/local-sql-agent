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
