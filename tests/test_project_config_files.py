# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``core.project_config_files``: the list of files a deployment must have.

The list lives apart from the loaders because the loaders cannot report on a
missing file without raising on it. These tests keep the two from drifting:
a file a loader requires and the list omits is what let
``scripts/verify_deployment.py`` check six files of ten.
"""

from __future__ import annotations

import re
from pathlib import Path

from appdb.config_versions import CONFIG_FILENAMES
from core.project_config_files import (
    REQUIRED_PROJECT_CONFIG_FILES,
    missing_project_config_files,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_DIR = _REPO_ROOT / "project_config.example"


class TestTheListMatchesTheLoaders:
    def test_it_is_the_versioned_bundle_plus_the_system_prompt(self):
        assert set(REQUIRED_PROJECT_CONFIG_FILES) == set(CONFIG_FILENAMES) | {"system_prompt.md"}

    def test_it_has_no_repeats_and_ten_entries(self):
        assert len(REQUIRED_PROJECT_CONFIG_FILES) == len(set(REQUIRED_PROJECT_CONFIG_FILES)) == 10

    def test_every_file_a_config_loader_reads_is_listed(self):
        source = (_REPO_ROOT / "knowledge" / "config_loader.py").read_text(encoding="utf-8")
        read_by_loader = set(re.findall(r'_load_validated\(\s*"([\w.]+)"', source))
        # schema.yaml is read by schema_data.registry, the prompt by load_system_prompt.
        read_by_loader |= {"schema.yaml", "system_prompt.md"}
        assert read_by_loader == set(REQUIRED_PROJECT_CONFIG_FILES)

    def test_the_example_directory_has_every_one_of_them(self):
        assert missing_project_config_files(_EXAMPLE_DIR) == []


class TestMissingProjectConfigFiles:
    def test_a_directory_that_does_not_exist_lacks_everything(self, tmp_path):
        assert missing_project_config_files(tmp_path / "nope") == list(REQUIRED_PROJECT_CONFIG_FILES)

    def test_it_names_only_what_is_absent(self, tmp_path):
        for name in REQUIRED_PROJECT_CONFIG_FILES:
            if name not in ("metrics.yaml", "system_prompt.md"):
                (tmp_path / name).write_text("{}", encoding="utf-8")
        assert missing_project_config_files(tmp_path) == ["metrics.yaml", "system_prompt.md"]

    def test_a_directory_with_the_name_of_a_file_does_not_count(self, tmp_path):
        (tmp_path / "metrics.yaml").mkdir()
        assert "metrics.yaml" in missing_project_config_files(tmp_path)
