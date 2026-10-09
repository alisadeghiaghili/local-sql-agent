# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Work out what a release is called and what it says, for the release workflow.

Usage::

    python scripts/release_notes.py version [--file core/version.py] [--expect X.Y.Z]
    python scripts/release_notes.py prepare --version X.Y.Z --target SHA \\
        --notes-file PATH [--changelog CHANGELOG.md] [--repo .]

``.github/workflows/release.yml`` calls this; the workflow itself only does
what has to happen on GitHub's side (push the tag, create the Release).
Everything that can be decided from the repository alone lives here, so it
can be tested without a runner.

A release follows one pattern. A ``chore/release-X.Y.Z`` pull request changes
only ``CHANGELOG.md`` (a ``## [X.Y.Z] — YYYY-MM-DD`` section) and
``core/version.py`` (``__version__ = "X.Y.Z"``); its commit subject is
``chore(release): X.Y.Z — <summary>``. After it merges:

* the tag is ``vX.Y.Z`` with the message ``X.Y.Z — <summary>``;
* the GitHub Release is titled ``X.Y.Z — <summary>``;
* the Release body is that version's ``CHANGELOG.md`` section without its
  heading line.

Pipeline overview
-----------------
1. :func:`read_version`              -- the version ``core/version.py`` declares.
2. :func:`find_release_summary`      -- the ``<summary>`` from the release
   commit's subject, searched in the history reachable from the target.
3. :func:`extract_changelog_section` -- the notes for the Release body.

``version`` prints (1); ``prepare`` prints (2) and writes (3) to a file.
Every failure raises :class:`ReleaseError`, which :func:`main` turns into a
one-line message and exit status 1, so the workflow log says what is wrong
rather than showing a traceback.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

# The release workflow runs this file from a sparse checkout that holds only it
# and core/console.py (see .github/workflows/release.yml), so the repository
# root is put on the path the way the other scripts do, and nothing else from
# the repository may be imported here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.console import use_utf8_console  # noqa: E402

#: Separator between the version and the summary in the release commit's
#: subject, the tag message and the Release title: an em dash, not a hyphen.
_DASH = "—"

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
_VERSION_ASSIGNMENT_RE = re.compile(
    r"""^__version__\s*=\s*(?P<quote>["'])(?P<version>[^"']*)(?P=quote)""",
    re.MULTILINE,
)


class ReleaseError(Exception):
    """A release cannot be prepared; the message says why and what to check."""


def validate_version(version: str) -> str:
    """Return *version* unchanged if it has the form ``X.Y.Z``, else raise.

    Parameters
    ----------
    version : str
        The candidate version, without a leading ``v``.

    Returns
    -------
    str
        *version*, unchanged.

    Raises
    ------
    ReleaseError
        If *version* is not three dot-separated integers.

    Examples
    --------
    >>> validate_version("6.1.0")
    '6.1.0'
    >>> validate_version("v6.1.0")
    Traceback (most recent call last):
        ...
    scripts.release_notes.ReleaseError: 'v6.1.0' is not a version of the form X.Y.Z (digits only, no leading 'v')
    """
    if not _VERSION_RE.fullmatch(version):
        raise ReleaseError(
            f"{version!r} is not a version of the form X.Y.Z "
            "(digits only, no leading 'v')"
        )
    return version


def read_version(path: str | Path) -> str:
    """Read the version declared by ``__version__ = "X.Y.Z"`` in *path*.

    Parameters
    ----------
    path : str or pathlib.Path
        The file to read, normally ``core/version.py``.

    Returns
    -------
    str
        The version string, validated to be of the form ``X.Y.Z``.

    Raises
    ------
    ReleaseError
        If the file cannot be read, has no ``__version__`` assignment, or the
        value is not of the form ``X.Y.Z``.

    Examples
    --------
    >>> import tempfile, pathlib
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     p = pathlib.Path(tmp) / "version.py"
    ...     _ = p.write_text('# Doc\\n__version__ = "6.2.0"\\n', encoding="utf-8")
    ...     read_version(p)
    '6.2.0'
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc
    match = _VERSION_ASSIGNMENT_RE.search(source)
    if match is None:
        raise ReleaseError(f'{path} has no line of the form __version__ = "X.Y.Z"')
    return validate_version(match.group("version"))


def extract_changelog_section(changelog: str, version: str) -> str:
    """Return the body of the ``## [version]`` section of a changelog.

    The heading line itself is excluded, and so is everything from the next
    ``## [`` heading on. Blank lines at the start and end are trimmed; the
    result ends with exactly one newline.

    Parameters
    ----------
    changelog : str
        The full text of ``CHANGELOG.md``.
    version : str
        The version whose section to extract, such as ``"6.1.0"``.

    Returns
    -------
    str
        The section body, ending in a newline.

    Raises
    ------
    ReleaseError
        If there is no ``## [version]`` heading, or the section has no
        content.

    Examples
    --------
    >>> text = (
    ...     "# Changelog\\n\\n"
    ...     "## [2.0.0] — 2026-02-01\\n\\nSecond.\\n\\n### Added\\n\\n- b\\n\\n"
    ...     "## [1.0.0] — 2026-01-01\\n\\nFirst.\\n"
    ... )
    >>> print(extract_changelog_section(text, "2.0.0"), end="")
    Second.
    <BLANKLINE>
    ### Added
    <BLANKLINE>
    - b
    >>> print(extract_changelog_section(text, "1.0.0"), end="")
    First.
    >>> extract_changelog_section(text, "3.0.0")
    Traceback (most recent call last):
        ...
    scripts.release_notes.ReleaseError: CHANGELOG.md has no '## [3.0.0]' section
    """
    heading = re.compile(rf"^## \[{re.escape(version)}\]")
    body: list[str] = []
    found = False
    for line in changelog.splitlines():
        if not found:
            found = bool(heading.match(line))
        elif line.startswith("## ["):
            break
        else:
            body.append(line)
    if not found:
        raise ReleaseError(f"CHANGELOG.md has no '## [{version}]' section")
    text = "\n".join(body).strip("\n")
    if not text.strip():
        raise ReleaseError(
            f"the '## [{version}]' section of CHANGELOG.md is empty; "
            "a Release with no notes is not published"
        )
    return text + "\n"


def find_release_summary(
    version: str, target: str = "HEAD", repo: str | Path = "."
) -> str:
    """Return the summary from the release commit's subject.

    Looks for the most recent commit reachable from *target* whose subject
    starts with ``chore(release): <version> — `` and returns the rest of
    that subject. Every parent is followed, not just the first: the release
    commit sits on the pull request's branch, behind the merge commit.

    Parameters
    ----------
    version : str
        The version being released, such as ``"6.1.0"``.
    target : str, default "HEAD"
        The commit (SHA, branch or tag) to search back from.
    repo : str or pathlib.Path, default "."
        The git repository to search. It needs the full history, not a
        shallow clone.

    Returns
    -------
    str
        The summary, for example ``"more than one warehouse data source"``.

    Raises
    ------
    ReleaseError
        If ``git log`` fails, or no such commit exists, or its summary is
        empty.

    Examples
    --------
    >>> import subprocess, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     _ = subprocess.run(["git", "init", "-q", tmp], check=True)
    ...     _ = subprocess.run(
    ...         ["git", "-C", tmp, "-c", "user.name=T", "-c", "user.email=t@example.com",
    ...          "commit", "-q", "--allow-empty",
    ...          "-m", "chore(release): 1.2.3 — the summary"],
    ...         check=True,
    ...     )
    ...     find_release_summary("1.2.3", repo=tmp)
    'the summary'
    """
    # git trims trailing blanks from a subject, so "<version> — " with an
    # empty summary arrives as "<version> —"; match that too, to report
    # the empty summary instead of "not found".
    lead = f"chore(release): {version} {_DASH}"
    prefix = lead + " "
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "i18n.logOutputEncoding=UTF-8",
                "log",
                "--format=%s",
                target,
                "--",
            ],
            cwd=repo,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or exc
        raise ReleaseError(f"git log {target} failed: {str(detail).strip()}") from exc
    for subject in result.stdout.splitlines():
        if subject == lead or subject.startswith(prefix):
            summary = subject[len(lead) :].strip()
            if not summary:
                raise ReleaseError(
                    f"the release commit for {version} has no summary after '{lead}'"
                )
            return summary
    raise ReleaseError(
        f"no commit reachable from {target} has a subject starting with "
        f"'{prefix}'. Was the release merged with a different subject, or is "
        "the checkout shallow (fetch-depth: 0 is needed)?"
    )


def _cmd_version(args: argparse.Namespace) -> int:
    """Print the declared version, optionally requiring it to equal ``--expect``."""
    version = read_version(args.file)
    if args.expect:
        validate_version(args.expect)
        if version != args.expect:
            raise ReleaseError(
                f"{args.file} declares {version}, not the requested {args.expect}. "
                "Dispatch with the version that file has at the chosen ref."
            )
    print(version)
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    """Write the Release notes to ``--notes-file`` and print the summary."""
    validate_version(args.version)
    summary = find_release_summary(args.version, args.target, args.repo)
    try:
        changelog = Path(args.changelog).read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(f"cannot read {args.changelog}: {exc}") from exc
    notes = extract_changelog_section(changelog, args.version)
    Path(args.notes_file).write_text(notes, encoding="utf-8", newline="\n")
    print(summary)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for :func:`main`."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_version = sub.add_parser("version", help="print the version core/version.py declares")
    p_version.add_argument("--file", default="core/version.py")
    p_version.add_argument("--expect", default="", help="fail unless the file declares this version")
    p_version.set_defaults(func=_cmd_version)

    p_prepare = sub.add_parser("prepare", help="write the notes file and print the summary")
    p_prepare.add_argument("--version", required=True)
    p_prepare.add_argument("--target", required=True, help="commit to search back from")
    p_prepare.add_argument("--notes-file", required=True)
    p_prepare.add_argument("--changelog", default="CHANGELOG.md")
    p_prepare.add_argument("--repo", default=".")
    p_prepare.set_defaults(func=_cmd_prepare)
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
        ``0`` on success, ``1`` if a :class:`ReleaseError` was raised.

    Examples
    --------
    >>> main(["version", "--file", "no/such/version.py"])
    1
    """
    args = _build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ReleaseError as exc:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error title=Release::{exc}", file=sys.stderr)
        print(f"release_notes: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # The summary contains an em dash, which a Windows console's code page
    # may not hold.
    use_utf8_console()
    sys.exit(main())
