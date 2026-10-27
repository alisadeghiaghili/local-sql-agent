# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Preflight advice for the filter value check: the dimension tables it cannot help with.

With ``FILTER_VALUE_CHECK`` on, ``scripts/verify_deployment.py`` lists one more
check (``check_filter_value_check_has_vocabularies``). It reads files only,
names tables and columns and never a value, and only advises: its PASS detail
starts ``advice:``. With the setting off the check list is what it always was
(``tests/test_filter_value_check_pins.py`` pins it).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config as cfg
from core import config_doctor as doctor
from scripts import verify_deployment as verify

NAME = "Filter value check has vocabularies"


def _write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text, encoding="utf-8", newline="\n")


def _table(key: str, schema: str, columns: tuple[str, ...], prefetchable: tuple[str, ...] = ()) -> str:
    lines = [f"  {key}:", f"    db_schema: {schema}", "    description: 'A curated description'", "    columns:"]
    lines += [f"      {column}: 'a column'" for column in columns]
    if prefetchable:
        lines.append("    prefetchable_columns: [" + ", ".join(prefetchable) + "]")
    return "\n".join(lines)


def _schema(*tables: str) -> str:
    return "tables:\n" + "\n".join(tables) + "\n"


def _entities(*keys: str) -> str:
    lines = ["entities:"]
    for key in keys:
        lines += [f"  {key}:", "    aliases: ['x']", f"    table: {key.rsplit('.', 1)[-1]}"]
    return "\n".join(lines) + "\n"


@pytest.fixture()
def project(tmp_path):
    with cfg.override_settings(project_config_dir=str(tmp_path)):
        yield tmp_path


class TestTheCheck:
    def test_a_dimension_table_with_label_columns_and_no_vocabulary_is_listed(self, project):
        _write(project, "schema.yaml", _schema(
            _table("Hall", "ref", ("ID", "Name", "Kind")),
            _table("Ring", "ref", ("ID", "Name"), ("Name",)),
        ))
        _write(project, "entities.yaml", _entities("Hall", "Ring"))

        result = verify.check_filter_value_check_has_vocabularies()

        assert result.name == NAME
        assert result.status == "PASS"
        assert result.detail.startswith("advice: 1 dimension table(s) have label-like columns and no prefetchable_columns")
        assert "Hall (Name, Kind)" in result.detail
        assert "Ring" not in result.detail
        assert result.render().isascii()

    def test_a_table_of_people_is_not_advised(self, project):
        _write(project, "schema.yaml", _schema(_table("Customer", "sales", ("ID", "Name", "FamilyName"))))
        _write(project, "entities.yaml", _entities("Customer"))

        result = verify.check_filter_value_check_has_vocabularies()

        assert result.status == "PASS"
        assert not result.detail.startswith("advice:")

    def test_only_dimension_tables_count(self, project):
        _write(project, "schema.yaml", _schema(
            _table("Trade", "sales", ("ID", "Status", "Amount")),
            _table("Hall", "ref", ("ID", "Name"), ("Name",)),
        ))
        _write(project, "entities.yaml", _entities("Hall"))

        result = verify.check_filter_value_check_has_vocabularies()

        assert result.detail == (
            "1 dimension table(s) list prefetchable_columns; no other has a label-like column the check could use"
        )

    def test_keys_that_share_a_bare_name_are_told_apart(self, project):
        _write(project, "schema.yaml", _schema(
            _table("sales.Region", "", ("ID", "Name")),
            _table("ref.Region", "", ("ID", "Name"), ("Name",)),
        ))
        _write(project, "entities.yaml", _entities("sales.Region", "ref.Region"))

        result = verify.check_filter_value_check_has_vocabularies()

        assert "1 dimension table(s)" in result.detail and "sales.Region (Name)" in result.detail
        assert "ref.Region" not in result.detail

    def test_a_long_list_is_cut_like_the_other_advice(self, project):
        keys = [f"Dim{n:02d}" for n in range(30)]
        _write(project, "schema.yaml", _schema(*[_table(key, "ref", ("ID", "Name")) for key in keys]))
        _write(project, "entities.yaml", _entities(*keys))

        result = verify.check_filter_value_check_has_vocabularies()

        assert result.detail.startswith("advice: 30 dimension table(s)")
        assert ", and " in result.detail and " more" in result.detail

    def test_it_never_fails(self, project):
        _write(project, "schema.yaml", _schema(_table("Hall", "ref", ("ID", "Name"))))
        _write(project, "entities.yaml", _entities("Hall"))

        assert verify.check_filter_value_check_has_vocabularies().status == "PASS"

    def test_unreadable_files_skip(self, project):
        assert verify.check_filter_value_check_has_vocabularies().status == "SKIP"
        _write(project, "schema.yaml", _schema(_table("Hall", "ref", ("ID", "Name"))))
        assert verify.check_filter_value_check_has_vocabularies().status == "SKIP"


class TestListedOnlyWithTheSettingOn:
    def test_off_it_is_not_in_the_list(self):
        with cfg.override_settings(filter_value_check=False):
            assert verify.check_filter_value_check_has_vocabularies not in verify.build_checks()

    def test_on_it_is_the_last_one(self):
        with cfg.override_settings(filter_value_check=True):
            checks = verify.build_checks()

        assert checks[-1] is verify.check_filter_value_check_has_vocabularies
        assert checks[:-1] == [*verify.build_checks()]

    def test_the_example_config_has_nothing_to_advise_about_its_dimension_tables(self):
        with cfg.override_settings(filter_value_check=True):
            result = verify.check_filter_value_check_has_vocabularies()

        assert result.status in ("PASS", "SKIP")


class TestCoverage:
    def test_the_view_reads_prefetchable_columns(self):
        view = doctor.read_schema_text(_schema(_table("Ring", "ref", ("ID", "Name"), ("Name",))))

        assert view.tables[0].prefetchable == ("Name",)
        assert doctor.read_schema_text(_schema(_table("Ring", "ref", ("ID", "Name")))).tables[0].prefetchable == ()
