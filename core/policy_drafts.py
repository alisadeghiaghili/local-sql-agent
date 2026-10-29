# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Draft the real values of ``memory_policy.yaml`` and ``session_policy.yaml``.

``scripts/draft_policies.py`` reads the distinct values of one small dimension
column and this module turns them into the two drafts, without touching a
database:

* :func:`usable_values` cuts the values read down to what may be an option: the
  rules of the setup wizard (:mod:`core.value_shapes`: no date, number, code,
  phone number, address, masked value or error text, nothing over 40
  characters), nothing longer than the memory key allows, nothing equal to the
  session policy's ``default_label``, one spelling of each value after the
  project's Persian and Arabic folding, sorted;
* :func:`choose_memory_keys` says which memory keys the dimension belongs to;
* :func:`draft_policies` edits the *text* of the two files, line by line, and
  replaces only ``options`` (and ``filter_key`` / ``column`` when the dimension
  changes). Every comment, every other field, the quoting and the order stay as
  they were, and the result is proved by loading it with the application's own
  validators and comparing it with the input plus exactly those fields.

The invariant the template's comments describe is kept and checked: the memory
options never contain the session policy's ``default_label``, so a pinned value
is always textually different from the unset default.

Public API
----------
DraftValues, DraftedPolicies
    The values kept and the reasons for the ones dropped, and the two texts.
usable_values(raw, default_label, max_length) -> DraftValues
choose_memory_keys(policies, requested) -> list[str]
draft_policies(policies, values, filter_key, column, memory_keys) -> DraftedPolicies
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import yaml

from core.persian import normalize_for_matching
from core.policy_values import MEMORY_FILE, SESSION_FILE, Policies
from core.value_shapes import unsuitable_value
from schema_data.yaml_text import (
    Edit,
    LayoutError,
    apply_edits,
    indent_of,
    is_blank_or_comment,
    quote,
)

__all__ = [
    "DraftValues",
    "DraftedPolicies",
    "choose_memory_keys",
    "draft_policies",
    "usable_values",
]


@dataclass(frozen=True)
class DraftValues:
    """The values kept as options, and why the others were not."""

    values: tuple[str, ...]
    """One spelling of each usable value, sorted."""
    read: int
    """How many values were given."""
    dropped: Mapping[str, int] = field(default_factory=dict)
    """``{reason: count}``; the reasons are the shape names of :mod:`core.value_shapes`
    (``date``, ``phone``, ``code``, ...), ``long``, ``short``, ``over max_length``,
    ``default label``, ``not text`` and ``duplicate``."""


@dataclass(frozen=True)
class DraftedPolicies:
    """The two drafted files as text, and what was changed in them."""

    memory_text: str
    session_text: str
    memory_keys: tuple[str, ...]
    """The memory keys whose ``options`` were replaced."""
    changed_dimension: bool
    """Whether ``filter_key`` (or a memory key's ``column``) was rewritten."""


def _has_arabic_letter(value: str) -> bool:
    """Whether *value* spells a letter with the Arabic ``ي`` or ``ك`` where Persian writes ``ی`` and ``ک``."""
    return "ي" in value or "ك" in value


def usable_values(raw: Iterable[object], default_label: str, max_length: int) -> DraftValues:
    """Cut the values read from a dimension column down to the options they may become.

    Parameters
    ----------
    raw:
        The distinct values read, in any order.
    default_label:
        The session policy's ``default_label``. A value that is the same word
        (after the folding below) is left out: a memory option must differ from it.
    max_length:
        The memory key's ``max_length``; a longer value could never be pinned.

    Returns
    -------
    DraftValues
        Surrounding whitespace is removed, inner spacing is kept. Two values
        that differ only by the letter forms, digits, spacing or case
        :func:`core.persian.normalize_for_matching` folds are one; the spelling
        kept is the first in sorted order, the Persian letters before the Arabic.

    Examples
    --------
    >>> found = usable_values(["Region B", "Region A", " region a ", "2024-05-01", "x", "All regions"],
    ...                       "All regions", 20)
    >>> found.values, found.read
    (('Region A', 'Region B'), 6)
    >>> sorted(found.dropped.items())
    [('date', 1), ('default label', 1), ('duplicate', 1), ('short', 1)]
    >>> usable_values(["علي", "علی"], "", 20).values
    ('علی',)
    >>> usable_values(["a" * 30], "", 20).dropped["over max_length"]
    1
    """
    dropped: Counter[str] = Counter()
    given = 0
    candidates: list[str] = []
    for item in raw:
        given += 1
        if not isinstance(item, str):
            dropped["not text"] += 1
            continue
        value = item.strip()
        why = unsuitable_value(value)
        if why is None and len(value) > max_length:
            why = "over max_length"
        if why is None and default_label and normalize_for_matching(value) == normalize_for_matching(default_label):
            why = "default label"
        if why is not None:
            dropped[why] += 1
            continue
        candidates.append(value)
    kept: dict[str, str] = {}
    for value in sorted(candidates, key=lambda v: (normalize_for_matching(v), _has_arabic_letter(v), v)):
        folded = normalize_for_matching(value)
        if folded in kept:
            dropped["duplicate"] += 1
        else:
            kept[folded] = value
    return DraftValues(tuple(kept.values()), given, dict(dropped))


def choose_memory_keys(policies: Policies, requested: str | None = None) -> list[str]:
    """The memory keys whose ``options`` belong to the session policy's dimension.

    Without *requested*: the keys whose ``filter_key`` is the session policy's
    (the same dimension) and that have a closed set of options. A key with no
    ``options`` takes any value up to its ``max_length`` by design, and closing
    it would change what an analyst can pin, so it is left alone unless it is
    named.

    Parameters
    ----------
    policies:
        From :func:`core.policy_values.read_policies`.
    requested:
        A memory key named by the operator; it is used whatever its options.

    Raises
    ------
    ValueError
        When *requested* is not a key of ``memory_policy.yaml``, or no key
        qualifies (the message says how to name one).

    Examples
    --------
    >>> from core.policy_values import SHIPPED_DIRECTORY, read_policies
    >>> template = read_policies(SHIPPED_DIRECTORY)
    >>> choose_memory_keys(template), choose_memory_keys(template, "scope")
    (['scope'], ['scope'])
    >>> choose_memory_keys(template, "other")
    Traceback (most recent call last):
        ...
    ValueError: memory_policy.yaml has no key 'other' (keys: scope)
    """
    keys = policies.memory.keys
    if requested is not None:
        if requested not in keys:
            raise ValueError(f"{MEMORY_FILE} has no key {requested!r} (keys: {', '.join(keys)})")
        return [requested]
    dimension = policies.session.default_scope.filter_key.strip().casefold()
    chosen = [
        name for name, entry in keys.items()
        if entry.filter_key.strip().casefold() == dimension and entry.options
    ]
    if not chosen:
        raise ValueError(
            f"no key of {MEMORY_FILE} has the filter_key of {SESSION_FILE} default_scope and a closed set of "
            f"options (keys: {', '.join(keys)}); name the one to draft with --memory-key"
        )
    return chosen


# ---------------------------------------------------------------------------
# Line-level editing of the two files
# ---------------------------------------------------------------------------

_KEY_LINE = re.compile(r"""^(?P<indent> *)(?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s#'"\[\]{},&*!|>%@`-][^#:]*?)[ \t]*:[ \t]*(?:\#.*)?$""")
_QUOTED = re.compile(r"""^(?:"(?:[^"\\]|\\.)*"|'(?:[^']|'')*')""")


def _comment_of(text: str) -> str:
    """The ``# ...`` comment that ends the YAML fragment *text*, or ``""`` (a ``#`` in quotes is not one)."""
    inside = ""
    previous = " "
    for position, char in enumerate(text):
        if inside:
            if char == inside and not (inside == '"' and previous == "\\"):
                inside = ""
        elif char in "\"'" and previous in " \t[,":
            inside = char
        elif char == "#" and previous in " \t":
            return text[position:].rstrip()
        previous = char
    return ""


def _top_block(lines: Sequence[str], name: str) -> tuple[int, int]:
    """``(header line, end)`` of the top-level mapping *name*; *end* is one past its last content line."""
    header = next(
        (i for i, line in enumerate(lines) if re.match(rf"^{re.escape(name)}[ \t]*:[ \t]*(?:#.*)?$", line)), None,
    )
    if header is None:
        raise LayoutError(f"no top-level `{name}:` mapping written as a block")
    end = header + 1
    for i in range(header + 1, len(lines)):
        if is_blank_or_comment(lines[i]):
            continue
        if indent_of(lines[i]) == 0:
            break
        end = i + 1
    return header, end


def _entries(lines: Sequence[str], first: int, end: int) -> list[tuple[str, int, int]]:
    """``(key, line, stop)`` of each mapping entry between *first* and *end*, at the indentation of the first."""
    body = [i for i in range(first, end) if not is_blank_or_comment(lines[i])]
    if not body:
        return []
    indent = indent_of(lines[body[0]])
    heads: list[tuple[str, int]] = []
    for i in body:
        match = _KEY_LINE.match(lines[i])
        if match is not None and indent_of(lines[i]) == indent:
            heads.append((str(yaml.safe_load(match.group("key"))), i))
    out: list[tuple[str, int, int]] = []
    for n, (key, at) in enumerate(heads):
        limit = heads[n + 1][1] if n + 1 < len(heads) else end
        last = max(i for i in range(at, limit) if not is_blank_or_comment(lines[i])) + 1
        out.append((key, at, last))
    return out


def _field(lines: Sequence[str], first: int, end: int, name: str) -> tuple[int, int, int] | None:
    """``(line, stop, indent)`` of the field *name* of the mapping whose body is ``lines[first:end]``."""
    body = [i for i in range(first, end) if not is_blank_or_comment(lines[i])]
    if not body:
        return None
    indent = indent_of(lines[body[0]])
    pattern = re.compile(rf"^ {{{indent}}}{re.escape(name)}[ \t]*:(?P<rest>.*)$")
    for i in body:
        match = pattern.match(lines[i])
        if match is None:
            continue
        rest = match.group("rest")
        value = rest.strip()
        stop = i + 1
        if not value or value.startswith("#"):
            for j in range(i + 1, end):
                if is_blank_or_comment(lines[j]):
                    continue
                if indent_of(lines[j]) > indent or (indent_of(lines[j]) == indent and lines[j].lstrip().startswith("- ")):
                    stop = j + 1
                else:
                    break
        return i, stop, indent
    return None


def _set_scalar(lines: Sequence[str], where: tuple[int, int, int], name: str, value: str) -> list[Edit]:
    """The edit that gives the one-line field at *where* the value *value*, keeping its comment."""
    line, stop, indent = where
    match = re.match(rf"^( {{{indent}}}{re.escape(name)}[ \t]*:[ \t]*)(?P<rest>.*)$", lines[line])
    assert match is not None
    rest = match.group("rest")
    comment = _comment_of(rest)
    source = rest[: len(rest) - len(comment)].rstrip() if comment else rest.rstrip()
    gap = rest[len(source): len(rest) - len(comment)] if comment else ""
    if stop != line + 1 or not source or source[0] in "|>[{&*!":
        raise LayoutError(f"`{name}:` is not a one-line scalar; edit it by hand")
    quoted = _QUOTED.match(source)
    if quoted is not None and quoted.end() != len(source):
        raise LayoutError(f"`{name}:` is not a one-line scalar; edit it by hand")
    if yaml.safe_load(source) == value:
        return []
    return [(line, line + 1, [match.group(1) + quote(value) + (gap + comment if comment else "")])]


def _set_options(lines: Sequence[str], where: tuple[int, int, int] | None, entry: tuple[int, int, int], values: Sequence[str]) -> list[Edit]:
    """The edit that makes ``options`` the block list of *values*, in place or after the entry's last field."""
    first, last, indent = entry
    if where is None:
        return [(last, last, [f"{' ' * indent}options:", *(f"{' ' * (indent + 2)}- {quote(v)}" for v in values)])]
    line, stop, indent = where
    header = lines[line]
    rest = header.split(":", 1)[1]
    comment = _comment_of(rest)
    flow = rest[: len(rest) - len(comment)].strip() if comment else rest.strip()
    if flow.startswith("[") and "]" not in flow:
        raise LayoutError("`options:` is a flow list written over several lines; edit it by hand")
    items = [lines[j] for j in range(line + 1, stop) if lines[j].lstrip().startswith("- ")]
    item_indent = indent_of(items[0]) if items else indent + 2
    gap = rest[: len(rest) - len(comment)][len(rest[: len(rest) - len(comment)].rstrip()):] if comment else ""
    rewritten = [f"{' ' * indent}options:" + (gap + comment if comment else "")]
    rewritten += [f"{' ' * item_indent}- {quote(v)}" for v in values]
    return [(line, stop, rewritten)]


def _edit_mapping(
    lines: Sequence[str], first: int, end: int, scalars: Mapping[str, str], options: Sequence[str] | None,
) -> list[Edit]:
    """Edits for the mapping whose body is ``lines[first:end]``: the scalars, then ``options``."""
    body = [i for i in range(first, end) if not is_blank_or_comment(lines[i])]
    if not body:
        raise LayoutError("an entry with no field")
    indent = indent_of(lines[body[0]])
    edits: list[Edit] = []
    last = body[-1] + 1
    for name, value in scalars.items():
        where = _field(lines, first, end, name)
        if where is None:
            edits.append((last, last, [f"{' ' * indent}{name}: {quote(value)}"]))
        else:
            edits.extend(_set_scalar(lines, where, name, value))
    if options is not None:
        edits.extend(_set_options(lines, _field(lines, first, end, "options"), (first, last, indent), options))
    return edits


def _join(lines: Sequence[str], eol: str, bom: bool) -> str:
    return ("﻿" if bom else "") + eol.join(lines)


def _split(text: str) -> tuple[list[str], str, bool]:
    """``(lines, line ending, had a byte order mark)`` of *text*."""
    bom = text.startswith("﻿")
    body = text[1:] if bom else text
    eol = "\r\n" if "\r\n" in body else "\n"
    return body.replace("\r\n", "\n").split("\n"), eol, bom


def _edit_memory(text: str, keys: Mapping[str, tuple[Mapping[str, str], Sequence[str]]]) -> str:
    lines, eol, bom = _split(text)
    header, end = _top_block(lines, "keys")
    found = {key: (at, stop) for key, at, stop in _entries(lines, header + 1, end)}
    edits: list[Edit] = []
    for key, (scalars, options) in keys.items():
        if key not in found:
            raise LayoutError(f"{MEMORY_FILE}: key {key!r} is not written as a block entry")
        at, stop = found[key]
        edits.extend(_edit_mapping(lines, at + 1, stop, scalars, options))
    return _join(apply_edits(list(lines), edits), eol, bom)


def _edit_session(text: str, scalars: Mapping[str, str], options: Sequence[str]) -> str:
    lines, eol, bom = _split(text)
    header, end = _top_block(lines, "default_scope")
    edits = _edit_mapping(lines, header + 1, end, scalars, options)
    return _join(apply_edits(list(lines), edits), eol, bom)


def draft_policies(
    policies: Policies,
    values: Sequence[str],
    filter_key: str | None = None,
    column: str | None = None,
    memory_keys: Sequence[str] | None = None,
) -> DraftedPolicies:
    """Draft both files with *values* as the options, changing nothing else.

    The memory keys get ``options`` = *values*; the session policy gets
    ``options`` = its ``default_label`` followed by *values*, the order the
    template uses. *filter_key* and *column*, when given, replace those fields
    in the session policy (``filter_key``) and in the chosen memory keys
    (``filter_key`` and ``column``); ``field_name``, ``default_label``,
    ``clarification_prompt``, ``max_length`` and any other field are never
    touched.

    Parameters
    ----------
    policies:
        The current files, from :func:`core.policy_values.read_policies`.
    values:
        The options, from :func:`usable_values`.
    filter_key:
        The new ``filter_key``, or ``None`` to keep each file's own.
    column:
        The new memory ``column``, or ``None``.
    memory_keys:
        The memory keys to draft; :func:`choose_memory_keys` when ``None``.

    Returns
    -------
    DraftedPolicies

    Raises
    ------
    ValueError
        When *values* is empty or holds the ``default_label``; when a file is
        laid out in a way the line editor cannot edit
        (:class:`~schema_data.yaml_text.LayoutError`, a ``ValueError``); or when
        the result does not load with the application's validators or differs
        from the input in any field but the ones above. Nothing is returned
        then, so nothing wrong is ever written.

    Examples
    --------
    >>> from core.policy_values import SHIPPED_DIRECTORY, read_policies
    >>> drafted = draft_policies(read_policies(SHIPPED_DIRECTORY), ["North", "South"], "Zone", "Title")
    >>> drafted.memory_keys, drafted.changed_dimension
    (('scope',), True)
    >>> print(drafted.session_text.split("options:")[1].split("clarification")[0].strip())
    - "All rings"
        - "North"
        - "South"
    >>> 'filter_key: "Zone"' in drafted.memory_text and 'column: "Title"' in drafted.memory_text
    True
    """
    from knowledge.config_loader import MemoryPolicyConfig, SessionPolicyConfig, validate_yaml_text

    options = list(values)
    if not options:
        raise ValueError("no value to draft the options from")
    scope = policies.session.default_scope
    folded_default = normalize_for_matching(scope.default_label)
    if any(normalize_for_matching(v) == folded_default for v in options):
        raise ValueError(f"a value equals the default_label of {SESSION_FILE}; a memory option must differ from it")
    chosen = list(memory_keys) if memory_keys is not None else choose_memory_keys(policies)

    memory_scalars = {name: value for name, value in (("filter_key", filter_key), ("column", column)) if value is not None}
    session_scalars = {"filter_key": filter_key} if filter_key is not None else {}
    memory_text = _edit_memory(policies.memory_text, {key: (memory_scalars, options) for key in chosen})
    session_text = _edit_session(policies.session_text, session_scalars, [scope.default_label, *options])

    # Proof: both load as the application loads them, and differ from the input only where intended.
    memory = validate_yaml_text(MEMORY_FILE, memory_text, MemoryPolicyConfig)
    session = validate_yaml_text(SESSION_FILE, session_text, SessionPolicyConfig)
    expected_memory = policies.memory.model_dump()
    for key in chosen:
        expected_memory["keys"][key].update(memory_scalars)
        expected_memory["keys"][key]["options"] = options
    expected_session = policies.session.model_dump()
    expected_session["default_scope"].update(session_scalars)
    expected_session["default_scope"]["options"] = [scope.default_label, *options]
    if memory.model_dump() != expected_memory or session.model_dump() != expected_session:
        raise LayoutError("the edit would change a field other than the ones asked for; edit the files by hand")
    entries = [policies.memory.keys[key] for key in chosen]
    moved = filter_key is not None and (
        scope.filter_key != filter_key or any(entry.filter_key != filter_key for entry in entries)
    )
    changed = moved or (column is not None and any(entry.column != column for entry in entries))
    return DraftedPolicies(memory_text, session_text, tuple(chosen), changed)
