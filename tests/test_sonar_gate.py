# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for scripts/sonar_gate.py.

The script decides whether a commit passes the quality gate, and a wrong
"passed" is the failure that matters, so what is pinned here is everything
between the server's answers and the exit status: how the answers are read,
what is reported, and that the server is removed whatever happens.

Docker is not started here. The commands are checked as lists; the part
that runs them is exercised by the ``sonar`` job itself, on every run.
The export runs against a throwaway git repository and the API client
against a real HTTP server on the loopback address, since what matters
there is what ``git archive`` and ``urllib`` actually do.
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

# Make the project root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import sonar_gate
from scripts.sonar_gate import (
    Condition,
    Finding,
    GateError,
    GateReport,
    SonarApi,
    analyse,
    check_gate,
    export_tree,
    main,
    parse_findings,
    parse_gate,
    parse_key_values,
    parse_new_lines,
    read_gate,
    render_markdown,
    render_text,
    scanner_command,
    server_command,
    use_profile,
    wait_for,
)

_PASSED = GateReport(
    status="OK",
    conditions=(Condition("new_coverage", "OK", "LT", "80", "92.9"),),
    findings=(),
    new_lines=373,
)
_FAILED = GateReport(
    status="ERROR",
    conditions=(
        Condition("new_violations", "ERROR", "GT", "0", "2"),
        Condition("new_coverage", "OK", "LT", "80", "92.9"),
    ),
    findings=(
        Finding("python:S3776", "CODE_SMELL", "api/runner.py", 41, "Too | complex."),
        Finding("python:S2068", "SECURITY_HOTSPOT", "app.py", 7, "Review this."),
    ),
    new_lines=120,
)


class TestParsing:
    def test_key_values_skip_comments_and_keep_later_equals(self) -> None:
        text = "# written by the scanner\nprojectKey=demo\n\nceTaskUrl=http://s/api/ce/task?id=A1\n"
        assert parse_key_values(text) == {
            "projectKey": "demo",
            "ceTaskUrl": "http://s/api/ce/task?id=A1",
        }

    def test_gate_reads_status_and_conditions_in_order(self) -> None:
        payload = {
            "projectStatus": {
                "status": "ERROR",
                "conditions": [
                    {"status": "OK", "metricKey": "new_coverage", "comparator": "LT",
                     "errorThreshold": "80", "actualValue": "92.9"},
                    {"status": "ERROR", "metricKey": "new_violations", "comparator": "GT",
                     "errorThreshold": "0", "actualValue": "2"},
                ],
            }
        }  # fmt: skip
        status, conditions = parse_gate(payload)
        assert status == "ERROR"
        assert conditions == (
            Condition("new_coverage", "OK", "LT", "80", "92.9"),
            Condition("new_violations", "ERROR", "GT", "0", "2"),
        )

    def test_gate_without_conditions_is_read(self) -> None:
        assert parse_gate({"projectStatus": {"status": "OK"}}) == ("OK", ())

    @pytest.mark.parametrize("payload", [{}, {"projectStatus": {}}, {"projectStatus": "OK"}])
    def test_gate_answer_without_a_status_is_an_error(self, payload: dict[str, Any]) -> None:
        with pytest.raises(GateError, match="unexpected quality gate answer"):
            parse_gate(payload)

    def test_findings_put_issues_before_hotspots_sorted_by_place(self) -> None:
        issues = {
            "issues": [
                {"rule": "python:S1192", "type": "CODE_SMELL", "component": "demo:b.py",
                 "line": 9, "message": "second"},
                {"rule": "python:S3776", "type": "CODE_SMELL", "component": "demo:a.py",
                 "line": 30, "message": "first"},
                {"rule": "python:S1192", "type": "CODE_SMELL", "component": "demo:b.py",
                 "message": "whole file"},
            ]
        }  # fmt: skip
        hotspots = {
            "hotspots": [
                {"ruleKey": "python:S2068", "component": "demo:a.py", "line": 1, "message": "h"}
            ]
        }
        found = parse_findings(issues, hotspots)
        assert [(f.path, f.line, f.kind) for f in found] == [
            ("a.py", 30, "CODE_SMELL"),
            ("b.py", 0, "CODE_SMELL"),
            ("b.py", 9, "CODE_SMELL"),
            ("a.py", 1, "SECURITY_HOTSPOT"),
        ]

    def test_findings_of_empty_answers_are_empty(self) -> None:
        assert parse_findings({}, {}) == ()

    def test_new_lines_default_to_zero(self) -> None:
        assert parse_new_lines({}) == 0
        assert parse_new_lines({"component": {"measures": [{"metric": "ncloc", "value": "9"}]}}) == 0


class TestReporting:
    def test_only_ok_passes(self) -> None:
        assert _PASSED.passed
        assert not _FAILED.passed
        assert not GateReport("NONE", (), (), 0).passed

    def test_text_names_the_verdict_each_condition_and_each_finding(self) -> None:
        assert render_text(_FAILED).splitlines() == [
            "Quality gate FAILED (120 new lines)",
            "  [ERROR] New issues: 2 (at most 0)",
            "  [OK] Coverage on new code (%): 92.9 (at least 80)",
            "  api/runner.py:41  python:S3776  Too | complex.",
            "  app.py:7  python:S2068  Review this.",
        ]

    def test_text_for_a_pass_has_no_finding_lines(self) -> None:
        assert render_text(_PASSED).splitlines() == [
            "Quality gate PASSED (373 new lines)",
            "  [OK] Coverage on new code (%): 92.9 (at least 80)",
        ]

    def test_an_unknown_metric_is_shown_by_its_key(self) -> None:
        report = GateReport("ERROR", (Condition("new_bugs", "ERROR", "GT", "0", "1"),), (), 5)
        assert "  [ERROR] new_bugs: 1 (at most 0)" in render_text(report)

    def test_markdown_escapes_pipes_in_messages(self) -> None:
        text = render_markdown(_FAILED)
        assert text.startswith("### Quality gate FAILED (120 new lines)\n")
        assert "| New issues | 2 | at most 0 | ERROR |" in text
        assert "| `api/runner.py:41` | `python:S3776` | Too \\| complex. |" in text
        assert text.endswith("\n")

    def test_markdown_for_a_pass_has_no_findings_table(self) -> None:
        assert "| File | Rule | Message |" not in render_markdown(_PASSED)


class TestCommands:
    def test_images_are_pinned_to_a_version(self) -> None:
        for image in (sonar_gate.SERVER_IMAGE, sonar_gate.SCANNER_IMAGE):
            tag = image.rpartition(":")[2]
            assert tag[0].isdigit(), image
            assert tag.count(".") >= 2, image

    def test_the_server_is_published_on_the_loopback_address_only(self) -> None:
        command = server_command("gate", "net")
        assert command[command.index("--publish") + 1] == "127.0.0.1::9000"
        assert command[command.index("--name") + 1] == "gate"
        assert command[command.index("--network") + 1] == "net"
        assert command[-1] == sonar_gate.SERVER_IMAGE

    def test_the_scanner_command_never_carries_the_token(self) -> None:
        command = scanner_command(
            Path("/work/head"), "head", server="gate", network="net", user="1001:118"
        )
        assert "SONAR_TOKEN" in command  # the name only: Docker copies the value
        assert not any(part.startswith("SONAR_TOKEN=") for part in command)
        assert "SONAR_HOST_URL=http://gate:9000" in command
        assert command[command.index("--user") + 1] == "1001:118"

    def test_the_scanner_ignores_commit_data_and_reports_into_the_tree(self) -> None:
        command = scanner_command(Path("/work/base"), "base", server="s", network="n", user=None)
        assert "--user" not in command
        assert "-Dsonar.scm.disabled=true" in command
        assert "-Dsonar.projectVersion=base" in command
        assert "-Dsonar.working.directory=/usr/src/.scannerwork" in command
        assert command[command.index("--volume") + 1].endswith(":/usr/src")


class TestRun:
    def test_a_missing_program_is_reported_by_name(self) -> None:
        with pytest.raises(GateError, match="no-such-program-xyz is not installed"):
            sonar_gate._run(["no-such-program-xyz"], what="run it")

    def test_a_failure_carries_the_end_of_the_output(self) -> None:
        code = "print('first'); print('the reason'); raise SystemExit(4)"
        with pytest.raises(GateError) as caught:
            sonar_gate._run([sys.executable, "-c", code], what="do the thing")
        message = str(caught.value)
        assert message.startswith("could not do the thing (exit status 4):")
        assert message.endswith("the reason")

    def test_a_timeout_is_reported(self) -> None:
        with pytest.raises(GateError, match="no answer after 0 s"):
            sonar_gate._run(
                [sys.executable, "-c", "import time; time.sleep(30)"], what="wait", timeout=0.2
            )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with two commits, tagged ``one`` and ``two``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("cache/\n", encoding="utf-8")
    (root / "sonar-project.properties").write_text("sonar.projectKey=demo\n", encoding="utf-8")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    _git(root, "tag", "one")
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "b.py").write_text("y = 1\n", encoding="utf-8")
    (root / "sonar-project.properties").write_text(
        "sonar.projectKey=demo\nsonar.exclusions=pkg/**\n", encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "two")
    _git(root, "tag", "two")
    # Neither of these is in a commit, so neither may reach an analysis.
    (root / "cache").mkdir()
    (root / "cache" / "junk.py").write_text("ignored = True\n", encoding="utf-8")
    (root / "untracked.py").write_text("untracked = True\n", encoding="utf-8")
    return root


class TestExportTree:
    def test_writes_the_files_of_that_commit_only(self, repo: Path, tmp_path: Path) -> None:
        destination = tmp_path / "out"
        destination.mkdir()
        export_tree(repo, "one", destination)
        assert sorted(p.name for p in destination.iterdir()) == [
            ".gitignore",
            "a.py",
            "sonar-project.properties",
        ]
        assert (destination / "a.py").read_text(encoding="utf-8") == "x = 1\n"
        assert not (tmp_path / "out.tar").exists()

    def test_leaves_out_ignored_and_untracked_files(self, repo: Path, tmp_path: Path) -> None:
        destination = tmp_path / "out"
        destination.mkdir()
        export_tree(repo, "two", destination)
        assert (destination / "pkg" / "b.py").is_file()
        assert not (destination / "cache").exists()
        assert not (destination / "untracked.py").exists()
        assert not (destination / ".git").exists()

    def test_an_unknown_revision_is_an_error(self, repo: Path, tmp_path: Path) -> None:
        destination = tmp_path / "out"
        destination.mkdir()
        with pytest.raises(GateError, match="could not export no-such-rev"):
            export_tree(repo, "no-such-rev", destination)


class TestWaitFor:
    def test_returns_the_first_value(self) -> None:
        answers = iter([None, None, "SUCCESS"])
        pauses: list[float] = []
        value = wait_for(
            lambda: next(answers), what="it", timeout_s=60, interval_s=2, sleep=pauses.append
        )
        assert value == "SUCCESS"
        assert pauses == [2, 2]

    def test_gives_up_after_the_timeout(self) -> None:
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with pytest.raises(GateError, match="start-up did not finish within 10 s"):
            wait_for(
                lambda: None,
                what="start-up",
                timeout_s=10,
                interval_s=4,
                clock=lambda: now[0],
                sleep=sleep,
            )
        assert now[0] == 12.0


class _Handler(BaseHTTPRequestHandler):
    """Answers like the server would, and records what it was asked."""

    seen: list[dict[str, Any]] = []

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        type(self).seen.append(
            {
                "method": self.command,
                "path": self.path,
                "body": self.rfile.read(length).decode("utf-8"),
                "authorization": self.headers.get("Authorization"),
            }
        )
        status, body = 200, b'{"ok": true}'
        if self.path.startswith("/api/denied"):
            status, body = 401, b"no"
        elif self.path.startswith("/api/empty"):
            body = b""
        elif self.path.startswith("/api/text"):
            body = b"<html>"
        elif self.path.startswith("/api/list"):
            body = b"[1]"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer  # noqa: N815  # the names http.server calls

    def log_message(self, *args: Any) -> None:  # silence the default stderr log
        return


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[SonarApi]:
    """A client of a real HTTP server, with a proxy configured that is dead."""
    _Handler.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    # A CI runner or a corporate laptop often has these set; the loopback
    # server must be reached directly all the same.
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield SonarApi(f"http://127.0.0.1:{server.server_address[1]}/", "admin:admin")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestSonarApi:
    def test_get_sends_parameters_in_the_query_with_basic_auth(self, api: SonarApi) -> None:
        assert api.call("api/ce/task", id="A 1") == {"ok": True}
        assert _Handler.seen == [
            {
                "method": "GET",
                "path": "/api/ce/task?id=A+1",
                "body": "",
                "authorization": "Basic YWRtaW46YWRtaW4=",
            }
        ]

    def test_post_sends_parameters_as_a_form(self, api: SonarApi) -> None:
        api.call("api/user_tokens/generate", post=True, name="gate", type="GLOBAL_ANALYSIS_TOKEN")
        assert _Handler.seen[0]["method"] == "POST"
        assert _Handler.seen[0]["path"] == "/api/user_tokens/generate"
        assert _Handler.seen[0]["body"] == "name=gate&type=GLOBAL_ANALYSIS_TOKEN"

    def test_an_empty_body_is_an_empty_answer(self, api: SonarApi) -> None:
        assert api.call("api/empty", post=True) == {}

    def test_an_error_status_is_an_error_with_the_body(self, api: SonarApi) -> None:
        with pytest.raises(GateError, match="api/denied answered 401: no"):
            api.call("api/denied")

    @pytest.mark.parametrize(
        ("path", "message"),
        [("api/text", "did not answer with JSON"), ("api/list", "did not answer with a JSON object")],
    )
    def test_anything_but_a_json_object_is_an_error(
        self, api: SonarApi, path: str, message: str
    ) -> None:
        with pytest.raises(GateError, match=message):
            api.call(path)

    def test_an_unreachable_server_is_an_error(self) -> None:
        with pytest.raises(GateError, match="api/system/status could not be reached"):
            SonarApi("http://127.0.0.1:9", "admin:admin").call("api/system/status")


class _FakeApi:
    """Answers from a table keyed by endpoint, and records every call."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, bool, dict[str, str]]] = []

    def call(self, path: str, *, post: bool = False, **params: str) -> dict[str, Any]:
        self.calls.append((path, post, params))
        answer = self.answers.get(path, {})
        if isinstance(answer, list):
            return answer.pop(0)
        return answer


class TestUseProfile:
    _PROFILES = {
        "api/qualityprofiles/search": {
            "profiles": [
                {"language": "py", "name": "Sonar way core"},
                {"language": "py", "name": "Sonar way extended"},
                {"language": "js", "name": "Sonar way extended"},
                {"language": "json", "name": "Sonar way comprehensive"},
            ]
        }
    }

    def test_sets_the_default_for_each_language_that_has_the_profile(self) -> None:
        fake = _FakeApi(dict(self._PROFILES))
        assert use_profile(fake, "Sonar way extended") == ["js", "py"]  # type: ignore[arg-type]
        assert fake.calls[1:] == [
            ("api/qualityprofiles/set_default", True,
             {"qualityProfile": "Sonar way extended", "language": "js"}),
            ("api/qualityprofiles/set_default", True,
             {"qualityProfile": "Sonar way extended", "language": "py"}),
        ]  # fmt: skip

    def test_an_unknown_profile_is_an_error_that_lists_the_names(self) -> None:
        fake = _FakeApi(dict(self._PROFILES))
        with pytest.raises(GateError, match="no quality profile is named 'Sonar wy'") as caught:
            use_profile(fake, "Sonar wy")  # type: ignore[arg-type]
        assert "Sonar way core" in str(caught.value)
        assert len(fake.calls) == 1


class TestReadGate:
    def test_assembles_the_report_from_new_code_only(self) -> None:
        fake = _FakeApi(
            {
                "api/qualitygates/project_status": {
                    "projectStatus": {
                        "status": "ERROR",
                        "conditions": [
                            {"status": "ERROR", "metricKey": "new_violations",
                             "comparator": "GT", "errorThreshold": "0", "actualValue": "1"}
                        ],
                    }
                },
                "api/issues/search": {
                    "issues": [
                        {"rule": "python:S3776", "type": "CODE_SMELL",
                         "component": "demo:a.py", "line": 3, "message": "m"}
                    ]
                },
                "api/hotspots/search": {"hotspots": []},
                "api/measures/component": {
                    "component": {"measures": [{"metric": "new_lines", "period": {"value": "44"}}]}
                },
            }
        )  # fmt: skip
        report = read_gate(fake, "demo")  # type: ignore[arg-type]
        assert report == GateReport(
            status="ERROR",
            conditions=(Condition("new_violations", "ERROR", "GT", "0", "1"),),
            findings=(Finding("python:S3776", "CODE_SMELL", "a.py", 3, "m"),),
            new_lines=44,
        )
        asked = {path: params for path, _, params in fake.calls}
        assert asked["api/issues/search"]["inNewCodePeriod"] == "true"
        assert asked["api/hotspots/search"]["inNewCodePeriod"] == "true"
        assert asked["api/qualitygates/project_status"] == {"projectKey": "demo"}


class TestAnalyse:
    @staticmethod
    def _scanner_that_reports(monkeypatch: pytest.MonkeyPatch, tree: Path, text: str | None) -> list:
        ran: list[dict[str, Any]] = []

        def fake_run(command: list[str], *, what: str, **kwargs: Any) -> str:
            ran.append({"command": command, "what": what, **kwargs})
            if text is not None:
                (tree / ".scannerwork").mkdir()
                (tree / ".scannerwork" / "report-task.txt").write_text(text, encoding="utf-8")
            return ""

        monkeypatch.setattr(sonar_gate, "_run", fake_run)
        monkeypatch.setattr(
            sonar_gate, "wait_for", functools.partial(wait_for, sleep=lambda seconds: None)
        )
        return ran

    def test_waits_for_the_server_and_returns_the_project_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ran = self._scanner_that_reports(monkeypatch, tmp_path, "projectKey=demo\nceTaskId=T1\n")
        fake = _FakeApi(
            {
                "api/ce/task": [
                    {"task": {"status": "PENDING"}},
                    {"task": {"status": "IN_PROGRESS"}},
                    {"task": {"status": "SUCCESS"}},
                ]
            }
        )
        key = analyse(fake, tmp_path, "head", token="secret", server="gate", network="net")  # type: ignore[arg-type]
        assert key == "demo"
        assert [params for _, _, params in fake.calls] == [{"id": "T1"}] * 3
        # The token travels in the environment, never on the command line.
        assert ran[0]["env"]["SONAR_TOKEN"] == "secret"
        assert "secret" not in " ".join(ran[0]["command"])

    def test_a_report_the_server_cannot_process_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._scanner_that_reports(monkeypatch, tmp_path, "projectKey=demo\nceTaskId=T1\n")
        fake = _FakeApi({"api/ce/task": {"task": {"status": "FAILED"}}})
        with pytest.raises(GateError, match="could not process the base analysis: FAILED"):
            analyse(fake, tmp_path, "base", token="t", server="gate", network="net")  # type: ignore[arg-type]

    @pytest.mark.parametrize("text", [None, "projectKey=demo\n"])
    def test_a_missing_or_incomplete_task_report_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str | None
    ) -> None:
        self._scanner_that_reports(monkeypatch, tmp_path, text)
        with pytest.raises(GateError, match="the scanner left no usable"):
            analyse(_FakeApi({}), tmp_path, "head", token="t", server="gate", network="net")  # type: ignore[arg-type]


class TestCheckGate:
    @staticmethod
    def _without_docker(monkeypatch: pytest.MonkeyPatch, *, fail_on: str | None = None) -> dict:
        """Replace everything that needs Docker; record what was analysed."""
        seen: dict[str, Any] = {"docker": [], "trees": {}}
        real_run = sonar_gate._run

        def fake_run(command: list[str], *, what: str, **kwargs: Any) -> str:
            if command[0] != "docker":
                return real_run(command, what=what, **kwargs)
            seen["docker"].append(command[1:4])
            return ""

        def fake_analyse(api: Any, tree: Path, version: str, **kwargs: Any) -> str:
            if version == fail_on:
                raise GateError(f"{version} broke")
            seen["trees"][version] = {
                p.relative_to(tree).as_posix(): p.read_text(encoding="utf-8")
                for p in sorted(tree.rglob("*"))
                if p.is_file()
            }
            seen["scratch"] = tree.parent
            return "demo"

        monkeypatch.setattr(sonar_gate, "_run", fake_run)
        monkeypatch.setattr(
            sonar_gate, "start_server", lambda name, network: _FakeApi(
                {"api/user_tokens/generate": {"token": "t"}}
            )
        )  # fmt: skip
        monkeypatch.setattr(sonar_gate, "use_profile", lambda api, profile: ["py"])
        monkeypatch.setattr(sonar_gate, "analyse", fake_analyse)
        monkeypatch.setattr(sonar_gate, "read_gate", lambda api, key: _PASSED)
        return seen

    def test_both_commits_are_analysed_with_the_heads_settings(
        self, monkeypatch: pytest.MonkeyPatch, repo: Path, tmp_path: Path
    ) -> None:
        seen = self._without_docker(monkeypatch)
        coverage = tmp_path / "report.xml"
        coverage.write_text("<coverage/>", encoding="utf-8")
        steps: list[str] = []
        report = check_gate(
            repo, base="one", head="two", coverage=coverage, profile="p", progress=steps.append
        )
        assert report is _PASSED
        head_settings = "sonar.projectKey=demo\nsonar.exclusions=pkg/**\n"
        assert seen["trees"]["base"] == {
            ".gitignore": "cache/\n",
            "a.py": "x = 1\n",
            "sonar-project.properties": head_settings,
        }
        assert seen["trees"]["head"] == {
            ".gitignore": "cache/\n",
            "a.py": "x = 2\n",
            "coverage.xml": "<coverage/>",
            "pkg/b.py": "y = 1\n",
            "sonar-project.properties": head_settings,
        }
        assert [step.split()[0] for step in steps] == ["Starting", "Profile", "Analysing", "Analysing"]

    def test_the_server_the_network_and_the_exports_are_removed(
        self, monkeypatch: pytest.MonkeyPatch, repo: Path
    ) -> None:
        seen = self._without_docker(monkeypatch)
        check_gate(repo, base="one", head="two", coverage=None, profile="p", progress=lambda _: None)
        assert [command[:2] for command in seen["docker"]] == [
            ["network", "create"],
            ["rm", "--force"],
            ["network", "rm"],
        ]
        assert not seen["scratch"].exists()
        assert "coverage.xml" not in seen["trees"]["head"]

    def test_they_are_removed_when_an_analysis_fails_too(
        self, monkeypatch: pytest.MonkeyPatch, repo: Path
    ) -> None:
        seen = self._without_docker(monkeypatch, fail_on="head")
        with pytest.raises(GateError, match="head broke"):
            check_gate(repo, base="one", head="two", coverage=None, profile="p", progress=lambda _: None)
        assert [command[:2] for command in seen["docker"]][-2:] == [["rm", "--force"], ["network", "rm"]]
        assert not seen["scratch"].exists()

    def test_a_head_without_settings_is_an_error_before_docker_is_touched(
        self, monkeypatch: pytest.MonkeyPatch, repo: Path
    ) -> None:
        seen = self._without_docker(monkeypatch)
        _git(repo, "rm", "-q", "sonar-project.properties")
        _git(repo, "commit", "-q", "-m", "three")
        with pytest.raises(GateError, match="HEAD has no sonar-project.properties"):
            check_gate(repo, base="one", head="HEAD", coverage=None, profile="p")
        assert seen["docker"] == []


class TestMain:
    @staticmethod
    def _gate_returns(monkeypatch: pytest.MonkeyPatch, outcome: GateReport | GateError) -> dict:
        asked: dict[str, Any] = {}

        def fake_check_gate(repo: Path, **kwargs: Any) -> GateReport:
            asked.update(kwargs, repo=repo)
            if isinstance(outcome, GateError):
                raise outcome
            return outcome

        monkeypatch.setattr(sonar_gate, "check_gate", fake_check_gate)
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        return asked

    def test_a_pass_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        asked = self._gate_returns(monkeypatch, _PASSED)
        assert main(["--base", "v1"]) == 0
        assert capsys.readouterr().out.startswith("Quality gate PASSED (373 new lines)\n")
        assert asked == {
            "base": "v1",
            "head": "HEAD",
            "coverage": None,
            "profile": sonar_gate.DEFAULT_PROFILE,
            "repo": Path("."),
        }

    def test_a_failed_gate_exits_one_and_appends_the_summary(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._gate_returns(monkeypatch, _FAILED)
        summary = tmp_path / "summary.md"
        summary.write_text("earlier step\n", encoding="utf-8")
        assert main(["--base", "v1", "--head", "abc", "--summary", str(summary)]) == 1
        text = summary.read_text(encoding="utf-8")
        assert text.startswith("earlier step\n### Quality gate FAILED (120 new lines)\n")

    def test_the_summary_defaults_to_the_job_summary_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._gate_returns(monkeypatch, _PASSED)
        summary = tmp_path / "job-summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        assert main(["--base", "v1"]) == 0
        assert summary.read_text(encoding="utf-8").startswith("### Quality gate PASSED")

    def test_a_gate_that_could_not_be_evaluated_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._gate_returns(monkeypatch, GateError("docker is not installed\nmore detail"))
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        assert main(["--base", "v1"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "::error title=SonarQube gate not evaluated::docker is not installed\n" in captured.err
        assert "sonar_gate: error: docker is not installed\nmore detail\n" in captured.err

    def test_a_missing_coverage_report_exits_two_without_starting_anything(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        asked = self._gate_returns(monkeypatch, _PASSED)
        assert main(["--base", "v1", "--coverage", str(tmp_path / "absent.xml")]) == 2
        assert asked == {}
        assert "does not exist" in capsys.readouterr().err

    def test_the_answers_are_plain_json(self) -> None:
        # The parsers take what json.loads returns, with no further shaping.
        payload = json.loads('{"projectStatus": {"status": "OK", "conditions": []}}')
        assert parse_gate(payload) == ("OK", ())
