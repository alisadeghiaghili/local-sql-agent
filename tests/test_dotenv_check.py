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
import io
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv.parser import parse_stream

import config
from config import override_settings
from core.dotenv_check import DotenvProblem, explain_unset, format_problems, scan_dotenv

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


def _parser_keeps_bom() -> bool:
    """Whether the installed python-dotenv leaves a leading BOM on the first name.

    Releases before 1.2.3 (1.2.2 among them) do, so the variable is lost;
    1.2.3 and newer strip it themselves, and requirements.lock pins one of
    those. The BOM tests derive what to expect from this instead of assuming
    one behaviour, so they hold on an older python-dotenv too.
    """
    first = next(iter(parse_stream(io.StringIO("\ufeffA=1\n"))))
    return first.key is not None and first.key.startswith("\ufeff")


PARSER_KEEPS_BOM = _parser_keeps_bom()


def _bom_if_kept(name: str) -> list[tuple[str, int, str | None]]:
    """The ``bom`` problem expected for a first assignment *name*, if any."""
    return [("bom", 1, name)] if PARSER_KEEPS_BOM else []


def _triples(problems: list[DotenvProblem]) -> list[tuple[str, int, str | None]]:
    return [(p.kind, p.line, p.name) for p in problems]


class TestByteOrderMark:
    """A "UTF-8 with BOM" file, on whichever python-dotenv is installed.

    Where the parser keeps the mark the first variable is read as U+FEFF +
    NAME and is reported; where it strips the mark nothing is lost and
    nothing is reported.
    """

    BOM = "\ufeff"

    def test_a_valid_first_line_gives_one_bom_problem_naming_the_variable(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}OPENAI_MODEL=x\nDB_HOST=localhost\n")
        assert _triples(problems) == _bom_if_kept("OPENAI_MODEL")

    def test_the_message_names_the_cause_and_the_fix(self):
        assert DotenvProblem("bom", 1, "OPENAI_MODEL").message == (
            "line 1: the file starts with a byte-order mark (BOM), so the first "
            "variable (OPENAI_MODEL) is read under the wrong name -- "
            "save .env as UTF-8 without BOM"
        )

    def test_the_message_omits_the_name_when_there_is_none(self):
        assert DotenvProblem("bom", 1).message == (
            "line 1: the file starts with a byte-order mark (BOM), so the first "
            "variable is read under the wrong name -- save .env as UTF-8 without BOM"
        )

    def test_the_first_binding_is_not_also_an_invalid_name(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}A=1\nB=2\n")
        assert "invalid_name" not in [p.kind for p in problems]

    def test_a_bom_before_a_comment_loses_nothing_and_is_not_reported(self, tmp_path):
        # Where the mark stays it glues to the comment and becomes a
        # None-valued "\ufeff" key that load_dotenv ignores.
        assert _scan(tmp_path, f"{self.BOM}# settings\nOPENAI_MODEL=x\n") == []

    def test_a_bom_before_a_blank_line_loses_nothing_and_is_not_reported(self, tmp_path):
        # Where the mark stays it attaches to the blank line, not to the
        # assignment below it.
        assert _scan(tmp_path, f"{self.BOM}\nOPENAI_MODEL=x\n") == []
        assert _scan(tmp_path, f"{self.BOM}\r\n\r\nOPENAI_MODEL=x\r\n") == []

    def test_a_file_that_is_only_a_bom_is_not_reported(self, tmp_path):
        assert _scan(tmp_path, self.BOM) == []

    def test_a_bom_before_a_bare_name_loses_nothing_and_is_not_reported(self, tmp_path):
        assert _scan(tmp_path, f"{self.BOM}FLAG\nA=1\n") == []

    def test_a_problem_is_reported_exactly_when_python_dotenv_loses_the_variable(self, tmp_path):
        from dotenv import dotenv_values

        for text in (
            f"{self.BOM}A=1\n",
            f"{self.BOM}A=1\r\nB=2\r\n",
            f"{self.BOM}# c\nA=1\n",
            f"{self.BOM}\nA=1\n",
            f"{self.BOM}   A=1\n",
        ):
            path = tmp_path / ".env"
            path.write_text(text, encoding="utf-8", newline="")
            lost = dotenv_values(path).get("A") != "1"
            reported = [p.kind for p in scan_dotenv(path)].count("bom")
            assert reported == int(lost), text

    def test_a_bom_before_an_invalid_name_is_an_invalid_name_not_a_bom(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}my-var=1\nB=2\n")
        assert _triples(problems) == [("invalid_name", 1, None)]

    def test_a_bom_before_whitespace_then_a_name(self, tmp_path):
        # Where the mark stays python-dotenv rejects the line, so A is lost
        # and both causes are named; where it is stripped the line is fine.
        problems = _scan(tmp_path, f"{self.BOM}   A=1\nB=2\n")
        expected = [("bom", 1, "A"), ("unparsable", 1, "A")] if PARSER_KEEPS_BOM else []
        assert _triples(problems) == expected

    def test_a_second_bom_is_still_a_bom_on_every_release(self, tmp_path):
        # Releases that strip the mark strip one; the other is in the name.
        problems = _scan(tmp_path, f"{self.BOM}{self.BOM}A=1\n")
        assert _triples(problems) == [("bom", 1, "A")]

    def test_crlf_changes_nothing(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}OPENAI_MODEL=x\r\nDB_HOST=localhost\r\n")
        assert _triples(problems) == _bom_if_kept("OPENAI_MODEL")

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
        assert _triples(problems) == _bom_if_kept("OPENAI_MODEL") + [
            ("unparsable", 2, "API_KEYS_JSON"),
            ("invalid_name", 4, None),
            ("reassigned", 5, "HOST"),
        ]

    def test_the_same_with_crlf_keeps_physical_line_numbers(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}A=1\r\n\r\nBAD='open\r\nB=2\r\n")
        assert _triples(problems) == _bom_if_kept("A") + [("unparsable", 3, "BAD")]

    def test_line_numbers_are_physical_after_a_bom_and_blank_lines(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}\n\nBAD='open\nB=2\n")
        assert _triples(problems) == [("unparsable", 3, "BAD")]

    def test_an_unparsable_first_line_keeps_its_name(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}BAD='open\nB=2\n")
        assert _triples(problems) == _bom_if_kept("BAD") + [("unparsable", 1, "BAD")]

    def test_a_comment_first_bom_file_still_reports_its_later_problems(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}# c\nA=1\nBAD='open\n")
        assert _triples(problems) == [("unparsable", 3, "BAD")]

    def test_a_bom_in_the_middle_of_the_file_is_not_a_byte_order_mark(self, tmp_path):
        problems = _scan(tmp_path, f"A=1\n{self.BOM}B=2\n")
        assert [(p.kind, p.line) for p in problems] == [("invalid_name", 2)]

    def test_the_value_is_never_in_a_message(self, tmp_path):
        problems = _scan(tmp_path, f"{self.BOM}DB_PASSWORD={SECRET}\n")
        assert _triples(problems) == _bom_if_kept("DB_PASSWORD")
        assert SECRET not in "".join(_messages(problems))
        assert SECRET not in format_problems(".env", problems)

    def test_the_same_file_without_the_bom_is_clean(self, tmp_path):
        assert _scan(tmp_path, "OPENAI_MODEL=x\nDB_HOST=localhost\n") == []

    def test_without_a_bom_the_first_line_is_checked_as_before(self, tmp_path):
        problems = _scan(tmp_path, "my-var=1\nB=2\n")
        assert _triples(problems) == [("invalid_name", 1, None)]

    def test_it_is_listed_with_the_other_problems(self):
        out = format_problems(".env", [DotenvProblem("bom", 1, "A")])
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
        cwd=_REPO_ROOT, env=run_env, capture_output=True, text=True, encoding="utf-8", check=True,
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


_MISSING_PROBE = """
import sys

sys.modules["dotenv"] = None  # makes "import dotenv" raise ImportError

import config

print(config._dotenv_missing)
print(config._dotenv_path == "")
for line in config.explain_unset_variable("Auction_DM_PASSWORD"):
    print(line)
"""


class TestPythonDotenvMissing:
    """Issue #237: python-dotenv absent, so .env is silently skipped."""

    def test_the_explanation_names_the_package_the_interpreter_and_the_fix(self, monkeypatch):
        monkeypatch.setattr(config, "_dotenv_missing", True)
        lines = config.explain_unset_variable("X")
        assert len(lines) == 1
        assert "python-dotenv is not installed" in lines[0]
        assert sys.executable in lines[0]
        assert "requirements.lock" in lines[0]
        assert ".env was not read" in lines[0]

    def test_it_does_not_consult_the_dotenv_file(self, monkeypatch, tmp_path):
        path = tmp_path / ".env"
        path.write_text(f"X={SECRET}\n", encoding="utf-8")
        monkeypatch.setattr(config, "_dotenv_missing", True)
        monkeypatch.setattr(config, "_dotenv_path", str(path))
        text = "\n".join(config.explain_unset_variable("X"))
        assert SECRET not in text
        assert str(path) not in text

    def test_a_present_python_dotenv_does_not_say_it_is_missing(self):
        assert config._dotenv_missing is False
        assert not any("not installed" in line for line in config.explain_unset_variable("X"))

    def test_a_process_without_python_dotenv_records_and_explains_it(self):
        import os

        run_env = {**os.environ, "PROJECT_CONFIG_DIR": "project_config.example"}
        run_env.pop("PYTHON_DOTENV_DISABLED", None)
        run_env.pop("Auction_DM_PASSWORD", None)
        done = subprocess.run(
            [sys.executable, "-c", _MISSING_PROBE],
            cwd=_REPO_ROOT, env=run_env, capture_output=True, text=True, encoding="utf-8", check=True,
        )
        out = done.stdout.splitlines()
        assert out[:2] == ["True", "True"]
        assert len(out) == 3
        assert "python-dotenv is not installed" in out[2]
        assert sys.executable in out[2]
        assert "requirements.lock" in out[2]


# ---------------------------------------------------------------------------
# Why a variable expected from .env is missing (issue #237)
# ---------------------------------------------------------------------------

_VAR = "Auction_DM_PASSWORD"


def _explain(tmp_path: Path, text: str | None, environ=None, **kwargs) -> list[str]:
    path = ""
    if text is not None:
        target = tmp_path / ".env"
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        path = str(target)
    problems = scan_dotenv(path) if path else []
    return explain_unset(_VAR, path, problems, environ or {}, tmp_path, **kwargs)


class TestExplainUnset:
    def test_no_dotenv_file_names_the_folder_searched(self, tmp_path):
        (out,) = _explain(tmp_path, None)
        assert f"no .env file was found in {tmp_path}" in out
        assert "copy .env.example" in out

    def test_a_dotenv_txt_is_named_without_blaming_one_operating_system(self, tmp_path):
        (tmp_path / ".env.txt").write_text(f"{_VAR}={SECRET}\n", encoding="utf-8")
        (out,) = _explain(tmp_path, None)
        assert str(tmp_path / ".env.txt") in out and "rename it to .env" in out
        assert "a file manager that hides extensions" in out
        assert "Windows" not in out and "create" not in out
        assert SECRET not in out

    def test_a_dotenv_txt_in_a_folder_above_is_found_as_find_dotenv_walks_up(self, tmp_path):
        (tmp_path / ".env.txt").write_text(f"{_VAR}={SECRET}\n", encoding="utf-8")
        deep = tmp_path / "app" / "src"
        deep.mkdir(parents=True)
        (out,) = explain_unset(_VAR, "", [], {}, deep)
        assert str(tmp_path / ".env.txt") in out and f"no .env file was found in {deep}" in out
        assert SECRET not in out

    def test_disabled_loading_is_named(self, tmp_path):
        (out,) = _explain(tmp_path, None, disabled=True)
        assert out.startswith("PYTHON_DOTENV_DISABLED is set")

    def test_a_file_without_the_variable(self, tmp_path):
        (out,) = _explain(tmp_path, f"DB_HOST=db1\nOTHER={SECRET}\n")
        assert out.endswith(f"does not assign {_VAR}; add a line {_VAR}=... to it")

    def test_an_empty_value_names_its_line(self, tmp_path):
        (out,) = _explain(tmp_path, f"DB_HOST=db1\n\n{_VAR}=\n")
        assert out.endswith(f"line 3 assigns {_VAR} an empty or blank value")

    def test_a_name_in_other_case_is_named(self, tmp_path):
        (out,) = _explain(tmp_path, f"AUCTION_DM_PASSWORD={SECRET}\n", case_sensitive=True)
        assert "line 1 assigns AUCTION_DM_PASSWORD" in out and f"spelled {_VAR}" in out
        assert SECRET not in out

    def test_an_unparsable_line_is_listed_without_its_value(self, tmp_path):
        out = _explain(tmp_path, f"{_VAR}='{SECRET}\n")
        assert out[0].endswith(f"and {_VAR} may be one of them:")
        assert out[1] == f"  - line 1: could not be parsed (looks like an assignment to {_VAR})"
        assert len(out) == 2  # the variable is the unparsable line; not also "does not assign"
        assert SECRET not in "\n".join(out)

    def test_a_crlf_file_with_a_bom_still_finds_the_variable(self, tmp_path):
        (out,) = _explain(tmp_path, f"\ufeff{_VAR}=\r\nB=1\r\n")
        assert out.endswith(f"line 1 assigns {_VAR} an empty or blank value")


def _pin_dotenv_state(monkeypatch, *, path: str = "", search_dir: Path | str | None = None) -> None:
    """Make ``config``'s import-time record independent of the runner's environment.

    ``PYTHON_DOTENV_DISABLED=1``, a missing python-dotenv, an unimportable scan
    module and the folder the search started from are all recorded once at
    import; a test of the explanation must say which it assumes.
    """
    monkeypatch.setattr(config, "_dotenv_path", path)
    monkeypatch.setattr(config, "_dotenv_disabled", False)
    monkeypatch.setattr(config, "_dotenv_missing", False)
    monkeypatch.setattr(config, "_dotenv_scan_unavailable", False)
    if search_dir is not None:
        monkeypatch.setattr(config, "_dotenv_search_dir", str(search_dir))


def _write_structured(directory: Path) -> None:
    (directory / "datasources.yaml").write_text(
        "default: Auction_DM\n"
        "datasources:\n"
        "  Auction_DM:\n"
        "    host: db1\n"
        "    database: AuctionDM\n"
        "    username: nlq\n"
        f"    password_env: {_VAR}\n",
        encoding="utf-8",
    )


class TestUnsetPasswordAtStartup:
    def test_validate_says_why_the_password_is_missing(self, tmp_path, monkeypatch):
        from database.datasources import UnsetVariableError, reset_datasources_cache

        (tmp_path / "datasources.yaml").write_text(
            "default: Auction_DM\n"
            "datasources:\n"
            "  Auction_DM:\n"
            "    host: db1\n"
            "    database: AuctionDM\n"
            "    username: nlq\n"
            f"    password_env: {_VAR}\n",
            encoding="utf-8",
        )
        monkeypatch.delenv(_VAR, raising=False)
        _pin_dotenv_state(monkeypatch, path="")
        reset_datasources_cache()
        try:
            with override_settings(
                openai_model="llama3", sql_dialect="tsql", project_config_dir=str(tmp_path),
            ):
                with pytest.raises(UnsetVariableError) as excinfo:
                    config.settings.validate()
        finally:
            reset_datasources_cache()
        text = str(excinfo.value)
        assert f"{_VAR} (named by password_env) is not set or is empty" in text
        assert "no .env file was found" in text
        assert excinfo.value.variable == _VAR


# ---------------------------------------------------------------------------
# The environment holds an empty or blank value (items 2, 3)
# ---------------------------------------------------------------------------

class TestEmptyVariableInTheEnvironment:
    def test_without_a_dotenv_the_fix_is_to_remove_or_set_it_not_to_create_a_file(self, tmp_path):
        out = _explain(tmp_path, None, environ={_VAR: ""})
        assert out[0].startswith(f"the process environment holds an empty or blank value for {_VAR};")
        assert f"unset it (PowerShell: Remove-Item Env:{_VAR}; bash: unset {_VAR})" in out[0]
        assert f"no .env file was found in {tmp_path}" in out[1]
        assert "create" not in "\n".join(out)

    def test_a_dotenv_without_the_line_still_reports_it_first(self, tmp_path):
        out = _explain(tmp_path, f"DB_HOST=db1\nOTHER={SECRET}\n", environ={_VAR: ""})
        assert out[0].startswith("the process environment holds an empty or blank value")
        assert out[-1].endswith(f"does not assign {_VAR}; add a line {_VAR}=... to it")
        assert SECRET not in "\n".join(out)

    def test_a_dotenv_that_assigns_it_does_not_replace_it(self, tmp_path):
        out = _explain(tmp_path, f"{_VAR}={SECRET}\n", environ={_VAR: ""})
        assert out[0].startswith("the process environment holds an empty or blank value")
        assert "python-dotenv never overrides a variable that is already set" in out[0]
        assert out[1].endswith(f"line 1 assigns {_VAR}")
        assert SECRET not in "\n".join(out)

    def test_a_whitespace_only_value_in_the_environment_is_blank(self, tmp_path):
        out = _explain(tmp_path, None, environ={_VAR: " \t "})
        assert out[0].startswith("the process environment holds an empty or blank value")

    def test_a_set_variable_is_not_reported(self, tmp_path):
        out = _explain(tmp_path, None, environ={_VAR: SECRET})
        assert not any("holds an empty" in line for line in out)
        assert SECRET not in "\n".join(out)

    def test_it_does_not_claim_where_the_empty_value_came_from(self, tmp_path):
        text = "\n".join(_explain(tmp_path, f"{_VAR}={SECRET}\n", environ={_VAR: ""}))
        assert "already holds" not in text
        assert "before startup" not in text
        assert "expansion in .env of an unset variable also yields an empty value" in text

    def test_disabled_loading_still_reports_the_empty_value_first(self, tmp_path):
        out = _explain(tmp_path, None, environ={_VAR: ""}, disabled=True)
        assert out[0].startswith("the process environment holds an empty or blank value")
        assert out[1].startswith("PYTHON_DOTENV_DISABLED is set")


class TestBlankAndInterpolatedValues:
    def test_a_whitespace_only_value_is_blank(self, tmp_path):
        out = _explain(tmp_path, f'DB_HOST=db1\n{_VAR}="   "\n')
        assert out[-1].endswith(f"line 2 assigns {_VAR} an empty or blank value")

    def test_an_interpolated_value_names_its_line_but_not_the_value(self, tmp_path):
        out = _explain(tmp_path, f"{_VAR}=${{VAULT_{SECRET}}}\n", environ={_VAR: ""})
        text = "\n".join(out)
        assert f"line 1 assigns {_VAR} a value containing ${{...}}" in text
        assert "empty value when it is not set" in text
        assert SECRET not in text

    def test_interpolation_is_reported_for_a_variable_missing_from_the_environment_too(self, tmp_path):
        out = _explain(tmp_path, f"{_VAR}=${{OTHER}}\n")
        assert any("containing ${...}" in line for line in out)
        assert not any("not in the process environment" in line for line in out)

    def test_assignments_record_the_flag_and_never_the_value(self, tmp_path):
        from core.dotenv_check import _assignments

        path = tmp_path / ".env"
        path.write_text(f"A=${{B}}{SECRET}\nC={SECRET}\nD=\nE='  '\n", encoding="utf-8")
        found = _assignments(path)
        assert found == {
            "A": [(1, False, True)],
            "C": [(2, False, False)],
            "D": [(3, True, False)],
            "E": [(4, True, False)],
        }
        assert SECRET not in repr(found)


class TestAssignedButNotInTheEnvironment:
    def test_the_reason_is_a_stale_process_or_another_dotenv(self, tmp_path):
        out = _explain(tmp_path, f"DB_HOST=db1\n{_VAR}={SECRET}\n")
        (line,) = out
        assert line.startswith(f"{tmp_path / '.env'} line 2 assigns {_VAR}, but {_VAR} is not in the process environment")
        assert "started before the file was edited (restart it)" in line
        assert f"a different .env than {tmp_path / '.env'} was loaded" in line
        assert SECRET not in line


class TestCaseSensitivity:
    def test_case_sensitive_names_the_mismatch(self, tmp_path):
        (out,) = _explain(tmp_path, f"AUCTION_DM_PASSWORD={SECRET}\n", case_sensitive=True)
        assert "line 1 assigns AUCTION_DM_PASSWORD; variable names are case-sensitive" in out
        assert f"so it must be spelled {_VAR}" in out
        assert "empty" not in out
        assert SECRET not in out

    def test_case_sensitive_says_when_the_other_case_line_is_empty_too(self, tmp_path):
        (out,) = _explain(tmp_path, "AUCTION_DM_PASSWORD=\n", case_sensitive=True)
        assert out.endswith("(and its value is empty or blank)")

    def test_case_insensitive_treats_the_other_case_line_as_the_variable(self, tmp_path):
        out = _explain(tmp_path, "AUCTION_DM_PASSWORD=\n", case_sensitive=False)
        assert out == [f"{tmp_path / '.env'} line 1 assigns {_VAR} an empty or blank value"]
        assert "case-sensitive" not in "\n".join(out)

    def test_case_insensitive_applies_the_interpolation_check_to_that_line(self, tmp_path):
        out = _explain(tmp_path, "auction_dm_password=${X}\n", case_sensitive=False)
        assert any("containing ${...}" in line for line in out)
        assert "case-sensitive" not in "\n".join(out)

    def test_case_insensitive_with_a_value_says_it_is_missing_from_the_environment(self, tmp_path):
        out = _explain(tmp_path, f"AUCTION_DM_PASSWORD={SECRET}\n", case_sensitive=False)
        assert len(out) == 1 and "is not in the process environment" in out[0]
        assert "case-sensitive" not in out[0] and SECRET not in out[0]

    def test_the_default_follows_the_operating_system(self):
        import inspect

        default = inspect.signature(explain_unset).parameters["case_sensitive"].default
        assert default is (__import__("os").name != "nt")


class TestAnUnreadableFile:
    def test_it_does_not_also_claim_the_variable_is_not_assigned(self, tmp_path):
        path = tmp_path / ".env"
        path.write_bytes(b"A=1\n" + _VAR.encode() + b"=\xff\xfe\n")
        problems = scan_dotenv(path)
        assert [p.kind for p in problems] == ["unreadable"]
        out = explain_unset(_VAR, str(path), problems, {}, tmp_path)
        assert any("could not be read as UTF-8 text" in line for line in out)
        assert not any("does not assign" in line for line in out)


# ---------------------------------------------------------------------------
# Where python-dotenv searched (item 1)
# ---------------------------------------------------------------------------

class TestSearchStart:
    @pytest.fixture()
    def plain_script(self, monkeypatch, tmp_path):
        """The state of a server started as ``python app.py``: no REPL, no tracer."""
        import types

        main = types.ModuleType("__main__")
        main.__file__ = str(tmp_path / "app.py")
        monkeypatch.setitem(sys.modules, "__main__", main)
        monkeypatch.delattr(sys, "ps1", raising=False)
        monkeypatch.delattr(sys, "ps2", raising=False)
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "gettrace", lambda: None)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        return elsewhere

    def test_a_plain_script_searches_from_the_folder_of_config_py(self, plain_script):
        assert config._dotenv_search_start() == str(Path(config.__file__).resolve().parent)

    def test_a_debugger_or_tracer_searches_from_the_working_directory(self, plain_script, monkeypatch):
        monkeypatch.setattr(sys, "gettrace", lambda: object())
        assert config._dotenv_search_start() == str(plain_script.resolve())

    def test_an_interactive_session_searches_from_the_working_directory(self, plain_script, monkeypatch):
        monkeypatch.setattr(sys, "ps1", ">>> ", raising=False)
        assert config._dotenv_search_start() == str(plain_script.resolve())

    def test_ps2_alone_counts_as_interactive(self, plain_script, monkeypatch):
        monkeypatch.setattr(sys, "ps2", "... ", raising=False)
        assert config._dotenv_search_start() == str(plain_script.resolve())

    def test_a_main_module_without_a_file_is_interactive(self, plain_script, monkeypatch):
        import types

        monkeypatch.setitem(sys.modules, "__main__", types.ModuleType("__main__"))
        assert config._dotenv_search_start() == str(plain_script.resolve())

    def test_a_frozen_executable_searches_from_the_working_directory(self, plain_script, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert config._dotenv_search_start() == str(plain_script.resolve())

    def test_the_helper_agrees_with_find_dotenv_itself(self, plain_script, monkeypatch):
        """Same conditions as python-dotenv: a .env in the cwd is found only when it searches there."""
        from dotenv import find_dotenv

        (plain_script / ".env").write_text("A=1\n", encoding="utf-8")
        assert find_dotenv() != str(plain_script / ".env")  # called from a file: not the cwd
        monkeypatch.setattr(sys, "gettrace", lambda: object())
        assert find_dotenv() == str(plain_script / ".env")
        assert config._dotenv_search_start() == str(plain_script.resolve())


class TestExplanationNamesTheFolderSearched:
    def test_the_recorded_folder_is_the_one_named(self, tmp_path, monkeypatch):
        cwd = tmp_path / "launched-from"
        cwd.mkdir()
        _pin_dotenv_state(monkeypatch, path="", search_dir=cwd)
        (out,) = config.explain_unset_variable(_VAR)
        assert f"no .env file was found in {cwd} " in out
        assert f"create {cwd / '.env'} " in out
        assert str(Path(config.__file__).resolve().parent) not in out

    def test_the_dotenv_txt_probe_uses_the_recorded_folder(self, tmp_path, monkeypatch):
        cwd = tmp_path / "launched-from"
        cwd.mkdir()
        (cwd / ".env.txt").write_text(f"{_VAR}={SECRET}\n", encoding="utf-8")
        _pin_dotenv_state(monkeypatch, path="", search_dir=cwd)
        (out,) = config.explain_unset_variable(_VAR)
        assert str(cwd / ".env.txt") in out and SECRET not in out

    def test_a_process_started_from_another_folder_records_the_working_directory(self, tmp_path):
        """End to end: ``python -c`` has no ``__file__``, so find_dotenv() searches the cwd."""
        import os

        (tmp_path / ".env").write_text(f"{_VAR}={SECRET}\n", encoding="utf-8")
        probe = (
            "import os, config\n"
            "print(os.getcwd())\n"
            "print(config._dotenv_search_dir)\n"
            "print(config._dotenv_path)\n"
        )
        run_env = {
            **os.environ,
            "PROJECT_CONFIG_DIR": str(_REPO_ROOT / "project_config.example"),
            "PYTHONPATH": str(_REPO_ROOT),
        }
        run_env.pop("PYTHON_DOTENV_DISABLED", None)
        done = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=tmp_path, env=run_env, capture_output=True, text=True, encoding="utf-8", check=True,
        )
        cwd, search_dir, found = done.stdout.splitlines()
        assert search_dir == cwd
        assert found == str(Path(cwd) / ".env")
        assert search_dir != str(_REPO_ROOT)


# ---------------------------------------------------------------------------
# The explanation never masks the real error (item 7)
# ---------------------------------------------------------------------------

class TestExplanationNeverMasksTheError:
    @pytest.fixture()
    def explain_raises(self, monkeypatch):
        import core.dotenv_check as module

        def boom(*args, **kwargs):
            raise RuntimeError(f"boom with {SECRET}")

        monkeypatch.setattr(module, "explain_unset", boom)

    def test_a_failing_explanation_is_an_empty_list(self, explain_raises, monkeypatch, caplog):
        _pin_dotenv_state(monkeypatch)
        with caplog.at_level("DEBUG"):
            assert config.explain_unset_variable(_VAR) == []
        assert SECRET not in caplog.text  # only the exception type is logged

    def test_validate_still_raises_the_original_error(self, explain_raises, tmp_path, monkeypatch):
        from database.datasources import UnsetVariableError, reset_datasources_cache

        _write_structured(tmp_path)
        monkeypatch.delenv(_VAR, raising=False)
        _pin_dotenv_state(monkeypatch)
        reset_datasources_cache()
        try:
            with override_settings(
                openai_model="llama3", sql_dialect="tsql", project_config_dir=str(tmp_path),
            ):
                with pytest.raises(UnsetVariableError) as excinfo:
                    config.settings.validate()
        finally:
            reset_datasources_cache()
        assert str(excinfo.value).endswith(f"{_VAR} (named by password_env) is not set or is empty")
        assert excinfo.value.variable == _VAR
        assert SECRET not in str(excinfo.value)

    def test_the_setup_tools_still_raise_the_original_error(self, explain_raises, tmp_path, monkeypatch):
        from database.datasources import reset_datasources_cache
        from database.setup_sources import SourceSelectionError, configured_sources

        _write_structured(tmp_path)
        monkeypatch.delenv(_VAR, raising=False)
        _pin_dotenv_state(monkeypatch)
        reset_datasources_cache()
        try:
            with override_settings(project_config_dir=str(tmp_path)):
                with pytest.raises(SourceSelectionError) as excinfo:
                    configured_sources()
        finally:
            reset_datasources_cache()
        assert str(excinfo.value).endswith(f"{_VAR} (named by password_env) is not set or is empty")


# ---------------------------------------------------------------------------
# A scan module that cannot be imported is not "python-dotenv is missing" (item 8)
# ---------------------------------------------------------------------------

_NO_SCAN_PROBE = """
import os
import sys
import dotenv

target = sys.argv[1]
dotenv.find_dotenv = lambda *args, **kwargs: target
sys.modules["core.dotenv_check"] = None  # makes "import core.dotenv_check" raise ImportError

import config

print(config._dotenv_missing)
print(config._dotenv_scan_unavailable)
print(config._dotenv_problems)
print(os.environ.get("NO_SCAN_PROBE_VARIABLE"))
for line in config.explain_unset_variable("NO_SCAN_PROBE_VARIABLE"):
    print(line)
"""


class TestScanModuleUnavailable:
    def test_dotenv_is_still_loaded_and_the_explanation_does_not_blame_python_dotenv(self, tmp_path):
        import os

        path = tmp_path / ".env"
        path.write_text("NO_SCAN_PROBE_VARIABLE=loaded-anyway\n", encoding="utf-8")
        run_env = {**os.environ, "PROJECT_CONFIG_DIR": "project_config.example"}
        run_env.pop("PYTHON_DOTENV_DISABLED", None)
        run_env.pop("NO_SCAN_PROBE_VARIABLE", None)
        done = subprocess.run(
            [sys.executable, "-c", _NO_SCAN_PROBE, str(path)],
            cwd=_REPO_ROOT, env=run_env, capture_output=True, text=True, encoding="utf-8", check=True,
        )
        out = done.stdout.splitlines()
        assert out[:4] == ["False", "True", "[]", "loaded-anyway"]
        assert done.stderr.strip() == ""  # no warning at import time
        text = "\n".join(out[4:])
        assert "not installed" not in text and "requirements.lock" not in text
        assert "core.dotenv_check could not be imported" in text
        assert str(path) in text
        assert "loaded-anyway" not in text

    def test_the_in_process_flag_gives_the_same_sentence(self, monkeypatch, tmp_path):
        _pin_dotenv_state(monkeypatch, path=str(tmp_path / ".env"))
        monkeypatch.setattr(config, "_dotenv_scan_unavailable", True)
        (out,) = config.explain_unset_variable(_VAR)
        assert "not installed" not in out and "could not be imported" in out

    def test_a_missing_python_dotenv_is_still_reported_as_missing(self, monkeypatch):
        _pin_dotenv_state(monkeypatch)
        monkeypatch.setattr(config, "_dotenv_missing", True)
        (out,) = config.explain_unset_variable(_VAR)
        assert "python-dotenv is not installed" in out


# ---------------------------------------------------------------------------
# A url_env / DB_CONNECTION_URL that is unset or empty gets the explanation too (item 5)
# ---------------------------------------------------------------------------

class TestUnsetConnectionStringAtStartup:
    @pytest.fixture()
    def validate_with(self, tmp_path, monkeypatch):
        from database.datasources import reset_datasources_cache

        _pin_dotenv_state(monkeypatch, path="", search_dir=tmp_path)

        def run(**settings):
            reset_datasources_cache()
            try:
                with override_settings(
                    openai_model="llama3", sql_dialect="tsql", project_config_dir=str(tmp_path), **settings,
                ):
                    config.settings.validate()
            finally:
                reset_datasources_cache()

        return run

    def test_an_unset_url_env_variable_says_why(self, tmp_path, monkeypatch, validate_with):
        from database.datasources import UnsetVariableError

        (tmp_path / "datasources.yaml").write_text(
            "default: main\ndatasources:\n  main:\n    url_env: DB_URL_MAIN\n", encoding="utf-8",
        )
        monkeypatch.delenv("DB_URL_MAIN", raising=False)
        with pytest.raises(UnsetVariableError, match="DB_URL_MAIN is not configured") as excinfo:
            validate_with()
        assert excinfo.value.variable == "DB_URL_MAIN"
        assert f"\n  no .env file was found in {tmp_path} " in str(excinfo.value)

    def test_an_empty_url_env_variable_with_an_empty_environment_value_says_so(
        self, tmp_path, monkeypatch, validate_with,
    ):
        (tmp_path / "datasources.yaml").write_text(
            "default: main\ndatasources:\n  main:\n    url_env: DB_URL_MAIN\n", encoding="utf-8",
        )
        monkeypatch.setenv("DB_URL_MAIN", "")
        with pytest.raises(ValueError, match="DB_URL_MAIN is not configured") as excinfo:
            validate_with()
        assert "the process environment holds an empty or blank value for DB_URL_MAIN" in str(excinfo.value)

    def test_an_empty_db_connection_url_says_why(self, tmp_path, monkeypatch, validate_with):
        monkeypatch.setenv("DB_CONNECTION_URL", "")
        monkeypatch.delenv("DB_PASSWORD", raising=False)
        with pytest.raises(ValueError, match="DB_CONNECTION_URL is not configured") as excinfo:
            validate_with(db_connection_url="")
        assert "the process environment holds an empty or blank value for DB_CONNECTION_URL" in str(excinfo.value)

    def test_an_unset_db_connection_url_that_is_the_factory_default_says_why(
        self, tmp_path, monkeypatch, validate_with,
    ):
        monkeypatch.delenv("DB_CONNECTION_URL", raising=False)
        monkeypatch.delenv("DB_PASSWORD", raising=False)
        with pytest.raises(ValueError, match="factory-default placeholder") as excinfo:
            validate_with(db_connection_url="mssql+pyodbc://username@server/db")
        assert f"\n  no .env file was found in {tmp_path} " in str(excinfo.value)

    def test_a_placeholder_value_is_not_blamed_on_a_missing_dotenv(self, tmp_path, monkeypatch, validate_with):
        (tmp_path / "datasources.yaml").write_text(
            "default: main\ndatasources:\n  main:\n    url_env: DB_URL_MAIN\n", encoding="utf-8",
        )
        monkeypatch.setenv("DB_URL_MAIN", "change_me")
        with pytest.raises(ValueError, match="DB_URL_MAIN is not configured") as excinfo:
            validate_with()
        assert "\n" not in str(excinfo.value)

    def test_check_warehouse_url_raises_the_unset_error_only_for_a_variable_label(self):
        from config import _check_warehouse_url
        from database.datasources import UnsetVariableError

        with pytest.raises(UnsetVariableError) as excinfo:
            _check_warehouse_url("DB_URL_X", "", "tsql", set())
        assert str(excinfo.value) == "DB_URL_X is not configured" and excinfo.value.variable == "DB_URL_X"
        with pytest.raises(ValueError, match="not configured") as plain:
            _check_warehouse_url("data source 'x'", "", "tsql", set())
        assert type(plain.value) is ValueError
