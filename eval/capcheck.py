# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Is a capped result really the first rows of the right answer?

A result that has exactly the row cap's rows, every one of them a row of the
reference, looks like a correct answer cut short by policy
(:func:`eval.diagnosis.diagnose_results` calls it ``match_capped``). It can
also be an over-filtered query that happens to return at least a cap's worth of
rows, all of them valid: the analyst then sees a plausible but incomplete list
and the cap hides it. Told apart by one more read-only question: how many rows
would the model's own SQL have returned **without** the cap?

    SELECT COUNT(*) FROM (<the model's SQL, before the cap was injected>) AS q

* equal to the reference's row count: the query was right and only the cap cut
  it, ``match_capped`` stands;
* different: it is not the reference's answer, ``actual_is_subset`` and a
  failure, with the count recorded as ``uncapped_rows``;
* the count could not be asked (the SQL does not parse into a derived table,
  the guard refuses it, the executor fails or times out): a failure too,
  ``actual_is_subset`` with ``capped_unverified``. Never a silent pass.

The query is built from the syntax tree (sqlglot, ``tsql``), not by joining
strings. SQL Server rejects, in a derived table, an ``ORDER BY`` without
``TOP``/``OFFSET``, a ``WITH`` clause, a column without a name and two columns
with the same name; those are removed, hoisted or aliased (``_c1``, ...)
without changing which rows the statement returns. The statement runs through
the same executor as every other query of the run, so the query timeout and
the rolled-back read-only transaction apply, and only the count is read.
Nothing but that count leaves this module.

When the reference itself was cut at ``MAX_ROWS_RETURNED`` its length is not
the true size: the check then asks only that the uncapped count be at least
that long.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pandas as pd
import sqlglot
from sqlglot import exp

import config as cfg
from eval.models import MismatchDiagnosis

__all__ = ["count_query", "verify_capped_match"]

_COUNT_ALIAS = "q"


def _rightmost(tree: exp.Expression) -> exp.Expression:
    """The last SELECT of a set operation (where a trailing ORDER BY is attached)."""
    while isinstance(tree, exp.SetOperation):
        tree = tree.args["expression"]
    return tree


def _has_row_limit(select: exp.Expression) -> bool:
    return any(select.args.get(key) for key in ("limit", "offset", "fetch"))


def _name_projections(select: exp.Select) -> None:
    """Give every unnamed or repeated output column a unique alias (``_c1``, ...)."""
    seen: set[str] = set()
    projections = list(select.expressions)
    for index, projection in enumerate(projections, start=1):
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            continue  # a star cannot be renamed without the schema
        name = projection.alias_or_name.lower()
        if not name or name in seen:
            name = f"_c{index}"
            if isinstance(projection, exp.Alias):
                projection.set("alias", exp.to_identifier(name))
            else:
                projection.replace(exp.alias_(projection.copy(), name, quoted=False))
        seen.add(name)


def count_query(sql: str, dialect: str = "tsql") -> str:
    """``SELECT COUNT(*) FROM (<sql>) AS q``, valid in a derived table.

    Parameters
    ----------
    sql:
        The statement as the model wrote it (a ``tsql`` SELECT).
    dialect:
        The dialect to write the count query in; ``tsql`` unless the
        deployment targets another (the SQL is read as ``tsql`` either way).

    Raises
    ------
    ValueError
        If *sql* is not a single statement sqlglot can parse.

    Examples
    --------
    >>> count_query("SELECT ID FROM T WHERE a = 1 ORDER BY ID")
    'SELECT COUNT(*) FROM (SELECT ID AS ID FROM T WHERE a = 1) AS q'
    >>> count_query("SELECT TOP 5 ID FROM T ORDER BY ID")
    'SELECT COUNT(*) FROM (SELECT TOP 5 ID AS ID FROM T ORDER BY ID) AS q'
    >>> count_query("WITH c AS (SELECT 1 AS a) SELECT a FROM c")
    'WITH c AS (SELECT 1 AS a) SELECT COUNT(*) FROM (SELECT a AS a FROM c) AS q'
    >>> count_query("SELECT COUNT(*), a.x, b.x FROM a JOIN b ON 1 = 1")
    'SELECT COUNT(*) FROM (SELECT COUNT(*) AS _col_0, a.x AS x, b.x AS _c3 FROM a JOIN b ON 1 = 1) AS q'
    >>> count_query("SELECT a FROM x UNION ALL SELECT a FROM y ORDER BY a")
    'SELECT COUNT(*) FROM (SELECT a FROM x UNION ALL SELECT a FROM y) AS q'
    >>> count_query("SELECT ( FROM")
    Traceback (most recent call last):
        ...
    ValueError: cannot build a count query: the SQL does not parse
    """
    try:
        trees = sqlglot.parse(sql, dialect="tsql")
    except Exception as exc:  # noqa: BLE001 - any parser failure means "cannot count"
        raise ValueError("cannot build a count query: the SQL does not parse") from exc
    if len(trees) != 1 or trees[0] is None:
        raise ValueError("cannot build a count query: expected exactly one statement")
    tree = trees[0]
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        raise ValueError("cannot build a count query: not a SELECT")

    with_clause = tree.args.pop("with_", None)
    last = _rightmost(tree)
    if last.args.get("order") is not None and not _has_row_limit(last) and (
        isinstance(tree, exp.SetOperation) or not _has_row_limit(tree)
    ):
        last.set("order", None)
    if isinstance(tree, exp.Select):
        _name_projections(tree)

    outer = exp.select(exp.Count(this=exp.Star())).from_(
        exp.Subquery(this=tree, alias=exp.TableAlias(this=exp.to_identifier(_COUNT_ALIAS)))
    )
    if with_clause is not None:
        outer.set("with_", with_clause)
    return outer.sql(dialect=dialect)


def verify_capped_match(
    diagnosis: MismatchDiagnosis,
    precap_sql: str | None,
    execute_fn: Callable[[str], pd.DataFrame],
) -> MismatchDiagnosis:
    """Confirm or demote a ``match_capped`` diagnosis with one count query.

    Parameters
    ----------
    diagnosis:
        A diagnosis whose class is ``match_capped``; returned unchanged
        otherwise.
    precap_sql:
        The model's final SQL before the cap was injected (``None`` when the
        run did not record it: then the case cannot be verified).
    execute_fn:
        The run's executor. The count query is validated by the SQL guard and
        run through it, so the timeout and read-only transaction apply.

    Returns
    -------
    MismatchDiagnosis
        ``match_capped`` with ``uncapped_rows`` set when the count agrees with
        the reference; ``actual_is_subset`` with ``uncapped_rows`` when it does
        not; ``actual_is_subset`` with ``capped_unverified`` when it could not
        be asked. Only counts are ever recorded.

    Examples
    --------
    >>> import pandas as pd
    >>> from schema_data.columns import TABLE_COLUMNS
    >>> sql = f"SELECT ID FROM [{next(iter(TABLE_COLUMNS))}]"
    >>> d = MismatchDiagnosis("match_capped", 5, 3, ["a"], ["a"], capped=True, row_cap=3)
    >>> run = lambda sql: pd.DataFrame({"n": [5]})
    >>> verify_capped_match(d, sql, run).uncapped_rows
    5
    >>> verify_capped_match(d, sql, lambda sql: pd.DataFrame({"n": [4]})).mismatch_class
    'actual_is_subset'
    >>> verify_capped_match(d, "SELECT ( FROM", run).capped_unverified
    True
    """
    if diagnosis.mismatch_class != "match_capped":
        return diagnosis
    unverified = replace(
        diagnosis, mismatch_class="actual_is_subset", capped=False, capped_unverified=True,
        uncapped_rows=None,
    )
    if not precap_sql:
        return unverified
    try:
        statement = count_query(precap_sql, cfg.settings.sql_dialect)
        from security.sql_guard import validate_sql

        validate_sql(statement, dialect=cfg.settings.sql_dialect)
        frame = execute_fn(statement)
        counted = int(frame.iat[0, 0])
    except Exception:  # noqa: BLE001 - whatever stopped the count, the case is not a pass
        return unverified

    expected = diagnosis.expected_rows or 0
    reference_was_cut = expected >= cfg.settings.max_rows_returned
    agrees = counted >= expected if reference_was_cut else counted == expected
    if agrees:
        return replace(diagnosis, uncapped_rows=counted)
    return replace(
        diagnosis, mismatch_class="actual_is_subset", capped=False, uncapped_rows=counted,
    )
