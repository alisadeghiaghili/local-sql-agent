# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The live evaluation pipeline: generate the way the server does.

``--live`` used to make one model call per question, clean the text and hand
it to the guard once. The server does more: :class:`llm.sql_agent.SQLAgent`
validates what the model wrote, tells it what was wrong (with the 6.11.0
hints that name the columns or tables that do exist) and asks again, up to
:data:`llm.sql_agent.MAX_CORRECTION_ATTEMPTS` times, for a rejected statement
and for one that fails to run; it caps the rows of a statement that has no
cap (``ensure_top``) and routes the question to a data source. A harness that
skips all that measures the model alone, which is below what an analyst gets.

This module runs the same ``SQLAgent`` under the harness, so the number is
the analyst's. What it keeps identical to the server:

* the agent itself, built the way ``api.runner`` builds it (the router from
  ``LLM_*`` settings, ``MAX_CORRECTION_ATTEMPTS``), with its guard, its
  correctable and policy rejections, its correction hints and its source
  routing;
* the executor, :func:`database.executor.execute_sql`: the query timeout, the
  rolled-back read-only transaction and the ``max_rows_returned`` cap;
* the row cap the agent injects (``default_top_n``) into a statement with
  none. The reference SQL is run as written, with the executor's cap only, so
  a question whose reference returns more than ``default_top_n`` rows is
  scored as the analyst would see it: a subset.

What differs, deliberately: **no principal policy.** The server passes the
caller's ``denied_columns``; an evaluation has no caller, so none are passed
(the default, unrestricted evaluation principal). A deployment whose keys deny
columns is therefore measured as if they did not. And the result cache, the
interpretation step and the audit log are not part of generation and are not
used.

The agent is wrapped, not copied: :class:`TracingSQLAgent` overrides only the
guard step, to count each round and keep the rejection's class and message,
and the executor is wrapped to keep a run failure. The generator returns the
final SQL and the executor hands back the result that SQL already produced,
once, so it is not run twice; the reference query goes to the real executor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

import config as cfg

from eval.runner import ExecuteFn, GenerateFn, PipelineFailure, SourceTrace
from llm.sql_agent import SQLAgent
from security.sql_guard import clean_sql

if TYPE_CHECKING:
    from llm.router import LLMRouter

__all__ = ["TracingSQLAgent", "make_production_pipeline"]

_OUT_OF_SCOPE = "OUT_OF_SCOPE"


class TracingSQLAgent(SQLAgent):
    """:class:`~llm.sql_agent.SQLAgent` that reports each round to a :class:`SourceTrace`.

    Only the guard step is overridden: it counts the round and, when the
    guard rejects the statement, records the exception's class and message
    before re-raising it unchanged, so the correction loop behaves exactly as
    the server's does.
    """

    def __init__(self, *args: object, trace: SourceTrace, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._trace = trace

    def _clean_validate_cap(self, raw, *, denied_columns=None):  # type: ignore[override]
        self._trace.begin_attempt()
        try:
            result = super()._clean_validate_cap(raw, denied_columns=denied_columns)
            # The model's statement before ensure_top injected the cap: what the
            # capped-match check counts. clean_sql is pure, so this is the same text
            # the parent cleaned.
            self._trace.record_precap_sql(clean_sql(raw))
            return result
        except ValueError as exc:
            self._trace.record_guard_rejection(type(exc).__name__, str(exc))
            raise


def make_production_pipeline(
    system_prompt: str,
    trace: SourceTrace,
    execute_fn: ExecuteFn,
    *,
    router: LLMRouter | None = None,
) -> tuple[GenerateFn, ExecuteFn]:
    """Build the ``(generate_fn, execute_fn)`` pair of the production pipeline.

    Parameters
    ----------
    system_prompt:
        The system prompt text the server loads.
    trace:
        The run's :class:`~eval.runner.SourceTrace`; receives the data source
        chosen and, per round, the guard rejections and run failures.
    execute_fn:
        The real executor (:func:`database.executor.execute_sql`). Used by
        the agent for generated SQL and, through the returned ``execute_fn``,
        by :func:`~eval.runner.run_case` for the reference query.
    router:
        An :class:`~llm.router.LLMRouter`; ``LLMRouter.from_settings()`` (the
        server's) when omitted. Tests pass one over a fake backend.

    Returns
    -------
    tuple[GenerateFn, ExecuteFn]
        ``generate_fn(question)`` runs the agent and returns the SQL it
        settled on; raises ``ValueError("OUT_OF_SCOPE")`` when the model
        declines, :class:`~eval.runner.PipelineFailure` when the correction
        rounds ran out, and the original exception for anything else (a
        transport failure). ``execute_fn(sql)`` returns the result the agent
        already got for that SQL, once, and otherwise runs *sql*.

    Examples
    --------
    A model that answers correctly at once costs no correction:

    >>> from llm.providers import MockBackend
    >>> from llm.router import LLMRouter
    >>> from schema_data.columns import TABLE_COLUMNS
    >>> import pandas as pd
    >>> table = next(iter(TABLE_COLUMNS))
    >>> trace = SourceTrace()
    >>> frame = pd.DataFrame({"n": [1]})
    >>> generate, execute = make_production_pipeline(
    ...     "You are a T-SQL expert.", trace, lambda sql: frame,
    ...     router=LLMRouter(default_chain=[MockBackend(
    ...         response=f"SELECT COUNT(*) AS n FROM [{table}]")]))
    >>> sql = generate("how many?")
    >>> execute(sql) is frame, trace.corrections
    (True, 0)
    """
    from llm.router import LLMRouter

    memo: dict[str, pd.DataFrame] = {}

    def traced_execute(sql: str) -> pd.DataFrame:
        try:
            return execute_fn(sql)
        except Exception as exc:  # noqa: BLE001 - recorded, then handled by the agent's loop
            trace.record_execution_error(type(exc).__name__, str(exc))
            raise

    agent = TracingSQLAgent(
        router=router if router is not None else LLMRouter.from_settings(),
        execute_fn=traced_execute,
        trace=trace,
    )

    def _record_selection(source: object) -> None:
        selection = getattr(source, "datasource_selection", None)
        if isinstance(selection, dict):
            trace.record(selection.get("chosen"))

    def generate(question: str) -> str:
        memo.clear()
        try:
            frame, result = agent.run(question, system_prompt, denied_columns=None)
        except ValueError as exc:
            _record_selection(exc)
            if str(exc) == _OUT_OF_SCOPE or not hasattr(exc, "candidate_sql"):
                raise
            raise PipelineFailure("guard_rejected", str(exc), exc.candidate_sql) from exc  # type: ignore[attr-defined]
        except RuntimeError as exc:
            _record_selection(exc)
            if not hasattr(exc, "candidate_sql"):
                raise  # not a run failure of a guard-approved statement: a transport failure
            raise PipelineFailure("execution_error", str(exc), exc.candidate_sql) from exc  # type: ignore[attr-defined]
        _record_selection(result)
        # The server cuts a result at the TOP it injected and, whatever the SQL says, at
        # MAX_ROWS_RETURNED: the lower of the two is what the analyst could ever see.
        cap = cfg.settings.max_rows_returned
        if result.injected_top:
            cap = min(cap, result.injected_top)
        trace.record_row_cap(cap)
        memo[result.sql] = frame
        return result.sql

    def execute(sql: str) -> pd.DataFrame:
        if sql in memo:
            return memo.pop(sql)
        return execute_fn(sql)

    return generate, execute
