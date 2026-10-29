# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Which ``examples.yaml`` questions the analyst page offers to start with.

An empty conversation showed one line, «برای شروع، پرسش خود را در پایین
بنویسید», and nothing to ask. ``examples.yaml`` already holds the questions an
operator curated for this deployment (with their SQL, as few-shot examples for
the model); ``GET /v2/examples`` offers the *questions* as one-click starts.

Only the question text leaves the server, never the SQL or the tags. A question
is offered to a principal only when its SQL passes the same guard an answer to
it would have to pass for that principal
(:func:`security.sql_guard.validate_sql` with the principal's
``denied_columns``): an example over a column the analyst may not read is not
suggested to them, and an example whose SQL the guard refuses for any other
reason is not suggested to anyone. Any failure while checking an example
leaves that example out (fail closed).

File reads only; nothing here touches the database.

Public API
----------
offered_questions(examples, *, denied_columns, limit, is_allowed=...) -> list[str]
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

MAX_LIMIT = 12
"""The most questions one response carries: a start, not a catalogue."""

MAX_QUESTION_LENGTH = 300
"""A longer ``question`` is not a one-click start; it is left out."""


def _guard_allows(sql: str, denied_columns: list[str]) -> bool:
    """True when *sql* passes :func:`security.sql_guard.validate_sql` for a
    principal with *denied_columns*; False on a refusal or any other error."""
    from security.sql_guard import validate_sql

    try:
        validate_sql(sql, denied_columns=denied_columns)
    except Exception:  # noqa: BLE001 -- fail closed: an example that cannot be checked is not offered
        return False
    return True


def offered_questions(
    examples: Iterable[Mapping[str, Any]],
    *,
    denied_columns: Iterable[str],
    limit: int,
    is_allowed: Callable[[str, list[str]], bool] = _guard_allows,
) -> list[str]:
    """The questions of *examples* to offer, in file order.

    Args:
        examples: ``examples.yaml``'s entries as mappings with ``question``
            and ``sql`` (:data:`knowledge.examples.EXAMPLES`).
        denied_columns: the principal's ``denied_columns``.
        limit: how many to return at most; clamped to 1..:data:`MAX_LIMIT`.
        is_allowed: ``(sql, denied_columns) -> bool``; the guard by default,
            replaceable in tests.

    Returns:
        Distinct, stripped, non-empty questions of at most
        :data:`MAX_QUESTION_LENGTH` characters whose SQL *is_allowed*, in
        the order the file lists them, at most *limit* of them.

    Raises:
        Nothing: an entry that is malformed or cannot be checked is skipped.

    Examples:
        >>> rows = [
        ...     {"question": "How many orders?", "sql": "SELECT 1"},
        ...     {"question": "How many orders?", "sql": "SELECT 2"},
        ...     {"question": "  ", "sql": "SELECT 3"},
        ...     {"question": "Orders by secret id", "sql": "SELECT SecretId"},
        ...     {"question": "Top customers", "sql": "SELECT 4"},
        ... ]
        >>> allow = lambda sql, denied: not any(c in sql for c in denied)
        >>> offered_questions(rows, denied_columns=["SecretId"], limit=5, is_allowed=allow)
        ['How many orders?', 'Top customers']
        >>> offered_questions(rows, denied_columns=[], limit=1, is_allowed=allow)
        ['How many orders?']
    """
    cap = max(1, min(int(limit), MAX_LIMIT))
    denied = [str(c) for c in denied_columns]
    seen: set[str] = set()
    out: list[str] = []
    for entry in examples:
        if len(out) >= cap:
            break
        if not isinstance(entry, Mapping):
            continue
        question = entry.get("question")
        sql = entry.get("sql")
        if not isinstance(question, str) or not isinstance(sql, str):
            continue
        text = " ".join(question.split())
        if not text or len(text) > MAX_QUESTION_LENGTH or text in seen:
            continue
        try:
            allowed = bool(is_allowed(sql, denied))
        except Exception:  # noqa: BLE001 -- fail closed, as _guard_allows does
            allowed = False
        if not allowed:
            continue
        seen.add(text)
        out.append(text)
    return out
