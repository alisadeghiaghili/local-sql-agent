# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``database.datasources_edit``: adding ``exclude_schemas`` to ``datasources.yaml`` as a text edit.

The file is documented in place, so an edit must add the one list and nothing
else: comments, quoting, blank lines and line endings stay. The result must
validate and equal the original except for the added patterns, or the file is
left alone.
"""

from __future__ import annotations

import os

import pytest

from database.datasources import validate_datasources_yaml_text
from database.datasources_edit import (
    DatasourcesEditError,
    add_exclude_schemas,
    exclude_schemas_snippet,
    record_exclude_schemas,
)

HEADER = "# the warehouses of this deployment\n\ndefault: sales\n\n"

BASE = HEADER + """\
datasources:
  # the main one
  sales:   # sales warehouse
    url_env: DB_URL_SALES
    description: "Sales, with a # inside"
    keywords: [stock, revenue]

  # Windows authentication:
  reports:
    host: db2
    database: ReportsDW
    trusted_connection: true
    # exclude_schemas: [Replay]
    options:
      TrustServerCertificate: true
"""


def listed(text: str, source: str) -> list[str]:
    return validate_datasources_yaml_text(text).datasources[source].exclude_schemas


class TestInsertion:
    def test_a_source_without_the_list_gets_one_line_and_every_comment_stays(self):
        new, added = add_exclude_schemas(BASE, {"sales": ["Scratch", "*_Tmp"]})
        assert added == {"sales": ("Scratch", "*_Tmp")}
        assert listed(new, "sales") == ["Scratch", "*_Tmp"]
        assert listed(new, "reports") == []
        old_lines, new_lines = BASE.splitlines(), new.splitlines()
        assert [line for line in new_lines if line not in old_lines] == ['    exclude_schemas: ["Scratch", "*_Tmp"]']
        assert [line for line in old_lines if line not in new_lines] == []
        # Inserted after the source's last setting, before the blank line that separates the next one.
        at = new_lines.index('    exclude_schemas: ["Scratch", "*_Tmp"]')
        assert new_lines[at - 1].startswith("    keywords:") and new_lines[at + 1] == ""

    def test_the_insertion_goes_after_a_nested_block_not_inside_it(self):
        new, _ = add_exclude_schemas(BASE, {"reports": ["Replay_Old"]})
        assert listed(new, "reports") == ["Replay_Old"]
        assert new.endswith('      TrustServerCertificate: true\n    exclude_schemas: ["Replay_Old"]\n')
        assert "    # exclude_schemas: [Replay]\n" in new

    def test_the_last_source_of_a_file_without_a_final_newline(self):
        new, _ = add_exclude_schemas(BASE.rstrip("\n"), {"reports": ["X"]})
        assert listed(new, "reports") == ["X"] and not new.endswith("\n")


class TestExistingLists:
    def test_a_block_list_is_extended_in_its_own_style(self):
        text = BASE.replace(
            "    keywords: [stock, revenue]\n",
            "    exclude_schemas:\n      - Replay   # change capture\n      - '*_Stg'\n    keywords: [stock]\n",
        )
        new, added = add_exclude_schemas(text, {"sales": ["Scratch"]})
        assert listed(new, "sales") == ["Replay", "*_Stg", "Scratch"]
        assert "      - '*_Stg'\n      - \"Scratch\"\n    keywords: [stock]\n" in new
        assert "- Replay   # change capture" in new

    def test_an_indentless_block_list_is_extended(self):
        text = BASE.replace("    keywords: [stock, revenue]\n", "    exclude_schemas:\n    - Replay\n")
        new, _ = add_exclude_schemas(text, {"sales": ["Scratch"]})
        assert listed(new, "sales") == ["Replay", "Scratch"]
        assert "    - Replay\n    - \"Scratch\"\n" in new

    def test_a_flow_list_gets_the_items_before_its_bracket_and_keeps_its_comment(self):
        text = BASE.replace("    keywords: [stock, revenue]\n", '    exclude_schemas: ["*_Stg", Replay]   # staging\n')
        new, _ = add_exclude_schemas(text, {"sales": ["Scratch"]})
        assert listed(new, "sales") == ["*_Stg", "Replay", "Scratch"]
        assert '    exclude_schemas: ["*_Stg", Replay, "Scratch"]   # staging\n' in new

    def test_an_empty_flow_list(self):
        text = BASE.replace("    keywords: [stock, revenue]\n", "    exclude_schemas: []\n")
        new, _ = add_exclude_schemas(text, {"sales": ["A", "B"]})
        assert '    exclude_schemas: ["A", "B"]\n' in new and listed(new, "sales") == ["A", "B"]

    def test_a_pattern_with_brackets_inside_a_quoted_item(self):
        text = BASE.replace("    keywords: [stock, revenue]\n", '    exclude_schemas: ["Load_[0-9]"]\n')
        new, _ = add_exclude_schemas(text, {"sales": ["Scratch"]})
        assert listed(new, "sales") == ["Load_[0-9]", "Scratch"]

    def test_a_pattern_already_listed_is_not_added_again_whatever_its_case(self):
        text = BASE.replace("    keywords: [stock, revenue]\n", "    exclude_schemas: [Scratch]\n")
        new, added = add_exclude_schemas(text, {"sales": ["scratch", "Other", "other"]})
        assert added == {"sales": ("Other",)} and listed(new, "sales") == ["Scratch", "Other"]

    def test_nothing_new_returns_the_text_unchanged(self):
        text = BASE.replace("    keywords: [stock, revenue]\n", "    exclude_schemas: [Scratch]\n")
        assert add_exclude_schemas(text, {"sales": ["SCRATCH"]}) == (text, {})


class TestLineEndings:
    def test_crlf_files_stay_crlf(self):
        text = BASE.replace("\n", "\r\n")
        new, _ = add_exclude_schemas(text, {"sales": ["Scratch"]})
        assert new.count("\n") == new.count("\r\n")
        assert listed(new, "sales") == ["Scratch"]

    def test_a_flow_list_in_a_crlf_file(self):
        text = BASE.replace("    keywords: [stock, revenue]\n", "    exclude_schemas: [A]\n").replace("\n", "\r\n")
        new, _ = add_exclude_schemas(text, {"sales": ["B"]})
        assert new.count("\n") == new.count("\r\n") and listed(new, "sales") == ["A", "B"]


class TestRefusals:
    def test_an_unknown_source(self):
        with pytest.raises(DatasourcesEditError, match="unknown data source 'nope'"):
            add_exclude_schemas(BASE, {"nope": ["X"]})

    def test_a_file_that_is_not_valid_as_it_stands(self):
        with pytest.raises(DatasourcesEditError, match="not valid as it stands"):
            add_exclude_schemas("datasources:\n  a:\n    nothing: 1\n", {"a": ["X"]})

    def test_a_source_written_inline(self):
        text = "datasources:\n  sales: {url_env: DB_URL_SALES}\n"
        with pytest.raises(DatasourcesEditError, match="not written as a `sales:` block"):
            add_exclude_schemas(text, {"sales": ["X"]})

    def test_a_flow_list_over_several_lines(self):
        text = BASE.replace("    keywords: [stock, revenue]\n", "    exclude_schemas: [A,\n      B]\n")
        with pytest.raises(DatasourcesEditError, match="does not end on its own line"):
            add_exclude_schemas(text, {"sales": ["X"]})


class TestRecord:
    def _file(self, tmp_path, text=BASE):
        path = tmp_path / "datasources.yaml"
        path.write_bytes(text.encode("utf-8"))
        return path

    def test_a_backup_is_made_and_the_file_replaced(self, tmp_path):
        path = self._file(tmp_path)
        done = record_exclude_schemas(path, {"sales": ["Scratch"]}, stamp="20260101-120000")
        assert done.backup == tmp_path / "datasources.yaml.bak-20260101-120000"
        assert done.backup.read_text(encoding="utf-8") == BASE
        assert done.added == {"sales": ("Scratch",)}
        assert listed(path.read_text(encoding="utf-8"), "sales") == ["Scratch"]
        assert not [p for p in tmp_path.iterdir() if ".tmp-" in p.name]

    def test_crlf_bytes_survive_the_write(self, tmp_path):
        path = self._file(tmp_path, BASE.replace("\n", "\r\n"))
        record_exclude_schemas(path, {"sales": ["Scratch"]}, stamp="20260101-120000")
        raw = path.read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n")

    def test_nothing_to_add_writes_nothing_and_makes_no_backup(self, tmp_path):
        path = self._file(tmp_path, BASE.replace("    keywords: [stock, revenue]\n", "    exclude_schemas: [X]\n"))
        done = record_exclude_schemas(path, {"sales": ["x"]}, stamp="20260101-120000")
        assert done.backup is None and done.added == {}
        assert [p.name for p in tmp_path.iterdir()] == ["datasources.yaml"]

    def test_a_refused_layout_leaves_the_file_and_the_directory_alone(self, tmp_path):
        text = "datasources:\n  sales: {url_env: DB_URL_SALES}\n"
        path = self._file(tmp_path, text)
        with pytest.raises(DatasourcesEditError):
            record_exclude_schemas(path, {"sales": ["X"]}, stamp="20260101-120000")
        assert path.read_text(encoding="utf-8") == text
        assert [p.name for p in tmp_path.iterdir()] == ["datasources.yaml"]

    def test_a_failed_replace_leaves_the_original_and_no_temporary_file(self, tmp_path, monkeypatch):
        path = self._file(tmp_path)

        def refuse(*_args):
            raise PermissionError("read-only")

        monkeypatch.setattr(os, "replace", refuse)
        with pytest.raises(DatasourcesEditError, match="cannot write"):
            record_exclude_schemas(path, {"sales": ["X"]}, stamp="20260101-120000")
        assert path.read_text(encoding="utf-8") == BASE
        assert not [p for p in tmp_path.iterdir() if ".tmp-" in p.name]

    def test_a_missing_file(self, tmp_path):
        with pytest.raises(DatasourcesEditError, match="cannot read"):
            record_exclude_schemas(tmp_path / "datasources.yaml", {"a": ["X"]}, stamp="s")


def test_the_snippet_names_each_source():
    assert exclude_schemas_snippet({"sales": ["A"], "on": ["B*"]}) == (
        'datasources:\n  sales:\n    exclude_schemas: ["A"]\n  "on":\n    exclude_schemas: ["B*"]'
    )
