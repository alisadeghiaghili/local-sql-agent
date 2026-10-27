# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``denied_columns`` entries carrying whitespace or invisible characters restrict what they name.

A Persian editor inserts U+200C, U+200F, U+200E, the bidi embeddings and
isolates and U+FEFF without showing them. Before 6.15.1 an entry such as
``"NationalID<RLM>"`` never equalled the column name and denied nothing, with
no message anywhere. Every source of keys now reads entries through
``security.column_policy.normalize_denied_entry``; what that leaves alone is
pinned by ``tests/test_denied_columns_pins.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging

import pytest

import config as cfg
from appdb.engine import dispose_app_engine
from appdb.key_store import (
    get_active_principals,
    invalidate_cache,
    issue_key,
    list_keys,
    update_denied_columns,
)
from security.auth import Principal, _parse_api_keys, load_api_keys, scope_key
from security.column_policy import (
    cached_column_policy,
    normalize_denied_entries,
    normalize_denied_entry,
    parse_column_policy,
)
from security.sql_guard import CorrectableRejection, PolicyRejection, validate_sql
from tests._source_fixtures import routed_sources

#: A statement of the shape that leaked a restricted column: it selects the
#: column next to an aggregate and groups by it.
_LEAKY_SQL = (
    "SELECT TOP 100 c.Name, c.NationalID, SUM(o.TotalAmount) AS Total "
    "FROM [sales].[Order] o JOIN [sales].[Customer] c ON o.CustomerID = c.ID "
    "GROUP BY c.Name, c.NationalID"
)

#: Spellings of ``NationalID`` that looked right and denied nothing.
_VARIANTS = {
    "trailing space": "NationalID ",
    "leading space": " NationalID",
    "tab and newline": "\tNationalID\n",
    "no-break spaces": " NationalID ",
    "right-to-left mark": "NationalID‏",
    "left-to-right mark": "NationalID‎",
    "zero-width non-joiner first": "‌NationalID",
    "zero-width joiner inside": "National‍ID",
    "zero-width space": "Nation​alID",
    "bidi embedding": "‪NationalID‬",
    "bidi override": "‮NationalID‬",
    "bidi isolate": "⁦NationalID⁩",
    "byte-order mark": "﻿NationalID",
    "soft hyphen": "National­ID",
    "word joiner": "National⁠ID",
}
_VARIANT_IDS = list(_VARIANTS)

_RAW_KEY = "norm-key-1"


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _key_json(denied, *, key_id: str = "analyst-1", raw_key: str = _RAW_KEY) -> str:
    return json.dumps(
        [{"id": key_id, "name": "Analyst", "key_sha256": _sha256(raw_key), "denied_columns": denied}],
        ensure_ascii=False,
    )


def _file_principal(denied) -> Principal:
    (principal,) = _parse_api_keys(_key_json(denied)).values()
    return principal


def _rejects(principal: Principal) -> None:
    with pytest.raises(PolicyRejection) as excinfo:
        validate_sql(_LEAKY_SQL, denied_columns=principal.denied_columns)
    assert excinfo.value.reason == "denied_column"


def _warnings(caplog, logger: str = "security.auth") -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name == logger]


@pytest.fixture()
def app_db(tmp_path):
    with cfg.override_settings(app_db_url=f"sqlite:///{tmp_path / 'appdb.db'}"):
        dispose_app_engine()
        invalidate_cache()
        yield
    dispose_app_engine()
    invalidate_cache()


class TestNormalizeEntry:
    @pytest.mark.parametrize("variant", _VARIANTS.values(), ids=_VARIANT_IDS)
    def test_every_variant_becomes_the_plain_name(self, variant):
        assert normalize_denied_entry(variant) == "NationalID"

    def test_case_letters_and_inner_spaces_are_not_touched(self):
        assert normalize_denied_entry("National ID") == "National ID"
        assert normalize_denied_entry("nationalID") == "nationalID"
        assert normalize_denied_entry("[Price List]") == "[Price List]"
        assert normalize_denied_entry("ملی") == "ملی"

    def test_it_is_idempotent(self):
        for variant in _VARIANTS.values():
            once = normalize_denied_entry(variant)
            assert normalize_denied_entry(once) == once

    def test_scoped_entries_are_cleaned_too(self):
        assert normalize_denied_entry("‏main:sales.Customer.‌NationalID ") == "main:sales.Customer.NationalID"

    def test_entries_report_what_changed_and_what_is_empty(self):
        result = normalize_denied_entries(["Phone", " Name‏", "‌", "", "  "])
        assert result.entries == ("Phone", "Name", "‌", "", "  ")
        assert result.changed == ((" Name‏", "Name"),)
        assert result.empty == ("‌", "  ")
        assert not normalize_denied_entries(["Phone", "Name"])


class TestPolicyAndGuard:
    @pytest.mark.parametrize("variant", _VARIANTS.values(), ids=_VARIANT_IDS)
    def test_the_guard_rejects_the_leaky_statement_for_every_variant(self, variant):
        with pytest.raises(PolicyRejection) as excinfo:
            validate_sql(_LEAKY_SQL, denied_columns=(variant,))
        assert excinfo.value.subject == "NationalID"

    @pytest.mark.parametrize("variant", _VARIANTS.values(), ids=_VARIANT_IDS)
    def test_the_parsed_policy_is_the_one_of_the_plain_name(self, variant):
        assert parse_column_policy([variant]) == parse_column_policy(["NationalID"])
        assert cached_column_policy([variant]).denied == frozenset({"NATIONALID"})

    @pytest.mark.parametrize("variant", [
        "sales.Customer.NationalID‏", " sales.Customer.NationalID", "sales.‌Customer.NationalID",
        "⁦sales.Customer.NationalID⁩",
    ])
    def test_a_scoped_entry_with_invisible_characters_is_join_only_as_written(self, variant):
        with pytest.raises(CorrectableRejection) as excinfo:
            validate_sql(_LEAKY_SQL, denied_columns=(variant,))
        # The subject is the cleaned entry, what an access request removes.
        assert excinfo.value.subject == "sales.Customer.NationalID"

    def test_a_source_scoped_entry_with_invisible_characters(self):
        with routed_sources({"Order": ("main",), "Customer": ("main",)}):
            policy = parse_column_policy(["main:‏sales.Customer.NationalID "])
        assert [s.entry for s in policy.join_only] == ["main:sales.Customer.NationalID"]

    def test_an_inner_space_is_not_repaired(self):
        validate_sql(_LEAKY_SQL, denied_columns=("National ID",))  # still denies nothing, as written

    @pytest.mark.parametrize("entry", ["", "  ", "\u200c", "\u200f \u200e"])
    def test_an_entry_with_nothing_visible_does_what_it_has_always_done(self, entry):
        # Any non-empty list turns on the check of a `*` that cannot be
        # resolved to a table; an entry with nothing in it keeps doing that,
        # so cleaning never makes a key looser.
        star = "WITH t AS (SELECT c.Name FROM [sales].[Customer] c) SELECT TOP 5 * FROM t"
        validate_sql(star, denied_columns=[])
        with pytest.raises(PolicyRejection):
            validate_sql(star, denied_columns=[entry])


class TestFileAndEnvKeys:
    @pytest.mark.parametrize("variant", _VARIANTS.values(), ids=_VARIANT_IDS)
    def test_a_key_in_the_text_is_cleaned_and_restricts(self, variant):
        principal = _file_principal([variant, "Phone"])
        assert principal.denied_columns == ("NationalID", "Phone")
        _rejects(principal)

    @pytest.mark.parametrize("variant", _VARIANTS.values(), ids=_VARIANT_IDS)
    def test_an_api_keys_json_key_restricts(self, variant):
        with cfg.override_settings(api_keys_file="", api_keys_json=_key_json([variant])):
            (principal,) = load_api_keys().values()
        _rejects(principal)

    @pytest.mark.parametrize("variant", _VARIANTS.values(), ids=_VARIANT_IDS)
    def test_an_api_keys_file_key_restricts(self, variant, tmp_path):
        from security.auth import _reset_api_keys_file_cache

        path = tmp_path / "keys.json"
        path.write_text(_key_json([variant]), encoding="utf-8")
        _reset_api_keys_file_cache()
        with cfg.override_settings(api_keys_file=str(path), api_keys_json=""):
            (principal,) = load_api_keys().values()
        _rejects(principal)

    def test_json_escapes_in_the_file_are_cleaned_like_literal_characters(self):
        text = '[{"id": "a", "name": "A", "key_sha256": "%s", "denied_columns": ["NationalID\\u200f"]}]' % _sha256(_RAW_KEY)
        (principal,) = _parse_api_keys(text).values()
        assert principal.denied_columns == ("NationalID",)

    def test_a_scoped_entry_is_cleaned_and_checked_against_the_schema(self):
        principal = _file_principal(["sales.Customer.‏NationalID "])
        assert principal.denied_columns == ("sales.Customer.NationalID",)

    def test_a_scoped_entry_that_names_nothing_still_stops_the_load(self):
        from security.auth import ApiKeyConfigError

        with pytest.raises(ApiKeyConfigError, match="NoSuchTable"):
            _file_principal(["sales.‏NoSuchTable.ID"])

    def test_the_cache_scope_of_an_entry_and_its_cleaned_twin_is_the_same(self):
        for variant in _VARIANTS.values():
            dirty = _file_principal([variant])
            clean = Principal(id="b", name="B", denied_columns=("NationalID",))
            assert scope_key(dirty, None) == scope_key(clean, None)

    def test_a_principal_built_by_hand_is_cleaned_too(self):
        principal = Principal(id="a", name="A", denied_columns=("NationalID‏", "Phone"))
        assert principal.denied_columns == ("NationalID", "Phone")
        assert principal.column_policy.denied == frozenset({"NATIONALID", "PHONE"})

    def test_a_clean_list_keeps_its_type_and_identity(self):
        entries = ["NationalID", "Phone"]
        assert Principal(id="a", name="A", denied_columns=entries).denied_columns is entries


class TestWarnings:
    def test_one_warning_per_key_names_the_id_and_shows_the_characters(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            _file_principal(["NationalID‏", " Phone", "Name"])
        (message,) = [m for m in _warnings(caplog) if "normalisation" in m]
        assert "analyst-1" in message
        assert "'NationalID\\u200f' -> 'NationalID'" in message
        assert "' Phone' -> 'Phone'" in message
        assert "Name" not in message.replace("NationalID", "")  # an unchanged entry is not listed
        assert "‏" not in message  # ascii(), never the raw character

    def test_the_key_hash_is_never_logged(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            _file_principal(["NationalID‏", "NoSuchColumnAnywhere"])
        assert _sha256(_RAW_KEY) not in "\n".join(_warnings(caplog))

    def test_it_is_logged_once_per_process_not_per_parse(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            _file_principal(["NationalID‏"])
            _file_principal(["NationalID‏"])
        assert len([m for m in _warnings(caplog) if "normalisation" in m]) == 1

    def test_an_edited_list_warns_again(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            _file_principal(["NationalID‏"])
            _file_principal(["Phone‏"])
        assert len([m for m in _warnings(caplog) if "normalisation" in m]) == 2

    def test_a_clean_key_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            _file_principal(["NationalID", "sales.Customer.Name"])
        assert _warnings(caplog) == []

    def test_an_entry_with_nothing_visible_is_kept_and_reported(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            principal = _file_principal(["NationalID", "‌"])
        assert principal.denied_columns == ("NationalID", "‌")
        (message,) = [m for m in _warnings(caplog) if "normalisation" in m]
        assert "'\\u200c' -> nothing visible" in message

    def test_the_load_does_not_fail_for_an_empty_entry(self):
        assert _file_principal(["", " ", "‏"]).id == "analyst-1"


class TestUnmatchedEntriesAtLoad:
    def test_a_name_no_column_has_is_warned_about_once(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            principal = _file_principal(["NationalID", "NatioanlID", "National ID"])
            _file_principal(["NationalID", "NatioanlID", "National ID"])
        (message,) = [m for m in _warnings(caplog) if "match no column" in m]
        assert "'NatioanlID'" in message and "'National ID'" in message
        assert "'NationalID'" not in message.replace("'NatioanlID'", "")
        assert "analyst-1" in message
        assert principal.denied_columns == ("NationalID", "NatioanlID", "National ID")  # still starts

    def test_the_match_ignores_case(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            _file_principal(["nationalid", "NAME"])
        assert _warnings(caplog) == []

    def test_a_cleaned_entry_is_compared_after_cleaning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            _file_principal(["NationalID‏"])
        assert [m for m in _warnings(caplog) if "match no column" in m] == []

    def test_scoped_entries_are_not_compared(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            _file_principal(["sales.Customer.Name"])
        assert _warnings(caplog) == []

    def test_no_schema_means_no_warning_and_no_failure(self, caplog):
        from unittest.mock import patch

        with patch("security.column_policy.load_schema_view", side_effect=RuntimeError("no schema")):
            with caplog.at_level(logging.WARNING, logger="security.auth"):
                principal = _file_principal(["NoSuchColumnAnywhere"])
        assert principal.denied_columns == ("NoSuchColumnAnywhere",)
        assert _warnings(caplog) == []


class TestPanelKeys:
    @pytest.mark.parametrize("variant", _VARIANTS.values(), ids=_VARIANT_IDS)
    def test_the_writer_stores_the_cleaned_entries(self, app_db, variant):
        _, entry = issue_key("panel-1", "Panel One")
        stored = update_denied_columns(entry["key_sha256"], [variant, "Phone"])
        assert stored == ["NationalID", "Phone"]
        row = next(r for r in list_keys() if r["key_sha256"] == entry["key_sha256"])
        assert row["denied_columns"] == ["NationalID", "Phone"]
        _rejects(get_active_principals()[entry["key_sha256"]])

    def test_a_scoped_entry_is_cleaned_and_validated_by_the_writer(self, app_db):
        _, entry = issue_key("panel-2", "Panel Two")
        stored = update_denied_columns(entry["key_sha256"], ["sales.Customer.‏NationalID "])
        assert stored == ["sales.Customer.NationalID"]

    def test_a_bad_scoped_entry_is_still_refused_with_the_entry_named(self, app_db):
        from security.column_policy import ColumnPolicyError

        _, entry = issue_key("panel-3", "Panel Three")
        with pytest.raises(ColumnPolicyError, match="NoSuchTable"):
            update_denied_columns(entry["key_sha256"], ["sales.‏NoSuchTable.ID"])

    def test_the_writer_keeps_an_entry_with_nothing_visible_as_written(self, app_db):
        _, entry = issue_key("panel-4", "Panel Four")
        assert update_denied_columns(entry["key_sha256"], ["Phone", "‌"]) == ["Phone", "‌"]

    @pytest.mark.parametrize("variant", _VARIANTS.values(), ids=_VARIANT_IDS)
    def test_a_row_stored_before_the_cleaning_existed_is_read_cleaned(self, app_db, variant, caplog):
        from sqlalchemy import update

        from appdb.engine import get_app_engine
        from appdb.models import admin_api_keys

        _, entry = issue_key("panel-5", "Panel Five")
        with get_app_engine().begin() as conn:
            conn.execute(
                update(admin_api_keys)
                .where(admin_api_keys.c.key_sha256 == entry["key_sha256"])
                .values(denied_columns_json=json.dumps([variant, "Phone"]))
            )
        invalidate_cache()
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            principal = get_active_principals()[entry["key_sha256"]]
            invalidate_cache()
            get_active_principals()
        assert principal.denied_columns == ("NationalID", "Phone")
        _rejects(principal)
        (message,) = [m for m in _warnings(caplog) if "normalisation" in m]
        assert "panel-5" in message and entry["key_sha256"] not in message

    def test_a_key_imported_from_the_environment_is_not_warned_about_twice(self, app_db, caplog):
        from appdb.key_store import bootstrap_from_env

        with cfg.override_settings(api_keys_file="", api_keys_json=_key_json(["NationalID\u200f", "Typo"])):
            with caplog.at_level(logging.WARNING, logger="security.auth"):
                bootstrap_from_env()
                (principal,) = get_active_principals().values()
        assert principal.denied_columns == ("NationalID", "Typo")
        messages = _warnings(caplog)
        assert len([m for m in messages if "normalisation" in m]) == 1
        assert len([m for m in messages if "match no column" in m]) == 1

    def test_the_acl_route_stores_and_returns_the_cleaned_entries(self, app_db):
        import api.server as server_module
        from fastapi.testclient import TestClient

        raw_security = "5" * 40
        keys = json.dumps([{
            "id": "security-admin", "name": "Security", "key_sha256": _sha256(raw_security), "security": True,
        }])
        with cfg.override_settings(api_keys_json=keys):
            invalidate_cache()
            server_module._system_prompt = "stub system prompt"
            client = TestClient(server_module.app, raise_server_exceptions=False)
            _, entry = issue_key("panel-6", "Panel Six")
            resp = client.patch(
                f"/admin/keys/{entry['key_sha256']}/acl",
                json={"denied_columns": ["NationalID‏", " Phone"]},
                headers={"Authorization": f"Bearer {raw_security}"},
            )
        assert resp.status_code == 200
        assert resp.json()["denied_columns"] == ["NationalID", "Phone"]

    def test_an_access_request_removes_the_entry_whichever_way_it_is_stored(self, app_db):
        from sqlalchemy import update

        from appdb.access_requests import approve_request
        from appdb.engine import get_app_engine
        from appdb.models import access_requests, admin_api_keys

        _, entry = issue_key("panel-7", "Panel Seven")
        with get_app_engine().begin() as conn:
            conn.execute(
                update(admin_api_keys)
                .where(admin_api_keys.c.key_sha256 == entry["key_sha256"])
                .values(denied_columns_json=json.dumps(["NationalID‏", "Phone"]))
            )
        invalidate_cache()
        with get_app_engine().begin() as conn:
            conn.execute(access_requests.insert().values(
                requester_principal_id="panel-7", session_id="s_1", turn_id="t_1",
                column_name="NationalID", status="open", created_at="2026-01-01T00:00:00+00:00",
            ))
        request_id = _open_request_ids()[0]
        _, updated = approve_request(request_id, actor_principal_id="security-admin")
        assert updated == 1
        row = next(r for r in list_keys() if r["key_sha256"] == entry["key_sha256"])
        assert row["denied_columns"] == ["Phone"]


def _open_request_ids():
    from sqlalchemy import select

    from appdb.engine import get_app_engine
    from appdb.models import access_requests

    with get_app_engine().connect() as conn:
        return [row.request_id for row in conn.execute(select(access_requests.c.request_id))]
