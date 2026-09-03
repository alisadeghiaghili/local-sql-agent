# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
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

Deployment-readiness pass added four checks beyond the original config/DB/
row-cap/timeout/model set: API-key authentication actually being possible
(``check_api_key_authenticates``, optionally proving one specific raw key
end-to-end via ``VERIFY_API_KEY``), the audit log directory being writable
(``check_audit_log_writable`` -- a silent failure here means a whole
production week produces no accuracy/latency data at all, since
``save_audit_record`` never raises to the caller by design),
``project_config/`` actually loading under the CURRENT code's schema
(``check_project_config_loads`` -- a stale ``schema.yaml`` missing a field
a later phase started requiring fails at load, not at startup), and the
rate limit being sane for this deployment's actual shape
(``check_rate_limit_sane_for_deployment`` -- one shared service key can put
many analysts in one bucket; see ``config.Settings.rate_limit_requests``).

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

import os
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


def check_api_key_authenticates() -> CheckResult:
    """An API key is configured, and (fail-closed) starting the server would
    not immediately refuse to run.

    Mirrors ``api/server.py``'s own ``lifespan`` startup gate exactly — see
    that function's "Phase 8: fail closed on authentication config" comment
    — so this FAILs here, before a real deploy attempt, instead of the
    server refusing to start on first launch with nobody watching.

    Beyond "at least one key is configured", this can optionally prove a
    *specific* raw key actually authenticates end-to-end: set
    ``VERIFY_API_KEY`` (the raw token, e.g. one printed once by
    ``scripts/issue_api_key.py``) in the environment running this script
    (never persisted anywhere -- read once, used once, discarded with the
    process). Without it, the check still PASSes on "at least one key is
    configured and AUTH_REQUIRED's fail-closed gate would not trip", but
    cannot prove any *specific* key actually round-trips through
    ``security.auth.resolve_principal`` the way a real caller's bearer
    token would.
    """
    from security.auth import ApiKeyConfigError, load_api_keys, resolve_principal

    try:
        keys = load_api_keys()
    except ApiKeyConfigError as exc:
        return CheckResult(
            "API key authentication", "FAIL",
            f"API_KEYS_JSON is invalid: {exc} -- the server would refuse to start",
        )

    if not cfg.settings.auth_required:
        return CheckResult(
            "API key authentication", "PASS",
            "AUTH_REQUIRED=false -- deliberate escape hatch, not fail-closed "
            "(every startup logs a WARNING for this; do not use in production)",
        )

    if not keys:
        return CheckResult(
            "API key authentication", "FAIL",
            "AUTH_REQUIRED is true but API_KEYS_JSON has no configured keys -- "
            "the server refuses to start (see api/server.py's lifespan). Issue "
            "one with: python scripts/issue_api_key.py --id analyst-1 --name "
            "\"Jane Analyst\", then set API_KEYS_JSON.",
        )

    raw_key = os.environ.get("VERIFY_API_KEY", "").strip()
    if not raw_key:
        return CheckResult(
            "API key authentication", "PASS",
            f"{len(keys)} key(s) configured, AUTH_REQUIRED=true -- the server "
            "will start. To also prove a specific key authenticates end-to-end, "
            "re-run with VERIFY_API_KEY=<raw key> set in the environment.",
        )

    principal = resolve_principal(f"Bearer {raw_key}", keys)
    if principal is None:
        return CheckResult(
            "API key authentication", "FAIL",
            "VERIFY_API_KEY was set but did not match any configured key's "
            "SHA-256 digest -- this raw key would get a 401 from the real "
            "server. Re-check it was copied correctly, or issue a fresh one.",
        )
    return CheckResult(
        "API key authentication", "PASS",
        f"VERIFY_API_KEY authenticated as principal '{principal.id}' ({principal.name})",
    )


def check_audit_log_writable() -> CheckResult:
    """``logs/audit_log.jsonl``'s directory must exist and be writable.

    This is the ONLY record a first production week produces of its own
    accuracy/latency numbers (see ``observability/audit.py``) -- a
    directory that can't be written to fails every single query's audit
    write silently (``save_audit_record`` never raises to the caller, by
    design -- see that module's second hard rule), so the whole week could
    run with zero audit trail and nobody would see an error about it
    anywhere except the application log. Checked here, loudly, before that
    can happen.

    Never writes into the real ``audit_log.jsonl`` itself (only creates the
    *directory* if missing, and probes writability with a throwaway sidecar
    file that is immediately removed) -- an audit log full of a preflight
    script's own test writes would be exactly the kind of noise
    ``scripts/analyze_audit_log.py``'s "records by model" section exists to
    surface, and there is no reason to add to it when a directory-level
    probe proves the same thing.
    """
    log_dir = Path(cfg.settings.log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            "Audit log directory writable", "FAIL",
            f"could not create {log_dir}: {exc}",
        )

    probe = log_dir / ".verify_deployment_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            "Audit log directory writable", "FAIL",
            f"{log_dir} exists but is not writable: {exc}",
        )

    audit_log_path = log_dir / "audit_log.jsonl"
    if audit_log_path.exists() and not os.access(audit_log_path, os.W_OK):
        return CheckResult(
            "Audit log directory writable", "FAIL",
            f"{audit_log_path} exists but is not writable (check file "
            "permissions/ownership)",
        )
    return CheckResult(
        "Audit log directory writable", "PASS",
        f"{log_dir} is writable"
        + (f"; {audit_log_path} exists and is writable" if audit_log_path.exists() else ""),
    )


def check_project_config_loads() -> CheckResult:
    """``project_config/`` (or wherever ``PROJECT_CONFIG_DIR`` points) is
    present and loads under the schema the CURRENT code expects.

    A ``project_config/`` copied from an older deployment (or restored from
    an old backup) can be missing fields a later phase started requiring --
    e.g. ``schema.yaml`` missing the ``db_schema`` qualifier or the
    resolvable/prefetchable-column allowlists Phase 5b's value resolver
    needs (see ``schema_data/registry.py``'s module docstring) -- and that
    surfaces as a ``ConfigNotFoundError``/``ValueError`` the first time a
    real question is asked, not at startup. Loads every
    ``knowledge.config_loader`` file plus ``schema_data.registry.load_schema()``
    here instead, so a stale config fails this preflight with a clear
    filename and field, not a confusing error mid-query on day one.
    """
    from knowledge.config_loader import (
        ConfigNotFoundError,
        load_aliases,
        load_business_rules,
        load_entities,
        load_examples,
        load_metrics,
    )
    from schema_data.registry import load_schema

    loaders: list[tuple[str, Callable[[], object]]] = [
        ("aliases.yaml", load_aliases),
        ("entities.yaml", load_entities),
        ("business_rules.yaml", load_business_rules),
        ("examples.yaml", load_examples),
        ("metrics.yaml", load_metrics),
        ("schema.yaml", load_schema),
    ]

    for filename, loader in loaders:
        try:
            loader()
        except ConfigNotFoundError as exc:
            return CheckResult(
                "project_config/ loads", "FAIL",
                f"{filename} not found under '{cfg.settings.project_config_dir}': {exc}",
            )
        except ValueError as exc:
            return CheckResult(
                "project_config/ loads", "FAIL",
                f"{filename} failed validation against the schema this code "
                f"expects: {exc} -- a project_config/ copied from an older "
                "deployment may be missing a field a later phase added "
                "(e.g. schema.yaml's db_schema qualifier); regenerate or "
                "hand-edit it to match schema_data/registry.py's current model",
            )
    return CheckResult(
        "project_config/ loads", "PASS",
        f"all six files loaded from '{cfg.settings.project_config_dir}'",
    )


def check_rate_limit_sane_for_deployment() -> CheckResult:
    """The rate limit must not throttle legitimate use in THIS deployment's
    shape: one shared service key (or a small handful of them) fronting
    however many analysts actually use it.

    ``RateLimitMiddleware`` buckets on ``(principal, ip)`` (Phase 8), so a
    single web UI issued a single service key still puts every one of its
    users in one bucket -- see ``config.Settings.rate_limit_requests``'s
    own docstring for the incident this already caused once at the old
    ``60``/``60``/``10`` defaults. Set ``VERIFY_EXPECTED_ANALYSTS`` to
    override the default assumption of 10 concurrent analysts behind the
    smallest configured key's bucket; the check FAILs when the configured
    sustained rate works out to less than one request per analyst every 10
    seconds, which would visibly throttle ordinary interactive use (a
    human asking a question every few seconds).
    """
    try:
        expected_analysts = int(os.environ.get("VERIFY_EXPECTED_ANALYSTS", "10"))
    except ValueError:
        expected_analysts = 10

    requests_per_window = cfg.settings.rate_limit_requests
    window = cfg.settings.rate_limit_window_seconds
    burst = cfg.settings.rate_limit_burst
    per_analyst_per_sec = (requests_per_window / window) / max(expected_analysts, 1)
    detail = (
        f"RATE_LIMIT_REQUESTS={requests_per_window} RATE_LIMIT_WINDOW_SEC={window} "
        f"RATE_LIMIT_BURST={burst} -> {per_analyst_per_sec:.3f} req/sec/analyst "
        f"assuming {expected_analysts} concurrent analysts sharing one bucket "
        "(override with VERIFY_EXPECTED_ANALYSTS)"
    )
    # 1 request per 10s per analyst (0.1/s) is a conservative floor for
    # "a human asking interactive questions" -- see config.Settings.
    # rate_limit_requests's own docstring for the 30-analysts/600-per-minute
    # reasoning this mirrors.
    if per_analyst_per_sec < 0.1:
        return CheckResult(
            "Rate limit sane for deployment", "FAIL",
            detail + " -- below the 0.1 req/sec/analyst floor; raise "
            "RATE_LIMIT_REQUESTS (or reduce VERIFY_EXPECTED_ANALYSTS if this "
            "overestimates real concurrency)",
        )
    return CheckResult("Rate limit sane for deployment", "PASS", detail)


# Order matters for readability, not correctness: each check is independent.
_CHECKS: list[Callable[[], CheckResult]] = [
    check_settings_valid,
    check_db_connectivity,
    check_login_is_read_only,
    check_row_cap,
    check_query_timeout,
    check_openai_model_exists,
    check_api_key_authenticates,
    check_audit_log_writable,
    check_project_config_loads,
    check_rate_limit_sane_for_deployment,
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
