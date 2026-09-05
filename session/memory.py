# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Cross-session memory — explicit, config-declared preferences (§5).

"An entry exists because the analyst pinned it. Nothing is inferred from
repetition." This module is deliberately small and has three jobs:

1. **Write-time validation** (:func:`validate_memory_value`) — a memory
   value is untrusted text that ends up in the prompt's variable suffix
   (never the static prefix, see ``prompt_engine`` and this contract's §8),
   so it is capped, rejects newlines/control characters outright, and is
   checked against a declared key's closed option set when one exists.
2. **Applying stored entries to one turn's assumptions**
   (:func:`apply_memory_to_assumptions`) — precedence is
   ``question > session > memory > default``: an entry only replaces an
   already-``"default"``-sourced assumption for the same field, is
   re-sourced ``"memory"`` so the UI's assumption chip shows where it came
   from, and is re-checked against the requesting principal's
   ``denied_columns`` on every call (the ACL can change after the entry
   was stored) — a now-denied entry is dropped for this turn and reported
   in a warning, never silently applied and never silently dropped.
3. Two small text-safety helpers (:func:`has_disallowed_chars`,
   :func:`truncate_at_word_boundary`) reused by both the validator above
   and ``api.v2_routes``'s session-title handling, which is user text
   under the exact same rules (§3).

No key name, warehouse column, or permitted value is a literal anywhere in
this module — every one of those comes from
:func:`knowledge.memory_policy.get_memory_keys`, the one place that
vocabulary is declared (``project_config/memory_policy.yaml``). Free-form
memory (an arbitrary remembered sentence) is deliberately out of scope: it
would be a prompt-injection channel with no schema to validate it against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from knowledge.memory_policy import get_memory_keys
from session.models import Assumption

#: A newline (checked separately, see :func:`has_disallowed_chars`) plus
#: the C0 control-character range excluding tab, and DEL -- the same
#: "safe for a single-line prompt suffix" shape this module's docstring
#: describes. Not a module-level *numeric* constant (test_tuning_layer.py's
#: concern) and not a domain literal (test_no_domain_literals.py's) -- a
#: fixed text-safety rule, the same bucket as security.sql_guard's own
#: compiled regexes.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MemoryValidationError(ValueError):
    """Raised by :func:`validate_memory_value` for an unknown key, or a
    value that fails the length / character / closed-option-set check."""


@dataclass(frozen=True)
class MemoryEntry:
    """One principal's stored value for one declared key (§5).

    Never carries the principal's id itself — callers key a mapping of
    these by the declared ``key`` (e.g. ``{"scope": MemoryEntry(...)}`` as
    stored per-principal by ``session.persistence``), so ownership is the
    caller's bookkeeping, not this dataclass's.
    """

    key: str
    field: str
    value: str
    updated_at: str


def has_disallowed_chars(value: str) -> bool:
    """True if *value* contains a newline or a control character.

    Examples
    --------
    >>> has_disallowed_chars("a\\nb")
    True
    >>> has_disallowed_chars("ordinary text")
    False
    """
    if "\n" in value or "\r" in value:
        return True
    return bool(_CONTROL_CHAR_RE.search(value))


def truncate_at_word_boundary(text: str, max_length: int) -> str:
    """Truncate *text* to at most *max_length* characters, at a word boundary.

    Used both for a session's auto-derived title (the first question,
    truncated to ``session_title_max_length``) and, in principle, any
    other user-facing text this codebase caps this way — never cuts a
    word in half when a space is available to back off to.

    Examples
    --------
    >>> truncate_at_word_boundary("one two three four five six seven eight nine ten", 20)
    'one two three four'
    >>> truncate_at_word_boundary("short", 20)
    'short'
    >>> truncate_at_word_boundary("nospacesatallwhatsoever", 10)
    'nospacesat'
    """
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip()


def validate_memory_value(key: str, value: str) -> None:
    """Raise :class:`MemoryValidationError` if *value* is not a valid
    stored value for the declared *key*.

    Checks, in order: *key* is actually declared
    (``project_config/memory_policy.yaml``); *value* contains no
    newline/control character; *value* does not exceed the key's
    ``max_length``; when the key declares a closed ``options`` set,
    *value* is one of them. Raises nothing (returns normally) when every
    check passes.
    """
    keys = get_memory_keys()
    key_cfg = keys.get(key)
    if key_cfg is None:
        raise MemoryValidationError(f"Unknown memory key: {key!r}")
    if has_disallowed_chars(value):
        raise MemoryValidationError(
            "Memory values may not contain newlines or control characters."
        )
    if len(value) > key_cfg.max_length:
        raise MemoryValidationError(
            f"Value for key {key!r} exceeds max_length={key_cfg.max_length}."
        )
    if key_cfg.options and value not in key_cfg.options:
        raise MemoryValidationError(
            f"Value {value!r} for key {key!r} is not one of the declared options."
        )


def apply_memory_to_assumptions(
    assumptions: list[Assumption],
    entries: dict[str, MemoryEntry],
    denied_columns: Sequence[str] | None,
) -> tuple[list[Assumption], list[str], dict[str, str]]:
    """Apply *entries* to *assumptions*, per §5's precedence and ACL rules.

    Parameters
    ----------
    assumptions:
        The turn's already-built assumption list (``session.ambiguity``'s
        output), before any ``PATCH .../assumptions`` override is applied.
    entries:
        ``{key: MemoryEntry}`` — the calling principal's stored memory,
        as returned by ``session.persistence.SessionPersistence.get_memory_entries``.
    denied_columns:
        The requesting principal's
        :attr:`~security.auth.Principal.denied_columns`, re-checked here
        on every call (the ACL can change after an entry was stored).

    Returns
    -------
    tuple[list[Assumption], list[str], dict[str, str]]
        ``(assumptions, warnings, used)``.

        ``assumptions`` is a NEW list (the input is never mutated) with
        each memory-applied entry's matching, ``"default"``-sourced
        assumption replaced (value + ``source="memory"``). An assumption
        already sourced ``"question"``/``"session"``/``"policy"`` is left
        untouched — precedence is ``question > session > memory >
        default``.

        ``warnings`` carries one Persian-language message per entry
        dropped because its column is in *denied_columns* — never applied,
        and never silently ignored either (§5).

        ``used`` is ``{key: value}`` for exactly the entries that actually
        changed an assumption — the set the caller must fold into the
        query cache's scope key (see ``security.auth.scope_key``), and
        only that set: hashing every stored entry regardless of whether it
        influenced this query would partition the cache per principal for
        no reason.
    """
    if not entries:
        return assumptions, [], {}

    keys = get_memory_keys()
    denied = set(denied_columns or ())
    result = list(assumptions)
    warnings: list[str] = []
    used: dict[str, str] = {}

    for key, entry in entries.items():
        key_cfg = keys.get(key)
        if key_cfg is None:
            # A stored entry for a key this deployment no longer declares
            # (project_config/memory_policy.yaml was edited after the
            # entry was written) -- nothing to associate it with, and
            # nothing to warn about either, since it never applies.
            continue
        if key_cfg.column in denied:
            warnings.append(
                f"مقدار به‌خاطرسپرده‌شدهٔ «{entry.field}» به دلیل محدودیت دسترسی به ستون "
                f"«{key_cfg.column}» برای این کاربر در این پرسش اعمال نشد."
            )
            continue
        for i, assumption in enumerate(result):
            if assumption.field == entry.field and assumption.source == "default":
                result[i] = assumption.model_copy(
                    update={"value": entry.value, "source": "memory"}
                )
                used[key] = entry.value
                break

    return result, warnings, used
