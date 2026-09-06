# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 3 -- appdb.config_versions. Frozen spec.

Real loaders, a real (temp-file) application database, and the real
offline eval harness throughout -- no mock at the boundary under test, per
the spec's testing discipline. Every test copies
``project_config.example/`` into an isolated ``tmp_path`` so nothing here
ever touches the real, git-ignored ``project_config/``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

import config as cfg
from appdb.config_versions import (
    CONFIG_FILENAMES,
    ConfigVersionValidationError,
    NotADraftError,
    StaleVersionError,
    VersionNotFoundError,
    approve_draft,
    export_active_version,
    get_active_version,
    get_version,
    import_bundle_from_directory,
    list_versions,
    propose_or_apply,
    reject_draft,
    restore,
)
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache
from security.auth import OPERATIONS_CAPABILITY, SECURITY_CAPABILITY

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"
_EXAMPLE_GOLDEN_PATH = _REPO_ROOT / "eval_data.example" / "golden.jsonl"

_OPS = frozenset({OPERATIONS_CAPABILITY})
_SECURITY = frozenset({SECURITY_CAPABILITY})
_BOTH = frozenset({OPERATIONS_CAPABILITY, SECURITY_CAPABILITY})


@pytest.fixture()
def project_dir(tmp_path) -> Path:
    dest = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE_CONFIG_DIR, dest)
    return dest


@pytest.fixture()
def app_env(tmp_path, project_dir):
    db_path = tmp_path / "appdb.db"
    export_dir = tmp_path / "export"
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}",
        project_config_dir=str(project_dir),
        eval_golden_path=str(_EXAMPLE_GOLDEN_PATH),
        config_export_dir=str(export_dir),
    ):
        dispose_app_engine()
        invalidate_cache()
        yield {"project_dir": project_dir, "export_dir": export_dir}
    dispose_app_engine()
    invalidate_cache()


def _remove_table(schema_text: str, table: str) -> str:
    """Remove *table* (and every relationship touching it) from schema.yaml
    text -- the shape a real "drop a table" edit takes."""
    doc = yaml.safe_load(schema_text)
    del doc["tables"][table]
    doc["relationships"] = [
        r for r in doc.get("relationships", [])
        if r["from_table"] != table and r["to_table"] != table
    ]
    return yaml.dump(doc, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

class TestBootstrap:
    def test_first_access_creates_version_one_from_disk(self, app_env):
        active = get_active_version()
        assert active["version_id"] == 1
        assert active["status"] == "applied"
        assert active["created_by"] == "bootstrap"
        assert set(active["files"]) == set(CONFIG_FILENAMES)
        on_disk = (app_env["project_dir"] / "schema.yaml").read_text(encoding="utf-8")
        assert active["files"]["schema.yaml"] == on_disk

    def test_bootstrap_only_happens_once(self, app_env):
        get_active_version()
        get_active_version()
        assert len(list_versions()) == 1


# ---------------------------------------------------------------------------
# Ordinary edits: the eight operations files apply directly
# ---------------------------------------------------------------------------

class TestOperationsFileEditsApplyDirectly:
    def test_business_rules_edit_by_operations_applies_immediately(self, app_env):
        active = get_active_version()
        new_text = active["files"]["business_rules.yaml"] + "\n# a trailing comment\n"
        result = propose_or_apply(
            {"business_rules.yaml": new_text},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )
        assert result["status"] == "applied"
        assert result["created_by_capability"] == OPERATIONS_CAPABILITY
        assert result["version_id"] == active["version_id"] + 1
        assert get_active_version()["version_id"] == result["version_id"]
        on_disk = (app_env["project_dir"] / "business_rules.yaml").read_text(encoding="utf-8")
        assert on_disk == new_text

    def test_unnamed_files_are_carried_forward_unchanged(self, app_env):
        active = get_active_version()
        result = propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"]},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )
        for filename in CONFIG_FILENAMES:
            if filename == "metrics.yaml":
                continue
            assert result["files"][filename] == active["files"][filename]


# ---------------------------------------------------------------------------
# §3 / §3.1: schema.yaml is security's; operations can only propose a draft
# ---------------------------------------------------------------------------

class TestSchemaYamlRequiresSecurityOrGoesToDraft:
    def test_operations_only_actor_creates_an_unapplied_draft(self, app_env):
        active = get_active_version()
        new_schema = _remove_table(active["files"]["schema.yaml"], "Customer")
        result = propose_or_apply(
            {"schema.yaml": new_schema},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )
        assert result["status"] == "draft"
        assert result["created_by_capability"] == OPERATIONS_CAPABILITY
        # The active configuration must be completely unaffected by a draft.
        assert get_active_version()["version_id"] == active["version_id"]
        on_disk = (app_env["project_dir"] / "schema.yaml").read_text(encoding="utf-8")
        assert on_disk == active["files"]["schema.yaml"]

    def test_security_actor_applies_a_schema_change_directly(self, app_env):
        active = get_active_version()
        new_schema = _remove_table(active["files"]["schema.yaml"], "Customer")
        result = propose_or_apply(
            {"schema.yaml": new_schema},
            based_on_version=active["version_id"],
            actor_principal_id="sec-1",
            actor_capabilities=_SECURITY,
        )
        assert result["status"] == "applied"
        assert result["created_by_capability"] == SECURITY_CAPABILITY
        assert get_active_version()["version_id"] == result["version_id"]

    def test_dual_capability_actor_applies_directly_too(self, app_env):
        active = get_active_version()
        new_schema = _remove_table(active["files"]["schema.yaml"], "Customer")
        result = propose_or_apply(
            {"schema.yaml": new_schema},
            based_on_version=active["version_id"],
            actor_principal_id="dual-1",
            actor_capabilities=_BOTH,
        )
        assert result["status"] == "applied"
        assert result["created_by_capability"] == SECURITY_CAPABILITY

    def test_a_non_schema_edit_from_a_pending_draft_author_still_applies(self, app_env):
        """Proposing a schema.yaml draft must not block operations from
        continuing to edit the other eight files directly in the meantime
        -- the draft and the active configuration are independent rows."""
        active = get_active_version()
        propose_or_apply(
            {"schema.yaml": _remove_table(active["files"]["schema.yaml"], "Customer")},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )
        result = propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"] + "\n"},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )
        assert result["status"] == "applied"


class TestApproveAndRejectDraft:
    def _make_draft(self, app_env):
        active = get_active_version()
        new_schema = _remove_table(active["files"]["schema.yaml"], "Customer")
        return active, propose_or_apply(
            {"schema.yaml": new_schema},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )

    def test_approving_a_draft_makes_it_the_active_version(self, app_env):
        active, draft = self._make_draft(app_env)
        approved = approve_draft(draft["version_id"], actor_principal_id="sec-1")
        assert approved["status"] == "applied"
        assert approved["reviewed_by"] == "sec-1"
        assert get_active_version()["version_id"] == draft["version_id"]
        on_disk = (app_env["project_dir"] / "schema.yaml").read_text(encoding="utf-8")
        assert "Customer" not in yaml.safe_load(on_disk)["tables"]

    def test_rejecting_a_draft_leaves_it_never_active(self, app_env):
        active, draft = self._make_draft(app_env)
        rejected = reject_draft(draft["version_id"], actor_principal_id="sec-1", reason="not now")
        assert rejected["status"] == "rejected"
        assert rejected["review_note"] == "not now"
        assert get_active_version()["version_id"] == active["version_id"]

    def test_approving_an_already_applied_version_is_refused(self, app_env):
        active = get_active_version()
        with pytest.raises(NotADraftError):
            approve_draft(active["version_id"], actor_principal_id="sec-1")

    def test_approving_a_stale_draft_is_refused(self, app_env):
        """§8: optimistic locking applies to approval too -- a draft based
        on a configuration that has since moved on must not silently land
        on top of whatever is active now."""
        active, draft = self._make_draft(app_env)
        # Someone else applies an unrelated edit while the draft is pending.
        propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# meanwhile\n"},
            based_on_version=active["version_id"],
            actor_principal_id="ops-2",
            actor_capabilities=_OPS,
        )
        with pytest.raises(StaleVersionError):
            approve_draft(draft["version_id"], actor_principal_id="sec-1")


# ---------------------------------------------------------------------------
# §4: validation through the real loaders; a bad file changes nothing
# ---------------------------------------------------------------------------

class TestValidationThroughRealLoaders:
    def test_malformed_yaml_syntax_is_rejected_with_the_loaders_own_error(self, app_env):
        active = get_active_version()
        with pytest.raises(ConfigVersionValidationError) as exc_info:
            propose_or_apply(
                {"business_rules.yaml": "not: [valid, yaml"},
                based_on_version=active["version_id"],
                actor_principal_id="ops-1",
                actor_capabilities=_OPS,
            )
        assert "business_rules.yaml" in str(exc_info.value)
        assert get_active_version()["version_id"] == active["version_id"]

    def test_structurally_wrong_shape_is_rejected_with_the_loaders_own_error(self, app_env):
        active = get_active_version()
        with pytest.raises(ConfigVersionValidationError) as exc_info:
            propose_or_apply(
                # `rules` must be a mapping of rule_key -> {rule_text: ...};
                # a bare list fails BusinessRulesConfig's own Pydantic
                # validation, surfacing knowledge.config_loader's own
                # "[business_rules.yaml] validation error at ...".
                {"business_rules.yaml": "rules: [1, 2, 3]"},
                based_on_version=active["version_id"],
                actor_principal_id="ops-1",
                actor_capabilities=_OPS,
            )
        assert "business_rules.yaml" in str(exc_info.value)
        assert get_active_version()["version_id"] == active["version_id"]

    def test_schema_structural_invariant_violation_is_rejected(self, app_env):
        """A schema.yaml edit that drops a table but leaves a relationship
        pointing at it fails the same structural-invariant check
        tests/test_schema_registry_snapshot.py pins -- called, not
        reimplemented (schema_data.registry.check_allowlist_structural_invariants).

        project_config.example/schema.yaml ships with no relationships at
        all, so one referencing "Customer" is added first (applied, since
        it is internally consistent on its own), then Customer itself is
        dropped without also dropping that relationship -- the exact
        "drop a table, forget its relationship" mistake the check exists
        to catch.
        """
        active = get_active_version()
        doc = yaml.safe_load(active["files"]["schema.yaml"])
        doc["relationships"] = [
            {"from_table": "Customer", "to_table": "Ring", "join_sql": "1=1"}
        ]
        with_relationship = yaml.dump(doc, allow_unicode=True, sort_keys=False)
        staged = propose_or_apply(
            {"schema.yaml": with_relationship},
            based_on_version=active["version_id"],
            actor_principal_id="sec-1",
            actor_capabilities=_SECURITY,
        )
        assert staged["status"] == "applied"

        doc2 = yaml.safe_load(staged["files"]["schema.yaml"])
        del doc2["tables"]["Customer"]  # the "Customer -> Ring" relationship is left dangling
        broken = yaml.dump(doc2, allow_unicode=True, sort_keys=False)
        with pytest.raises(ValueError, match="Customer"):
            propose_or_apply(
                {"schema.yaml": broken},
                based_on_version=staged["version_id"],
                actor_principal_id="sec-1",
                actor_capabilities=_SECURITY,
            )
        assert get_active_version()["version_id"] == staged["version_id"]

    def test_unknown_filename_is_rejected(self, app_env):
        active = get_active_version()
        with pytest.raises(ValueError, match="unknown config file"):
            propose_or_apply(
                {"not_a_real_file.yaml": "x: 1"},
                based_on_version=active["version_id"],
                actor_principal_id="ops-1",
                actor_capabilities=_OPS,
            )


# ---------------------------------------------------------------------------
# §8: optimistic locking, not merging
# ---------------------------------------------------------------------------

class TestOptimisticLocking:
    def test_stale_based_save_is_refused_naming_who_and_when(self, app_env):
        active = get_active_version()
        propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# first\n"},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )
        with pytest.raises(StaleVersionError) as exc_info:
            propose_or_apply(
                {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# second\n"},
                based_on_version=active["version_id"],  # stale: someone already moved past this
                actor_principal_id="ops-2",
                actor_capabilities=_OPS,
            )
        message = str(exc_info.value)
        assert "ops-1" in message  # names who changed it
        assert str(active["version_id"] + 1) in message  # and to which version

    def test_no_merge_is_attempted_the_second_writer_must_reload(self, app_env):
        active = get_active_version()
        first = propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# first\n"},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )
        # Reloading and re-basing on the now-current version succeeds.
        second = propose_or_apply(
            {"business_rules.yaml": first["files"]["business_rules.yaml"] + "\n# second\n"},
            based_on_version=first["version_id"],
            actor_principal_id="ops-2",
            actor_capabilities=_OPS,
        )
        assert second["status"] == "applied"
        assert "# first\n" in second["files"]["metrics.yaml"]


# ---------------------------------------------------------------------------
# §2/§6.1: restore is revert, never reset
# ---------------------------------------------------------------------------

class TestRestoreIsRevertNeverReset:
    def test_restoring_an_old_version_creates_a_new_one_and_keeps_history(self, app_env):
        v1 = get_active_version()
        v2 = propose_or_apply(
            {"metrics.yaml": v1["files"]["metrics.yaml"] + "\n# v2\n"},
            based_on_version=v1["version_id"], actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        v3 = propose_or_apply(
            {"metrics.yaml": v2["files"]["metrics.yaml"] + "\n# v3\n"},
            based_on_version=v2["version_id"], actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        v4 = propose_or_apply(
            {"metrics.yaml": v3["files"]["metrics.yaml"] + "\n# v4\n"},
            based_on_version=v3["version_id"], actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        assert (v1["version_id"], v2["version_id"], v3["version_id"], v4["version_id"]) == (1, 2, 3, 4)

        restored = restore(
            from_version_id=v1["version_id"], filename=None,
            actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        # Restoring version 1 (with 2, 3, 4 already existing) creates
        # version 5 -- it does NOT overwrite or delete 2, 3, or 4.
        assert restored["version_id"] == 5
        assert restored["restored_from_version"] == 1
        assert get_version(2)["status"] == "applied"
        assert get_version(3)["status"] == "applied"
        assert get_version(4)["status"] == "applied"
        assert get_version(2)["files"]["metrics.yaml"] == v2["files"]["metrics.yaml"]
        assert get_version(3)["files"]["metrics.yaml"] == v3["files"]["metrics.yaml"]
        assert get_active_version()["files"]["metrics.yaml"] == v1["files"]["metrics.yaml"]

    def test_per_file_restore_produces_a_whole_bundle_version(self, app_env):
        v1 = get_active_version()
        v2 = propose_or_apply(
            {
                "metrics.yaml": v1["files"]["metrics.yaml"] + "\n# v2\n",
                "business_rules.yaml": v1["files"]["business_rules.yaml"] + "\n# v2\n",
            },
            based_on_version=v1["version_id"], actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        restored = restore(
            from_version_id=v1["version_id"], filename="metrics.yaml",
            actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        # Only metrics.yaml reverts; business_rules.yaml (also changed in
        # v2) is untouched by this per-file restore, and the result is
        # still one full, self-contained bundle version.
        assert restored["restored_file"] == "metrics.yaml"
        assert restored["files"]["metrics.yaml"] == v1["files"]["metrics.yaml"]
        assert restored["files"]["business_rules.yaml"] == v2["files"]["business_rules.yaml"]
        assert set(restored["files"]) == set(CONFIG_FILENAMES)

    def test_restoring_a_schema_change_still_goes_through_the_role_split(self, app_env):
        """A rollback is an edit like any other (spec §4) -- restoring a
        schema.yaml version from an operations-only actor still produces
        a draft, not an immediate apply."""
        v1 = get_active_version()
        v2 = propose_or_apply(
            {"schema.yaml": _remove_table(v1["files"]["schema.yaml"], "Customer")},
            based_on_version=v1["version_id"], actor_principal_id="sec-1", actor_capabilities=_SECURITY,
        )
        result = restore(
            from_version_id=v1["version_id"], filename="schema.yaml",
            actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        assert result["status"] == "draft"
        assert get_active_version()["version_id"] == v2["version_id"]

    def test_restore_from_an_unknown_version_is_refused(self, app_env):
        get_active_version()
        with pytest.raises(VersionNotFoundError):
            restore(
                from_version_id=999999, filename=None,
                actor_principal_id="ops-1", actor_capabilities=_OPS,
            )


# ---------------------------------------------------------------------------
# §4.3: the schema.yaml diff names tables/columns entering and leaving
# ---------------------------------------------------------------------------

class TestSchemaDiff:
    def test_diff_names_removed_table(self, app_env):
        active = get_active_version()
        result = propose_or_apply(
            {"schema.yaml": _remove_table(active["files"]["schema.yaml"], "Customer")},
            based_on_version=active["version_id"],
            actor_principal_id="sec-1", actor_capabilities=_SECURITY,
        )
        assert result["diff"]["files_changed"] == ["schema.yaml"]
        assert result["diff"]["schema"]["tables_removed"] == ["Customer"]
        assert result["diff"]["schema"]["tables_added"] == []

    def test_diff_is_empty_for_files_that_did_not_change(self, app_env):
        active = get_active_version()
        result = propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"]},  # byte-identical
            based_on_version=active["version_id"],
            actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        assert result["diff"]["files_changed"] == []


# ---------------------------------------------------------------------------
# §5: dry-run against the golden set catches a schema.yaml regression
# ---------------------------------------------------------------------------

class TestDryRunCatchesAGoldenSetRegression:
    """``eval_data.example/golden.jsonl`` ships real-schema-shaped SQL
    (against tables like ``Contract``/``Order``/``CustomerContract``) while
    ``project_config.example/schema.yaml`` ships an unrelated, generic
    placeholder schema -- the two example fixtures are not designed to be
    mutually consistent, only each individually plausible. So these tests
    compare the *baseline* missing-table set (whatever it already is
    against this pair of fixtures) before and after a table removal,
    rather than asserting it is empty to start with.
    """

    def test_removing_a_table_the_golden_set_needs_is_flagged(self, app_env):
        """"A schema.yaml from five versions ago may name tables the
        warehouse no longer has. Restoring it blindly turns working
        questions into refusals." -- the dry-run's job is to see this
        BEFORE the version is applied, not after."""
        active = get_active_version()
        baseline = propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# baseline\n"},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        baseline_missing = set(
            baseline["dry_run"]["golden_set_tables_missing_from_candidate_schema"]
        )
        assert "Customer" not in baseline_missing  # still present in the unchanged schema

        result = propose_or_apply(
            {"schema.yaml": _remove_table(baseline["files"]["schema.yaml"], "Customer")},
            based_on_version=baseline["version_id"],
            actor_principal_id="sec-1", actor_capabilities=_SECURITY,
        )
        dry_run = result["dry_run"]
        assert dry_run["ran"] is True
        assert "Customer" in dry_run["golden_set_tables_missing_from_candidate_schema"]

    def test_an_unchanged_schema_flags_nothing_new(self, app_env):
        active = get_active_version()
        baseline = propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# baseline\n"},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        baseline_missing = set(
            baseline["dry_run"]["golden_set_tables_missing_from_candidate_schema"]
        )
        result = propose_or_apply(
            {"metrics.yaml": baseline["files"]["metrics.yaml"] + "\n# noop-ish\n"},
            based_on_version=baseline["version_id"],
            actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        # schema.yaml did not change in either save, so the missing-table
        # set must be unchanged too.
        assert set(
            result["dry_run"]["golden_set_tables_missing_from_candidate_schema"]
        ) == baseline_missing


# ---------------------------------------------------------------------------
# §7 / §10: export round-trips byte-identically into an empty deployment
# ---------------------------------------------------------------------------

class TestExportImportRoundTrip:
    def test_export_then_import_into_an_empty_deployment_is_byte_identical(
        self, app_env, tmp_path
    ):
        active = get_active_version()
        edited = propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# edited before export\n"},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1", actor_capabilities=_OPS,
        )
        manifest = export_active_version(app_env["export_dir"])
        assert manifest["version_id"] == edited["version_id"]

        # A fresh, empty deployment: its own database and project_config/.
        fresh_db = tmp_path / "fresh_appdb.db"
        fresh_project_dir = tmp_path / "fresh_project_config"
        with cfg.override_settings(
            app_db_url=f"sqlite:///{fresh_db}",
            project_config_dir=str(fresh_project_dir),
        ):
            dispose_app_engine()
            invalidate_cache()
            try:
                imported = import_bundle_from_directory(
                    app_env["export_dir"], actor_principal_id="ops-1", actor_capabilities=_OPS,
                )
                assert imported["version_id"] == 1  # the fresh deployment's own first version
                assert imported["files"] == edited["files"]
            finally:
                dispose_app_engine()
                invalidate_cache()
