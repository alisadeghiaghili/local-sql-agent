# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Admin panel phase 3, spec §6: the prefix version tracks the config
version, and only the config version.

``docs/admin-panel-architecture.md`` §6: "Today an innocuous-looking edit
changes [the static prompt prefix], invalidates the KV cache, and halves
throughput with no error anywhere." The fix is
:func:`prompt_engine.static_prefix.prefix_version_for_config`: derive the
identity from the ``project_config/`` bundle's own version identifier
(:func:`appdb.config_versions.get_active_version_id`), not from re-hashing
whatever happens to be loaded in memory. This module is the two-property
test the spec calls for directly: it moves when the config version does,
and it does not move when an unrelated setting does -- exercised here
against the real, versioned bundle (:mod:`appdb.config_versions`), not a
bare integer stand-in.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import config as cfg
from appdb.config_versions import get_active_version, get_active_version_id, propose_or_apply
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache
from prompt_engine.static_prefix import prefix_version, prefix_version_for_config
from security.auth import OPERATIONS_CAPABILITY

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"
_OPS = frozenset({OPERATIONS_CAPABILITY})
_SYSTEM_PROMPT = "You are a T-SQL expert."


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


class TestPureFunctionProperties:
    """The function's own contract, independent of appdb.config_versions --
    any hashable config_version identifier, not only a real bundle's."""

    def test_same_inputs_produce_the_same_version(self):
        v1 = prefix_version_for_config(_SYSTEM_PROMPT, 1)
        v2 = prefix_version_for_config(_SYSTEM_PROMPT, 1)
        assert v1 == v2

    def test_a_different_config_version_produces_a_different_result(self):
        v1 = prefix_version_for_config(_SYSTEM_PROMPT, 1)
        v2 = prefix_version_for_config(_SYSTEM_PROMPT, 2)
        assert v1 != v2

    def test_it_does_not_collapse_to_the_plain_content_hash(self):
        """It must genuinely depend on config_version, not merely proxy
        prefix_version(system_prompt) while ignoring its second argument."""
        assert prefix_version_for_config(_SYSTEM_PROMPT, 1) != prefix_version(_SYSTEM_PROMPT)


class TestTracksTheRealConfigVersion:
    """Against a real, versioned project_config/ bundle."""

    def test_moves_when_the_config_version_changes(self, app_env):
        active = get_active_version()
        before = prefix_version_for_config(_SYSTEM_PROMPT, get_active_version_id())

        propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"] + "\n# bumps the bundle version\n"},
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )
        after = prefix_version_for_config(_SYSTEM_PROMPT, get_active_version_id())

        assert get_active_version_id() == active["version_id"] + 1
        assert after != before

    def test_does_not_move_for_an_unrelated_setting(self, app_env):
        """An "unrelated setting" is, by construction, anything that is
        not part of the versioned project_config/ bundle at all -- e.g. an
        LLM endpoint setting. Changing one must leave the config version
        (and therefore this function's output) completely untouched."""
        config_version = get_active_version_id()
        before = prefix_version_for_config(_SYSTEM_PROMPT, config_version)

        with cfg.override_settings(openai_model="a-completely-different-model"):
            still_config_version = get_active_version_id()
            after = prefix_version_for_config(_SYSTEM_PROMPT, still_config_version)

        assert still_config_version == config_version
        assert after == before

    def test_repeated_apply_of_byte_identical_content_still_moves_it(self, app_env):
        """Bundle-versioned (spec §1): saving byte-identical content still
        creates a new bundle version (a new row, a new identifier) -- so
        the prefix version moves too, even though the *rendered* prefix
        text has not changed at all. This is the deliberately conservative
        half of §6: a save the panel warns about resets the KV cache
        whether or not the text ended up different, because the identity
        this function reports is the bundle version, not a content diff."""
        active = get_active_version()
        before = prefix_version_for_config(_SYSTEM_PROMPT, get_active_version_id())

        propose_or_apply(
            {"metrics.yaml": active["files"]["metrics.yaml"]},  # byte-identical
            based_on_version=active["version_id"],
            actor_principal_id="ops-1",
            actor_capabilities=_OPS,
        )
        after = prefix_version_for_config(_SYSTEM_PROMPT, get_active_version_id())

        assert get_active_version_id() == active["version_id"] + 1
        assert after != before
