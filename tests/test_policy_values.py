# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``core.policy_values`` and the preflight check ``Session and memory policies use real values``.

A first install copies ``project_config.example/``, whose ``session_policy.yaml``
and ``memory_policy.yaml`` hold made-up options, and nothing used to say so:
every real value an analyst tried to pin was refused. The check says it, names
the file and the key and never prints a value. The shipped example must still
pass its own preflight, and a deployment whose policy files already hold real
values (or an empty, open option set) must not be flagged.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

import config as cfg
import scripts.verify_deployment as verify
from core import config_doctor as doctor
from core import policy_values as values

_EXAMPLE = Path(__file__).resolve().parent.parent / "project_config.example"

MEMORY_TEMPLATE = (_EXAMPLE / "memory_policy.yaml").read_text(encoding="utf-8")

REAL_MEMORY = """\
keys:
  scope:
    filter_key: "Ring"
    field_name: "ring"
    column: "Name"
    options:
      - "Channel One"
      - "Channel Two"
    max_length: 120
"""

REAL_SESSION = """\
default_scope:
  filter_key: "Ring"
  field_name: "ring"
  default_label: "All channels"
  options:
    - "All channels"
    - "Channel One"
    - "Channel Two"
  clarification_prompt: "Which channel do you mean?"
"""


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """A copy of the shipped template: what a first install starts from."""
    target = tmp_path / "project_config"
    shutil.copytree(_EXAMPLE, target)
    return target


def _add_tables(directory: Path, entries: str) -> None:
    """Put table entries at the end of ``schema.yaml``'s ``tables`` map (before ``relationships:``)."""
    path = directory / "schema.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("\nrelationships:", "\n" + entries + "\nrelationships:", 1), encoding="utf-8")


def _run(directory: Path):
    with cfg.override_settings(project_config_dir=str(directory)):
        return verify.check_policies_use_real_values()


def _write(directory: Path, memory: str | None = None, session: str | None = None) -> None:
    if memory is not None:
        (directory / "memory_policy.yaml").write_text(memory, encoding="utf-8")
    if session is not None:
        (directory / "session_policy.yaml").write_text(session, encoding="utf-8")


class TestTheTemplateIsNotRealValues:
    def test_a_copy_of_the_template_fails_and_names_both_files_and_keys(self, config_dir):
        result = _run(config_dir)

        assert result.status == "FAIL"
        assert "session_policy.yaml default_scope: options are the sample values" in result.detail
        assert "memory_policy.yaml keys.scope: options are the sample values" in result.detail

    def test_it_points_to_the_command_and_the_runbook(self, config_dir):
        detail = _run(config_dir).detail

        assert "python scripts/draft_policies.py" in detail
        assert "docs/deployment-runbook.md section 2.5" in detail

    def test_it_never_prints_an_option_value(self, config_dir):
        detail = _run(config_dir).detail

        for sample in ("Ring A", "Ring B", "All rings"):
            assert sample not in detail

    def test_only_the_file_that_is_still_the_template_is_named(self, config_dir):
        _write(config_dir, memory=REAL_MEMORY)

        result = _run(config_dir)

        assert result.status == "FAIL"
        assert "session_policy.yaml default_scope" in result.detail
        assert "memory_policy.yaml keys" not in result.detail

    def test_the_shipped_example_passes_its_own_preflight(self):
        result = _run(_EXAMPLE)

        assert result.status == "PASS", result.detail
        assert result.detail.startswith("2 place(s) name a table and column of schema.yaml")

    def test_the_example_reached_by_a_relative_path_passes_too(self):
        with cfg.override_settings(project_config_dir="project_config.example"):
            assert verify.check_policies_use_real_values().status == "PASS"


class TestRealValuesPass:
    def test_real_options_pass(self, config_dir):
        _write(config_dir, REAL_MEMORY, REAL_SESSION)

        result = _run(config_dir)

        assert result.status == "PASS", result.detail
        assert "advice:" not in result.detail

    def test_one_changed_value_is_enough_to_not_be_the_template(self, config_dir):
        # Same session options as the template except one value: a person edited it.
        edited = MEMORY_TEMPLATE.replace('"Ring B"', '"Ring C"')
        session = (_EXAMPLE / "session_policy.yaml").read_text(encoding="utf-8").replace('"Ring B"', '"Ring C"')
        _write(config_dir, edited, session)

        assert _run(config_dir).status == "PASS"

    def test_an_open_option_set_is_not_the_template(self, config_dir):
        _write(config_dir, REAL_MEMORY.replace('    options:\n      - "Channel One"\n      - "Channel Two"\n', ""), REAL_SESSION)

        assert _run(config_dir).status == "PASS"

    def test_the_order_of_the_template_options_does_not_hide_them(self, config_dir):
        reordered = MEMORY_TEMPLATE.replace('      - "Ring A"\n      - "Ring B"\n', '      - "Ring B"\n      - "Ring A"\n')
        _write(config_dir, reordered, REAL_SESSION)

        result = _run(config_dir)

        assert result.status == "FAIL"
        assert "memory_policy.yaml keys.scope" in result.detail


class TestTheDimensionMustExist:
    def test_a_filter_key_that_is_no_table_fails(self, config_dir):
        _write(config_dir, REAL_MEMORY.replace('"Ring"', '"Nowhere"'), REAL_SESSION)

        result = _run(config_dir)

        assert result.status == "FAIL"
        assert "memory_policy.yaml keys.scope: filter_key 'Nowhere' names no table of schema.yaml" in result.detail
        assert "session_policy.yaml default_scope" not in result.detail

    def test_the_session_policy_filter_key_is_checked_too(self, config_dir):
        _write(config_dir, REAL_MEMORY, REAL_SESSION.replace('filter_key: "Ring"', 'filter_key: "Nowhere"'))

        result = _run(config_dir)

        assert result.status == "FAIL"
        assert "session_policy.yaml default_scope: filter_key 'Nowhere' names no table" in result.detail

    def test_a_column_that_is_not_in_the_table_fails(self, config_dir):
        _write(config_dir, REAL_MEMORY.replace('column: "Name"', 'column: "Title"'), REAL_SESSION)

        result = _run(config_dir)

        assert result.status == "FAIL"
        assert "memory_policy.yaml keys.scope: column 'Title' is not a column of Ring in schema.yaml" in result.detail

    def test_the_table_is_found_the_way_the_application_finds_it(self, config_dir):
        for spelling in ("Ring", "[Ring]", "ring", "RING"):
            _write(config_dir, REAL_MEMORY.replace('"Ring"', f'"{spelling}"'),
                   REAL_SESSION.replace('"Ring"', f'"{spelling}"'))
            assert _run(config_dir).status == "PASS", spelling

    def test_a_table_keyed_with_its_schema_is_found_bare_and_qualified(self, config_dir):
        _add_tables(
            config_dir,
            "  sales.Region:\n    description: Regions of the sales schema\n"
            "    columns:\n      ID: Primary key\n      Name: Display name\n",
        )
        for spelling in ("Region", "sales.Region", "[sales].[region]"):
            _write(config_dir, REAL_MEMORY.replace('"Ring"', f'"{spelling}"'),
                   REAL_SESSION.replace('"Ring"', f'"{spelling}"'))
            assert _run(config_dir).status == "PASS", spelling

    def test_a_qualifier_that_names_another_schema_is_no_table(self, config_dir):
        _write(config_dir, REAL_MEMORY.replace('"Ring"', '"sales.Ring"'), REAL_SESSION)

        assert _run(config_dir).status == "FAIL"

    def test_the_column_is_found_whatever_its_letter_case(self, config_dir):
        _write(config_dir, REAL_MEMORY.replace('column: "Name"', 'column: "name"'), REAL_SESSION)

        assert _run(config_dir).status == "PASS"

    def test_a_table_that_lists_no_column_gives_no_verdict_on_the_column(self, config_dir):
        _add_tables(config_dir, "  Plain:\n    description: A table described without its columns\n")
        _write(config_dir, REAL_MEMORY.replace('"Ring"', '"Plain"'), REAL_SESSION.replace('"Ring"', '"Plain"'))

        assert _run(config_dir).status == "PASS"


class TestSkips:
    def test_a_missing_directory_skips(self, tmp_path):
        assert _run(tmp_path / "missing").status == "SKIP"

    def test_a_policy_file_that_does_not_load_skips_and_says_where_to_look(self, config_dir):
        _write(config_dir, memory="keys: [not, a, mapping]\n")

        result = _run(config_dir)

        assert result.status == "SKIP"
        assert "memory_policy.yaml" in result.detail
        assert "project_config/ loads" in result.detail
        assert "not, a, mapping" not in result.detail  # the loader's quotation of the file stays out

    def test_it_reads_files_only(self, config_dir):
        with patch("database.connection.get_engine", side_effect=AssertionError("database touched")):
            assert _run(config_dir).status == "FAIL"
        assert "File reads only" in " ".join((verify.check_policies_use_real_values.__doc__ or "").split())


class TestRegistration:
    def test_it_is_one_global_check_after_the_entity_check_and_is_not_deep(self):
        from api.admin_routes import _DEEP_CHECK_NAMES

        names = [c.__name__ for c in verify.build_checks()]

        assert names.count("check_policies_use_real_values") == 1
        assert names.index("check_policies_use_real_values") == names.index("check_entities_match_schema_keys") + 1
        assert "check_policies_use_real_values" not in _DEEP_CHECK_NAMES


class TestAdviceFromTheCachedDimension:
    def _advice(self, config_dir, cached):
        _write(config_dir, REAL_MEMORY, REAL_SESSION)
        with patch.object(verify, "_cached_dimension_values", lambda table, column: cached):
            return _run(config_dir)

    def test_options_that_are_not_cached_are_counted_and_never_listed(self, config_dir):
        result = self._advice(config_dir, ["Channel One", "Channel Three", "Channel Four"])

        assert result.status == "PASS"
        assert "advice:" in result.detail
        assert "memory_policy.yaml keys.scope: 1 of 2 option(s) not among the 3 cached value(s) of Ring.Name" in result.detail
        assert "session_policy.yaml default_scope: 1 of 2 option(s)" in result.detail
        for value in ("Channel One", "Channel Two", "Channel Three", "All channels"):
            assert value not in result.detail

    def test_the_default_label_is_not_expected_in_the_warehouse(self, config_dir):
        result = self._advice(config_dir, ["Channel One", "Channel Two"])

        assert "advice:" not in result.detail

    def test_persian_and_arabic_spellings_of_a_value_count_as_the_same(self, config_dir):
        _write(
            config_dir,
            REAL_MEMORY.replace("Channel One", "علی").replace("Channel Two", "کار"),
            REAL_SESSION.replace("Channel One", "علی").replace("Channel Two", "کار"),
        )
        arabic = ["علي", "كار"]
        with patch.object(verify, "_cached_dimension_values", lambda table, column: arabic):
            result = _run(config_dir)

        assert "advice:" not in result.detail

    def test_nothing_cached_means_no_advice(self, config_dir):
        assert "advice:" not in self._advice(config_dir, None).detail

    def test_the_real_lookup_does_not_import_the_application_to_look(self):
        import sys

        with patch.dict(sys.modules):
            sys.modules.pop("retrieval.dimension_vocabulary", None)
            assert verify._cached_dimension_values("Ring", "Name") is None
            assert "retrieval.dimension_vocabulary" not in sys.modules


class TestCoreHelpers:
    def test_the_template_directory_is_recognised_but_a_copy_is_not(self, config_dir):
        assert values.is_shipped_directory(_EXAMPLE)
        assert not values.is_shipped_directory(config_dir)

    def test_a_filter_key_is_resolved_like_a_configured_table_name(self):
        view = doctor.read_schema(_EXAMPLE)

        assert doctor.resolve_table_keys(view, "Ring") == ("Ring",)
        assert doctor.resolve_table_keys(view, "[ring]") == ("Ring",)
        assert doctor.resolve_table_keys(view, "sales.Ring") == ()
        assert doctor.resolve_table_keys(view, "Nothing") == ()
