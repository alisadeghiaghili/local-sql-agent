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

With more than one warehouse data source configured
(``project_config/datasources.yaml``, see ``docs/design/DATASOURCES.md``),
every database check below (connectivity, read-only login, row cap, query
timeout) runs once per source -- see :func:`build_checks` -- plus one more
check, ``check_table_datasources``, confirming every ``schema.yaml``
table's ``datasource:`` (if any) actually names a configured source, and
``check_tables_in_assigned_sources``, which fails -- printing the
``datasource:`` line to write -- when a table is missing from the source(s)
``schema.yaml`` assigns it to but another source has it (the same finding
as the admin panel's schema-drift card). With the default single source
this script's output is unchanged: the database checks keep their plain
names and the placement check is skipped.

``check_schema_structure_matches_databases`` (always run, one or several
sources) is ``python scripts/sync_schema.py --check`` as a preflight check:
it FAILs when a column, a recorded column type or a ``datasource:`` line of
``schema.yaml`` is out of step with the databases, and names the command that
writes the proposed fix. It SKIPs when a database cannot be read.

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
from functools import partial, update_wrapper
from typing import Callable

import requests
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

# Run as `python scripts/verify_deployment.py` from anywhere: Python puts
# only this script's own directory on sys.path, not the repo root, so
# `import config` (and every other top-level package this script touches)
# would otherwise fail unless the repo root is on the path explicitly —
# same fix as scripts/analyze_misses.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.console import use_utf8_console
import config as cfg

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCRATCH_TABLE = "_nlq_agent_deploy_verify_probe"


@dataclass
class CheckResult:
    """One line of the report."""

    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""

    def render(self) -> str:
        return f"[{self.status:4s}] {self.name}" + (f" -- {self.detail}" if self.detail else "")


def check_settings_valid() -> CheckResult:
    """``Settings.validate()`` must pass -- required config, no placeholders."""
    try:
        cfg.settings.validate()
    except ValueError as exc:
        return CheckResult("Settings.validate()", "FAIL", str(exc))
    return CheckResult("Settings.validate()", "PASS")


def _label(name: str, datasource: str | None) -> str:
    """Check name, suffixed with the data source when there are several."""
    return name if datasource is None else f"{name} [{datasource}]"


def _source_url(datasource: str | None) -> str:
    """The data source's connection URL with its password masked.

    Never the raw connection string -- see the module docstring's "no
    credentials are ever printed" guarantee.
    """
    from database.datasources import get_datasource

    return get_datasource(datasource).redacted_url


def check_db_connectivity(datasource: str | None = None) -> CheckResult:
    """A trivial ``SELECT 1`` must succeed against the data source."""
    name = _label("Database connectivity", datasource)
    try:
        from database.connection import get_engine
        engine = get_engine(datasource)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        return CheckResult(
            name, "FAIL",
            f"could not connect to {_source_url(datasource)}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", str(exc))
    return CheckResult(name, "PASS", f"connected to {_source_url(datasource)}")


def check_login_is_read_only(datasource: str | None = None) -> CheckResult:
    """The configured login must be refused (or rolled back) on a write.

    Mirrors ``docs/db-hardening.md``'s verification checklist item: "Confirm
    ``auction_nlq_reader`` gets a permission error ... attempting
    INSERT/UPDATE/DELETE/DROP/CREATE/EXEC on any table." Uses a scratch
    table name unlikely to collide with anything real, and cleans up
    defensively in a ``finally`` regardless of outcome.
    """
    name = _label("Login is read-only", datasource)
    try:
        from database.connection import get_engine
        engine = get_engine(datasource)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "SKIP", f"no database connection: {exc}")

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
        return CheckResult(name, "FAIL", f"could not verify: {exc}")
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
            name, "FAIL",
            f"{_SCRATCH_TABLE} PERSISTED -- the login can write and/or the "
            "always-rolled-back transaction did not hold",
        )
    if write_refused:
        return CheckResult(
            name, "PASS",
            f"CREATE TABLE was refused ({write_error[:120]}) and nothing persisted",
        )
    return CheckResult(
        name, "PASS",
        "CREATE TABLE did not raise, but the transaction rollback held: nothing persisted",
    )


def _db_reachable(datasource: str | None = None) -> str | None:
    """Return ``None`` if a trivial query succeeds, else a reason string.

    Shared pre-check for the probes below: without it, a database that is
    simply unreachable (wrong host, VPN down, ...) makes ``execute_sql``
    raise the SAME ``RuntimeError`` a probe is watching for, which would
    otherwise be misread as "the behaviour under test happened" instead
    of "the database was never reached at all".
    """
    try:
        from database.connection import get_engine
        with get_engine(datasource).connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


def check_row_cap(datasource: str | None = None) -> CheckResult:
    """``execute_sql`` must never return more than ``max_rows_returned`` rows."""
    name = _label("Row cap", datasource)
    unreachable = _db_reachable(datasource)
    if unreachable is not None:
        return CheckResult(name, "SKIP", f"database unreachable: {unreachable}")

    try:
        from database.executor import execute_sql
        cap = cfg.settings.max_rows_returned
        df = execute_sql(
            f"SELECT TOP {cap * 10 + 10} name FROM sys.all_objects", datasource=datasource,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "SKIP", f"could not run probe query: {exc}")
    if len(df) > cfg.settings.max_rows_returned:
        return CheckResult(
            name, "FAIL",
            f"returned {len(df)} rows, expected <= {cfg.settings.max_rows_returned}",
        )
    return CheckResult(
        name, "PASS",
        f"returned {len(df)} rows (cap {cfg.settings.max_rows_returned})",
    )


def check_query_timeout(datasource: str | None = None) -> CheckResult:
    """A deliberately slow query must abort near the configured timeout."""
    name = _label("Query timeout", datasource)
    unreachable = _db_reachable(datasource)
    if unreachable is not None:
        return CheckResult(name, "SKIP", f"database unreachable: {unreachable}")

    try:
        from database.executor import execute_sql
        from config import override_settings
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "SKIP", str(exc))

    probe_timeout = min(cfg.settings.query_timeout_seconds, 5) or 5
    start = time.perf_counter()
    try:
        with override_settings(query_timeout_seconds=probe_timeout):
            execute_sql(
                f"WAITFOR DELAY '00:00:{probe_timeout + 10:02d}'; SELECT 1 AS x",
                datasource=datasource,
            )
    except RuntimeError:
        elapsed = time.perf_counter() - start
        if elapsed < probe_timeout + 8:
            return CheckResult(
                name, "PASS",
                f"aborted after {elapsed:.1f}s (configured timeout {probe_timeout}s)",
            )
        return CheckResult(
            name, "FAIL",
            f"took {elapsed:.1f}s -- longer than the {probe_timeout}s timeout should allow",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "SKIP", f"could not run probe query: {exc}")
    else:
        elapsed = time.perf_counter() - start
        return CheckResult(
            name, "FAIL",
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


def _verify_api_key_invocation() -> str:
    """A copy-pasteable invocation setting ``VERIFY_API_KEY``, for *this* shell.

    The advice this replaces read ``VERIFY_API_KEY=<raw key>`` — the
    POSIX inline-environment form, which PowerShell does not have. On
    Windows, where ``docs/fa/getting-started.md`` does its whole setup
    walk-through in ``powershell`` blocks, pasting it does not fail as a
    misconfigured check: it fails as ``The term 'VERIFY_API_KEY=...' is
    not recognized as a name of a cmdlet``, which reads as "this script
    is broken" rather than "your shell spells this differently".

    Branching on :data:`os.name` rather than printing both forms keeps
    the hint one line, and gets the common case right: the reader is
    being told what to type into the shell they just typed something
    into.
    """
    if os.name == "nt":
        return '$env:VERIFY_API_KEY = "<raw key>"; python -m scripts.verify_deployment'
    return "VERIFY_API_KEY='<raw key>' python -m scripts.verify_deployment"


def _key_sources(merged: dict, env_keys: dict) -> str:
    """``"1 from API_KEYS_FILE (keys.json), 2 from the application database"``.

    The first part names the environment source in use (``API_KEYS_FILE`` with
    its path, or ``API_KEYS_JSON``). A key present in both is counted under
    it; only hashes the environment does not have are attributed to the
    application database.
    """
    from security.auth import api_keys_source

    from_env = len(merged.keys() & env_keys.keys())
    parts = []
    if from_env:
        parts.append(f"{from_env} from {api_keys_source()}")
    if len(merged) - from_env:
        parts.append(f"{len(merged) - from_env} from the application database")
    return ", ".join(parts)


def check_api_key_authenticates() -> CheckResult:
    """An API key is configured, and (fail-closed) starting the server would
    not immediately refuse to run.

    Mirrors ``api/server.py``'s own ``lifespan`` startup gate -- see that
    function's "Phase 8: fail closed on authentication config" comment --
    so this FAILs here, before a real deploy attempt, instead of the server
    refusing to start on first launch with nobody watching. Like the server,
    it asks whether any key exists in the *merged* set
    (:func:`appdb.key_store.get_active_principals`: ``API_KEYS_JSON`` or the
    ``API_KEYS_FILE`` file, plus the application database), so a deployment whose keys were imported at
    first start or issued from the admin panel passes.

    This script runs in its own process, so it reads ``API_KEYS_FILE`` as it is
    now; a server already running keeps the copy it read at start-up and needs
    a restart after the file is edited.

    This only reads. It never calls ``bootstrap_from_env`` and writes no key
    rows; opening the application database does create its tables when
    missing, exactly as the server's own startup would. If the database
    cannot be read (not configured, unreachable, driver missing) the check
    falls back to the environment keys alone, says so in the detail, and FAILs
    only if that has no keys either.

    Beyond "at least one key is configured", this can optionally prove a
    *specific* raw key actually authenticates end-to-end: set
    ``VERIFY_API_KEY`` (the raw token, e.g. one printed once by
    ``scripts/issue_api_key.py`` or by the admin panel) in the environment
    running this script (never persisted anywhere -- read once, used once,
    discarded with the process). It is resolved against the same merged set
    the server uses, so a key that lives only in the application database
    can be proven too. Without it, the check still PASSes on "at least one
    key is configured and AUTH_REQUIRED's fail-closed gate would not trip",
    but cannot prove any *specific* key actually round-trips through
    ``security.auth.resolve_principal`` the way a real caller's bearer
    token would.
    """
    from security.auth import (
        ApiKeyConfigError,
        api_keys_source,
        load_api_keys,
        resolve_principal,
    )

    try:
        env_keys = load_api_keys()
    except ApiKeyConfigError as exc:
        return CheckResult(
            "API key authentication", "FAIL",
            f"API key configuration is invalid: {exc} -- the server would refuse to start",
        )

    if not cfg.settings.auth_required:
        return CheckResult(
            "API key authentication", "PASS",
            "AUTH_REQUIRED=false -- deliberate escape hatch, not fail-closed "
            "(every startup logs a WARNING for this; do not use in production)",
        )

    keys = env_keys
    db_note = ""
    try:
        from appdb.key_store import get_active_principals

        keys = get_active_principals()
    except Exception as exc:  # noqa: BLE001 - any failure reading the app DB degrades to env-only
        reason = " ".join(f"{type(exc).__name__}: {exc}".split())[:120]
        db_note = (
            f" (application database not readable: {reason}; "
            f"counted {api_keys_source()} only)"
        )

    if not keys:
        detail = (
            "AUTH_REQUIRED is true but there are no configured keys in "
            "API_KEYS_FILE, API_KEYS_JSON or the application database -- the "
            "server refuses to start (see api/server.py's lifespan). Issue "
            "one with: python -m scripts.issue_api_key --id analyst-1 --name "
            "\"Jane Analyst\", then add its entry to the array in "
            "API_KEYS_FILE (or set API_KEYS_JSON), or issue one from the "
            "admin panel."
        )
        if env_keys:
            detail = (
                f"AUTH_REQUIRED is true but every key in {api_keys_source()} is "
                "revoked or disabled in the application database, so there "
                "are no configured keys the server would accept -- it refuses "
                "to start (see api/server.py's lifespan)."
            )
        return CheckResult("API key authentication", "FAIL", detail + db_note)

    raw_key = os.environ.get("VERIFY_API_KEY", "").strip()
    if not raw_key:
        return CheckResult(
            "API key authentication", "PASS",
            f"{len(keys)} key(s) configured ({_key_sources(keys, env_keys)}), "
            "AUTH_REQUIRED=true -- the server will start. To also prove a "
            "specific key authenticates end-to-end, re-run with VERIFY_API_KEY "
            f"set: {_verify_api_key_invocation()}{db_note}",
        )

    principal = resolve_principal(f"Bearer {raw_key}", keys)
    if principal is None:
        return CheckResult(
            "API key authentication", "FAIL",
            "VERIFY_API_KEY was set but did not match any configured key's "
            "SHA-256 digest -- this raw key would get a 401 from the real "
            "server. Re-check it was copied correctly, or issue a fresh one."
            f"{db_note}",
        )
    return CheckResult(
        "API key authentication", "PASS",
        f"VERIFY_API_KEY authenticated as principal '{principal.id}' "
        f"({principal.name}); {len(keys)} key(s) configured "
        f"({_key_sources(keys, env_keys)}){db_note}",
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


def check_session_store_writable() -> CheckResult:
    """``session_store_path``'s directory must exist and be writable
    (§9/§10 — session and cross-session-memory persistence).

    Mirrors ``check_audit_log_writable`` above exactly, for the same
    reason: ``session.persistence.SessionPersistence`` is constructed
    lazily, on the FIRST ``POST /v2/sessions`` or ``GET/PUT /v2/memory*``
    request this process ever serves — a directory that cannot be written
    to would surface as that first analyst's request failing, not as a
    startup-time error anyone is watching for. Skipped (not failed) when
    ``session_store_path`` is empty — the documented, deliberate way to
    disable persistence entirely.
    """
    if not cfg.settings.session_store_path:
        return CheckResult(
            "Session store directory writable", "SKIP",
            "session_store_path is empty -- persistence deliberately disabled",
        )

    store_dir = Path(cfg.settings.session_store_path).parent
    if str(store_dir) in ("", "."):
        store_dir = Path(".")
    try:
        store_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            "Session store directory writable", "FAIL",
            f"could not create {store_dir}: {exc}",
        )

    probe = store_dir / ".verify_deployment_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            "Session store directory writable", "FAIL",
            f"{store_dir} exists but is not writable: {exc}",
        )
    return CheckResult(
        "Session store directory writable", "PASS",
        f"{store_dir} is writable (session_store_path={cfg.settings.session_store_path!r})",
    )


def _project_config_dir() -> Path:
    """The directory the application reads ``project_config/`` from.

    Resolved the way ``knowledge.config_loader`` does: a relative
    ``PROJECT_CONFIG_DIR`` is relative to the repository root, not to the
    directory this script was started from.
    """
    configured = Path(cfg.settings.project_config_dir)
    return configured if configured.is_absolute() else _REPO_ROOT / configured


def _project_config_loaders() -> list[tuple[str, Callable[[], object]]]:
    """``(file name, loader)`` for every required ``project_config/`` file.

    One entry per name in ``core.project_config_files.REQUIRED_PROJECT_CONFIG_FILES``
    (a test pins the two together), each the function the application itself
    uses to read that file, so a pass here means the application can load it.

    Raises:
        ImportError: If the loaders cannot be imported (a broken install).
            Importing them reads no ``project_config/`` file; each loader
            reads its own file when called.
    """
    from knowledge.config_loader import (
        load_aliases,
        load_business_rules,
        load_entities,
        load_examples,
        load_memory_policy,
        load_metrics,
        load_retrieval_hints,
        load_session_policy,
        load_system_prompt,
    )
    from schema_data.registry import load_schema

    def load_prompt() -> str:
        prompt = load_system_prompt()
        if not prompt.strip():
            raise ValueError("the system prompt is empty")
        return prompt

    return [
        ("aliases.yaml", load_aliases),
        ("entities.yaml", load_entities),
        ("business_rules.yaml", load_business_rules),
        ("examples.yaml", load_examples),
        ("metrics.yaml", load_metrics),
        ("schema.yaml", load_schema),
        ("retrieval_hints.yaml", load_retrieval_hints),
        ("session_policy.yaml", load_session_policy),
        ("memory_policy.yaml", load_memory_policy),
        ("system_prompt.md", load_prompt),
    ]


def check_project_config_loads() -> CheckResult:
    """``project_config/`` (or wherever ``PROJECT_CONFIG_DIR`` points) is
    present and loads under the schema the CURRENT code expects.

    A ``project_config/`` copied from an older deployment (or restored from
    an old backup) can be missing fields a later phase started requiring --
    e.g. ``schema.yaml`` missing the ``db_schema`` qualifier or the
    resolvable/prefetchable-column allowlists Phase 5b's value resolver
    needs (see ``schema_data/registry.py``'s module docstring) -- and that
    surfaces as a ``ConfigNotFoundError``/``ValueError`` the first time a
    real question is asked, not at startup. Loads all ten required files
    (``core.project_config_files.REQUIRED_PROJECT_CONFIG_FILES``: the nine
    ``knowledge.config_loader`` / ``schema_data.registry`` YAML files and
    ``system_prompt.md``) here instead, so a stale config fails this
    preflight with a clear filename and field, not a confusing error
    mid-query on day one. Every missing file is named at once, not only the
    first.
    """
    from core.project_config_files import (
        REQUIRED_PROJECT_CONFIG_FILES,
        missing_project_config_files,
    )

    directory = _project_config_dir()
    missing = missing_project_config_files(directory)
    if missing:
        return CheckResult(
            "project_config/ loads", "FAIL",
            f"{len(missing)} of {len(REQUIRED_PROJECT_CONFIG_FILES)} required files not found "
            f"under '{cfg.settings.project_config_dir}': {', '.join(missing)} -- copy each "
            "from project_config.example/ and edit it for your data",
        )

    from knowledge.config_loader import ConfigNotFoundError

    for filename, loader in _project_config_loaders():
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
        f"all {len(REQUIRED_PROJECT_CONFIG_FILES)} files loaded from '{cfg.settings.project_config_dir}'",
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


def check_table_datasources() -> CheckResult:
    """Every ``schema.yaml`` table must name a configured data source."""
    try:
        from database.datasources import check_table_datasources as _check, datasource_names

        _check()
        names = datasource_names()
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Tables map to data sources", "FAIL", str(exc))
    return CheckResult(
        "Tables map to data sources", "PASS",
        f"{len(names)} data source(s): {', '.join(names)}",
    )


#: How many "set datasource: ..." hints :func:`check_tables_in_assigned_sources`
#: prints before summarising the rest.
_MAX_PLACEMENT_HINTS = 10


def check_tables_in_assigned_sources() -> CheckResult:
    """A table must be in the data source(s) ``schema.yaml`` assigns it to.

    Reads :func:`schema_data.drift.check_schema_drift`'s ``misplaced_tables``:
    tables every one of whose columns is missing from an assigned source
    while another configured source has them. FAIL names each table with the
    ``datasource:`` value to put in ``schema.yaml``
    (``scripts/assign_datasources.py`` writes a whole file of them). SKIP
    with a single data source. Read-only; the drift baseline is not moved.
    """
    name = "Tables are in their data source"
    try:
        from database.datasources import datasource_names

        if len(datasource_names()) < 2:
            return CheckResult(name, "SKIP", "one data source -- nothing to place")
        from schema_data.drift import check_schema_drift

        misplaced = check_schema_drift(persist_baseline=False).misplaced_tables
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "FAIL", f"could not compare schema.yaml with the data sources: {exc}")
    if not misplaced:
        return CheckResult(name, "PASS", "no table is in a different data source than schema.yaml assigns")
    shown = [str(m["hint"]) for m in misplaced[:_MAX_PLACEMENT_HINTS]]
    more = len(misplaced) - len(shown)
    detail = "; ".join(shown) + (f"; and {more} more" if more > 0 else "")
    return CheckResult(
        name, "FAIL",
        f"{len(misplaced)} table(s): {detail} -- `python scripts/sync_schema.py` writes all of "
        "these datasource: lines (and the rest of the structure) for review",
    )


def check_schema_structure_matches_databases() -> CheckResult:
    """``schema.yaml``'s structure must match what the databases have.

    Runs the same comparison as ``python scripts/sync_schema.py --check``
    (:func:`schema_data.sync.sync_schema_text`, default options): FAIL when a
    sync would change the file -- a ``datasource:`` missing or wrong, a column
    the database has that ``schema.yaml`` lacks, a recorded column type that
    differs, a column or table newly missing from the database -- naming the
    counts and the command that writes the proposed file. Columns already
    marked ``# not in database`` are reported in the detail, not failed.
    Reads three ``INFORMATION_SCHEMA`` views per data source and nothing else.

    SKIP when the comparison cannot be made: ``schema.yaml`` does not load
    (``project_config/ loads`` says why), a database cannot be reached (its
    own connectivity check fails), or the file is laid out in a way the sync
    cannot edit.
    """
    name = "Schema structure matches the databases"
    try:
        from database.catalogue import read_catalogue
        from database.connection import get_engine
        from database.datasources import datasource_names, default_datasource_name
        from schema_data.registry import schema_yaml_path, validate_schema_yaml_text
        from schema_data.sync import sync_schema_text

        original = schema_yaml_path().read_text(encoding="utf-8-sig")
        validate_schema_yaml_text(original)
        sources = datasource_names()
        default = default_datasource_name()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "SKIP", f"schema.yaml or the data sources did not load: {type(exc).__name__}")
    catalogues = {}
    for source in sources:
        try:
            catalogues[source] = read_catalogue(get_engine(source))
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name, "SKIP",
                f"could not read the catalogue of data source '{source}' ({type(exc).__name__})",
            )
    try:
        result = sync_schema_text(original, sources, default, catalogues)
    except ValueError as exc:
        return CheckResult(name, "SKIP", f"schema.yaml cannot be compared: {str(exc).splitlines()[0][:160]}")
    plan = result.plan
    stale = sum(len(t.stale) for t in plan.tables)
    if not result.changed:
        detail = "schema.yaml matches the databases"
        if stale or any(t.nowhere for t in plan.tables):
            detail += (
                f" ({stale} column(s) and {sum(t.nowhere for t in plan.tables)} table(s) marked as "
                "not in the database -- `python scripts/sync_schema.py --prune` removes them)"
            )
        return CheckResult(name, "PASS", detail)
    parts = [
        f"{sum(len(t.adds) for t in plan.tables)} column(s) missing from schema.yaml",
        f"{len(result.stats.marked_columns)} column(s) and {len(result.stats.marked_tables)} table(s) "
        "no longer in the database",
        f"{sum(len(t.changed) for t in plan.tables)} column type(s) differ",
        f"{sum(bool(plan.multi_source and t.placement.found_in and t.placement.wanted != t.placement.current) for t in plan.tables)} "
        "datasource: line(s) to set",
    ]
    return CheckResult(
        name, "FAIL",
        "; ".join(parts) + " -- run `python scripts/sync_schema.py`, review schema.synced.yaml "
        "and replace schema.yaml (`--check` is the same test for CI)",
    )


#: Checks run once per data source, in this order, after the global ones
#: that precede them in :func:`_checks`.
_PER_SOURCE_CHECKS: list[Callable[..., CheckResult]] = [
    check_db_connectivity,
    check_login_is_read_only,
    check_row_cap,
    check_query_timeout,
]

#: Checks that run once for the whole deployment.
_GLOBAL_CHECKS: list[Callable[[], CheckResult]] = [
    check_tables_in_assigned_sources,
    check_schema_structure_matches_databases,
    check_openai_model_exists,
    check_api_key_authenticates,
    check_audit_log_writable,
    check_session_store_writable,
    check_project_config_loads,
    check_rate_limit_sane_for_deployment,
]


def build_checks() -> list[Callable[[], CheckResult]]:
    """Return the full, ordered check list for the current configuration.

    Settings first, then the table-to-source mapping, then every database
    check for each data source in turn, then the rest. With one data
    source the database checks keep their plain names; with several each
    result is suffixed with ``[source]``. Order matters for readability,
    not correctness: each check is independent.

    Every entry keeps its underlying check function's ``__name__`` (a
    per-source entry is a :func:`functools.partial` wrapped with
    :func:`functools.update_wrapper`), so ``api/admin_routes.py`` can still
    leave out the slow checks by name.

    Returns
    -------
    list[Callable[[], CheckResult]]
        Zero-argument callables, run in order by :func:`main` and by
        ``GET /admin/health/checks``.
    """
    try:
        from database.datasources import datasource_names

        names: tuple[str, ...] = datasource_names()
    except Exception:  # noqa: BLE001 - reported by check_table_datasources
        names = ()
    per_source: list[Callable[[], CheckResult]] = []
    if len(names) <= 1:
        per_source = list(_PER_SOURCE_CHECKS)
    else:
        for name in names:
            per_source.extend(
                update_wrapper(partial(check, name), check) for check in _PER_SOURCE_CHECKS
            )
    return [check_settings_valid, check_table_datasources, *per_source, *_GLOBAL_CHECKS]


def main() -> int:
    print("Deployment verification")
    print("=" * 60)

    results: list[CheckResult] = []
    for check in build_checks():
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
    use_utf8_console()
    sys.exit(main())
