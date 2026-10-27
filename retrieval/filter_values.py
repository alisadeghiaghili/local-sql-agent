# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Does a literal the model filtered on exist in the column? -- the filter value check.

The model sometimes invents the value it compares a dimension's label column
to: ``WHERE r.[Name] = N'Ring'`` (the table's own name), a word from the
question that names no ring (``N'physical market'``), a column name. The
statement is valid, the guard has no reason to refuse it, and the answer is
empty or wrong. :mod:`retrieval.dimension_vocabulary` already holds, for the
columns a table lists in ``prefetchable_columns`` in ``schema.yaml``, the whole
set of their distinct values; until now it only helped to read the question.
This module uses the same cache to read the statement.

What it does
------------
For a statement the guard has accepted, :func:`check_filter_values` finds the
string literals compared to a column by ``=``, ``<>``/``!=``, ``IN`` and
``NOT IN`` (:func:`string_comparisons`), resolves each column to the stored
column or columns it reads (:func:`security.sql_guard.column_origins`, the
guard's own resolution through aliases, CTEs and derived tables), and compares
the literal with the cached vocabulary of that column the way the question is
compared with it (:func:`core.persian.normalize_for_matching`: Persian and
Arabic letter forms, digits, ZWNJ, case, whitespace). A literal whose
normalised form is not among the normalised values is *unknown*, and
:func:`correction_text` words the one correction message the caller sends: the
column, that the value does not exist, the nearest real values, and the
instruction to drop the filter if the question names no specific value.

What it leaves alone
--------------------
* ``LIKE``, numbers, dates, parameters, expressions around a column.
* A column that is not in ``prefetchable_columns``, or whose vocabulary is not
  cached yet: nothing is known about it, so nothing is claimed. The cache is
  only read (a stale list is used while the existing background refresh runs;
  a missing one triggers that refresh, as a question mentioning the table
  does). There is no database read on this path.
* A vocabulary that may be cut short: the prefetch reads at most
  ``DEFAULT_TOP_N`` distinct values, so a list that long says nothing about
  what is not in it.
* A column the caller may not see (a denied or join-only column): never
  checked, never named, its values never offered.
* The value of a column shared by several tables of the statement
  (``Name`` unqualified, two tables with one): known if any of them has it,
  and not checked at all unless every one of them has a cached vocabulary.

Audit
-----
:func:`audit_block` is what the audit record keeps: the columns, a hash and
the length of each unknown literal, and what happened to the statement. Not
the literal and not the nearest values (the latter are warehouse data), the
same "names, never values" rule :attr:`observability.audit.AuditRecord.resolved_columns`
follows.

Nothing here raises: a failure to check is a statement that is not checked.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

import config as cfg
from core.persian import normalize_for_matching
from prompt_engine.untrusted import UNTRUSTED_INSTRUCTION, fence_untrusted
from retrieval import dimension_vocabulary as dv
from security.column_policy import cached_column_policy, hidden_value_columns
from security.sql_guard import column_origins

logger = logging.getLogger(__name__)

__all__ = [
    "StringComparison",
    "UnknownValue",
    "ValueCheck",
    "audit_block",
    "check_filter_values",
    "correction_text",
    "nearest_values",
    "string_comparisons",
]

#: How many real values a correction message offers per unknown literal.
NEAREST_LIMIT = 5

#: Trigram similarity below which a value is not offered as "near".
_MIN_SIMILARITY = 0.15

#: Longest value (characters) quoted in a correction message.
_QUOTED_MAX = 120


@dataclass(frozen=True)
class StringComparison:
    """One string literal compared to one column of the statement.

    Attributes
    ----------
    origins:
        The stored columns the compared column reads, as
        ``(table key, lower-cased column)``; see
        :func:`security.sql_guard.column_origins`. Empty when it is not a
        plain read of a stored column.
    value:
        The literal's text (``N'x'`` and ``'x'`` alike).
    operator:
        ``"="``, ``"<>"``, ``"IN"`` or ``"NOT IN"``.
    """

    origins: tuple[tuple[str, str], ...]
    value: str
    operator: str


@dataclass(frozen=True)
class UnknownValue:
    """A literal that is not a value of the column it is compared to.

    Attributes
    ----------
    columns:
        ``"Table.Column"`` of every column the compared column reads (one,
        unless an unqualified name is shared by several tables).
    value:
        The literal as the model wrote it.
    nearest:
        Up to :data:`NEAREST_LIMIT` real values, nearest first; empty when
        none is similar.
    sample:
        A few real values to show when nothing is similar.
    """

    columns: tuple[str, ...]
    value: str
    nearest: tuple[str, ...]
    sample: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValueCheck:
    """The outcome of checking one statement.

    Attributes
    ----------
    checked:
        How many distinct literal-and-column pairs had a vocabulary to be
        checked against.
    unknown:
        Those that are not in it, in the order the statement has them.
    """

    checked: int = 0
    unknown: tuple[UnknownValue, ...] = ()


def _unparen(node: exp.Expression | None) -> exp.Expression | None:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _literal_text(node: exp.Expression | None) -> str | None:
    """The text of a string literal node (``'x'``, ``N'x'``, either in parentheses), else ``None``."""
    node = _unparen(node)
    if isinstance(node, exp.National):
        inner = node.this
        if isinstance(inner, exp.Literal):
            return inner.this if inner.is_string else None
        return inner if isinstance(inner, str) else None
    if isinstance(node, exp.Literal) and node.is_string:
        return node.this
    return None


def string_comparisons(sql: str, dialect: str = "tsql") -> list[StringComparison]:
    """The string literals *sql* compares to a column by ``=``, ``<>``, ``IN`` or ``NOT IN``.

    Pure: no vocabulary, no settings. One entry per literal, in the order the
    statement is written. A column written inside a function or an
    expression, a number, a date written as a number, a parameter, ``LIKE``,
    ``BETWEEN`` and ``IN (SELECT ...)`` give nothing. A statement that does
    not parse gives an empty list.

    Parameters
    ----------
    sql:
        A statement the guard accepted.
    dialect:
        The dialect it is written in.

    Returns
    -------
    list[StringComparison]
        With ``origins`` empty for a column that is not a plain read of a
        stored column; callers decide what to do with those.

    Examples
    --------
    >>> found = string_comparisons(
    ...     "SELECT r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'Alpha' AND r.ID IN (1, 2)")
    >>> [(c.origins, c.value, c.operator) for c in found]
    [((('Ring', 'name'),), 'Alpha', '=')]
    >>> string_comparisons("SELECT r.Name FROM Ring r WHERE r.Name LIKE N'%a%'")
    []
    >>> string_comparisons("not sql at all (")
    []
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - a statement that cannot be read is not checked
        return []
    if tree is None:
        return []

    found: list[StringComparison] = []
    for node in tree.find_all(exp.EQ, exp.NEQ, exp.In, bfs=False):
        if isinstance(node, exp.In):
            column, texts = _unparen(node.this), [_literal_text(e) for e in node.expressions]
            operator = "NOT IN" if isinstance(node.parent, exp.Not) else "IN"
        else:
            left, right = _unparen(node.this), _unparen(node.expression)
            if isinstance(left, exp.Column):
                column, texts = left, [_literal_text(right)]
            elif isinstance(right, exp.Column):
                column, texts = right, [_literal_text(left)]
            else:
                continue
            operator = "=" if isinstance(node, exp.EQ) else "<>"
        if not isinstance(column, exp.Column):
            continue
        values = [text for text in texts if text is not None]
        if not values:
            continue
        origins = column_origins(column, tree)
        found.extend(StringComparison(origins, text, operator) for text in values)
    return found


def _trigrams(text: str) -> frozenset[str]:
    padded = f"  {text} "
    return frozenset(padded[i:i + 3] for i in range(len(padded) - 2))


def nearest_values(value: str, vocabulary: Iterable[str], limit: int = NEAREST_LIMIT) -> list[str]:
    """Up to *limit* values of *vocabulary* most like *value*, nearest first.

    Character trigrams of the normalised forms
    (:func:`core.persian.normalize_for_matching`), Jaccard similarity; a
    value below a small similarity is not offered. Deterministic: ties are
    broken by the normalised value, then the value itself. Meant for the
    small sets the prefetch holds.

    Examples
    --------
    >>> nearest_values("Alpa Hall", ["Beta Hall", "Alpha Hall", "Gamma Port"], limit=2)
    ['Alpha Hall', 'Beta Hall']
    >>> nearest_values("zzz", ["Alpha Hall"])
    []
    """
    wanted = _trigrams(normalize_for_matching(value))
    scored: list[tuple[float, str, str]] = []
    for candidate in dict.fromkeys(vocabulary):
        normalized = normalize_for_matching(candidate)
        grams = _trigrams(normalized)
        score = len(wanted & grams) / len(wanted | grams)
        if score >= _MIN_SIMILARITY:
            scored.append((-score, normalized, candidate))
    scored.sort()
    return [candidate for _score, _normalized, candidate in scored[:limit]]


def _configured_column(table: str, column_lower: str) -> str | None:
    """*column_lower* as ``schema.yaml`` spells it, when it is a prefetchable column of *table*."""
    for configured in dv.PREFETCH_COLUMNS.get(table, ()):
        if configured.lower() == column_lower:
            return configured
    return None


def _usable_vocabulary(table: str, column: str) -> list[str] | None:
    """The cached values of ``table.column`` when they can be relied on to be all of them, else ``None``."""
    values = dv.get_vocabulary_for_check(table, column)
    if not values or len(values) >= cfg.settings.default_top_n:
        return None
    return values


def check_filter_values(
    sql: str,
    *,
    denied_columns: Iterable[str] | None = None,
    dialect: str = "tsql",
    limit: int = NEAREST_LIMIT,
) -> ValueCheck:
    """Compare the string literals *sql* filters on with the cached vocabularies.

    Parameters
    ----------
    sql:
        A statement the guard accepted.
    denied_columns:
        The caller's ``denied_columns`` entries, as the guard receives them;
        a denied or join-only column is not checked (see the module
        docstring).
    dialect:
        The dialect *sql* is written in.
    limit:
        How many nearest values to keep per unknown literal.

    Returns
    -------
    ValueCheck
        Empty when nothing could be checked. Never raises.

    Examples
    --------
    >>> dv.clear_vocabulary_cache()
    >>> import pandas as pd
    >>> _ = dv.refresh_vocabulary(
    ...     "Ring", "Name", execute_fn=lambda sql, params: pd.DataFrame({"Name": ["Alpha Hall", "Beta Hall"]}))
    >>> check = check_filter_values(
    ...     "SELECT r.Name FROM [ref].[Ring] r WHERE r.Name = N'Alpha Hall' OR r.Name = N'Ring'")
    >>> check.checked, [(u.columns, u.value) for u in check.unknown]
    (2, [(('Ring.Name',), 'Ring')])
    >>> dv.clear_vocabulary_cache()
    """
    try:
        comparisons = string_comparisons(sql, dialect)
        if not comparisons:
            return ValueCheck()
        policy = cached_column_policy(denied_columns)
        hidden = hidden_value_columns(policy)

        checked = 0
        unknown: list[UnknownValue] = []
        seen: set[tuple[tuple[tuple[str, str], ...], str]] = set()
        for comparison in comparisons:
            normalized = normalize_for_matching(comparison.value)
            key = (comparison.origins, normalized)
            if not comparison.origins or not normalized or key in seen:
                continue
            seen.add(key)

            vocabularies: list[tuple[str, str, list[str]]] = []
            for table, column_lower in comparison.origins:
                column = _configured_column(table, column_lower)
                if (
                    column is None
                    or column.upper() in policy.denied
                    or (table, column_lower) in hidden
                ):
                    vocabularies = []
                    break
                values = _usable_vocabulary(table, column)
                if values is None:
                    vocabularies = []
                    break
                vocabularies.append((table, column, values))
            if not vocabularies:
                continue

            checked += 1
            pool = [value for _t, _c, values in vocabularies for value in values]
            if any(normalize_for_matching(value) == normalized for value in pool):
                continue
            nearest = nearest_values(comparison.value, pool, limit)
            unknown.append(UnknownValue(
                columns=tuple(f"{table}.{column}" for table, column, _v in vocabularies),
                value=comparison.value,
                nearest=tuple(nearest),
                sample=() if nearest else tuple(sorted(dict.fromkeys(pool), key=normalize_for_matching)[:limit]),
            ))
        return ValueCheck(checked=checked, unknown=tuple(unknown))
    except Exception as exc:  # noqa: BLE001 - a check that fails is a statement that is not checked
        logger.warning("filter value check skipped (%s)", type(exc).__name__)
        return ValueCheck()


def _quoted(text: str) -> str:
    one_line = " ".join(text.split())
    shown = one_line if len(one_line) <= _QUOTED_MAX else one_line[:_QUOTED_MAX - 3] + "..."
    return "'" + shown.replace("'", "''") + "'"


def correction_text(sql: str, check: ValueCheck) -> str:
    """The correction message for the unknown literals of *check*, starting without a blank line.

    Per value: the column, that the value does not exist, and the nearest
    real values (or a few that exist, when none is near), in the fence
    :mod:`prompt_engine.untrusted` puts around warehouse data; then what to
    do: use the real value the question means, keep a value it names that is
    not listed (the cache lags behind the database), or, if it names no
    specific value of the column, remove the filter. The caller appends it to the
    question segment, exactly as it does the text of any other correction.

    Parameters
    ----------
    sql:
        The statement that was checked.
    check:
        Its :class:`ValueCheck`; at least one unknown value.

    Examples
    --------
    >>> text = correction_text(
    ...     "SELECT 1", ValueCheck(1, (UnknownValue(("Ring.Name",), "Ring", ("Alpha Hall",)),)))
    >>> "Ring.Name: the value 'Ring' does not exist" in text
    True
    >>> "Ring.Name, nearest to 'Ring': Alpha Hall" in text
    True
    >>> "remove this filter" in text
    True
    """
    problems: list[str] = []
    real: list[str] = []
    for item in check.unknown:
        columns = ", ".join(item.columns)
        problems.append(f"{columns}: the value {_quoted(item.value)} does not exist in this column.")
        if item.nearest:
            offered = ", ".join(item.nearest)
            real.append(f"{columns}, nearest to {_quoted(item.value)}: {offered}")
        elif item.sample:
            offered = ", ".join(item.sample)
            real.append(f"{columns}, no value is similar to {_quoted(item.value)}; values that exist include: {offered}")
    parts = [
        "The SQL query you generated filters on a value that does not exist.",
        "",
        "--- SQL ---",
        sql,
        "",
        "--- VALUES THAT DO NOT EXIST ---",
        *problems,
    ]
    if real:
        parts += ["", "--- REAL VALUES ---", UNTRUSTED_INSTRUCTION, fence_untrusted("\n".join(real))]
    parts += [
        "",
        "--- INSTRUCTIONS ---",
        "For each value above: if the question names a specific value of that column, use the real "
        "value it means, written exactly as listed; the list was read a little while ago, so if the "
        "question names a value that is not listed, keep it as you wrote it. If the question does not "
        "name a specific value of this column, remove this filter. Do not change anything else about "
        "the query. Return only the corrected SQL statement.",
        "",
        "SQL:",
        "",
    ]
    return "\n".join(parts)


def _digest(value: str) -> str:
    return hashlib.sha256(normalize_for_matching(value).encode("utf-8")).hexdigest()[:16]


def audit_block(check: ValueCheck, *, corrected: bool, fallback: bool) -> dict[str, Any] | None:
    """What the audit record keeps of a check, or ``None`` when nothing was checked.

    ``{"checked": n, "unknown": [{"column", "value_sha256", "length"}, ...],
    "corrected": bool, "fallback": bool}``: *checked* literals had a
    vocabulary, *unknown* lists the ones that were not in it by column, the
    first 16 hex digits of the SHA-256 of the normalised literal (the same
    invented value can be counted across requests) and its length; the
    literal itself is not written. *corrected* is true when the statement
    that ran is the corrected one, *fallback* when the correction was asked
    for and the original statement ran instead.

    Examples
    --------
    >>> block = audit_block(
    ...     ValueCheck(2, (UnknownValue(("Ring.Name",), "Gamma", ()),)), corrected=False, fallback=True)
    >>> block["checked"], block["corrected"], block["fallback"]
    (2, False, True)
    >>> sorted(block["unknown"][0])
    ['column', 'length', 'value_sha256']
    >>> "Gamma" in str(block["unknown"])
    False
    >>> audit_block(ValueCheck(), corrected=False, fallback=False) is None
    True
    """
    if not check.checked and not check.unknown:
        return None
    return {
        "checked": check.checked,
        "unknown": [
            {
                "column": ", ".join(item.columns),
                "value_sha256": _digest(item.value),
                "length": len(item.value),
            }
            for item in check.unknown
        ],
        "corrected": corrected,
        "fallback": fallback,
    }
