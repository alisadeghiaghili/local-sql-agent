# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Check the SonarQube quality gate on a throwaway server, with no account.

Usage::

    python scripts/sonar_gate.py --base REV [--head REV] [--coverage PATH]
        [--profile NAME] [--summary PATH] [--repo .]

The ``sonar`` job in ``.github/workflows/ci.yml`` calls this when the run
has no ``SONAR_TOKEN``, and it runs the same way on a developer's machine.
It needs Docker and git, and nothing else: apart from ``core.console``,
which itself imports nothing, only the standard library is imported, so no
virtual environment is required.

What it does
------------
A quality gate judges *new code*, so one analysis is not enough: a server
that has seen a project once has nothing to compare it with and passes it.
The script therefore analyses two commits on a SonarQube Community Build
server that it starts and throws away:

1. :func:`export_tree`    -- write the files of ``--base`` and of ``--head``
   to two clean directories (tracked files only).
2. :func:`start_server`   -- start the server and wait until it answers.
3. :func:`analyse`        -- analyse the base as version ``base``, then the
   head as version ``head``. The server's default new-code definition is
   "previous version", so everything that differs in the head is new code.
4. :func:`read_gate`      -- read the gate's verdict, its conditions and the
   issues raised in new code.
5. :func:`render_text` / :func:`render_markdown` -- report them, because
   nobody can open the server once it is gone.

Both analyses run with ``sonar.scm.disabled=true``. Without commit data the
server dates each line by comparing the two analyses, so "new" means exactly
"differs from the base", whatever the commit dates say. With commit data it
would compare commit dates with the date of the base analysis instead, and
a branch whose commits are older than the base's tip would have no new code.

What it is not
--------------
The server is real and so are its analyzers and its built-in "Sonar way"
gate, but it starts empty on every run. It therefore knows no history, no
issue marked "accepted" or "false positive" in a user interface, and no
reviewed security hotspot. A hotspot in new code fails the gate unless the
line carries ``# NOSONAR`` with its reason, which keeps the reason in the
repository.

Exit status: ``0`` the gate passed, ``1`` it failed, ``2`` it could not be
evaluated (:class:`GateError`).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# Make the project root importable when the script is run by path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.console import use_utf8_console  # noqa: E402

#: Pinned so that a new analyzer release cannot change the verdict of an
#: unchanged commit. Raise both together and read the run that follows.
SERVER_IMAGE = "sonarqube:26.11.0.134175-community"
SCANNER_IMAGE = "sonarsource/sonar-scanner-cli:12.4.0.4321_8.4.0"

#: The built-in profile made the default for every language that has one.
#: "Sonar way core" is the server's own default; "extended" adds, among
#: others, cognitive complexity (S3776) and repeated literals (S1192).
DEFAULT_PROFILE = "Sonar way extended"

#: The account a new SonarQube server is created with. The server lives for
#: one run and listens on the loopback address only.
_BOOTSTRAP_ACCOUNT = "admin:admin"

_SETTINGS_FILE = "sonar-project.properties"
_COVERAGE_FILE = "coverage.xml"
_TASK_REPORT = Path(".scannerwork") / "report-task.txt"
_SERVER_PORT = "9000"
_SERVER_START_TIMEOUT_S = 300.0
_TASK_TIMEOUT_S = 600.0
_SCAN_TIMEOUT_S = 1800.0
_PAGE_SIZE = 500
_GATE_OK = "OK"

#: Shown for a gate metric in place of its key.
_METRIC_LABELS: Mapping[str, str] = {
    "new_violations": "New issues",
    "new_coverage": "Coverage on new code (%)",
    "new_duplicated_lines_density": "Duplicated lines on new code (%)",
    "new_security_hotspots_reviewed": "Security hotspots reviewed (%)",
}
_COMPARATORS: Mapping[str, str] = {"GT": "at most", "LT": "at least"}


class GateError(Exception):
    """The gate could not be evaluated; the message says why."""


@dataclass(frozen=True)
class Condition:
    """One condition of the quality gate, as the server evaluated it.

    Attributes
    ----------
    metric : str
        The metric key, such as ``new_coverage``.
    status : str
        ``OK`` or ``ERROR``.
    comparator : str
        ``GT`` (fails above the threshold) or ``LT`` (fails below it).
    threshold : str
        The value the gate requires.
    actual : str
        The value measured, or ``""`` when the server reported none.
    """

    metric: str
    status: str
    comparator: str
    threshold: str
    actual: str


@dataclass(frozen=True)
class Finding:
    """An issue or a security hotspot raised in new code.

    Attributes
    ----------
    rule : str
        The rule key, such as ``python:S3776``.
    kind : str
        The issue type, or ``SECURITY_HOTSPOT``.
    path : str
        The file, relative to the repository root.
    line : int
        The line, or ``0`` for a finding on the whole file.
    message : str
        The server's message.
    """

    rule: str
    kind: str
    path: str
    line: int
    message: str


@dataclass(frozen=True)
class GateReport:
    """The verdict of the quality gate on the head commit.

    Attributes
    ----------
    status : str
        The gate status; ``OK`` means it passed.
    conditions : tuple of Condition
        The conditions the server evaluated. Conditions on coverage and
        duplication are left out by the server below 20 new lines.
    findings : tuple of Finding
        Issues and hotspots in new code.
    new_lines : int
        The number of new lines the server counted.
    """

    status: str
    conditions: tuple[Condition, ...]
    findings: tuple[Finding, ...]
    new_lines: int

    @property
    def passed(self) -> bool:
        """Whether the gate passed.

        Returns
        -------
        bool
            ``True`` when the status is ``OK``.

        Examples
        --------
        >>> GateReport("OK", (), (), 0).passed
        True
        >>> GateReport("ERROR", (), (), 0).passed
        False
        """
        return self.status == _GATE_OK


# -- Parsing -------------------------------------------------------------------


def parse_key_values(text: str) -> dict[str, str]:
    """Read ``key=value`` lines, as in the scanner's ``report-task.txt``.

    Parameters
    ----------
    text : str
        The file's content. Blank lines, ``#`` comments and lines without
        ``=`` are skipped. Line continuations are not joined: the keys this
        script reads are never continued.

    Returns
    -------
    dict of str to str
        Keys and values, both stripped. A later line wins.

    Examples
    --------
    >>> parse_key_values("# note\\nprojectKey = demo\\nceTaskId=AZ=1\\n\\n")
    {'projectKey': 'demo', 'ceTaskId': 'AZ=1'}
    """
    pairs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def parse_gate(payload: Mapping[str, Any]) -> tuple[str, tuple[Condition, ...]]:
    """Read the answer of ``api/qualitygates/project_status``.

    Parameters
    ----------
    payload : mapping
        The decoded JSON answer.

    Returns
    -------
    tuple of (str, tuple of Condition)
        The gate status and its conditions, in the server's order.

    Raises
    ------
    GateError
        If the answer has no ``projectStatus.status``.

    Examples
    --------
    >>> status, conditions = parse_gate({"projectStatus": {"status": "ERROR",
    ...     "conditions": [{"status": "ERROR", "metricKey": "new_violations",
    ...                     "comparator": "GT", "errorThreshold": "0",
    ...                     "actualValue": "2"}]}})
    >>> status, conditions[0].metric, conditions[0].actual
    ('ERROR', 'new_violations', '2')
    """
    project = payload.get("projectStatus")
    if not isinstance(project, Mapping) or "status" not in project:
        raise GateError(f"unexpected quality gate answer: {payload!r}")
    conditions = tuple(
        Condition(
            metric=str(item.get("metricKey", "")),
            status=str(item.get("status", "")),
            comparator=str(item.get("comparator", "")),
            threshold=str(item.get("errorThreshold", "")),
            actual=str(item.get("actualValue", "")),
        )
        for item in project.get("conditions", ())
    )
    return str(project["status"]), conditions


def _relative_path(component: str) -> str:
    """Return the file path of a component key ``<project key>:<path>``.

    Parameters
    ----------
    component : str
        The component key.

    Returns
    -------
    str
        The part after the first colon, or the key itself without one.

    Examples
    --------
    >>> _relative_path("demo:api/runner.py")
    'api/runner.py'
    >>> _relative_path("demo")
    'demo'
    """
    return component.partition(":")[2] or component


def parse_findings(
    issues: Mapping[str, Any], hotspots: Mapping[str, Any]
) -> tuple[Finding, ...]:
    """Read the answers of ``api/issues/search`` and ``api/hotspots/search``.

    Parameters
    ----------
    issues : mapping
        The decoded issues answer.
    hotspots : mapping
        The decoded hotspots answer.

    Returns
    -------
    tuple of Finding
        Issues first, then hotspots, each sorted by file and line.

    Examples
    --------
    >>> found = parse_findings(
    ...     {"issues": [{"rule": "python:S3776", "type": "CODE_SMELL",
    ...                  "component": "demo:b.py", "line": 7, "message": "m"},
    ...                 {"rule": "python:S1192", "type": "CODE_SMELL",
    ...                  "component": "demo:a.py", "message": "n"}]},
    ...     {"hotspots": [{"ruleKey": "python:S2068",
    ...                    "component": "demo:c.py", "line": 3, "message": "o"}]})
    >>> [(f.path, f.line, f.kind) for f in found]
    [('a.py', 0, 'CODE_SMELL'), ('b.py', 7, 'CODE_SMELL'), ('c.py', 3, 'SECURITY_HOTSPOT')]
    """
    raised = [
        Finding(
            rule=str(item.get("rule", "")),
            kind=str(item.get("type", "")),
            path=_relative_path(str(item.get("component", ""))),
            line=int(item.get("line", 0)),
            message=str(item.get("message", "")),
        )
        for item in issues.get("issues", ())
    ]
    to_review = [
        Finding(
            rule=str(item.get("ruleKey", "")),
            kind="SECURITY_HOTSPOT",
            path=_relative_path(str(item.get("component", ""))),
            line=int(item.get("line", 0)),
            message=str(item.get("message", "")),
        )
        for item in hotspots.get("hotspots", ())
    ]

    def by_place(finding: Finding) -> tuple[str, int, str]:
        return finding.path, finding.line, finding.rule

    return (*sorted(raised, key=by_place), *sorted(to_review, key=by_place))


def parse_new_lines(payload: Mapping[str, Any]) -> int:
    """Read ``new_lines`` from the answer of ``api/measures/component``.

    Parameters
    ----------
    payload : mapping
        The decoded JSON answer.

    Returns
    -------
    int
        The number of new lines, or ``0`` when the measure is absent.

    Examples
    --------
    >>> parse_new_lines({"component": {"measures": [
    ...     {"metric": "new_lines", "period": {"value": "373"}}]}})
    373
    >>> parse_new_lines({"component": {"measures": []}})
    0
    """
    for measure in payload.get("component", {}).get("measures", ()):
        if measure.get("metric") == "new_lines":
            return int(measure.get("period", {}).get("value", 0))
    return 0


# -- Reporting -----------------------------------------------------------------


def _requirement(condition: Condition) -> str:
    """Say what a condition requires, such as ``at least 80``.

    Parameters
    ----------
    condition : Condition
        The condition.

    Returns
    -------
    str
        The comparator in words, then the threshold.

    Examples
    --------
    >>> _requirement(Condition("new_coverage", "OK", "LT", "80", "92.9"))
    'at least 80'
    """
    words = _COMPARATORS.get(condition.comparator, condition.comparator)
    return f"{words} {condition.threshold}"


def _verdict_line(report: GateReport) -> str:
    """Say in one line whether the gate passed.

    Parameters
    ----------
    report : GateReport
        The verdict.

    Returns
    -------
    str
        ``Quality gate PASSED`` or ``FAILED``, with the new-line count.

    Examples
    --------
    >>> _verdict_line(GateReport("OK", (), (), 373))
    'Quality gate PASSED (373 new lines)'
    """
    verdict = "PASSED" if report.passed else "FAILED"
    return f"Quality gate {verdict} ({report.new_lines} new lines)"


def render_text(report: GateReport) -> str:
    """Render the verdict for a terminal or a job log.

    Parameters
    ----------
    report : GateReport
        The verdict.

    Returns
    -------
    str
        The verdict line, one line for each condition and one for each
        finding, without a trailing newline.

    Examples
    --------
    >>> print(render_text(GateReport(
    ...     "ERROR",
    ...     (Condition("new_violations", "ERROR", "GT", "0", "1"),),
    ...     (Finding("python:S3776", "CODE_SMELL", "a.py", 7, "Too complex."),),
    ...     40)))
    Quality gate FAILED (40 new lines)
      [ERROR] New issues: 1 (at most 0)
      a.py:7  python:S3776  Too complex.
    """
    lines = [_verdict_line(report)]
    for condition in report.conditions:
        label = _METRIC_LABELS.get(condition.metric, condition.metric)
        lines.append(
            f"  [{condition.status}] {label}: {condition.actual} "
            f"({_requirement(condition)})"
        )
    lines.extend(
        f"  {finding.path}:{finding.line}  {finding.rule}  {finding.message}"
        for finding in report.findings
    )
    return "\n".join(lines)


def _cell(text: str) -> str:
    """Make *text* safe inside a Markdown table cell.

    Parameters
    ----------
    text : str
        The cell's content.

    Returns
    -------
    str
        *text* on one line, with ``|`` escaped.

    Examples
    --------
    >>> _cell("a | b\\nc")
    'a \\\\| b c'
    """
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: GateReport) -> str:
    """Render the verdict as Markdown, for the job summary.

    Parameters
    ----------
    report : GateReport
        The verdict.

    Returns
    -------
    str
        A heading, a table of conditions and, when there are findings, a
        table of them, ending with a newline.

    Examples
    --------
    >>> print(render_markdown(GateReport(
    ...     "OK", (Condition("new_coverage", "OK", "LT", "80", "92.9"),), (), 373)),
    ...     end="")
    ### Quality gate PASSED (373 new lines)
    <BLANKLINE>
    | Condition | Measured | Required | Status |
    |---|---|---|---|
    | Coverage on new code (%) | 92.9 | at least 80 | OK |
    """
    lines = [f"### {_verdict_line(report)}", ""]
    if report.conditions:
        lines += ["| Condition | Measured | Required | Status |", "|---|---|---|---|"]
        lines += [
            f"| {_METRIC_LABELS.get(c.metric, c.metric)} | {c.actual} "
            f"| {_requirement(c)} | {c.status} |"
            for c in report.conditions
        ]
    if report.findings:
        lines += ["", "| File | Rule | Message |", "|---|---|---|"]
        lines += [
            f"| `{f.path}:{f.line}` | `{f.rule}` | {_cell(f.message)} |"
            for f in report.findings
        ]
    return "\n".join(lines) + "\n"


# -- Commands ------------------------------------------------------------------


def server_command(name: str, network: str) -> list[str]:
    """Build the ``docker run`` command that starts the server.

    The server's port is published on a free loopback port, so two runs on
    one machine do not collide. Its data directory is in memory: the server
    refuses to start when less than a tenth of the disk it writes to is
    free, and nothing it stores is wanted after the run.

    Parameters
    ----------
    name : str
        The container's name, which is also its host name on *network*.
    network : str
        The Docker network the scanner will join.

    Returns
    -------
    list of str
        The command.

    Examples
    --------
    >>> command = server_command("gate-1", "net-1")
    >>> command[:4], command[-1] == SERVER_IMAGE
    (['docker', 'run', '--detach', '--name'], True)
    >>> "127.0.0.1::9000" in command
    True
    """
    return [
        "docker", "run", "--detach",
        "--name", name,
        "--network", network,
        "--publish", f"127.0.0.1::{_SERVER_PORT}",
        "--tmpfs", "/opt/sonarqube/data:rw,size=2g,uid=1000,gid=0,mode=0770",
        # The production check on vm.max_map_count; a runner's kernel
        # setting is not this script's to change.
        "--env", "SONAR_ES_BOOTSTRAP_CHECKS_DISABLE=true",
        "--env", "SONAR_TELEMETRY_ENABLE=false",
        SERVER_IMAGE,
    ]  # fmt: skip


def scanner_command(
    tree: Path, version: str, *, server: str, network: str, user: str | None
) -> list[str]:
    """Build the ``docker run`` command that analyses one directory.

    The token is not in the command: Docker copies ``SONAR_TOKEN`` from the
    environment of the process that runs it.

    Parameters
    ----------
    tree : pathlib.Path
        The directory to analyse, mounted as the scanner's working directory.
    version : str
        The project version this analysis is recorded under.
    server : str
        The server container's name.
    network : str
        The Docker network both containers are on.
    user : str or None
        ``uid:gid`` to run the scanner as, so the files it writes into
        *tree* belong to the caller; ``None`` to use the image's own user.

    Returns
    -------
    list of str
        The command.

    Examples
    --------
    >>> command = scanner_command(Path("/work/head"), "head", server="gate-1",
    ...                           network="net-1", user="1001:1001")
    >>> command[command.index("--user") + 1], command[-2:]
    ('1001:1001', ['-Dsonar.projectVersion=head', '-Dsonar.scm.disabled=true'])
    >>> "--user" in scanner_command(Path("/work/base"), "base", server="s",
    ...                             network="n", user=None)
    False
    """
    as_user = ["--user", user] if user else []
    return [
        "docker", "run", "--rm",
        "--network", network,
        *as_user,
        "--env", f"SONAR_HOST_URL=http://{server}:{_SERVER_PORT}",
        "--env", "SONAR_TOKEN",
        # Writable by any user, unlike the image's own cache directory.
        "--env", "SONAR_USER_HOME=/tmp/sonar-home",
        "--volume", f"{tree.resolve()}:/usr/src",
        SCANNER_IMAGE,
        # The image's own setting is /tmp inside the container, where the
        # task report could not be read once the container is gone.
        f"-Dsonar.working.directory=/usr/src/{_TASK_REPORT.parent}",
        f"-Dsonar.projectVersion={version}",
        "-Dsonar.scm.disabled=true",
    ]  # fmt: skip


def _run(command: Sequence[str], *, what: str, **kwargs: Any) -> str:
    """Run *command* and return what it printed, or raise with its output.

    Parameters
    ----------
    command : sequence of str
        The program and its arguments.
    what : str
        What the command was for, used in the error message.
    **kwargs
        Passed to :func:`subprocess.run` (``cwd``, ``env``, ``timeout``).

    Returns
    -------
    str
        The command's standard output and error, stripped.

    Raises
    ------
    GateError
        If the program is missing, times out or exits with a non-zero
        status. The message ends with the last lines it printed.

    Examples
    --------
    >>> _run([sys.executable, "-c", "print('ok')"], what="say ok")
    'ok'
    >>> _run([sys.executable, "-c", "raise SystemExit(3)"], what="fail")
    Traceback (most recent call last):
        ...
    scripts.sonar_gate.GateError: could not fail (exit status 3)
    """
    try:
        done = subprocess.run(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **kwargs,
        )
    except FileNotFoundError as exc:
        raise GateError(f"could not {what}: {command[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"could not {what}: no answer after {exc.timeout:.0f} s") from exc
    output = done.stdout.strip()
    if done.returncode != 0:
        tail = "\n".join(output.splitlines()[-40:])
        detail = f":\n{tail}" if tail else ""
        raise GateError(f"could not {what} (exit status {done.returncode}){detail}")
    return output


def export_tree(repo: Path, revision: str, destination: Path) -> None:
    """Write the tracked files of *revision* into *destination*.

    A clean export rather than a checkout: with ``sonar.scm.disabled`` the
    scanner no longer reads ``.gitignore``, and would analyse caches, build
    output and anything else lying in a working tree.

    Parameters
    ----------
    repo : pathlib.Path
        The repository.
    revision : str
        A commit, tag or branch.
    destination : pathlib.Path
        An existing empty directory.

    Raises
    ------
    GateError
        If *revision* is not a commit of *repo* or cannot be written out.
    """
    archive = destination.with_suffix(".tar")
    _run(
        ["git", "-C", str(repo), "archive", "--format=tar", "-o", str(archive), revision],
        what=f"export {revision}",
    )
    try:
        _run(["tar", "-xf", str(archive), "-C", str(destination)], what=f"unpack {revision}")
    finally:
        archive.unlink(missing_ok=True)


# -- The server ----------------------------------------------------------------


class SonarApi:
    """The few calls this script makes to the server's web API.

    Parameters
    ----------
    base_url : str
        Where the server listens, such as ``http://127.0.0.1:32768``.
    account : str
        ``login:password`` for HTTP basic authentication.
    """

    def __init__(self, base_url: str, account: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._authorization = "Basic " + base64.b64encode(account.encode()).decode()
        # The server is on the loopback address: never through a proxy,
        # whatever the environment configures for outbound traffic.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path: str, *, post: bool = False, **params: str) -> dict[str, Any]:
        """Call one endpoint and decode its JSON answer.

        Parameters
        ----------
        path : str
            The endpoint, such as ``api/system/status``.
        post : bool, optional
            Send the parameters as a form with ``POST`` instead of ``GET``.
        **params : str
            The endpoint's parameters.

        Returns
        -------
        dict
            The decoded answer; ``{}`` when the server sent no body.

        Raises
        ------
        GateError
            If the server cannot be reached, answers with an error status
            or sends something that is not a JSON object.
        """
        url = f"{self._base_url}/{path}"
        query = urllib.parse.urlencode(params)
        data = query.encode() if post else None
        if query and not post:
            url = f"{url}?{query}"
        request = urllib.request.Request(  # noqa: S310  # http:// on loopback by construction
            url, data=data, headers={"Authorization": self._authorization}
        )
        try:
            with self._opener.open(request, timeout=60) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise GateError(f"{path} answered {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise GateError(f"{path} could not be reached: {exc}") from exc
        if not body.strip():
            return {}
        try:
            answer = json.loads(body)
        except ValueError as exc:
            raise GateError(f"{path} did not answer with JSON: {body[:200]!r}") from exc
        if not isinstance(answer, dict):
            raise GateError(f"{path} did not answer with a JSON object")
        return answer


def wait_for(
    probe: Callable[[], str | None],
    *,
    what: str,
    timeout_s: float,
    interval_s: float = 3.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Call *probe* until it returns a value.

    Parameters
    ----------
    probe : callable
        Returns the awaited value, or ``None`` to be asked again.
    what : str
        What is awaited, used in the error message.
    timeout_s : float
        How long to keep asking.
    interval_s : float, optional
        The pause between two calls.
    clock, sleep : callable, optional
        Replaced in tests.

    Returns
    -------
    str
        The first value *probe* returned.

    Raises
    ------
    GateError
        If *probe* returned nothing within *timeout_s*.

    Examples
    --------
    >>> answers = iter([None, None, "UP"])
    >>> wait_for(lambda: next(answers), what="the server", timeout_s=9,
    ...          sleep=lambda seconds: None)
    'UP'
    """
    deadline = clock() + timeout_s
    while True:
        value = probe()
        if value is not None:
            return value
        if clock() >= deadline:
            raise GateError(f"{what} did not finish within {timeout_s:.0f} s")
        sleep(interval_s)


def start_server(name: str, network: str) -> SonarApi:
    """Start the server and wait until it accepts analyses.

    Parameters
    ----------
    name : str
        The container's name.
    network : str
        An existing Docker network.

    Returns
    -------
    SonarApi
        The API of the running server.

    Raises
    ------
    GateError
        If the container cannot be started, stops by itself (the message
        then carries the end of its log) or is not up in time.
    """
    _run(server_command(name, network), what="start SonarQube")
    published = _run(["docker", "port", name, f"{_SERVER_PORT}/tcp"], what="find the server's port")
    address = published.splitlines()[0].strip()
    api = SonarApi(f"http://{address}", _BOOTSTRAP_ACCOUNT)  # NOSONAR(S5332) a loopback port; the image serves no TLS

    def up() -> str | None:
        running = _run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            what="inspect the server",
        )
        if running != "true":
            log = _run(["docker", "logs", "--tail", "25", name], what="read the server's log")
            raise GateError(f"SonarQube stopped while starting:\n{log}")
        try:
            status = api.call("api/system/status").get("status")
        except GateError:
            return None
        return "UP" if status == "UP" else None

    wait_for(up, what="SonarQube start-up", timeout_s=_SERVER_START_TIMEOUT_S)
    return api


def use_profile(api: SonarApi, profile: str) -> list[str]:
    """Make *profile* the default for every language that has one.

    Parameters
    ----------
    api : SonarApi
        The server.
    profile : str
        A built-in profile name, such as ``Sonar way extended``.

    Returns
    -------
    list of str
        The language keys now analysed with *profile*, sorted.

    Raises
    ------
    GateError
        If no language has a profile of that name.
    """
    profiles = api.call("api/qualityprofiles/search").get("profiles", [])
    languages = sorted(p["language"] for p in profiles if p.get("name") == profile)
    if not languages:
        names = sorted({str(p.get("name")) for p in profiles})
        raise GateError(f"no quality profile is named {profile!r}; the server has {names}")
    for language in languages:
        api.call(
            "api/qualityprofiles/set_default",
            post=True,
            qualityProfile=profile,
            language=language,
        )
    return languages


def analyse(
    api: SonarApi,
    tree: Path,
    version: str,
    *,
    token: str,
    server: str,
    network: str,
) -> str:
    """Analyse *tree* and wait until the server has processed the report.

    Parameters
    ----------
    api : SonarApi
        The server.
    tree : pathlib.Path
        The directory to analyse; it holds ``sonar-project.properties``.
    version : str
        The project version to record the analysis under.
    token : str
        An analysis token of the server.
    server : str
        The server container's name.
    network : str
        The Docker network both containers are on.

    Returns
    -------
    str
        The project key the analysis was stored under.

    Raises
    ------
    GateError
        If the scanner fails, or the server fails to process its report.
    """
    getuid = getattr(os, "getuid", None)
    user = f"{getuid()}:{os.getgid()}" if getuid else None
    _run(
        scanner_command(tree, version, server=server, network=network, user=user),
        what=f"analyse the {version} commit",
        env={**os.environ, "SONAR_TOKEN": token},
        timeout=_SCAN_TIMEOUT_S,
    )
    try:
        task = parse_key_values((tree / _TASK_REPORT).read_text(encoding="utf-8"))
        task_id, project_key = task["ceTaskId"], task["projectKey"]
    except (OSError, KeyError) as exc:
        raise GateError(f"the scanner left no usable {_TASK_REPORT}: {exc}") from exc

    def processed() -> str | None:
        status = api.call("api/ce/task", id=task_id).get("task", {}).get("status")
        return status if status in ("SUCCESS", "FAILED", "CANCELED") else None

    outcome = wait_for(processed, what=f"processing of the {version} analysis", timeout_s=_TASK_TIMEOUT_S)
    if outcome != "SUCCESS":
        raise GateError(f"the server could not process the {version} analysis: {outcome}")
    return project_key


def read_gate(api: SonarApi, project_key: str) -> GateReport:
    """Read the gate's verdict on the latest analysis of *project_key*.

    Parameters
    ----------
    api : SonarApi
        The server.
    project_key : str
        The project.

    Returns
    -------
    GateReport
        The status, the conditions and the findings in new code. At most
        500 issues and 500 hotspots are listed; the gate's own count of
        new issues is exact either way.

    Raises
    ------
    GateError
        If the server's answers cannot be read.
    """
    status, conditions = parse_gate(api.call("api/qualitygates/project_status", projectKey=project_key))
    page = str(_PAGE_SIZE)
    issues = api.call(
        "api/issues/search",
        components=project_key,
        inNewCodePeriod="true",
        issueStatuses="OPEN,CONFIRMED",
        ps=page,
    )
    hotspots = api.call(
        "api/hotspots/search",
        project=project_key,
        inNewCodePeriod="true",
        status="TO_REVIEW",
        ps=page,
    )
    measures = api.call("api/measures/component", component=project_key, metricKeys="new_lines")
    return GateReport(
        status=status,
        conditions=conditions,
        findings=parse_findings(issues, hotspots),
        new_lines=parse_new_lines(measures),
    )


def _remove(*commands: Sequence[str]) -> None:
    """Run clean-up commands, ignoring every failure.

    Parameters
    ----------
    *commands : sequence of str
        Commands to run in order.
    """
    for command in commands:
        try:
            _run(command, what="clean up", timeout=120)
        except GateError:
            continue


def _announce(line: str) -> None:
    """Print one progress line and flush it.

    A step takes a minute or more, and a piped stream such as a CI log is
    block-buffered: without the flush, every line would appear when the run
    ends, and a run that hangs would show nothing at all.

    Parameters
    ----------
    line : str
        The line to print.

    Examples
    --------
    >>> _announce("Analysing base (v1)")
    Analysing base (v1)
    """
    print(line, flush=True)


def check_gate(
    repo: Path,
    *,
    base: str,
    head: str,
    coverage: Path | None,
    profile: str,
    progress: Callable[[str], None] = _announce,
) -> GateReport:
    """Analyse *base*, then *head*, and return the gate's verdict on *head*.

    Parameters
    ----------
    repo : pathlib.Path
        The repository.
    base : str
        The commit new code is measured against.
    head : str
        The commit to judge.
    coverage : pathlib.Path or None
        A Cobertura report for *head*, written with paths relative to the
        repository root; ``None`` to analyse without coverage.
    profile : str
        The quality profile to analyse with (see :func:`use_profile`).
    progress : callable, optional
        Receives one line for each step, when the step starts. The default
        prints it and flushes (:func:`_announce`).

    Returns
    -------
    GateReport
        The verdict.

    Raises
    ------
    GateError
        If any step fails. The server, the network and the exported
        directories are removed either way.
    """
    run_id = f"sonar-gate-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="sonar-gate-") as scratch:
        trees = {"base": Path(scratch) / "base", "head": Path(scratch) / "head"}
        for name, revision in (("base", base), ("head", head)):
            trees[name].mkdir()
            export_tree(repo, revision, trees[name])
        settings = trees["head"] / _SETTINGS_FILE
        if not settings.is_file():
            raise GateError(f"{head} has no {_SETTINGS_FILE}")
        # One scope for both analyses: the head's settings decide what is
        # analysed, or a changed exclusion would itself look like new code.
        shutil.copyfile(settings, trees["base"] / _SETTINGS_FILE)
        if coverage is not None:
            shutil.copyfile(coverage, trees["head"] / _COVERAGE_FILE)

        _run(["docker", "network", "create", run_id], what="create a Docker network")
        try:
            progress(f"Starting {SERVER_IMAGE}")
            api = start_server(run_id, run_id)
            token = api.call(
                "api/user_tokens/generate", post=True, name=run_id, type="GLOBAL_ANALYSIS_TOKEN"
            )["token"]
            languages = use_profile(api, profile)
            progress(f"Profile {profile!r} for: {', '.join(languages)}")
            project_key = ""
            for name, revision in (("base", base), ("head", head)):
                progress(f"Analysing {name} ({revision})")
                project_key = analyse(
                    api, trees[name], name, token=token, server=run_id, network=run_id
                )
            return read_gate(api, project_key)
        finally:
            _remove(
                ["docker", "rm", "--force", "--volumes", run_id],
                ["docker", "network", "rm", run_id],
            )


# -- Command line --------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns
    -------
    argparse.ArgumentParser
        The parser for the options listed in the module docstring.
    """
    parser = argparse.ArgumentParser(
        prog="sonar_gate",
        description="Check the SonarQube quality gate on a throwaway server.",
    )
    parser.add_argument("--base", required=True, help="commit new code is measured against")
    parser.add_argument("--head", default="HEAD", help="commit to judge (default: HEAD)")
    parser.add_argument(
        "--coverage",
        type=Path,
        help="Cobertura report for the head commit; without it coverage reads as 0",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=f"built-in quality profile to analyse with (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=os.environ.get("GITHUB_STEP_SUMMARY") or None,
        help="append a Markdown report to this file (default: $GITHUB_STEP_SUMMARY)",
    )
    parser.add_argument("--repo", type=Path, default=Path("."), help="the repository (default: .)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; return the process exit status.

    Parameters
    ----------
    argv : sequence of str, optional
        Arguments after the program name. Defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        ``0`` if the gate passed, ``1`` if it failed, ``2`` if it could
        not be evaluated.

    Examples
    --------
    >>> main(["--base", "HEAD", "--coverage", "no/such/coverage.xml"])
    2
    """
    args = _build_parser().parse_args(argv)
    try:
        if args.coverage is not None and not args.coverage.is_file():
            raise GateError(f"coverage report {args.coverage} does not exist")
        report = check_gate(
            args.repo,
            base=args.base,
            head=args.head,
            coverage=args.coverage,
            profile=args.profile,
        )
    except GateError as exc:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            first_line = str(exc).splitlines()[0] if str(exc) else "unknown error"
            print(f"::error title=SonarQube gate not evaluated::{first_line}", file=sys.stderr)
        print(f"sonar_gate: error: {exc}", file=sys.stderr)
        return 2
    print(render_text(report))
    if args.summary is not None:
        with open(args.summary, "a", encoding="utf-8") as summary:
            summary.write(render_markdown(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    # A scanner's output and a path can hold characters a Windows console's
    # code page does not.
    use_utf8_console()
    sys.exit(main())
