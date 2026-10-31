# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Find the lines of a ``.env`` file that python-dotenv could not use.

python-dotenv loads what it can parse and skips the rest with a one-line
warning on stderr, which is easy to miss. A pretty-printed ``API_KEYS_JSON``
that is not wrapped in single quotes (or has an apostrophe inside a value)
is skipped that way, its leftover lines are read as variables named ``]``
or ``{"id":"x",``, and the server then reports that no usable key is
configured -- true, but nowhere near the cause. A variable assigned twice
silently takes the later value, which hides a stale line the same way.

:func:`scan_dotenv` reads a file with python-dotenv's own parser and
returns what it could not use; ``config.Settings.validate`` refuses to
start on a non-empty result.

No ``.env`` value is ever read into a message here: a problem carries line
numbers and variable names only, because this file holds passwords.

A tool that needs a variable it cannot find (a data source's
``password_env``, say) calls :func:`explain_unset` for the likely reason:
no ``.env`` was found at all (a ``.env.txt`` that a file manager hiding
extensions saved is named), the file has lines python-dotenv skipped, the
variable is not in it (or is spelled with different case, where names are
case-sensitive), it is empty, blank or built from ``${...}`` references
there, it is there but missing from the process environment (the process
predates the edit, or another ``.env`` was loaded), or the process
environment holds an empty or blank value for it, which python-dotenv never
overrides.

Public API
----------
DotenvProblem
scan_dotenv(path) -> list[DotenvProblem]
format_problems(path, problems) -> str
explain_unset(variable, path, problems, environ, search_dir, *, disabled=False,
               case_sensitive=os.name != "nt") -> list[str]
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Iterator, Literal

from dotenv.parser import Binding, parse_stream

__all__ = ["DotenvProblem", "explain_unset", "format_problems", "scan_dotenv"]

#: What a shell, a container runtime and ``os.environ`` agree is a variable name.
_VALID_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: ``NAME=`` (optionally ``export NAME=``) at the start of a line python-dotenv
#: rejected. Only the leading identifier is captured, never the value.
_ASSIGNED_NAME_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

_NEWLINE_RE = re.compile(r"\r\n|\n|\r")

#: What "UTF-8 with BOM" puts at the start of a file. python-dotenv opens the
#: file as plain utf-8, so it stays in the text, glued to the first name.
_BOM = "\ufeff"

Kind = Literal[
    "unparsable", "invalid_name", "cut_off", "reassigned", "bom", "unreadable"
]


@dataclass(frozen=True)
class DotenvProblem:
    """One ``.env`` line (or variable) python-dotenv could not use as intended.

    Attributes
    ----------
    kind:
        ``"unparsable"`` (python-dotenv rejected the line),
        ``"invalid_name"`` (it parsed, but the name is not a valid
        environment variable -- typically a leftover line of a broken
        multi-line value), ``"cut_off"`` (an unquoted value that is just
        ``[`` or ``{`` and is followed by such a leftover line: the start of
        a multi-line value that was not wrapped in single quotes),
        ``"reassigned"`` (same name assigned more than
        once with different values), ``"bom"`` (the file starts with a
        byte-order mark that python-dotenv left glued to its first
        assignment, so it sets a variable whose name begins with U+FEFF and
        the real name is missing; releases that strip the mark themselves,
        and a mark before a comment or blank line, lose nothing and are not
        reported) or ``"unreadable"`` (the file itself
        could not be read).
    line:
        1-based line the problem starts on; the first assignment for
        ``"reassigned"``; always ``1`` for ``"bom"``; ``0`` for
        ``"unreadable"``.
    name:
        The variable name when one is known; never the value, and never the
        text of an invalid name. For ``"bom"`` it is the variable that is
        missing, without the byte-order mark.
    lines:
        Every line that assigns *name*, in file order. Only for
        ``"reassigned"``; the last one is the value that wins.

    Examples
    --------
    >>> DotenvProblem("unparsable", 3, "API_KEYS_JSON").message
    'line 3: could not be parsed (looks like an assignment to API_KEYS_JSON)'
    >>> DotenvProblem("cut_off", 1, "API_KEYS_JSON").message
    'line 1: API_KEYS_JSON ends at the line break, so the lines after it are not part of its value'
    >>> DotenvProblem("reassigned", 2, "DB_HOST", (2, 9)).message
    'DB_HOST is assigned on lines 2 and 9 with different values; the last one (line 9) wins'
    >>> DotenvProblem("bom", 1, "OPENAI_MODEL").message
    'line 1: the file starts with a byte-order mark (BOM), so the first variable (OPENAI_MODEL) is read under the wrong name -- save .env as UTF-8 without BOM'
    """

    kind: Kind
    line: int
    name: str | None = None
    lines: tuple[int, ...] = ()

    @property
    def message(self) -> str:
        """One human-readable line; line numbers and names only."""
        if self.kind == "unparsable":
            hint = f" (looks like an assignment to {self.name})" if self.name else ""
            return f"line {self.line}: could not be parsed{hint}"
        if self.kind == "invalid_name":
            return (
                f"line {self.line}: not a NAME=value assignment -- usually the "
                "leftover of a multi-line value that broke"
            )
        if self.kind == "cut_off":
            return (
                f"line {self.line}: {self.name} ends at the line break, so the "
                "lines after it are not part of its value"
            )
        if self.kind == "bom":
            which = f" ({self.name})" if self.name else ""
            return (
                f"line {self.line}: the file starts with a byte-order mark (BOM), "
                f"so the first variable{which} is read under the wrong name -- "
                "save .env as UTF-8 without BOM"
            )
        if self.kind == "reassigned":
            *head, last = self.lines
            where = ", ".join(str(n) for n in head)
            where = f"{where} and {last}" if len(head) == 1 else f"{where}, and {last}"
            return (
                f"{self.name} is assigned on lines {where} with different values; "
                f"the last one (line {last}) wins"
            )
        return "the file could not be read as UTF-8 text"


def _start_line(string: str, line: int) -> int:
    """The line a binding's text really starts on.

    python-dotenv marks a binding before it skips blank lines, so its
    ``original.line`` points at the blank lines above an assignment, not at
    the assignment.
    """
    leading = string[: len(string) - len(string.lstrip())]
    return line + len(_NEWLINE_RE.findall(leading))


def _unparsable(string: str, line: int) -> DotenvProblem:
    match = _ASSIGNED_NAME_RE.match(string)
    return DotenvProblem("unparsable", line, match.group(1) if match else None)


def _bom_problems(binding: Binding) -> list[DotenvProblem] | None:
    """What a byte-order mark python-dotenv left on the first binding amounts to.

    python-dotenv releases before 1.2.3 (1.2.2 among them) open the file as
    plain utf-8 and read ``\\ufeffNAME=value`` as a variable named
    ``"\\ufeffNAME"``, so ``NAME`` is lost. That is one problem with one
    cause, so the line is reported as the byte-order mark and not also as an
    invalid name. A line python-dotenv rejects outright is still reported as
    unparsable. Releases from 1.2.3 on (the ones requirements.lock pins)
    strip the mark themselves; the decision is made from what the parser
    returned, never from the raw text, so they report nothing here.

    A byte-order mark in front of a comment, a blank line or a bare name
    loses nothing (python-dotenv keeps a ``None``-valued ``"\\ufeff"`` key
    that ``load_dotenv`` ignores): that is ``[]``. ``None`` means the parser
    left no mark on this binding, or the line is wrong for another reason,
    and it gets the ordinary checks.
    """
    if binding.error:
        if not binding.original.string.startswith(_BOM):
            return None
        unparsable = _unparsable(binding.original.string.lstrip(_BOM), 1)
        return [DotenvProblem("bom", 1, unparsable.name), unparsable]
    key = binding.key or ""
    if not key.startswith(_BOM):
        return None
    name = key.lstrip(_BOM)
    valid = bool(_VALID_NAME_RE.match(name))
    if valid and binding.value is not None:
        return [DotenvProblem("bom", 1, name)]
    if binding.value is None and (valid or not name):
        return []
    return None


def _scan_text(text: str) -> Iterator[DotenvProblem]:
    bindings = list(parse_stream(io.StringIO(text)))
    if bindings:
        # The BOM is not a line: the first binding is still line 1.
        reported = _bom_problems(bindings[0])
        if reported is not None:
            yield from reported
            bindings = bindings[1:]
    assigned: dict[str, list[tuple[int, str]]] = {}
    # The assignment just before the current binding, if its value is only an
    # opening bracket: python-dotenv stops an unquoted value at the line break.
    opener: tuple[int, str] | None = None  # (line, name)
    for binding in bindings:
        line = _start_line(binding.original.string, binding.original.line)
        invalid_name = binding.key is not None and not _VALID_NAME_RE.match(binding.key)
        before, opener = opener, None
        if binding.error or invalid_name:
            if before is not None:
                yield DotenvProblem("cut_off", *before)
            if binding.error:
                yield _unparsable(binding.original.string, line)
            else:
                yield DotenvProblem("invalid_name", line)
        elif binding.key is not None and binding.value is not None:
            assigned.setdefault(binding.key, []).append((line, binding.value))
            if binding.value.strip() in ("[", "{"):
                opener = (line, binding.key)
    for name, assignments in assigned.items():
        if len({value for _, value in assignments}) > 1:
            lines = tuple(n for n, _ in assignments)
            yield DotenvProblem("reassigned", lines[0], name, lines)


def scan_dotenv(path: str | Path) -> list[DotenvProblem]:
    """Report what python-dotenv cannot use in the ``.env`` file at *path*.

    Parameters
    ----------
    path:
        The file python-dotenv loaded. Empty, or a file that does not exist,
        means there is no ``.env`` (containers set real variables) and is not
        a problem.

    Returns
    -------
    list[DotenvProblem]
        In file order, with reassigned variables last. Empty when the file is
        clean. The same variable assigned twice with an identical value is
        harmless and not reported.

    Examples
    --------
    >>> import tempfile, os
    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = os.path.join(d, ".env")
    ...     _ = Path(p).write_text("A=1\\nB='[\\n]'\\nC=2\\nC=3\\n", encoding="utf-8")
    ...     [problem.message for problem in scan_dotenv(p)]
    ['C is assigned on lines 4 and 5 with different values; the last one (line 5) wins']
    >>> scan_dotenv("")
    []
    """
    if not path or not Path(path).is_file():
        return []
    try:
        # utf-8 with universal newlines: exactly how python-dotenv opens it.
        with open(path, encoding="utf-8") as stream:
            return list(_scan_text(stream.read()))
    except (OSError, UnicodeDecodeError):
        # Never echo the exception: a decode error quotes the offending bytes.
        return [DotenvProblem("unreadable", 0)]


def format_problems(path: str | Path, problems: list[DotenvProblem]) -> str:
    """The text ``Settings.validate`` raises for a non-empty problem list.

    Parameters
    ----------
    path:
        The ``.env`` file the problems were found in.
    problems:
        What :func:`scan_dotenv` returned.

    Returns
    -------
    str
        Every problem on its own line, then the likely causes and the fix.

    Examples
    --------
    >>> text = format_problems(".env", [DotenvProblem("unparsable", 4, "API_KEYS_JSON")])
    >>> print(text.splitlines()[0])
    .env has lines python-dotenv could not use, so what they set is missing or wrong:
    >>> "  - line 4: could not be parsed (looks like an assignment to API_KEYS_JSON)" in text
    True
    """
    lines = [
        f"{path} has lines python-dotenv could not use, so what they set is missing or wrong:"
    ]
    lines.extend(f"  - {problem.message}" for problem in problems)
    lines.append(
        "Likely causes: a multi-line value (such as a pretty-printed API_KEYS_JSON) "
        "must be wrapped in single quotes and must not contain an apostrophe; "
        "double quotes around JSON break it; the same variable is set twice; the file was saved as UTF-8 with a BOM. "
        "For API keys, put the JSON array in a file and set API_KEYS_FILE "
        "(e.g. project_config/api_keys.json) instead of API_KEYS_JSON. "
        "Fix the lines above and restart."
    )
    return "\n".join(lines)


def _assignments(path: str | Path) -> dict[str, list[tuple[int, bool, bool]]]:
    """Every variable *path* assigns: ``name -> [(line, blank, interpolates), ...]``.

    Only names, line numbers and two facts about a value are kept: whether it
    is empty or blank (whitespace only), and whether it contains ``${``
    (python-dotenv expands such a reference from the environment, and an
    unset variable expands to an empty value). A value itself never leaves
    this function. An unreadable file assigns nothing (:func:`scan_dotenv`
    reports it as ``"unreadable"``).
    """
    try:
        with open(path, encoding="utf-8") as stream:
            bindings = list(parse_stream(io.StringIO(stream.read())))
    except (OSError, UnicodeDecodeError):
        return {}
    found: dict[str, list[tuple[int, bool, bool]]] = {}
    for binding in bindings:
        if binding.error or binding.key is None or binding.value is None:
            continue
        line = _start_line(binding.original.string, binding.original.line)
        name = binding.key.lstrip(_BOM)
        found.setdefault(name, []).append(
            (line, not binding.value.strip(), _INTERPOLATION in binding.value)
        )
    return found


#: What python-dotenv expands from the environment inside a value.
_INTERPOLATION = "${"

def _blank_in_environ(variable: str) -> str:
    """The sentence for a variable that is in the environment but empty or blank.

    It does not claim where the empty value came from: python-dotenv never
    overrides a variable that is already set, and ``${...}`` expansion in
    ``.env`` of an unset variable also yields an empty value.
    """
    return (
        f"the process environment holds an empty or blank value for {variable}; "
        "python-dotenv never overrides a variable that is already set, and "
        "${...} expansion in .env of an unset variable also yields an empty value; "
        f"unset it (PowerShell: Remove-Item Env:{variable}; bash: unset {variable}) "
        "or set it to the real value, and restart"
    )


def _misnamed_dotenv(search: Path) -> Path | None:
    """A ``.env.txt`` in *search* or a folder above it, as ``find_dotenv`` walks."""
    for folder in (search, *search.parents):
        candidate = folder / ".env.txt"
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - an unreadable folder on the way up
            continue
    return None


def explain_unset(
    variable: str,
    path: str | Path,
    problems: Sequence[DotenvProblem],
    environ: Mapping[str, str],
    search_dir: str | Path,
    *,
    disabled: bool = False,
    case_sensitive: bool = os.name != "nt",
) -> list[str]:
    """Why *variable* is unset or empty although it is supposed to come from ``.env``.

    Parameters
    ----------
    variable:
        The environment variable a caller needed (for example a data
        source's ``password_env``).
    path:
        The ``.env`` file python-dotenv loaded; empty when none was found.
    problems:
        What :func:`scan_dotenv` reported for *path*.
    environ:
        The process environment after ``.env`` was loaded.
    search_dir:
        The folder python-dotenv started its search from (it then walks up
        to the root). Named in the message when no file was found.
    disabled:
        ``PYTHON_DOTENV_DISABLED`` was set, so nothing was loaded.
    case_sensitive:
        Whether environment variable names are case-sensitive here (they are
        not on Windows). When ``False``, a ``.env`` line that differs from
        *variable* only in case is the variable itself, and no message says
        otherwise.

    Returns
    -------
    list[str]
        One or more sentences, most likely cause first. Line numbers and
        variable names only, never a value. A variable that is in *environ*
        with an empty or blank value is reported first, whatever the
        ``.env`` holds, because python-dotenv never replaces it.

    Examples
    --------
    >>> import tempfile, os
    >>> with tempfile.TemporaryDirectory() as d:
    ...     env = os.path.join(d, ".env")
    ...     _ = Path(env).write_text("Auction_DM_PASSWORD=\\n", encoding="utf-8")
    ...     explain_unset("Auction_DM_PASSWORD", env, [], {}, d)[0].endswith(
    ...         "line 1 assigns Auction_DM_PASSWORD an empty or blank value")
    True
    >>> explain_unset("PW", "", [], {}, "/app", disabled=True)[0]
    'PYTHON_DOTENV_DISABLED is set, so no .env file was loaded; set PW in the environment itself'
    >>> explain_unset("PW", "", [], {"PW": " "}, "/nonexistent/app")[0].split(";")[0]
    'the process environment holds an empty or blank value for PW'
    """
    blank_in_environ = variable in environ and not environ[variable].strip()
    reasons: list[str] = [_blank_in_environ(variable)] if blank_in_environ else []

    if disabled:
        reasons.append(
            f"PYTHON_DOTENV_DISABLED is set, so no .env file was loaded; "
            f"set {variable} in the environment itself"
        )
        return reasons
    search = Path(search_dir)
    if not path:
        hint = (
            f"no .env file was found in {search} or any folder above it, "
            "so nothing was loaded from one"
        )
        misnamed = _misnamed_dotenv(search)
        if misnamed is not None:
            hint += (
                f"; {misnamed} exists (a file manager that hides extensions may "
                "have saved the file under that name): rename it to .env"
            )
        elif not blank_in_environ:
            hint += f"; create {search / '.env'} (copy .env.example) and set {variable} in it"
        reasons.append(hint)
        return reasons

    if problems:
        reasons.append(f"{path} has lines python-dotenv could not use, and {variable} may be one of them:")
        reasons.extend(f"  - {problem.message}" for problem in problems)

    if any(problem.kind == "unreadable" for problem in problems):
        # Nothing can be said about what the file assigns; "unreadable" above
        # is the reason, and "does not assign" would be a guess.
        return reasons

    assigned = _assignments(path)
    folded = variable.casefold()
    if case_sensitive:
        matches = [(name, entry) for name, entries in assigned.items()
                   for entry in entries if name == variable]
    else:
        matches = [(name, entry) for name, entries in assigned.items()
                   for entry in entries if name.casefold() == folded]
    if matches:
        _, (line, blank, interpolates) = max(matches, key=lambda match: match[1][0])
        if blank:
            reasons.append(f"{path} line {line} assigns {variable} an empty or blank value")
        if interpolates:
            reasons.append(
                f"{path} line {line} assigns {variable} a value containing ${{...}}: "
                "python-dotenv replaces each reference with that variable's value, "
                "or with an empty value when it is not set, so check the variables "
                "the line refers to"
            )
        if not blank and not interpolates and variable not in environ:
            reasons.append(
                f"{path} line {line} assigns {variable}, but {variable} is not in the "
                "process environment: the process started before the file was edited "
                f"(restart it), or a different .env than {path} was loaded"
            )
        if not reasons or (blank_in_environ and not blank and not interpolates):
            reasons.append(f"{path} line {line} assigns {variable}")
        return reasons

    near = sorted(
        (entries[0][0], name) for name, entries in assigned.items()
        if case_sensitive and name.casefold() == folded
    )
    if near:
        for line, name in near:
            text = (
                f"{path} line {line} assigns {name}; variable names are "
                f"case-sensitive, so it must be spelled {variable}"
            )
            if all(blank for _, blank, _ in assigned[name]):
                text += " (and its value is empty or blank)"
            reasons.append(text)
    elif not any(problem.name == variable for problem in problems):
        reasons.append(f"{path} does not assign {variable}; add a line {variable}=... to it")
    return reasons
