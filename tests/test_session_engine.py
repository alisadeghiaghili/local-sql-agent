# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""End-to-end tests for ``session.engine.TurnEngine`` — the §2 correctness proof.

Runs the exit-criteria three-turn scenario (Q1 fresh, Q2 refines via CTE,
malicious-previous-turn rejection, cap-truncation warning, PATCH re-run,
no-row-data-in-prompt, single audit record) against a REAL, if tiny,
SQLite database — not a hand-picked mock DataFrame — so that "Q2's answer
is identical to the same query written out in full and explicitly" is
proven by actually executing both and comparing rows, not by asserting
against a hardcoded expectation that could hide a composition bug.

T-SQL -> SQLite translation is via ``sqlglot.transpile`` (handles ``TOP``
-> ``LIMIT`` at every nesting level) plus one small regex to drop the
``N''`` national-string prefix SQLite doesn't understand -- this is test
plumbing only, never something production code does.

Every test in this module is marked ``domain_data`` (see module-level
``pytestmark`` below) and skips whenever ``PROJECT_CONFIG_DIR`` points at
``project_config.example/`` (CI, a fresh clone). The ``sqlite_conn``
fixture's own ``CREATE TABLE`` statements hardcode the real schema's
table/column names (``Customer``, ``Ring``, ``CustomerContract`` with its
real FK/wage columns) specifically so the generated SQL those tests run
resolves against the real ``schema_data.columns.TABLE_COLUMNS`` allowlist
-- and the scenario's actual business assertions (buyer ranking by real
Rial trade volume, a real trading-hall name filter) only mean something
against that real shape. This is squarely "the real schema snapshot"
category, not a test that merely reached for a real name as a stand-in --
rewriting it against a generic 3-table example would mean inventing a
different, no-longer-representative scenario, not fixing a coincidence.
"""

from __future__ import annotations

import re
import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest
import sqlglot

from config import override_settings
from llm.providers import MockBackend
from llm.router import LLMRouter
from session.engine import TurnEngine, build_session_context_text
from session.models import ResultColumn, Turn, TurnResult
from session.store import SessionStore

pytestmark = pytest.mark.domain_data

SYSTEM_PROMPT = "You are a T-SQL expert for the Auction domain."

Q1_QUESTION = "معاملات مشتری‌های تالار سیمان را نشان بده"
Q2_QUESTION = "از بین آن‌ها ۱۰ مشتری برتر به لحاظ حجم معامله"

Q1_SQL = (
    "SELECT TOP 2 c.Name AS CustomerName, SUM(ct.TotalPrice) AS TotalValue "
    "FROM CustomerContract ct "
    "JOIN Customer c ON ct.BuyerCustomer_ID = c.ID "
    "JOIN Ring r ON ct.Ring_ID = r.ID "
    "WHERE r.Name = N'تالار سیمان' "
    "GROUP BY c.Name ORDER BY TotalValue DESC"
)

# The mocked model's outer query for Q2 — references _prev's projected
# columns (c_Name, ct_Quantity), per session.composer.predicate_columns.
Q2_OUTER_SQL = (
    "SELECT TOP 2 c_Name, SUM(ct_Quantity) AS TotalVolume "
    "FROM _prev GROUP BY c_Name ORDER BY TotalVolume DESC"
)

EXPLICIT_EQUIVALENT_SQL = (
    "SELECT TOP 2 c.Name AS CustomerName, SUM(ct.Quantity) AS TotalVolume "
    "FROM CustomerContract ct "
    "JOIN Customer c ON ct.BuyerCustomer_ID = c.ID "
    "JOIN Ring r ON ct.Ring_ID = r.ID "
    "WHERE r.Name = N'تالار سیمان' "
    "GROUP BY c.Name ORDER BY TotalVolume DESC"
)


def _to_sqlite(sql: str) -> str:
    out = sqlglot.transpile(sql, read="tsql", write="sqlite")[0]
    return re.sub(r"\bN'", "'", out)


@pytest.fixture()
def sqlite_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE Customer (ID INTEGER PRIMARY KEY, Name TEXT, NationalID TEXT, IsActive INTEGER)")
    conn.execute("CREATE TABLE Ring (ID INTEGER PRIMARY KEY, Name TEXT, Code TEXT)")
    conn.execute(
        "CREATE TABLE CustomerContract ("
        "ID INTEGER PRIMARY KEY, Date_ID INTEGER, Ring_ID INTEGER, Symbol_ID INTEGER, "
        "BuyerCustomer_ID INTEGER, BuyerBroker_ID INTEGER, SellerBroker_ID INTEGER, "
        "TotalPrice REAL, Quantity REAL, BuyBrokerWage REAL, SellBrokerWage REAL, "
        "BuyIMEWage REAL, SellIMEWage REAL, BuySEOWage REAL, SellSEOWage REAL)"
    )
    conn.executemany(
        "INSERT INTO Customer (ID, Name, IsActive) VALUES (?, ?, 1)",
        [(1, "A"), (2, "B"), (3, "C"), (4, "D")],
    )
    conn.executemany("INSERT INTO Ring (ID, Name) VALUES (?, ?)", [(1, "تالار سیمان"), (2, "تالار فلزات")])
    conn.executemany(
        "INSERT INTO CustomerContract (ID, BuyerCustomer_ID, Ring_ID, TotalPrice, Quantity) VALUES (?, ?, ?, ?, ?)",
        [
            (1, 1, 1, 1000.0, 5.0),   # A: highest value, low volume
            (2, 2, 1, 900.0, 50.0),   # B: 2nd by value, 2nd by volume
            (3, 3, 1, 800.0, 3.0),    # C: displayed-out by TOP 2 either way
            (4, 4, 1, 10.0, 100.0),   # D: lowest value (NOT in Q1's TOP 2 display), highest volume
            (5, 4, 2, 5000.0, 1.0),   # different ring -- must be excluded by the predicate
        ],
    )
    conn.commit()
    yield conn
    conn.close()


def _execute_fn(conn):
    def execute(sql: str) -> pd.DataFrame:
        return pd.read_sql_query(_to_sqlite(sql), conn)
    return execute


def _engine(response_sql: str, execute_fn) -> TurnEngine:
    router = LLMRouter(default_chain=[MockBackend(response=response_sql)])
    return TurnEngine(router=router, execute_fn=execute_fn)


# ---------------------------------------------------------------------------
# Exit criterion 1 — the §2 correctness proof
# ---------------------------------------------------------------------------

class TestSection2CorrectnessProof:
    def test_three_turn_scenario_q2_matches_explicit_equivalent(self, sqlite_conn):
        execute_fn = _execute_fn(sqlite_conn)
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create()

        with override_settings(refinement_scan_cap=10_000, default_top_n=1000):
            turn1 = _engine(Q1_SQL, execute_fn).ask(record, Q1_QUESTION, SYSTEM_PROMPT)
            assert turn1.error is None
            assert turn1.basis.kind == "fresh"
            assert turn1.ambiguity.is_ambiguous is False
            assert turn1.result.row_count == 2
            # D is the highest-volume customer but is NOT among Q1's displayed rows.
            assert "D" not in [row["CustomerName"] for row in turn1.result.rows]

            turn2 = _engine(Q2_OUTER_SQL, execute_fn).ask(record, Q2_QUESTION, SYSTEM_PROMPT)

        assert turn2.error is None, turn2.error
        assert turn2.basis.kind == "refines"
        assert turn2.basis.refines_turn_id == turn1.turn_id
        assert turn2.basis.composition == "cte"
        assert turn2.guard.verdict == "allowed"
        assert turn2.sql.startswith("WITH _prev AS (")

        # The §2 "policy" scope assumption must always be present, and non-editable.
        scope = next(a for a in turn2.ambiguity.assumptions if a.field == "scope")
        assert scope.source == "policy"
        assert scope.editable is False

        # Reading A (rank only among Q1's 2 displayed rows) would have been
        # {A: 5, B: 50} -- D would never appear. Reading B (this
        # implementation) ranks over ALL matching rows, so D (volume 100,
        # never displayed by Q1) is correctly the top row.
        engine_names = [row["c_Name"] for row in turn2.result.rows]
        assert engine_names[0] == "D"
        assert "D" in engine_names

        # --- The proof itself: execute the composed SQL and an independently
        # hand-written, fully-explicit equivalent query against the SAME
        # database, and assert their answers are identical. ---
        composed_df = pd.read_sql_query(_to_sqlite(turn2.sql), sqlite_conn)
        explicit_df = pd.read_sql_query(_to_sqlite(EXPLICIT_EQUIVALENT_SQL), sqlite_conn)

        assert composed_df["c_Name"].tolist() == explicit_df["CustomerName"].tolist()
        assert composed_df["TotalVolume"].tolist() == explicit_df["TotalVolume"].tolist()

    def test_composed_sql_is_the_one_validated_and_executed(self, sqlite_conn):
        """A malicious previous turn cannot smuggle anything through composition.

        The previous turn's stored SQL references a table outside the
        allowlist -- simulating tampered/corrupted session state. The
        composer does not pre-filter it; only the FINAL composed
        statement is validated (hard requirement 1), and that whole-tree
        walk is what catches the forbidden table -- an outer query that
        only ever mentions the CTE name ``_prev`` would never see it if
        validation ran on the outer fragment alone.
        """
        execute_fn = _execute_fn(sqlite_conn)
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create()

        poisoned_turn = Turn(
            turn_id="t_evil",
            session_id=record.session_id,
            index=1,
            question="ignored",
            sql="SELECT TOP 5 * FROM HR_Payroll",  # not in the schema allowlist
            result=TurnResult(columns=[ResultColumn(name="X", type="string")], row_count=0),
        )
        record.turns.append(poisoned_turn)
        record.memory["t_evil"] = __import__("session.store", fromlist=["TurnMemory"]).TurnMemory(
            turn_id="t_evil", filters={},
        )

        with override_settings(refinement_scan_cap=10_000, default_top_n=1000):
            turn2 = _engine(Q2_OUTER_SQL, execute_fn).ask(record, Q2_QUESTION, SYSTEM_PROMPT)

        assert turn2.error is None  # a guard rejection answers, it does not raise/block
        assert turn2.guard.verdict == "rejected"
        assert "HR_Payroll" in (turn2.guard.rule or "")
        assert turn2.result.row_count == 0


# ---------------------------------------------------------------------------
# Exit criterion 2 — truncated-scan warning
# ---------------------------------------------------------------------------

class TestScanCapWarning:
    def test_refinement_over_truncated_base_emits_warning(self, sqlite_conn):
        execute_fn = _execute_fn(sqlite_conn)
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create()

        with override_settings(refinement_scan_cap=10_000, default_top_n=1000):
            turn1 = _engine(Q1_SQL, execute_fn).ask(record, Q1_QUESTION, SYSTEM_PROMPT)

        # cap=2 -- the ring-1 predicate matches 4 rows, so this MUST be flagged.
        with override_settings(refinement_scan_cap=2, default_top_n=1000):
            turn2 = _engine(Q2_OUTER_SQL, execute_fn).ask(record, Q2_QUESTION, SYSTEM_PROMPT)

        assert turn2.error is None
        assert any("refinement_scan_cap" in w for w in turn2.warnings)

    def test_no_warning_when_cap_not_hit(self, sqlite_conn):
        execute_fn = _execute_fn(sqlite_conn)
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create()

        with override_settings(refinement_scan_cap=10_000, default_top_n=1000):
            _engine(Q1_SQL, execute_fn).ask(record, Q1_QUESTION, SYSTEM_PROMPT)
            turn2 = _engine(Q2_OUTER_SQL, execute_fn).ask(record, Q2_QUESTION, SYSTEM_PROMPT)

        assert turn2.warnings == []


# ---------------------------------------------------------------------------
# Exit criterion 4 — every assumption carries a source; policy is locked
# ---------------------------------------------------------------------------

class TestAssumptionSourcing:
    def test_fresh_ranking_question_with_no_context_gets_default_assumptions(self, sqlite_conn):
        """The same question asked with NO prior session context is answered
        under declared default assumptions, never refused (§5)."""
        execute_fn = _execute_fn(sqlite_conn)
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create()

        fresh_sql = (
            "SELECT TOP 2 c.Name AS CustomerName, SUM(ct.TotalPrice) AS TotalValue "
            "FROM CustomerContract ct JOIN Customer c ON ct.BuyerCustomer_ID = c.ID "
            "GROUP BY c.Name ORDER BY TotalValue DESC"
        )
        with override_settings(default_top_n=1000):
            turn = _engine(fresh_sql, execute_fn).ask(record, "۱۰ مشتری برتر را نشان بده", SYSTEM_PROMPT)

        assert turn.error is None
        assert turn.ambiguity.is_ambiguous is True
        sources = {a.field: a.source for a in turn.ambiguity.assumptions}
        assert sources == {"ring": "default", "measure": "default", "period": "default"}
        assert all(a.editable for a in turn.ambiguity.assumptions)
        assert len(turn.ambiguity.clarifications) >= 1


# ---------------------------------------------------------------------------
# Exit criterion 5 — PATCH re-run produces a new turn, never mutates the old
# ---------------------------------------------------------------------------

class TestPatchDoesNotMutate:
    def test_assumption_override_appends_new_turn_and_leaves_original_untouched(self, sqlite_conn):
        execute_fn = _execute_fn(sqlite_conn)
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create()

        fresh_sql = (
            "SELECT TOP 2 c.Name AS CustomerName, SUM(ct.TotalPrice) AS TotalValue "
            "FROM CustomerContract ct JOIN Customer c ON ct.BuyerCustomer_ID = c.ID "
            "GROUP BY c.Name ORDER BY TotalValue DESC"
        )
        with override_settings(default_top_n=1000):
            original = _engine(fresh_sql, execute_fn).ask(record, "۱۰ مشتری برتر را نشان بده", SYSTEM_PROMPT)
            original_snapshot = original.model_copy(deep=True)

            patched = _engine(fresh_sql, execute_fn).ask(
                record, original.question, SYSTEM_PROMPT,
                assumption_overrides={"ring": "تالار فلزات"},
            )

        assert original == original_snapshot  # untouched
        assert patched.turn_id != original.turn_id
        assert len(record.turns) == 2
        ring_assumption = next(a for a in patched.ambiguity.assumptions if a.field == "ring")
        assert ring_assumption.value == "تالار فلزات"
        assert ring_assumption.source == "question"  # user now explicitly said so


# ---------------------------------------------------------------------------
# Exit criterion 6 — no row data in the prompt's session-context block
# ---------------------------------------------------------------------------

class TestNoRowDataInPrompt:
    def test_session_context_carries_columns_and_counts_never_row_values(self):
        turn = Turn(
            turn_id="t_01", session_id="s_1", index=1,
            question="q", sql="SELECT TOP 1 SecretName FROM Customer",
            result=TurnResult(
                columns=[ResultColumn(name="SecretName", type="string")],
                rows=[{"SecretName": "قرارداد محرمانه شرکت الف"}],
                row_count=1,
            ),
        )
        text = build_session_context_text([turn], max_turns=3)
        assert "SecretName" in text          # column name: allowed
        assert "1" in text                    # row_count: allowed
        assert "قرارداد محرمانه شرکت الف" not in text  # row VALUE: forbidden


# ---------------------------------------------------------------------------
# Exit criterion 7 — exactly one audit record per turn, every path
# ---------------------------------------------------------------------------

class TestSingleAuditRecord:
    def test_success_path_writes_exactly_one_record(self, sqlite_conn):
        execute_fn = _execute_fn(sqlite_conn)
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create()

        with patch("session.engine.save_audit_record") as mock_save:
            with override_settings(default_top_n=1000):
                _engine(Q1_SQL, execute_fn).ask(record, Q1_QUESTION, SYSTEM_PROMPT)
        assert mock_save.call_count == 1
        saved = mock_save.call_args[0][0]
        assert saved.columns == ["CustomerName", "TotalValue"]
        assert all(row not in str(saved.as_dict()) for row in ("قرارداد",))  # sanity: no stray row text

    def test_guard_rejection_path_writes_exactly_one_record(self, sqlite_conn):
        execute_fn = _execute_fn(sqlite_conn)
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create()
        poisoned = Turn(
            turn_id="t_evil", session_id=record.session_id, index=1,
            question="ignored", sql="SELECT TOP 5 * FROM HR_Payroll",
        )
        record.turns.append(poisoned)
        from session.store import TurnMemory
        record.memory["t_evil"] = TurnMemory(turn_id="t_evil", filters={})

        with patch("session.engine.save_audit_record") as mock_save:
            with override_settings(refinement_scan_cap=10_000, default_top_n=1000):
                _engine(Q2_OUTER_SQL, execute_fn).ask(record, Q2_QUESTION, SYSTEM_PROMPT)
        assert mock_save.call_count == 1

    def test_transport_failure_path_writes_exactly_one_record(self):
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create()

        class _BrokenBackend:
            name = "broken"

            def generate_with_meta_segments(self, segments):
                raise RuntimeError("connection refused")

        router = LLMRouter(default_chain=[_BrokenBackend()])
        engine = TurnEngine(router=router, execute_fn=lambda sql: pd.DataFrame())

        with patch("session.engine.save_audit_record") as mock_save:
            turn = engine.ask(record, "چیزی", SYSTEM_PROMPT)

        assert turn.error is not None
        assert turn.error.code == "MODEL_UNAVAILABLE"
        assert mock_save.call_count == 1


# ---------------------------------------------------------------------------
# Exit criterion 8 — static prefix byte-identical across turns of a session
# ---------------------------------------------------------------------------

class TestStaticPrefixInvariance:
    def test_static_prefix_unaffected_by_session_context(self, sqlite_conn):
        """Session context must sit only in the variable suffix. This test
        fails if session context ever leaks into the prefix: it captures
        every prompt actually sent to the model across two turns of one
        session and asserts the shared prefix portion is byte-identical,
        even though the second turn carries a non-empty session history
        the first did not.
        """
        from prompt_engine.static_prefix import build_static_prefix

        captured_prompts: list[str] = []

        class _RecordingBackend:
            name = "recording"

            def __init__(self, response: str):
                self._response = response

            def generate_with_meta_segments(self, segments):
                captured_prompts.append(segments.flatten())
                return self._response, {"raw": {}, "endpoint_status": 200, "attempts": 1}

        execute_fn = _execute_fn(sqlite_conn)
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create()

        with override_settings(refinement_scan_cap=10_000, default_top_n=1000):
            engine1 = TurnEngine(
                router=LLMRouter(default_chain=[_RecordingBackend(Q1_SQL)]), execute_fn=execute_fn,
            )
            engine1.ask(record, Q1_QUESTION, SYSTEM_PROMPT)

            engine2 = TurnEngine(
                router=LLMRouter(default_chain=[_RecordingBackend(Q1_SQL)]), execute_fn=execute_fn,
            )
            # A second FRESH turn (not a refinement) still carries session
            # context in its suffix (§8) -- the prefix must still match turn 1's.
            engine2.ask(record, "معاملات مشتری‌های تالار فلزات را نشان بده", SYSTEM_PROMPT)

        assert len(captured_prompts) == 2
        prefix = build_static_prefix(SYSTEM_PROMPT)
        assert captured_prompts[0].startswith(prefix)
        assert captured_prompts[1].startswith(prefix)
        # Turn 2's full prompt differs (it carries turn 1 in its session
        # context) but the two prompts must share the identical prefix.
        assert captured_prompts[0][: len(prefix)] == captured_prompts[1][: len(prefix)]
        assert captured_prompts[0] != captured_prompts[1]


class TestAuditRecordCarriesTheConversation:
    """A v2 turn's audit record must name its session and turn.

    Without these the audit trail can say nothing about the
    conversational product. A follow-up and a fresh question look
    identical in the log; "this answer was wrong" cannot be traced back
    to the turn that produced it, nor to the turns it refined.

    `docs/admin-panel-architecture.md` names their absence as the
    prerequisite blocking the panel's entire first tier, and the cost is
    not recoverable later: a week of production logs written without them
    is a week that is permanently blind to it. That is why this landed
    during a deployment rather than after.

    Both are identifiers this system generated, not user content -- the
    same category as `request_id`.
    """

    def test_session_and_turn_reach_the_audit_record(self, sqlite_conn, monkeypatch):
        saved: list = []
        monkeypatch.setattr(
            "session.engine.save_audit_record", lambda record: saved.append(record),
        )

        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=10)
        record = store.create(owner_id="analyst-1")
        engine = _engine(Q1_SQL, _execute_fn(sqlite_conn))

        with override_settings(refinement_scan_cap=10_000, default_top_n=1000):
            turn = engine.ask(record, Q1_QUESTION, SYSTEM_PROMPT)

        assert saved, "every turn must produce exactly one audit record"
        audited = saved[-1]
        assert audited.session_id == record.session_id, (
            "the audit record must name the conversation this turn belongs to"
        )
        assert audited.turn_id == turn.turn_id, (
            "and the turn, so a flagged answer can be traced back to it"
        )

    def test_audit_record_also_carries_config_version_and_assumptions(self, sqlite_conn, monkeypatch):
        """Admin panel phase 4 (spec §2.2, §7): the feedback row itself
        must not duplicate the question, the SQL, or the turn's declared
        assumptions -- they are resolved from the audit record instead.
        Neither field existed on ``AuditRecord`` before this phase; this is
        the equivalent, for phase 4, of the class-level test above proving
        session_id/turn_id reached the record for phase 3."""
        from appdb.config_versions import get_active_version_id

        saved: list = []
        monkeypatch.setattr(
            "session.engine.save_audit_record", lambda record: saved.append(record),
        )

        # A fresh ranking question with no prior context -- exactly the
        # scenario TestAssumptionSourcing above proves produces real,
        # non-empty declared assumptions -- so this test also proves the
        # non-trivial case, not just "None survives unchanged".
        execute_fn = _execute_fn(sqlite_conn)
        store = SessionStore(ttl_seconds=1800, max_size=10, max_turns=50)
        record = store.create(owner_id="analyst-1")
        fresh_sql = (
            "SELECT TOP 2 c.Name AS CustomerName, SUM(ct.TotalPrice) AS TotalValue "
            "FROM CustomerContract ct JOIN Customer c ON ct.BuyerCustomer_ID = c.ID "
            "GROUP BY c.Name ORDER BY TotalValue DESC"
        )

        with override_settings(default_top_n=1000):
            turn = _engine(fresh_sql, execute_fn).ask(record, "۱۰ مشتری برتر را نشان بده", SYSTEM_PROMPT)

        assert turn.error is None
        assert turn.ambiguity.assumptions, "sanity: this scenario must produce real assumptions"

        audited = saved[-1]
        assert audited.config_version_id == get_active_version_id(), (
            "the audit record must name which configuration version produced "
            "this answer -- 'which config produced this wrong answer?' is "
            "exactly the question phase 4's triage needs answered"
        )
        assert audited.assumptions == [a.model_dump() for a in turn.ambiguity.assumptions], (
            "the triage queue reads assumptions off the audit record, never "
            "a second, duplicated store"
        )
