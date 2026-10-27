# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""What ``denied_columns`` does today for every entry that is already well formed.

These tests describe behaviour that must not change: a clean exact entry, a
scoped entry (``Table.Column``, ``Source:Table.Column``), the keys the admin
panel issues, ``"denied_columns": []``, a missing field and its warning, the
cache scope key and the memory ACL re-check. They were written before the
entry normalisation of 6.15.1 and pass on both sides of it.

Runs against the committed example schema (``Customer.NationalID`` is a real
column there).
"""

from __future__ import annotations

import hashlib
import json
import logging

import pytest

import config as cfg
from appdb.engine import dispose_app_engine
from appdb.key_store import (
    bootstrap_from_env,
    get_active_principals,
    invalidate_cache,
    issue_key,
    list_keys,
    update_denied_columns,
)
from security.auth import Principal, _parse_api_keys, scope_key
from security.column_policy import cached_column_policy, parse_column_policy
from security.sql_guard import CorrectableRejection, PolicyRejection, validate_sql
from tests._source_fixtures import routed_sources

#: The shape of statement that leaked a restricted column: it selects it, joins
#: on another column and groups by it.
_LEAKY_SQL = (
    "SELECT TOP 100 c.Name, c.NationalID, SUM(o.TotalAmount) AS Total "
    "FROM [sales].[Order] o JOIN [sales].[Customer] c ON o.CustomerID = c.ID "
    "GROUP BY c.Name, c.NationalID"
)

_RAW_KEY = "pin-key-1"


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _key_json(denied=None, *, omit: bool = False) -> str:
    entry: dict = {"id": "analyst-1", "name": "Analyst", "key_sha256": _sha256(_RAW_KEY)}
    if not omit:
        entry["denied_columns"] = denied
    return json.dumps([entry])


def _principal_from(denied) -> Principal:
    (principal,) = _parse_api_keys(_key_json(denied)).values()
    return principal


@pytest.fixture()
def app_db(tmp_path):
    with cfg.override_settings(app_db_url=f"sqlite:///{tmp_path / 'appdb.db'}"):
        dispose_app_engine()
        invalidate_cache()
        yield
    dispose_app_engine()
    invalidate_cache()


class TestExactEntriesReject:
    @pytest.mark.parametrize("entry", ["NationalID", "nationalid", "NATIONALID"])
    def test_an_exact_entry_rejects_the_statement_in_any_case(self, entry):
        with pytest.raises(PolicyRejection) as excinfo:
            validate_sql(_LEAKY_SQL, denied_columns=(entry,))
        assert excinfo.value.reason == "denied_column"

    def test_the_rejection_names_the_column_as_the_query_spelled_it(self):
        with pytest.raises(PolicyRejection) as excinfo:
            validate_sql(_LEAKY_SQL, denied_columns=("NationalID",))
        assert excinfo.value.subject == "NationalID"

    def test_a_scoped_entry_is_join_only_on_that_table(self):
        with pytest.raises(CorrectableRejection) as excinfo:
            validate_sql(_LEAKY_SQL, denied_columns=("sales.Customer.NationalID",))
        assert excinfo.value.subject == "sales.Customer.NationalID"

    def test_a_scoped_entry_still_allows_the_join(self):
        sql = (
            "SELECT TOP 10 COUNT(*) AS N FROM [sales].[Order] o "
            "JOIN [sales].[Customer] c ON o.CustomerID = c.ID"
        )
        validate_sql(sql, denied_columns=("sales.Customer.ID",))

    def test_an_empty_list_restricts_nothing(self):
        validate_sql(_LEAKY_SQL, denied_columns=[])

    @pytest.mark.parametrize("entry", ["NoSuchColumnAnywhere", "", "  "])
    def test_any_non_empty_list_refuses_a_star_it_cannot_resolve(self, entry):
        star = "WITH t AS (SELECT c.Name FROM [sales].[Customer] c) SELECT TOP 5 * FROM t"
        validate_sql(star, denied_columns=[])
        with pytest.raises(PolicyRejection):
            validate_sql(star, denied_columns=[entry])


class TestFileAndEnvKeys:
    def test_clean_entries_are_kept_exactly_as_written(self):
        entries = ["NationalID", "sales.Customer.Name", "main:sales.Order.ID", "nationalid"]
        with routed_sources({"Order": ("main",)}):
            assert _principal_from(entries).denied_columns == tuple(entries)

    def test_an_empty_list_gives_no_restriction_and_no_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            principal = _principal_from([])
        assert principal.denied_columns == ()
        assert caplog.records == []

    def test_a_missing_field_gives_no_restriction_and_one_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            (principal,) = _parse_api_keys(_key_json(omit=True)).values()
            _parse_api_keys(_key_json(omit=True))
        assert principal.denied_columns == ()
        warnings = [r for r in caplog.records if "no denied_columns field" in r.getMessage()]
        assert len(warnings) == 1
        assert _sha256(_RAW_KEY) not in warnings[0].getMessage()

    def test_a_clean_entry_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.auth"):
            _principal_from(["NationalID"])
        assert caplog.records == []

    @pytest.mark.parametrize("denied", [
        ["NoSuchColumnAnywhere"],
        [""],
        ["   "],
        ["National ID"],
        ["NationalID", "NationalID"],
    ])
    def test_every_list_that_loads_today_still_loads(self, denied):
        assert _principal_from(denied).id == "analyst-1"

    def test_a_list_given_as_a_tuple_or_list_is_stored_as_a_tuple(self):
        assert isinstance(_principal_from(["NationalID"]).denied_columns, tuple)

    def test_a_bad_scoped_entry_still_stops_the_load(self):
        from security.auth import ApiKeyConfigError

        with pytest.raises(ApiKeyConfigError, match="denied_columns"):
            _parse_api_keys(_key_json(["sales.NoSuchTable.ID"]))


class TestPanelKeys:
    def test_an_issued_key_denies_every_schema_column(self, app_db):
        from schema_data.registry import get_table_columns

        _, entry = issue_key("pin-issued", "Pin Issued")
        everything = {c for cols in get_table_columns().values() for c in cols}
        assert set(entry["denied_columns"]) == everything
        principal = get_active_principals()[entry["key_sha256"]]
        assert set(principal.denied_columns) == everything

    def test_a_clean_acl_is_stored_and_served_exactly(self, app_db):
        _, entry = issue_key("pin-acl", "Pin ACL")
        wanted = ["NationalID", "sales.Customer.Name", "OnlyThis"]
        update_denied_columns(entry["key_sha256"], wanted)
        stored = next(r for r in list_keys() if r["key_sha256"] == entry["key_sha256"])
        assert stored["denied_columns"] == wanted
        assert get_active_principals()[entry["key_sha256"]].denied_columns == tuple(wanted)

    def test_an_empty_acl_is_stored_as_no_restriction(self, app_db):
        _, entry = issue_key("pin-empty", "Pin Empty")
        update_denied_columns(entry["key_sha256"], [])
        assert get_active_principals()[entry["key_sha256"]].denied_columns == ()

    def test_a_malformed_scoped_entry_is_still_refused_and_nothing_is_stored(self, app_db):
        from security.column_policy import ColumnPolicyError

        _, entry = issue_key("pin-bad", "Pin Bad")
        before = next(r for r in list_keys() if r["key_sha256"] == entry["key_sha256"])
        with pytest.raises(ColumnPolicyError):
            update_denied_columns(entry["key_sha256"], ["a:b:c"])
        after = next(r for r in list_keys() if r["key_sha256"] == entry["key_sha256"])
        assert after["denied_columns"] == before["denied_columns"]

    def test_a_key_imported_from_the_environment_keeps_its_entries(self, app_db):
        with cfg.override_settings(api_keys_json=_key_json(["NationalID", "sales.Order.ID"])):
            bootstrap_from_env()
            (stored,) = [r for r in list_keys() if r["principal_id"] == "analyst-1"]
        assert stored["denied_columns"] == ["NationalID", "sales.Order.ID"]


class TestScopeKey:
    """The cache partition key of a principal: pinned to the exact digest."""

    @pytest.mark.parametrize("denied, digest", [
        ((), "5ef5ef0364b6939c"),
        (("NationalID",), "e6db28d8b128aa65"),
        (("NationalID", "Phone"), "e2aa4d7d58bf626d"),
        (("Phone", "NationalID"), "e2aa4d7d58bf626d"),
        (("sales.Order.ID",), "14b7a7d203203a15"),
        (("Src:sales.Order.ID", "NationalID"), "591bbeebb0c86f59"),
    ])
    def test_the_digest_of_a_clean_entry_list_is_unchanged(self, denied, digest):
        assert scope_key(Principal(id="a", name="A", denied_columns=denied), None) == digest

    def test_the_digest_with_memory_is_unchanged(self):
        principal = Principal(id="a", name="A", denied_columns=("NationalID",))
        assert scope_key(principal, {"scope": "x"}) == "edf28e0ecb3616b2"

    def test_a_loaded_key_has_the_same_scope_as_a_hand_built_principal(self):
        loaded = _principal_from(["NationalID", "Phone"])
        built = Principal(id="b", name="B", denied_columns=("Phone", "NationalID"))
        assert scope_key(loaded, None) == scope_key(built, None)


class TestPolicy:
    def test_a_bare_entry_is_upper_cased_and_a_scoped_one_is_not(self):
        policy = parse_column_policy(["NationalID", "sales.Customer.Name"], validate=False)
        assert policy.denied == frozenset({"NATIONALID"})
        assert [s.entry for s in policy.join_only] == ["sales.Customer.Name"]

    def test_the_cached_policy_ignores_order_and_repeats(self):
        assert cached_column_policy(["a", "b"]) is cached_column_policy(("b", "a", "a"))


class TestMemoryAclRecheck:
    def test_an_entry_on_a_denied_column_is_dropped_with_a_warning(self):
        from knowledge.memory_policy import get_memory_keys
        from session.memory import MemoryEntry, apply_memory_to_assumptions
        from session.models import Assumption

        key, key_cfg = next(iter(get_memory_keys().items()))
        entries = {key: MemoryEntry(key=key, field=key_cfg.field_name, value="x", updated_at="2026-01-01T00:00:00")}
        assumption = Assumption(
            field=key_cfg.field_name, label="l", value="default", source="default", editable=True,
        )
        result, warnings, used = apply_memory_to_assumptions(
            [assumption], entries, denied_columns=(key_cfg.column,),
        )
        assert result == [assumption]
        assert used == {}
        assert len(warnings) == 1 and key_cfg.column in warnings[0]
