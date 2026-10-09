# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Every command-line program must print Persian on a console that cannot.

Why this exists
---------------
Reports, prompts and error messages carry Persian questions and table
names. On Windows, when a program's output is piped or redirected (the
usual way an operator captures a report from PowerShell), Python encodes it
with the legacy code page -- cp1252, cp1256, cp437 -- and ``print`` raises
``UnicodeEncodeError`` part-way through. :func:`core.console.use_utf8_console`
fixes that for a program, and each entry point calls it first thing in its
``if __name__ == "__main__":`` block.

Two kinds of test keep it that way:

* :class:`TestEveryEntryPointRunsOnANarrowConsole` runs each command-line
  program in a child process whose streams are cp1252 (what
  ``PYTHONIOENCODING`` reproduces on any platform) with arguments that make
  it print Persian, or another character cp1252 cannot hold, and checks
  that it neither crashes nor writes anything but valid UTF-8.
* :class:`TestEntryPointGuard` scans the source tree and fails when a
  ``__main__`` block does not call the helper -- so a new script cannot
  quietly bring the bug back -- or when a program has no case in the first
  class.

A case that cannot print anything cp1252 rejects without writing to
something real (``webapp/app.py``) still runs the ``__main__`` path, which
catches an import or call that is broken; the guard covers the rest.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A Persian letter, the marker for "the program really printed Persian".
PERSIAN = re.compile("[؀-ۿ]")

#: Directory and table names that cp1252 cannot encode, used so that the
#: program's own output (a path, a table name) is what carries the Persian.
FA_DIR_NAME = "پوشه_فارسی"
FA_CONFIG_NAME = "پیکربندی"
FA_TABLE = "جدول_نمونه"
FA_QUESTION = "چند مشتری فعال داریم؟"
FA_NAME = "تحلیلگر ارشد"
FA_SUMMARY = "نسخه پایدار نخست"

Marker = str | re.Pattern[str]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ctx:
    """Everything a case needs, all of it under one temporary directory."""

    work: Path  #: the child's working directory (nothing is written to the repository)
    fa_dir: Path  #: a directory whose name cp1252 cannot encode
    config: Path  #: a copy of ``project_config.example`` with a Persian table
    sqlite_url: str  #: an empty SQLite database inside ``fa_dir``
    inspect_url: str  #: a SQLite database holding a Persian table with a Persian column
    audit_log: Path  #: an audit log holding Persian questions
    candidates: Path  #: harvested candidates awaiting review, with a Persian question
    release_repo: Path  #: a git repository whose release commit has a Persian summary
    release_changelog: Path


def _write_config(dest: Path) -> None:
    """Copy the example config and add one Persian-named table to ``schema.yaml``."""
    shutil.copytree(
        REPO_ROOT / "project_config.example", dest, ignore=shutil.ignore_patterns("_test_fixtures")
    )
    schema_path = dest / "schema.yaml"
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    schema["tables"][FA_TABLE] = {
        "description": "جدول نمونه",
        "db_schema": "sales",
        "columns": {"شناسه": "int", "نام": "nvarchar"},
    }
    schema_path.write_text(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _write_persian_database(path: Path) -> None:
    """A SQLite database with one Persian-named table and column.

    The programs that read a database print its table names, so this puts
    Persian in their output whatever way the connection URL is rendered
    (SQLAlchemy 2.1 percent-encodes a non-ASCII path; 2.0 does not).
    """
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f'CREATE TABLE "{FA_TABLE}" ("شناسه" INTEGER PRIMARY KEY, "نام" TEXT)')
        conn.commit()


def _write_audit_log(path: Path) -> None:
    records = [
        {
            "timestamp": f"2026-10-01T10:0{i}:00+00:00",
            "request_id": f"req_{i}",
            "question": f"{FA_QUESTION} {i}",
            "generated_sql": "SELECT 1 AS n",
            "error_code": None if i % 2 else "FORBIDDEN_SQL",
            "row_count": 1,
            "datasource": "sales",
            "principal_id": "analyst-1",
            "llm": {"model": "local-model"},
        }
        for i in range(6)
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )


def _write_candidates(path: Path) -> None:
    case = {
        "id": "cand_0001",
        "question": FA_QUESTION,
        "tags": ["lang:fa", "outcome:success"],
        "expected_sql": "SELECT 1 AS n",
        "expect": "success",
        "status": "pending_review",
    }
    path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")


def _make_release_repo(repo: Path, changelog: Path) -> None:
    """A repository with a release commit whose summary is Persian."""
    repo.mkdir()
    changelog.write_text("## [1.0.0] - 2026-01-01\n\n- First release.\n", encoding="utf-8")
    message = repo.parent / "commit-message.txt"
    message.write_text(f"chore(release): 1.0.0 — {FA_SUMMARY}\n", encoding="utf-8", newline="\n")
    git = ["git", "-c", "user.name=Test Author", "-c", "user.email=test@example.com",
           "-c", "commit.gpgsign=false", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-F", str(message)], check=True)


@pytest.fixture()
def ctx(tmp_path: Path) -> Ctx:
    fa_dir = tmp_path / FA_DIR_NAME
    fa_dir.mkdir()
    config = fa_dir / FA_CONFIG_NAME
    _write_config(config)
    audit_log = fa_dir / "audit.jsonl"
    _write_audit_log(audit_log)
    _write_persian_database(fa_dir / "tables.db")
    candidates = fa_dir / "pending.jsonl"
    _write_candidates(candidates)
    work = tmp_path / "work"
    work.mkdir()
    release_repo = tmp_path / "release-repo"
    release_changelog = tmp_path / "CHANGELOG.md"
    if shutil.which("git"):
        _make_release_repo(release_repo, release_changelog)
    return Ctx(
        work=work,
        fa_dir=fa_dir,
        config=config,
        sqlite_url=f"sqlite:///{(fa_dir / 'app.db').as_posix()}",
        inspect_url=f"sqlite:///{(fa_dir / 'tables.db').as_posix()}",
        audit_log=audit_log,
        candidates=candidates,
        release_repo=release_repo,
        release_changelog=release_changelog,
    )


# ---------------------------------------------------------------------------
# The cases: one per command-line program
# ---------------------------------------------------------------------------


def _port_8000_is_open() -> bool:
    """True when something listens on the load test's hard-coded port."""
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.5):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class Case:
    """One program, the arguments that make it print, and what must show up.

    ``marker`` is text (or a pattern) that must appear in what the program
    printed, so a case cannot pass by printing nothing. ``returncodes`` are
    the exit statuses that count as the program having run: a deployment
    check that finds nothing configured legitimately exits 1.
    """

    script: str  #: repo-relative path of the program, as the guard test names it
    args: Callable[[Ctx], list[str]]
    marker: Marker
    returncodes: tuple[int, ...] = (0,)
    env: Callable[[Ctx], dict[str, str]] = field(default=lambda ctx: {})
    skip: Callable[[], str | None] = field(default=lambda: None)

    @property
    def id(self) -> str:
        return self.script


def _sqlite_env(ctx: Ctx) -> dict[str, str]:
    return {"DB_CONNECTION_URL": ctx.sqlite_url, "SQL_DIALECT": "sqlite"}


def _stress_skip() -> str | None:
    return "something is listening on port 8000" if _port_8000_is_open() else None


def _release_skip() -> str | None:
    return None if shutil.which("git") else "git is not installed"


CASES: tuple[Case, ...] = (
    Case(
        "app.py",
        lambda c: ["app.py"],
        "Question:",
        env=lambda c: {**_sqlite_env(c), "OPENAI_API_KEY": "local-key"},
    ),
    Case(
        "setup_project.py",
        lambda c: ["setup_project.py", "--db-url", c.inspect_url, "--llm-provider", "mock",
                   "--non-interactive", "--dry-run", "--language", "fa"],
        FA_TABLE,  # in the discovered-tables listing and the drafts
    ),
    Case(
        "webapp/app.py",
        lambda c: ["webapp/app.py", "create-user", FA_NAME, ""],
        "Username and password must not be empty.",
        returncodes=(2,),
        env=lambda c: {"ADMIN_USER": "admin"},
    ),
    Case(
        "database/schema_inspector_cli.py",
        lambda c: ["-m", "database.schema_inspector_cli", "--db-url", c.inspect_url,
                   "--dry-run", "--sample-rows", "0"],
        FA_TABLE,
    ),
    Case(
        "eval/cli.py",
        lambda c: ["-m", "eval.cli", "recall", "--golden",
                   str(REPO_ROOT / "eval_data.example" / "golden.jsonl"), "--json"],
        PERSIAN,
    ),
    Case(
        "eval/benchmarks/retrieval_synth.py",
        lambda c: ["-m", "eval.benchmarks.retrieval_synth", "generate", "--out",
                   str(c.fa_dir / "bench"), "--tables", "60", "--questions", "4"],
        FA_DIR_NAME,
    ),
    Case(
        "scripts/analyze_audit_log.py",
        lambda c: ["scripts/analyze_audit_log.py", str(c.audit_log), "--include-examples", "--json"],
        PERSIAN,
    ),
    Case("scripts/analyze_misses.py", lambda c: ["scripts/analyze_misses.py"], "✅"),
    Case(
        "scripts/assign_datasources.py",
        lambda c: ["scripts/assign_datasources.py", "--check"],
        FA_TABLE,
        returncodes=(0, 1),
        env=_sqlite_env,
    ),
    Case(
        "scripts/create_db.py",
        lambda c: ["scripts/create_db.py"],
        "✅",
        env=lambda c: {"SQLITE_DB_PATH": str(c.fa_dir / "نمونه.db")},
    ),
    Case(
        "scripts/golden_sheet.py",
        lambda c: ["scripts/golden_sheet.py", "export", "--cases", str(c.candidates),
                   "--out", str(c.fa_dir / "بازبینی.csv")],
        FA_DIR_NAME,
    ),
    Case(
        "scripts/harvest_golden.py",
        lambda c: ["scripts/harvest_golden.py", str(c.audit_log), "--out",
                   str(c.fa_dir / "candidates.jsonl"), "--include-examples"],
        PERSIAN,
    ),
    Case(
        "scripts/issue_api_key.py",
        lambda c: ["-m", "scripts.issue_api_key", "--id", "analyst-1", "--name", FA_NAME],
        FA_NAME,
    ),
    Case(
        "scripts/migrate_app_db.py",
        lambda c: ["-m", "scripts.migrate_app_db", "--from", f"sqlite:///{(c.fa_dir / 'a.db').as_posix()}",
                   "--to", f"sqlite:///{(c.fa_dir / 'b.db').as_posix()}", "--dry-run"],
        FA_DIR_NAME,
    ),
    Case(
        "scripts/prompt_budget.py",
        lambda c: ["scripts/prompt_budget.py", "--no-model", "--context-length", "8192"],
        FA_CONFIG_NAME,
    ),
    Case(
        "scripts/release_notes.py",
        lambda c: ["scripts/release_notes.py", "prepare", "--version", "1.0.0", "--target", "HEAD",
                   "--notes-file", str(c.fa_dir / "notes.md"), "--changelog", str(c.release_changelog),
                   "--repo", str(c.release_repo)],
        FA_SUMMARY,
        skip=_release_skip,
    ),
    Case(
        "scripts/sync_schema.py",
        lambda c: ["scripts/sync_schema.py", "--dry-run"],
        FA_TABLE,
        env=_sqlite_env,
    ),
    Case(
        "scripts/verify_deployment.py",
        lambda c: ["scripts/verify_deployment.py"],
        FA_CONFIG_NAME,
        returncodes=(0, 1),
        env=lambda c: {**_sqlite_env(c), "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"},
    ),
    Case(
        "tests/test_stress.py",
        lambda c: ["tests/test_stress.py"],
        PERSIAN,
        skip=_stress_skip,
    ),
)


def _child_env(ctx: Ctx, case: Case) -> dict[str, str]:
    """The child's environment: a narrow console, the example config, nothing of the host's."""
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONUTF8", "PYTHONENCODING"}}
    env.update(
        PYTHONIOENCODING="cp1252",  # a console that cannot encode Persian
        # The child runs outside the repository; anything already on the path stays.
        PYTHONPATH=os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH", "")])),
        PROJECT_CONFIG_DIR=str(ctx.config),
        LLM_ALLOW_REMOTE="false",
    )
    env.update(case.env(ctx))
    return env


def _argv(case: Case, ctx: Ctx) -> list[str]:
    """Programs run by path get an absolute path, since the child's cwd is not the repository."""
    args = case.args(ctx)
    if args and not args[0].startswith("-"):
        args[0] = str(REPO_ROOT / args[0])
    return [sys.executable, *args]


def _matches(marker: Marker, text: str) -> bool:
    return bool(marker.search(text)) if isinstance(marker, re.Pattern) else marker in text


class TestEveryEntryPointRunsOnANarrowConsole:
    """The program survives a cp1252 console and what it writes is UTF-8."""

    @pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
    def test_prints_without_unicode_encode_error(self, case: Case, ctx: Ctx) -> None:
        reason = case.skip()
        if reason:
            pytest.skip(reason)

        result = subprocess.run(
            _argv(case, ctx),
            cwd=ctx.work,
            env=_child_env(ctx, case),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=300,
            check=False,
        )
        stderr = result.stderr.decode("utf-8", "replace")
        assert "UnicodeEncodeError" not in stderr, stderr[-2000:]
        assert result.returncode in case.returncodes, stderr[-2000:]
        # What the program wrote must be UTF-8, whatever the console says.
        text = result.stdout.decode("utf-8") + stderr
        assert _matches(case.marker, text), f"{case.marker!r} not in the output:\n{text[-2000:]}"


# ---------------------------------------------------------------------------
# The guard: a new program cannot skip the helper
# ---------------------------------------------------------------------------

#: ``__main__`` blocks that are not command-line programs that print text,
#: or that must not be touched, each with the reason. A new entry belongs
#: here only with a reason; a program that prints belongs in CASES instead.
NOT_CONSOLE_ENTRY_POINTS: dict[str, str] = {
    "api/__main__.py": (
        "the web server launcher: hands the process to uvicorn and the logging "
        "configuration, which own the streams; deliberately left alone"
    ),
    "scripts/dev_v2_demo_server.py": (
        "runs the web server under uvicorn (deliberately left alone) and prints "
        "only an ASCII banner"
    ),
    "tests/test_stress_sql_agent.py": (
        "a manual load test that needs a live model; prints only ASCII figures "
        "and its fixed English questions"
    ),
}

_SKIP_DIRS = {"venv", "env", "node_modules", "site-packages", "__pycache__", "build", "dist"}


def _python_files() -> list[Path]:
    """Every ``.py`` file in the repository, without dot-directories and virtualenvs."""
    found: list[Path] = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS
                   and not d.startswith("venv")]
        found.extend(Path(root) / f for f in files if f.endswith(".py"))
    return sorted(found)


def _is_main_guard(node: ast.AST) -> bool:
    """True for ``if __name__ == "__main__":`` at module level."""
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    operands = [test.left, *test.comparators]
    names = {o.id for o in operands if isinstance(o, ast.Name)}
    consts = {o.value for o in operands if isinstance(o, ast.Constant)}
    return len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq) and names == {"__name__"} and consts == {"__main__"}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _main_blocks(path: Path) -> list[ast.If]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [n for n in tree.body if _is_main_guard(n)]


def _is_pytest_runner(block: ast.If) -> bool:
    """A test module's ``__main__`` block that only hands over to ``pytest.main``."""
    return any(
        isinstance(n, ast.Call) and _dotted(n.func) == "pytest.main" for n in ast.walk(block)
    )


#: Modules that carry their own copy of the helper, by the name they call.
#: ``scripts/release_notes.py`` is fetched alone by the release workflow
#: (a sparse checkout of that one file), so it cannot import ``core``.
LOCAL_HELPERS: dict[str, str] = {"scripts/release_notes.py": "_use_utf8_console"}


def _calls_helper_first(block: ast.If, helper: str = "use_utf8_console") -> bool:
    """The console helper is called before anything runs.

    Only imports and ``sys.path.insert(...)`` (needed to reach ``core`` when
    a script is run by path) may precede it.
    """
    for stmt in block.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            callee = _dotted(stmt.value.func)
            if callee == helper:
                return True
            if callee == "sys.path.insert":
                continue
        return False
    return False


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _entry_points() -> dict[str, list[ast.If]]:
    """``__main__`` blocks by repo-relative path, pytest self-runners left out."""
    found: dict[str, list[ast.If]] = {}
    for path in _python_files():
        blocks = [b for b in _main_blocks(path) if not _is_pytest_runner(b)]
        if blocks:
            found[_relative(path)] = blocks
    return found


class TestEntryPointGuard:
    def test_the_scan_finds_the_programs_it_should(self) -> None:
        # A scanner that silently found nothing would pass everything below.
        found = _entry_points()
        assert "eval/cli.py" in found
        assert "scripts/prompt_budget.py" in found
        assert "api/__main__.py" in found
        assert len(found) >= len(CASES)

    def test_every_main_block_calls_the_helper_first(self) -> None:
        offenders = []
        for rel, blocks in _entry_points().items():
            if rel in NOT_CONSOLE_ENTRY_POINTS:
                continue
            for block in blocks:
                if not _calls_helper_first(block, LOCAL_HELPERS.get(rel, "use_utf8_console")):
                    offenders.append(f"{rel}:{block.lineno}")
        assert not offenders, (
            "These `if __name__ == \"__main__\":` blocks do not call "
            "core.console.use_utf8_console() before anything else, so their "
            "output fails with UnicodeEncodeError on a Windows console that "
            "cannot encode Persian. Add the call (and a case to CASES in "
            "tests/test_cli_utf8_console.py), or, if the module is not a "
            "command-line program, list it in NOT_CONSOLE_ENTRY_POINTS with a "
            "reason:\n  " + "\n  ".join(offenders)
        )

    def test_every_covered_program_has_a_subprocess_case(self) -> None:
        covered = {rel for rel, blocks in _entry_points().items()
                   if rel not in NOT_CONSOLE_ENTRY_POINTS}
        cased = {c.script for c in CASES}
        assert covered - cased == set(), "no subprocess case for: " + ", ".join(sorted(covered - cased))
        assert cased - covered == set(), "a case for a program that is gone: " + ", ".join(sorted(cased - covered))

    def test_the_allowlist_has_no_stale_entries(self) -> None:
        entry_points = _entry_points()
        for rel, reason in NOT_CONSOLE_ENTRY_POINTS.items():
            assert reason.strip(), rel
            assert rel in entry_points, f"{rel} no longer has a __main__ block; remove it from the allowlist"

    def test_release_notes_stays_standalone(self) -> None:
        # The release workflow fetches this file alone; an import from the
        # repository would break the release pipeline, and only a real release
        # would show it.
        path = REPO_ROOT / "scripts" / "release_notes.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stdlib = set(sys.stdlib_module_names)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.split(".")[0] not in stdlib]
            elif isinstance(node, ast.ImportFrom):
                if node.level or (node.module or "").split(".")[0] not in stdlib:
                    offenders.append("." * node.level + (node.module or ""))
        assert not offenders, f"scripts/release_notes.py must import only the standard library: {offenders}"
        assert not any(
            isinstance(n, ast.Call) and _dotted(n.func) == "sys.path.insert" for n in ast.walk(tree)
        ), "scripts/release_notes.py must not extend sys.path"

    def test_release_notes_copy_behaves_like_the_shared_helper(self) -> None:
        import io

        from core.console import use_utf8_console
        from scripts.release_notes import _use_utf8_console

        def run(helper: Callable[[], None], out: Any, err: Any) -> None:
            saved = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = out, err
            try:
                helper()
            finally:
                sys.stdout, sys.stderr = saved

        def state(helper: Callable[[], None]) -> list[object]:
            out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
            err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="backslashreplace")
            run(helper, out, err)
            read = io.TextIOWrapper(io.BytesIO(b"x"), encoding="cp1252")
            read.read()  # a stream already read from: whatever happens, it must not raise
            run(helper, io.StringIO(), object())  # no `reconfigure`: skipped
            run(helper, read, io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="replace"))
            return [out.encoding, out.errors, err.encoding, err.errors, read.encoding]

        local, shared = state(_use_utf8_console), state(use_utf8_console)
        assert local == shared
        assert local[:4] == ["utf-8", "strict", "utf-8", "backslashreplace"]

    def test_a_block_that_forgets_the_helper_is_caught(self) -> None:
        source = 'import sys\n\nif __name__ == "__main__":\n    sys.exit(main())\n'
        block = next(n for n in ast.parse(source).body if _is_main_guard(n))
        assert not _calls_helper_first(block)

    @pytest.mark.parametrize(
        "body",
        [
            "use_utf8_console()\n    sys.exit(main())",
            "sys.path.insert(0, root)\n    from core.console import use_utf8_console\n    use_utf8_console()\n    main()",
        ],
    )
    def test_a_block_that_calls_it_first_passes(self, body: str) -> None:
        block = next(n for n in ast.parse(f'if __name__ == "__main__":\n    {body}\n').body if _is_main_guard(n))
        assert _calls_helper_first(block)

    def test_release_notes_must_call_its_own_copy(self) -> None:
        # The shared helper's name does not satisfy the guard for that file,
        # and a missing call still fails.
        block = next(n for n in ast.parse(
            'if __name__ == "__main__":\n    sys.exit(main())\n').body if _is_main_guard(n))
        local = LOCAL_HELPERS["scripts/release_notes.py"]
        assert not _calls_helper_first(block, local)
        calls_local = next(n for n in ast.parse(
            f'if __name__ == "__main__":\n    {local}()\n    main()\n').body if _is_main_guard(n))
        assert _calls_helper_first(calls_local, local)
        assert not _calls_helper_first(calls_local)

    def test_a_call_after_the_work_has_started_is_caught(self) -> None:
        source = 'if __name__ == "__main__":\n    main()\n    use_utf8_console()\n'
        block = next(n for n in ast.parse(source).body if _is_main_guard(n))
        assert not _calls_helper_first(block)



# ---------------------------------------------------------------------------
# The other side of the fix: readers of our programs' output
# ---------------------------------------------------------------------------

_SUBPROCESS_CALLS = {
    "subprocess.run", "subprocess.Popen", "subprocess.check_output",
    "subprocess.check_call", "subprocess.call",
}


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _launches_a_program_of_ours(call: ast.Call) -> bool:
    """False only for a command that is plainly another tool (a literal head such as ``"node"``).

    Anything else -- ``[sys.executable, ...]``, a variable holding the
    command -- is treated as one of ours, since the cost of being wrong
    that way is a one-keyword fix.
    """
    if not call.args:
        return True
    command = call.args[0]
    if isinstance(command, (ast.List, ast.Tuple)) and command.elts:
        head = command.elts[0]
        return not (isinstance(head, ast.Constant) and isinstance(head.value, str))
    return True


def _text_mode_without_encoding(path: Path) -> list[int]:
    """Line numbers of subprocess calls that decode with the locale's code page."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _dotted(node.func) not in _SUBPROCESS_CALLS:
            continue
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        text_mode = _is_true(keywords.get("text")) or _is_true(keywords.get("universal_newlines"))
        if text_mode and "encoding" not in keywords and _launches_a_program_of_ours(node):
            lines.append(node.lineno)
    return lines


class TestReadersDecodeAsUtf8:
    """A parent that reads our programs' output in text mode must say UTF-8.

    The programs now write UTF-8 whatever the console. ``text=True`` alone
    decodes with the parent's locale code page (cp1252 on Windows), which
    fails on the first byte cp1252 does not define -- in a reader thread, so
    the symptom is ``stdout`` being ``None``, far from the cause.
    """

    def test_no_repository_program_is_read_with_the_locale_code_page(self) -> None:
        offenders = [
            f"{_relative(path)}:{line}"
            for path in _python_files()
            for line in _text_mode_without_encoding(path)
        ]
        assert not offenders, (
            "These subprocess calls read a program's output in text mode without "
            "encoding=; add encoding=\"utf-8\" (the programs write UTF-8):\n  "
            + "\n  ".join(offenders)
        )

    def test_the_scan_sees_a_call_that_omits_encoding(self, tmp_path: Path) -> None:
        source = (
            "import subprocess, sys\n"
            "subprocess.run([sys.executable, 'x.py'], capture_output=True, text=True)\n"
            "subprocess.run(cmd, universal_newlines=True)\n"
            "subprocess.run([sys.executable, 'x.py'], capture_output=True, text=True, encoding='utf-8')\n"
            "subprocess.run([sys.executable, 'x.py'], capture_output=True)\n"
            "subprocess.run(['node', 'x.js'], capture_output=True, text=True)\n"
        )
        path = tmp_path / "sample.py"
        path.write_text(source, encoding="utf-8")
        assert _text_mode_without_encoding(path) == [2, 3]
