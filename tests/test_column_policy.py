# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``security.column_policy`` -- parsing and checking scoped ``denied_columns`` entries.

Runs against whichever ``schema.yaml`` the suite loads (CI: the committed
example, whose tables ``Order``, ``Customer``, ``Date`` live in schema
``sales`` and ``Broker`` in ``ref``). The data sources are patched in by
:func:`sources`, because the suite has no ``datasources.yaml``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from security.auth import ApiKeyConfigError, Principal, _parse_api_keys
from security.column_policy import (
    EMPTY_POLICY,
    ColumnPolicyError,
    ScopedColumn,
    cached_column_policy,
    hidden_value_columns,
    join_only_prompt_line,
    load_schema_view,
    parse_column_policy,
    resolve_join_only,
)
from tests._source_fixtures import routed_sources

#: Source names used throughout; ``main`` is the default.
_NAMES = ("main", "archive")


def sources(table_sets: dict[str, tuple[str, ...]] | None = None):
    """Two data sources, ``main`` (default) and ``archive``.

    Unlisted tables live in the default source. By default ``Order`` and
    ``Date`` are in both, ``Customer`` in ``main`` only, ``Broker`` in
    ``archive`` only.
    """
    return routed_sources(
        table_sets if table_sets is not None else {
            "Order": ("main", "archive"),
            "Date": ("main", "archive"),
            "Customer": ("main",),
            "Broker": ("archive",),
        },
    )


class TestParsing:
    def test_a_plain_name_is_a_legacy_full_denial(self):
        policy = parse_column_policy(["NationalID", "phone"])
        assert policy.denied == frozenset({"NATIONALID", "PHONE"})
        assert policy.join_only == ()

    def test_schema_table_column(self):
        with sources():
            (entry,) = parse_column_policy(["sales.Order.ID"]).join_only
        assert (entry.datasource, entry.table, entry.column) == (None, "sales.Order", "ID")
        assert entry.entry == "sales.Order.ID"

    def test_source_schema_table_column(self):
        with sources():
            (entry,) = parse_column_policy(["archive:sales.Order.ID"]).join_only
        assert (entry.datasource, entry.table, entry.column) == ("archive", "sales.Order", "ID")

    def test_source_column(self):
        with sources():
            (entry,) = parse_column_policy(["archive:ID"]).join_only
        assert (entry.datasource, entry.table, entry.column) == ("archive", None, "ID")

    def test_the_four_forms_together(self):
        with sources():
            policy = parse_column_policy(
                ["NationalID", "sales.Order.ID", "archive:sales.Order.BrokerID", "archive:ID"]
            )
        assert policy.denied == frozenset({"NATIONALID"})
        assert [s.entry for s in policy.join_only] == [
            "sales.Order.ID", "archive:sales.Order.BrokerID", "archive:ID",
        ]

    def test_the_entry_keeps_its_original_spelling(self):
        # It is the guard rejection's subject and what an approved access
        # request removes from the key, so it is never re-cased.
        with sources():
            (entry,) = parse_column_policy(["SALES.order.id"]).join_only
        assert entry.entry == "SALES.order.id"

    def test_duplicates_differing_only_in_case_collapse(self):
        with sources():
            policy = parse_column_policy(["sales.Order.ID", "SALES.ORDER.id"])
        assert len(policy.join_only) == 1

    def test_none_and_empty_are_the_empty_policy(self):
        assert parse_column_policy(None) is EMPTY_POLICY
        assert not parse_column_policy([])
        assert cached_column_policy(None) is EMPTY_POLICY

    def test_a_bare_string_is_refused_not_split_into_letters(self):
        with pytest.raises(ColumnPolicyError):
            parse_column_policy("NationalID")  # type: ignore[arg-type]

    def test_brackets_around_a_part_are_ignored(self):
        with sources():
            policy = parse_column_policy(["[sales].[Order].ID"])
        assert policy.join_only[0].table == "[sales].[Order]"
        with sources():
            assert resolve_join_only(policy, load_schema_view(), None).entry("Order", "id")

    def test_the_cached_policy_ignores_order_and_repeats(self):
        assert cached_column_policy(["a", "b"]) is cached_column_policy(("b", "a", "a"))


class TestMalformedEntries:
    @pytest.mark.parametrize("entry", [
        "a:b:c",
        "src:sales.Order:ID",
        "sales..ID",
        ".ID",
        "sales.Order.",
        ":ID",
        "src:",
        "src:.ID",
        "a.b.c.d.e.f.g",
    ])
    def test_syntax_errors_name_the_entry(self, entry):
        with pytest.raises(ColumnPolicyError) as excinfo:
            parse_column_policy([entry], validate=False)
        assert repr(entry) in str(excinfo.value)

    def test_syntax_is_checked_even_when_validation_is_off(self):
        with pytest.raises(ColumnPolicyError):
            parse_column_policy(["a:b:c"], validate=False)

    def test_a_non_string_entry_is_refused(self):
        with pytest.raises(ColumnPolicyError):
            parse_column_policy([5])  # type: ignore[list-item]


class TestExistenceChecks:
    def test_unknown_source(self):
        with sources(), pytest.raises(ColumnPolicyError, match=r"'nowhere'.*not configured"):
            parse_column_policy(["nowhere:sales.Order.ID"])

    def test_unknown_table(self):
        with sources(), pytest.raises(ColumnPolicyError, match=r"sales\.NoSuchTable.*not in schema"):
            parse_column_policy(["sales.NoSuchTable.ID"])

    def test_wrong_schema_for_a_real_table(self):
        with sources(), pytest.raises(ColumnPolicyError, match="not in schema"):
            parse_column_policy(["ref.Order.ID"])

    def test_table_not_in_the_named_source(self):
        # Customer lives in `main` only.
        with sources(), pytest.raises(ColumnPolicyError, match=r"not in data source 'archive'"):
            parse_column_policy(["archive:sales.Customer.ID"])

    def test_unknown_column(self):
        with sources(), pytest.raises(ColumnPolicyError, match=r"no column 'Nope'"):
            parse_column_policy(["sales.Order.Nope"])

    def test_source_column_form_needs_the_column_somewhere_in_the_source(self):
        with sources(), pytest.raises(ColumnPolicyError, match="no table of data source 'archive'"):
            parse_column_policy(["archive:NoSuchColumn"])

    def test_a_legacy_name_is_never_looked_up(self):
        with sources():
            assert parse_column_policy(["NoSuchColumnAnywhere"]).denied == {"NOSUCHCOLUMNANYWHERE"}

    def test_source_and_table_match_case_insensitively(self):
        with sources():
            policy = parse_column_policy(["ARCHIVE:SALES.ORDER.id"])
            restrictions = resolve_join_only(policy, load_schema_view(), "archive")
        assert restrictions.entry("Order", "ID") == "ARCHIVE:SALES.ORDER.id"

    def test_a_bare_table_name_is_accepted_when_one_table_has_it(self):
        with sources():
            policy = parse_column_policy(["Order.ID"])
            assert resolve_join_only(policy, load_schema_view(), None).entry("Order", "id")

    def test_a_bare_name_shared_by_two_tables_must_be_qualified(self):
        from security.column_policy import SchemaView

        view = SchemaView(
            columns_by_table={
                "sales.Customer": frozenset({"id"}), "ref.Customer": frozenset({"id"}),
            },
            qualifiers={"sales.Customer": "sales", "ref.Customer": "ref"},
        )
        with sources({}), pytest.raises(ColumnPolicyError, match="more than one schema"):
            parse_column_policy(["Customer.ID"], view=view)
        with sources({}):
            policy = parse_column_policy(["ref.Customer.ID"], view=view)
        assert policy.join_only

    def test_a_missing_schema_defers_the_check_instead_of_failing(self):
        with patch("security.column_policy.load_schema_view", side_effect=RuntimeError("no schema")):
            policy = parse_column_policy(["sales.NoSuchTable.ID"])
        assert policy.join_only[0].entry == "sales.NoSuchTable.ID"

    def test_the_deferred_entry_fails_loudly_on_first_use(self):
        policy = parse_column_policy(["sales.NoSuchTable.ID"], validate=False)
        with sources(), pytest.raises(ColumnPolicyError):
            resolve_join_only(policy, load_schema_view(), None)


class TestResolution:
    def test_a_table_entry_applies_in_every_source(self):
        with sources():
            policy = parse_column_policy(["sales.Order.ID"])
            view = load_schema_view()
            assert resolve_join_only(policy, view, "main").entry("Order", "id")
            assert resolve_join_only(policy, view, "archive").entry("Order", "id")

    def test_a_source_scoped_entry_is_inactive_elsewhere(self):
        with sources():
            policy = parse_column_policy(["archive:sales.Order.ID"])
            view = load_schema_view()
            assert resolve_join_only(policy, view, "archive").entry("Order", "ID")
            assert not resolve_join_only(policy, view, "main")

    def test_source_column_form_covers_every_table_of_that_source_with_the_column(self):
        with sources():
            policy = parse_column_policy(["archive:ID"])
            restrictions = resolve_join_only(policy, load_schema_view(), "archive")
        # Order, Date and Broker are in `archive`; Customer is not.
        assert {"Order", "Date", "Broker"} <= set(restrictions.by_table)
        assert "Customer" not in restrictions.by_table
        assert restrictions.names == frozenset({"id"})

    def test_an_unknown_executing_source_activates_every_entry(self):
        with sources():
            policy = parse_column_policy(["archive:sales.Order.ID"])
            assert resolve_join_only(policy, load_schema_view(), None)

    def test_hidden_value_columns_ignore_the_source(self):
        with sources():
            policy = parse_column_policy(["archive:sales.Order.ID", "Broker.ID"])
            hidden = hidden_value_columns(policy)
        assert ("Order", "id") in hidden and ("Broker", "id") in hidden
        assert hidden_value_columns(EMPTY_POLICY) == frozenset()


class TestPromptLine:
    def test_nothing_for_a_principal_with_only_legacy_entries(self):
        assert join_only_prompt_line(["NationalID"], None) == ""
        assert join_only_prompt_line(None, "main") == ""

    def test_lists_table_scoped_entries(self):
        line = join_only_prompt_line(["sales.Order.ID", "NationalID"], "main")
        assert "JOIN ... ON" in line
        assert line.endswith("sales.Order.ID")
        assert "NationalID" not in line

    def test_only_entries_active_on_the_source(self):
        with sources():
            entries = ["archive:sales.Order.ID", "main:Broker.ID"]
            assert "sales.Order.ID" in join_only_prompt_line(entries, "archive")
            assert "Broker.ID" not in join_only_prompt_line(entries, "archive")
            assert join_only_prompt_line(entries, "main").endswith("Broker.ID")

    def test_the_default_source_is_assumed_without_one(self):
        with sources():
            assert join_only_prompt_line(["main:sales.Order.ID"], None) != ""
            assert join_only_prompt_line(["archive:sales.Order.ID"], None) == ""

    def test_a_source_wide_entry_reads_as_every_table(self):
        with sources():
            assert "ID (on every table)" in join_only_prompt_line(["archive:ID"], "archive")


class TestScopedColumnValue:
    def test_default_entry_is_the_rendered_form(self):
        assert ScopedColumn("S", "a.B", "C").entry == "S:a.B.C"
        assert ScopedColumn(None, "a.B", "C").entry == "a.B.C"
        assert ScopedColumn("S", None, "C").entry == "S:C"

    def test_it_is_frozen_and_hashable(self):
        scoped = ScopedColumn(None, "a.B", "C")
        with pytest.raises(AttributeError):
            scoped.column = "D"  # type: ignore[misc]
        assert hash(scoped) == hash(ScopedColumn(None, "a.B", "C"))


class TestKeyLoading:
    _HASH = "a" * 64

    def _keys(self, denied):
        import json

        return json.dumps([{
            "id": "analyst", "name": "Analyst", "key_sha256": self._HASH,
            "denied_columns": denied,
        }])

    def test_a_valid_scoped_entry_loads(self):
        with sources():
            keys = _parse_api_keys(self._keys(["NationalID", "sales.Order.ID"]))
        principal = keys[self._HASH]
        assert principal.denied_columns == ("NationalID", "sales.Order.ID")
        assert [s.entry for s in principal.column_policy.join_only] == ["sales.Order.ID"]

    def test_a_typo_stops_loading_and_names_the_entry(self):
        with sources(), pytest.raises(ApiKeyConfigError) as excinfo:
            _parse_api_keys(self._keys(["sales.Ordr.ID"]), "API_KEYS_FILE (keys.json)")
        text = str(excinfo.value)
        assert "API_KEYS_FILE (keys.json)[0].denied_columns" in text
        assert "'sales.Ordr.ID'" in text

    def test_an_unknown_source_stops_loading(self):
        with sources(), pytest.raises(ApiKeyConfigError, match="not configured"):
            _parse_api_keys(self._keys(["elsewhere:ID"]))

    def test_legacy_only_keys_never_touch_the_schema(self):
        with patch(
            "security.column_policy.load_schema_view", side_effect=AssertionError("looked up"),
        ):
            keys = _parse_api_keys(self._keys(["NationalID", "Whatever"]))
        assert keys[self._HASH].denied_columns == ("NationalID", "Whatever")

    def test_a_malformed_entry_in_a_principal_surfaces_when_its_policy_is_read(self):
        principal = Principal(id="p", name="P", denied_columns=("a:b:c",))
        with pytest.raises(ColumnPolicyError):
            principal.column_policy


class TestScopeKey:
    def _key(self, *entries):
        from security.auth import scope_key

        return scope_key(Principal(id="p", name="P", denied_columns=entries), memory_used=None)

    def test_differently_scoped_policies_differ(self):
        keys = {
            self._key("ID"),
            self._key("sales.Order.ID"),
            self._key("archive:sales.Order.ID"),
            self._key("main:sales.Order.ID"),
            self._key("archive:ID"),
            self._key("sales.Order.ID", "archive:ID"),
        }
        assert len(keys) == 6

    def test_a_colon_cannot_forge_a_collision(self):
        assert self._key("a:b") != self._key("a", "b")
        assert self._key("a:b", "c") != self._key("a", "b:c")

    def test_order_does_not_matter(self):
        assert self._key("x", "sales.Order.ID") == self._key("sales.Order.ID", "x")

    def test_no_restriction_has_its_own_key(self):
        assert self._key() != self._key("ID")
