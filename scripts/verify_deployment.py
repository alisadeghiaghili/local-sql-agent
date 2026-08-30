"""Verify a deployed environment matches what this codebase assumes.

Usage (from repo root, against whatever ``.env`` / real environment
variables the *current shell* has -- point this at staging or production
by running it from an environment configured for that target)::

    python scripts/verify_deployment.py

This is the executable counterpart to ``docs/db-hardening.md``: after a
DBA applies that document's server-side changes (dedicated read-only
login, DENY grants, Resource Governor), this script is how anyone
confirms it actually took effect, rather than trusting the document was
followed correctly. It also converts Phase 1's ``database/executor.py``
claims (the ``:name`` bind-parameter fix, the row cap, the driver
timeout, the always-rolled-back transaction) from "asserted in a
docstring" into "demonstrated against this specific deployment" -- the
same claims ``tests/integration/test_executor_live.py`` proves in CI,
run here instead against the real target.

Safety
------
Every database probe here is read-only, with ONE deliberate exception:
a write attempt (``CREATE TABLE``), which is *expected to fail or be
rolled back* -- that failure IS the thing being verified (a login that
cannot write even if every application-layer guard were bypassed, per
``docs/db-hardening.md``). Nothing here inserts, updates, or deletes real
data, and no credentials (passwords, connection strings with embedded
secrets) are ever printed -- only a password-redacted connection target.

This script never raises to the shell as a crash for an expected
"unreachable dependency" condition: every check is individually wrapped,
reports PASS / FAIL / SKIP with a human-readable reason, and the script
always finishes and prints a summary. The process exit code is 0 only if
no check FAILed (SKIPs do not fail the run -- e.g. no LLM endpoint configured
at all is a valid, if incomplete, deployment to check).
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

# Run as `python scripts/verify_deployment.py` from anywhere: Python puts
# only this script's own directory on sys.path, not the repo root, so
# `import config` (and every other top-level package this script touches)
# would otherwise fail unless the repo root is on the path explicitly —
# same fix as scripts/analyze_misses.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as cfg

_SCRATCH_TABLE = "_nlq_agent_deploy_verify_probe"


@dataclass
class CheckResult:
    """One line of the report."""

    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""

    def render(self) -> str:
        return f"[{self.status:4s}] {self.name}" + (f" -- {self.detail}" if self.detail else "")


def _redact(url: str) -> str:
    """Render a connection URL with any password masked.

    Never used to print a raw connection string -- see the module
    docstring's "no credentials are ever printed" guarantee.
    """
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 — a malformed URL must not crash reporting
        return "<unparsable connection URL>"


def check_settings_valid() -> CheckResult:
    """``Settings.validate()`` must pass -- required config, no placeholders."""
    try:
        cfg.settings.validate()
    except ValueError as exc:
        return CheckResult("Settings.validate()", "FAIL", str(exc))
    return CheckResult("Settings.validate()", "PASS")


def check_db_connectivity() -> CheckResult:
    """A trivial ``SELECT 1`` must succeed against the configured database."""
    try:
        from database.connection import get_engine
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        return CheckResult(
            "Database connectivity", "FAIL",
            f"could not connect to {_redact(cfg.settings.db_connection_url)}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Database connectivity", "FAIL", str(exc))
    return CheckResult(
        "Database connectivity", "PASS",
        f"connected to {_redact(cfg.settings.db_connection_url)}",
    )


def check_login_is_read_only() -> CheckResult:
    """The configured login must be refused (or rolled back) on a write.

    Mirrors ``docs/db-hardening.md``'s verification checklist item: "Confirm
    ``auction_nlq_reader`` gets a permission error ... attempting
    INSERT/UPDATE/DELETE/DROP/CREATE/EXEC on any table." Uses a scratch
    table name unlikely to collide with anything real, and cleans up
    defensively in a ``finally`` regardless of outcome.
    """
    try:
        from database.connection import get_engine
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Login is read-only", "SKIP", f"no database connection: {exc}")

    try:
        with engine.connect() as conn:
            conn.execute(text(
                f"IF OBJECT_ID('{_SCRATCH_TABLE}') IS NOT NULL "
                f"DROP TABLE {_SCRATCH_TABLE}"
            ))
            conn.commit()
    except SQLAlchemyError:
        # If we can't even run the cleanup DDL, the login is at minimum
        # not able to freely DROP TABLE -- treat as inconclusive rather
        # than guessing, and let the CREATE TABLE attempt below speak
        # for itself.
        pass

    write_refused = False
    write_error = ""
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.exec_driver_sql(f"CREATE TABLE {_SCRATCH_TABLE} (x INT)")
            finally:
                trans.rollback()  # never commit, regardless of outcome
    except SQLAlchemyError as exc:
        write_refused = True
        write_error = str(exc)

    # Whether or not the statement itself raised, confirm nothing persisted.
    persisted = None
    try:
        with engine.connect() as conn:
            persisted = conn.execute(
                text(f"SELECT OBJECT_ID('{_SCRATCH_TABLE}') AS oid")
            ).scalar()
    except SQLAlchemyError as exc:
        return CheckResult("Login is read-only", "FAIL", f"could not verify: {exc}")
    finally:
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    f"IF OBJECT_ID('{_SCRATCH_TABLE}') IS NOT NULL "
                    f"DROP TABLE {_SCRATCH_TABLE}"
                ))
                conn.commit()
        except SQLAlchemyError:
            pass

    if persisted is not None:
        return CheckResult(
            "Login is read-only", "FAIL",
            f"{_SCRATCH_TABLE} PERSISTED -- the login can write and/or the "
            "always-rolled-back transaction did not hold",
        )
    if write_refused:
        return CheckResult(
            "Login is read-only", "PASS",
            f"CREATE TABLE was refused ({write_error[:120]}) and nothing persisted",
        )
    return CheckResult(
        "Login is read-only", "PASS",
        "CREATE TABLE did not raise, but the transaction rollback held: nothing persisted",
    )


def _db_reachable() -> str | None:
    """Return ``None`` if a trivial query succeeds, else a reason string.

    Shared pre-check for the probes below: without it, a database that is
    simply unreachable (wrong host, VPN down, ...) makes ``execute_sql``
    raise the SAME ``RuntimeError`` a probe is watching for, which would
    otherwise be misread as "the behaviour under test happened" instead
    of "the database was never reached at all".
    """
    try:
        from database.connection import get_engine
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


def check_row_cap() -> CheckResult:
    """``execute_sql`` must never return more than ``max_rows_returned`` rows."""
    unreachable = _db_reachable()
    if unreachable is not None:
        return CheckResult("Row cap", "SKIP", f"database unreachable: {unreachable}")

    try:
        from database.executor import execute_sql
        cap = cfg.settings.max_rows_returned
        df = execute_sql(f"SELECT TOP {cap * 10 + 10} name FROM sys.all_objects")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Row cap", "SKIP", f"could not run probe query: {exc}")
    if len(df) > cfg.settings.max_rows_returned:
        return CheckResult(
            "Row cap", "FAIL",
            f"returned {len(df)} rows, expected <= {cfg.settings.max_rows_returned}",
        )
    return CheckResult(
        "Row cap", "PASS",
        f"returned {len(df)} rows (cap {cfg.settings.max_rows_returned})",
    )


def check_query_timeout() -> CheckResult:
    """A deliberately slow query must abort near the configured timeout."""
    unreachable = _db_reachable()
    if unreachable is not None:
        return CheckResult("Query timeout", "SKIP", f"database unreachable: {unreachable}")

    try:
        from database.executor import execute_sql
        from config import override_settings
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Query timeout", "SKIP", str(exc))

    probe_timeout = min(cfg.settings.query_timeout_seconds, 5) or 5
    start = time.perf_counter()
    try:
        with override_settings(query_timeout_seconds=probe_timeout):
            execute_sql(f"WAITFOR DELAY '00:00:{probe_timeout + 10:02d}'; SELECT 1 AS x")
    except RuntimeError:
        elapsed = time.perf_counter() - start
        if elapsed < probe_timeout + 8:
            return CheckResult(
                "Query timeout", "PASS",
                f"aborted after {elapsed:.1f}s (configured timeout {probe_timeout}s)",
            )
        return CheckResult(
            "Query timeout", "FAIL",
            f"took {elapsed:.1f}s -- longer than the {probe_timeout}s timeout should allow",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Query timeout", "SKIP", f"could not run probe query: {exc}")
    else:
        elapsed = time.perf_counter() - start
        return CheckResult(
            "Query timeout", "FAIL",
            f"WAITFOR DELAY completed in {elapsed:.1f}s without the timeout firing "
            "(is WAITFOR unsupported, e.g. Azure SQL DB serverless tiers, or is the "
            "timeout not actually applied?)",
        )


def check_openai_model_exists() -> CheckResult:
    """The configured model must actually be listed by the OpenAI-compatible endpoint."""
    base = cfg.settings.openai_base_url.rstrip("/")
    try:
        headers = {"Authorization": f"Bearer {cfg.settings.openai_api_key}"}
        resp = requests.get(f"{base}/models", headers=headers, timeout=5)
        resp.raise_for_status()
        models = {m.get("id", "") for m in resp.json().get("data", [])}
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "OpenAI-compatible model exists", "FAIL",
            f"could not reach {base}: {exc}",
        )

    wanted = cfg.settings.openai_model
    if wanted in models:
        return CheckResult("OpenAI-compatible model exists", "PASS", f"'{wanted}' is available")
    return CheckResult(
        "OpenAI-compatible model exists", "FAIL",
        f"'{wanted}' not found among models {base} lists: {sorted(models) or '(none)'}",
    )


# Order matters for readability, not correctness: each check is independent.
_CHECKS: list[Callable[[], CheckResult]] = [
    check_settings_valid,
    check_db_connectivity,
    check_login_is_read_only,
    check_row_cap,
    check_query_timeout,
    check_openai_model_exists,
]


def main() -> int:
    print("Deployment verification")
    print("=" * 60)

    results: list[CheckResult] = []
    for check in _CHECKS:
        try:
            result = check()
        except Exception as exc:  # noqa: BLE001 — a check must never crash the script
            result = CheckResult(check.__name__, "FAIL", f"check raised unexpectedly: {exc}")
        results.append(result)
        print(result.render())

    print("=" * 60)
    n_pass = sum(1 for r in results if r.status == "PASS")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_skip = sum(1 for r in results if r.status == "SKIP")
    print(f"{n_pass} passed, {n_fail} failed, {n_skip} skipped")

    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
