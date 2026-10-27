# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""The preflight check ``Column restrictions name real columns``, and the look-alike advice.

A plain ``denied_columns`` entry that matches no column of ``schema.yaml``
denies nothing, and before 6.15.1 nothing said so. The check FAILs with the
key id and the entry (the server still starts); when every entry matches, its
PASS detail advises which columns look like a denied name and are not denied.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import config as cfg
from appdb.engine import dispose_app_engine
from appdb.key_store import invalidate_cache, issue_key, update_denied_columns
from scripts.verify_deployment import build_checks, check_column_restrictions_name_real_columns
from security.column_policy import look_alike_columns, schema_column_names, unmatched_bare_entries

NAME = "Column restrictions name real columns"
_EXAMPLE = Path(__file__).resolve().parent.parent / "project_config.example"


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _keys(**by_id) -> str:
    return json.dumps(
        [
            {"id": key_id, "name": key_id, "key_sha256": _sha256(f"raw-{key_id}"), "denied_columns": denied}
            for key_id, denied in by_id.items()
        ],
        ensure_ascii=False,
    )


def _run(keys_json: str, *, config_dir: Path = _EXAMPLE):
    invalidate_cache()
    with cfg.override_settings(api_keys_file="", api_keys_json=keys_json, project_config_dir=str(config_dir)):
        return check_column_restrictions_name_real_columns()


class TestUnmatchedEntries:
    def test_a_typo_fails_naming_the_key_and_the_entry(self):
        result = _run(_keys(**{"analyst-1": ["NatioanlID", "Name"]}))
        assert (result.name, result.status) == (NAME, "FAIL")
        assert "analyst-1" in result.detail
        assert "'NatioanlID'" in result.detail
        assert "'Name'" not in result.detail
        assert "deny nothing" in result.detail
        assert "server starts anyway" in result.detail

    def test_an_inner_space_is_a_different_name(self):
        result = _run(_keys(**{"analyst-1": ["National ID"]}))
        assert result.status == "FAIL" and "'National ID'" in result.detail

    def test_the_entry_is_shown_in_ascii_so_an_invisible_character_is_seen(self):
        result = _run(_keys(**{"analyst-1": ["\u200c"]}))
        assert result.status == "FAIL"
        assert "'\\u200c'" in result.detail and "\u200c" not in result.detail

    def test_two_keys_with_the_same_typo_are_listed_together(self):
        result = _run(_keys(**{"analyst-1": ["Typo"], "analyst-2": ["Typo"], "analyst-3": ["Name"]}))
        assert result.status == "FAIL"
        assert "analyst-1, analyst-2: 'Typo'" in result.detail
        assert "analyst-3" not in result.detail
        assert result.detail.count("'Typo'") == 1

    def test_every_key_is_named_when_the_entries_differ(self):
        result = _run(_keys(**{"analyst-1": ["TypoOne"], "analyst-2": ["TypoTwo"]}))
        assert "analyst-1: 'TypoOne'" in result.detail and "analyst-2: 'TypoTwo'" in result.detail

    def test_a_panel_key_is_checked_too_and_its_hash_is_not_printed(self):
        invalidate_cache()
        _, entry = issue_key("panel-1", "Panel One")
        update_denied_columns(entry["key_sha256"], ["Typo", "Name"])
        result = _run(_keys())
        assert result.status == "FAIL"
        assert "panel-1" in result.detail and "'Typo'" in result.detail
        assert entry["key_sha256"] not in result.detail

    def test_the_hash_of_a_file_key_is_not_printed(self):
        result = _run(_keys(**{"analyst-1": ["Typo"]}))
        assert _sha256("raw-analyst-1") not in result.detail

    def test_an_entry_that_only_needed_cleaning_is_not_a_failure(self):
        result = _run(_keys(**{"analyst-1": ["NationalID\u200f", " Name"]}))
        assert result.status == "PASS"

    def test_matching_ignores_case(self):
        assert _run(_keys(**{"analyst-1": ["nationalid", "NAME"]})).status == "PASS"

    def test_scoped_entries_are_not_judged_here(self):
        assert _run(_keys(**{"analyst-1": ["sales.Customer.Name"]})).status == "PASS"


_PERSIAN = "\u06a9\u062f\u200c\u0645\u0644\u06cc"


def _persian_schema(directory: Path) -> Path:
    (directory / "schema.yaml").write_text(
        "tables:\n  Customer:\n    description: d\n    columns:\n"
        f"      ID: id\n      '{_PERSIAN}': a personal number\n      Name: name\n",
        encoding="utf-8",
    )
    return directory


class TestFormatCharactersInsideAName:
    def test_a_persian_entry_with_a_non_joiner_matches_the_column_that_has_it(self, tmp_path):
        result = _run(_keys(**{"analyst-1": [_PERSIAN]}), config_dir=_persian_schema(tmp_path))
        assert result.status == "PASS" and not result.detail.startswith("advice:")

    def test_the_same_entry_without_the_non_joiner_matches_no_column_that_has_it(self, tmp_path):
        without = _PERSIAN.replace("\u200c", "")
        result = _run(_keys(**{"analyst-1": [without]}), config_dir=_persian_schema(tmp_path))
        assert result.status == "FAIL" and ascii(without) in result.detail

    def test_an_entry_with_a_character_inside_matches_the_column_without_it(self):
        assert _run(_keys(**{"analyst-1": ["Na\u200dme", "National\u200dID"]})).status == "PASS"

    def test_an_entry_with_a_character_inside_that_matches_neither_spelling_fails(self):
        result = _run(_keys(**{"analyst-1": ["Na\u200dmee"]}))
        assert result.status == "FAIL" and "'Na\\u200dmee'" in result.detail

    def test_the_advice_is_computed_on_the_name_without_the_character(self):
        result = _run(_keys(**{"analyst-1": ["National\u200dID"]}))
        assert result.status == "PASS" and result.detail.startswith("advice:")
        assert "NationalCode" in result.detail

    def test_look_alikes_of_a_name_with_a_character_inside(self):
        found = look_alike_columns(["National\u200dID"], _COLUMNS)
        assert found["National\u200dID"] == look_alike_columns(["NationalID"], _COLUMNS)["NationalID"]

    def test_the_unmatched_helper_accepts_either_spelling(self):
        assert unmatched_bare_entries(["Na\u200dme"], ["Name"]) == ()
        assert unmatched_bare_entries(["Na\u200dme"], ["Na\u200dme"]) == ()
        assert unmatched_bare_entries(["Na\u200dmee"], ["Name"]) == ("Na\u200dmee",)


class TestPass:
    def test_every_entry_names_a_column(self):
        result = _run(_keys(**{"analyst-1": ["Name", "IsActive"], "analyst-2": ["Year"]}))
        assert (result.name, result.status) == (NAME, "PASS")
        assert not result.detail.startswith("advice:")
        assert "3 plain denied_columns entries of 2 key(s)" in result.detail

    def test_no_plain_entry_at_all(self):
        result = _run(_keys(**{"analyst-1": [], "analyst-2": ["sales.Customer.Name"]}))
        assert result.status == "PASS" and "no key of the 2 has a plain" in result.detail

    def test_a_key_that_denies_every_column_has_no_advice(self):
        invalidate_cache()
        issue_key("panel-1", "Panel One")
        result = _run(_keys())
        assert result.status == "PASS" and not result.detail.startswith("advice:")


class TestAdvice:
    def test_a_look_alike_column_is_advised_and_never_fails(self):
        # Supplier.NationalCode sits next to Customer.NationalID in the example schema.
        result = _run(_keys(**{"analyst-1": ["NationalID"]}))
        assert (result.name, result.status) == (NAME, "PASS")
        assert result.detail.startswith("advice:")
        assert "analyst-1" in result.detail
        assert "NationalID -> 1: NationalCode" in result.detail

    def test_a_column_the_key_already_denies_is_not_advised(self):
        result = _run(_keys(**{"analyst-1": ["NationalID", "NationalCode"]}))
        assert not result.detail.startswith("advice:")

    def test_keys_with_the_same_advice_are_grouped(self):
        result = _run(_keys(**{"a-1": ["NationalID"], "a-2": ["NationalID"], "a-3": ["Name"]}))
        assert "a-1, a-2: NationalID" in result.detail

    def test_the_advice_names_columns_not_values(self):
        result = _run(_keys(**{"analyst-1": ["NationalID"]}))
        assert "0012345678" not in result.detail


class TestSkips:
    def test_no_key_at_all(self):
        result = _run("")
        assert (result.name, result.status) == (NAME, "SKIP")
        assert "no API key" in result.detail

    def test_a_schema_that_is_not_there(self, tmp_path):
        result = _run(_keys(**{"analyst-1": ["Name"]}), config_dir=tmp_path)
        assert result.status == "SKIP" and "schema.yaml" in result.detail

    def test_a_schema_with_no_columns(self, tmp_path):
        (tmp_path / "schema.yaml").write_text("tables:\n  T:\n    description: d\n", encoding="utf-8")
        result = _run(_keys(**{"analyst-1": ["Name"]}), config_dir=tmp_path)
        assert result.status == "SKIP" and "no column" in result.detail

    def test_an_invalid_key_configuration_is_left_to_the_key_check(self):
        result = _run("{not json")
        assert result.status == "SKIP" and "API key authentication" in result.detail


class TestRegistration:
    def test_it_is_one_global_check_and_not_a_deep_check(self):
        from api.admin_routes import _DEEP_CHECK_NAMES

        names = [c.__name__ for c in build_checks()]
        assert names.count("check_column_restrictions_name_real_columns") == 1
        assert "check_column_restrictions_name_real_columns" not in _DEEP_CHECK_NAMES

    def test_it_runs_after_the_key_check(self):
        names = [c.__name__ for c in build_checks()]
        assert names.index("check_column_restrictions_name_real_columns") == names.index(
            "check_api_key_authenticates"
        ) + 1

    def test_a_failure_does_not_stop_the_preflight_from_finishing(self):
        invalidate_cache()
        with cfg.override_settings(
            api_keys_file="", api_keys_json=_keys(**{"analyst-1": ["Typo"]}), project_config_dir=str(_EXAMPLE),
        ):
            results = [c() for c in build_checks() if c.__name__ == "check_column_restrictions_name_real_columns"]
        assert [r.status for r in results] == ["FAIL"]


@pytest.fixture(autouse=True)
def _fresh_app_db_here(tmp_path):
    with cfg.override_settings(app_db_url=f"sqlite:///{tmp_path / 'appdb.db'}"):
        dispose_app_engine()
        invalidate_cache()
        yield
    dispose_app_engine()
    invalidate_cache()


_COLUMNS = ["NationalID", "NationalCode", "CustomerNationalID", "BrokerNationalCode", "Name", "ID", "Code"]


class TestLookAlikeColumns:
    def test_a_column_that_contains_the_name_is_found(self):
        assert look_alike_columns(["NationalID"], _COLUMNS)["NationalID"] == (
            "BrokerNationalCode", "CustomerNationalID", "NationalCode",
        )

    def test_the_same_stem_with_another_ending_is_found(self):
        found = look_alike_columns(["NationalCode"], ["NationalID", "NationalNo", "NationalNumber", "NationalNum", "Name"])
        assert found == {"NationalCode": ("NationalID", "NationalNo", "NationalNum", "NationalNumber")}

    @pytest.mark.parametrize("ending", ["ID", "Id", "Code", "No", "Number", "Num"])
    def test_every_identifier_ending_is_swapped(self, ending):
        found = look_alike_columns(["PolicyNumber"], [f"Policy{ending}"])
        assert found == ({} if ending == "Number" else {"PolicyNumber": (f"Policy{ending}",)})

    def test_the_denied_name_itself_is_never_a_look_alike(self):
        assert "NationalID" not in look_alike_columns(["NationalID"], _COLUMNS)["NationalID"]
        assert look_alike_columns(["NationalID"], ["NationalID", "nationalid"]) == {}

    def test_columns_the_entries_already_deny_are_left_out(self):
        found = look_alike_columns(["NationalID", "NationalCode", "CustomerNationalID"], _COLUMNS)
        assert found["NationalID"] == ("BrokerNationalCode",)
        assert "NationalCode" not in found["NationalID"] and "CustomerNationalID" not in found["NationalID"]

    def test_a_denied_name_is_matched_ignoring_case(self):
        assert look_alike_columns(["nationalid"], ["CUSTOMERNATIONALID"]) == {"nationalid": ("CUSTOMERNATIONALID",)}

    def test_a_short_name_says_nothing(self):
        assert look_alike_columns(["ID", "No", "Code"], _COLUMNS) == {}

    def test_unrelated_columns_are_not_advised(self):
        assert look_alike_columns(["NationalID"], ["Name", "Phone", "NationalityName"]) == {}

    def test_scoped_and_empty_entries_are_ignored(self):
        assert look_alike_columns(["sales.Customer.NationalID", "", "\u200c"], _COLUMNS) == {}

    def test_entries_are_cleaned_first(self):
        assert "NationalID" in look_alike_columns(["NationalID\u200f"], _COLUMNS)

    def test_names_come_back_in_the_schema_spelling_without_repeats(self):
        found = look_alike_columns(["NationalID"], ["customerNationalId", "CustomerNationalID", "customernationalid"])
        assert found == {"NationalID": ("customerNationalId",)}

    def test_nothing_denied_nothing_found(self):
        assert look_alike_columns([], _COLUMNS) == {}


class TestUnmatchedBareEntries:
    def test_names_no_column_has(self):
        assert unmatched_bare_entries(["Name", "Nmae", "Nmae", "a.b.c"], ["Name"]) == ("Nmae",)

    def test_an_entry_is_cleaned_before_it_is_compared(self):
        assert unmatched_bare_entries(["Name\u200f "], ["Name"]) == ()

    def test_the_loaded_schema_column_names_are_lower_case(self):
        names = schema_column_names()
        assert names is not None and "nationalid" in names and "NationalID" not in names
