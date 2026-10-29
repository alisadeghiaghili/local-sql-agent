# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Spell a filter literal the way the column stores it -- the filter value spelling fix.

The warehouse may store a value with the Arabic letters ``ي``/``ك`` while the
model, and the question, always write the Persian ``ی``/``ک``::

    WHERE r.[Name] = N'<value with Arabic YEH>'    -- returns rows
    WHERE r.[Name] = N'<same value, Persian YEH>'  -- returns nothing

Under the deployment's collation the two are different strings. The filter
value check (:mod:`retrieval.filter_values`) compares *normalised* forms, so it
counts the Persian spelling as a known value and changes nothing. This module
is the deterministic complement: no model call, one decision a person would
make by looking at the column.

What it rewrites
----------------
For a statement the guard accepted, on a column whose whole vocabulary is
cached (the rules of :func:`retrieval.filter_values.usable_vocabularies`: a
missing, empty or possibly truncated vocabulary, a denied or join-only column, a
column that is not a plain read of a stored one: nothing is known, nothing is
changed). The cache is only read: no query, and not even the background refresh
a cold or stale column would start for the filter value check, so a deployment
that never asked for value checking issues no read it did not before. A stale
list is used as the filter value check uses it; a value added to the column
since the last refresh is simply not known, and a literal that is not known to
mean a stored value stays as written.

* ``=``, ``<>``/``!=``, ``IN`` and ``NOT IN`` with a string literal that is
  **not byte-equal to any stored value**: when exactly one distinct stored value
  has the same :func:`core.persian.letter_form_key`, the literal becomes that
  stored value, character for character. Two or more candidates, or none: the
  literal stays.
* ``LIKE`` and ``NOT LIKE`` with a string pattern: for each letter group
  (``ی ي ى``, ``ک ك``), when the column's stored values use **one** form of
  the group and the pattern uses another, the pattern's letters of that group
  become the stored form. A column that stores both forms of a group, none, or
  any Arabic presentation form is left alone.

Everything else in the statement is kept byte for byte: the edit replaces the
characters of the literal's own token (found through the position sqlglot
records for it), keeping the ``N`` prefix and the quoting, and the result is
parsed again and compared with the tree it should be. If anything differs, the
statement is not changed.

What counts as "the same letter"
--------------------------------
:func:`core.persian.letter_form_key` folds, and nothing else:

* ``ي`` (U+064A) and ``ى`` (U+0649) with ``ی`` (U+06CC);
* ``ك`` (U+0643) with ``ک`` (U+06A9);
* Arabic presentation forms and ligatures (U+FB50-FDFF, U+FE70-FEFF) to their
  base letters, canonical composition (NFC), and ZWNJ with none
  (``می‌خواهم`` and ``میخواهم``).

Deliberately **not** folded, each for a reason:

* ``ة`` and ``ه``: different letters (teh marbuta is not a form of heh), and
  ``ه`` is in nearly every Persian word, so a "single form" test for it would
  say nothing.
* ``ۀ`` and ``هٔ``: a letter and an optional mark, one more encoding of a
  diacritic; ``normalize_for_matching`` leaves diacritics alone for the same
  reason.
* the alefs ``آ أ إ ا``: different letters in Persian.
* digits (``۱۲۳``, ``١٢٣``, ``123``): a different mistake with its own risk
  (codes, years, ids). ``normalize_for_matching`` folds them for *finding* a
  value; rewriting a literal with a different digit script is a separate
  decision.
* case, spaces (a ZWNJ is not a space), punctuation.

The ``LIKE`` rule, and why there is no ``OR`` expansion
--------------------------------------------------------
A column that stores one form of a group is unambiguous: the pattern letter
can only mean it. A column that stores both has the same word in two
spellings, and finding all of them would take ``(x LIKE a OR x LIKE b)`` at
that place of the condition: inside ``NOT``, under ``OR``, with one copy per
letter of the pattern (two letters, four patterns), it changes the shape of
the statement the model wrote, which this module otherwise keeps to the
character. That case is left to the model and to the filter value check; the
statement runs as it did before.

Audit and settings
------------------
:func:`audit_block` is what the audit record keeps: how many literals were
rewritten and the ``Table.Column`` names, never the literal and never the stored
value (the same "names, never values" rule as
:attr:`observability.audit.AuditRecord.value_check`).
:attr:`config.Settings.filter_value_spelling` (``FILTER_VALUE_SPELLING``)
switches the fix off; with it off nothing here is called.

Nothing here raises: a fix that cannot be made is a statement that is not
changed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from core.persian import LETTER_FORM_GROUPS, PRESENTATION_FORMS_RE, letter_form_key
from retrieval.filter_values import comparison_literals, like_patterns, usable_vocabularies
from security.column_policy import cached_column_policy, hidden_value_columns
from security.sql_guard import column_origins, validate_sql

logger = logging.getLogger(__name__)

__all__ = [
    "SpellingFix",
    "audit_block",
    "fix_filter_spelling",
    "respell_filters",
    "respell_like_pattern",
    "respell_literal",
]

#: ``N'text'`` or ``'text'`` as it is written in the statement.
_QUOTED = re.compile(r"(?P<prefix>[Nn]?)'(?P<body>.*)'", re.DOTALL)


@dataclass(frozen=True)
class SpellingFix:
    """The outcome of one pass over a statement.

    Attributes
    ----------
    sql:
        The statement to run: the rewritten one when ``fixed`` is not zero,
        otherwise the statement as it was given.
    fixed:
        How many literals were rewritten.
    columns:
        ``"Table.Column"`` of every column a rewritten literal is compared to,
        sorted and without repeats.
    """

    sql: str
    fixed: int = 0
    columns: tuple[str, ...] = ()


def respell_literal(value: str, stored: Iterable[str]) -> str | None:
    """The stored value a literal compared with ``=``/``IN`` was meant to be, or ``None``.

    Parameters
    ----------
    value:
        The literal's text.
    stored:
        The column's cached values.

    Returns
    -------
    str | None
        The one stored value that equals *value* up to letter form
        (:func:`core.persian.letter_form_key`), when *value* is not itself a
        stored value; ``None`` when it is one, when none matches, and when more
        than one distinct value does.

    Examples
    --------
    >>> respell_literal("\\u06a9\\u06cc\\u0634", ["\\u0643\\u064a\\u0634", "\\u0628\\u0646\\u062f\\u0631"]) == "\\u0643\\u064a\\u0634"
    True
    >>> respell_literal("\\u0643\\u064a\\u0634", ["\\u0643\\u064a\\u0634"]) is None
    True
    >>> respell_literal("\\u06a9\\u06cc\\u0634", ["\\u0643\\u064a\\u0634", "\\u06a9\\u064a\\u0634"]) is None
    True
    >>> respell_literal("Alpha", ["alpha", "Beta"]) is None
    True
    """
    stored = list(dict.fromkeys(stored))
    if value in stored:
        return None
    key = letter_form_key(value)
    if not key:
        return None
    candidates = [candidate for candidate in stored if letter_form_key(candidate) == key]
    return candidates[0] if len(candidates) == 1 else None


def respell_like_pattern(pattern: str, stored: Iterable[str]) -> str | None:
    """The pattern with its variant letters written as the column stores them, or ``None``.

    Parameters
    ----------
    pattern:
        The text of a ``LIKE`` pattern, wildcards included.
    stored:
        The column's cached values.

    Returns
    -------
    str | None
        The rewritten pattern; ``None`` when nothing changes: the column mixes
        the forms of every group the pattern uses, stores none of them, holds
        an Arabic presentation form, or the pattern already uses the stored
        form.

    Examples
    --------
    >>> respell_like_pattern("%\\u06a9\\u06cc%", ["\\u0643\\u064a\\u0634", "\\u0628\\u0646\\u062f\\u0631"]) == "%\\u0643\\u064a%"
    True
    >>> respell_like_pattern("%\\u06a9\\u06cc%", ["\\u0643\\u064a\\u0634", "\\u06a9\\u06cc\\u0634"]) is None
    True
    >>> respell_like_pattern("%\\u0643\\u064a%", ["\\u0643\\u064a\\u0634"]) is None
    True
    >>> respell_like_pattern("%a%", ["\\u0643\\u064a\\u0634"]) is None
    True
    """
    stored = list(dict.fromkeys(stored))
    if any(PRESENTATION_FORMS_RE.search(value) for value in stored):
        return None
    rewritten = pattern
    for group in LETTER_FORM_GROUPS:
        used = {form for form in group if any(form in value for value in stored)}
        if len(used) != 1:
            continue
        (target,) = used
        for form in group:
            if form != target:
                rewritten = rewritten.replace(form, target)
    return rewritten if rewritten != pattern else None


def _source_span(sql: str, node: exp.Expression, text: str) -> tuple[int, int, str] | None:
    """Where the literal *node* is written in *sql*: ``(start, end, prefix)``, else ``None``.

    sqlglot records the first and last character of a literal's token on the
    node (``meta["start"]``/``meta["end"]``, inclusive). The span is used only
    when the characters there are one quoted literal whose text is *text*;
    anything else (no position, another dialect's escaping) is not edited.
    """
    start, end = node.meta.get("start"), node.meta.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end < len(sql):
        return None
    written = _QUOTED.fullmatch(sql[start:end + 1])
    if written is None or written["body"].replace("''", "'") != text or "'" in written["body"].replace("''", ""):
        return None
    return start, end, written["prefix"]


def _set_text(node: exp.Expression, text: str) -> None:
    """Give the literal *node* the text *text* (the tree the new statement must parse to)."""
    inner = node.this
    if isinstance(node, exp.National) and isinstance(inner, exp.Literal):
        inner.set("this", text)
    else:
        node.set("this", text)


def fix_filter_spelling(
    sql: str,
    *,
    denied_columns: Iterable[str] | None = None,
    dialect: str = "tsql",
) -> SpellingFix:
    """Rewrite the filter literals of *sql* that a column stores with other letter forms.

    See the module docstring for the rules. Pure apart from the read of the
    vocabulary cache; the guard is not run here (:func:`respell_filters` does
    that).

    Parameters
    ----------
    sql:
        A statement the guard accepted.
    denied_columns:
        The caller's ``denied_columns`` entries, as the guard receives them; a
        denied or join-only column is never read for its values.
    dialect:
        The dialect *sql* is written in.

    Returns
    -------
    SpellingFix
        With ``sql`` unchanged and ``fixed`` zero when there is nothing to do or
        the rewrite could not be made safely. Never raises.

    Examples
    --------
    >>> from retrieval import dimension_vocabulary as dv
    >>> import pandas as pd
    >>> dv.clear_vocabulary_cache()
    >>> _ = dv.refresh_vocabulary(
    ...     "Ring", "Name", execute_fn=lambda s, p: pd.DataFrame({"Name": ["\\u0643\\u064a\\u0634", "Beta"]}))
    >>> fix = fix_filter_spelling("SELECT r.Name FROM [ref].[Ring] AS r WHERE r.Name = N'\\u06a9\\u06cc\\u0634'")
    >>> fix.fixed, fix.columns
    (1, ('Ring.Name',))
    >>> fix.sql == "SELECT r.Name FROM [ref].[Ring] AS r WHERE r.Name = N'\\u0643\\u064a\\u0634'"
    True
    >>> fix_filter_spelling("SELECT r.Name FROM [ref].[Ring] AS r WHERE r.Name = N'Beta'").fixed
    0
    >>> dv.clear_vocabulary_cache()
    """
    try:
        return _fix(sql, denied_columns, dialect)
    except Exception as exc:  # noqa: BLE001 - a fix that fails is a statement that is not changed
        logger.warning("filter value spelling skipped (%s)", type(exc).__name__)
        return SpellingFix(sql)


def _fix(sql: str, denied_columns: Iterable[str] | None, dialect: str) -> SpellingFix:
    tree = sqlglot.parse_one(sql, read=dialect)
    if tree is None:
        return SpellingFix(sql)
    policy = cached_column_policy(denied_columns)
    hidden = hidden_value_columns(policy)

    def pool_of(column: exp.Column) -> tuple[list[str], list[str]] | None:
        origins = column_origins(column, tree)
        if not origins:
            return None
        vocabularies = usable_vocabularies(origins, policy.denied, hidden, refresh=False)
        if not vocabularies:
            return None
        return (
            [value for _t, _c, values in vocabularies for value in values],
            [f"{table}.{name}" for table, name, _v in vocabularies],
        )

    # node id -> (node, text it had, text it gets, columns)
    edits: dict[int, tuple[exp.Expression, str, str, list[str]]] = {}
    for column, literals, _operator in comparison_literals(tree):
        pool = pool_of(column)
        if pool is None:
            continue
        for node, text in literals:
            replacement = respell_literal(text, pool[0])
            if replacement is not None:
                edits[id(node)] = (node, text, replacement, pool[1])
    for column, node, text, _negated in like_patterns(tree):
        pool = pool_of(column)
        if pool is None:
            continue
        replacement = respell_like_pattern(text, pool[0])
        if replacement is not None:
            edits[id(node)] = (node, text, replacement, pool[1])
    if not edits:
        return SpellingFix(sql)

    spans: list[tuple[int, int, str]] = []
    for node, text, replacement, _columns in edits.values():
        span = _source_span(sql, node, text)
        if span is None:
            return SpellingFix(sql)
        start, end, prefix = span
        quoted = replacement.replace("'", "''")
        spans.append((start, end, f"{prefix}'{quoted}'"))
    spans.sort(reverse=True)
    if any(smaller[1] >= larger[0] for smaller, larger in zip(spans[1:], spans)):
        return SpellingFix(sql)
    rewritten = sql
    for start, end, source in spans:
        rewritten = rewritten[:start] + source + rewritten[end + 1:]

    # The new text must read as the old tree with just those literals changed.
    for node, _text, replacement, _columns in edits.values():
        _set_text(node, replacement)
    if sqlglot.parse_one(rewritten, read=dialect).sql(dialect=dialect) != tree.sql(dialect=dialect):
        return SpellingFix(sql)

    columns = sorted({name for *_rest, names in edits.values() for name in names})
    return SpellingFix(rewritten, len(edits), tuple(columns))


def audit_block(fix: SpellingFix) -> dict[str, Any] | None:
    """What the audit record keeps of a fix, or ``None`` when nothing was rewritten.

    ``{"fixed": n, "columns": ["Table.Column", ...]}``: how many literals
    were rewritten and the columns they are compared to. Not the literal
    and not the stored value (warehouse data).

    Parameters
    ----------
    fix:
        What :func:`fix_filter_spelling` returned.

    Returns
    -------
    dict[str, Any] | None
        The block for :attr:`observability.audit.AuditRecord.value_spelling`.

    Examples
    --------
    >>> audit_block(SpellingFix("SELECT 1", 2, ("Ring.Name",)))
    {'fixed': 2, 'columns': ['Ring.Name']}
    >>> audit_block(SpellingFix("SELECT 1")) is None
    True
    """
    if not fix.fixed:
        return None
    return {"fixed": fix.fixed, "columns": list(fix.columns)}


def respell_filters(
    sql: str,
    *,
    denied_columns: Iterable[str] | None = None,
    dialect: str = "tsql",
) -> tuple[str, dict[str, Any] | None]:
    """The statement to run, and the audit block, after the spelling fix.

    :func:`fix_filter_spelling`, then the guard again on the rewritten text
    (:func:`security.sql_guard.validate_sql`, in the dialect it will run in,
    with the caller's ``denied_columns``): if the guard refuses it, which a
    change of one literal's letters should never cause, the original
    statement is returned.

    Parameters
    ----------
    sql:
        A statement the guard accepted (and, for a non-``tsql`` target, the
        transpiled one).
    denied_columns:
        As for :func:`fix_filter_spelling`; also given to the guard.
    dialect:
        The dialect *sql* is written in.

    Returns
    -------
    tuple[str, dict[str, Any] | None]
        ``(sql to run, audit block)``; the block is ``None`` and the statement
        is the one given when nothing was rewritten. Never raises.

    Examples
    --------
    >>> respell_filters("SELECT 1 AS x")
    ('SELECT 1 AS x', None)
    """
    fix = fix_filter_spelling(sql, denied_columns=denied_columns, dialect=dialect)
    if not fix.fixed:
        return sql, None
    try:
        validate_sql(fix.sql, denied_columns=denied_columns, dialect=dialect)
    except Exception as exc:  # noqa: BLE001 - whatever the guard says, the original statement runs
        logger.warning("filter value spelling not used: the guard refused the rewritten statement (%s)", type(exc).__name__)
        return sql, None
    logger.info("filter value spelling: %d literal(s) rewritten", fix.fixed)
    return fix.sql, audit_block(fix)
