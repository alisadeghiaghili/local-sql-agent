# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""What a correction prompt tells the model about a name it got wrong.

When the SQL guard rejects a statement because it names a column that does
not exist on a table, or a table that does not exist, the self-correction
loops (:mod:`llm.sql_agent`, ``session/engine.py``) re-prompt with the
failed SQL and the guard's message ("Unknown column 'X' on table 'T'"). The
message says what is wrong and nothing about what is right, so a model that
had no good reason to doubt the name tends to try another guess, and in the
production audit log a request that began with such a rejection used every
correction round (three model calls) and was refused in the end. This module
adds the missing half, deterministically and from the loaded ``schema.yaml``:

* **unknown column on a known table** -- the table's real column names, in
  ``schema.yaml`` order (names only, no descriptions);
* **unknown table** -- the real tables whose names are closest to the one
  written, each as the reference to write in ``FROM`` (``[schema].[Table]``).

Nothing else is added: no other rejection, and no execution error, gets a
hint, and a hint is empty rather than guessed whenever its input is missing.

Access policy
-------------
The hint must not show a caller more than the guard lets the caller use:

* a column the caller's ``denied_columns`` fully denies is **left out** of the
  list (the guard rejects it as a :class:`~security.sql_guard.PolicyRejection`,
  which is never retried, so naming it would only invite the refusal);
* a **join-only** column (``Source:schema.Table.Column`` entries) is **kept,
  marked ``(join key only)``** -- the guard allows exactly that use of it, the
  first-attempt prompt already tells the model so
  (:func:`security.column_policy.join_only_prompt_line`), and leaving it out
  would send the model away from a join it needs;
* if the caller's policy cannot be read, the hint is empty.

Prompt placement
----------------
A hint is part of the correction text, which both loops append to the
*question* segment only. It never enters the static prefix, so it cannot
disturb the model server's prefix cache.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)

__all__ = ["correction_hint"]

#: Marks a join-only column in the list of a table's columns.
JOIN_KEY_MARK = " (join key only)"


def _columns_hint(
    table: str, denied_columns: Iterable[str] | None, max_columns: int,
) -> str:
    from schema_data.registry import get_table_columns
    from security.column_policy import (
        cached_column_policy,
        load_schema_view,
        resolve_join_only,
    )

    columns = get_table_columns().get(table)
    if not columns:
        return ""
    policy = cached_column_policy(denied_columns)
    join_only = (
        resolve_join_only(policy, load_schema_view(), None).by_table.get(table, {})
        if policy.join_only else {}
    )
    names: list[str] = []
    for name in columns:
        if name.upper() in policy.denied:
            continue
        names.append(name + (JOIN_KEY_MARK if name.casefold() in join_only else ""))
    if not names:
        return ""
    shown = names[:max_columns]
    more = len(names) - len(shown)
    listing = ", ".join(shown) + (f", ... and {more} more" if more else "")
    return (
        f"--- COLUMNS THAT EXIST ON {table} ---\n"
        f"{listing}\n"
        f"Use only these names for {table}; any other column does not exist there."
    )


def _tables_hint(subject: str, source: str | None, max_tables: int) -> str:
    from schema_data.registry import (
        SchemaRegistry,
        bare_table_name,
        get_table_columns,
        get_table_schema_qualifiers,
        table_reference_sql,
    )

    keys = list(get_table_columns())
    if source is not None:
        in_source = set(SchemaRegistry.tables_for_source(source))
        keys = [key for key in keys if key in in_source]
    by_name: dict[str, list[str]] = {}
    for key in keys:
        by_name.setdefault(bare_table_name(key).casefold(), []).append(key)
    wanted = bare_table_name(subject).casefold()
    close = difflib.get_close_matches(wanted, sorted(by_name), n=max_tables, cutoff=0.6)
    chosen = [key for name in close for key in by_name[name]][:max_tables]
    if not chosen:
        return ""
    qualifiers = get_table_schema_qualifiers()
    references = ", ".join(table_reference_sql(key, qualifiers.get(key, "")) for key in chosen)
    return (
        f"--- TABLES THAT EXIST, CLOSEST TO {subject} ---\n"
        f"{references}\n"
        "Use one of these names exactly as written, or another table from the schema above."
    )


def correction_hint(
    exc: BaseException,
    *,
    denied_columns: Iterable[str] | None = None,
    source: str | None = None,
    max_columns: int = 200,
    max_tables: int = 5,
) -> str:
    """The extra block of a correction prompt for *exc*, or ``""`` when there is none.

    Parameters
    ----------
    exc:
        The guard's rejection. Only an unknown column (it carries ``table``)
        and an unknown table (``reason == "unknown_table"``, without a
        "did you mean" of its own) get a hint.
    denied_columns:
        The caller's ``denied_columns`` entries, as the guard receives them;
        see the module docstring for what they leave out or mark.
    source:
        The data source the prompt is for, when several are configured: the
        table suggestions are then limited to that source's tables.
    max_columns, max_tables:
        Bounds on the list sizes; a longer column list ends with ``... and N more``.

    Returns
    -------
    str
        A block starting with a ``--- ... ---`` heading and ending without a
        newline, deterministic for the same inputs. Never raises: a failure
        to build it is logged and yields ``""``.

    Examples
    --------
    >>> correction_hint(ValueError("syntax error"))
    ''
    >>> from security.sql_guard import CorrectableRejection
    >>> correction_hint(CorrectableRejection("Unknown column 'x'", reason="other", subject="x"))
    ''
    """
    try:
        table = getattr(exc, "table", None)
        if table and getattr(exc, "subject", None):
            return _columns_hint(table, denied_columns, max_columns)
        if getattr(exc, "reason", None) == "unknown_table" and "did you mean" not in str(exc):
            subject = getattr(exc, "subject", None)
            if subject:
                return _tables_hint(str(subject), source, max_tables)
    except Exception as hint_exc:  # noqa: BLE001 - a hint is an extra; the correction goes ahead without it
        logger.warning("correction hint skipped (%s)", type(hint_exc).__name__)
    return ""
