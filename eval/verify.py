# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Run each case's reference SQL against the database and record what it returns.

``python -m eval.cli verify`` is the last step of building a golden set
(harvest -> review sheet -> import -> **verify**): it takes the cases a
person has reviewed (``status`` ``"reviewed"``) or that are already
``"active"``, executes every ``expected_sql`` read-only through the
application's own executor (the same routing, ``NOLOCK`` rewriting,
timeouts and rolled-back transaction as production), and

* reports every case whose reference does not hold up -- the guard
  rejects it, the database raises, it returns no rows although ``expect``
  says ``"success"`` (or rows although it says ``"empty"``);
* fills ``expected_rows`` and ``expected_fingerprint`` from the live
  result of each case that passed, which is what the offline CI replay
  needs;
* with ``accept=True``, and only then, moves the ``"reviewed"`` cases that
  passed to ``"active"`` so the regression gate starts running them.

The caller owns I/O: this module takes the cases and an ``execute_fn`` and
returns new cases plus a value-free outcome per case, so it is testable
with a fake executor and never prints row data itself. Rows are written
to the golden file (which lives only on the server, git-ignored) and
nowhere else; a report carries counts and case ids only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pandas as pd

import config as cfg
from eval.fingerprint import fingerprint_dataframe
from eval.models import GoldenCase
from eval.runner import ExecuteFn
from security.sql_guard import validate_sql

#: Statuses ``verify`` examines. Pending cases have nothing to verify yet.
_VERIFIABLE = ("reviewed", "active")


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """What verifying one case found.

    Parameters
    ----------
    case_id:
        The case.
    problem:
        ``None`` when the reference holds up, otherwise one of
        ``"guard_rejected"``, ``"database_error"``, ``"empty_result"``,
        ``"unexpected_rows"`` (``expect="empty"`` but rows came back),
        ``"duplicate_columns"`` (two result columns share a name, which a
        fingerprint and the recorded ``{column: value}`` rows cannot
        represent) or ``"replay_unstable"`` (the recorded rows would not
        reproduce the live fingerprint offline).
    detail:
        A short description for the report -- never a cell value.
    row_count:
        Rows the reference returned, ``None`` if it never ran.
    truncated:
        The result reached the row cap (``max_rows_returned``), so what is
        recorded is only the first part of a larger answer.
    accepted:
        The case moved from ``"reviewed"`` to ``"active"`` in this run.
    was_reviewed:
        The case entered this run with status ``"reviewed"``.
    """

    case_id: str
    problem: str | None = None
    detail: str = ""
    row_count: int | None = None
    truncated: bool = False
    accepted: bool = False
    was_reviewed: bool = False


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Outcome of :func:`verify_cases`.

    Parameters
    ----------
    cases:
        Every input case, in order, with recorded rows / fingerprint and
        status updated where verification succeeded.
    outcomes:
        One :class:`VerifyOutcome` per case that was examined.
    skipped:
        Number of cases left alone because their status is neither
        ``"reviewed"`` nor ``"active"``.
    changed:
        Whether any case differs from its input (i.e. whether the file
        needs rewriting).
    """

    cases: list[GoldenCase]
    outcomes: list[VerifyOutcome] = field(default_factory=list)
    skipped: int = 0
    changed: bool = False

    @property
    def problems(self) -> list[VerifyOutcome]:
        """The outcomes that found a problem."""
        return [o for o in self.outcomes if o.problem is not None]


def _jsonable(value: Any) -> Any:
    """One cell as a JSON value that replays to the same fingerprint.

    Mirrors :func:`eval.fingerprint._normalise_scalar` (so a frame rebuilt
    from the stored rows fingerprints like the live one) but keeps floats
    unrounded.

    Examples
    --------
    >>> _jsonable(Decimal("2.50")), _jsonable(None), _jsonable(float("nan"))
    (2.5, None, None)
    >>> _jsonable(datetime(2024, 1, 2, 3, 4)), _jsonable(date(2024, 1, 2))
    ('2024-01-02T03:04:00', '2024-01-02')
    >>> import numpy as np
    >>> _jsonable(np.int64(4)), type(_jsonable(np.int64(4))).__name__
    (4, 'int')
    """
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and type(value).__module__ == "numpy":
        value = value.item()
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return str(value)


def frame_to_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    """The rows of *df* as ``[{column: value}]`` suitable for ``expected_rows``.

    Parameters
    ----------
    df:
        A result set.

    Returns
    -------
    list[dict[str, Any]]
        JSON-safe rows; replaying them with ``pd.DataFrame(rows)`` yields a
        frame with the same fingerprint as *df* (for results without
        duplicate column names).

    Examples
    --------
    >>> df = pd.DataFrame({"n": [1, 2], "d": [Decimal("1.5"), None]})
    >>> frame_to_rows(df)
    [{'n': 1, 'd': 1.5}, {'n': 2, 'd': None}]
    """
    columns = [str(c) for c in df.columns]
    return [
        {name: _jsonable(value) for name, value in zip(columns, row)}
        for row in df.astype(object).itertuples(index=False, name=None)
    ]


def _verify_one(
    case: GoldenCase, execute_fn: ExecuteFn, *, accept: bool, refresh: bool
) -> tuple[GoldenCase, VerifyOutcome]:
    if case.is_out_of_scope:
        # Nothing to execute: the expectation is that the generator declines.
        accepted = accept and case.status == "reviewed"
        updated = replace(case, status="active") if accepted else case
        return updated, VerifyOutcome(case.id, detail="out of scope: nothing to execute", accepted=accepted)

    assert case.expected_sql is not None  # guaranteed for a reviewed/active, non-out-of-scope case
    try:
        validate_sql(case.expected_sql)
    except ValueError as exc:
        return case, VerifyOutcome(case.id, "guard_rejected", f"expected_sql rejected by the guard: {exc}")
    try:
        df = execute_fn(case.expected_sql)
    except Exception as exc:  # noqa: BLE001 - any database failure is a finding, not a crash
        return case, VerifyOutcome(
            case.id, "database_error", f"{type(exc).__name__}: {exc}"
        )

    row_count = len(df)
    truncated = row_count >= cfg.settings.max_rows_returned
    if case.expect == "success" and row_count == 0:
        return case, VerifyOutcome(
            case.id, "empty_result", "returned no rows but expect is 'success'",
            row_count=0,
        )
    if case.expect == "empty" and row_count > 0:
        return case, VerifyOutcome(
            case.id, "unexpected_rows",
            f"returned {row_count} row(s) but expect is 'empty'", row_count=row_count,
            truncated=truncated,
        )

    if df.columns.has_duplicates:
        return case, VerifyOutcome(
            case.id, "duplicate_columns",
            "the reference selects two columns with the same name; alias them so each "
            "result column is distinct",
            row_count=row_count, truncated=truncated,
        )

    rows = frame_to_rows(df)
    fingerprint = fingerprint_dataframe(df)
    if fingerprint_dataframe(pd.DataFrame(rows, columns=[str(c) for c in df.columns])) != fingerprint:
        return case, VerifyOutcome(
            case.id, "replay_unstable",
            "the recorded rows would not reproduce this result's fingerprint offline "
            "(an unusual value type)",
            row_count=row_count, truncated=truncated,
        )

    fill = (
        refresh
        or case.status == "reviewed"
        or case.expected_rows is None
        or case.expected_fingerprint is None
    )
    accepted = accept and case.status == "reviewed"
    updated = case
    if fill:
        updated = replace(updated, expected_rows=rows, expected_fingerprint=fingerprint)
    if accepted:
        updated = replace(updated, status="active")
    detail = "result reached the row cap; only its first rows are recorded" if truncated else ""
    return updated, VerifyOutcome(
        case.id, detail=detail, row_count=row_count, truncated=truncated, accepted=accepted
    )


def verify_cases(
    cases: Sequence[GoldenCase],
    execute_fn: ExecuteFn,
    *,
    accept: bool = False,
    refresh: bool = False,
) -> VerifyResult:
    """Execute every reviewable case's reference SQL and record the result.

    Parameters
    ----------
    cases:
        The whole golden set (every status), in file order.
    execute_fn:
        The application's executor
        (:func:`database.executor.execute_sql`); anything with the same
        contract in tests.
    accept:
        Move ``"reviewed"`` cases that passed to ``"active"``. Cases with a
        problem never move.
    refresh:
        Overwrite ``expected_rows`` / ``expected_fingerprint`` of ``"active"``
        cases that already have them. By default an active case's recorded
        answer is kept and only filled when missing; a ``"reviewed"``
        case's is always filled.

    Returns
    -------
    VerifyResult

    Examples
    --------
    >>> ok = GoldenCase(id="a", question="how many?", expected_sql="SELECT COUNT(*) AS n FROM Customer",
    ...                 status="reviewed")
    >>> result = verify_cases([ok], lambda sql: pd.DataFrame({"n": [3]}), accept=True)
    >>> result.cases[0].status, result.cases[0].expected_rows
    ('active', [{'n': 3}])
    >>> result.problems
    []

    Without ``accept`` the answer is recorded but the status stays:

    >>> verify_cases([ok], lambda sql: pd.DataFrame({"n": [3]})).cases[0].status
    'reviewed'

    A reference that returns nothing although a result is expected is
    reported and never accepted:

    >>> result = verify_cases([ok], lambda sql: pd.DataFrame({"n": []}), accept=True)
    >>> [(o.case_id, o.problem) for o in result.problems], result.cases[0].status
    ([('a', 'empty_result')], 'reviewed')
    """
    new_cases: list[GoldenCase] = []
    outcomes: list[VerifyOutcome] = []
    skipped = 0
    changed = False
    for case in cases:
        if case.status not in _VERIFIABLE:
            new_cases.append(case)
            skipped += 1
            continue
        updated, outcome = _verify_one(case, execute_fn, accept=accept, refresh=refresh)
        new_cases.append(updated)
        outcomes.append(replace(outcome, was_reviewed=case.status == "reviewed"))
        changed = changed or updated != case
    return VerifyResult(new_cases, outcomes, skipped, changed)


def render_verify_text(result: VerifyResult, *, accept: bool, written: bool) -> str:
    """Render *result* as a counts-and-ids summary (no questions, no rows).

    Parameters
    ----------
    result:
        From :func:`verify_cases`.
    accept:
        Whether ``--accept`` was given (changes the wording of the tail).
    written:
        Whether the golden file was rewritten.

    Returns
    -------
    str

    Examples
    --------
    >>> case = GoldenCase(id="a", question="q", expected_sql="SELECT COUNT(*) AS n FROM Customer",
    ...                   status="reviewed")
    >>> res = verify_cases([case], lambda sql: pd.DataFrame({"n": [3]}), accept=True)
    >>> print(render_verify_text(res, accept=True, written=True))
    Verified 1 case(s), skipped 0 (not reviewed or active).
      ok: 1, with a problem: 0, accepted (reviewed -> active): 1
    Golden file rewritten (previous version kept as .bak).
    """
    ok = sum(1 for o in result.outcomes if o.problem is None)
    accepted = sum(1 for o in result.outcomes if o.accepted)
    lines = [
        f"Verified {len(result.outcomes)} case(s), skipped {result.skipped} "
        "(not reviewed or active).",
        f"  ok: {ok}, with a problem: {len(result.problems)}, "
        f"accepted (reviewed -> active): {accepted}",
    ]
    for outcome in result.problems:
        lines.append(f"  PROBLEM {outcome.case_id}: {outcome.problem} -- {outcome.detail}")
    for outcome in result.outcomes:
        if outcome.problem is None and outcome.truncated:
            lines.append(f"  WARNING {outcome.case_id}: {outcome.detail}")
    pending = sum(
        1 for o in result.outcomes if o.problem is None and o.was_reviewed and not o.accepted
    )
    if pending:
        lines.append(f"  {pending} reviewed case(s) passed; re-run with --accept to activate them.")
    lines.append(
        "Golden file rewritten (previous version kept as .bak)."
        if written else "Golden file not modified."
    )
    return "\n".join(lines)
