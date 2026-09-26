# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""D4 (2026 hall-filter audit): ``entities.yaml``'s optional ``label`` field
-- the analyst-facing display name a "vocabulary unavailable" warning
(``retrieval.context_retriever.ContextRetriever.retrieve``) uses in place
of the generic phrasing, when a deployment's config sets one.

Covers the parsing/loading layer (``knowledge.config_loader.EntityDefinition``,
``knowledge.entities.ENTITIES``) directly, on a temporary
``project_config/`` this test writes itself -- never the repository's own
``project_config.example/`` content, and no real deployment's entity
names. ``tests/test_context_retriever_value_resolution.py``'s
``TestVocabularyUnavailableWarnings`` covers the warning-text wiring that
*consumes* this field; this file covers the field existing and defaulting
correctly in the first place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import override_settings
from knowledge.config_loader import load_entities

_ENTITIES_YAML_WITH_LABEL = """
entities:
  Widget:
    aliases: ["widget", "\\u062f\\u0633\\u062a\\u06af\\u0627\\u0647"]
    table: "WidgetTable"
    label: "\\u062f\\u0633\\u062a\\u06af\\u0627\\u0647"
  Gadget:
    aliases: ["gadget"]
    table: "GadgetTable"
"""


@pytest.fixture()
def _project_config_dir(tmp_path: Path) -> Path:
    (tmp_path / "entities.yaml").write_text(_ENTITIES_YAML_WITH_LABEL, encoding="utf-8")
    return tmp_path


class TestEntityDefinitionLabel:
    def test_label_is_parsed_when_present(self, _project_config_dir):
        with override_settings(project_config_dir=str(_project_config_dir)):
            cfg = load_entities()
        assert cfg.entities["Widget"].label == "دستگاه"

    def test_label_defaults_to_none_when_absent(self, _project_config_dir):
        with override_settings(project_config_dir=str(_project_config_dir)):
            cfg = load_entities()
        assert cfg.entities["Gadget"].label is None

    def test_entities_dict_exposes_label_for_both_cases(self, _project_config_dir, monkeypatch):
        import knowledge.entities as entities_module

        monkeypatch.setattr(entities_module, "_cache", {})
        with override_settings(project_config_dir=str(_project_config_dir)):
            from knowledge.entities import ENTITIES

        assert ENTITIES["Widget"]["label"] == "دستگاه"
        assert ENTITIES["Gadget"]["label"] is None
        monkeypatch.setattr(entities_module, "_cache", {})
