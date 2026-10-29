# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``scripts/draft_policies.py`` and ``core.policy_drafts``: real option values for the two policies.

The reader of the warehouse is a fake that records what it was asked, so no test
needs a database. What is pinned: only ``options`` (and the dimension when it is
moved) change, every other byte of both files stays, the memory options never hold
the session policy's ``default_label``, a column with too many values is refused,
the wizard's value rules apply, the drafts load with the application's validators,
nothing is written into the live directory without ``--overwrite-live`` (and a
``.bak`` is made with it), the table's own data source is read, and no warehouse
value is printed without ``--show-values``.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

import config as cfg
from core import policy_drafts
from core.policy_values import read_policies
from knowledge.config_loader import MemoryPolicyConfig, SessionPolicyConfig, validate_yaml_text
from scripts import draft_policies as command

_EXAMPLE = Path(__file__).resolve().parent.parent / "project_config.example"

MEMORY = """\
# kept comment
keys:
  scope:
    filter_key: "Ring"   # the dimension
    field_name: "ring"
    column: "Name"
    # why the default label is not here
    options:  # closed set
      - "Ring A"
      - "Ring B"
    max_length: 40
"""

SESSION = """\
# kept comment
default_scope:
  filter_key: "Ring"
  field_name: "ring"
  default_label: "All rings"
  options:
    - "All rings"
    - "Ring A"
  clarification_prompt: "Which ring do you mean?"
"""

VALUES = ["North", "South", "East"]


class Reader:
    """A fake warehouse: returns *values* and records every call."""

    def __init__(self, values=VALUES):
        self.values = values
        self.calls: list[tuple] = []

    def __call__(self, source, qualifier, key, column, limit):
        self.calls.append((source, qualifier, key, column, limit))
        return list(self.values)


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    target = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE, target)
    (target / "memory_policy.yaml").write_text(MEMORY, encoding="utf-8")
    (target / "session_policy.yaml").write_text(SESSION, encoding="utf-8")
    return target


def _run(config_dir: Path, tmp_path: Path, *extra: str, reader=None, output: Path | None = None):
    out = output if output is not None else tmp_path / "draft"
    reader = reader if reader is not None else Reader()
    code = command.main(
        ["--config-dir", str(config_dir), "--output", str(out), *extra], read_values=reader,
    )
    return code, out, reader


class TestTheDrafts:
    def test_options_are_replaced_and_everything_else_is_kept_byte_for_byte(self, config_dir, tmp_path):
        code, out, _ = _run(config_dir, tmp_path)

        assert code == 0
        assert (out / "memory_policy.yaml").read_text(encoding="utf-8") == MEMORY.replace(
            '      - "Ring A"\n      - "Ring B"\n',
            '      - "East"\n      - "North"\n      - "South"\n',
        )
        assert (out / "session_policy.yaml").read_text(encoding="utf-8") == SESSION.replace(
            '    - "All rings"\n    - "Ring A"\n',
            '    - "All rings"\n    - "East"\n    - "North"\n    - "South"\n',
        )

    def test_both_drafts_load_with_the_applications_validators(self, config_dir, tmp_path):
        _, out, _ = _run(config_dir, tmp_path)

        memory = validate_yaml_text("memory_policy.yaml", (out / "memory_policy.yaml").read_text(encoding="utf-8"), MemoryPolicyConfig)
        session = validate_yaml_text("session_policy.yaml", (out / "session_policy.yaml").read_text(encoding="utf-8"), SessionPolicyConfig)
        assert memory.keys["scope"].options == ["East", "North", "South"]
        assert session.default_scope.options == ["All rings", "East", "North", "South"]
        assert memory.keys["scope"].max_length == 40

    def test_the_default_label_is_never_a_memory_option(self, config_dir, tmp_path):
        _, out, _ = _run(config_dir, tmp_path, reader=Reader(["All rings", " all  rings ", *VALUES]))

        memory = validate_yaml_text("memory_policy.yaml", (out / "memory_policy.yaml").read_text(encoding="utf-8"), MemoryPolicyConfig)
        assert "All rings" not in memory.keys["scope"].options
        session = validate_yaml_text("session_policy.yaml", (out / "session_policy.yaml").read_text(encoding="utf-8"), SessionPolicyConfig)
        assert session.default_scope.options.count("All rings") == 1

    def test_the_wizards_value_rules_apply_and_duplicates_fold(self, config_dir, tmp_path, capsys):
        raw = ["North", "north ", "2024-05-01", "09120000001", "x", "Q" * 41, "A1-77/9", "Login failed for user", "South"]
        code, out, _ = _run(config_dir, tmp_path, reader=Reader(raw))

        assert code == 0
        memory = validate_yaml_text("memory_policy.yaml", (out / "memory_policy.yaml").read_text(encoding="utf-8"), MemoryPolicyConfig)
        assert memory.keys["scope"].options == ["North", "South"]
        printed = capsys.readouterr().out
        assert "read: 9 distinct value(s); kept: 2 option(s)" in printed
        assert "1 date" in printed and "1 phone" in printed and "1 duplicate" in printed

    def test_a_value_longer_than_the_memory_key_allows_is_left_out(self, config_dir, tmp_path):
        (config_dir / "memory_policy.yaml").write_text(MEMORY.replace("max_length: 40", "max_length: 5"), encoding="utf-8")

        _, out, _ = _run(config_dir, tmp_path, reader=Reader(["North", "Center", "East"]))

        text = (out / "memory_policy.yaml").read_text(encoding="utf-8")
        assert '"East"' in text and '"North"' in text and "Center" not in text

    def test_a_key_with_no_options_is_left_open_unless_it_is_named(self, config_dir, tmp_path):
        open_memory = MEMORY.replace('    options:  # closed set\n      - "Ring A"\n      - "Ring B"\n', "")
        (config_dir / "memory_policy.yaml").write_text(open_memory, encoding="utf-8")

        code, _, _ = _run(config_dir, tmp_path)
        assert code == 2  # nothing qualifies, and the message says to name the key

        code, out, _ = _run(config_dir, tmp_path, "--memory-key", "scope")
        assert code == 0
        assert '- "North"' in (out / "memory_policy.yaml").read_text(encoding="utf-8")

    def test_the_dimension_can_be_moved_and_only_then_filter_key_and_column_change(self, config_dir, tmp_path):
        schema = (config_dir / "schema.yaml").read_text(encoding="utf-8")
        (config_dir / "schema.yaml").write_text(
            schema.replace("\nrelationships:", "\n  sales.Zone:\n    description: Zones\n    columns:\n"
                           "      ID: Primary key\n      Title: Zone title\n\nrelationships:", 1),
            encoding="utf-8",
        )

        code, out, reader = _run(config_dir, tmp_path, "--table", "Zone", "--column", "title")

        assert code == 0
        assert reader.calls == [("default", "sales", "sales.Zone", "Title", 201)]
        memory = (out / "memory_policy.yaml").read_text(encoding="utf-8")
        assert 'filter_key: "sales.Zone"   # the dimension' in memory
        assert 'column: "Title"' in memory and 'field_name: "ring"' in memory
        assert 'filter_key: "sales.Zone"' in (out / "session_policy.yaml").read_text(encoding="utf-8")

    def test_a_crlf_file_stays_crlf(self, config_dir, tmp_path):
        (config_dir / "memory_policy.yaml").write_bytes(MEMORY.replace("\n", "\r\n").encode("utf-8"))

        _, out, _ = _run(config_dir, tmp_path)

        raw = (out / "memory_policy.yaml").read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")

    def test_text_that_cannot_be_edited_safely_is_refused_not_guessed(self, config_dir):
        template = read_policies(_EXAMPLE)
        # The example is checked out with CRLF line endings on Windows.
        broken, replaced = re.subn(r"options:(\r?\n)", r"options: [\1", template.memory_text, count=1)
        assert replaced == 1
        flow = type(template)(broken, template.session_text, template.memory, template.session)

        with pytest.raises(ValueError):
            policy_drafts.draft_policies(flow, ["North"])

    def test_nothing_usable_is_an_error_and_writes_nothing(self, config_dir, tmp_path):
        code, out, _ = _run(config_dir, tmp_path, reader=Reader(["2024-01-01", "x"]))

        assert code == 2
        assert not out.exists()


class TestTheReading:
    def test_too_many_values_are_refused_with_an_explanation(self, config_dir, tmp_path, capsys):
        code, out, reader = _run(config_dir, tmp_path, "--max-values", "3", reader=Reader(["a1", "b1", "c1", "d1"]))

        assert code == 2
        assert reader.calls[0][-1] == 4  # one more than the limit is asked for
        assert "more than 3 distinct values" in capsys.readouterr().err
        assert not out.exists()

    def test_exactly_the_limit_is_accepted(self, config_dir, tmp_path):
        code, _, _ = _run(config_dir, tmp_path, "--max-values", "3")

        assert code == 0

    def test_the_limit_cannot_exceed_the_row_cap(self, config_dir, tmp_path, capsys):
        with cfg.override_settings(max_rows_returned=10):
            code, _, reader = _run(config_dir, tmp_path, "--max-values", "10")

        assert code == 2 and reader.calls == []
        assert "MAX_ROWS_RETURNED" in capsys.readouterr().err

    def test_a_table_of_a_named_data_source_is_read_from_that_source(self, config_dir, tmp_path):
        schema = (config_dir / "schema.yaml").read_text(encoding="utf-8")
        (config_dir / "schema.yaml").write_text(
            schema.replace('    db_schema: "ref"\n    columns:\n      ID: "Primary key"\n      Name: "Channel display name"',
                           '    db_schema: "ref"\n    datasource: archive\n    columns:\n      ID: "Primary key"\n      Name: "Channel display name"', 1),
            encoding="utf-8",
        )

        _, _, reader = _run(config_dir, tmp_path)

        assert reader.calls == [("archive", "ref", "Ring", "Name", 201)]

    def test_a_table_without_a_data_source_is_read_from_the_default_one(self, config_dir, tmp_path):
        _, _, reader = _run(config_dir, tmp_path)

        assert reader.calls == [("default", "ref", "Ring", "Name", 201)]

    def test_a_reader_that_fails_is_one_error_without_a_traceback(self, config_dir, tmp_path, capsys):
        def broken(*_):
            raise RuntimeError("Database error: login failed")

        code, out, _ = _run(config_dir, tmp_path, reader=broken)

        assert code == 2 and not out.exists()
        assert "cannot read the values of Ring.Name" in capsys.readouterr().err


class TestTheDimension:
    def test_a_filter_key_that_is_no_table_needs_table_and_column(self, config_dir, tmp_path, capsys):
        (config_dir / "session_policy.yaml").write_text(SESSION.replace('"Ring"', '"Nowhere"'), encoding="utf-8")
        (config_dir / "memory_policy.yaml").write_text(MEMORY.replace('"Ring"', '"Nowhere"'), encoding="utf-8")

        code, _, reader = _run(config_dir, tmp_path)

        assert code == 2 and reader.calls == []
        assert "names no table of schema.yaml" in capsys.readouterr().err

    def test_a_column_that_is_not_in_the_table_is_refused_and_the_columns_are_listed(self, config_dir, tmp_path, capsys):
        code, _, reader = _run(config_dir, tmp_path, "--column", "Title")

        assert code == 2 and reader.calls == []
        assert "not a column of Ring" in capsys.readouterr().err

    def test_a_personal_column_is_never_read(self, config_dir, tmp_path, capsys):
        schema = (config_dir / "schema.yaml").read_text(encoding="utf-8")
        (config_dir / "schema.yaml").write_text(
            schema.replace("\nrelationships:", "\n  Person:\n    description: People\n    columns:\n"
                           "      ID: Primary key\n      Name: Full name\n\nrelationships:", 1),
            encoding="utf-8",
        )

        code, _, reader = _run(config_dir, tmp_path, "--table", "Person", "--column", "Name")

        assert code == 2 and reader.calls == []
        assert "personal or contact data" in capsys.readouterr().err

    def test_a_name_that_fits_two_tables_is_refused(self, config_dir, tmp_path, capsys):
        schema = (config_dir / "schema.yaml").read_text(encoding="utf-8")
        (config_dir / "schema.yaml").write_text(
            schema.replace("\nrelationships:", "\n  a.Zone:\n    description: Zones\n    columns:\n      Name: n\n"
                           "  b.Zone:\n    description: Zones\n    columns:\n      Name: n\n\nrelationships:", 1),
            encoding="utf-8",
        )

        code, _, reader = _run(config_dir, tmp_path, "--table", "Zone", "--column", "Name")

        assert code == 2 and reader.calls == []
        assert "fits 2 tables" in capsys.readouterr().err


class TestWhereItWrites:
    def test_the_live_directory_is_refused_without_the_flag_and_left_untouched(self, config_dir, tmp_path, capsys):
        before = {p.name: p.read_bytes() for p in config_dir.iterdir() if p.is_file()}
        with cfg.override_settings(project_config_dir=str(config_dir)):
            code, _, _ = _run(config_dir, tmp_path, output=config_dir)

        assert code == 2
        assert "--overwrite-live" in capsys.readouterr().err
        assert {p.name: p.read_bytes() for p in config_dir.iterdir() if p.is_file()} == before

    def test_the_template_directory_is_refused_too(self, config_dir, tmp_path):
        with cfg.override_settings(project_config_dir=str(config_dir)):
            code, _, _ = _run(config_dir, tmp_path, output=_EXAMPLE)

        assert code == 2
        assert "Ring A" in (_EXAMPLE / "memory_policy.yaml").read_text(encoding="utf-8")

    def test_with_the_flag_each_replaced_file_is_backed_up_first(self, config_dir, tmp_path):
        with cfg.override_settings(project_config_dir=str(config_dir)):
            code, _, _ = _run(config_dir, tmp_path, "--overwrite-live", output=config_dir)

        assert code == 0
        backups = sorted(p.name.split(".bak-")[0] for p in config_dir.glob("*.bak-*"))
        assert backups == ["memory_policy.yaml", "session_policy.yaml"]
        saved = next(config_dir.glob("memory_policy.yaml.bak-*"))
        assert saved.read_text(encoding="utf-8") == MEMORY
        assert '"North"' in (config_dir / "memory_policy.yaml").read_text(encoding="utf-8")

    def test_an_existing_draft_is_kept_unless_forced(self, config_dir, tmp_path, capsys):
        out = tmp_path / "draft"
        out.mkdir()
        (out / "memory_policy.yaml").write_text("keep me", encoding="utf-8")

        code, _, _ = _run(config_dir, tmp_path, output=out)
        assert code == 2 and (out / "memory_policy.yaml").read_text(encoding="utf-8") == "keep me"
        assert not (out / "session_policy.yaml").exists()

        code, _, _ = _run(config_dir, tmp_path, "--force", output=out)
        assert code == 0 and '"North"' in (out / "memory_policy.yaml").read_text(encoding="utf-8")

    def test_a_dry_run_writes_nothing(self, config_dir, tmp_path):
        code, out, reader = _run(config_dir, tmp_path, "--dry-run")

        assert code == 0 and len(reader.calls) == 1
        assert not out.exists()


class TestWhatIsPrinted:
    def test_no_warehouse_value_is_printed_by_default(self, config_dir, tmp_path, capsys):
        _run(config_dir, tmp_path, "--dry-run")
        _run(config_dir, tmp_path)

        shown = capsys.readouterr()
        for value in VALUES:
            assert value not in shown.out + shown.err

    def test_show_values_prints_the_first_few(self, config_dir, tmp_path, capsys):
        _run(config_dir, tmp_path, "--dry-run", "--show-values")

        assert "values: East, North, South" in capsys.readouterr().out

    def test_the_default_run_prints_counts(self, config_dir, tmp_path, capsys):
        _run(config_dir, tmp_path, "--dry-run")

        printed = capsys.readouterr().out
        assert "read: 3 distinct value(s); kept: 3 option(s)" in printed
        assert "dry run: nothing was written" in printed


class TestTheQuery:
    def test_it_is_one_bounded_statement_with_the_limit_as_a_parameter(self):
        sql = command.distinct_values_query("ref.Region", "ref", "Name")

        assert sql == "SELECT DISTINCT TOP (?) [Name] FROM [ref].[Region] WHERE [Name] IS NOT NULL ORDER BY [Name]"

    def test_another_dialect_gets_its_own_limit_clause(self):
        sql = command.distinct_values_query("Region", "", "Name", "sqlite")

        assert "TOP" not in sql.upper() and "LIMIT ?" in sql.upper()

    def test_a_reader_goes_through_the_applications_executor(self, monkeypatch):
        import pandas as pd

        import database.executor as executor

        seen = {}

        def fake(sql, params, *, datasource=None):
            seen.update(sql=sql, params=params, datasource=datasource)
            return pd.DataFrame({"Name": ["North", "South"]})

        monkeypatch.setattr(executor, "execute_sql_params", fake)

        values = command.default_reader()("archive", "ref", "Ring", "Name", 201)

        assert values == ["North", "South"]
        assert (seen["params"], seen["datasource"]) == ((201,), "archive")
        assert seen["sql"].startswith("SELECT DISTINCT TOP (?)")


class TestTheCircle:
    def test_a_first_install_fails_the_preflight_and_passes_it_after_the_drafts_are_copied(self, tmp_path):
        import scripts.verify_deployment as verify

        live = tmp_path / "project_config"
        shutil.copytree(_EXAMPLE, live)
        with cfg.override_settings(project_config_dir=str(live)):
            assert verify.check_policies_use_real_values().status == "FAIL"
            code, out, _ = _run(live, tmp_path)
            assert code == 0
            for name in ("memory_policy.yaml", "session_policy.yaml"):
                shutil.copy(out / name, live / name)
            result = verify.check_policies_use_real_values()

        assert result.status == "PASS", result.detail
