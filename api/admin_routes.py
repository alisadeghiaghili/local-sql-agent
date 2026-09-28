# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``GET /admin/*`` — read-only operator observability, phase 1.

``docs/admin-panel-architecture.md`` is the full design contract; this
module implements only its deliberately small first slice (see
``docs/admin-panel-phase1`` — the frozen spec this module was built
against): surfacing analysis that already exists, with no write path of
any kind.

Every route here:

* requires :func:`api.auth.require_admin` — a 403
  (:class:`~api.errors.AdminRequiredError`) for any principal that is not
  an admin, including :data:`~security.auth.ANONYMOUS` (see that
  dependency's own docstring for why the ``AUTH_REQUIRED=false`` escape
  hatch cannot confer it);
* is a ``GET`` — no route under this router ever mutates anything. That
  is not merely a convention followed here: ``tests/test_admin.py``
  enumerates the live route table and asserts every ``/admin/*`` method is
  ``GET``, so a future ``POST`` added under this prefix without updating
  that test fails the build, not just a review.

No result rows ever appear in a response from this router —
``observability/audit.py``'s structural "column names, never row values"
rule is inherited unchanged, and none of the four endpoints below reads a
row of warehouse data in the first place.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

import config as cfg
from api.admin_result_cache import admin_expensive_cache
from api.auth import require_admin
from security.auth import Principal

router = APIRouter(prefix="/admin", tags=["admin"])

#: scripts.verify_deployment checks this admin-panel route never runs
#: automatically -- Required item 2, 2026 warehouse-load audit.
#: ``check_login_is_read_only`` attempts a real (always-rolled-back)
#: ``CREATE TABLE`` against the warehouse; ``check_query_timeout`` runs a
#: ``WAITFOR DELAY`` probe that deliberately blocks for several seconds.
#: Both are safe to run deliberately (that is exactly what
#: ``python -m scripts.verify_deployment`` -- the CLI, unaffected by this
#: set -- is for) but neither belongs in a check this panel would ever
#: run on a timer or on every page load; see ``admin_health_checks``'s
#: own ``deep`` parameter below.
_DEEP_CHECK_NAMES = frozenset({"check_login_is_read_only", "check_query_timeout"})


# ---------------------------------------------------------------------------
# GET /admin/summary
# ---------------------------------------------------------------------------

@router.get(
    "/summary",
    summary="Aggregate audit-log report (scripts.analyze_audit_log.build_report)",
)
def admin_summary(
    include_examples: bool = Query(
        False,
        description=(
            "Opt-in escape hatch -- attaches a small number of verbatim "
            "example questions/error messages. See "
            "scripts/analyze_audit_log.py's module docstring ('Two modes') "
            "before ever setting this true on a report that will leave the "
            "server. Defaults to the aggregate-safe mode."
        ),
    ),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """The same report ``python scripts/analyze_audit_log.py --json`` prints,
    computed by that module's own :func:`~scripts.analyze_audit_log.build_report`
    — no analysis is reimplemented here. Defaults to the aggregate-safe mode
    (``include_examples=False``); the response's own ``mode`` field always
    says which mode produced it (``"aggregate_safe"`` or
    ``"aggregate_with_examples"``), per that function's own contract.
    """
    from observability.audit import audit_write_failures
    from scripts.analyze_audit_log import build_report, iter_records, resolve_log_paths

    # Mirrors scripts/analyze_audit_log.py's own default glob (the active
    # log plus any rotated `.1`, `.2`, ... backups) but rooted at
    # cfg.settings.log_dir rather than a hardcoded "logs/" -- this module
    # runs inside the long-lived server process, which already has an
    # authoritative log directory setting (the same one
    # scripts/verify_deployment.py's check_audit_log_writable checks),
    # unlike the standalone script's own "run from the repo root" convention.
    paths = resolve_log_paths([f"{cfg.settings.log_dir}/audit_log.jsonl*"])
    records = list(iter_records(paths))
    report = build_report(records, include_examples=include_examples)
    # Finding 4 (2026 audit): a full disk, a permissions change, or
    # someone making the log unwritable used to leave this report's
    # numbers looking normal -- every one of them is *derived from the
    # log*, so a query that ran but never got recorded is invisible to
    # every field above. audit_write_failures() is the one counter here
    # that is NOT read from the log itself, precisely so a broken audit
    # trail shows up even when the log it would have shown up in cannot
    # be written. See observability/audit.py's "Observable failure"
    # section.
    report["audit_write_failures"] = audit_write_failures()
    return report


# ---------------------------------------------------------------------------
# GET /admin/health/checks
# ---------------------------------------------------------------------------

@router.get(
    "/health/checks",
    summary="Run scripts.verify_deployment's checks now (cached)",
)
def admin_health_checks(
    refresh: bool = Query(
        False,
        description=(
            "Bypass this route's own cached result and run the checks now, "
            "regardless of ADMIN_EXPENSIVE_CACHE_TTL_SECONDS."
        ),
    ),
    deep: bool = Query(
        False,
        description=(
            "Also run check_login_is_read_only (a rolled-back CREATE TABLE "
            "DDL attempt) and check_query_timeout (a WAITFOR DELAY probe). "
            "Both are skipped by default from this panel route -- see the "
            "route's own docstring -- and always run from the CLI "
            "(python -m scripts.verify_deployment) regardless of this flag."
        ),
    ),
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Every check ``python scripts/verify_deployment.py`` runs, executed now
    against this running deployment — no analysis is reimplemented here.
    Every one of those checks is already safe to run against a live system
    (see that module's own "Safety" docstring section: read-only, with the
    one deliberate write attempt always rolled back) — this route adds no
    new risk by calling them from a request instead of a shell.

    Required item 1/2, 2026 warehouse-load audit
    ---------------------------------------------
    Two changes on top of "just run every check": the result is cached for
    :attr:`config.Settings.admin_expensive_cache_ttl_seconds` (``refresh=1``
    forces a fresh run; the panel's own auto-refresh no longer calls this
    route at all -- see ``web/admin/main.js``), and ``check_login_is_read_only``
    /``check_query_timeout`` (see :data:`_DEEP_CHECK_NAMES`) are skipped
    unless ``deep=1`` is explicitly passed -- a DDL attempt and a multi-
    second WAITFOR probe have no business running on a timer or on every
    page load, only when an operator deliberately asks for them.
    """
    from scripts.verify_deployment import CheckResult, build_checks

    all_checks = build_checks()
    checks_to_run = (
        all_checks if deep else [c for c in all_checks if c.__name__ not in _DEEP_CHECK_NAMES]
    )

    def _run_checks() -> list[CheckResult]:
        results: list[CheckResult] = []
        for check in checks_to_run:
            try:
                results.append(check())
            except Exception as exc:  # noqa: BLE001 - a check must never crash this route
                results.append(
                    CheckResult(check.__name__, "FAIL", f"check raised unexpectedly: {exc}")
                )
        return results

    # Deep and non-deep results are cached separately -- a plain refresh
    # must never serve a stale answer that happens to have come from a
    # deep run, or vice versa.
    cache_key = "health_checks_deep" if deep else "health_checks"
    cached = admin_expensive_cache.get_or_compute(
        cache_key, cfg.settings.admin_expensive_cache_ttl_seconds, _run_checks, force=refresh,
    )
    results: list[CheckResult] = cached.value

    return {
        "checks": [
            {"name": r.name, "status": r.status, "detail": r.detail} for r in results
        ],
        "deep": deep,
        "cache": {
            "cached": cached.cached,
            "age_seconds": round(cached.age_seconds, 1),
            "ttl_seconds": cached.ttl_seconds,
        },
    }


# ---------------------------------------------------------------------------
# GET /admin/cache
# ---------------------------------------------------------------------------

@router.get(
    "/cache",
    summary="Current query-result cache statistics",
)
def admin_cache(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """The same snapshot ``GET /cache/stats`` returns
    (:meth:`api.query_cache.QueryCache.stats`) — surfaced here too so the
    panel needs only the admin capability, not a second key issued for the
    analyst-facing cache endpoint.
    """
    from api.query_cache import query_cache

    return query_cache.stats()


# ---------------------------------------------------------------------------
# GET /admin/config
# ---------------------------------------------------------------------------

#: One entry per ``project_config/`` file this deployment loads at
#: runtime: its filename, the loader function that parses it, and a
#: function that counts its meaningful entries from the loaded, validated
#: model. Counting is not analysis -- it is exactly the same "did this
#: file load, and how big is it" question
#: ``scripts/verify_deployment.py``'s ``check_project_config_loads``
#: already asks (that check does not itself return counts, only
#: pass/fail; this reuses its loaders and adds the count each of them
#: already implies once the file has loaded).
def _aliases_count(parsed: Any) -> int:
    return len(parsed.ring_aliases) + len(parsed.synonyms)


def _entities_count(parsed: Any) -> int:
    return len(parsed.entities)


def _business_rules_count(parsed: Any) -> int:
    return len(parsed.rules)


def _examples_count(parsed: Any) -> int:
    return len(parsed.examples)


def _metrics_count(parsed: Any) -> int:
    return len(parsed.metrics)


def _schema_count(parsed: Any) -> int:
    # The table count, not table+relationship: this is the number that
    # matters operationally -- it is the SQL guard's table allowlist size
    # (schema_data.registry.get_table_columns).
    return len(parsed.tables)


def _retrieval_hints_count(parsed: Any) -> int:
    return len(parsed.fact_tables)


def _session_policy_count(parsed: Any) -> int:
    # session_policy.yaml describes exactly one policy record (the default
    # scope), not a collection -- "1" means "loaded", not "empty".
    return 1


def _memory_policy_count(parsed: Any) -> int:
    return len(parsed.keys)


def _config_loaders() -> list[tuple[str, Callable[[], Any], Callable[[Any], int]]]:
    from knowledge.config_loader import (
        load_aliases,
        load_business_rules,
        load_entities,
        load_examples,
        load_memory_policy,
        load_metrics,
        load_retrieval_hints,
        load_session_policy,
    )
    from schema_data.registry import load_schema

    return [
        ("aliases.yaml", load_aliases, _aliases_count),
        ("entities.yaml", load_entities, _entities_count),
        ("business_rules.yaml", load_business_rules, _business_rules_count),
        ("examples.yaml", load_examples, _examples_count),
        ("metrics.yaml", load_metrics, _metrics_count),
        ("schema.yaml", load_schema, _schema_count),
        ("retrieval_hints.yaml", load_retrieval_hints, _retrieval_hints_count),
        ("session_policy.yaml", load_session_policy, _session_policy_count),
        ("memory_policy.yaml", load_memory_policy, _memory_policy_count),
    ]


@router.get(
    "/config",
    summary="Which project_config/ files loaded, and how many entries each yielded",
)
def admin_config(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """*That* each of ``project_config/``'s nine files loaded, and how many
    entries each yielded -- never file contents. ``schema.yaml`` in
    particular is the SQL guard's table allowlist; this endpoint reports
    its size, not its text, the same "no result rows, ever" posture this
    admin surface inherits from ``observability/audit.py`` extended to
    configuration text.
    """
    from knowledge.config_loader import ConfigNotFoundError

    files: list[dict[str, Any]] = []
    for filename, loader, counter in _config_loaders():
        try:
            parsed = loader()
        except ConfigNotFoundError as exc:
            files.append({"file": filename, "loaded": False, "count": None, "error": str(exc)})
            continue
        except ValueError as exc:
            files.append({"file": filename, "loaded": False, "count": None, "error": str(exc)})
            continue
        files.append({"file": filename, "loaded": True, "count": counter(parsed), "error": None})

    return {"project_config_dir": cfg.settings.project_config_dir, "files": files}
