# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Per-key column restrictions: full denial, and table/source-scoped join-only.

A key's ``denied_columns`` array holds two kinds of entry:

======================================  =====================================
Entry                                   Meaning
======================================  =====================================
``Col``                                 Legacy full deny. The column may not
                                        be referenced anywhere, on any table
                                        of any data source. Unchanged.
``schema.Table.Col``                    **Join-only** on that table, whichever
                                        data source the query runs on.
``Source:schema.Table.Col``             Join-only on that table, only when the
                                        query executes on ``Source``.
``Source:Col``                          Join-only on every table of ``Source``
                                        that has a column ``Col``.
======================================  =====================================

*Join-only* means the column may appear in one place only: as one side of an
equality between two columns inside a ``JOIN ... ON``. It can never reach
the output, a filter, a grouping or an ordering (the enforcement lives in
:func:`security.sql_guard.validate_sql`). The table part is a ``schema.yaml``
table key, written as the key itself or as ``<db_schema>.<name>``; a bare
table name is accepted when exactly one table has it. Names are matched
case-insensitively. Brackets around a part are ignored; a name that itself
contains ``.`` or ``:`` cannot be written (a column name containing either
is read as a scoped entry, not as a legacy one).

Parsing and checking are two steps, kept apart on purpose:

* :func:`parse_column_policy` reads the syntax into a frozen
  :class:`ColumnPolicy` and, by default, checks every scoped entry against
  the loaded ``schema.yaml`` and ``datasources.yaml`` (the source exists,
  the table exists and belongs to that source, the column exists on it), so
  a typo fails at start-up instead of silently restricting nothing.
* :func:`resolve_join_only` turns the entries into the table -> columns map
  the guard enforces for one executing data source. It runs the same checks
  again, so a policy that was only syntax-checked (see below) still fails
  loudly the first time it is used.

When the schema cannot be loaded at the moment a key is parsed (a test that
builds keys before any ``schema.yaml`` exists, a partially configured
checkout), :func:`parse_column_policy` keeps the syntax check, logs that the
existence check was deferred, and returns the policy. The guard needs the
schema to import at all, so no query can run on an unchecked policy: its
first use re-checks and refuses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Mapping

__all__ = [
    "ColumnPolicy",
    "ColumnPolicyError",
    "EMPTY_POLICY",
    "JoinOnlyRestrictions",
    "ScopedColumn",
    "SchemaView",
    "cached_column_policy",
    "hidden_value_columns",
    "join_only_prompt_line",
    "load_schema_view",
    "parse_column_policy",
    "resolve_join_only",
    "validate_column_policy",
]

logger = logging.getLogger(__name__)

#: Whether the "existence check deferred" warning has been logged yet.
_deferred_warning_logged = False


class ColumnPolicyError(ValueError):
    """A ``denied_columns`` entry is malformed, or names something that does not exist.

    The message always names the offending entry and the reason. Entries are
    column and table names, never credentials, so quoting one is safe.
    """


def _entry_error(entry: str, reason: str) -> ColumnPolicyError:
    return ColumnPolicyError(f"denied_columns entry {entry!r}: {reason}")


@dataclass(frozen=True)
class ScopedColumn:
    """One join-only entry: *column*, optionally limited to a *table* and/or *datasource*.

    Attributes
    ----------
    datasource:
        The data source the restriction applies on, or ``None`` for every
        source. Kept as written; compared case-insensitively.
    table:
        The table the restriction applies to (``schema.Table`` as written),
        or ``None`` for every table of *datasource* that has *column*. Never
        ``None`` together with a ``None`` *datasource* (that is a legacy
        entry, not a scoped one).
    column:
        The column name, as written.
    entry:
        The entry exactly as it appears in ``denied_columns``. This is what
        a guard rejection reports as its ``subject`` and what approving an
        access request removes from the key, so it is never rebuilt or
        re-cased. Defaults to the rendered form of the other three fields.

    Examples
    --------
    >>> ScopedColumn("Sales", "sales.Order", "ID").entry
    'Sales:sales.Order.ID'
    >>> ScopedColumn(None, "sales.Order", "ID").entry
    'sales.Order.ID'
    >>> ScopedColumn("Sales", None, "ID").entry
    'Sales:ID'
    """

    datasource: str | None
    table: str | None
    column: str
    entry: str = ""

    def __post_init__(self) -> None:
        if not self.entry:
            object.__setattr__(self, "entry", self.render())

    def render(self) -> str:
        """The canonical spelling of this restriction."""
        head = f"{self.datasource}:" if self.datasource is not None else ""
        tail = f"{self.table}.{self.column}" if self.table is not None else self.column
        return head + tail

    def key(self) -> tuple[str, str, str]:
        """Case-folded identity, for dropping duplicate entries."""
        return (
            (self.datasource or "").casefold(),
            (self.table or "").casefold(),
            self.column.casefold(),
        )

    def display(self) -> str:
        """How the prompt hint names this restriction (no data-source part).

        Examples
        --------
        >>> ScopedColumn(None, "sales.Order", "ID").display()
        'sales.Order.ID'
        >>> ScopedColumn("Sales", None, "ID").display()
        'ID (on every table)'
        """
        if self.table is None:
            return f"{self.column} (on every table)"
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class ColumnPolicy:
    """The parsed ``denied_columns`` of one key.

    Attributes
    ----------
    denied:
        Legacy full-deny column names, upper-cased (the form
        :func:`~security.sql_guard.validate_sql` compares against).
    join_only:
        The scoped, join-only restrictions, in the order written, without
        duplicates.
    """

    denied: frozenset[str] = frozenset()
    join_only: tuple[ScopedColumn, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.denied or self.join_only)


#: The policy of a key with no restriction at all.
EMPTY_POLICY = ColumnPolicy()


def _parse_scoped(entry: str) -> ScopedColumn:
    """Parse a scoped entry (one that contains ``:`` or ``.``).

    Raises
    ------
    ColumnPolicyError
        More than one ``:``, an empty part, or a table part with more than
        three qualifiers.
    """
    if entry.count(":") > 1:
        raise _entry_error(
            entry, "at most one ':' is allowed (Source:schema.Table.Column or Source:Column)"
        )
    datasource: str | None = None
    rest = entry
    if ":" in entry:
        head, rest = entry.split(":", 1)
        datasource = head.strip()
        if not datasource:
            raise _entry_error(entry, "the data source before ':' is empty")
    parts = [part.strip() for part in rest.split(".")]
    if any(not part for part in parts):
        raise _entry_error(entry, "has an empty part (check for a stray '.' or ':')")
    if len(parts) == 1:
        # `Source:Col` -- a bare column with no source cannot get here (it has
        # neither ':' nor '.'), so `datasource` is set.
        return ScopedColumn(datasource, None, parts[0], entry=entry)
    # A table key carries at most catalog.database.schema before its name
    # (schema_data.registry.split_table_key); one more part is the column.
    if len(parts) > 5:
        raise _entry_error(
            entry,
            "the table part has too many parts (at most catalog.database.schema.Table "
            "before the column)",
        )
    return ScopedColumn(datasource, ".".join(parts[:-1]), parts[-1], entry=entry)


def parse_column_policy(
    entries: Iterable[str] | None,
    *,
    validate: bool = True,
    view: "SchemaView | None" = None,
) -> ColumnPolicy:
    """Parse a key's ``denied_columns`` into a :class:`ColumnPolicy`.

    Parameters
    ----------
    entries:
        The raw ``denied_columns`` strings (``None`` is no restriction).
    validate:
        Check each scoped entry against ``schema.yaml`` and
        ``datasources.yaml`` as well as its syntax. When the schema cannot be
        loaded the existence check is skipped with a log line and left to
        :func:`resolve_join_only`, which the guard runs on every query. Pass
        ``False`` to read the syntax only (what :attr:`Principal.column_policy
        <security.auth.Principal.column_policy>` does, so a request never
        pays for a schema load it does not need).
    view:
        The schema to check against; the loaded one when ``None``.

    Returns
    -------
    ColumnPolicy

    Raises
    ------
    ColumnPolicyError
        An entry is malformed or (with *validate*) names a source, table or
        column that does not exist. The message names the entry.

    Examples
    --------
    >>> policy = parse_column_policy(
    ...     ["NationalID", "sales.Order.ID", "Archive:ID"], validate=False
    ... )
    >>> sorted(policy.denied)
    ['NATIONALID']
    >>> [(s.datasource, s.table, s.column) for s in policy.join_only]
    [(None, 'sales.Order', 'ID'), ('Archive', None, 'ID')]
    >>> parse_column_policy(["a:b:c"], validate=False)
    Traceback (most recent call last):
        ...
    security.column_policy.ColumnPolicyError: denied_columns entry 'a:b:c': at most one ':' is allowed (Source:schema.Table.Column or Source:Column)
    >>> parse_column_policy(["sales..ID"], validate=False)
    Traceback (most recent call last):
        ...
    security.column_policy.ColumnPolicyError: denied_columns entry 'sales..ID': has an empty part (check for a stray '.' or ':')
    """
    if entries is None:
        return EMPTY_POLICY
    if isinstance(entries, str):
        raise ColumnPolicyError(
            "denied_columns must be a list of entries, not a single string"
        )
    denied: set[str] = set()
    scoped: dict[tuple[str, str, str], ScopedColumn] = {}
    for entry in entries:
        if not isinstance(entry, str):
            raise ColumnPolicyError(
                f"denied_columns entries must be strings, got {type(entry).__name__}"
            )
        if ":" not in entry and "." not in entry:
            denied.add(entry.upper())
            continue
        column = _parse_scoped(entry)
        scoped.setdefault(column.key(), column)
    policy = ColumnPolicy(denied=frozenset(denied), join_only=tuple(scoped.values()))
    if validate and policy.join_only:
        global _deferred_warning_logged
        try:
            view = view or load_schema_view()
        except Exception as exc:  # noqa: BLE001 - any failure to load means "not available yet"
            # Once per process: key loading repeats on every key-cache refresh.
            if not _deferred_warning_logged:
                _deferred_warning_logged = True
                logger.warning(
                    "denied_columns: the schema could not be loaded (%s); scoped "
                    "entries were checked for syntax only -- their tables and "
                    "columns are checked again when a query is validated",
                    type(exc).__name__,
                )
            return policy
        validate_column_policy(policy, view)
    return policy


@lru_cache(maxsize=256)
def _cached_policy(entries: frozenset[str]) -> ColumnPolicy:
    return parse_column_policy(entries, validate=False)


def cached_column_policy(entries: Iterable[str] | None) -> ColumnPolicy:
    """The syntax-only policy of *entries*, memoised.

    The guard and the prompt builder call this on every request with the
    key's raw ``denied_columns``; entries repeat, parsing does not. Order and
    repetition of *entries* do not change the result.

    Raises
    ------
    ColumnPolicyError
        As :func:`parse_column_policy` for a malformed entry.

    Examples
    --------
    >>> cached_column_policy(None) is EMPTY_POLICY
    True
    >>> cached_column_policy(["Name"]) is cached_column_policy(("Name", "Name"))
    True
    """
    if not entries:
        return EMPTY_POLICY
    if isinstance(entries, str):
        raise ColumnPolicyError(
            "denied_columns must be a list of entries, not a single string"
        )
    return _cached_policy(frozenset(entries))


# ---------------------------------------------------------------------------
# Checking entries against the schema and the data sources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaView:
    """The part of ``schema.yaml`` that entry checking needs.

    Passed in explicitly (rather than read inside) so the guard can hand over
    the very lookup it enforces with, and a test can supply a tiny schema.

    Attributes
    ----------
    columns_by_table:
        Canonical ``schema.yaml`` table key -> lower-cased column names.
    qualifiers:
        Canonical table key -> its effective qualifier, dot-joined
        (``"sales"``), ``""`` or absent when it has none.
    """

    columns_by_table: Mapping[str, frozenset[str]]
    qualifiers: Mapping[str, str] = field(default_factory=dict)


def load_schema_view() -> SchemaView:
    """The :class:`SchemaView` of the loaded ``schema.yaml``.

    Raises
    ------
    Exception
        Whatever loading the schema raises when it is missing or invalid
        (``knowledge.config_loader.ConfigNotFoundError``, a validation error).
    """
    from schema_data.registry import get_table_columns, get_table_schema_qualifiers

    return SchemaView(
        columns_by_table={
            key: frozenset(col.lower() for col in cols)
            for key, cols in get_table_columns().items()
        },
        qualifiers=dict(get_table_schema_qualifiers()),
    )


def _norm(text: str) -> str:
    """Case-folded dotted name with per-part brackets removed."""
    return ".".join(part.strip().strip("[]").casefold() for part in text.split("."))


def _bare(key: str) -> str:
    from schema_data.registry import bare_table_name

    return bare_table_name(key)


def _match_table(text: str, view: SchemaView) -> str | None:
    """The canonical table key *text* names, or ``None``.

    Tried in order: the key itself or ``<qualifier>.<name>`` (exact, case-
    insensitive), then a bare name that exactly one table has.

    Raises
    ------
    ColumnPolicyError
        More than one table matches the bare name (the message lists them).
    """
    wanted = _norm(text)
    exact = []
    for key in view.columns_by_table:
        qualifier = view.qualifiers.get(key, "")
        spellings = {_norm(key)}
        if qualifier:
            spellings.add(_norm(f"{qualifier}.{_bare(key)}"))
        if wanted in spellings:
            exact.append(key)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ColumnPolicyError(
            f"table {text!r} is ambiguous: matches {sorted(exact)}"
        )
    if "." in wanted:
        return None
    named = [key for key in view.columns_by_table if _bare(key).casefold() == wanted]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise ColumnPolicyError(
            f"table {text!r} exists in more than one schema {sorted(named)}; "
            "write schema.Table to say which"
        )
    return None


def _sources_of(table_key: str) -> tuple[str, ...]:
    """Every data source *table_key* lives in (routing's own answer)."""
    from database.routing import group_tables_by_datasource

    return tuple(group_tables_by_datasource([table_key]))


def _configured_source(name: str) -> str | None:
    """The configured spelling of data source *name* (case-insensitive), or ``None``."""
    from database.datasources import datasource_names

    for configured in datasource_names():
        if configured.casefold() == name.casefold():
            return configured
    return None


def _resolve_entry(
    scoped: ScopedColumn, view: SchemaView,
) -> tuple[str | None, tuple[str, ...]]:
    """``(configured source or None, canonical tables)`` for one entry.

    Raises
    ------
    ColumnPolicyError
        The source is not configured, the table is unknown or not in that
        source, or the column does not exist.
    """
    source: str | None = None
    if scoped.datasource is not None:
        source = _configured_source(scoped.datasource)
        if source is None:
            from database.datasources import datasource_names

            raise _entry_error(
                scoped.entry,
                f"data source {scoped.datasource!r} is not configured "
                f"(configured: {sorted(datasource_names())})",
            )
    column = scoped.column.casefold()

    if scoped.table is not None:
        try:
            key = _match_table(scoped.table, view)
        except ColumnPolicyError as exc:
            raise _entry_error(scoped.entry, str(exc)) from None
        if key is None:
            raise _entry_error(scoped.entry, f"table {scoped.table!r} is not in schema.yaml")
        if column not in view.columns_by_table[key]:
            raise _entry_error(
                scoped.entry, f"table {key!r} has no column {scoped.column!r}"
            )
        if source is not None and source not in _sources_of(key):
            raise _entry_error(
                scoped.entry,
                f"table {key!r} is not in data source {source!r} "
                f"(it is in: {sorted(_sources_of(key))})",
            )
        return source, (key,)

    # `Source:Col` -- every table of the source that has the column.
    assert source is not None  # a bare column without a source is a legacy entry
    tables = tuple(
        key for key, cols in view.columns_by_table.items()
        if column in cols and source in _sources_of(key)
    )
    if not tables:
        raise _entry_error(
            scoped.entry,
            f"no table of data source {source!r} has a column {scoped.column!r}",
        )
    return source, tables


def validate_column_policy(policy: ColumnPolicy, view: SchemaView | None = None) -> None:
    """Check every scoped entry of *policy* against the schema and data sources.

    Raises
    ------
    ColumnPolicyError
        The first entry that names a source, table or column that does not
        exist (see :func:`parse_column_policy`).
    """
    if not policy.join_only:
        return
    view = view or load_schema_view()
    for scoped in policy.join_only:
        _resolve_entry(scoped, view)


@dataclass(frozen=True)
class JoinOnlyRestrictions:
    """The join-only columns in force for one query.

    Attributes
    ----------
    by_table:
        Canonical table key -> ``{lower-cased column: policy entry}``.
    names:
        Every restricted column name, lower-cased (a quick pre-filter).
    """

    by_table: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    names: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.by_table)

    def entry(self, table: str, column: str) -> str | None:
        """The policy entry restricting *column* on *table*, or ``None``."""
        return self.by_table.get(table, {}).get(column.casefold())


def resolve_join_only(
    policy: ColumnPolicy, view: SchemaView, source: str | None,
) -> JoinOnlyRestrictions:
    """The join-only restrictions of *policy* that are active on *source*.

    Every entry is checked first (:func:`validate_column_policy`'s rules), so
    a typo raises here even when its source is not the executing one.

    Parameters
    ----------
    policy:
        The key's parsed policy.
    view:
        The schema to resolve table names against.
    source:
        The data source the query executes on. ``None`` activates every
        entry whatever its source (used where the executing source is not
        known and over-restricting is the safe side).

    Raises
    ------
    ColumnPolicyError
        As :func:`validate_column_policy`.
    """
    by_table: dict[str, dict[str, str]] = {}
    for scoped in policy.join_only:
        entry_source, tables = _resolve_entry(scoped, view)
        if (
            source is not None
            and entry_source is not None
            and entry_source.casefold() != source.casefold()
        ):
            continue
        column = scoped.column.casefold()
        for table in tables:
            by_table.setdefault(table, {}).setdefault(column, scoped.entry)
    names = frozenset(col for cols in by_table.values() for col in cols)
    return JoinOnlyRestrictions(by_table=by_table, names=names)


def hidden_value_columns(policy: ColumnPolicy) -> frozenset[tuple[str, str]]:
    """``(table key, lower-cased column)`` pairs whose *values* must not be shown.

    The value resolver and the dimension vocabulary read distinct values of a
    column and hand them to the prompt or the analyst; a join-only column
    must not leak that way either. Every entry counts here whatever its data
    source: those lookups do not know where the final query will run.

    Raises
    ------
    ColumnPolicyError
        An entry does not match the loaded schema.

    Examples
    --------
    >>> hidden_value_columns(EMPTY_POLICY)
    frozenset()
    """
    if not policy.join_only:
        return frozenset()
    restrictions = resolve_join_only(policy, load_schema_view(), None)
    return frozenset(
        (table, column)
        for table, columns in restrictions.by_table.items()
        for column in columns
    )


def join_only_prompt_line(entries: Iterable[str] | None, source: str | None) -> str:
    """The one-line prompt hint naming the join-only columns active on *source*.

    Built from the entries alone (no schema lookup), so it costs nothing on
    the request path. ``""`` when nothing applies.

    Parameters
    ----------
    entries:
        The key's raw ``denied_columns``.
    source:
        The data source the prompt is for; ``None`` is the deployment's
        default source.

    Examples
    --------
    >>> join_only_prompt_line(["Name"], None)
    ''
    >>> join_only_prompt_line(["sales.Order.ID"], None)
    'Columns you may use only in JOIN ... ON equality (a.col = b.col), never select, filter, group by or order by them: sales.Order.ID'
    """
    policy = cached_column_policy(entries)
    if not policy.join_only:
        return ""
    if source is None:
        from database.routing import default_datasource_name

        source = default_datasource_name()
    shown = [
        scoped.display()
        for scoped in policy.join_only
        if scoped.datasource is None or scoped.datasource.casefold() == source.casefold()
    ]
    if not shown:
        return ""
    return (
        "Columns you may use only in JOIN ... ON equality (a.col = b.col), never "
        "select, filter, group by or order by them: " + ", ".join(shown)
    )
