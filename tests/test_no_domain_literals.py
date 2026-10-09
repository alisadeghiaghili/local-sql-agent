# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Whole-tree guard: no real deployment identifier may appear anywhere in
the tracked tree -- in a file's *contents* or in its *path*.

Why this replaces the old, narrower guard
-------------------------------------------
An earlier version of this module (see git history) was an AST-based,
first-party-Python-only scanner that treated docstrings/doctests as a safe
carve-out from a small, literal wordlist. That shape cannot do the job
this module now does: prove that none of a fixed set of real-deployment
identifiers survives anywhere in this repository, in any tracked file, in
any format (Markdown, YAML, JS, JSON-lines, ...), in any casing -- not
just inside first-party Python source, and not just in string literals. A
format-specific, docstring-aware AST scanner is the wrong tool for a
full-repo, format-mixed sweep; a plain text/path scan over every tracked
file is the right one, so that is what replaced it.

What counts as a violation
---------------------------
Six real identifiers -- fixed, documented only with the project's owner,
never spelled out in this file (see "Why hashes, not a literal list"
below) -- must never reappear in this tree, in any casing, in file
*contents* or in a *path component* of any tracked file's path.

Deliberately a short, unmistakable set, not a wordlist. ``Order``,
``Customer``, ``Date`` and similar are ordinary English words;
``project_config.example/``'s own generic schema uses several of them for
its placeholder tables. A wordlist broad enough to catch domain
*concepts* would flag those too, produce constant noise, get disabled
within a month, and then protect nothing -- exactly the failure mode this
module's own predecessor's docstring warned against, now avoided by
scoping to whole, reconstructed compounds only (see "Tokenising and
windowing").

Why hashes, not a literal list
-------------------------------
Storing the six identifiers as literal strings in this module would
violate, inside the very file meant to enforce it, the same "none of
these six may appear anywhere in the tracked tree" invariant this module
exists to check. Instead, :data:`_FORBIDDEN_HASHES` holds only the
**salted SHA-256 digest** of each identifier's normalised (lower-cased)
whole-form variants -- never a bare constituent piece on its own (see
"Tokenising and windowing") and never a human-readable label next to the
hash anywhere in this module. A match is reported as ``path:line:
forbidden identifier (hash <12-hex-prefix>)`` -- the digest prefix shown
is generated fresh from the *matched text* at report time, not looked up
in any name table, so nothing in this module's source, or its own output,
ever maps a hash back to a name.

:data:`_SALT` is a fixed, arbitrary, checked-in constant, documented here
rather than hidden -- the goal is not cryptographic secrecy (anyone with
this file and a candidate string can verify a guess in milliseconds; that
is fine and expected), only that the digests below are not themselves
directly recognisable as "the plain SHA-256 of a well-known word" by a
casual reader. Rotating the salt invalidates every digest below and
requires regenerating all of them together, offline, from the six real
identifiers (never committed in that form) via :func:`_hash_token`. The
self-test (:class:`TestSelfTestDetectsAViolation`) never hardcodes an
expected digest -- it generates its own synthetic one through the same
function the forbidden-set builder uses -- so it keeps testing the real
tokenise-to-hash-to-compare pipeline even after a salt rotation, rather
than silently passing against a stale expectation.

Tokenising and windowing
--------------------------
A scanned file's content (and, separately, each path *component* of its
own tracked path) is split into maximal identifier-like runs
(``[A-Za-z_][A-Za-z0-9_]*``) -- never crossing whitespace, punctuation, or
a bracket/dot boundary. Each run is then split into pieces on ``_`` and
CamelCase boundaries (so a run spelled ``FooBar`` splits into
``["Foo", "Bar"]``; ``foo_bar`` splits into ``["foo", "bar"]``). For every
start index and window length 1-3 *within that one run's own piece list*,
the window is joined both with no separator and with ``_``, lower-cased,
and hashed with :func:`_hash_token`; a hit is any window whose hash is in
:data:`_FORBIDDEN_HASHES`.

Only a **window of the scanned text** is ever hashed and compared against
the forbidden set -- the forbidden set itself holds only each identifier's
full, reconstructed whole form (see :data:`_FORBIDDEN_HASHES`'s own
generation recipe above), never a single bare piece added on its own. A
lone ordinary English word -- one that could be a single piece of one of
the six identifiers -- never matches by itself; only an actual compound,
written as one contiguous run in the scanned text, does. This is also why
two unrelated words that happen to sit next to each other in a sentence
("...the Customer table and the Order table...") never combine into a
false positive: they are two separate *runs*, split by whitespace, never
re-joined into one candidate string.

Scope
-----
Every file ``git ls-files -z`` reports, from the repository root -- both
its content and its path. If git is unavailable (no ``git`` on ``PATH``,
or the command fails -- e.g. a source tarball extracted with no ``.git/``
directory), this module falls back to a plain filesystem walk from the
repository root, excluding a short, explicit directory denylist
(:data:`_WALK_EXCLUDE_DIRS`) -- deliberately a *superset* of what git
would report (it also picks up local, untracked files), which is the safe
direction to err in for a leak-prevention guard: over-scanning costs a
little time, under-scanning defeats the point. This is a degraded,
best-effort mode, not a silent no-op: :class:`TestFallbackWalk` exercises
it directly rather than leaving it untested.

A file that cannot be decoded as UTF-8 (binary) is skipped, not failed --
see :func:`_read_lines`. A CRLF file is handled the same as an LF one
(``Path.read_text`` performs universal-newline translation on read).

Allowlist
---------
:data:`_ALLOWED_HASH_LOCATIONS` -- keyed ``"relative/path::lineno"``, each
entry individually justified in a comment -- covers a *content* hit that
is real but, on inspection, not a leak worth failing the build over. It
starts empty: given SHA-256's collision space, an innocent word's hash
coinciding with one of the eleven forbidden ones is not a practical risk,
so this mechanism is expected to stay empty. It is kept only because a
hash-based guard having *no* allowlist escape hatch at all would be a
worse failure mode than "the escape hatch is unused" --
:class:`TestAllowlistSelfCheck` fails the build if any entry ever stops
matching anything (stale coverage silently masking a different, real hit
that later lands on the same line). A *path* hit has no allowlist
mechanism at all -- a forbidden identifier has no legitimate reason to be
part of a tracked file's own path, so a path hit always fails.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: See the module docstring's "Why hashes, not a literal list". Rotating
#: this invalidates every digest in _FORBIDDEN_HASHES; regenerate all of
#: them together, offline, from the six real identifiers via
#: _hash_token, and paste the new hex digests in below.
_SALT = "local-sql-agent:domain-identifier-guard:v1"


def _hash_token(token: str) -> str:
    """Salted SHA-256 hex digest of *token*, lower-cased first.

    The single point every digest in this module passes through -- the
    eleven forbidden ones below and every synthetic one a test generates
    -- so rotating :data:`_SALT` only ever requires re-deriving
    :data:`_FORBIDDEN_HASHES` from the six real identifiers, never
    touching this function or any call site.
    """
    return hashlib.sha256((_SALT + token.lower()).encode("utf-8")).hexdigest()


#: The eleven salted digests of the six real identifiers' whole-form
#: variants (lower-cased; joined with no separator and, for the five
#: two-piece identifiers, also joined with "_" -- the two forms collapse
#: to one digest for the single-piece identifier, hence eleven, not
#: twelve). Generated once, offline, from the six real identifiers
#: themselves (never committed in that form) via _hash_token, and pasted
#: here as opaque hex strings -- see the module docstring's "Why hashes,
#: not a literal list" for why this is the only form they may take here.
_FORBIDDEN_HASHES: frozenset[str] = frozenset(
    {
        "127177c1867541d64186eb1ec999bb002aa8f8290849249e0e90e56a90d77411",
        "1bcb4ca270fe5a8864d53608b342ff729a071e017ae5f7edb341b60f6a47c42c",
        "1ffd8b3a98038f9f174f2420d5e4cbfec0099ef6ca9d39ad1b6788a76b6b3209",
        "39e7913709c5820f75afad41a1642c6af673ce76b45abae0c31054afc7fd99c5",
        "421fd2aa197f9aaa4a429dfa47071278ae26a1eb76452ac519119ec1496d34f5",
        "77945af453a9022617bb644323dbb20720538c94e8e541b0043369be8785bf99",
        "796f780ffc4a07badcd9a29d5f07900ba1cbf8c478597e2954faf19c67a324a2",
        "a0052515f82bf7fa17e3c8b3e8ad690bb8490aa717d0343a82c0a89ae210a238",
        "aaabef23cada69e5b5e574d019533931bea9357216fad2ec93aaa8fad563ce03",
        "da10e3accf3ac837f0e5892c8d83d759e3cf472568f2c6efa8c4a16bf27e1bf0",
        "fe706d801c6171c9f51c65f37616b8504a631f33d03c03a477b6f8fbd80f0026",
    }
)

#: ``(length, first_char_ord, last_char_ord)`` signatures of the eleven
#: forbidden variants, all on the lower-cased candidate. Disclosing this
#: leaks nothing usable about content (countless ordinary words/compounds
#: share any given length + first/last letter -- it is a coarse filter,
#: not a hash of anything secret, and it is never used as the actual
#: comparison; see below), but it is what makes the scan fast: length
#: alone (only 8 distinct values, spanning the range ordinary source
#: identifiers cluster in) passes the large majority of candidates
#: through on a real codebase, which does not save anything. Adding the
#: first and last character as two more, independent dimensions cuts the
#: false-positive rate low enough that a candidate is joined into a
#: string and salt-hashed only on an actual near-match, not on nearly
#: every ordinarily-sized identifier piece -- and, unlike a full
#: character-sum, both are O(1) per window to compute (a piece's own
#: first/last character, precomputed once per piece in
#: :func:`_hashes_in_text` and reused across every window it appears in),
#: never requiring an O(piece length) pass over the piece's middle. See
#: TestForbiddenIdentifiersNeverAppear.test_scan_completes_in_a_few_seconds
#: for why this matters (a whole-tree scan with no such filter, with
#: length alone, and with a full running character-sum, were all measured
#: well over budget on this repository). The real forbidden-set
#: comparison is always the full salted SHA-256 against
#: :data:`_FORBIDDEN_HASHES` -- this signature is only ever used to
#: decide whether that comparison is worth attempting; a signature
#: collision costs one wasted hash, never a missed real match, since it
#: is derived deterministically, by the same formula, from the same six
#: real identifiers that produced :data:`_FORBIDDEN_HASHES` itself.
_FORBIDDEN_SIGNATURES: frozenset[tuple[int, int, int]] = frozenset(
    {
        (5, 116, 114),
        (10, 97, 109),
        (10, 103, 109),
        (11, 97, 109),
        (11, 97, 116),
        (11, 103, 109),
        (12, 97, 116),
        (13, 100, 101),
        (14, 100, 101),
        (16, 99, 116),
        (17, 99, 116),
    }
)

#: One content hit that is real but individually judged not worth failing
#: the build over, keyed "relative/path::lineno". Starts empty -- see the
#: module docstring's "Allowlist" section for why, and why it has no path
#: (as opposed to content) equivalent.
_ALLOWED_HASH_LOCATIONS: dict[str, str] = {}

#: Identifier-like runs: letters/digits/underscore, never crossing
#: whitespace, punctuation, or a bracket/dot boundary.
_RUN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Splits one run into pieces on "_" and CamelCase boundaries.
#:
#: Order matters: regex alternation takes the first alternative that
#: matches at a position, not the longest, so the whole-acronym
#: alternative (``[A-Z]+(?![a-z])``) must come *before*
#: ``[A-Z][a-z0-9]*`` -- otherwise the single-capital alternative wins at
#: every position of an all-caps run (e.g. a synthetic "FOO_BAR") and
#: shatters it into one-character pieces ("F", "O", "O", ...), so a whole
#: all-caps identifier never reassembles into a window long enough to
#: match :data:`_FORBIDDEN_HASHES`, silently breaking the guard's
#: documented case-insensitivity for any all-caps spelling of a
#: forbidden identifier. With this order, "FOO_BAR" splits into
#: ["FOO", "BAR"] (each matched whole by the acronym alternative, since
#: nothing lower-case ever follows within the run), while "FooBar" still
#: splits into ["Foo", "Bar"] as before (at each capital, the acronym
#: alternative's negative lookahead fails against the following
#: lower-case letter, so the engine falls through to the single-capital
#: alternative) and "SQLConnection" still splits into
#: ["SQL", "Connection"].
_PIECE_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

#: Extensions that are never worth attempting a UTF-8 decode of -- a fast
#: pre-filter; the real safety net is the try/except in _read_lines, so
#: this list does not need to be exhaustive.
_BINARY_EXTENSIONS = frozenset(
    {
        ".zip", ".gz", ".tar", ".7z", ".rar",
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".db", ".sqlite", ".sqlite3",
        ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe",
        ".pdf",
    }
)

#: Directories excluded from the fallback filesystem walk (git-unavailable
#: path only) -- a superset of what git would report, deliberately.
_WALK_EXCLUDE_DIRS = frozenset(
    {
        ".git", "__pycache__", ".venv", "venv", "node_modules",
        ".pytest_cache", "htmlcov", "dist", "build",
        "project_config", "project_config_draft", "eval_data",
        "exports", "logs",
    }
)


def _split_pieces(run: str) -> list[str]:
    """Split one identifier-like run into its "_"/CamelCase pieces."""
    pieces: list[str] = []
    for part in run.split("_"):
        if part:
            pieces.extend(_PIECE_RE.findall(part))
    return pieces


def _hashes_in_text(
    text: str,
    signatures: frozenset[tuple[int, int, int]] = _FORBIDDEN_SIGNATURES,
) -> set[str]:
    """Every window-hash produced by scanning *text*.

    Applies the tokenise -> split-into-pieces -> slide 1/2/3-piece
    windows -> join (both ways) -> lower-case -> hash pipeline described
    in the module docstring's "Tokenising and windowing", and returns
    *every* resulting digest whose candidate ``(length, first_char_ord,
    last_char_ord)`` signature is in *signatures* -- callers intersect the
    result with whichever forbidden set they care about
    (:data:`_FORBIDDEN_HASHES` for the real guard, or a synthetic one-off
    set in a test, in which case they also pass that set's own candidate
    signatures here), rather than this function hardcoding which set
    matters. Used for both file *contents* (one call per line) and file
    *paths* (one call per path component).

    *signatures* is a pre-filter, applied before any hashing -- indeed
    before any string is even built -- for each window: a window's first
    and last character come straight from its first and last *piece*'s
    own precomputed first/last character (an O(1) lookup, never an
    O(piece length) scan of the piece's middle), and its length is a
    running integer sum, so a non-matching window (the overwhelming
    majority, on ordinary source text) costs a couple of integer
    additions and one frozenset lookup, never a slice/join/lower-case/
    hash. See :data:`_FORBIDDEN_SIGNATURES`'s own docstring for why
    length alone was not selective enough, and
    TestForbiddenIdentifiersNeverAppear.test_scan_completes_in_a_few_seconds
    for the measured budget this keeps the whole-tree scan inside.
    """
    hashes: set[str] = set()
    for run_match in _RUN_RE.finditer(text):
        pieces = _split_pieces(run_match.group())
        n = len(pieces)
        if n == 0:
            continue
        piece_lens = [len(p) for p in pieces]
        piece_firsts = [ord(p[0].lower()) for p in pieces]
        piece_lasts = [ord(p[-1].lower()) for p in pieces]
        for i in range(n):
            first_ord = piece_firsts[i]
            joined_len = 0
            for length in (1, 2, 3):
                j = i + length
                if j > n:
                    break
                joined_len += piece_lens[j - 1]
                last_ord = piece_lasts[j - 1]
                if (joined_len, first_ord, last_ord) in signatures:
                    hashes.add(_hash_token("".join(pieces[i:j])))
                if length > 1:
                    underscored_len = joined_len + (length - 1)
                    if (underscored_len, first_ord, last_ord) in signatures:
                        hashes.add(_hash_token("_".join(pieces[i:j])))
    return hashes


def _git_tracked_files() -> list[str]:
    """Every path ``git ls-files -z`` reports, relative to the repo root.

    Returns an empty list (never raises) when git is unavailable or the
    command fails, so :func:`_iter_tracked_paths` can fall back to a
    filesystem walk instead of erroring out.
    """
    if shutil.which("git") is None:
        return []
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=_REPO_ROOT,
            capture_output=True,
            timeout=30,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    raw = result.stdout.decode("utf-8", errors="surrogateescape")
    return [p for p in raw.split("\0") if p]


def _walk_tracked_files() -> list[str]:
    """Fallback file list when git is unavailable -- see the module
    docstring's "Scope" section for why this is a deliberate superset."""
    paths: list[str] = []
    for path in _REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(_REPO_ROOT)
        if any(part in _WALK_EXCLUDE_DIRS for part in rel.parts):
            continue
        paths.append(rel.as_posix())
    return paths


def _iter_tracked_paths(use_git: bool = True) -> list[str]:
    if use_git:
        paths = _git_tracked_files()
        if paths:
            return paths
    return _walk_tracked_files()


def _read_lines(path: Path) -> list[str] | None:
    """*path*'s lines, or ``None`` if it cannot be decoded as UTF-8.

    A binary file is skipped, not failed -- see the module docstring's
    "Scope" section. ``Path.read_text`` performs universal-newline
    translation, so a CRLF file is handled the same as an LF one with no
    special-casing needed here.
    """
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return None
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return None


def _content_hits(
    relpaths: list[str], forbidden: frozenset[str] = _FORBIDDEN_HASHES
) -> list[tuple[str, int, str]]:
    """``(relpath, lineno, digest)`` for every non-allowlisted content hit."""
    hits: list[tuple[str, int, str]] = []
    for relpath in relpaths:
        lines = _read_lines(_REPO_ROOT / relpath)
        if lines is None:
            continue
        for lineno, line in enumerate(lines, start=1):
            for digest in _hashes_in_text(line) & forbidden:
                if f"{relpath}::{lineno}" in _ALLOWED_HASH_LOCATIONS:
                    continue
                hits.append((relpath, lineno, digest))
    return hits


def _path_hits(
    relpaths: list[str], forbidden: frozenset[str] = _FORBIDDEN_HASHES
) -> list[tuple[str, str]]:
    """``(relpath, digest)`` for every tracked path with a forbidden
    identifier in one of its own components (directory name or filename).

    No allowlist applies here -- see the module docstring's "Allowlist"
    section.
    """
    hits: list[tuple[str, str]] = []
    for relpath in relpaths:
        for component in Path(relpath).parts:
            for digest in _hashes_in_text(component) & forbidden:
                hits.append((relpath, digest))
    return hits


def _format_hit(relpath: str, lineno: int, digest: str) -> str:
    # Never the identifier, never which of the eleven variants matched --
    # only a hash prefix generated fresh from the match itself.
    return f"{relpath}:{lineno}: forbidden identifier (hash {digest[:12]})"


def _format_path_hit(relpath: str, digest: str) -> str:
    return f"{relpath}: forbidden identifier in path (hash {digest[:12]})"


# ---------------------------------------------------------------------------
# Discovery sanity check
# ---------------------------------------------------------------------------


_TRACKED_PATHS = _iter_tracked_paths()


def test_git_ls_files_is_available() -> None:
    """Without this, an unavailable/broken git would make every check
    below vacuously pass with an empty or tiny file list -- exactly the
    kind of silent, always-green failure this module exists to prevent."""
    if not _TRACKED_PATHS:
        pytest.skip("git is unavailable or this checkout is not a git repository")
    assert len(_TRACKED_PATHS) > 100, (
        f"expected the repository's full tracked-file set, found "
        f"{len(_TRACKED_PATHS)}"
    )


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


class TestForbiddenIdentifiersNeverAppear:
    def test_no_forbidden_identifier_in_tracked_file_contents(self) -> None:
        hits = _content_hits(_TRACKED_PATHS)
        assert hits == [], "\n".join(_format_hit(*hit) for hit in hits)

    def test_no_forbidden_identifier_in_tracked_file_paths(self) -> None:
        hits = _path_hits(_TRACKED_PATHS)
        assert hits == [], "\n".join(_format_path_hit(*hit) for hit in hits)

    def test_scan_completes_in_a_few_seconds(self) -> None:
        """~a few hundred tracked files, a couple of cheap regex passes
        and an O(1)-per-window signature pre-filter (see
        :data:`_FORBIDDEN_SIGNATURES`) -- low-single-digit seconds in
        isolation, even on a slower CI runner, including on Windows.
        Measured in isolation on this repository (Windows, this module
        alone): ~5s. Run at the end of the full suite, on a machine
        already busy running ~3000 other tests, wall-clock time for the
        exact same computation was measured up to ~11s -- environmental
        noise (disk cache pressure, CPU contention, GC), not a slower
        algorithm; the assertion below is deliberately generous about
        that noise rather than tuned to the isolated figure, so this
        test does not flake under a busy CI runner while still catching
        a genuine multi-minute regression. Prints the measured wall-clock
        time to stdout (visible with `pytest -s` or in CI logs) so a real
        regression is easy to see, not just implicitly bounded by the
        assertion."""
        start = time.perf_counter()
        _content_hits(_TRACKED_PATHS)
        _path_hits(_TRACKED_PATHS)
        elapsed = time.perf_counter() - start
        print(f"\ndomain-literal guard: full-tree scan took {elapsed:.3f}s")
        assert elapsed < 30.0, (
            f"whole-tree scan took {elapsed:.3f}s -- expected a few "
            f"seconds (tens of seconds at most, even under a busy CI "
            f"runner); investigate before this becomes a CI-time problem"
        )


class TestHandlesAwkwardFilesWithoutCrashing:
    def test_crlf_file_scans_cleanly(self, tmp_path: Path) -> None:
        f = tmp_path / "crlf.txt"
        f.write_bytes(b"line one\r\nline two\r\nline three\r\n")
        # Must not raise, and universal-newline translation on read means
        # a CRLF file's lines come back exactly like an LF file's would.
        assert _read_lines(f) == ["line one", "line two", "line three"]

    def test_non_utf8_file_is_skipped_not_failed(self, tmp_path: Path) -> None:
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\xff\xfe\x00\x01\x02not-utf8\x80\x81")
        assert _read_lines(f) is None

    def test_denylisted_extension_is_skipped_without_reading(self, tmp_path: Path) -> None:
        f = tmp_path / "demo.zip"
        f.write_bytes(b"PK\x03\x04garbage-not-a-real-zip")
        assert _read_lines(f) is None


class TestAllowlistSelfCheck:
    def test_allowlist_entries_still_exist(self) -> None:
        """Every allowlisted location must still produce a hit -- an
        entry that matches nothing any more is silently permitting
        nothing (or, worse, a *different* hit that happens to land on the
        same line after an unrelated edit), which would mask a real new
        violation instead of covering the one it was triaged for."""
        # _content_hits filters allowlisted lines out, so re-scan without
        # that filter to see what each allowlist entry actually covers
        # today.
        raw_hits = set()
        for relpath in _TRACKED_PATHS:
            lines = _read_lines(_REPO_ROOT / relpath)
            if lines is None:
                continue
            for lineno, line in enumerate(lines, start=1):
                if _hashes_in_text(line) & _FORBIDDEN_HASHES:
                    raw_hits.add(f"{relpath}::{lineno}")
        stale = sorted(set(_ALLOWED_HASH_LOCATIONS) - raw_hits)
        assert stale == [], (
            f"Allowlist entries no longer found: {stale}. The hit was "
            f"moved, rewritten, or removed -- re-triage the new location "
            f"(or remove the stale entry) rather than leaving it in "
            f"_ALLOWED_HASH_LOCATIONS."
        )


class TestSelfTestDetectsAViolation:
    """Plants a synthetic, never-real identifier and proves the whole
    tokenise -> window -> hash -> compare pipeline detects it end to end,
    independent of whether any of the eleven real hashes currently
    matches anything in the tree (after the scrub, none should)."""

    _SYNTHETIC_PIECES = ["Zephyr", "Quanta"]

    def _synthetic_forbidden(self) -> frozenset[str]:
        joined_us = "_".join(self._SYNTHETIC_PIECES).lower()
        joined_none = "".join(self._SYNTHETIC_PIECES).lower()
        return frozenset({_hash_token(joined_us), _hash_token(joined_none)})

    def _synthetic_signatures(self) -> frozenset[tuple[int, int, int]]:
        joined_us = "_".join(self._SYNTHETIC_PIECES).lower()
        joined_none = "".join(self._SYNTHETIC_PIECES).lower()
        return frozenset(
            {
                (len(joined_us), ord(joined_us[0]), ord(joined_us[-1])),
                (len(joined_none), ord(joined_none[0]), ord(joined_none[-1])),
            }
        )

    def test_detects_synthetic_identifier_in_content(self, tmp_path: Path) -> None:
        target = tmp_path / "planted.py"
        target.write_text(
            "value = 'Zephyr_Quanta'  # a synthetic, never-real identifier\n",
            encoding="utf-8",
        )
        synthetic = self._synthetic_forbidden()
        signatures = self._synthetic_signatures()
        lines = _read_lines(target)
        assert lines is not None
        # A single planted compound run legitimately produces two matching
        # digests here (the "_"-joined and no-separator variants of the
        # SAME two-piece window both match "Zephyr_Quanta" itself) -- what
        # matters is that they land on exactly one, correct line, not that
        # there is exactly one digest.
        found_lines = {
            lineno
            for lineno, line in enumerate(lines, start=1)
            if _hashes_in_text(line, signatures) & synthetic
        }
        assert found_lines == {1}

    def test_detects_synthetic_identifier_split_camelcase(self) -> None:
        synthetic = self._synthetic_forbidden()
        signatures = self._synthetic_signatures()
        assert _hashes_in_text("ZephyrQuanta", signatures) & synthetic

    def test_detects_synthetic_identifier_in_path(self, tmp_path: Path) -> None:
        planted_dir = tmp_path / "zephyr_quanta"
        planted_dir.mkdir()
        planted_file = planted_dir / "module.py"
        planted_file.write_text("x = 1\n", encoding="utf-8")
        synthetic = self._synthetic_forbidden()
        signatures = self._synthetic_signatures()
        component_hits = {
            component: _hashes_in_text(component, signatures) & synthetic
            for component in planted_file.parts
        }
        assert any(component_hits.values())

    def test_detects_synthetic_identifier_all_caps(self, tmp_path: Path) -> None:
        """Regression guard for the piece-splitter's alternation order:
        an ALL-CAPS spelling of a multi-piece identifier must be
        detected exactly like its PascalCase and lower_case forms are,
        both in content and in a path component. Before the
        ``_PIECE_RE`` alternative ordering fix, "ZEPHYR_QUANTA" was
        shattered into single-character pieces and never matched."""
        synthetic = self._synthetic_forbidden()
        signatures = self._synthetic_signatures()

        target = tmp_path / "planted_caps.py"
        target.write_text(
            "value = 'ZEPHYR_QUANTA'  # a synthetic, never-real identifier\n",
            encoding="utf-8",
        )
        lines = _read_lines(target)
        assert lines is not None
        found_lines = {
            lineno
            for lineno, line in enumerate(lines, start=1)
            if _hashes_in_text(line, signatures) & synthetic
        }
        assert found_lines == {1}

        assert _hashes_in_text("ZEPHYRQUANTA", signatures) & synthetic

        planted_dir = tmp_path / "ZEPHYR_QUANTA"
        planted_dir.mkdir()
        planted_file = planted_dir / "module.py"
        planted_file.write_text("x = 1\n", encoding="utf-8")
        component_hits = {
            component: _hashes_in_text(component, signatures) & synthetic
            for component in planted_file.parts
        }
        assert any(component_hits.values())

    def test_does_not_flag_a_single_synthetic_piece_alone(self) -> None:
        """The refinement in the module docstring's "Tokenising and
        windowing": a lone piece of a multi-piece identifier is never
        itself in the forbidden set, so scanning just "Zephyr" or just
        "Quanta" alone must never match the synthetic pair's hashes.

        Passes each single piece's OWN signature (rather than
        :meth:`_synthetic_signatures`, which only covers the two-piece
        compound) so hashing of that lone piece is forced to happen --
        this proves the *hashing pipeline* itself never adds a bare
        single piece to a multi-piece forbidden set, not merely that a
        6-character candidate happens to be filtered out by signature
        before hashing is even attempted."""
        synthetic = self._synthetic_forbidden()
        zephyr_signature = frozenset({(len("zephyr"), ord("z"), ord("r"))})
        quanta_signature = frozenset({(len("quanta"), ord("q"), ord("a"))})
        assert not (_hashes_in_text("Zephyr", zephyr_signature) & synthetic)
        assert not (_hashes_in_text("Quanta", quanta_signature) & synthetic)


class TestFallbackWalk:
    def test_fallback_walk_finds_tracked_files(self) -> None:
        paths = _walk_tracked_files()
        assert len(paths) > 100

    def test_fallback_walk_excludes_denylisted_directories(self, tmp_path: Path) -> None:
        # Not exercised against the real repo root (too slow / noisy to
        # rely on real .git internals here) -- proves the exclusion logic
        # itself against a small synthetic tree.
        (tmp_path / "keep").mkdir()
        (tmp_path / "keep" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "b.js").write_text("x = 1\n", encoding="utf-8")

        found = []
        for path in tmp_path.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(tmp_path)
            if any(part in _WALK_EXCLUDE_DIRS for part in rel.parts):
                continue
            found.append(rel.as_posix())
        assert found == ["keep/a.py"]

    def test_iter_tracked_paths_falls_back_when_git_unavailable(self) -> None:
        paths = _iter_tracked_paths(use_git=False)
        assert len(paths) > 100
