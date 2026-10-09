# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``import knowledge`` is lazy; server start-up still fails fast.

``knowledge/__init__.py`` used to import five submodules at the top, which
read five ``project_config/`` files the moment anything under ``knowledge``
was imported -- contradicting every submodule's own "import never fails"
docstring, and making the loaders needed to *report* a missing file
unimportable. It now re-exports the names on first access.

Fail-fast must not weaken: the API still has to refuse to start on a broken
``project_config/``. It does, because the modules it imports bind the
knowledge names at import time. The start-up tests below run in a fresh
interpreter, since an import-time effect cannot be observed in-process once
``knowledge`` is cached in ``sys.modules``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE_DIR = _REPO_ROOT / "project_config.example"
_BREAKABLE = "business_rules.yaml"


def _run(code: str, config_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run *code* in a fresh interpreter with ``PROJECT_CONFIG_DIR`` set.

    Args:
        code: Python source passed to ``python -c``.
        config_dir: Directory to use as ``PROJECT_CONFIG_DIR``.

    Returns:
        The completed process (stdout and stderr captured as text).
    """
    env = {**os.environ, "PROJECT_CONFIG_DIR": str(config_dir)}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


@pytest.fixture
def empty_config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "empty_project_config"
    directory.mkdir()
    return directory


class TestImportIsLazy:
    def test_importing_knowledge_with_an_empty_config_dir_does_not_raise(
        self, empty_config_dir
    ):
        done = _run("import knowledge, knowledge.config_loader; print('ok')", empty_config_dir)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "ok"

    @pytest.mark.parametrize(
        "module", ["aliases", "business_rules", "entities", "examples", "metrics"]
    )
    def test_each_submodule_imports_with_an_empty_config_dir(self, empty_config_dir, module):
        done = _run(f"import knowledge.{module}; print('ok')", empty_config_dir)
        assert done.returncode == 0, done.stderr

    def test_importing_the_package_loads_no_config_submodule(self, empty_config_dir):
        code = (
            "import sys, knowledge\n"
            "loaded = [m for m in ('aliases','business_rules','entities','examples','metrics')"
            " if f'knowledge.{m}' in sys.modules]\n"
            "print(loaded)\n"
        )
        done = _run(code, empty_config_dir)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "[]"

    def test_first_access_with_an_empty_config_dir_raises_config_not_found(
        self, empty_config_dir
    ):
        code = (
            "import knowledge\n"
            "from knowledge.config_loader import ConfigNotFoundError\n"
            "try:\n"
            "    knowledge.ENTITIES\n"
            "except ConfigNotFoundError as exc:\n"
            "    print('ConfigNotFoundError', 'entities.yaml' in str(exc))\n"
        )
        done = _run(code, empty_config_dir)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "ConfigNotFoundError True"


class TestPublicApiIsUnchanged:
    def test_all_lists_the_same_five_names(self):
        import knowledge

        assert knowledge.__all__ == [
            "RING_ALIASES",
            "BUSINESS_RULES",
            "ENTITIES",
            "EXAMPLES",
            "METRICS",
        ]

    def test_package_attributes_are_the_submodule_objects(self):
        import knowledge
        import knowledge.aliases
        import knowledge.business_rules
        import knowledge.entities
        import knowledge.examples
        import knowledge.metrics

        assert knowledge.RING_ALIASES is knowledge.aliases.RING_ALIASES
        assert knowledge.BUSINESS_RULES is knowledge.business_rules.BUSINESS_RULES
        assert knowledge.ENTITIES is knowledge.entities.ENTITIES
        assert knowledge.EXAMPLES is knowledge.examples.EXAMPLES
        assert knowledge.METRICS is knowledge.metrics.METRICS

    def test_from_import_and_star_import_still_work(self):
        namespace: dict[str, object] = {}
        exec("from knowledge import *", namespace)  # noqa: S102 - exercising the public API
        for name in ("RING_ALIASES", "BUSINESS_RULES", "ENTITIES", "EXAMPLES", "METRICS"):
            assert name in namespace
        from knowledge import ENTITIES  # noqa: F401

    def test_dir_lists_the_lazy_names(self):
        import knowledge

        assert {"RING_ALIASES", "ENTITIES"} <= set(dir(knowledge))

    def test_unknown_attribute_raises_attribute_error(self):
        import knowledge

        with pytest.raises(AttributeError, match="NO_SUCH_NAME"):
            knowledge.NO_SUCH_NAME  # noqa: B018


class TestServerStartupStillFailsFast:
    """``import api.server`` is what ``uvicorn api.server:app`` does first."""

    def test_control_the_example_config_imports_cleanly(self):
        done = _run("import api.server; print('ok')", _EXAMPLE_DIR)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "ok"

    def test_an_empty_config_dir_stops_the_server_import(self, empty_config_dir):
        done = _run("import api.server", empty_config_dir)
        assert done.returncode != 0
        assert "ConfigNotFoundError" in done.stderr

    @pytest.mark.parametrize(
        ("break_it", "expected"),
        [
            (lambda p: p.write_text("rules: [not, a, mapping\n", encoding="utf-8"), "business_rules.yaml"),
            (lambda p: p.unlink(), "business_rules.yaml"),
        ],
        ids=["invalid-yaml", "missing-file"],
    )
    def test_a_broken_file_stops_the_server_import_naming_the_file(
        self, tmp_path, break_it, expected
    ):
        broken = tmp_path / "broken_project_config"
        shutil.copytree(_EXAMPLE_DIR, broken)
        break_it(broken / _BREAKABLE)

        done = _run("import api.server", broken)

        assert done.returncode != 0
        assert expected in done.stderr
