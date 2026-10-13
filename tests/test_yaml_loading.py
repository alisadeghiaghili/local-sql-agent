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


def _doc_with_duplicates(n: int) -> str:
    """``n`` keys ``k00``..., each written twice; ``k00`` on lines 1 and 2."""
    return "".join(f"k{i:02d}: 1\nk{i:02d}: 2\n" for i in range(n))


class TestEveryDuplicateIsReported:
    def test_one_duplicate_keeps_the_single_key_message(self):
        with pytest.raises(yaml.YAMLError) as excinfo:
            safe_load_strict("a: 1\nb: 2\na: 3\n")
        assert str(excinfo.value) == "duplicate key 'a' (first on line 1, again on line 3)"

    def test_several_duplicates_are_listed_together_in_file_order(self):
        text = "b: 1\na: 1\nb: 2\nc: 1\na: 2\nc: 2\n"
        with pytest.raises(yaml.YAMLError) as excinfo:
            safe_load_strict(text)
        assert str(excinfo.value) == (
            "3 duplicate keys: 'b' (lines 1, 3); 'a' (lines 2, 5); 'c' (lines 4, 6)"
        )

    def test_a_key_written_three_times_lists_all_its_lines(self):
        text = "a: 1\nb: 1\na: 2\nb: 2\na: 3\n"
        with pytest.raises(yaml.YAMLError) as excinfo:
            safe_load_strict(text)
        assert str(excinfo.value) == (
            "2 duplicate keys: 'a' (lines 1, 3, 5); 'b' (lines 2, 4)"
        )

    def test_a_lone_key_written_three_times_names_every_later_line(self):
        with pytest.raises(yaml.YAMLError) as excinfo:
            safe_load_strict("a: 1\na: 2\na: 3\n")
        assert str(excinfo.value) == (
            "duplicate key 'a' (first on line 1, again on lines 2, 3)"
        )

    def test_at_most_twenty_are_listed_then_a_count_of_the_rest(self):
        with pytest.raises(yaml.YAMLError) as excinfo:
            safe_load_strict(_doc_with_duplicates(23))
        message = str(excinfo.value)
        assert message.startswith("23 duplicate keys: 'k00' (lines 1, 2); 'k01' (lines 3, 4)")
        assert message.count(" (lines ") == 20
        assert "'k19' (lines 39, 40)" in message
        assert "'k20'" not in message
        assert message.endswith("; ... and 3 more")

    def test_exactly_twenty_have_no_tail(self):
        with pytest.raises(yaml.YAMLError) as excinfo:
            safe_load_strict(_doc_with_duplicates(20))
        message = str(excinfo.value)
        assert message.startswith("20 duplicate keys: ")
        assert "more" not in message

    def test_nested_mappings_are_reported_with_the_top_level(self):
        text = (
            "tables:\n"            # 1
            "  A:\n"               # 2
            "    x: 1\n"           # 3
            "    x: 2\n"           # 4
            "  B:\n"               # 5
            "    y: 1\n"           # 6
            "tables2:\n"           # 7
            "  inner: {k: 1, k: 2}\n"  # 8
            "tables:\n"            # 9
            "  C: {}\n"            # 10
        )
        with pytest.raises(yaml.YAMLError) as excinfo:
            safe_load_strict(text)
        assert str(excinfo.value) == (
            "3 duplicate keys: 'tables' (lines 1, 9); 'x' (lines 3, 4); 'k' (lines 8, 8)"
        )

    def test_same_key_in_two_sibling_mappings_is_two_duplicates(self):
        text = "p:\n  k: 1\n  k: 2\nq:\n  k: 1\n  k: 2\n"
        with pytest.raises(yaml.YAMLError) as excinfo:
            safe_load_strict(text)
        assert str(excinfo.value) == "2 duplicate keys: 'k' (lines 2, 3); 'k' (lines 5, 6)"

    def test_a_block_overridden_by_a_later_duplicate_is_still_checked(self):
        # The first ``a`` block loses to the second, but a repeat inside it
        # is still an operator mistake.
        text = "a:\n  x: 1\n  x: 2\na:\n  y: 1\n"
        with pytest.raises(yaml.YAMLError) as excinfo:
            safe_load_strict(text)
        assert str(excinfo.value) == "2 duplicate keys: 'a' (lines 1, 4); 'x' (lines 2, 3)"

    def test_duplicate_inside_a_list_item_is_reported(self):
        with pytest.raises(yaml.YAMLError, match=r"duplicate key 'n' \(first on line 2, again on line 3\)"):
            safe_load_strict("items:\n  - n: 1\n    n: 2\n")


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

    def test_every_duplicate_reaches_the_operator_unchanged(self, tmp_path):
        text = "tables:\n  A: {}\n  B: {}\n  A: {}\n  B: {}\nrelationships: []\n"
        expected = "2 duplicate keys: 'A' (lines 2, 4); 'B' (lines 3, 5)"
        with pytest.raises(ValueError) as from_text:
            validate_schema_yaml_text(text)
        assert str(from_text.value) == f"[schema.yaml] {expected}"
        (tmp_path / "schema.yaml").write_text(text, encoding="utf-8")
        with override_settings(project_config_dir=str(tmp_path)):
            with pytest.raises(ValueError) as from_disk:
                load_schema()
        assert str(from_disk.value) == f"[schema.yaml] {expected}"
        with pytest.raises(ValueError) as from_datasources:
            validate_datasources_yaml_text("datasources: {}\ndatasources: {}\nx: 1\nx: 2\n")
        assert str(from_datasources.value) == (
            "[datasources.yaml] is not valid YAML: "
            "2 duplicate keys: 'datasources' (lines 1, 2); 'x' (lines 3, 4)"
        )

    def test_a_clean_file_still_loads(self):
        cfg = validate_datasources_yaml_text(
            "datasources:\n  main:\n    url_env: DB_URL_MAIN\n"
        )
        assert cfg.default_name == "main"
