# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Versioned ``project_config/`` bundle -- history, diff, rollback (phase 3).

``docs/admin-panel-architecture.md`` §6 and the phase 3 spec: the nine
``project_config/*.yaml`` files (see :data:`CONFIG_FILENAMES`) become
editable, versioned, and reversible through the application database
(:mod:`appdb.engine`), the same durable store phase 2 already put the key
store and role grants in.

Shape (spec §1, §6.1)
----------------------
**Bundle-versioned, with per-file restore.** One row in
``config_bundle_versions`` (:data:`appdb.models.config_bundle_versions`) is
a full snapshot of all nine files -- never a diff, so restoring any depth
is a read, not a replay. The *active* version is always the highest
``version_id`` whose ``status`` is ``"applied"``; there is deliberately no
separate flag to keep in sync with that.

**Revert, never reset.** :func:`restore` never mutates or deletes an
earlier row -- it creates a brand-new version whose content equals the
target's, going through the exact same validate-diff-dry_run-apply path as
any other edit (:func:`propose_or_apply`). Restoring version 3 when
versions 4 and 5 already exist leaves both of them exactly where they were
and creates version 6; the restore itself is just another version, and is
therefore itself reversible.

**Propose and approve (spec §3.1).** ``schema.yaml`` is the guard's
allowlist (security admin's, per the architecture's §2). An operations
principal may still *propose* a change that touches it: this module then
creates the version as an unapplied ``"draft"`` -- carrying its diff and
dry-run results, computed once, at proposal time, so what a reviewer sees
is exactly what they are approving -- and only a principal holding the
security capability can promote it (:func:`approve_draft`) or reject it
(:func:`reject_draft`). Approval re-checks optimistic locking (below)
against the *current* active version, not the one open when the draft was
created, so a change nobody has seen yet cannot be approved out from under
a more recent edit.

Validated, every time (spec §4)
---------------------------------
Before anything is written, :func:`propose_or_apply` (1) parses and
validates every file through the real loaders in
:mod:`knowledge.config_loader` and :mod:`schema_data.registry` -- a
malformed file is rejected with the loader's own error, and the active
configuration is left completely untouched; (2) when ``schema.yaml``
changed, runs the same schema-agnostic structural-invariant check
``tests/test_schema_registry_snapshot.py``'s ``TestAllowlistStructuralInvariants``
pins (:func:`schema_data.registry.check_allowlist_structural_invariants`
-- called, not reimplemented: both this module and that test call the one
function); (3) computes a structural diff -- for ``schema.yaml``,
specifically which tables and columns enter and leave the allowlist, not a
text diff of YAML; and (4) dry-runs the golden set through the real
:mod:`eval` harness in offline mode (no LLM, no database -- see
:mod:`eval.runner`'s own docstring for what that mode does and does not
prove) and, for a ``schema.yaml`` change specifically, cross-checks every
table the golden set's own reference SQL touches
(:func:`security.sql_guard.extract_touched_tables`) against the
*candidate* allowlist -- this is what catches "a schema.yaml from five
versions ago names tables the warehouse no longer has" before it is
applied, not after.

Optimistic locking, not merging (spec §8)
--------------------------------------------
Every write here (:func:`propose_or_apply`, :func:`approve_draft`) takes
the version its caller had open (``based_on_version``) and refuses with
:class:`StaleVersionError` -- naming who changed it and when -- if the
active version has moved on since. No three-way merge is attempted
anywhere in this module.

What takes effect immediately, and what needs a restart
------------------------------------------------------------
Applying a version always writes the bundle to
``cfg.settings.project_config_dir`` (so the very next process restart
picks it up unconditionally) and, for the eight files that do not feed
``security.sql_guard``'s allowlist, also clears their in-process caches
(:mod:`knowledge`'s lazy ``knowledge.*`` loader modules, and
:mod:`prompt_engine.static_prefix`'s ``lru_cache``s when a prefix-visible
file changed) so a long-running server reflects the change on its very
next request, the same "immediate, not eventually" bar
:mod:`appdb.key_store` already holds revocation to.

``schema.yaml`` is deliberately excluded from that live-reload:
``security.sql_guard`` resolves its table/column allowlist from a
module-level binding computed once, at import time
(:data:`schema_data.columns.TABLE_COLUMNS` frozen into
``security.sql_guard``'s own namespace), and mutating that binding for one
in-flight admin request would race every concurrently-executing analyst
request validating SQL against it -- a security-critical global has no
business being swapped out from under a live server. A ``schema.yaml``
change is therefore validated and dry-run exactly as thoroughly as any
other file, written to disk immediately, and takes effect for the guard
(and the prompt's schema block, which is fed by the same frozen import) on
the next restart -- exactly the "an innocuous-looking edit ... halves
throughput with no error anywhere" risk §6 of the architecture describes
being made *visible* via the prefix version, not eliminated by an
unsafe live patch of security-critical state.

Export (spec §7)
------------------
Every applied version is also written to ``cfg.settings.config_export_dir``
when configured -- an operator pointing that at their own git repository
gets a commit per change, and an off-box backup, without this module's
correctness depending on git being installed at all (see
:func:`import_bundle_from_directory` for the other half of that round
trip).
"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import yaml
from sqlalchemy import func, select

import config as cfg
from appdb.engine import get_app_engine
from appdb.models import config_bundle_versions
from security.auth import OPERATIONS_CAPABILITY, SECURITY_CAPABILITY

#: The nine ``project_config/*.yaml`` files this module versions as one
#: bundle (spec §1; architecture §6). ``relationships.yaml`` (a separate,
#: optional file consumed by ``database/relationship_map.py``'s schema
#: auto-discovery, not by :mod:`knowledge.config_loader`) is deliberately
#: NOT one of these nine.
CONFIG_FILENAMES: tuple[str, ...] = (
    "aliases.yaml",
    "business_rules.yaml",
    "entities.yaml",
    "examples.yaml",
    "metrics.yaml",
    "retrieval_hints.yaml",
    "session_policy.yaml",
    "memory_policy.yaml",
    "schema.yaml",
)

#: The one file of the nine that is security-admin's, not operations'
#: (spec §3; architecture §2) -- it is the guard's allowlist, not merely
#: descriptive domain data.
SECURITY_ONLY_FILES: frozenset[str] = frozenset({"schema.yaml"})

#: Which of the nine files feed the byte-identical static prompt prefix
#: (:mod:`prompt_engine.static_prefix`) -- only these three warrant
#: clearing that module's ``lru_cache``s on apply. ``schema.yaml`` also
#: feeds the prefix but is excluded here on purpose -- see the module
#: docstring's "What takes effect immediately" section.
_PREFIX_RELEVANT_FILES: frozenset[str] = frozenset(
    {"business_rules.yaml", "metrics.yaml", "examples.yaml"}
)

#: filename -> the dotted module name of its ``knowledge.*`` lazy-loader
#: shim, for cache invalidation on apply. ``schema.yaml`` has no entry
#: here -- see the module docstring.
_KNOWLEDGE_CACHE_MODULES: dict[str, str] = {
    "aliases.yaml": "knowledge.aliases",
    "business_rules.yaml": "knowledge.business_rules",
    "entities.yaml": "knowledge.entities",
    "examples.yaml": "knowledge.examples",
    "metrics.yaml": "knowledge.metrics",
    "retrieval_hints.yaml": "knowledge.retrieval_hints",
    "session_policy.yaml": "knowledge.session_policy",
    "memory_policy.yaml": "knowledge.memory_policy",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConfigVersionValidationError(ValueError):
    """A candidate bundle failed to parse/validate through a real loader.

    ``str(self)`` is the *first* failing file's own error message --
    matching :func:`knowledge.config_loader._load_validated`'s own "surface
    the first error" convention -- and :attr:`errors` carries every
    failing file's message, in file order, for a caller that wants the
    complete picture (e.g. a panel showing every problem at once).
    """

    def __init__(self, errors: list[str]) -> None:
        super().__init__(errors[0] if errors else "invalid configuration")
        self.errors = errors


class VersionNotFoundError(LookupError):
    """No ``config_bundle_versions`` row matches the given ``version_id``."""


class StaleVersionError(RuntimeError):
    """A save/approval was based on a version that is no longer active
    (spec §8) -- never merged, always refused. The message names which
    version is now active, who created it, and when."""


class NotADraftError(RuntimeError):
    """:func:`approve_draft` / :func:`reject_draft` called on a version
    whose ``status`` is not ``"draft"``."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_content(files: dict[str, str]) -> str:
    canonical = json.dumps(files, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _content_dict(row: dict[str, Any]) -> dict[str, str]:
    return json.loads(row["content_json"])


def _text_validators() -> dict[str, Any]:
    """``{filename: callable(text) -> parsed model}`` -- the real,
    in-memory text validators for each of :data:`CONFIG_FILENAMES` (see
    module docstring's "Validated, every time" section).

    Every entry is a genuine loader function
    (:func:`knowledge.config_loader.validate_yaml_text` /
    :func:`schema_data.registry.validate_schema_yaml_text`), not a
    reimplementation -- both raise the exact same
    ``"[filename] validation error at ...'"`` :class:`ValueError`
    :mod:`knowledge.config_loader`'s file-based loaders raise for the same
    content on disk. Deliberately the *text*-based entry points, not the
    file-based ``load_*`` functions: those read
    ``cfg.settings.project_config_dir`` and this module must validate a
    candidate bundle that may never be written to disk, without the
    thread-unsafe ``cfg.override_settings`` detour (see
    :func:`~knowledge.config_loader.validate_yaml_text`'s docstring).

    Deferred import: this module must not force-import the whole
    knowledge/schema layer merely by being imported.
    """
    from knowledge.config_loader import (
        AliasesConfig,
        BusinessRulesConfig,
        EntitiesConfig,
        ExamplesConfig,
        MemoryPolicyConfig,
        MetricsConfig,
        RetrievalHintsConfig,
        SessionPolicyConfig,
        validate_yaml_text,
    )
    from schema_data.registry import validate_schema_yaml_text

    return {
        "aliases.yaml": lambda text: validate_yaml_text("aliases.yaml", text, AliasesConfig),
        "business_rules.yaml": lambda text: validate_yaml_text(
            "business_rules.yaml", text, BusinessRulesConfig
        ),
        "entities.yaml": lambda text: validate_yaml_text("entities.yaml", text, EntitiesConfig),
        "examples.yaml": lambda text: validate_yaml_text("examples.yaml", text, ExamplesConfig),
        "metrics.yaml": lambda text: validate_yaml_text("metrics.yaml", text, MetricsConfig),
        "retrieval_hints.yaml": lambda text: validate_yaml_text(
            "retrieval_hints.yaml", text, RetrievalHintsConfig
        ),
        "session_policy.yaml": lambda text: validate_yaml_text(
            "session_policy.yaml", text, SessionPolicyConfig
        ),
        "memory_policy.yaml": lambda text: validate_yaml_text(
            "memory_policy.yaml", text, MemoryPolicyConfig
        ),
        "schema.yaml": lambda text: validate_schema_yaml_text(text),
    }


def _public(row: dict[str, Any], *, include_content: bool = False) -> dict[str, Any]:
    out = {
        "version_id": row["version_id"],
        "status": row["status"],
        "content_hash": row["content_hash"],
        "based_on_version": row["based_on_version"],
        "restored_from_version": row["restored_from_version"],
        "restored_file": row["restored_file"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "created_by_capability": row["created_by_capability"],
        "reviewed_by": row["reviewed_by"],
        "reviewed_at": row["reviewed_at"],
        "review_note": row["review_note"],
        "diff": json.loads(row["diff_json"]) if row["diff_json"] else None,
        "dry_run": json.loads(row["dry_run_json"]) if row["dry_run_json"] else None,
    }
    if include_content:
        out["files"] = _content_dict(row)
    return out


# ---------------------------------------------------------------------------
# Bootstrap -- lazy, on first access, mirroring knowledge/*.py's own
# "loaded on first access, never at import time" discipline.
# ---------------------------------------------------------------------------

def _ensure_bootstrapped(conn) -> None:
    count = conn.execute(select(func.count()).select_from(config_bundle_versions)).scalar()
    if count:
        return
    base_dir = Path(cfg.settings.project_config_dir)
    files = {
        filename: (base_dir / filename).read_text(encoding="utf-8")
        if (base_dir / filename).exists() else ""
        for filename in CONFIG_FILENAMES
    }
    conn.execute(
        config_bundle_versions.insert().values(
            status="applied",
            content_json=json.dumps(files, sort_keys=True),
            content_hash=_hash_content(files),
            based_on_version=None,
            restored_from_version=None,
            restored_file=None,
            created_at=_now_iso(),
            created_by="bootstrap",
            created_by_capability=OPERATIONS_CAPABILITY,
            reviewed_by=None,
            reviewed_at=None,
            review_note=None,
            diff_json=None,
            dry_run_json=None,
        )
    )


def _active_version_row(conn) -> dict[str, Any] | None:
    row = (
        conn.execute(
            select(config_bundle_versions)
            .where(config_bundle_versions.c.status == "applied")
            .order_by(config_bundle_versions.c.version_id.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _version_row(conn, version_id: int) -> dict[str, Any]:
    row = (
        conn.execute(
            select(config_bundle_versions).where(
                config_bundle_versions.c.version_id == version_id
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise VersionNotFoundError(f"no config version {version_id!r}")
    return dict(row)


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

def get_active_version() -> dict[str, Any]:
    """The current active bundle version, with its content."""
    engine = get_app_engine()
    with engine.begin() as conn:
        _ensure_bootstrapped(conn)
        row = _active_version_row(conn)
    assert row is not None  # _ensure_bootstrapped guarantees at least one applied row
    return _public(row, include_content=True)


def get_active_version_id() -> int:
    """The current active bundle version's ``version_id`` alone -- the
    "configuration version identifier" :mod:`prompt_engine.static_prefix`'s
    ``prefix_version_for_config`` derives the prefix version from (spec
    §6)."""
    return get_active_version()["version_id"]


def get_version(version_id: int) -> dict[str, Any]:
    """One version's full metadata and content.

    Raises
    ------
    VersionNotFoundError
    """
    engine = get_app_engine()
    with engine.begin() as conn:
        row = _version_row(conn, version_id)
    return _public(row, include_content=True)


def list_versions() -> list[dict[str, Any]]:
    """Every version's metadata (never content), newest first."""
    engine = get_app_engine()
    with engine.begin() as conn:
        _ensure_bootstrapped(conn)
        rows = (
            conn.execute(
                select(config_bundle_versions).order_by(
                    config_bundle_versions.c.version_id.desc()
                )
            )
            .mappings()
            .all()
        )
    return [_public(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Validation, diff, dry-run (spec §4, §5)
# ---------------------------------------------------------------------------

def _validate_bundle(files: dict[str, str]) -> None:
    """Parse and validate every one of :data:`CONFIG_FILENAMES` through the
    real loaders' in-memory text-validation entry points (see
    :func:`_text_validators`) -- never against the live
    ``project_config/`` on disk, and never via
    :func:`config.override_settings` (thread-unsafe: see
    :func:`knowledge.config_loader.validate_yaml_text`'s docstring), so a
    bad candidate never disturbs the active configuration and this
    function is safe to call from a concurrently-served admin request.

    Raises
    ------
    ConfigVersionValidationError
        Carrying every failing file's own loader error.
    """
    errors: list[str] = []
    validators = _text_validators()
    for filename in CONFIG_FILENAMES:
        try:
            validators[filename](files.get(filename, ""))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ConfigVersionValidationError(errors)


def _schema_table_columns(schema_yaml_text: str) -> dict[str, set[str]]:
    """``{table: {column, ...}}`` for the tables schema.yaml text describes
    with a ``columns`` key -- i.e. the guard allowlist a candidate
    ``schema.yaml`` would produce, computed without touching
    :mod:`schema_data.registry`'s process-lifetime cache at all."""
    from schema_data.registry import SchemaConfig

    if not schema_yaml_text.strip():
        return {}
    parsed = SchemaConfig.model_validate(yaml.safe_load(schema_yaml_text) or {})
    return {
        name: set(table.columns or {})
        for name, table in parsed.tables.items()
        if table.columns
    }


def _diff_schema(old_text: str, new_text: str) -> dict[str, Any]:
    """Which tables and columns enter and leave the allowlist (spec §4.3) --
    never a text diff of the YAML."""
    old_tables = _schema_table_columns(old_text)
    new_tables = _schema_table_columns(new_text)

    tables_changed_columns: dict[str, dict[str, list[str]]] = {}
    for table in sorted(set(old_tables) & set(new_tables)):
        added = sorted(new_tables[table] - old_tables[table])
        removed = sorted(old_tables[table] - new_tables[table])
        if added or removed:
            tables_changed_columns[table] = {"columns_added": added, "columns_removed": removed}

    return {
        "tables_added": sorted(set(new_tables) - set(old_tables)),
        "tables_removed": sorted(set(old_tables) - set(new_tables)),
        "tables_changed_columns": tables_changed_columns,
    }


def _diff_bundles(old: dict[str, str], new: dict[str, str]) -> dict[str, Any]:
    files_changed = sorted(f for f in CONFIG_FILENAMES if old.get(f, "") != new.get(f, ""))
    diff: dict[str, Any] = {"files_changed": files_changed}
    if "schema.yaml" in files_changed:
        diff["schema"] = _diff_schema(old.get("schema.yaml", ""), new.get("schema.yaml", ""))
    return diff


def _dry_run_bundle(files: dict[str, str]) -> dict[str, Any]:
    """Run the golden set through the real, offline :mod:`eval` harness
    (spec §5) -- never reimplemented, only called -- plus, when
    ``schema.yaml`` is present, cross-check every table the golden set's
    reference SQL touches against the *candidate* allowlist.

    Offline mode replays the golden set's own ``expected_sql`` (see
    :mod:`eval.runner`'s docstring), so its accuracy figure is not a
    measure of generation quality against this candidate's domain
    knowledge -- what it *does* prove, every time, is that the harness
    still runs end to end and that the golden set's own reference queries
    still parse and pass the guard. The schema table cross-check below is
    what actually catches "this candidate schema.yaml would make a
    currently-working golden question fail."
    """
    from eval.report import build_report
    from eval.runner import load_golden_cases, make_offline_executor, make_offline_generator, run_golden_set
    from security.sql_guard import extract_touched_tables

    golden_path = Path(cfg.settings.eval_golden_path)
    if not golden_path.exists():
        return {"ran": False, "reason": f"golden set not found: {golden_path}"}

    cases = load_golden_cases(golden_path)
    generate_fn = make_offline_generator(cases)
    execute_fn = make_offline_executor(cases)
    report = build_report(run_golden_set(cases, generate_fn, execute_fn), mode="offline")

    missing_tables: list[str] = []
    schema_text = files.get("schema.yaml", "")
    if schema_text.strip():
        candidate_tables = set(_schema_table_columns(schema_text))
        touched: set[str] = set()
        for case in cases:
            if case.expected_sql:
                touched.update(extract_touched_tables(case.expected_sql))
        missing_tables = sorted(touched - candidate_tables)

    return {
        "ran": True,
        "mode": report.mode,
        "total": report.total,
        "passed": report.passed,
        "accuracy_pct": report.accuracy_pct,
        "guard_rejections": report.guard_rejections,
        "status_counts": dict(report.status_counts),
        "golden_set_tables_missing_from_candidate_schema": missing_tables,
    }


# ---------------------------------------------------------------------------
# Cache invalidation on apply -- see module docstring's "What takes effect
# immediately, and what needs a restart".
# ---------------------------------------------------------------------------

def _invalidate_runtime_caches(files_changed: Sequence[str]) -> None:
    touched_prefix_relevant = False
    for filename in files_changed:
        module_name = _KNOWLEDGE_CACHE_MODULES.get(filename)
        if module_name is None:
            continue  # schema.yaml (or an unrecognised name): left alone
        module = importlib.import_module(module_name)
        module._cache.clear()
        if filename in _PREFIX_RELEVANT_FILES:
            touched_prefix_relevant = True

    if touched_prefix_relevant:
        from prompt_engine.static_prefix import build_static_prefix, prefix_version

        # static_prefix_token_estimate/should_use_static_prefix are plain
        # functions that call build_static_prefix internally -- they carry
        # no lru_cache of their own, so clearing these two is sufficient.
        build_static_prefix.cache_clear()
        prefix_version.cache_clear()


def _write_bundle_to_disk(files: dict[str, str]) -> None:
    base_dir = Path(cfg.settings.project_config_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    for filename in CONFIG_FILENAMES:
        (base_dir / filename).write_text(files.get(filename, ""), encoding="utf-8")


def _write_export(files: dict[str, str], version_id: int) -> None:
    """Write *files* to ``cfg.settings.config_export_dir``, when configured
    (spec §7). A no-op when that setting is empty (the default)."""
    export_dir = cfg.settings.config_export_dir
    if not export_dir:
        return
    target = Path(export_dir)
    target.mkdir(parents=True, exist_ok=True)
    for filename in CONFIG_FILENAMES:
        (target / filename).write_text(files.get(filename, ""), encoding="utf-8")
    manifest = {"version_id": version_id, "exported_at": _now_iso()}
    (target / ".config_export_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------

def propose_or_apply(
    files: dict[str, str],
    *,
    based_on_version: int,
    actor_principal_id: str,
    actor_capabilities: frozenset[str] | set[str],
) -> dict[str, Any]:
    """Validate, diff and dry-run *files* against the active version, then
    either apply it as a new version or -- if it touches ``schema.yaml``
    and *actor_capabilities* lacks the security capability -- save it as an
    unapplied draft for a security admin to review (spec §3.1).

    *files* need not name every one of :data:`CONFIG_FILENAMES`: any
    filename not given is carried forward unchanged from the version named
    by *based_on_version*.

    Parameters
    ----------
    files:
        ``{filename: yaml_text}`` for the file(s) being changed.
    based_on_version:
        The ``version_id`` the caller had open (optimistic locking, spec
        §8).
    actor_principal_id, actor_capabilities:
        Who is saving, and which capabilities they hold.

    Returns
    -------
    dict
        The newly created version's public representation (see
        :func:`get_version`) -- its ``status`` tells the caller whether it
        was applied immediately or saved as a draft.

    Raises
    ------
    StaleVersionError
        *based_on_version* is not the current active version.
    ConfigVersionValidationError
        A file failed to parse/validate through its real loader.
    ValueError
        *files* names a filename outside :data:`CONFIG_FILENAMES`, or
        ``schema.yaml``'s structural invariants fail (spec §4 item 2).
    """
    unknown = set(files) - set(CONFIG_FILENAMES)
    if unknown:
        raise ValueError(f"unknown config file(s): {sorted(unknown)}")

    engine = get_app_engine()
    with engine.begin() as conn:
        _ensure_bootstrapped(conn)
        active = _active_version_row(conn)
        assert active is not None
        if active["version_id"] != based_on_version:
            raise StaleVersionError(
                f"based_on_version={based_on_version} is no longer the "
                f"active configuration -- version {active['version_id']} "
                f"was created at {active['created_at']} by "
                f"{active['created_by']!r}. Reload the current "
                "configuration and re-apply your change on top of it."
            )

        base_content = _content_dict(active)
        new_content = dict(base_content)
        new_content.update(files)

        _validate_bundle(new_content)
        if "schema.yaml" in files and new_content["schema.yaml"].strip():
            from schema_data.registry import SchemaConfig, check_allowlist_structural_invariants

            parsed = SchemaConfig.model_validate(yaml.safe_load(new_content["schema.yaml"]) or {})
            table_columns = {
                name: t.columns for name, t in parsed.tables.items() if t.columns
            }
            table_descriptions = {name: t.description for name, t in parsed.tables.items()}
            relationships = {
                f"{r.from_table} -> {r.to_table}": r.join_sql for r in parsed.relationships
            }
            violations = check_allowlist_structural_invariants(
                table_columns, table_descriptions, relationships
            )
            if violations:
                raise ValueError(
                    "schema.yaml failed the guard allowlist's structural "
                    f"invariants: {'; '.join(violations)}"
                )

        diff = _diff_bundles(base_content, new_content)
        dry_run = _dry_run_bundle(new_content)
        # The role split (spec §3) turns on whether any SECURITY_ONLY_FILES
        # entry actually changed -- not merely whether one was named in
        # *files* (a caller may pass a filename whose content happens to be
        # byte-identical to the active version's).
        schema_changed = bool(SECURITY_ONLY_FILES & set(diff["files_changed"]))

        if schema_changed and SECURITY_CAPABILITY not in actor_capabilities:
            status = "draft"
            created_by_capability = OPERATIONS_CAPABILITY
        else:
            status = "applied"
            created_by_capability = (
                SECURITY_CAPABILITY if schema_changed else OPERATIONS_CAPABILITY
            )

        now = _now_iso()
        result = conn.execute(
            config_bundle_versions.insert().values(
                status=status,
                content_json=json.dumps(new_content, sort_keys=True),
                content_hash=_hash_content(new_content),
                based_on_version=based_on_version,
                restored_from_version=None,
                restored_file=None,
                created_at=now,
                created_by=actor_principal_id,
                created_by_capability=created_by_capability,
                reviewed_by=None,
                reviewed_at=None,
                review_note=None,
                diff_json=json.dumps(diff, sort_keys=True),
                dry_run_json=json.dumps(dry_run, sort_keys=True),
            )
        )
        version_id = result.inserted_primary_key[0]

    if status == "applied":
        _write_bundle_to_disk(new_content)
        _write_export(new_content, version_id)
        _invalidate_runtime_caches(diff["files_changed"])

    return get_version(version_id)


def _stamp_restore_provenance(
    version_id: int, from_version_id: int, filename: str | None
) -> None:
    engine = get_app_engine()
    with engine.begin() as conn:
        conn.execute(
            config_bundle_versions.update()
            .where(config_bundle_versions.c.version_id == version_id)
            .values(restored_from_version=from_version_id, restored_file=filename)
        )


def restore(
    *,
    from_version_id: int,
    filename: str | None,
    actor_principal_id: str,
    actor_capabilities: frozenset[str] | set[str],
) -> dict[str, Any]:
    """Restore *filename* (or, when ``None``, the whole bundle) from
    ``from_version_id`` -- spec §2's "revert, never reset": this creates a
    brand-new version and never touches ``from_version_id`` or any version
    in between it and the current active one.

    Goes through the exact same :func:`propose_or_apply` path as any other
    edit (optimistic locking against the *current* active version,
    validation, diff, dry-run, and the same operations/security split for
    a ``schema.yaml`` restore) -- a rollback is an edit like any other and
    gets the same treatment (spec §4).

    Raises
    ------
    VersionNotFoundError
        *from_version_id* does not exist.
    ValueError
        *filename* is not one of :data:`CONFIG_FILENAMES`.
    """
    if filename is not None and filename not in CONFIG_FILENAMES:
        raise ValueError(f"unknown config file: {filename!r}")

    engine = get_app_engine()
    with engine.begin() as conn:
        _ensure_bootstrapped(conn)
        active = _active_version_row(conn)
        assert active is not None
        source = _version_row(conn, from_version_id)

    source_content = _content_dict(source)
    files = dict(source_content) if filename is None else {filename: source_content.get(filename, "")}

    result = propose_or_apply(
        files,
        based_on_version=active["version_id"],
        actor_principal_id=actor_principal_id,
        actor_capabilities=actor_capabilities,
    )
    _stamp_restore_provenance(result["version_id"], from_version_id, filename)
    return get_version(result["version_id"])


def approve_draft(version_id: int, *, actor_principal_id: str) -> dict[str, Any]:
    """Promote a pending draft to a real, applied version (spec §3.1).

    Raises
    ------
    VersionNotFoundError
    NotADraftError
        *version_id*'s status is not ``"draft"``.
    StaleVersionError
        The configuration has moved on since this draft was proposed.
    """
    engine = get_app_engine()
    with engine.begin() as conn:
        draft = _version_row(conn, version_id)
        if draft["status"] != "draft":
            raise NotADraftError(
                f"version {version_id} is not a pending draft (status={draft['status']!r})"
            )
        active = _active_version_row(conn)
        assert active is not None
        if active["version_id"] != draft["based_on_version"]:
            raise StaleVersionError(
                f"this draft was based on version {draft['based_on_version']}, "
                f"but version {active['version_id']} is now the active "
                f"configuration (created {active['created_at']} by "
                f"{active['created_by']!r}). Reject this draft and re-propose "
                "the change on top of the current configuration."
            )
        now = _now_iso()
        conn.execute(
            config_bundle_versions.update()
            .where(config_bundle_versions.c.version_id == version_id)
            .values(status="applied", reviewed_by=actor_principal_id, reviewed_at=now)
        )
        content = _content_dict(draft)
        files_changed = json.loads(draft["diff_json"])["files_changed"] if draft["diff_json"] else []

    _write_bundle_to_disk(content)
    _write_export(content, version_id)
    _invalidate_runtime_caches(files_changed)
    return get_version(version_id)


def reject_draft(version_id: int, *, actor_principal_id: str, reason: str) -> dict[str, Any]:
    """Reject a pending draft -- it never becomes active, and the row is
    kept (never deleted) recording who rejected it, when, and why (spec
    §3.1).

    Raises
    ------
    VersionNotFoundError
    NotADraftError
    """
    engine = get_app_engine()
    with engine.begin() as conn:
        draft = _version_row(conn, version_id)
        if draft["status"] != "draft":
            raise NotADraftError(
                f"version {version_id} is not a pending draft (status={draft['status']!r})"
            )
        conn.execute(
            config_bundle_versions.update()
            .where(config_bundle_versions.c.version_id == version_id)
            .values(
                status="rejected",
                reviewed_by=actor_principal_id,
                reviewed_at=_now_iso(),
                review_note=reason,
            )
        )
    return get_version(version_id)


# ---------------------------------------------------------------------------
# Export round trip (spec §7, §10)
# ---------------------------------------------------------------------------

def export_active_version(target_dir: str | Path) -> dict[str, Any]:
    """Write the current active version's bundle to *target_dir*, on
    demand -- the same shape :func:`_write_export` writes automatically to
    ``cfg.settings.config_export_dir`` on every apply, usable for an
    explicit "export now" action or to hand to
    :func:`import_bundle_from_directory` when setting up a second
    deployment."""
    active = get_active_version()
    files = active["files"]
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    for filename in CONFIG_FILENAMES:
        (target / filename).write_text(files.get(filename, ""), encoding="utf-8")
    manifest = {"version_id": active["version_id"], "exported_at": _now_iso()}
    (target / ".config_export_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def import_bundle_from_directory(
    source_dir: str | Path,
    *,
    actor_principal_id: str,
    actor_capabilities: frozenset[str] | set[str],
) -> dict[str, Any]:
    """Import a previously exported bundle (see :func:`export_active_version`)
    from *source_dir*.

    On a deployment with no version history yet, this seeds it directly as
    the bootstrap version -- unconditionally, mirroring
    :func:`appdb.key_store.bootstrap_from_env`'s own "import once, no
    validation gate on the very first version" bootstrap semantics (there
    is nothing yet to validate a first version *against*). On a deployment
    that already has an active version, this instead goes through the
    normal :func:`propose_or_apply` path -- validated, diffed, dry-run, and
    subject to the same schema.yaml draft/approve split as any other edit.
    """
    source = Path(source_dir)
    files = {
        filename: (source / filename).read_text(encoding="utf-8")
        if (source / filename).exists() else ""
        for filename in CONFIG_FILENAMES
    }

    engine = get_app_engine()
    with engine.begin() as conn:
        count = conn.execute(select(func.count()).select_from(config_bundle_versions)).scalar()

    if not count:
        with engine.begin() as conn:
            result = conn.execute(
                config_bundle_versions.insert().values(
                    status="applied",
                    content_json=json.dumps(files, sort_keys=True),
                    content_hash=_hash_content(files),
                    based_on_version=None,
                    restored_from_version=None,
                    restored_file=None,
                    created_at=_now_iso(),
                    created_by=actor_principal_id,
                    created_by_capability=(
                        SECURITY_CAPABILITY
                        if SECURITY_CAPABILITY in actor_capabilities
                        else OPERATIONS_CAPABILITY
                    ),
                    reviewed_by=None,
                    reviewed_at=None,
                    review_note=None,
                    diff_json=None,
                    dry_run_json=None,
                )
            )
            version_id = result.inserted_primary_key[0]
        _write_bundle_to_disk(files)
        _invalidate_runtime_caches(CONFIG_FILENAMES)
        return get_version(version_id)

    active = get_active_version()
    return propose_or_apply(
        files,
        based_on_version=active["version_id"],
        actor_principal_id=actor_principal_id,
        actor_capabilities=actor_capabilities,
    )
