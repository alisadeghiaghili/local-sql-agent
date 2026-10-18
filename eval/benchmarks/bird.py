# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""BIRD mini-dev as a benchmark track: the dataset, and one golden case per question.

BIRD mini-dev (Li et al., "Can LLM Already Serve as A Database Interface? A
BIg bench for Large-scale Database Grounded text-to-SQLs", NeurIPS 2023; the
mini-dev release) has 500 questions over 11 SQLite databases, published as
SQLite, MySQL and PostgreSQL but not T-SQL. It is distributed under CC BY-SA
4.0 (runbook 18.7 has the sources). Nothing of it is part of this repository.

On-disk layout read here (the downloaded package)::

    <root>/mini_dev_sqlite.json                       question_id, db_id, question, evidence, SQL, difficulty
    <root>/dev_databases/<db_id>/<db_id>.sqlite
    <root>/dev_databases/<db_id>/database_description/<table>.csv

Where the reference result comes from
-------------------------------------
The expected result of every question is produced by running the dataset's own
``SQL`` on the dataset's own ``.sqlite`` file, opened read-only. There is no
translated reference, so a translation error cannot make a question wrong.

The model is asked for T-SQL against a SQL Server copy of the data, so one more
thing could go wrong: the copy might not answer the question the way the
original does (case-sensitive text comparison, integer division, a date held as
text...). The **load-fidelity check** catches that. The reference SQL is
transpiled to T-SQL with ``sqlglot``, run on the SQL Server copy, and its
result compared with the SQLite result by execution accuracy
(:func:`eval.compare.compare_to_reference`). A question whose results differ is
excluded from scoring with the reason ``load_fidelity``: it measures the load,
not the model. The transpilation is used for this check and kept as the case's
``expected_sql`` (for ordering, table retrieval and reading); in a stored run
it is never executed.

Cases that are not scored
-------------------------
A case is ``excluded`` (status; :data:`EXCLUSION_REASONS` lists the reasons) and
stays in the file, so the import report can list it:

``reference_failed``
    The SQLite reference raised an error or ran longer than the time limit.
``reference_empty``
    It returned no row, or only NULL cells; there is nothing to compare with.
``reference_too_large``
    It returned as many rows as the server's row cap, so the model's capped
    result could never equal it.
``load_fidelity``
    The transpiled reference ran on the SQL Server copy and returned another result.
``fidelity_unverified``
    The reference could not be transpiled, or the transpilation failed on the
    SQL Server copy, so the copy could not be checked for this question.
    ``keep_unverified`` scores these anyway.
"""

from __future__ import annotations

import csv
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import sqlglot
from sqlalchemy.engine import Engine
from sqlglot import exp

from eval.benchmarks.sqlite_load import open_sqlite_readonly
from eval.compare import ComparisonOptions, compare_to_reference
from eval.fingerprint import fingerprint_dataframe
from eval.models import GoldenCase
from eval.rows import frame_to_rows

__all__ = [
    "DEFAULT_QUERY_TIMEOUT",
    "Tally",
    "build_cases",
    "open_connections",
    "BENCH_NAME",
    "BENCH_TAG",
    "EVIDENCE_MARKER",
    "EXCLUSION_REASONS",
    "BirdQuestion",
    "BirdSource",
    "Decision",
    "Reference",
    "check_fidelity",
    "decide",
    "golden_case",
    "load_questions",
    "locate_source",
    "read_descriptions",
    "run_reference",
    "target_dialect",
    "transpile_reference",
    "unique_columns",
]

BENCH_NAME = "bird"
BENCH_TAG = f"bench:{BENCH_NAME}"

#: How the evidence is marked when it is appended to a question (the ablation).
EVIDENCE_MARKER = "Hint (external knowledge from the benchmark's annotators):"

#: Why an imported case is excluded from scoring.
EXCLUSION_REASONS: tuple[str, ...] = (
    "reference_failed",
    "reference_empty",
    "reference_too_large",
    "load_fidelity",
    "fidelity_unverified",
)

#: A reference that runs longer than this on SQLite is excluded (seconds).
DEFAULT_QUERY_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# The dataset
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BirdQuestion:
    """One entry of ``mini_dev_sqlite.json``.

    Parameters
    ----------
    question_id:
        The dataset's id (as text); the position when the file has none.
    db_id:
        The database the question is about.
    question, evidence, sql:
        The question, the annotators' hint (may be empty) and the gold SQLite query.
    difficulty:
        ``simple``, ``moderate`` or ``challenging`` (``unknown`` when absent).
    """

    question_id: str
    db_id: str
    question: str
    evidence: str
    sql: str
    difficulty: str = "unknown"


@dataclass(frozen=True, slots=True)
class BirdSource:
    """A located BIRD package on disk."""

    root: Path
    questions_file: Path
    databases_dir: Path

    def sqlite_path(self, db_id: str) -> Path:
        """The ``.sqlite`` file of *db_id*."""
        return self.databases_dir / db_id / f"{db_id}.sqlite"

    def description_dir(self, db_id: str) -> Path:
        """The folder of ``database_description/*.csv`` of *db_id*."""
        return self.databases_dir / db_id / "database_description"

    def database_ids(self) -> list[str]:
        """Every database that has its ``.sqlite`` file, sorted."""
        if not self.databases_dir.is_dir():
            return []
        return sorted(
            p.name for p in self.databases_dir.iterdir() if (p / f"{p.name}.sqlite").is_file()
        )


def locate_source(root: str | Path) -> BirdSource:
    """Find ``mini_dev_sqlite.json`` and ``dev_databases/`` under *root*.

    The package unpacks into a nested folder (``minidev/MINIDEV`` or
    ``mini_dev_data``), so the shallowest match under *root* is used.

    Raises
    ------
    FileNotFoundError
        If there is no ``mini_dev_sqlite.json`` below *root*, or no
        ``dev_databases`` folder beside it.

    Examples
    --------
    >>> import tempfile
    >>> d = Path(tempfile.mkdtemp()) / "pkg" / "MINIDEV"
    >>> (d / "dev_databases").mkdir(parents=True)
    >>> _ = (d / "mini_dev_sqlite.json").write_text("[]", encoding="utf-8")
    >>> locate_source(d.parent.parent).questions_file.name
    'mini_dev_sqlite.json'
    >>> locate_source(tempfile.mkdtemp())
    Traceback (most recent call last):
        ...
    FileNotFoundError: no mini_dev_sqlite.json under ...
    """
    base = Path(root)
    if not base.is_dir():
        raise FileNotFoundError(f"source directory not found: {base}")
    found = sorted(base.rglob("mini_dev_sqlite.json"), key=lambda p: (len(p.parts), str(p)))
    if not found:
        raise FileNotFoundError(f"no mini_dev_sqlite.json under {base}")
    questions = found[0]
    databases = questions.parent / "dev_databases"
    if not databases.is_dir():
        raise FileNotFoundError(f"no dev_databases/ folder beside {questions}")
    return BirdSource(root=questions.parent, questions_file=questions, databases_dir=databases)


def load_questions(path: str | Path) -> list[BirdQuestion]:
    """Read ``mini_dev_sqlite.json``.

    ``db_id``, ``question`` and ``SQL`` are required. ``evidence`` may be
    absent or null (read as empty); ``question_id`` and ``difficulty`` may be
    absent (the position and ``unknown``). Other keys are ignored.

    Raises
    ------
    ValueError
        If the file is not a JSON list of objects, an entry lacks a required
        key, or two entries share a ``question_id``.

    Examples
    --------
    >>> import tempfile
    >>> p = Path(tempfile.mkdtemp()) / "q.json"
    >>> _ = p.write_text(json.dumps([
    ...     {"question_id": 7, "db_id": "shop", "question": "How many?", "evidence": None,
    ...      "SQL": "SELECT 1", "difficulty": "simple"},
    ...     {"db_id": "shop", "question": "Which?", "SQL": "SELECT 2"}]), encoding="utf-8")
    >>> [(q.question_id, q.difficulty, q.evidence) for q in load_questions(p)]
    [('7', 'simple', ''), ('1', 'unknown', '')]
    """
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p.name}: not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"{p.name}: expected a JSON list of questions")
    questions: list[BirdQuestion] = []
    seen: set[str] = set()
    for position, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"{p.name}: entry {position} is not an object")
        missing = [k for k in ("db_id", "question", "SQL") if not str(item.get(k) or "").strip()]
        if missing:
            raise ValueError(f"{p.name}: entry {position} lacks {', '.join(missing)}")
        raw_id = item.get("question_id")
        question_id = str(raw_id) if raw_id is not None else str(position)
        if question_id in seen:
            raise ValueError(f"{p.name}: question_id {question_id} appears twice")
        seen.add(question_id)
        questions.append(
            BirdQuestion(
                question_id=question_id,
                db_id=str(item["db_id"]).strip(),
                question=str(item["question"]).strip(),
                evidence=str(item.get("evidence") or "").strip(),
                sql=str(item["SQL"]).strip(),
                difficulty=str(item.get("difficulty") or "unknown").strip().lower(),
            )
        )
    return questions


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes almost everything; this is the floor
        text = path.read_text(encoding="utf-8", errors="replace")
    reader = csv.DictReader(text.splitlines())
    return [
        {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        for row in reader
    ]


def read_descriptions(description_dir: str | Path) -> dict[str, dict[str, str]]:
    """The column descriptions of one database: ``{table: {column: text}}``, lower-cased keys.

    Reads ``database_description/<table>.csv`` (columns ``original_column_name``,
    ``column_name``, ``column_description``, ``data_format``,
    ``value_description``). The text joins the description, the data format and
    the value description; a column with none of them is left out. A missing
    folder gives ``{}``.

    Examples
    --------
    >>> import tempfile
    >>> d = Path(tempfile.mkdtemp())
    >>> _ = (d / "Orders.csv").write_text(
    ...     "original_column_name,column_name,column_description,data_format,value_description\\n"
    ...     "Total,total,order total,real,in euros\\n"
    ...     "Note,note,,text,\\n"
    ...     "Plain,plain,,,\\n", encoding="utf-8")
    >>> read_descriptions(d)
    {'orders': {'total': 'order total (real). in euros', 'note': '(text)'}}
    """
    folder = Path(description_dir)
    result: dict[str, dict[str, str]] = {}
    if not folder.is_dir():
        return result
    for csv_file in sorted(folder.glob("*.csv")):
        columns: dict[str, str] = {}
        for row in _read_csv_rows(csv_file):
            name = row.get("original_column_name") or row.get("column_name") or ""
            if not name:
                continue
            description = row.get("column_description", "")
            data_format = row.get("data_format", "")
            values = row.get("value_description", "")
            parts = [description] if description else []
            if data_format:
                parts.append(f"({data_format})")
            text = " ".join(parts)
            if values:
                text = f"{text}. {values}" if text else values
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                columns[name.lower()] = text
        result[csv_file.stem.lower()] = columns
    return result


# ---------------------------------------------------------------------------
# Reference results
# ---------------------------------------------------------------------------


def unique_columns(names: Sequence[str]) -> list[str]:
    """Column names made distinct (a gold query may select two columns of one name).

    Examples
    --------
    >>> unique_columns(["id", "name", "id", "", "name"])
    ['id', 'name', 'id_2', 'column_4', 'name_2']
    """
    seen: dict[str, int] = {}
    result: list[str] = []
    for position, name in enumerate(names, start=1):
        base = name or f"column_{position}"
        count = seen.get(base.lower(), 0) + 1
        seen[base.lower()] = count
        result.append(base if count == 1 else f"{base}_{count}")
    return result


@dataclass(frozen=True, slots=True)
class Reference:
    """What the gold SQL returned on the original SQLite database.

    Parameters
    ----------
    outcome:
        ``ok``, ``failed`` (error or timeout), ``empty`` (no rows, or only
        NULLs) or ``too_large`` (as many rows as the row cap).
    frame:
        The result when ``ok``.
    detail:
        A short, value-free explanation otherwise.
    """

    outcome: Literal["ok", "failed", "empty", "too_large"]
    frame: pd.DataFrame | None = None
    detail: str = ""


def run_reference(
    conn: Any, sql: str, *, max_rows: int, timeout: float = DEFAULT_QUERY_TIMEOUT
) -> Reference:
    """Run the gold *sql* on an open (read-only) SQLite connection.

    Parameters
    ----------
    conn:
        From :func:`eval.benchmarks.sqlite_load.open_sqlite_readonly`.
    sql:
        The gold query.
    max_rows:
        The server's row cap (``MAX_ROWS_RETURNED``); a result that reaches it is ``too_large``.
    timeout:
        Seconds before the query is interrupted.

    Examples
    --------
    >>> import sqlite3
    >>> conn = sqlite3.connect(":memory:")
    >>> _ = conn.execute("CREATE TABLE t (a, b)")
    >>> _ = conn.execute("INSERT INTO t VALUES (1, 'x'), (2, NULL)")
    >>> run_reference(conn, "SELECT a, b FROM t ORDER BY a", max_rows=10).frame.values.tolist()
    [[1, 'x'], [2, None]]
    >>> run_reference(conn, "SELECT a FROM t WHERE a > 5", max_rows=10).outcome
    'empty'
    >>> run_reference(conn, "SELECT MAX(a) FROM t WHERE a > 5", max_rows=10).outcome
    'empty'
    >>> run_reference(conn, "SELECT a FROM t", max_rows=2).outcome
    'too_large'
    >>> run_reference(conn, "SELECT nope FROM t", max_rows=10).outcome
    'failed'
    """
    deadline = time.monotonic() + timeout
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cursor = conn.execute(sql)
        names = unique_columns([d[0] for d in cursor.description or ()])
        rows = cursor.fetchmany(max_rows)
    except Exception as exc:  # noqa: BLE001 - any SQLite failure is an exclusion, not a crash
        message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        if "interrupted" in message.lower():
            message = f"stopped after {timeout:g} s"
        return Reference("failed", detail=f"{type(exc).__name__}: {message}")
    finally:
        conn.set_progress_handler(None, 0)
    if not rows or all(cell is None for row in rows for cell in row):
        return Reference("empty", detail="no row" if not rows else "only NULL cells")
    if len(rows) >= max_rows:
        return Reference("too_large", detail=f"{len(rows)} rows or more (row cap {max_rows})")
    return Reference("ok", frame=pd.DataFrame(rows, columns=names))


# ---------------------------------------------------------------------------
# The SQL Server copy
# ---------------------------------------------------------------------------


def target_dialect(engine: Engine) -> str:
    """The sqlglot dialect that writes SQL for *engine*'s database (``tsql`` for SQL Server).

    Examples
    --------
    >>> import sqlalchemy as sa
    >>> target_dialect(sa.create_engine("sqlite://")), target_dialect(sa.create_engine("mssql+pyodbc://"))
    ('sqlite', 'tsql')
    """
    name = engine.dialect.name
    return "tsql" if name == "mssql" else name


def transpile_reference(sql: str, schema: str, dialect: str) -> str:
    """Rewrite the gold SQLite *sql* for the target, with every table qualified by *schema*.

    Identifiers are all quoted (so a column called ``Date`` or ``Order`` is
    safe). A name defined by a ``WITH`` clause is left unqualified.

    Raises
    ------
    sqlglot.errors.SqlglotError
        If the query cannot be parsed or written.

    Examples
    --------
    >>> transpile_reference("SELECT T1.name FROM schools AS T1 ORDER BY T1.name LIMIT 3", "ca", "tsql")
    'SELECT TOP 3 [T1].[name] FROM [ca].[schools] AS [T1] ORDER BY [T1].[name]'
    >>> transpile_reference("WITH c AS (SELECT 1 AS n) SELECT n FROM c", "ca", "tsql")
    'WITH [c] AS (SELECT 1 AS [n]) SELECT [n] FROM [c]'
    """
    tree = sqlglot.parse_one(sql, read="sqlite")
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if table.args.get("db") or not table.name or table.name.lower() in ctes:
            continue
        table.set("db", exp.to_identifier(schema, quoted=True))
    return tree.sql(dialect=dialect, identify=True)


def check_fidelity(
    engine: Engine,
    expected_sql: str,
    reference: pd.DataFrame,
    *,
    dialect: str,
    options: ComparisonOptions | None = None,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
) -> tuple[Literal["same", "differs", "error"], str]:
    """Run the transpiled reference on the SQL Server copy and compare it with the SQLite result.

    Returns
    -------
    tuple[str, str]
        ``("same", "")``, ``("differs", reason)`` with the comparison's
        value-free reason, or ``("error", message)`` when the target rejected
        the query.
    """
    try:
        with engine.connect() as conn:
            try:
                conn.connection.driver_connection.timeout = int(timeout)  # type: ignore[union-attr]
            except (AttributeError, TypeError):  # pragma: no cover - driver without a query timeout
                pass
            result = conn.exec_driver_sql(expected_sql)
            frame = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
    except Exception as exc:  # noqa: BLE001 - the target refusing the query is a finding
        message = str(getattr(exc, "orig", exc)).splitlines()
        return ("error", f"{type(exc).__name__}: {message[0] if message else ''}")
    comparison = compare_to_reference(
        expected_sql, reference, frame, options=options, dialect=dialect
    )
    return ("same", "") if comparison.equal else ("differs", comparison.reason)


# ---------------------------------------------------------------------------
# One question -> one decision -> one golden case
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decision:
    """What the importer decided about one question.

    Parameters
    ----------
    reason:
        ``None`` when the case is scored, else one of :data:`EXCLUSION_REASONS`.
    detail:
        A short, value-free explanation of the reason.
    expected_sql:
        The transpiled reference (or the SQLite SQL when it could not be transpiled).
    frame:
        The SQLite result, when there is one.
    """

    reason: str | None
    detail: str
    expected_sql: str
    frame: pd.DataFrame | None = None


def decide(
    question: BirdQuestion,
    source_conn: Any,
    engine: Engine | None,
    *,
    max_rows: int,
    keep_unverified: bool = False,
    options: ComparisonOptions | None = None,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
    dialect: str | None = None,
) -> Decision:
    """Run the reference, transpile it, check the load, and decide whether the case is scored.

    Parameters
    ----------
    question:
        The question.
    source_conn:
        Read-only connection to the original SQLite database.
    engine:
        The SQL Server copy (admin login). ``None`` skips the load-fidelity
        check, and every question is then ``fidelity_unverified`` unless
        *keep_unverified*.
    max_rows:
        The row cap of the server.
    keep_unverified:
        Score a case whose load fidelity could not be checked.
    options, timeout, dialect:
        Comparison tolerance; seconds a query may run; the target's sqlglot
        dialect (derived from *engine* when omitted).

    Examples
    --------
    >>> import sqlite3
    >>> conn = sqlite3.connect(":memory:")
    >>> _ = conn.execute("CREATE TABLE t (a)")
    >>> _ = conn.execute("INSERT INTO t VALUES (1)")
    >>> q = BirdQuestion("1", "ca", "Which?", "", "SELECT a FROM t")
    >>> d = decide(q, conn, None, max_rows=100, dialect="tsql")
    >>> d.reason, d.expected_sql
    ('fidelity_unverified', 'SELECT [a] FROM [ca].[t]')
    >>> decide(q, conn, None, max_rows=100, dialect="tsql", keep_unverified=True).reason is None
    True
    >>> decide(BirdQuestion("2", "ca", "q", "", "SELECT x FROM t"), conn, None, max_rows=100).reason
    'reference_failed'
    """
    reference = run_reference(source_conn, question.sql, max_rows=max_rows, timeout=timeout)
    if reference.outcome != "ok":
        reason = {
            "failed": "reference_failed",
            "empty": "reference_empty",
            "too_large": "reference_too_large",
        }[reference.outcome]
        return Decision(reason, reference.detail, question.sql)

    frame = reference.frame
    assert frame is not None
    target = dialect or (target_dialect(engine) if engine is not None else "tsql")
    try:
        expected_sql = transpile_reference(question.sql, question.db_id, target)
    except Exception as exc:  # noqa: BLE001 - sqlglot raises several error types
        detail = f"could not be transpiled ({type(exc).__name__})"
        return Decision(None if keep_unverified else "fidelity_unverified", detail, question.sql, frame)

    if engine is None:
        return Decision(
            None if keep_unverified else "fidelity_unverified",
            "the SQL Server copy was not queried", expected_sql, frame,
        )
    verdict, message = check_fidelity(
        engine, expected_sql, frame, dialect=target, options=options, timeout=timeout
    )
    if verdict == "same":
        return Decision(None, "", expected_sql, frame)
    if verdict == "differs":
        return Decision("load_fidelity", message, expected_sql, frame)
    return Decision(
        None if keep_unverified else "fidelity_unverified",
        f"the transpiled reference failed on the copy ({message})", expected_sql, frame,
    )


def golden_case(
    question: BirdQuestion, decision: Decision, *, with_evidence: bool = False
) -> GoldenCase:
    """The golden case of *question* under *decision*.

    Tags are ``bench:bird``, ``db:<db_id>`` and ``difficulty:<x>``. A scored
    case carries the SQLite result as ``expected_rows`` with ``match="execution"``,
    which is what ``run --reference stored`` compares with.

    Parameters
    ----------
    question, decision:
        From :func:`load_questions` and :func:`decide`.
    with_evidence:
        Append the evidence to the question, under :data:`EVIDENCE_MARKER`
        (the ablation). A question without evidence is unchanged.

    Examples
    --------
    >>> q = BirdQuestion("12", "ca", "How many schools?", "schools = rows of frpm", "SELECT COUNT(*) FROM frpm", "simple")
    >>> frame = pd.DataFrame({"COUNT(*)": [9]})
    >>> case = golden_case(q, Decision(None, "", "SELECT COUNT(*) FROM [ca].[frpm]", frame))
    >>> case.id, case.tags, case.status, case.match, case.expected_rows
    ('bird_12', ['bench:bird', 'db:ca', 'difficulty:simple'], 'active', 'execution', [{'COUNT(*)': 9}])
    >>> case.question
    'How many schools?'
    >>> golden_case(q, Decision(None, "", "SELECT 1", frame), with_evidence=True).question.splitlines()[-1]
    'schools = rows of frpm'
    >>> golden_case(q, Decision("load_fidelity", "result values differ", "SELECT 1", frame)).exclusion_reason
    'load_fidelity'
    """
    text = question.question
    if with_evidence and question.evidence:
        text = f"{text}\n\n{EVIDENCE_MARKER}\n{question.evidence}"
    tags = [BENCH_TAG, f"db:{question.db_id}", f"difficulty:{question.difficulty}"]
    notes = (
        f"BIRD mini-dev question_id={question.question_id}. The reference result was produced by "
        "running the dataset's SQL on the original SQLite database; expected_sql is its sqlglot "
        f"transpilation (used for ordering and table retrieval, not executed by a stored run). "
        f"SQLite SQL: {question.sql}"
    )
    common: dict[str, Any] = {
        "id": f"bird_{question.question_id}",
        "question": text,
        "tags": tags,
        "expected_sql": decision.expected_sql,
        "notes": notes,
    }
    if decision.reason is not None:
        common["notes"] += f" EXCLUDED from scoring: {decision.reason}" + (
            f" ({decision.detail})." if decision.detail else "."
        )
        return GoldenCase(**common, status="excluded", exclusion_reason=decision.reason)
    frame = decision.frame
    assert frame is not None
    return GoldenCase(
        **common,
        expected_rows=frame_to_rows(frame),
        expected_fingerprint=fingerprint_dataframe(frame),
        match="execution",
    )


@dataclass(slots=True)
class Tally:
    """Counts of scored and excluded cases, with the excluded ones listed."""

    scored: int = 0
    excluded: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def add(self, case_id: str, decision: Decision) -> None:
        if decision.reason is None:
            self.scored += 1
        else:
            self.excluded.setdefault(decision.reason, []).append((case_id, decision.detail))

    @property
    def total(self) -> int:
        return self.scored + sum(len(v) for v in self.excluded.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "scored": self.scored,
            "excluded": {
                reason: [{"case_id": cid, "detail": detail} for cid, detail in items]
                for reason, items in sorted(self.excluded.items())
            },
        }


def build_cases(
    questions: Sequence[BirdQuestion],
    connections: Mapping[str, Any],
    engine: Engine | None,
    *,
    max_rows: int,
    keep_unverified: bool = False,
    options: ComparisonOptions | None = None,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[GoldenCase], list[GoldenCase], dict[str, Tally]]:
    """Decide every question; return the plain cases, the evidence-variant cases and the tallies per database.

    Parameters
    ----------
    questions:
        The selected questions.
    connections:
        ``{db_id: read-only SQLite connection}``.
    engine:
        The SQL Server copy, or ``None`` to skip the load-fidelity check.
    max_rows, keep_unverified, options, timeout:
        See :func:`decide`.
    progress:
        Called now and then with a short message.
    """
    plain: list[GoldenCase] = []
    hinted: list[GoldenCase] = []
    tallies: dict[str, Tally] = {}
    for position, question in enumerate(questions, start=1):
        decision = decide(
            question, connections[question.db_id], engine, max_rows=max_rows,
            keep_unverified=keep_unverified, options=options, timeout=timeout,
        )
        case = golden_case(question, decision)
        plain.append(case)
        hinted.append(golden_case(question, decision, with_evidence=True))
        tallies.setdefault(question.db_id, Tally()).add(case.id, decision)
        if progress is not None and (position % 25 == 0 or position == len(questions)):
            progress(f"  {position}/{len(questions)} questions checked")
    return plain, hinted, tallies


def open_connections(source: BirdSource, db_ids: Sequence[str]) -> dict[str, Any]:
    """Open each database read-only (the caller closes them)."""
    return {db: open_sqlite_readonly(source.sqlite_path(db)) for db in db_ids}
