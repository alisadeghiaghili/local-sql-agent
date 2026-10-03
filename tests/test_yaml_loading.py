# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Unit tests for ``core.yaml_loading.safe_load_strict``.

``yaml.safe_load`` keeps the last of two identical keys without a word, so
a config file that repeats ``datasources:`` loses its first block with no
error. These tests pin that the strict loader refuses it, names the key and
both lines, and still accepts every spelling that is not a duplicate: merge
keys with an explicit override, the same key in sibling mappings, and list
items that share their keys.

The last two classes go through the public validators of ``datasources.yaml``
and ``schema.yaml`` (and the on-disk ``load_schema``) to prove an operator
sees the refusal with the file name.
"""

from __future__ import annotations

import io

import pytest
import yaml

from config import override_settings
from core.yaml_loading import safe_load_strict
from database.datasources import validate_datasources_yaml_text
from schema_data.registry import load_schema, validate_schema_yaml_text


class TestDuplicateKeysAreRefused:
    def test_duplicate_top_level_key(self):
        with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key 'a'"):
            safe_load_strict("a: 1\nb: 2\na: 3\n")

    def test_duplicate_nested_key(self):
        text = "outer:\n  inner:\n    k: 1\n    k: 2\n"
        with pytest.raises(yaml.YAMLError, match="duplicate key 'k'"):
            safe_load_strict(text)

    def test_duplicate_in_a_flow_mapping(self):
        with pytest.raises(yaml.YAMLError, match="duplicate key 'x'"):
            safe_load_strict("m: {x: 1, y: 2, x: 3}\n")

    def test_message_names_both_lines_one_based(self):
        text = "head: 1\ndatasources:\n  a: {}\nother: 2\ndatasources:\n  b: {}\n"
        with pytest.raises(yaml.YAMLError) as excinfo:
            safe_load_strict(text)
        assert str(excinfo.value) == (
            "duplicate key 'datasources' (first on line 2, again on line 5)"
        )

    def test_keys_are_compared_by_value_not_spelling(self):
        with pytest.raises(yaml.YAMLError, match=r"duplicate key 1 "):
            safe_load_strict("1: a\n0x1: b\n")

    def test_quoted_and_plain_spellings_collide(self):
        with pytest.raises(yaml.YAMLError, match="duplicate key 'a'"):
            safe_load_strict("a: 1\n'a': 2\n")

    def test_is_a_yaml_error_so_existing_handlers_keep_working(self):
        assert issubclass(yaml.constructor.ConstructorError, yaml.YAMLError)
        with pytest.raises(yaml.YAMLError):
            safe_load_strict("a: 1\na: 2\n")

    def test_unhashable_key_still_gets_pyyamls_own_error(self):
        with pytest.raises(yaml.YAMLError, match="unhashable"):
            safe_load_strict("? [1, 2]\n: v\n")


class TestNonDuplicatesAreAccepted:
    def test_same_key_in_sibling_mappings(self):
        assert safe_load_strict("a: {k: 1}\nb: {k: 2}\n") == {
            "a": {"k": 1},
            "b": {"k": 2},
        }

    def test_list_of_mappings_with_the_same_keys(self):
        text = "items:\n  - name: x\n    v: 1\n  - name: y\n    v: 2\n"
        assert safe_load_strict(text) == {
            "items": [{"name": "x", "v": 1}, {"name": "y", "v": 2}]
        }

    def test_merge_key_is_expanded(self):
        text = "base: &b {x: 1, y: 2}\nuse:\n  <<: *b\n  z: 3\n"
        assert safe_load_strict(text)["use"] == {"x": 1, "y": 2, "z": 3}

    def test_explicit_key_may_override_a_merged_one(self):
        text = "base: &b {x: 1, y: 2}\nuse:\n  <<: *b\n  x: 9\n"
        assert safe_load_strict(text)["use"] == {"x": 9, "y": 2}

    def test_merge_of_a_list_of_anchors_with_overlapping_keys(self):
        text = "a: &a {x: 1}\nb: &b {x: 2, y: 3}\nuse: {<<: [*a, *b], z: 4}\n"
        assert safe_load_strict(text)["use"] == {"x": 1, "y": 3, "z": 4}

    def test_override_inside_a_merge_target_constructed_after_its_merger(self):
        # PyYAML builds shallow mappings before deeper ones, so ``late`` is
        # constructed first and flattens ``anchor`` in place; ``anchor``'s
        # own explicit override must not then read as a duplicate.
        text = (
            "base: &base {x: 1}\n"
            "mid:\n"
            "  deep:\n"
            "    anchor: &a {<<: *base, x: 2}\n"
            "late: {<<: *a, k: 1}\n"
        )
        assert safe_load_strict(text) == {
            "base": {"x": 1},
            "mid": {"deep": {"anchor": {"x": 2}}},
            "late": {"x": 2, "k": 1},
        }

    def test_a_duplicate_inside_a_merge_target_is_still_refused(self):
        text = "base: &b\n  x: 1\n  x: 2\nuse: {<<: *b}\n"
        with pytest.raises(yaml.YAMLError, match="duplicate key 'x'"):
            safe_load_strict(text)

    def test_empty_document_is_none_like_safe_load(self):
        assert safe_load_strict("") is None
        assert safe_load_strict("# only a comment\n") is None


class TestInputShapes:
    def test_text_stream(self):
        assert safe_load_strict(io.StringIO("a: 1\nb: [1, 2]\n")) == {
            "a": 1,
            "b": [1, 2],
        }

    def test_text_stream_with_a_duplicate(self):
        with pytest.raises(yaml.YAMLError, match="first on line 1, again on line 2"):
            safe_load_strict(io.StringIO("a: 1\na: 2\n"))

    def test_bytes(self):
        assert safe_load_strict(b"a: 1\n") == {"a": 1}

    def test_result_matches_safe_load_for_a_valid_document(self):
        text = "a: 1\nb: [x, {c: 2.5}]\nd: null\ne: 2026-10-03\nf: 'yes'\n"
        assert safe_load_strict(text) == yaml.safe_load(text)

    def test_unsafe_tags_are_still_refused(self):
        with pytest.raises(yaml.YAMLError):
            safe_load_strict("a: !!python/object/apply:os.getcwd []\n")


_DUPLICATE_DATASOURCES = (
    "datasources:\n"
    "  main:\n"
    "    url_env: DB_URL_MAIN\n"
    "other: 1\n"
    "datasources:\n"
    "  reporting:\n"
    "    url_env: DB_URL_REPORTING\n"
)

_DUPLICATE_SCHEMA = (
    "tables:\n"
    "  Customer:\n"
    "    columns: {ID: pk}\n"
    "relationships: []\n"
    "tables:\n"
    "  Order:\n"
    "    columns: {ID: pk}\n"
)


class TestOperatorSeesTheRefusal:
    def test_datasources_yaml_text_is_refused_with_the_file_name(self):
        with pytest.raises(ValueError) as excinfo:
            validate_datasources_yaml_text(_DUPLICATE_DATASOURCES)
        assert str(excinfo.value) == (
            "[datasources.yaml] is not valid YAML: "
            "duplicate key 'datasources' (first on line 1, again on line 5)"
        )

    def test_schema_yaml_text_is_refused_with_the_file_name(self):
        with pytest.raises(ValueError) as excinfo:
            validate_schema_yaml_text(_DUPLICATE_SCHEMA)
        assert str(excinfo.value) == (
            "[schema.yaml] duplicate key 'tables' (first on line 1, again on line 5)"
        )

    def test_schema_yaml_on_disk_is_refused_with_the_file_name(self, tmp_path):
        (tmp_path / "schema.yaml").write_text(_DUPLICATE_SCHEMA, encoding="utf-8")
        with override_settings(project_config_dir=str(tmp_path)):
            with pytest.raises(ValueError) as excinfo:
                load_schema()
        assert str(excinfo.value) == (
            "[schema.yaml] duplicate key 'tables' (first on line 1, again on line 5)"
        )

    def test_a_clean_file_still_loads(self):
        cfg = validate_datasources_yaml_text(
            "datasources:\n  main:\n    url_env: DB_URL_MAIN\n"
        )
        assert cfg.default_name == "main"
