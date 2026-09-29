# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for scripts/release_notes.py.

The release workflow trusts this script for three things: which version is
being released, what the Release is called, and what its body says. A
mistake in any of them is published under a tag that is awkward to take
back, so each is pinned here, including the em dash the titles depend on.
The history lookup runs against a throwaway git repository rather than a
mock, since what matters is what ``git log`` actually returns.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Make the project root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.release_notes import (
    ReleaseError,
    extract_changelog_section,
    find_release_summary,
    main,
    read_version,
    validate_version,
)

_DASH = "—"

_CHANGELOG = f"""\
# Changelog

Intro text that belongs to no release.

---

## [3.0.0] {_DASH} 2026-03-01

Newest summary line.

### Added

- **Third (PR #3).** Detail.

## [2.0.0] {_DASH} 2026-02-01

Middle summary line.

### Fixed

- Second.

## [1.0.0] {_DASH} 2026-01-01

Oldest summary line.

- First.
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


class TestValidateVersion:
    @pytest.mark.parametrize("version", ["0.0.1", "6.1.0", "10.20.30"])
    def test_accepts_x_y_z(self, version: str) -> None:
        assert validate_version(version) == version

    @pytest.mark.parametrize(
        "version",
        ["", "6", "6.1", "v6.1.0", "6.1.0.1", "6.1.0-rc1", "6.1.x", " 6.1.0", "6.1.0\n"],
    )
    def test_rejects_anything_else(self, version: str) -> None:
        with pytest.raises(ReleaseError, match="X.Y.Z"):
            validate_version(version)


class TestReadVersion:
    def test_reads_a_double_quoted_version(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "version.py", '__version__ = "6.2.0"\n')
        assert read_version(path) == "6.2.0"

    def test_reads_a_single_quoted_version(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "version.py", "__version__ = '1.2.3'\n")
        assert read_version(str(path)) == "1.2.3"

    def test_ignores_the_name_when_it_only_appears_in_the_docstring(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path / "version.py",
            '"""Notes on ``__version__ = "9.9.9"`` in prose."""\n\n'
            "from __future__ import annotations\n\n"
            '__version__ = "6.2.0"\n',
        )
        assert read_version(path) == "6.2.0"

    def test_reads_the_real_version_file(self) -> None:
        from core.version import __version__

        real = Path(__file__).resolve().parent.parent / "core" / "version.py"
        assert read_version(real) == __version__

    def test_missing_assignment_is_an_error(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "version.py", '"""No version here."""\n')
        with pytest.raises(ReleaseError, match="__version__"):
            read_version(path)

    def test_a_non_release_version_is_an_error(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "version.py", '__version__ = "6.2"\n')
        with pytest.raises(ReleaseError, match="X.Y.Z"):
            read_version(path)

    def test_missing_file_is_an_error_that_names_it(self, tmp_path: Path) -> None:
        with pytest.raises(ReleaseError, match="cannot read"):
            read_version(tmp_path / "absent.py")


class TestExtractChangelogSection:
    def test_first_section_stops_at_the_next_heading(self) -> None:
        notes = extract_changelog_section(_CHANGELOG, "3.0.0")
        assert notes == (
            "Newest summary line.\n\n### Added\n\n- **Third (PR #3).** Detail.\n"
        )

    def test_middle_section_is_bounded_on_both_sides(self) -> None:
        notes = extract_changelog_section(_CHANGELOG, "2.0.0")
        assert notes == "Middle summary line.\n\n### Fixed\n\n- Second.\n"

    def test_last_section_runs_to_the_end_of_the_file(self) -> None:
        notes = extract_changelog_section(_CHANGELOG, "1.0.0")
        assert notes == "Oldest summary line.\n\n- First.\n"

    def test_the_heading_line_is_not_part_of_the_notes(self) -> None:
        for version in ("3.0.0", "2.0.0", "1.0.0"):
            notes = extract_changelog_section(_CHANGELOG, version)
            assert f"[{version}]" not in notes
            assert not notes.startswith("##")
            assert _DASH not in notes.splitlines()[0]

    def test_text_before_the_first_release_is_never_included(self) -> None:
        assert "Intro text" not in extract_changelog_section(_CHANGELOG, "3.0.0")
        assert "---" not in extract_changelog_section(_CHANGELOG, "3.0.0")

    def test_sub_headings_do_not_end_the_section(self) -> None:
        text = "## [1.0.0] - d\n\nBody.\n\n### Added\n\n- x\n\n#### Deeper\n\n- y\n"
        assert "#### Deeper" in extract_changelog_section(text, "1.0.0")

    def test_a_version_is_not_matched_by_a_longer_one(self) -> None:
        text = "## [1.0.10] - d\n\nTen.\n\n## [1.0.1] - d\n\nOne.\n"
        assert extract_changelog_section(text, "1.0.1") == "One.\n"
        assert extract_changelog_section(text, "1.0.10") == "Ten.\n"

    def test_a_dot_in_the_version_is_not_a_wildcard(self) -> None:
        with pytest.raises(ReleaseError, match="no '## \\[1x0.0\\]'"):
            extract_changelog_section("## [1.0.0] - d\n\nBody.\n", "1x0.0")

    def test_the_em_dash_in_the_heading_is_handled(self) -> None:
        text = f"## [4.0.0] {_DASH} 2026-04-01\n\nBody {_DASH} with a dash.\n"
        assert extract_changelog_section(text, "4.0.0") == f"Body {_DASH} with a dash.\n"

    def test_crlf_line_endings_give_lf_notes(self) -> None:
        text = "## [1.0.0] - d\r\n\r\nBody.\r\n\r\n- item\r\n"
        assert extract_changelog_section(text, "1.0.0") == "Body.\n\n- item\n"

    def test_missing_version_is_an_error_that_names_it(self) -> None:
        with pytest.raises(ReleaseError, match=r"no '## \[9\.9\.9\]' section"):
            extract_changelog_section(_CHANGELOG, "9.9.9")

    def test_a_heading_with_no_content_is_an_error(self) -> None:
        text = f"## [2.0.0] {_DASH} d\n\n## [1.0.0] {_DASH} d\n\nBody.\n"
        with pytest.raises(ReleaseError, match="empty"):
            extract_changelog_section(text, "2.0.0")

    def test_a_last_heading_with_no_content_is_an_error(self) -> None:
        with pytest.raises(ReleaseError, match="empty"):
            extract_changelog_section("## [1.0.0] - d\n\n\n  \n", "1.0.0")

    def test_the_real_changelog_yields_notes_for_its_newest_release(self) -> None:
        from core.version import __version__

        real = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
        notes = extract_changelog_section(real.read_text(encoding="utf-8"), __version__)
        assert notes.strip()
        assert not notes.lstrip().startswith("## [")
        assert "\n## [" not in notes


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo* as a fixed identity, whatever the machine's config is."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test Author",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, subject: str) -> str:
    """Make an empty commit whose subject is *subject*; return its SHA."""
    message = repo / ".git" / "COMMIT_MSG_TEST"
    message.write_text(subject + "\n", encoding="utf-8", newline="\n")
    _git(repo, "commit", "-q", "--allow-empty", "-F", str(message))
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repository shaped like this project's history around a release."""
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _commit(path, "feat: something before any release")
    return path


def _merge_release_branch(repo: Path, release_subject: str, merge_subject: str) -> str:
    """Merge a branch holding one release commit, as a merge commit; return its SHA."""
    _git(repo, "checkout", "-q", "-b", "chore/release-branch")
    _commit(repo, release_subject)
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "fix: landed on main while the release PR was open")
    message = repo / ".git" / "MERGE_MSG_TEST"
    message.write_text(merge_subject + "\n", encoding="utf-8", newline="\n")
    _git(repo, "merge", "-q", "--no-ff", "-F", str(message), "chore/release-branch")
    return _git(repo, "rev-parse", "HEAD")


class TestFindReleaseSummary:
    def test_finds_the_summary_on_the_head_commit(self, repo: Path) -> None:
        _commit(repo, f"chore(release): 1.0.0 {_DASH} first stable release")
        assert find_release_summary("1.0.0", "HEAD", repo) == "first stable release"

    def test_finds_it_behind_a_merge_commit(self, repo: Path) -> None:
        # The real shape: the target is the merge commit, and the release
        # commit is on the second parent, so first-parent history misses it.
        merge = _merge_release_branch(
            repo,
            f"chore(release): 1.1.0 {_DASH} one thing {_DASH} and another",
            "Merge pull request #1 from someone/chore/release-1.1.0",
        )
        assert (
            find_release_summary("1.1.0", merge, repo) == f"one thing {_DASH} and another"
        )

    def test_the_target_limits_the_search(self, repo: Path) -> None:
        before = _git(repo, "rev-parse", "HEAD")
        _commit(repo, f"chore(release): 1.2.0 {_DASH} later")
        with pytest.raises(ReleaseError, match="no commit reachable"):
            find_release_summary("1.2.0", before, repo)

    def test_the_most_recent_matching_commit_wins(self, repo: Path) -> None:
        _commit(repo, f"chore(release): 1.3.0 {_DASH} first attempt")
        _commit(repo, f"chore(release): 1.3.0 {_DASH} second attempt")
        assert find_release_summary("1.3.0", "HEAD", repo) == "second attempt"

    def test_not_found_names_what_was_searched_for(self, repo: Path) -> None:
        with pytest.raises(ReleaseError) as info:
            find_release_summary("4.5.6", "HEAD", repo)
        message = str(info.value)
        assert f"chore(release): 4.5.6 {_DASH} " in message
        assert "fetch-depth" in message

    def test_a_subject_for_a_different_version_is_not_a_match(self, repo: Path) -> None:
        _commit(repo, f"chore(release): 2.0.0 {_DASH} the other release")
        with pytest.raises(ReleaseError, match="no commit reachable"):
            find_release_summary("2.0.1", "HEAD", repo)
        with pytest.raises(ReleaseError, match="no commit reachable"):
            find_release_summary("2.0", "HEAD", repo)

    def test_a_version_is_not_matched_by_a_longer_one(self, repo: Path) -> None:
        _commit(repo, f"chore(release): 2.0.10 {_DASH} ten")
        with pytest.raises(ReleaseError, match="no commit reachable"):
            find_release_summary("2.0.1", "HEAD", repo)

    def test_only_a_subject_prefix_counts_not_a_mention(self, repo: Path) -> None:
        _commit(repo, f"docs: explain what chore(release): 3.0.0 {_DASH} x means")
        with pytest.raises(ReleaseError, match="no commit reachable"):
            find_release_summary("3.0.0", "HEAD", repo)

    def test_a_hyphen_is_not_the_separator(self, repo: Path) -> None:
        _commit(repo, "chore(release): 3.1.0 - written with a hyphen")
        with pytest.raises(ReleaseError, match="no commit reachable"):
            find_release_summary("3.1.0", "HEAD", repo)

    def test_the_body_of_the_commit_message_is_not_searched(self, repo: Path) -> None:
        _commit(repo, f"chore: tidy\n\nchore(release): 3.2.0 {_DASH} in the body")
        with pytest.raises(ReleaseError, match="no commit reachable"):
            find_release_summary("3.2.0", "HEAD", repo)

    def test_a_subject_with_nothing_after_the_dash_is_an_error(self, repo: Path) -> None:
        _commit(repo, f"chore(release): 3.3.0 {_DASH}  ")
        with pytest.raises(ReleaseError, match="no summary"):
            find_release_summary("3.3.0", "HEAD", repo)

    def test_an_unknown_target_is_an_error(self, repo: Path) -> None:
        with pytest.raises(ReleaseError, match="git log"):
            find_release_summary("1.0.0", "no-such-ref", repo)


class TestCommandLine:
    def test_version_prints_the_declared_version(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        version_file = _write(tmp_path / "version.py", '__version__ = "6.2.0"\n')
        assert main(["version", "--file", str(version_file)]) == 0
        assert capsys.readouterr().out == "6.2.0\n"

    def test_version_accepts_a_matching_expectation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        version_file = _write(tmp_path / "version.py", '__version__ = "6.2.0"\n')
        assert main(["version", "--file", str(version_file), "--expect", "6.2.0"]) == 0
        assert capsys.readouterr().out == "6.2.0\n"

    def test_version_rejects_a_mismatched_expectation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        version_file = _write(tmp_path / "version.py", '__version__ = "6.2.0"\n')
        assert main(["version", "--file", str(version_file), "--expect", "6.1.0"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "declares 6.2.0, not the requested 6.1.0" in captured.err

    def test_version_rejects_a_malformed_expectation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        version_file = _write(tmp_path / "version.py", '__version__ = "6.2.0"\n')
        assert main(["version", "--file", str(version_file), "--expect", "v6.2.0"]) == 1
        assert "X.Y.Z" in capsys.readouterr().err

    def test_prepare_writes_the_notes_and_prints_the_summary(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _commit(repo, f"chore(release): 2.0.0 {_DASH} the middle one")
        notes_file = tmp_path / "notes.md"
        code = main(
            [
                "prepare",
                "--version", "2.0.0",
                "--target", "HEAD",
                "--repo", str(repo),
                "--changelog", str(_write(tmp_path / "CHANGELOG.md", _CHANGELOG)),
                "--notes-file", str(notes_file),
            ]
        )  # fmt: skip
        assert code == 0
        assert capsys.readouterr().out == "the middle one\n"
        assert notes_file.read_text(encoding="utf-8") == (
            "Middle summary line.\n\n### Fixed\n\n- Second.\n"
        )

    def test_prepare_fails_without_a_release_commit(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        notes_file = tmp_path / "notes.md"
        code = main(
            [
                "prepare",
                "--version", "2.0.0",
                "--target", "HEAD",
                "--repo", str(repo),
                "--changelog", str(_write(tmp_path / "CHANGELOG.md", _CHANGELOG)),
                "--notes-file", str(notes_file),
            ]
        )  # fmt: skip
        assert code == 1
        assert "no commit reachable" in capsys.readouterr().err
        assert not notes_file.exists()

    def test_prepare_fails_on_a_missing_changelog_section(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _commit(repo, f"chore(release): 7.0.0 {_DASH} not in the changelog")
        notes_file = tmp_path / "notes.md"
        code = main(
            [
                "prepare",
                "--version", "7.0.0",
                "--target", "HEAD",
                "--repo", str(repo),
                "--changelog", str(_write(tmp_path / "CHANGELOG.md", _CHANGELOG)),
                "--notes-file", str(notes_file),
            ]
        )  # fmt: skip
        assert code == 1
        assert "no '## [7.0.0]' section" in capsys.readouterr().err
        assert not notes_file.exists()

    def test_prepare_rejects_a_malformed_version(
        self, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            [
                "prepare",
                "--version", "v2.0.0",
                "--target", "HEAD",
                "--repo", str(repo),
                "--notes-file", str(tmp_path / "notes.md"),
            ]
        )  # fmt: skip
        assert code == 1
        assert "X.Y.Z" in capsys.readouterr().err

    def test_errors_become_github_annotations_on_a_runner(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        assert main(["version", "--file", str(tmp_path / "absent.py")]) == 1
        assert "::error title=Release::" in capsys.readouterr().err
