# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Offline retrieval-recall measurement for a golden set.

Table selection (:class:`~retrieval.context_retriever.ContextRetriever`) is
the first thing that can go wrong in the pipeline: a table that is not in the
prompt cannot appear in the SQL, whatever the model does. This module measures
that stage on its own, with **no LLM and no database**:

1. For every golden case with an ``expected_sql``, the *gold tables* are the
   tables that reference reads. They are resolved with the SQL guard's own
   table resolution (:func:`security.sql_guard.resolve_table_key`), so a gold
   table is spelled exactly like a ``schema.yaml`` key and compares equal to a
   retrieved one, schema qualifiers and same-name tables in two schemas
   included.
2. ``ContextRetriever.retrieve(question)`` runs and its ``selected_tables``
   (what the prompt would show on the retrieval path) are compared with the
   gold tables.
3. When several data sources are configured and a case states its
   ``datasource``, the source :func:`~retrieval.source_selector.select_source_for_question`
   picks for the retrieved context is compared with it.

What is reported (see :class:`RecallReport`):

* **recall** per case, ``|gold & retrieved| / |gold|``; its mean over cases;
  and the percentage of cases whose recall is 1.0 (every gold table present);
* **mean tables retrieved** and **mean precision** (``|gold & retrieved| /
  |retrieved|``), the cost side of the trade: recall can always be bought by
  retrieving more;
* **in-budget recall**: recall again, but a case whose retrieved tables would
  not fit ``PROMPT_RETRIEVAL_TOKEN_BUDGET`` (estimated from the schema block
  they render to) counts as 0. Retrieval falls back to "every table" for a
  question it understands nothing of, which scores full recall and yields an
  unusable prompt; this is the figure that does not reward it;
* **source-selection accuracy** over the cases that carry a ``datasource``;
* the same figures per golden-case tag.

A case whose ``expected_sql`` names a table the loaded ``schema.yaml`` does not
know (or only ambiguously) cannot be scored honestly -- counting it as a miss
would blame retrieval for a stale golden set -- so it is listed under
``skipped`` with the reason instead of entering any mean.

Nothing here reaches a database: the dimension-vocabulary background refresh
that ``ContextRetriever`` would otherwise start for a cold cache is switched
off for the duration of the run. Output is deterministic (tables sorted, no
timestamps), so two runs over the same inputs are byte-identical and a report
can be diffed.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from eval.models import GoldenCase

__all__ = [
    "GoldTables",
    "RecallCaseResult",
    "RecallReport",
    "SkippedCase",
    "SourceAccuracy",
    "TagRecall",
    "evaluate_recall",
    "gold_tables_from_sql",
    "render_recall_text",
    "report_to_json",
]

#: Golden-case statuses whose ``expected_sql`` a person has vouched for.
#: ``pending_review`` holds only a model's proposal, ``pending_expected`` has
#: no SQL at all; neither says which tables the right answer reads.
_SCORED_STATUSES: tuple[str, ...] = ("active", "reviewed")

#: ``retrieve(question)`` -> an object with a ``selected_tables`` list.
RetrieveFn = Callable[[str], Any]

#: ``select(question, context)`` -> a ``SourceSelection`` (or ``None`` when
#: fewer than two data sources are configured).
SelectSourceFn = Callable[[str, Any], Any]

#: ``tokens(tables)`` -> estimated tokens of the schema block those tables
#: render to in the prompt.
SchemaTokensFn = Callable[[Sequence[str]], int]


# ---------------------------------------------------------------------------
# Gold tables
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoldTables:
    """The tables a SQL statement reads, resolved against ``schema.yaml``.

    Attributes
    ----------
    resolved:
        Canonical ``schema.yaml`` keys, sorted and de-duplicated.
    unresolved:
        References that name no single allowlisted table (unknown, a
        qualifier that matches nothing, or a bare name two schemas share),
        written as they appear in the SQL, sorted and de-duplicated.
    """

    resolved: tuple[str, ...]
    unresolved: tuple[str, ...]


def gold_tables_from_sql(sql: str, dialect: str = "tsql") -> GoldTables:
    """Resolve every table *sql* reads to its ``schema.yaml`` key.

    Uses :func:`security.sql_guard.resolve_table_key`, the resolution the
    guard applies to a generated query, so the result is spelled like the
    keys retrieval returns. A CTE reference is not a table and is skipped; a
    reference that cannot be resolved is reported in
    :attr:`GoldTables.unresolved` rather than dropped silently (which would
    inflate recall).

    Parameters
    ----------
    sql:
        A reference query, T-SQL by default.
    dialect:
        sqlglot dialect used to parse *sql*.

    Returns
    -------
    GoldTables

    Raises
    ------
    ValueError
        If *sql* is empty or does not parse.

    Examples
    --------
    >>> from schema_data.columns import TABLE_COLUMNS
    >>> name = next(iter(TABLE_COLUMNS))
    >>> gold_tables_from_sql(f"SELECT COUNT(*) FROM {name}").resolved == (name,)
    True
    >>> gold_tables_from_sql("SELECT * FROM no_such_table_anywhere").unresolved
    ('no_such_table_anywhere',)
    >>> gold_tables_from_sql("")
    Traceback (most recent call last):
        ...
    ValueError: empty SQL
    """
    from security.sql_guard import resolve_table_key

    if not sql or not sql.strip():
        raise ValueError("empty SQL")
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError as exc:
        raise ValueError(f"SQL does not parse: {exc}") from exc
    if not statements:
        raise ValueError("empty SQL")

    resolved: set[str] = set()
    unresolved: set[str] = set()
    for tree in statements:
        cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
        for table in tree.find_all(exp.Table):
            name = table.name
            if not name or name.lower() in cte_names:
                continue  # a table-valued function, or a CTE reference
            key = resolve_table_key(table, tree)
            if key is None:
                unresolved.add(table.sql(dialect=dialect))
            else:
                resolved.add(key)
    return GoldTables(tuple(sorted(resolved)), tuple(sorted(unresolved)))


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecallCaseResult:
    """Retrieval outcome of one scored golden case.

    Attributes
    ----------
    case_id, question, tags:
        Copied from the :class:`~eval.models.GoldenCase`.
    gold:
        The tables the reference SQL reads (sorted).
    retrieved:
        ``context.selected_tables``, sorted.
    missed:
        ``gold`` tables that were not retrieved.
    recall:
        ``|gold & retrieved| / |gold|``.
    precision:
        ``|gold & retrieved| / |retrieved|`` (``0.0`` when nothing was
        retrieved).
    schema_tokens, within_budget:
        Estimated tokens of the schema block the retrieved tables render to,
        and whether that is within the token budget.
    expected_datasource, selected_datasource:
        The case's stated source and the one source selection picked;
        ``None`` when the case states none, or fewer than two sources are
        configured.
    source_correct:
        Whether the two agree; ``None`` when not scored.
    """

    case_id: str
    question: str
    tags: tuple[str, ...]
    gold: tuple[str, ...]
    retrieved: tuple[str, ...]
    missed: tuple[str, ...]
    recall: float
    precision: float
    expected_datasource: str | None = None
    selected_datasource: str | None = None
    source_correct: bool | None = None
    schema_tokens: int = 0
    within_budget: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON form of the record."""
        return {
            "case_id": self.case_id,
            "question": self.question,
            "tags": list(self.tags),
            "gold": list(self.gold),
            "retrieved": list(self.retrieved),
            "missed": list(self.missed),
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "schema_tokens": self.schema_tokens,
            "within_budget": self.within_budget,
            "expected_datasource": self.expected_datasource,
            "selected_datasource": self.selected_datasource,
            "source_correct": self.source_correct,
        }


@dataclass(frozen=True, slots=True)
class SkippedCase:
    """A golden case that could not be scored, and why."""

    case_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class TagRecall:
    """Recall figures for the scored cases carrying one tag."""

    cases: int
    mean_recall: float
    full_recall_pct: float
    mean_tables: float

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON form of the record."""
        return {
            "cases": self.cases,
            "mean_recall": round(self.mean_recall, 4),
            "full_recall_pct": round(self.full_recall_pct, 2),
            "mean_tables": round(self.mean_tables, 2),
        }


@dataclass(frozen=True, slots=True)
class SourceAccuracy:
    """Source-selection accuracy over the cases that state a ``datasource``.

    Attributes
    ----------
    correct, total:
        Cases where the selected source equals the stated one, and cases
        scored.
    unscorable:
        Cases whose stated ``datasource`` is not a configured source (a typo
        or a stale golden set); they are left out of ``correct``/``total``.
    """

    correct: int
    total: int
    unscorable: int = 0

    @property
    def pct(self) -> float:
        """``100 * correct / total`` (``0.0`` for no cases)."""
        return 100.0 * self.correct / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON form of the record."""
        return {
            "correct": self.correct,
            "total": self.total,
            "accuracy_pct": round(self.pct, 2),
            "unscorable": self.unscorable,
        }


@dataclass(frozen=True, slots=True)
class RecallReport:
    """Aggregate of a recall run.

    Attributes
    ----------
    total_cases:
        Cases in the golden file.
    scored:
        Cases that entered the means (``total_cases - len(skipped)``).
    skipped:
        Cases that could not be scored, each with its reason.
    mean_recall, full_recall_pct, mean_tables, mean_precision:
        Means over the scored cases; ``full_recall_pct`` is the percentage
        of them with every gold table retrieved.
    budget_tokens, over_budget, mean_recall_in_budget:
        The token budget applied, the number of scored cases whose retrieved
        tables exceeded it, and the mean recall with those cases counted as
        0.
    median_tables, max_tables:
        Median and largest number of tables retrieved for one question. A
        retriever that falls back to "every table" on a question it
        understands nothing of scores recall 1.0 there; the maximum (and a
        median far below the mean) is what shows it.
    by_tag:
        The same figures per tag, tags sorted.
    source_selection:
        :class:`SourceAccuracy`, or ``None`` when fewer than two data
        sources are configured or no scored case states a ``datasource``.
    cases:
        One :class:`RecallCaseResult` per scored case, in golden-file order.
    """

    total_cases: int
    scored: int
    skipped: tuple[SkippedCase, ...]
    mean_recall: float
    full_recall_pct: float
    mean_tables: float
    mean_precision: float
    median_tables: float = 0.0
    max_tables: int = 0
    budget_tokens: int = 0
    over_budget: int = 0
    mean_recall_in_budget: float = 0.0
    by_tag: dict[str, TagRecall] = field(default_factory=dict)
    source_selection: SourceAccuracy | None = None
    cases: tuple[RecallCaseResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON form of the report (stable key order, no timestamps)."""
        return {
            "total_cases": self.total_cases,
            "scored": self.scored,
            "skipped": [{"case_id": s.case_id, "reason": s.reason} for s in self.skipped],
            "mean_recall": round(self.mean_recall, 4),
            "full_recall_pct": round(self.full_recall_pct, 2),
            "mean_tables": round(self.mean_tables, 2),
            "median_tables": round(self.median_tables, 2),
            "max_tables": self.max_tables,
            "budget_tokens": self.budget_tokens,
            "over_budget": self.over_budget,
            "mean_recall_in_budget": round(self.mean_recall_in_budget, 4),
            "mean_precision": round(self.mean_precision, 4),
            "by_tag": {tag: rec.to_dict() for tag, rec in self.by_tag.items()},
            "source_selection": (
                None if self.source_selection is None else self.source_selection.to_dict()
            ),
            "cases": [c.to_dict() for c in self.cases],
        }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@contextmanager
def _no_background_refresh() -> Iterator[None]:
    """Keep ``ContextRetriever`` from starting a warehouse read.

    A cold dimension vocabulary makes ``match_question_against_vocabulary``
    start a background refresh; that is a database read, which an offline
    measurement must never cause. The switch is restored afterwards.
    """
    from retrieval import dimension_vocabulary

    previous = dimension_vocabulary.is_background_refresh_enabled()
    dimension_vocabulary.set_background_refresh_enabled(False)
    try:
        yield
    finally:
        dimension_vocabulary.set_background_refresh_enabled(previous)


def _default_retrieve(question: str) -> Any:
    from retrieval.context_retriever import ContextRetriever

    return ContextRetriever.retrieve(question)


def _default_select_source(question: str, context: Any) -> Any:
    from retrieval.source_selector import select_source_for_question

    return select_source_for_question(question, context)


def _default_schema_tokens(tables: Sequence[str]) -> int:
    from prompt_engine.static_prefix import estimate_tokens
    from schema_data.registry import SchemaRegistry

    return estimate_tokens(SchemaRegistry.build_schema_context(list(tables)))


def _default_budget() -> int:
    import config as cfg

    return int(cfg.settings.prompt_retrieval_token_budget)


def _configured_sources() -> tuple[str, ...]:
    from database.datasources import datasource_names

    return tuple(datasource_names())


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    return float(ordered[middle]) if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _tag_recall(results: Sequence[RecallCaseResult]) -> TagRecall:
    return TagRecall(
        cases=len(results),
        mean_recall=_mean([r.recall for r in results]),
        full_recall_pct=100.0 * sum(1 for r in results if not r.missed) / len(results),
        mean_tables=_mean([float(len(r.retrieved)) for r in results]),
    )


def evaluate_recall(
    cases: Sequence[GoldenCase],
    *,
    retrieve: RetrieveFn | None = None,
    select_source: SelectSourceFn | None = None,
    sources: Sequence[str] | None = None,
    token_budget: int | None = None,
    schema_tokens: SchemaTokensFn | None = None,
) -> RecallReport:
    """Measure table-retrieval recall over *cases*.

    Parameters
    ----------
    cases:
        Golden cases (see :func:`eval.runner.load_golden_cases`). Only
        ``active`` and ``reviewed`` cases with an ``expected_sql`` are scored;
        the rest are listed under ``skipped``.
    retrieve:
        ``question -> context`` with a ``selected_tables`` attribute. Defaults
        to :meth:`retrieval.context_retriever.ContextRetriever.retrieve`; a
        test injects a fake.
    select_source:
        ``(question, context) -> SourceSelection | None``. Defaults to
        :func:`retrieval.source_selector.select_source_for_question`, which
        returns ``None`` unless several data sources are configured.
    sources:
        The configured data source names, used to tell a case whose
        ``datasource`` is not a configured source from a wrong selection.
        Defaults to :func:`database.datasources.datasource_names`.

    Returns
    -------
    RecallReport

    Examples
    --------
    >>> from types import SimpleNamespace
    >>> from schema_data.columns import TABLE_COLUMNS
    >>> name = next(iter(TABLE_COLUMNS))
    >>> case = GoldenCase(id="c1", question="q", expected_sql=f"SELECT 1 FROM {name}")
    >>> hit = evaluate_recall([case], retrieve=lambda q: SimpleNamespace(selected_tables=[name]))
    >>> (hit.mean_recall, hit.full_recall_pct)
    (1.0, 100.0)
    >>> miss = evaluate_recall([case], retrieve=lambda q: SimpleNamespace(selected_tables=[]))
    >>> (miss.mean_recall, miss.cases[0].missed == (name,))
    (0.0, True)
    """
    retrieve_fn = retrieve or _default_retrieve
    select_fn = select_source or _default_select_source
    tokens_fn = schema_tokens or _default_schema_tokens
    budget = _default_budget() if token_budget is None else token_budget

    scored: list[RecallCaseResult] = []
    skipped: list[SkippedCase] = []
    source_correct = 0
    source_total = 0
    source_unscorable = 0

    with _no_background_refresh():
        configured = tuple(sources) if sources is not None else _configured_sources()
        for case in cases:
            if case.status not in _SCORED_STATUSES:
                skipped.append(SkippedCase(case.id, f"status {case.status!r} is not scored"))
                continue
            if not case.expected_sql:
                skipped.append(SkippedCase(case.id, "no expected_sql"))
                continue
            try:
                gold = gold_tables_from_sql(case.expected_sql)
            except ValueError as exc:
                skipped.append(SkippedCase(case.id, str(exc)))
                continue
            if gold.unresolved:
                skipped.append(SkippedCase(
                    case.id,
                    "expected_sql names table(s) the loaded schema.yaml does not "
                    f"resolve: {', '.join(gold.unresolved)}",
                ))
                continue
            if not gold.resolved:
                skipped.append(SkippedCase(case.id, "expected_sql reads no table"))
                continue

            context = retrieve_fn(case.question)
            retrieved = tuple(sorted(set(context.selected_tables)))
            used_tokens = tokens_fn(retrieved)
            hits = set(gold.resolved) & set(retrieved)
            missed = tuple(t for t in gold.resolved if t not in hits)

            expected_source: str | None = None
            selected_source: str | None = None
            correct: bool | None = None
            if case.datasource is not None:
                selection = select_fn(case.question, context)
                if selection is not None:
                    if case.datasource not in configured:
                        source_unscorable += 1
                    else:
                        expected_source = case.datasource
                        selected_source = selection.chosen
                        correct = selected_source == expected_source
                        source_total += 1
                        source_correct += int(correct)

            scored.append(RecallCaseResult(
                case_id=case.id,
                question=case.question,
                tags=tuple(case.tags),
                gold=gold.resolved,
                retrieved=retrieved,
                missed=missed,
                recall=len(hits) / len(gold.resolved),
                precision=len(hits) / len(retrieved) if retrieved else 0.0,
                expected_datasource=expected_source,
                selected_datasource=selected_source,
                source_correct=correct,
                schema_tokens=used_tokens,
                within_budget=used_tokens <= budget,
            ))

    by_tag_cases: dict[str, list[RecallCaseResult]] = defaultdict(list)
    for result in scored:
        for tag in result.tags:
            by_tag_cases[tag].append(result)

    source_accuracy = (
        SourceAccuracy(source_correct, source_total, source_unscorable)
        if source_total or source_unscorable
        else None
    )
    return RecallReport(
        total_cases=len(cases),
        scored=len(scored),
        skipped=tuple(skipped),
        mean_recall=_mean([r.recall for r in scored]),
        full_recall_pct=(
            100.0 * sum(1 for r in scored if not r.missed) / len(scored) if scored else 0.0
        ),
        mean_tables=_mean([float(len(r.retrieved)) for r in scored]),
        mean_precision=_mean([r.precision for r in scored]),
        median_tables=_median([float(len(r.retrieved)) for r in scored]),
        max_tables=max((len(r.retrieved) for r in scored), default=0),
        budget_tokens=budget,
        over_budget=sum(1 for r in scored if not r.within_budget),
        mean_recall_in_budget=_mean([r.recall if r.within_budget else 0.0 for r in scored]),
        by_tag={tag: _tag_recall(by_tag_cases[tag]) for tag in sorted(by_tag_cases)},
        source_selection=source_accuracy,
        cases=tuple(scored),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_recall_text(report: RecallReport, *, all_cases: bool = False) -> str:
    """Human-readable summary of *report*.

    Parameters
    ----------
    report:
        The result of :func:`evaluate_recall`.
    all_cases:
        List every scored case. By default only the cases that missed a gold
        table are listed (a full-recall case has nothing to investigate).

    Returns
    -------
    str
        Multi-line text, no trailing newline.

    Examples
    --------
    >>> text = render_recall_text(RecallReport(0, 0, (), 0.0, 0.0, 0.0, 0.0))
    >>> text.splitlines()[0]
    'Retrieval recall (offline: no LLM, no database)'
    """
    lines = ["Retrieval recall (offline: no LLM, no database)"]
    lines.append(
        f"  cases: {report.total_cases} in file, {report.scored} scored, "
        f"{len(report.skipped)} skipped"
    )
    lines.append(f"  mean recall:          {report.mean_recall:.4f}")
    lines.append(
        f"  in-budget recall:     {report.mean_recall_in_budget:.4f} "
        f"({report.over_budget} case(s) over {report.budget_tokens} estimated schema tokens count as 0)"
    )
    lines.append(f"  full-recall cases:    {report.full_recall_pct:.2f}%")
    lines.append(
        f"  tables retrieved:     mean {report.mean_tables:.2f}, "
        f"median {report.median_tables:g}, max {report.max_tables}"
    )
    lines.append(f"  mean precision:       {report.mean_precision:.4f}")
    if report.source_selection is None:
        lines.append("  source selection:     n/a (needs two or more data sources and cases with a datasource)")
    else:
        sel = report.source_selection
        extra = f", {sel.unscorable} case(s) name an unconfigured source" if sel.unscorable else ""
        lines.append(
            f"  source selection:     {sel.pct:.2f}% ({sel.correct}/{sel.total}){extra}"
        )

    if report.by_tag:
        lines.append("")
        lines.append("By tag:")
        width = max(len(tag) for tag in report.by_tag)
        for tag, rec in report.by_tag.items():
            lines.append(
                f"  {tag:<{width}}  n={rec.cases:<4} recall={rec.mean_recall:.4f}  "
                f"full={rec.full_recall_pct:6.2f}%  tables={rec.mean_tables:.2f}"
            )

    listed = [c for c in report.cases if all_cases or c.missed]
    if listed:
        lines.append("")
        lines.append("Cases:" if all_cases else "Cases missing a gold table:")
        for case in listed:
            missed = f"  missed: {', '.join(case.missed)}" if case.missed else ""
            lines.append(
                f"  {case.case_id}: recall={case.recall:.2f} "
                f"({len(case.retrieved)} retrieved){missed}"
            )

    if report.skipped:
        lines.append("")
        lines.append("Skipped (not scored):")
        for skip in report.skipped:
            lines.append(f"  {skip.case_id}: {skip.reason}")
    return "\n".join(lines)


def report_to_json(report: RecallReport) -> str:
    """Serialise *report* as indented, UTF-8-friendly JSON.

    Parameters
    ----------
    report:
        The result of :func:`evaluate_recall`.

    Returns
    -------
    str
        The same text for the same report, every time.

    Examples
    --------
    >>> json.loads(report_to_json(RecallReport(0, 0, (), 0.0, 0.0, 0.0, 0.0)))["scored"]
    0
    """
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
