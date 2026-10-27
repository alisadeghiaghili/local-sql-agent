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

Eight more checks tell a curated ``project_config/`` from a raw draft of
``database.schema_inspector_cli``, ``setup_project.py`` or
``scripts/draft_descriptions.py`` (they read files only, no database, so the
admin health card runs them too):
``check_project_config_is_curated`` (the ``AUTO-GENERATED`` draft headers of
the three tools, placeholder descriptions, descriptions that are only a
table's name or ``<type> column``), ``check_aliases_are_curated`` (synonym seeds),
``check_aliases_hold_no_raw_values`` (synonym keys that are dates, phone
numbers, addresses), ``check_entities_match_schema_keys`` (an entity or ring
alias key that is not a ``schema.yaml`` key, two entities for one table),
``check_every_data_source_is_described``
(a source with no table: a lost database), and three that only advise,
``check_no_staging_tables``, ``check_no_sample_values_in_descriptions`` and
``check_no_table_in_excluded_schema`` (a listed table whose schema the setup
tools are told never to scan: ``EXCLUDE_SCHEMAS``, a source's ``exclude_schemas:``).
With ``FILTER_VALUE_CHECK`` on, one more advisory check is listed,
``check_filter_value_check_has_vocabularies``: the dimension tables with
label-like columns and no ``prefetchable_columns``, where the check cannot help.
There is no WARN status: advice is a PASS whose detail starts ``advice:``, so
the exit code, the summary line and the admin card need no new state. The file
reading lives in :mod:`core.config_doctor`.

``check_policies_use_real_values`` (file reads only, so the admin card runs it too)
fails when ``session_policy.yaml`` or ``memory_policy.yaml`` still holds the sample
options of ``project_config.example/``, or names a table or a column ``schema.yaml``
does not have: on a first install no setup tool writes them, so every real value an
analyst tries to pin would be refused. It points to ``scripts/draft_policies.py``, and
its PASS detail advises how many options are not among the dimension values the
running application has cached, as counts only. The shipped example is not compared
with itself.

``check_column_restrictions_name_real_columns`` reads the keys (the key file or
``API_KEYS_JSON``, and the application database) and fails, naming the key id
and the entry, when a plain ``denied_columns`` entry matches no column of
``schema.yaml``: such an entry denies nothing. Its PASS detail also advises (it
starts ``advice:``) which columns look like a denied name and are not denied
(``CustomerNationalID`` and ``NationalCode`` next to ``NationalID``). It reads
files and the key set only, so the admin panel's preflight card runs it too.

``check_web_ui_origin_allowed`` is the one check that speaks for the browser,
which sends an ``Origin`` that this script, the CLI and ``curl`` do not: with
``VERIFY_UI_ORIGIN`` set to the address the web UI is opened with it PASSes when
``CORS_ALLOWED_ORIGINS`` holds that origin and FAILs, printing the ``.env`` line,
when it does not; without it, it only advises (settings only, no network).

``check_model_answers_each_prompt`` sends two tiny requests whose prompts differ
only in a random token and fails when the endpoint answers the second with the
first's token: a response cache (semantic or prefix) in a gateway in front of the
model, which would serve one question's answer for a similar question. It spends
model-server time, so the admin panel runs it only under "deep checks".

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
from core.cors_origins import clean_origin, env_line, is_loopback_origin, parse_origin
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
                # The timeout IS the expected outcome: no NOLOCK attempt on
                # this statement and no "SQL execution failed" error line.
                probe=True,
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


#: Name of the response-cache probe, as printed and shown on the admin card.
_ECHO_CHECK_NAME = "Model endpoint answers each prompt itself"

#: Completion budget of each probe request: a 32-hex nonce is about 20 to 30
#: tokens, and the rest is headroom for a short preamble or a brief reasoning
#: pass. Two requests at this size are the whole cost of the check.
_ECHO_MAX_TOKENS = 128

#: Seconds each probe request may take before the check gives up (SKIP).
_ECHO_TIMEOUT_SECONDS = 15


def _echo_prompt(nonce: str) -> str:
    """The probe prompt: identical for every nonce except the nonce itself.

    Examples
    --------
    >>> _echo_prompt("ab12")
    'Reply with exactly this token and nothing else: ab12'
    """
    return f"Reply with exactly this token and nothing else: {nonce}"


def check_model_answers_each_prompt() -> CheckResult:
    """Two prompts that differ only in a random token must get two different answers.

    A gateway in front of the model server can keep a response cache that
    matches *similar* prompts (a semantic cache) or prompts that start the
    same way (a prefix cache) and answer a new prompt with a stored reply.
    The application then gets another question's answer: in the setup
    wizard, one alias list for a hundred different tables; in SQL generation,
    the SQL of "count of inactive customers" for "count of active customers",
    with nothing to say so.

    The check sends two requests through the same :class:`~llm.providers.OpenAIBackend`
    settings the server uses (same payload builder, so ``LLM_EXTRA_BODY``,
    the seed and the temperature apply; only ``max_tokens`` is cut to
    :data:`_ECHO_MAX_TOKENS`). Each asks for one fresh random token
    (``uuid4`` hex) and the prompts are identical except for that token. An
    honest endpoint returns each token to its own request. The check passes
    when each reply contains its own token and fails when the second reply
    carries the first token or when either reply lacks its own.

    Skipped under ``LLM_PROVIDER=mock``, for a remote endpoint while
    ``LLM_ALLOW_REMOTE`` is off, and when the endpoint does not answer (the
    ``OpenAI-compatible model exists`` check reports that). The detail gives
    the reply latencies in milliseconds and never the model's text.
    """
    name = _ECHO_CHECK_NAME
    if str(cfg.settings.llm_provider).lower() == "mock":
        return CheckResult(name, "SKIP", "LLM_PROVIDER=mock has no model endpoint")

    import uuid

    from llm.providers import OpenAIBackend
    from llm.trust import default_trust_for_url

    base = cfg.settings.openai_base_url.rstrip("/")
    trusted = (
        cfg.settings.llm_trusted
        if cfg.settings.llm_trusted is not None
        else default_trust_for_url(base)
    )
    if not trusted and not cfg.settings.llm_allow_remote:
        return CheckResult(
            name, "SKIP",
            f"{base} is a remote endpoint and LLM_ALLOW_REMOTE is off; no test request is sent to it",
        )

    class _ProbeBackend(OpenAIBackend):
        """The server's backend, with only the completion budget cut."""

        def _build_payload(self, prompt: str) -> dict:
            payload = super()._build_payload(prompt)
            payload["max_tokens"] = min(
                int(payload.get("max_tokens") or _ECHO_MAX_TOKENS), _ECHO_MAX_TOKENS,
            )
            return payload

    backend = _ProbeBackend(
        model=cfg.settings.openai_model,
        api_key=cfg.settings.openai_api_key,
        base_url=base,
        trusted=trusted,
        retries=1,
        timeout=_ECHO_TIMEOUT_SECONDS,
    )

    nonces = [uuid.uuid4().hex, uuid.uuid4().hex]
    replies: list[str] = []
    latencies: list[int] = []
    for nonce in nonces:
        try:
            raw, meta = backend.generate_with_meta(_echo_prompt(nonce))
        except Exception as exc:  # noqa: BLE001 - not a cache finding; the model check reports reachability
            return CheckResult(
                name, "SKIP",
                f"the endpoint did not answer a test request ({type(exc).__name__}); "
                "`OpenAI-compatible model exists` reports an unreachable endpoint",
            )
        if not raw and meta.get("finish_reason") == "length":
            return CheckResult(
                name, "SKIP",
                f"the model used all {_ECHO_MAX_TOKENS} tokens of the test request before it "
                "answered (a reasoning model); the test is inconclusive. Turn reasoning off "
                "for the request with LLM_EXTRA_BODY to run it",
            )
        replies.append(raw)
        latencies.append(int(meta.get("total_ms") or 0))

    timing = f"replies in {latencies[0]} ms and {latencies[1]} ms"
    own = [nonces[i] in replies[i] for i in range(2)]
    if own[0] and own[1]:
        return CheckResult(name, "PASS", f"each test request was answered with its own token ({timing})")

    consequence = (
        "A response cache in front of the model returned another prompt's answer. "
        "The same cache can serve the wrong SQL for a similar question, and the setup "
        "wizard would write one answer for many tables. Fix: disable response caching "
        "for this client at the gateway, or point OPENAI_BASE_URL at the model server "
        "directly (runbook section 20.10)."
    )
    if nonces[0] in replies[1]:
        return CheckResult(
            name, "FAIL",
            f"the second test request, which carried a different random token, was answered "
            f"with the first request's token ({timing}). {consequence}",
        )
    which = "first" if not own[0] else "second"
    return CheckResult(
        name, "FAIL",
        f"the reply to the {which} test request did not contain its own random token ({timing}). "
        f"{consequence} (A model that ignores the instruction fails this way too: "
        "ask the model server directly to tell the two apart.)",
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


#: How many table keys / file names the curation checks below print before
#: summarising the rest.
_MAX_LISTED = 10

#: Where the runbook explains drafting and curating ``project_config/``.
_RUNBOOK_CURATION = "docs/deployment-runbook.md section 2.3, 2.4 and 16.3"
_RUNBOOK_COLUMN_RESTRICTIONS = "docs/deployment-runbook.md section 2, denied_columns"
_RUNBOOK_POLICIES = "docs/deployment-runbook.md section 2.5"
_RUNBOOK_VALUE_CHECK = "docs/deployment-runbook.md, the filter value check"


def _listed(items: list[str]) -> str:
    """``a, b, c`` with at most :data:`_MAX_LISTED` items, then ``and N more``."""
    shown = items[:_MAX_LISTED]
    more = len(items) - len(shown)
    return ", ".join(shown) + (f", and {more} more" if more > 0 else "")


def _percent(part: int, whole: int) -> str:
    return f"{100 * part // whole}%" if whole else "0%"


def check_project_config_is_curated() -> CheckResult:
    """``project_config/`` must be curated, not a raw ``schema_inspector_cli`` draft.

    ``database.schema_inspector_cli`` writes drafts: every table of one
    database, each described by a placeholder, under a ``# AUTO-GENERATED
    DRAFT`` header. The 6.9.3 and later drafts are in ``sync_schema.py``'s
    format: the description is the table's own name (``Sales_Fact.Trade``)
    and each column's is ``<sql type> column``. ``setup_project.py`` writes
    its files under ``# AUTO-GENERATED by setup_project.py``, and
    ``scripts/draft_descriptions.py`` writes the descriptions a model drafted
    under ``# AUTO-GENERATED by draft_descriptions.py``. An operator who
    copies those files over a curated ``project_config/`` gets a deployment
    that loads and starts, and that answers worse: curated tables, aliases
    and a whole data source are gone, and nothing says so. FAIL when any
    ``*.yaml`` file of ``project_config/`` still carries either header
    (delete the line when the file really was reviewed), when more than
    :data:`core.config_doctor.DRAFT_DESCRIPTION_SHARE` of ``schema.yaml``'s
    table descriptions are the older placeholder, or when more than
    :data:`core.config_doctor.SYNC_DRAFT_SHARE` of them only repeat the
    table's name, or of its column descriptions are only ``<type> column``
    (a file whose header line was deleted is caught here). The detail gives
    the counts, and the next step for both situations: an upgrade restores
    the curated files, a first install (there are none) drafts the
    descriptions with ``scripts/draft_descriptions.py``, reviews them and
    deletes the header line. ``schema.yaml`` is read even when its repeated
    keys keep the strict loader from loading it. File reads only. SKIP when
    the directory has no ``schema.yaml``.
    """
    from core import config_doctor as doctor

    name = "project_config/ is curated, not a raw draft"
    directory = _project_config_dir()
    view = doctor.read_schema(directory)
    if not view.present:
        return CheckResult(name, "SKIP", "schema.yaml was not found or cannot be read -- `project_config/ loads` says why")
    drafts = doctor.find_draft_header_kinds(directory)
    placeholders, total = doctor.placeholder_descriptions(view)
    named, named_total = doctor.name_only_descriptions(view)
    typed, typed_total = doctor.type_only_column_notes(view)
    mostly_placeholders = total > 0 and placeholders / total > doctor.DRAFT_DESCRIPTION_SHARE
    mostly_named = named_total > 0 and named / named_total > doctor.SYNC_DRAFT_SHARE
    mostly_typed = typed_total > 0 and typed / typed_total > doctor.SYNC_DRAFT_SHARE
    if not drafts and not (mostly_placeholders or mostly_named or mostly_typed):
        detail = (
            f"no file carries the {doctor.DRAFT_HEADER} header; {placeholders} of {total} "
            "table description(s) are the inspector's placeholder"
        )
        if named:
            detail += f"; {named} of {named_total} only repeat the table's name"
        if typed:
            detail += f"; {typed} of {typed_total} column description(s) are only `<type> column`"
        return CheckResult(name, "PASS", detail)
    found: list[str] = []
    for header in doctor.DRAFT_HEADERS:
        files = [file for file, kind in drafts.items() if kind == header]
        if files:
            found.append(f"{len(files)} file(s) carry the {header} header: {_listed(files)}")
    if mostly_placeholders:
        found.append(
            f"schema.yaml: {placeholders} of {total} table descriptions ({_percent(placeholders, total)}) "
            "are still the placeholder `auto-detected ... table`"
        )
    if mostly_named:
        found.append(
            f"schema.yaml: {named} of {named_total} table descriptions ({_percent(named, named_total)}) "
            "are only the table's own name (`Schema.Table`)"
        )
    if mostly_typed:
        found.append(
            f"schema.yaml: {typed} of {typed_total} column descriptions ({_percent(typed, typed_total)}) "
            "are only `<type> column`"
        )
    return CheckResult(
        name, "FAIL",
        "; ".join(found) + " -- this is a draft from database.schema_inspector_cli, setup_project.py or "
        "scripts/draft_descriptions.py, not a curated configuration. "
        "Restore the curated files (from a backup or version control) if this is an upgrade, and bring "
        "new tables in with `python scripts/sync_schema.py --add-tables 'Schema.*'`. "
        "On a first install there are no curated files to restore: draft the descriptions with "
        "`python scripts/draft_descriptions.py`, read what it wrote (schema.described.yaml; every drafted "
        "line ends with `# REVIEW`), replace schema.yaml with it and delete its header line. "
        f"If a file was really reviewed, delete its header line ({_RUNBOOK_CURATION})",
    )


def check_aliases_are_curated() -> CheckResult:
    """``aliases.yaml`` must hold a curated vocabulary, not a draft's seeds.

    The inspector's ``aliases.yaml`` draft has ``ring_aliases: {}`` and, as
    ``synonyms``, the sample values it read from string columns: with a
    warehouse that stores timestamps as text, mostly timestamps. FAIL when
    more than :data:`core.config_doctor.NON_WORD_SYNONYM_SHARE` of the
    synonym keys are not words (no run of two letters: timestamps, numbers),
    or more than :data:`core.config_doctor.REVIEW_MARKED_SHARE` of the
    synonym lines still end in the inspector's ``# REVIEW`` marker. The second
    test exists because a seed is a sample value of any string column, so
    only part of a real draft is timestamps and numbers (a quarter of one
    measured on a production warehouse), while all of it carries the marker.

    An empty ``ring_aliases`` alone is NOT a failure: ``knowledge.aliases``
    is read by ``retrieval.value_retriever.ValueRetriever.extract_ring``
    only, which looks for a ring (trading hall) name in the question and sets
    the ``Ring`` filter; a deployment whose warehouse has no such dimension
    has nothing to put there, and the setup wizard itself writes an empty map
    when it finds no dimension table. It is reported as advice in the PASS
    detail while ``schema.yaml`` or ``entities.yaml`` describe tables, and
    stands out in the FAIL detail when it comes with non-word seeds. File
    reads only. SKIP when ``aliases.yaml`` is absent or not a mapping.
    """
    from core import config_doctor as doctor

    name = "Aliases are curated"
    directory = _project_config_dir()
    aliases = doctor.read_aliases(directory)
    if aliases is None:
        return CheckResult(name, "SKIP", "aliases.yaml was not found or is not a mapping -- `project_config/ loads` says why")
    keys = aliases.synonym_keys
    non_words = doctor.non_word_synonym_keys(keys)
    seeds = min(aliases.review_marked, len(keys))
    empty_rings = aliases.ring_aliases == 0
    reasons: list[str] = []
    if keys and non_words / len(keys) > doctor.NON_WORD_SYNONYM_SHARE:
        reasons.append(f"{non_words} of {len(keys)} synonym keys are not words (timestamps or numbers)")
    if keys and seeds / len(keys) > doctor.REVIEW_MARKED_SHARE:
        reasons.append(f"{seeds} of {len(keys)} synonym entries still end in the inspector's `# REVIEW` marker")
    if reasons:
        return CheckResult(
            name, "FAIL",
            "; ".join(reasons) + ("; ring_aliases is empty" if empty_rings else "")
            + " -- these are the seeds database.schema_inspector_cli copies from sample values of the "
            f"live rows, not a vocabulary. Restore the curated aliases.yaml ({_RUNBOOK_CURATION})",
        )
    detail = f"{len(keys)} synonym key(s), {aliases.ring_aliases} ring alias(es)"
    if empty_rings and (
        doctor.read_schema(directory).tables
        or (directory / "entities.yaml").is_file()
    ):
        detail += (
            "; advice: ring_aliases is empty -- right when your questions never name a ring (hall), "
            "since only the ring filter reads it; otherwise add the ring names"
        )
    return CheckResult(name, "PASS", detail)


def check_aliases_hold_no_raw_values() -> CheckResult:
    """``aliases.yaml`` must not hold values copied out of rows as synonym keys.

    The setup wizard of 6.13 and 6.14 turned the sample values of every text
    column into ``synonyms`` keys: dates, phone numbers, national ids, street
    addresses, IP addresses, partly masked names and error messages, hundreds
    of them, in a file that goes into the prompt and into version control.
    Counts the synonym keys shaped like such a value
    (:func:`core.value_shapes.value_shape`) and FAILs above
    :data:`core.config_doctor.RAW_VALUE_SYNONYM_LIMIT`. The detail has the
    count per shape (``<date>``, ``<phone>``, ...), never a key. File reads
    only. SKIP when ``aliases.yaml`` is absent.
    """
    from core import config_doctor as doctor
    from core.value_shapes import shape_label

    name = "Aliases hold no raw data values"
    aliases = doctor.read_aliases(_project_config_dir())
    if aliases is None:
        return CheckResult(name, "SKIP", "aliases.yaml was not found or is not a mapping -- `project_config/ loads` says why")
    shapes = doctor.raw_value_synonym_keys(aliases.synonym_keys)
    found = sum(shapes.values())
    if found <= doctor.RAW_VALUE_SYNONYM_LIMIT:
        return CheckResult(
            name, "PASS",
            f"{found} of {len(aliases.synonym_keys)} synonym key(s) look like a date, a number or an address",
        )
    kinds = ", ".join(
        f"{shape_label(shape)}: {count}" for shape, count in sorted(shapes.items(), key=lambda item: (-item[1], item[0]))
    )
    return CheckResult(
        name, "FAIL",
        f"{found} of {len(aliases.synonym_keys)} synonym keys look like raw data values ({kinds}) -- values copied "
        "from rows (dates, phone numbers, addresses, masked names) are in the prompt and in version control. "
        "Remove them from aliases.yaml (keep only words people type); setup_project.py no longer writes "
        f"them unless --value-synonyms is given ({_RUNBOOK_CURATION})",
    )


def check_entities_match_schema_keys() -> CheckResult:
    """Every entity and ring alias must name a ``schema.yaml`` table by its key, once.

    Retrieval uses an entity's key as the name of a table. The wizard of 6.13
    and 6.14 wrote ``Schema.Table`` for every table when an earlier bare
    entry of the same table made the name look shared, while ``schema.yaml``
    keys a unique table by its bare name: the qualified key matched nothing,
    the bare duplicate beside it had no aliases, and the table was lost to
    retrieval. FAIL when an entity key resolves to no ``schema.yaml`` table,
    when two entities stand for the same table, or when a ``ring_aliases`` key
    names a table of ``schema.yaml`` by another spelling than its key (a hall
    name that is no table is fine). The detail lists table keys and entity
    names only. File reads only. SKIP when ``schema.yaml`` or ``entities.yaml``
    cannot be read.
    """
    from core import config_doctor as doctor

    name = "Entities match schema keys"
    directory = _project_config_dir()
    view = doctor.read_schema(directory)
    if not view.parsed:
        return CheckResult(name, "SKIP", "schema.yaml did not load -- `project_config/ loads` says why")
    entities = doctor.read_entities(directory)
    if entities is None:
        return CheckResult(name, "SKIP", "entities.yaml was not found or is not a mapping -- `project_config/ loads` says why")
    aliases = doctor.read_aliases(directory)
    findings = doctor.entity_key_findings(view, entities, aliases.ring_keys if aliases else ())
    if not findings.found:
        return CheckResult(
            name, "PASS",
            f"all {len(entities)} entity key(s) name a schema.yaml table and no table has two entities",
        )
    parts: list[str] = []
    if findings.unknown_entities:
        parts.append(
            f"{len(findings.unknown_entities)} entity key(s) are not a schema.yaml table key: "
            + _listed([f"{key} (use {right})" if right else key for key, right in findings.unknown_entities])
        )
    if findings.duplicates:
        parts.append(
            f"{len(findings.duplicates)} table(s) have two entities: "
            + _listed([f"{table} ({' and '.join(keys)})" for table, keys in findings.duplicates])
        )
    if findings.unknown_rings:
        parts.append(
            f"{len(findings.unknown_rings)} ring_aliases key(s) name a table but not by its schema.yaml key: "
            + _listed([f"{key} (use {right})" if right else key for key, right in findings.unknown_rings])
        )
    return CheckResult(
        name, "FAIL",
        "; ".join(parts) + " -- retrieval reads an entity's key as the table's schema.yaml key, so such a table "
        "is never found by its aliases. Rename the key to the schema.yaml key, keep one entity per table "
        f"(merge their aliases) and fix ring_aliases the same way ({_RUNBOOK_CURATION})",
    )


def _cached_dimension_values(table: str, column: str) -> list[str] | None:
    """The dimension values the running application has cached for ``table.column``, or ``None``.

    A cache read only. The cache lives in the process that serves questions, so
    this is empty in the command line and has values in the admin panel's card;
    it is read only when the application's module is already imported, never
    imported (and never queried) for the sake of an advice.
    """
    import sys

    module = sys.modules.get("retrieval.dimension_vocabulary")
    if module is None:
        return None
    try:
        return module.get_cached_vocabulary(table, column)
    except Exception:  # noqa: BLE001 - an advice is never worth a failure
        return None


def check_policies_use_real_values() -> CheckResult:
    """The session and memory policies must name a real table and column, and real values.

    ``session_policy.yaml`` (``default_scope``: the scope a ranking question gets
    when it names none, and the options its clarification offers) and
    ``memory_policy.yaml`` (the closed set of values an analyst may pin) are
    among the ten required files, and no setup tool wrote them: on a first
    install they are the shipped template, whose options are made-up samples.
    The application still starts and memory still "works", but every real value
    an analyst tries to pin is outside the closed set and refused, and the
    clarification offers values the warehouse does not have.

    FAILs, naming the file and the key (never a value), when a ``filter_key``
    names no table of ``schema.yaml`` (resolved as the application resolves a
    configured table name: a key, or a bare name), when a memory key's
    ``column`` is not a column of that table, or when the options of a file are
    exactly the sample values of the shipped ``project_config.example/`` (a set
    that differs by one value is not flagged). The detail points to
    ``python scripts/draft_policies.py``. The template directory itself is not
    compared with itself, so the shipped example passes its own preflight.

    The PASS detail may carry advice (it then starts ``advice:``): when the
    application has cached the dimension's values (the admin panel's card, in
    the running server; the command line has no cache), the number of options
    that are not among them, as counts only. File reads only. SKIP when
    ``schema.yaml`` or a policy file does not load.
    """
    from core import config_doctor as doctor
    from core import policy_values as values

    name = "Session and memory policies use real values"
    directory = _project_config_dir()
    view = doctor.read_schema(directory)
    if not view.parsed:
        return CheckResult(name, "SKIP", "schema.yaml did not load -- `project_config/ loads` says why")
    try:
        policies = values.read_policies(directory)
    except ValueError as exc:
        return CheckResult(name, "SKIP", f"{exc} -- `project_config/ loads` says why")
    shipped = None if values.is_shipped_directory(directory) else values.shipped_policies()
    findings = values.policy_findings(policies, view, shipped)
    if findings.problems:
        return CheckResult(
            name, "FAIL",
            "; ".join(findings.problems) + " -- an analyst can pin only the options of memory_policy.yaml, and a "
            "ranking question that names no scope is offered the options of session_policy.yaml: with sample "
            "values, or a table the warehouse does not have, every real value is refused and the clarification "
            "offers values that do not exist. Draft the real ones with `python scripts/draft_policies.py` "
            "(it reads the distinct values of one small dimension and writes project_config_draft/), read "
            f"them, and copy the two files into project_config/ ({_RUNBOOK_POLICIES})",
        )
    detail = f"{len(findings.sites)} place(s) name a table and column of schema.yaml and none holds the template's sample values"
    gaps = values.vocabulary_gaps(policies, view, _cached_dimension_values)
    if gaps:
        shown = _listed([
            f"{gap.site.label}: {gap.missing} of {gap.options} option(s) not among the {gap.cached} cached "
            f"value(s) of {gap.table}.{gap.column}"
            for gap in gaps
        ])
        detail += (
            f"; advice: {shown} -- such an option matches no row; draft the options again with "
            f"`python scripts/draft_policies.py` ({_RUNBOOK_POLICIES})"
        )
    return CheckResult(name, "PASS", detail)


def check_filter_value_check_has_vocabularies() -> CheckResult:
    """Advise which dimension tables the filter value check cannot help with.

    Only listed while ``FILTER_VALUE_CHECK`` is on
    (:attr:`config.Settings.filter_value_check`). The check compares a string
    literal in a statement the model wrote with the cached value set of the column
    it is compared to, and those sets exist only for a table's
    ``prefetchable_columns``. A dimension table (one ``entities.yaml`` names)
    that has label-like columns (``Name``, ``Title``, ``Kind``, ``Status``,
    ``Type`` ..., not a person's name) and lists no ``prefetchable_columns``
    gets no protection: an invented value in it still runs. This never fails:
    the PASS detail starts with ``advice:`` and lists table and column names
    only, never a value. Add ``prefetchable_columns`` to the small ones (the
    prefetch reads at most ``DEFAULT_TOP_N`` distinct values and a column that
    long is not checked) and leave out tables of people or anything large.
    File reads only. SKIP when ``schema.yaml`` or ``entities.yaml`` cannot be
    read.
    """
    from core import config_doctor as doctor

    name = "Filter value check has vocabularies"
    directory = _project_config_dir()
    view = doctor.read_schema(directory)
    if not view.parsed:
        return CheckResult(name, "SKIP", "schema.yaml did not load -- `project_config/ loads` says why")
    entities = doctor.read_entities(directory)
    if entities is None:
        return CheckResult(name, "SKIP", "entities.yaml was not found or is not a mapping -- `project_config/ loads` says why")
    coverage = doctor.vocabulary_coverage(view, entities)
    if not coverage.uncovered:
        return CheckResult(
            name, "PASS",
            f"{coverage.covered} dimension table(s) list prefetchable_columns; "
            "no other has a label-like column the check could use",
        )
    shown = _listed([f"{key} ({', '.join(columns)})" for key, columns in coverage.uncovered])
    return CheckResult(
        name, "PASS",
        f"advice: {len(coverage.uncovered)} dimension table(s) have label-like columns and no "
        f"prefetchable_columns, so an invented value in them is not caught: {shown} -- add "
        "prefetchable_columns to the small ones in schema.yaml (a few hundred distinct values at "
        "most) and leave out large tables and tables of people "
        f"({_RUNBOOK_VALUE_CHECK})",
    )


def check_every_data_source_is_described() -> CheckResult:
    """Every data source of ``datasources.yaml`` must have tables in ``schema.yaml``.

    A source with no table is a database the assistant can never be asked
    about, and the usual cause is a replaced ``schema.yaml`` that was drafted
    from one database of two. A table belongs to the source its ``datasource:``
    line names (every one of them for a list) and to the default source when it
    has none; without ``datasources.yaml`` the one source is ``default`` and
    every table is in it. FAIL names each source without a table, and the case
    of no table at all. File reads only. SKIP when ``schema.yaml`` or
    ``datasources.yaml`` does not load (their own checks say why).
    """
    from core import config_doctor as doctor

    name = "Every data source is described"
    directory = _project_config_dir()
    view = doctor.read_schema(directory)
    if not view.parsed:
        return CheckResult(name, "SKIP", "schema.yaml did not load -- `project_config/ loads` says why")
    try:
        from database.datasources import DEFAULT_DATASOURCE, validate_datasources_yaml_text

        path = directory / "datasources.yaml"
        if path.is_file():
            config = validate_datasources_yaml_text(path.read_text(encoding="utf-8-sig"))
            sources, default = tuple(config.datasources), config.default_name
        else:
            sources, default = (DEFAULT_DATASOURCE,), DEFAULT_DATASOURCE
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "SKIP", f"datasources.yaml did not load ({type(exc).__name__}) -- `Tables map to data sources` says why")
    if not view.tables:
        return CheckResult(name, "FAIL", "schema.yaml describes no table")
    missing = doctor.sources_without_tables(view, sources, default)
    if missing:
        return CheckResult(
            name, "FAIL",
            f"{len(missing)} of {len(sources)} data source(s) have no table in schema.yaml: "
            f"{', '.join(missing)} -- either its tables are listed without a `datasource:` line "
            "(they then count toward the default source, and queries never reach this one: run "
            "`python scripts/sync_schema.py`, which sets the lines), or a whole database is missing "
            "from the configuration (restore the curated schema.yaml, or add its tables with "
            f"`python scripts/sync_schema.py --add-tables 'Schema.*'`) ({_RUNBOOK_CURATION})",
        )
    return CheckResult(
        name, "PASS",
        f"all {len(sources)} data source(s) have tables ({len(view.tables)} table(s) in schema.yaml)",
    )


def check_no_staging_tables() -> CheckResult:
    """Advise when ``schema.yaml`` describes staging or change-capture tables.

    Staging schemas (``*_Stg``) and the ``<Table>_Inserts`` / ``_Updates`` /
    ``_Deletes`` tables of a change-capture process copy the tables analysts
    ask about, so with them in the file the model sees the same name several
    times and the prompt budget goes to tables nobody should query. A
    deployment may have a reason for one of them, so this never fails: the
    PASS detail starts with ``advice:`` and lists the tables per schema, never
    a value. The patterns are the ``STAGING_PATTERNS`` setting
    (:attr:`config.Settings.staging_patterns`); empty switches the check off.
    A schema in a source's ``keep_staging_schemas:`` is not counted for the
    schema patterns (the operator chose to keep it); its ``table:`` matches
    still are. File reads only. SKIP when ``schema.yaml`` does not load.
    """
    from core import config_doctor as doctor

    name = "No staging or change-capture tables"
    patterns = doctor.parse_staging_patterns(cfg.settings.staging_patterns)
    if not patterns:
        return CheckResult(name, "SKIP", "STAGING_PATTERNS is empty -- the check is switched off")
    view = doctor.read_schema(_project_config_dir())
    if not view.parsed:
        return CheckResult(name, "SKIP", "schema.yaml did not load -- `project_config/ loads` says why")
    keep: dict[str, tuple[str, ...]] = {}
    default = ""
    try:
        from database.datasources import datasource_keep_staging_schemas, default_datasource_name

        keep = datasource_keep_staging_schemas()
        default = default_datasource_name()
    except Exception:  # noqa: BLE001 - an unreadable datasources.yaml is another check's finding
        keep = {}
    counts = doctor.staging_tables(view, patterns, keep, default)
    if not counts:
        return CheckResult(name, "PASS", f"none of the {len(view.tables)} table(s) matches STAGING_PATTERNS")
    total = sum(counts.values())
    per_schema = _listed([
        f"{schema or '(no schema)'}: {n}"
        for schema, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ])
    return CheckResult(
        name, "PASS",
        f"advice: {total} of {len(view.tables)} table(s) look like staging or change-capture tables "
        f"({per_schema}) -- they repeat the tables analysts ask about; remove them from schema.yaml "
        "unless analysts need them (STAGING_PATTERNS sets what counts)",
    )


def check_no_sample_values_in_descriptions() -> CheckResult:
    """Advise when descriptions quote what look like live sample values.

    The inspector writes a column note as ``<type> column; e.g. a, b, c`` with
    values read from live rows, and the note goes into the prompt sent to the
    model: names, phone numbers, addresses and national ids of real people.
    Looks at the text after each ``e.g.`` in a table description or column
    note for a digit run of seven or more (phone-like), of exactly ten
    (national-id-like), or a long segment with digits (address-like); dates
    and plain words are left alone. Advice only (the PASS detail starts with
    ``advice:``), because the match is a heuristic. The detail has counts and
    table keys, never a value. File reads only. SKIP when ``schema.yaml``
    does not load.
    """
    from core import config_doctor as doctor

    name = "No live sample values in descriptions"
    view = doctor.read_schema(_project_config_dir())
    if not view.parsed:
        return CheckResult(name, "SKIP", "schema.yaml did not load -- `project_config/ loads` says why")
    found = doctor.sample_value_notes(view)
    if not found.notes:
        return CheckResult(name, "PASS", "no description quotes phone-, id- or address-like sample values")
    return CheckResult(
        name, "PASS",
        f"advice: {found.notes} note(s) in {len(found.tables)} table(s) seem to quote live sample values "
        f"(phone-like: {found.phone_like}, national-id-like: {found.national_id_like}, "
        f"address-like: {found.address_like}); tables: {_listed(found.tables)} -- the notes are sent "
        "to the model; replace each `e.g.` list with a description of the column",
    )


def check_no_table_in_excluded_schema() -> CheckResult:
    """Advise when ``schema.yaml`` lists a table the setup tools do not scan.

    The exclusion (:mod:`database.schema_filter`: the system schemas, the
    ``EXCLUDE_SCHEMAS`` setting and a source's ``exclude_schemas:``, and the
    ``EXCLUDE_TABLES`` setting and a source's ``exclude_tables:``) decides
    which database tables the inspector drafts and ``sync_schema.py`` proposes;
    it never removes a table ``schema.yaml`` already lists. A listed table whose
    schema every one of its sources excludes, or that table patterns name in
    every one of its sources, is a contradiction: either the exclusion was
    added after the table, or the table was copied in from a draft. This never
    fails (the PASS detail starts with ``advice:``); it counts the tables,
    lists the first keys and names the two fixes: remove the table, or narrow
    the exclusion. SKIP when no exclusion is configured beyond the system
    schemas and no table matches, or when ``schema.yaml`` or
    ``datasources.yaml`` does not load. File reads only.
    """
    from core import config_doctor as doctor
    from database.datasources import (
        datasource_exclude_schemas,
        datasource_exclude_tables,
        datasource_names,
        default_datasource_name,
    )
    from database.schema_filter import exclusion_patterns, table_exclusion_patterns

    name = "No table in an excluded schema"
    view = doctor.read_schema(_project_config_dir())
    if not view.parsed:
        return CheckResult(name, "SKIP", "schema.yaml did not load -- `project_config/ loads` says why")
    try:
        sources = datasource_names()
        default = default_datasource_name()
        patterns = {source: exclusion_patterns(source) for source in sources}
        table_patterns = {source: table_exclusion_patterns(source) for source in sources}
        configured = bool(cfg.settings.exclude_schemas.strip()) or any(datasource_exclude_schemas().values())
        tables_configured = bool(cfg.settings.exclude_tables.strip()) or any(datasource_exclude_tables().values())
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "SKIP", f"datasources.yaml did not load ({type(exc).__name__}) -- `Tables map to data sources` says why")
    found = doctor.tables_in_excluded_schemas(view, patterns, default)
    named = [t for t in doctor.tables_matching_excluded_tables(view, table_patterns, default) if t not in found]
    found = [*found, *named]
    if not found:
        if not (configured or tables_configured):
            return CheckResult(name, "SKIP", "no schema is excluded beyond the system schemas (EXCLUDE_SCHEMAS and exclude_schemas: are empty)")
        tail = " or named by an excluded table pattern" if tables_configured else ""
        return CheckResult(name, "PASS", f"none of the {len(view.tables)} table(s) is in an excluded schema{tail}")
    scope = "in a schema the setup tools are told not to scan"
    fixes = "(EXCLUDE_SCHEMAS in .env, exclude_schemas: in datasources.yaml)"
    if named:
        scope += ", or named by a table pattern"
        fixes = "(EXCLUDE_SCHEMAS / EXCLUDE_TABLES in .env, exclude_schemas: / exclude_tables: in datasources.yaml)"
    return CheckResult(
        name, "PASS",
        f"advice: {len(found)} of {len(view.tables)} table(s) are {scope}: "
        f"{_listed([t.key for t in found])} -- remove them from schema.yaml, or narrow the exclusion "
        f"{fixes}. "
        "The exclusion only limits what the inspector and sync_schema.py propose; these tables are still synced",
    )


def _key_ids(ids: list[str]) -> str:
    """``"a, b"``, or ``"a, b, c and 4 more"`` when there are many."""
    shown = ids[:3]
    return ", ".join(shown) + (f" and {len(ids) - 3} more" if len(ids) > 3 else "")


def check_column_restrictions_name_real_columns() -> CheckResult:
    """Every plain ``denied_columns`` entry names a column of ``schema.yaml``.

    A plain entry (no ``.`` and no ``:``) is compared with column names,
    case-insensitively, by the SQL guard. One that matches no column of any
    table denies nothing, and nothing in the server says so: a typo, a name
    with an inner space, a column that was renamed. Scoped entries
    (``Table.Column``, ``Source:Table.Column``) are checked when the key is
    loaded and stop the server if wrong, so they are not looked at again here.
    Entries are read after normalisation (whitespace and invisible Unicode
    characters removed at either end), as the guard reads them. An entry
    matches when it, or it without any format character inside it, is a column
    name (a Persian column name may contain a zero-width non-joiner).

    FAILs, naming each key id and the entry (written with :func:`ascii`, so an
    invisible character shows), when any plain entry matches no column. The
    server still starts: that is a choice of 6.15.1, so an installation that
    works today keeps working, and this check is how the entries are found.

    When every entry matches, the PASS detail may carry advice (it then starts
    ``advice:``): for each plain denied name, the columns that probably hold
    the same data and are not denied, because the same personal number lives
    under many names in a warehouse. The test is the spelling
    (:func:`security.column_policy.look_alike_columns`), so it never fails.

    The keys are the merged set the server uses
    (:func:`appdb.key_store.get_active_principals`), falling back to the
    environment keys when the application database cannot be read. SKIP when
    there is no key, when the key configuration is invalid (``API key
    authentication`` says why), or when ``schema.yaml`` does not load or lists
    no column.
    """
    from core import config_doctor as doctor
    from security.auth import ApiKeyConfigError, api_keys_source, load_api_keys
    from security.column_policy import look_alike_columns, unmatched_bare_entries

    name = "Column restrictions name real columns"
    try:
        keys = load_api_keys()
    except ApiKeyConfigError:
        return CheckResult(name, "SKIP", "the API key configuration is invalid -- `API key authentication` says why")
    try:
        from appdb.key_store import get_active_principals

        keys = get_active_principals()
    except Exception:  # noqa: BLE001 - an unreadable application database degrades to the environment keys
        pass
    if not keys:
        return CheckResult(
            name, "SKIP",
            "no API key is configured (API_KEYS_FILE, API_KEYS_JSON, application database)",
        )
    view = doctor.read_schema(_project_config_dir())
    if not view.parsed:
        return CheckResult(name, "SKIP", "schema.yaml did not load -- `project_config/ loads` says why")
    columns = [column for table in view.tables for column in table.columns]
    if not columns:
        return CheckResult(name, "SKIP", "schema.yaml lists no column")

    principals: dict[str, tuple[str, ...]] = {}
    for principal in keys.values():
        principals[principal.id] = tuple(dict.fromkeys([*principals.get(principal.id, ()), *principal.denied_columns]))

    unmatched_by_id = {
        pid: unmatched_bare_entries(entries, columns) for pid, entries in sorted(principals.items())
    }
    unmatched_by_id = {pid: found for pid, found in unmatched_by_id.items() if found}
    plain = sum(
        1 for entries in principals.values() for entry in entries
        if isinstance(entry, str) and ":" not in entry and "." not in entry
    )
    if unmatched_by_id:
        groups: dict[tuple[str, ...], list[str]] = {}
        for pid, found in unmatched_by_id.items():
            groups.setdefault(found, []).append(pid)
        total = sum(len(found) for found in unmatched_by_id.values())
        listing = "; ".join(
            f"{_key_ids(ids)}: {', '.join(ascii(entry) for entry in found)}"
            for found, ids in groups.items()
        )
        return CheckResult(
            name, "FAIL",
            f"{total} plain denied_columns entr{'y' if total == 1 else 'ies'} of "
            f"{len(unmatched_by_id)} key(s) match no column of schema.yaml, so they deny nothing -- "
            f"{listing} -- correct the spelling in {api_keys_source()} (or "
            "`PATCH /admin/keys/<key>/acl` for a key of the admin panel); the match ignores case "
            "but not inner spaces. The server starts anyway; the entry restricts nothing until it is fixed "
            f"({_RUNBOOK_COLUMN_RESTRICTIONS})",
        )

    alike_by_id = {pid: look_alike_columns(entries, columns) for pid, entries in sorted(principals.items())}
    alike_by_id = {pid: found for pid, found in alike_by_id.items() if found}
    if not alike_by_id:
        if not plain:
            return CheckResult(name, "PASS", f"no key of the {len(principals)} has a plain denied_columns entry to check")
        return CheckResult(
            name, "PASS",
            f"all {plain} plain denied_columns entr{'y' if plain == 1 else 'ies'} of "
            f"{len(principals)} key(s) name a column of schema.yaml",
        )
    groups_alike: dict[tuple[tuple[str, tuple[str, ...]], ...], list[str]] = {}
    for pid, found in alike_by_id.items():
        groups_alike.setdefault(tuple(sorted(found.items())), []).append(pid)
    parts = []
    for found, ids in list(groups_alike.items())[:3]:
        shown = []
        for entry, names in found[:3]:
            more = f", +{len(names) - 5} more" if len(names) > 5 else ""
            shown.append(f"{entry} -> {len(names)}: {', '.join(names[:5])}{more}")
        if len(found) > 3:
            shown.append(f"+{len(found) - 3} more name(s)")
        parts.append(f"{_key_ids(ids)}: {'; '.join(shown)}")
    if len(groups_alike) > 3:
        parts.append(f"+{len(groups_alike) - 3} more group(s) of keys")
    return CheckResult(
        name, "PASS",
        f"advice: all {plain} plain denied_columns entries name a column, but {len(alike_by_id)} key(s) deny a "
        "name while columns that look like it (it appears inside their name, or the same stem ends in "
        "ID, Code, No, Num or Number) are not denied -- the same data often lives under several names; "
        f"check each and add the ones that hold it: {' | '.join(parts)} ({_RUNBOOK_COLUMN_RESTRICTIONS})",
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


def check_web_ui_origin_allowed() -> CheckResult:
    """The address the web UI is opened with must be in ``CORS_ALLOWED_ORIGINS``.

    A browser page whose origin is not allowed gets its preflight answered
    ``400`` and reports only "Failed to fetch": every light red, while this
    script, the CLI and a ``curl`` all pass (the 6.14.0 incident: the list
    held ``http://172.16.101.42:8077`` and the page was opened as
    ``http://localhost:8077``). Nothing else here can see that, because none
    of them is a browser and none sends an ``Origin``.

    With ``VERIFY_UI_ORIGIN`` set (the address as typed in the browser, e.g.
    ``http://172.16.101.42:8077``; a trailing slash or path is ignored) the
    check PASSes when that origin is in the list and FAILs otherwise, printing
    the ``.env`` line that adds it. Without it nothing can be proved, so the
    check only advises (a PASS whose detail starts ``advice:``), in the two
    shapes that produced the incident: every allowed origin is loopback while
    ``API_HOST`` makes the API reachable from other machines, or the list has
    a single non-loopback origin so the same page opened any other way (the
    host name, ``localhost``) is refused. It sees ``API_HOST`` only, not a
    ``--host`` given to ``uvicorn`` on the command line. Settings only: no
    network, no database.
    """
    name = "Web UI origin is allowed by CORS"
    allowed = list(cfg.settings.cors_allowed_origins)
    shown_list = ", ".join(allowed) or "(none)"
    wildcard = "*" in allowed

    raw = os.environ.get("VERIFY_UI_ORIGIN", "").strip()
    if raw:
        origin = parse_origin(raw)
        if origin is None:
            return CheckResult(
                name, "FAIL",
                f"VERIFY_UI_ORIGIN={clean_origin(raw, 80)!r} is not an address -- write it as scheme://host[:port], "
                "exactly as the browser shows it (e.g. http://172.16.101.42:8077)",
            )
        if wildcard or origin in allowed:
            return CheckResult(name, "PASS", f"{origin} is allowed (CORS_ALLOWED_ORIGINS={','.join(allowed)})")
        return CheckResult(
            name, "FAIL",
            f"{origin} is not in CORS_ALLOWED_ORIGINS ({shown_list}): a page opened at that address "
            f"gets 'Failed to fetch' and every light shows red. Set in .env and restart the API: "
            f"{env_line(allowed, origin)} -- or open the page with exactly an allowed address "
            "(scheme, host and port)",
        )

    how_to_check = "set VERIFY_UI_ORIGIN=<the address in the browser's address bar> to check it"
    if wildcard:
        return CheckResult(name, "PASS", f"any origin is allowed (CORS_ALLOWED_ORIGINS={','.join(allowed)})")
    api_host = cfg.settings.api_host.strip()
    api_host_url = f"http://[{api_host}]" if ":" in api_host else f"http://{api_host}"
    reachable_elsewhere = bool(api_host) and not is_loopback_origin(api_host_url)
    if allowed and all(is_loopback_origin(o) for o in allowed) and reachable_elsewhere:
        return CheckResult(
            name, "PASS",
            f"advice: every allowed origin is loopback ({shown_list}) but API_HOST={api_host} makes the API "
            "reachable from other machines; a page opened there by IP address or host name is refused. "
            "List every address the page is opened with (IP address, host name, localhost) in "
            f"CORS_ALLOWED_ORIGINS, and {how_to_check}",
        )
    if len(allowed) == 1 and not is_loopback_origin(allowed[0]):
        return CheckResult(
            name, "PASS",
            f"advice: only {allowed[0]} is allowed; the same page opened any other way (the host name instead "
            "of the IP address, localhost, another port) is refused. List every address the page is opened "
            f"with (IP address, host name, localhost) in CORS_ALLOWED_ORIGINS, and {how_to_check}",
        )
    return CheckResult(name, "PASS", f"{len(allowed)} origin(s) allowed: {shown_list}; {how_to_check}")


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
    sync would change the file in a way that means it is out of step -- a
    ``datasource:`` missing or wrong, a column the database has that
    ``schema.yaml`` lacks, a recorded column type that differs, a column or
    table newly missing from the database -- naming the counts and the
    command that writes the proposed file
    (:func:`schema_data.sync.describe_drift`; when the file differs in a way
    none of the counts covers, it says so and points at ``--dry-run``, never
    a row of zeros). Columns already marked ``# not in database`` are
    reported in the detail, not failed.

    A column type that is merely not recorded yet is not drift: a
    ``schema.yaml`` written before 6.9.0 has no ``column_types:`` map, so
    every column lands in the sync's ``recorded`` list although nothing in
    the databases changed. When recording types is the only thing a sync
    would do (:attr:`schema_data.sync.SyncResult.only_records_types`) the
    check PASSes and the detail says how many types are not recorded and that
    ``python scripts/sync_schema.py`` records them. Real drift together with
    unrecorded types FAILs, and the detail adds ``N column type(s) to
    record``. Reads three ``INFORMATION_SCHEMA`` views per data source and
    nothing else.

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
        from schema_data.sync import describe_drift, sync_schema_text, unrecorded_types_note

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
    if not result.changed or result.only_records_types:
        detail = "schema.yaml matches the databases"
        if stale or any(t.nowhere for t in plan.tables):
            detail += (
                f" ({stale} column(s) and {sum(t.nowhere for t in plan.tables)} table(s) marked as "
                "not in the database -- `python scripts/sync_schema.py --prune` removes them)"
            )
        if result.only_records_types:
            detail += f"; {unrecorded_types_note(result)}"
        return CheckResult(name, "PASS", detail)
    return CheckResult(name, "FAIL", describe_drift(result))


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
    check_model_answers_each_prompt,
    check_api_key_authenticates,
    check_column_restrictions_name_real_columns,
    check_audit_log_writable,
    check_session_store_writable,
    check_project_config_loads,
    check_project_config_is_curated,
    check_aliases_are_curated,
    check_every_data_source_is_described,
    check_no_staging_tables,
    check_no_sample_values_in_descriptions,
    check_no_table_in_excluded_schema,
    check_aliases_hold_no_raw_values,
    check_entities_match_schema_keys,
    check_policies_use_real_values,
    check_rate_limit_sane_for_deployment,
    check_web_ui_origin_allowed,
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
    optional = [check_filter_value_check_has_vocabularies] if cfg.settings.filter_value_check else []
    return [check_settings_valid, check_table_datasources, *per_source, *_GLOBAL_CHECKS, *optional]


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
