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
from api.auth import require_admin
from security.auth import Principal

router = APIRouter(prefix="/admin", tags=["admin"])


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
    return build_report(records, include_examples=include_examples)


# ---------------------------------------------------------------------------
# GET /admin/health/checks
# ---------------------------------------------------------------------------

@router.get(
    "/health/checks",
    summary="Run scripts.verify_deployment's checks now",
)
def admin_health_checks(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Every check ``python scripts/verify_deployment.py`` runs, executed now
    against this running deployment — no analysis is reimplemented here.
    Every one of those checks is already safe to run against a live system
    (see that module's own "Safety" docstring section: read-only, with the
    one deliberate write attempt always rolled back) — this route adds no
    new risk by calling them from a request instead of a shell.
    """
    from scripts.verify_deployment import _CHECKS, CheckResult

    results: list[CheckResult] = []
    for check in _CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # noqa: BLE001 - a check must never crash this route
            results.append(
                CheckResult(check.__name__, "FAIL", f"check raised unexpectedly: {exc}")
            )

    return {
        "checks": [
            {"name": r.name, "status": r.status, "detail": r.detail} for r in results
        ],
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
