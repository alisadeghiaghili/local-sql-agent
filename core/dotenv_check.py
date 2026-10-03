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

Public API
----------
DotenvProblem
scan_dotenv(path) -> list[DotenvProblem]
format_problems(path, problems) -> str
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from dotenv.parser import parse_stream

__all__ = ["DotenvProblem", "format_problems", "scan_dotenv"]

#: What a shell, a container runtime and ``os.environ`` agree is a variable name.
_VALID_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: ``NAME=`` (optionally ``export NAME=``) at the start of a line python-dotenv
#: rejected. Only the leading identifier is captured, never the value.
_ASSIGNED_NAME_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

_NEWLINE_RE = re.compile(r"\r\n|\n|\r")

Kind = Literal["unparsable", "invalid_name", "cut_off", "reassigned", "unreadable"]


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
        once with different values) or ``"unreadable"`` (the file itself
        could not be read).
    line:
        1-based line the problem starts on; the first assignment for
        ``"reassigned"``; ``0`` for ``"unreadable"``.
    name:
        The variable name when one is known; never the value, and never the
        text of an invalid name.
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


def _scan_stream(stream: io.TextIOBase) -> Iterator[DotenvProblem]:
    assigned: dict[str, list[tuple[int, str]]] = {}
    # The assignment just before the current binding, if its value is only an
    # opening bracket: python-dotenv stops an unquoted value at the line break.
    opener: tuple[int, str] | None = None  # (line, name)
    for binding in parse_stream(stream):
        line = _start_line(binding.original.string, binding.original.line)
        invalid_name = binding.key is not None and not _VALID_NAME_RE.match(binding.key)
        before, opener = opener, None
        if binding.error or invalid_name:
            if before is not None:
                yield DotenvProblem("cut_off", *before)
            if binding.error:
                match = _ASSIGNED_NAME_RE.match(binding.original.string)
                yield DotenvProblem("unparsable", line, match.group(1) if match else None)
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
            return list(_scan_stream(stream))
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
        "double quotes around JSON break it; the same variable is set twice. "
        "For API keys, put the JSON array in a file and set API_KEYS_FILE "
        "(e.g. project_config/api_keys.json) instead of API_KEYS_JSON. "
        "Fix the lines above and restart."
    )
    return "\n".join(lines)
