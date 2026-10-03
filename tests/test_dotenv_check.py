# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``core.dotenv_check`` and the start-up refusal built on it.

python-dotenv skips a ``.env`` line it cannot parse with one line on stderr.
These tests pin what is reported instead -- line numbers and variable names,
never a value -- and that ``Settings.validate()`` (so the server and
``verify_deployment``) refuses to continue. Nothing here reads the
repository's own ``.env``: files are written to ``tmp_path``, and
``Settings.validate()`` is tested by patching the state ``config`` records.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

import config
from config import override_settings
from core.dotenv_check import DotenvProblem, format_problems, scan_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Appears only as a value in the fixtures below. Any message containing it
#: has leaked a ``.env`` value.
SECRET = "hunter2-distinctive-secret"


def _scan(tmp_path: Path, text: str) -> list[DotenvProblem]:
    path = tmp_path / ".env"
    # newline="" keeps CRLF exactly as written.
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return scan_dotenv(path)


def _messages(problems: list[DotenvProblem]) -> list[str]:
    return [problem.message for problem in problems]


# ---------------------------------------------------------------------------
# What is reported
# ---------------------------------------------------------------------------

class TestUnparsableLines:
    def test_apostrophe_inside_a_quoted_value_names_the_variable_and_line(self, tmp_path):
        text = (
            "DB_HOST=localhost\n"
            "API_KEYS_JSON='[\n"
            '  {"id": "a", "name": "Ali\'s key"}\n'
            "]'\n"
        )
        problems = _scan(tmp_path, text)
        assert [(p.kind, p.line, p.name) for p in problems] == [
            ("unparsable", 2, "API_KEYS_JSON"),
            ("invalid_name", 4, None),
        ]

    def test_unquoted_multiline_value_reports_every_leftover_line(self, tmp_path):
        text = 'API_KEYS_JSON=[\n  {"id": "a"}\n]\n'
        problems = _scan(tmp_path, text)
        # Line 1 parses (as the value "["); python-dotenv rejects the next
        # line and reads the closing bracket as a variable named "]".
        assert [(p.kind, p.line, p.name) for p in problems] == [
            ("cut_off", 1, "API_KEYS_JSON"),
            ("unparsable", 2, None),
            ("invalid_name", 3, None),
        ]

    def test_the_cut_off_start_is_named_once_for_the_whole_leftover(self, tmp_path):
        problems = _scan(tmp_path, 'X=1\nAPI_KEYS_JSON=[\n{"a":1}\n{"b":2}\n]\n')
        assert [p.kind for p in problems].count("cut_off") == 1
        assert [(p.kind, p.line, p.name) for p in problems][0] == ("cut_off", 2, "API_KEYS_JSON")

    def test_a_lone_opening_bracket_with_nothing_wrong_after_it_is_left_alone(self, tmp_path):
        assert _scan(tmp_path, "A=[\nB=2\n") == []

    def test_a_comment_after_the_opener_is_not_mistaken_for_a_cut_off_value(self, tmp_path):
        problems = _scan(tmp_path, 'A=[\n# note\n]\n')
        assert [p.kind for p in problems] == ["invalid_name"]

    def test_double_quoted_json_is_unparsable(self, tmp_path):
        problems = _scan(tmp_path, 'A=1\nAPI_KEYS_JSON="[{"id":"a"}]"\n')
        assert [(p.kind, p.line, p.name) for p in problems] == [
            ("unparsable", 2, "API_KEYS_JSON")
        ]

    def test_line_without_a_name_is_reported_without_one(self, tmp_path):
        problems = _scan(tmp_path, "A=1\n=oops\n")
        assert [(p.kind, p.line, p.name) for p in problems] == [("unparsable", 2, None)]

    def test_export_prefix_does_not_hide_the_name(self, tmp_path):
        problems = _scan(tmp_path, "export API_KEYS_JSON='[\n{\"n\": \"Ali's\"}\n]'\n")
        assert problems[0].kind == "unparsable"
        assert problems[0].name == "API_KEYS_JSON"

    def test_blank_lines_and_comments_before_a_problem_do_not_shift_its_line(self, tmp_path):
        # python-dotenv marks a binding before skipping the blank lines above it.
        text = "# header\n\n\nA=1\n\n# note\n\nBAD='unterminated\n"
        problems = _scan(tmp_path, text)
        assert [(p.kind, p.line, p.name) for p in problems] == [("unparsable", 8, "BAD")]


class TestInvalidNames:
    def test_a_name_that_is_not_an_environment_variable_is_reported(self, tmp_path):
        problems = _scan(tmp_path, "A=1\nmy-var=2\n")
        assert [(p.kind, p.line) for p in problems] == [("invalid_name", 2)]

    def test_leading_digit_is_invalid(self, tmp_path):
        assert [p.kind for p in _scan(tmp_path, "1A=1\n")] == ["invalid_name"]

    def test_a_bare_name_without_a_value_is_not_a_problem(self, tmp_path):
        assert _scan(tmp_path, "FLAG\nOTHER_FLAG=1\n") == []

    def test_the_text_of_an_invalid_name_is_never_echoed(self, tmp_path):
        problems = _scan(tmp_path, f"{SECRET}-name=1\n")
        assert [p.kind for p in problems] == ["invalid_name"]
        assert SECRET not in problems[0].message


class TestReassignedVariables:
    def test_different_values_report_name_and_both_lines(self, tmp_path):
        problems = _scan(tmp_path, "A=1\nHOST=one\nB=2\n\nHOST=two\n")
        assert [(p.kind, p.line, p.name, p.lines) for p in problems] == [
            ("reassigned", 2, "HOST", (2, 5))
        ]
        assert problems[0].message == (
            "HOST is assigned on lines 2 and 5 with different values; "
            "the last one (line 5) wins"
        )

    def test_three_assignments_list_every_line(self, tmp_path):
        problems = _scan(tmp_path, "X=1\nX=2\nX=1\n")
        assert problems[0].lines == (1, 2, 3)
        assert "lines 1, 2, and 3" in problems[0].message

    def test_an_identical_repeat_is_harmless_and_not_reported(self, tmp_path):
        # An operator pasting the same password line twice changed nothing.
        assert _scan(tmp_path, f"DB_PASSWORD={SECRET}\nOTHER=1\nDB_PASSWORD={SECRET}\n") == []

    def test_values_never_appear_in_the_message(self, tmp_path):
        problems = _scan(tmp_path, f"DB_PASSWORD={SECRET}\nDB_PASSWORD={SECRET}-changed\n")
        assert [p.kind for p in problems] == ["reassigned"]
        assert SECRET not in problems[0].message
        assert SECRET not in format_problems(".env", problems)


class TestByteOrderMark:
    """A "UTF-8 with BOM" file: python-dotenv reads the first name as U+FEFF + NAME."""

    BOM = "\ufeff"

    def test_a_valid_first_line_gives_exactly_one_bom_problem_naming_the_variable(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}OPENAI_MODEL=x\nDB_HOST=localhost\n")
        assert [(p.kind, p.line, p.name) for p in problems] == [("bom", 1, "OPENAI_MODEL")]

    def test_the_message_names_the_cause_and_the_fix(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}OPENAI_MODEL=x\n")
        assert problems[0].message == (
            "line 1: the file starts with a byte-order mark (BOM), so the first "
            "variable (OPENAI_MODEL) is read under the wrong name -- "
            "save .env as UTF-8 without BOM"
        )

    def test_the_first_binding_is_not_also_an_invalid_name(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}A=1\nB=2\n")
        assert "invalid_name" not in [p.kind for p in problems]

    def test_a_first_line_that_is_no_valid_name_omits_it_from_the_message(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}my-var=1\nB=2\n")
        assert [(p.kind, p.line, p.name) for p in problems] == [("bom", 1, None)]
        assert problems[0].message == (
            "line 1: the file starts with a byte-order mark (BOM), so the first "
            "variable is read under the wrong name -- save .env as UTF-8 without BOM"
        )

    def test_a_comment_first_line_names_no_variable(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}# settings\nA=1\n")
        assert [(p.kind, p.line, p.name) for p in problems] == [("bom", 1, None)]

    def test_a_file_that_is_only_a_bom_is_reported(self, tmp_path):
        assert [(p.kind, p.name) for p in _scan(tmp_path, self.BOM)] == [("bom", None)]

    def test_crlf_changes_nothing(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}OPENAI_MODEL=x\r\nDB_HOST=localhost\r\n")
        assert [(p.kind, p.line, p.name) for p in problems] == [("bom", 1, "OPENAI_MODEL")]

    def test_later_problems_are_still_reported_on_their_own_lines(self, tmp_path):
        text = (
            f"{self.BOM}OPENAI_MODEL=x\n"
            "API_KEYS_JSON='[\n"
            '  {"id": "a", "name": "Ali\'s key"}\n'
            "]'\n"
            "HOST=one\n"
            "HOST=two\n"
        )
        problems = _scan(tmp_path, text)
        assert [(p.kind, p.line, p.name) for p in problems] == [
            ("bom", 1, "OPENAI_MODEL"),
            ("unparsable", 2, "API_KEYS_JSON"),
            ("invalid_name", 4, None),
            ("reassigned", 5, "HOST"),
        ]

    def test_the_same_with_crlf_keeps_physical_line_numbers(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}A=1\r\n\r\nBAD='open\r\nB=2\r\n")
        assert [(p.kind, p.line, p.name) for p in problems] == [
            ("bom", 1, "A"),
            ("unparsable", 3, "BAD"),
        ]

    def test_an_unparsable_first_line_is_reported_as_both(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}BAD='open\nB=2\n")
        assert [(p.kind, p.line, p.name) for p in problems] == [
            ("bom", 1, "BAD"),
            ("unparsable", 1, "BAD"),
        ]

    def test_a_bom_in_the_middle_of_the_file_is_not_a_byte_order_mark(self, tmp_path):
        problems = _scan(tmp_path, f"A=1\n{self.BOM}B=2\n")
        assert [(p.kind, p.line) for p in problems] == [("invalid_name", 2)]

    def test_the_value_is_never_in_the_message(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}DB_PASSWORD={SECRET}\n")
        assert [p.kind for p in problems] == ["bom"]
        assert SECRET not in problems[0].message
        assert SECRET not in format_problems(".env", problems)

    def test_the_same_file_without_the_bom_is_clean(self, tmp_path):
        assert _scan(tmp_path, "OPENAI_MODEL=x\nDB_HOST=localhost\n") == []

    def test_without_a_bom_the_first_line_is_checked_as_before(self, tmp_path):
        problems = _scan(tmp_path, "my-var=1\nB=2\n")
        assert [(p.kind, p.line, p.name) for p in problems] == [("invalid_name", 1, None)]

    def test_it_is_listed_with_the_other_problems(self, tmp_path):
        out = format_problems(".env", _scan(tmp_path, f"{self.BOM}A=1\n"))
        assert "  - line 1: the file starts with a byte-order mark (BOM)" in out
        assert "BOM" in out.splitlines()[-1]


class TestNothingToReport:
    def test_values_are_never_in_any_message(self, tmp_path):
        text = (
            f"OPENAI_API_KEY={SECRET}\n"
            f"API_KEYS_JSON='[\n{{\"name\": \"{SECRET}'s\"}}\n]'\n"
        )
        problems = _scan(tmp_path, text)
        assert problems
        assert SECRET not in format_problems(".env", problems)

    def test_a_clean_file_has_no_problems(self, tmp_path):
        text = (
            "# comment\n"
            "DB_HOST=localhost\n"
            "export PORT=8000\n"
            "QUOTED=\"two words\"\n"
            "EMPTY=\n"
            "API_KEYS_JSON='[\n"
            '  {"id": "a", "name": "A", "key_sha256": "x"}\n'
            "]'\n"
        )
        assert _scan(tmp_path, text) == []

    def test_a_crlf_file_with_a_valid_multiline_value_has_no_problems(self, tmp_path):
        text = (
            "A=1\r\n"
            "API_KEYS_JSON='[\r\n"
            '  {"id": "a", "name": "A"}\r\n'
            "]'\r\n"
            "B=2\r\n"
        )
        assert _scan(tmp_path, text) == []

    def test_crlf_line_numbers_are_still_physical_lines(self, tmp_path):
        problems = _scan(tmp_path, "A=1\r\n\r\nBAD='open\r\nB=2\r\n")
        assert [(p.line, p.name) for p in problems] == [(3, "BAD")]

    def test_an_empty_file_has_no_problems(self, tmp_path):
        assert _scan(tmp_path, "") == []

    def test_no_file_means_no_problems(self, tmp_path):
        assert scan_dotenv(tmp_path / "missing.env") == []

    def test_an_empty_path_means_no_problems(self):
        # find_dotenv() returns "" when there is no .env (containers).
        assert scan_dotenv("") == []

    def test_a_directory_is_not_a_dotenv_file(self, tmp_path):
        assert scan_dotenv(tmp_path) == []

    def test_a_file_that_is_not_utf8_is_reported_without_its_bytes(self, tmp_path):
        path = tmp_path / ".env"
        path.write_bytes(b"A=\xff\xfe\n")
        problems = scan_dotenv(path)
        assert [p.kind for p in problems] == ["unreadable"]
        assert "0xff" not in problems[0].message


class TestFormat:
    def test_every_problem_is_listed_with_the_causes_and_the_fix(self, tmp_path):
        text = "API_KEYS_JSON='[\n{\"n\": \"Ali's\"}\n]'\nHOST=a\nHOST=b\n"
        problems = _scan(tmp_path, text)
        out = format_problems(tmp_path / ".env", problems)
        assert str(tmp_path / ".env") in out
        assert "  - line 1: could not be parsed (looks like an assignment to API_KEYS_JSON)" in out
        assert "  - line 3: not a NAME=value assignment" in out
        assert "single quotes" in out
        assert "  - HOST is assigned on lines 4 and 5" in out
        assert "single quotes" in out
        assert "apostrophe" in out
        assert "double quotes" in out
        assert "API_KEYS_FILE" in out


# ---------------------------------------------------------------------------
# Settings.validate() refuses; the server and verify_deployment both see it
# ---------------------------------------------------------------------------

_PROBLEMS = [
    DotenvProblem("unparsable", 12, "API_KEYS_JSON"),
    DotenvProblem("invalid_name", 15),
    DotenvProblem("reassigned", 3, "DB_HOST", (3, 20)),
]


@pytest.fixture
def broken_dotenv(monkeypatch):
    monkeypatch.setattr(config, "_dotenv_path", "/srv/app/.env")
    monkeypatch.setattr(config, "_dotenv_problems", list(_PROBLEMS))


class TestValidateRefuses:
    def test_validate_raises_value_error_listing_every_problem(self, broken_dotenv):
        with override_settings(openai_model="llama3"):
            with pytest.raises(ValueError) as excinfo:
                config.settings.validate()
        text = str(excinfo.value)
        assert "/srv/app/.env" in text
        assert "line 12" in text and "API_KEYS_JSON" in text
        assert "line 15" in text
        assert "DB_HOST is assigned on lines 3 and 20" in text

    def test_it_is_checked_before_any_other_setting(self, broken_dotenv):
        # A broken .env is often why OPENAI_MODEL looks unset; say that first.
        with override_settings(openai_model=""):
            with pytest.raises(ValueError, match="python-dotenv could not use"):
                config.settings.validate()

    def test_no_recorded_problems_leaves_validate_alone(self, monkeypatch):
        monkeypatch.setattr(config, "_dotenv_problems", [])
        with override_settings(openai_model=""):
            with pytest.raises(ValueError, match="OPENAI_MODEL is not configured"):
                config.settings.validate()

    def test_the_server_refuses_to_start(self, broken_dotenv):
        import api.server as server_module

        async def _start():
            async with server_module.lifespan(server_module.app):
                pass  # pragma: no cover - must not be reached

        with override_settings(openai_model="llama3"):
            with pytest.raises(RuntimeError, match="Invalid configuration: ") as excinfo:
                asyncio.run(_start())
        assert "line 12" in str(excinfo.value)
        assert "DB_HOST is assigned on lines 3 and 20" in str(excinfo.value)

    def test_verify_deployment_fails_with_the_same_text(self, broken_dotenv):
        from scripts.verify_deployment import check_settings_valid

        with override_settings(openai_model="llama3"):
            result = check_settings_valid()
        assert result.status == "FAIL"
        assert "line 12" in result.detail
        assert "line 15" in result.detail
        assert "DB_HOST is assigned on lines 3 and 20" in result.detail


# ---------------------------------------------------------------------------
# The import-time wiring in config.py, in a fresh process
# ---------------------------------------------------------------------------

_PROBE = """
import sys
import dotenv

target = sys.argv[1]
dotenv.find_dotenv = lambda *args, **kwargs: target

import config

print(repr(config._dotenv_path))
for problem in config._dotenv_problems:
    print(problem.message)
"""


def _import_config_against(tmp_path: Path, dotenv_text: str | None, **env: str) -> list[str]:
    import os

    target = ""
    if dotenv_text is not None:
        path = tmp_path / ".env"
        path.write_text(dotenv_text, encoding="utf-8")
        target = str(path)
    run_env = {**os.environ, "PROJECT_CONFIG_DIR": "project_config.example", **env}
    run_env.pop("PYTHON_DOTENV_DISABLED", None)
    run_env.update(env)
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, target],
        cwd=_REPO_ROOT, env=run_env, capture_output=True, text=True, check=True,
    )
    return done.stdout.splitlines()


class TestConfigScansTheFileItLoaded:
    def test_a_broken_file_is_recorded_with_its_path(self, tmp_path):
        out = _import_config_against(tmp_path, f"A=1\nBAD='x\nDB_PASSWORD={SECRET}\n")
        assert out[0] == repr(str(tmp_path / ".env"))
        assert out[1:] == ["line 2: could not be parsed (looks like an assignment to BAD)"]
        assert SECRET not in "\n".join(out)

    def test_no_dotenv_file_records_nothing(self, tmp_path):
        assert _import_config_against(tmp_path, None) == ["''"]

    def test_disabled_loading_skips_the_check(self, tmp_path):
        out = _import_config_against(tmp_path, "BAD='x\n", PYTHON_DOTENV_DISABLED="1")
        assert out == ["''"]
