# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Schema-independent tests for the "among those" path of ``session.engine.TurnEngine``.

Pins how a refinement turn is composed over the previous turn's SQL
(``session.composer``), how the scan-cap truncation probe is built and run,
and what the turn tells the analyst when that probe reports truncation or
cannot run at all.

Why this module is not ``domain_data``
--------------------------------------
``tests/test_session_engine.py`` covers the same engine path, but against a
real deployment's table and column names, loaded from a git-ignored fixture
file; it therefore skips everywhere the real config is absent (CI, a fresh
clone). Nothing here needs a real value. Every test needs only "some
known table with at least two columns", so the scenario is derived at run
time from whatever schema is loaded (``schema_data.columns.TABLE_COLUMNS``,
with the qualifier resolved through the registry's public helpers) and the
module runs identically under ``project_config.example/`` and a real
configuration. No table or column name appears in this file, and no
``_test_fixtures`` file is read.

How the refinement path is selected
-----------------------------------
``session.refinement.classify_basis`` is a deterministic cue-phrase match.
The English cue ``"among those"`` is part of the code's own
``CTE_REFINEMENT_CUES`` list, so the refining question is built from that
constant rather than from any deployment vocabulary. Nothing is patched.

Scenario
--------
Turn 1 selects one column of the chosen table. Turn 2's mocked model output
is ``SELECT TOP 3 t_<col> FROM _prev ORDER BY t_<col>``, written against the
``{alias}_{column}`` names ``session.composer.predicate_columns`` exposes.
The table lives in an in-memory SQLite database (every column ``TEXT``);
each schema qualifier in use is attached as a database of the same name so
the guard-accepted ``[schema].[Table]`` form resolves unchanged.

Run::

    pytest tests/test_session_engine_refinement.py -v
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterator

import pandas as pd
import pytest
import sqlglot

import config as cfg
from config import override_settings
from llm.providers import MockBackend
from llm.router import LLMRouter
from schema_data.columns import TABLE_COLUMNS
from schema_data.registry import get_table_schema_qualifiers, table_reference_sql
from security.sql_guard import validate_sql
from session.composer import predicate_columns, truncation_probe_sql
from session.engine import TurnEngine
from session.models import Turn
from session.refinement import CTE_REFINEMENT_CUES
from session.store import SessionStore

SYSTEM_PROMPT = "You write read-only SQL for the configured schema."

#: Rows inserted into the scenario table. Larger than every small cap below.
ROW_COUNT = 6

#: Outer-query row limit the mocked model uses for the refinement turn.
OUTER_TOP = 3

#: Fresh question for turn 1; the mocked model ignores its wording.
FRESH_QUESTION = "list the values"

#: The refining question: contains the code's own first English CTE cue.
_ENGLISH_CUE = next(cue for cue in CTE_REFINEMENT_CUES if cue.isascii())
REFINING_QUESTION = f"{_ENGLISH_CUE}, show the first few"

_SQLITE_NATIONAL_PREFIX = re.compile(r"\bN'")

ExecuteFn = Callable[[str], pd.DataFrame]


# ---------------------------------------------------------------------------
# Scenario derived from the loaded schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    """A table, its qualifier and the column the scenario selects."""

    key: str
    qualifier: str
    columns: tuple[str, ...]
    column: str
    reference: str

    @property
    def q1_sql(self) -> str:
        return f"SELECT TOP 100 t.[{self.column}] FROM {self.reference} AS t"

    @property
    def q2_outer_sql(self) -> str:
        name = f"t_{self.column}"
        return f"SELECT TOP {OUTER_TOP} {name} FROM _prev ORDER BY {name}"


def _candidate_scenarios() -> Iterator[Scenario]:
    """Every usable (table, column) pair, in a deterministic order.

    Tables are walked in sorted key order; a table qualifies when it has at
    least two columns and its qualifier is at most one part (SQLite can
    attach a schema name, not a multi-part ``db.schema`` path).
    """
    qualifiers = get_table_schema_qualifiers()
    for key in sorted(TABLE_COLUMNS):
        columns = tuple(TABLE_COLUMNS[key])
        qualifier = qualifiers.get(key, "")
        if len(columns) < 2 or len(qualifier.split(".")) > 1:
            continue
        reference = table_reference_sql(key, qualifier)
        for column in columns:
            yield Scenario(key, qualifier, columns, column, reference)


def _first_valid_scenario() -> Scenario | None:
    """The first candidate whose turn-1 SQL the guard accepts, or ``None``."""
    for scenario in _candidate_scenarios():
        try:
            validate_sql(scenario.q1_sql)
        except ValueError:
            continue
        return scenario
    return None


# ---------------------------------------------------------------------------
# Harness (mirrors tests/test_session_engine.py)
# ---------------------------------------------------------------------------

def _to_sqlite(sql: str) -> str:
    """Test plumbing only: T-SQL to SQLite, minus the ``N''`` prefix."""
    out = sqlglot.transpile(sql, read="tsql", write="sqlite")[0]
    return _SQLITE_NATIONAL_PREFIX.sub("'", out)


@pytest.fixture()
def scenario() -> Scenario:
    found = _first_valid_scenario()
    if found is None:
        pytest.fail(
            "the loaded schema offers no table with >= 2 columns and a single-part "
            "qualifier whose scenario SQL passes validate_sql; see "
            "TestScenarioAssumptions for the individual checks"
        )
    return found


@pytest.fixture()
def sqlite_conn(scenario: Scenario) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    if scenario.qualifier:
        conn.execute(f'ATTACH DATABASE \':memory:\' AS "{scenario.qualifier}"')
    column_ddl = ", ".join(f'"{c}" TEXT' for c in scenario.columns)
    target = f'"{scenario.qualifier}"."{scenario.key.split(".")[-1]}"' if scenario.qualifier \
        else f'"{scenario.key.split(".")[-1]}"'
    conn.execute(f"CREATE TABLE {target} ({column_ddl})")
    placeholders = ", ".join("?" for _ in scenario.columns)
    conn.executemany(
        f"INSERT INTO {target} VALUES ({placeholders})",
        [tuple(f"v{i}_{c}" for c in scenario.columns) for i in range(ROW_COUNT)],
    )
    conn.commit()
    yield conn
    conn.close()


def _recording_execute(
    conn: sqlite3.Connection,
    *,
    transpile: bool = True,
    fail_on: str | None = None,
) -> tuple[ExecuteFn, list[str]]:
    """An ``execute_fn`` over *conn* plus the list every SQL string lands in.

    *fail_on*: raise ``RuntimeError`` for any SQL containing this substring.
    *transpile*: translate T-SQL to SQLite first; ``False`` when the engine
    already produced SQLite text (``sql_dialect = "sqlite"``).
    """
    seen: list[str] = []

    def execute(sql: str) -> pd.DataFrame:
        seen.append(sql)
        if fail_on is not None and fail_on in sql:
            raise RuntimeError("probe failed")
        return pd.read_sql_query(_to_sqlite(sql) if transpile else sql, conn)

    return execute, seen


def _engine(response_sql: str, execute_fn: ExecuteFn) -> TurnEngine:
    router = LLMRouter(default_chain=[MockBackend(response=response_sql)])
    return TurnEngine(router=router, execute_fn=execute_fn)


def _run_two_turns(
    scenario: Scenario,
    first_execute: ExecuteFn,
    second_execute: ExecuteFn,
    *,
    cap_first: int = 10_000,
    cap_second: int = 10_000,
    dialect: str | None = None,
) -> tuple[Turn, Turn]:
    """Run the fresh turn, then the refining turn, in one session."""
    store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
    record = store.create()
    extra = {} if dialect is None else {"sql_dialect": dialect}
    with override_settings(refinement_scan_cap=cap_first, default_top_n=1000, **extra):
        turn1 = _engine(scenario.q1_sql, first_execute).ask(record, FRESH_QUESTION, SYSTEM_PROMPT)
    with override_settings(refinement_scan_cap=cap_second, default_top_n=1000, **extra):
        turn2 = _engine(scenario.q2_outer_sql, second_execute).ask(
            record, REFINING_QUESTION, SYSTEM_PROMPT,
        )
    assert turn1.error is None, turn1.error
    assert turn1.sql, "the fresh turn must have stored the SQL it ran"
    return turn1, turn2


def _probes(seen: list[str]) -> list[str]:
    return [sql for sql in seen if "_check" in sql]


# ---------------------------------------------------------------------------
# Scenario preconditions
# ---------------------------------------------------------------------------

class TestScenarioAssumptions:
    """A schema that cannot support the scenario fails here, loudly."""

    def test_schema_offers_a_usable_table(self) -> None:
        assert _first_valid_scenario() is not None

    def test_turn_one_sql_passes_the_guard(self, scenario: Scenario) -> None:
        validate_sql(scenario.q1_sql)

    def test_turn_one_sql_uses_the_registry_qualified_reference(self, scenario: Scenario) -> None:
        assert scenario.reference in scenario.q1_sql

    def test_refining_question_selects_the_cte_path_by_the_codes_own_cue(self) -> None:
        assert _ENGLISH_CUE in REFINING_QUESTION
        assert _ENGLISH_CUE in CTE_REFINEMENT_CUES

    def test_outer_query_columns_exist_in_the_projection(self, scenario: Scenario) -> None:
        assert f"t_{scenario.column}" in predicate_columns(scenario.q1_sql)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

class TestRefinementTurnComposition:
    def test_refinement_turn_returns_rows_over_a_composed_cte(
        self, scenario: Scenario, sqlite_conn: sqlite3.Connection,
    ) -> None:
        first, _ = _recording_execute(sqlite_conn)
        second, seen = _recording_execute(sqlite_conn)

        turn1, turn2 = _run_two_turns(scenario, first, second)

        assert turn2.error is None, turn2.error
        assert turn2.basis.kind == "refines"
        assert turn2.basis.composition == "cte"
        assert turn2.basis.refines_turn_id == turn1.turn_id
        assert turn2.result.row_count == OUTER_TOP
        assert turn2.sql.startswith("WITH _prev AS (")

        non_probe = [sql for sql in seen if "_check" not in sql]
        assert len(non_probe) == 1
        executed = non_probe[0]
        assert executed.startswith("WITH _prev AS (")
        assert scenario.reference in executed


# ---------------------------------------------------------------------------
# Truncation warning
# ---------------------------------------------------------------------------

class TestScanCapWarning:
    def test_warns_when_the_base_scan_hit_the_cap(
        self, scenario: Scenario, sqlite_conn: sqlite3.Connection,
    ) -> None:
        first, _ = _recording_execute(sqlite_conn)
        second, _ = _recording_execute(sqlite_conn)

        _, turn2 = _run_two_turns(scenario, first, second, cap_second=2)

        assert turn2.error is None, turn2.error
        assert any("refinement_scan_cap" in w for w in turn2.warnings)
        assert any("متوقف شد" in w for w in turn2.warnings)

    def test_no_warning_when_the_cap_is_not_hit(
        self, scenario: Scenario, sqlite_conn: sqlite3.Connection,
    ) -> None:
        first, _ = _recording_execute(sqlite_conn)
        second, _ = _recording_execute(sqlite_conn)

        _, turn2 = _run_two_turns(scenario, first, second, cap_second=10_000)

        assert turn2.error is None, turn2.error
        assert turn2.warnings == []

    def test_no_warning_when_the_scan_exactly_fills_the_cap(
        self, scenario: Scenario, sqlite_conn: sqlite3.Connection,
    ) -> None:
        """Exactly ``cap`` matching rows is not truncation; ``cap + 1`` is."""
        first, _ = _recording_execute(sqlite_conn)
        second, _ = _recording_execute(sqlite_conn)

        _, turn2 = _run_two_turns(scenario, first, second, cap_second=ROW_COUNT)

        assert turn2.error is None, turn2.error
        assert turn2.warnings == []


# ---------------------------------------------------------------------------
# Failed probe
# ---------------------------------------------------------------------------

class TestFailedTruncationProbe:
    def test_failed_probe_still_answers_logs_and_warns(
        self,
        scenario: Scenario,
        sqlite_conn: sqlite3.Connection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        first, _ = _recording_execute(sqlite_conn)
        second, _ = _recording_execute(sqlite_conn, fail_on="_check")

        with caplog.at_level("WARNING"):
            _, turn2 = _run_two_turns(scenario, first, second)

        assert turn2.error is None, turn2.error
        assert turn2.result.row_count > 0
        assert "refinement truncation check failed" in caplog.text
        assert any(
            w.startswith("بررسی کامل‌بودن اسکن پایهٔ این بازپالایش انجام نشد")
            for w in turn2.warnings
        )


# ---------------------------------------------------------------------------
# The probe itself
# ---------------------------------------------------------------------------

class TestTruncationProbeStatement:
    def test_probe_is_built_from_the_stored_previous_sql_and_the_cap(
        self, scenario: Scenario, sqlite_conn: sqlite3.Connection,
    ) -> None:
        cap = 5
        first, _ = _recording_execute(sqlite_conn)
        second, seen = _recording_execute(sqlite_conn)

        turn1, _ = _run_two_turns(scenario, first, second, cap_second=cap)

        expected = truncation_probe_sql(turn1.sql, cap, dialect=cfg.settings.sql_dialect)
        assert _probes(seen) == [expected]


# ---------------------------------------------------------------------------
# Dialect pass-through
# ---------------------------------------------------------------------------

class TestDialectPassThrough:
    def test_sqlite_dialect_reaches_the_probe_and_the_refinement(
        self, scenario: Scenario, sqlite_conn: sqlite3.Connection,
    ) -> None:
        """With ``sql_dialect = "sqlite"`` for both turns, the stored turn-1
        SQL is SQLite text, the probe is rendered with ``LIMIT`` (not
        ``TOP``), and the refinement still answers."""
        cap = 5
        first, _ = _recording_execute(sqlite_conn, transpile=False)
        second, seen = _recording_execute(sqlite_conn, transpile=False)

        turn1, turn2 = _run_two_turns(
            scenario, first, second, cap_first=cap, cap_second=cap, dialect="sqlite",
        )

        assert "TOP" not in turn1.sql.upper().split()
        assert turn2.error is None, turn2.error
        assert turn2.basis.composition == "cte"
        assert turn2.result.row_count == OUTER_TOP

        probes = _probes(seen)
        assert probes == [truncation_probe_sql(turn1.sql, cap, dialect="sqlite")]
        assert "LIMIT" in probes[0]
        assert "TOP" not in probes[0].upper().split()
