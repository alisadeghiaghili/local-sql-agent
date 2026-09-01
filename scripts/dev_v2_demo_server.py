# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Run the real v2 API against a synthetic in-memory SQLite database.

Usage (from repo root)::

    .venv/Scripts/python.exe scripts/dev_v2_demo_server.py

This is manual-verification tooling, not production code. There is no
live LLM endpoint or SQL Server available in this environment (see the Phase 3
report), so this script demonstrates the actual FastAPI app, the actual
``session.engine.TurnEngine`` pipeline, the actual SQL guard, and the
actual §2 CTE composition — end to end — against two stand-ins:

* ``database.executor.execute_query`` is monkeypatched to run the
  (guard-approved, already-composed) SQL against a tiny in-memory SQLite
  database instead of a real SQL Server, via the same
  ``sqlglot.transpile(sql, read="tsql", write="sqlite")`` translation
  ``tests/test_session_engine.py`` uses for its own §2 correctness proof.
* The LLM backend is a small keyword-dispatching stub (``_DemoBackend``
  below) that returns one of three canned, schema-valid SQL strings
  depending on which turn it's being asked to answer — standing in for
  a real model, which is not available here either.

Everything else — session store, ambiguity/assumption logic, the guard,
the CTE composer, the audit trail, CORS, and every HTTP route — is the
real, unmodified application code. What this script cannot demonstrate is
generation *quality* from a real model or execution against a real
production database; both are explicitly out of reach in this
environment and are called out as unverified in the Phase 3 report.

Serves on http://localhost:8000. Pair with a static file server for
``web/`` (e.g. ``python -m http.server 8080`` from that directory) and
open ``http://localhost:8080/?live=1&base=http://localhost:8000``.
"""

from __future__ import annotations

import os
import re
import sqlite3

# Must be set before `import config` (module-level Settings() singleton
# reads the environment at import time) -- see config.py's own docstring.
os.environ.setdefault("OPENAI_MODEL", "demo-stub")
os.environ.setdefault(
    "DB_CONNECTION_URL",
    "mssql+pyodbc://demo@localhost:1433/Demo?driver=ODBC+Driver+17+for+SQL+Server",
)
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:8080,http://localhost:8081")
os.environ.setdefault("LLM_PROVIDER", "mock")

import pandas as pd
import sqlglot

import api.server as server_module
import session.engine as engine_module

# ---------------------------------------------------------------------------
# Synthetic database — same shape as tests/test_session_engine.py's fixture,
# extended with a Date dimension so turn 3 ("همین را برای سال قبل") has a
# real year filter to apply.
# ---------------------------------------------------------------------------

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.execute("CREATE TABLE Customer (ID INTEGER PRIMARY KEY, Name TEXT, NationalID TEXT, IsActive INTEGER)")
_conn.execute("CREATE TABLE Ring (ID INTEGER PRIMARY KEY, Name TEXT, Code TEXT)")
_conn.execute("CREATE TABLE Date (ID INTEGER PRIMARY KEY, PersianYear INTEGER)")
_conn.execute(
    "CREATE TABLE CustomerContract (ID INTEGER PRIMARY KEY, Date_ID INTEGER, Ring_ID INTEGER, "
    "Symbol_ID INTEGER, BuyerCustomer_ID INTEGER, BuyerBroker_ID INTEGER, SellerBroker_ID INTEGER, "
    "TotalPrice REAL, Quantity REAL, BuyBrokerWage REAL, SellBrokerWage REAL, BuyIMEWage REAL, "
    "SellIMEWage REAL, BuySEOWage REAL, SellSEOWage REAL)"
)
_conn.executemany(
    "INSERT INTO Customer (ID, Name, IsActive) VALUES (?, ?, 1)",
    [(1, "شرکت سیمان الوند"), (2, "بازرگانی پارس بتن"), (3, "شرکت آریا سیمان"), (4, "هلدینگ مهر بنا"), (5, "تعاونی سیمان شرق")],
)
_conn.executemany("INSERT INTO Ring (ID, Name) VALUES (?, ?)", [(1, "تالار سیمان"), (2, "تالار فلزات")])
_conn.executemany("INSERT INTO Date (ID, PersianYear) VALUES (?, ?)", [(1, 1404), (2, 1403)])
_conn.executemany(
    "INSERT INTO CustomerContract (ID, Date_ID, BuyerCustomer_ID, Ring_ID, TotalPrice, Quantity) VALUES (?, ?, ?, ?, ?, ?)",
    [
        (1, 1, 1, 1, 1000.0, 5.0),
        (2, 1, 2, 1, 900.0, 50.0),
        (3, 1, 3, 1, 800.0, 3.0),
        (4, 1, 4, 1, 10.0, 100.0),   # highest volume this year, but NOT in a TOP-2-by-value display
        (5, 1, 4, 2, 5000.0, 1.0),   # different ring -- must be excluded from the ring-1 predicate
        (6, 2, 1, 1, 500.0, 20.0),   # last year
        (7, 2, 2, 1, 400.0, 60.0),   # last year
        (8, 2, 5, 1, 300.0, 90.0),   # last year, highest volume
    ],
)
_conn.commit()


def _to_sqlite(sql: str) -> str:
    out = sqlglot.transpile(sql, read="tsql", write="sqlite")[0]
    return re.sub(r"\bN'", "'", out)


def _demo_execute_query(sql: str) -> pd.DataFrame:
    return pd.read_sql_query(_to_sqlite(sql), _conn)


# ---------------------------------------------------------------------------
# Stub LLM backend — dispatches on cues already present in the prompt text
# session.engine builds, standing in for a real model (none available here).
# ---------------------------------------------------------------------------

_Q1_SQL = (
    "SELECT TOP 2 c.Name AS CustomerName, SUM(ct.TotalPrice) AS TotalValue "
    "FROM CustomerContract ct JOIN Customer c ON ct.BuyerCustomer_ID = c.ID "
    "JOIN Ring r ON ct.Ring_ID = r.ID WHERE r.Name = N'تالار سیمان' "
    "GROUP BY c.Name ORDER BY TotalValue DESC"
)
_Q2_OUTER_SQL = "SELECT TOP 10 c_Name, SUM(ct_Quantity) AS TotalVolume FROM _prev GROUP BY c_Name ORDER BY TotalVolume DESC"
_Q3_SQL = (
    "SELECT TOP 10 c.Name AS CustomerName, SUM(ct.Quantity) AS TotalVolume "
    "FROM CustomerContract ct JOIN Customer c ON ct.BuyerCustomer_ID = c.ID "
    "JOIN Ring r ON ct.Ring_ID = r.ID JOIN [Date] d ON ct.Date_ID = d.ID "
    "WHERE r.Name = N'تالار سیمان' AND d.PersianYear = 1403 "
    "GROUP BY c.Name ORDER BY TotalVolume DESC"
)


class _DemoBackend:
    name = "demo:stub"

    def generate_with_meta_segments(self, segments):
        text = segments.question
        # "already prepared for you" is the CTE-refinement instruction
        # session.engine._handle_cte_refinement builds -- a much narrower
        # signal than a bare "_prev" substring, which can otherwise also
        # appear incidentally inside an earlier turn's SQL once it's quoted
        # back in a later turn's session-context block.
        if "already prepared for you" in text:
            sql = _Q2_OUTER_SQL
        elif "سال قبل" in text or "1403" in text:
            sql = _Q3_SQL
        else:
            sql = _Q1_SQL
        meta = {"raw": {"usage": {"prompt_tokens": 128, "completion_tokens": 40}}, "endpoint_status": 200, "attempts": 1}
        return sql, meta


def _patch_engine_defaults() -> None:
    """Force every new TurnEngine (the module's lazy singleton) to use the
    demo backend/executor instead of a real LLM endpoint/SQL Server connection."""
    from llm.router import LLMRouter

    demo_router = LLMRouter(default_chain=[_DemoBackend()])
    engine_module._reset_router_for_testing(demo_router)

    import database.executor as executor_module

    executor_module.execute_query = _demo_execute_query

    # GET /health would otherwise spend ~5-10s per call on a real (absent)
    # LLM endpoint/SQL Server probe before reporting "down" -- report the demo's
    # actual (synthetic) status instead, instantly.
    import api.health as health_module
    from api.models import HealthResponse

    health_module.check_health = lambda: HealthResponse(
        status="ok", openai=True, database=True, model="demo-stub",
    )


if __name__ == "__main__":
    import uvicorn

    _patch_engine_defaults()
    print("Demo v2 server: http://localhost:8000  (docs: /docs)")
    print("LLM: keyword-dispatching stub. Database: in-memory SQLite. See this file's docstring.")
    uvicorn.run(server_module.app, host="0.0.0.0", port=8000, log_level="info")
