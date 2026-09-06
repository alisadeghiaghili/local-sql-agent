# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The proof the whole role split rests on (spec §3 / architecture §2.2).

``docs/admin-panel-architecture.md`` §2.2: "Someone could write a few-shot
example designed to steer the model toward a denied column. It would not
work: ``denied_columns`` is enforced in ``validate_sql``, not in the
prompt." That single sentence is why editing ``examples.yaml`` (and the
other seven operations files) is safe for an operations admin while
``schema.yaml`` is not -- and this module is the test that demonstrates it
is actually true, not merely asserted.

Two halves, both required:

1. **Operations can legitimately edit ``examples.yaml``** -- including an
   example an attacker (or a careless operator) crafted specifically to
   steer the model toward a denied column. Nothing about that edit is
   refused; it is exactly as ordinary as any other operations edit
   (:mod:`appdb.config_versions.propose_or_apply` applies it directly).
2. **The crafted example's own SQL is still refused by the real guard**
   the moment a ``denied_columns`` policy names the column it targets --
   at the exact HTTP boundary a live deployment would enforce it
   (:func:`security.sql_guard.validate_sql`, called the way
   ``api/runner.py`` calls it, with a principal's real
   ``denied_columns``). No mock at the boundary under test: this is the
   genuine guard, not a stand-in.

Alongside it: the other half of the same coin --
``schema.yaml`` itself (the allowlist the guard's check depends on)
is exactly what an operations admin CANNOT push live, proven here at the
:mod:`appdb.config_versions` boundary (the HTTP-level version of the same
proof lives in ``tests/test_admin_config_routes.py``'s
``TestSaveVersionRoleSplit``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

import config as cfg
from appdb.config_versions import get_active_version, propose_or_apply
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache
from security.auth import OPERATIONS_CAPABILITY, SECURITY_CAPABILITY
from security.sql_guard import PolicyRejection, validate_sql

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"

_OPS = frozenset({OPERATIONS_CAPABILITY})
_SECURITY = frozenset({SECURITY_CAPABILITY})

#: The column a crafted few-shot example below tries to steer the model
#: toward. Present on project_config.example/schema.yaml's ``Customer``
#: table, which is why that table is used throughout.
_TARGETED_COLUMN = "NationalID"

#: A few-shot example an attacker -- or simply a careless operator --
#: might add to steer the model toward reproducing a query against a
#: column a real deployment's security admin has denied for this
#: principal. Nothing about its *shape* is unusual: it looks exactly like
#: an ordinary, legitimate example.
_CRAFTED_EXAMPLE_QUESTION = "What is this customer's national ID?"
_CRAFTED_EXAMPLE_SQL = f"SELECT {_TARGETED_COLUMN} FROM Customer WHERE ID = 1"


@pytest.fixture()
def app_env(tmp_path):
    project_dir = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE_CONFIG_DIR, project_dir)
    db_path = tmp_path / "appdb.db"
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}",
        project_config_dir=str(project_dir),
    ):
        dispose_app_engine()
        invalidate_cache()
        yield project_dir
    dispose_app_engine()
    invalidate_cache()


def _add_crafted_example(examples_yaml_text: str) -> str:
    doc = yaml.safe_load(examples_yaml_text) or {}
    examples = list(doc.get("examples") or [])
    examples.append(
        {
            "tags": ["customer", "national-id"],
            "question": _CRAFTED_EXAMPLE_QUESTION,
            "sql": _CRAFTED_EXAMPLE_SQL,
        }
    )
    doc["examples"] = examples
    return yaml.dump(doc, allow_unicode=True, sort_keys=False)


class TestCraftedFewShotExampleCannotReachADeniedColumn:
    def test_operations_can_add_the_crafted_example_without_any_refusal(self, app_env):
        """Step 1: nothing stops operations from adding this example --
        editing examples.yaml is ordinary operations territory (spec §3),
        precisely BECAUSE step 2 below holds regardless of what the
        prompt contains."""
        active = get_active_version()
        new_examples_yaml = _add_crafted_example(active["files"]["examples.yaml"])

        result = propose_or_apply(
            {"examples.yaml": new_examples_yaml},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )

        assert result["status"] == "applied"
        assert result["created_by_capability"] == OPERATIONS_CAPABILITY
        parsed = yaml.safe_load(result["files"]["examples.yaml"])
        questions = [ex["question"] for ex in parsed["examples"]]
        assert _CRAFTED_EXAMPLE_QUESTION in questions

    def test_the_crafted_examples_own_sql_is_still_refused_with_denied_columns_active(
        self, app_env,
    ):
        """Step 2, the actual proof: the crafted example's own SQL text --
        the exact string an operations admin just legitimately put in the
        prompt -- is refused by the REAL security.sql_guard.validate_sql
        the moment a denied_columns policy names the column it targets.
        This is the one call site api/runner.py uses to enforce a
        principal's ACL; nothing here is mocked or reimplemented.
        """
        with pytest.raises(PolicyRejection, match=_TARGETED_COLUMN):
            validate_sql(_CRAFTED_EXAMPLE_SQL, denied_columns={_TARGETED_COLUMN})

    def test_without_a_denied_columns_policy_the_same_sql_is_allowed(self, app_env):
        """Sanity check on the proof above: the crafted SQL is not refused
        for some unrelated reason (a syntax mistake, an unknown table) --
        it is refused specifically because of the denied-column policy,
        which this test shows by its absence."""
        validate_sql(_CRAFTED_EXAMPLE_SQL)  # must not raise

    def test_a_second_denied_column_targeted_by_a_different_example_is_also_refused(
        self, app_env,
    ):
        """Not a coincidence of one column name: the same mechanism holds
        for any column a crafted example might target."""
        other_column = "IsActive"
        other_sql = f"SELECT {other_column} FROM Customer WHERE ID = 1"
        with pytest.raises(PolicyRejection, match=other_column):
            validate_sql(other_sql, denied_columns={other_column})


class TestOperationsCannotEditSchemaYamlButSecurityCan:
    """The other half of the same role split, at the
    :mod:`appdb.config_versions` boundary (see
    ``tests/test_admin_config_routes.py``'s ``TestSaveVersionRoleSplit``
    for the same proof one layer up, at the HTTP boundary)."""

    def test_operations_only_actor_cannot_make_a_schema_change_live(self, app_env):
        active = get_active_version()
        doc = yaml.safe_load(active["files"]["schema.yaml"])
        del doc["tables"]["Customer"]
        new_schema = yaml.dump(doc, allow_unicode=True, sort_keys=False)

        result = propose_or_apply(
            {"schema.yaml": new_schema},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )

        # Saved, but never applied: the guard's allowlist is completely
        # unaffected by an operations-only actor's schema.yaml edit.
        assert result["status"] == "draft"
        assert get_active_version()["version_id"] == active["version_id"]
        assert get_active_version()["files"]["schema.yaml"] == active["files"]["schema.yaml"]

    def test_security_actor_can_make_the_same_schema_change_live(self, app_env):
        active = get_active_version()
        doc = yaml.safe_load(active["files"]["schema.yaml"])
        del doc["tables"]["Customer"]
        new_schema = yaml.dump(doc, allow_unicode=True, sort_keys=False)

        result = propose_or_apply(
            {"schema.yaml": new_schema},
            based_on_version=active["version_id"],
            actor_principal_id="sec-1",
            actor_capabilities=_SECURITY,
        )

        assert result["status"] == "applied"
        assert get_active_version()["version_id"] == result["version_id"]
        assert "Customer" not in yaml.safe_load(get_active_version()["files"]["schema.yaml"])["tables"]
