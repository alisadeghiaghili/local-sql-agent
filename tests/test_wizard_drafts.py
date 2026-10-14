# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``core.wizard_drafts``: protected directories, backups and the merge rules."""

from __future__ import annotations

from core.wizard_drafts import (
    back_up,
    hold_back_empty,
    merge_examples,
    merge_mapping,
    merge_relationships,
    merge_synonyms,
    protected_directory,
)


class TestProtectedDirectory:
    def test_a_symbolic_link_to_the_live_directory_is_the_live_directory(self, tmp_path):
        live = tmp_path / "project_config"
        live.mkdir()
        link = tmp_path / "shortcut"
        link.symlink_to(live, target_is_directory=True)
        assert protected_directory(link, live, tmp_path) == "live"

    def test_a_draft_directory_next_to_the_live_one_is_not_protected(self, tmp_path):
        assert protected_directory(tmp_path / "project_config_draft", tmp_path / "project_config", tmp_path) is None


class TestBackUp:
    def test_a_second_backup_in_the_same_second_does_not_overwrite_the_first(self, tmp_path):
        target = tmp_path / "entities.yaml"
        target.write_text("one", encoding="utf-8")
        (first,) = back_up([target], "20260101-000000")
        target.write_text("two", encoding="utf-8")
        (second,) = back_up([target], "20260101-000000")
        assert first != second
        assert (first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8")) == ("one", "two")


class TestMergeNeverShrinks:
    def test_mapping_keeps_every_old_key(self):
        old = {f"k{i}": i for i in range(10)}
        merged, stats = merge_mapping(old, {"k3": "new", "z": 0})
        assert set(old) <= set(merged) and merged["k3"] == "new"
        assert (stats.kept, stats.added, stats.replaced) == (9, 1, 1)

    def test_a_key_spelled_in_another_case_is_the_same_table(self):
        merged, stats = merge_mapping({"customer": 1}, {"Customer": 2})
        assert merged == {"Customer": 2} and stats.replaced == 1

    def test_rename_applies_before_the_merge_and_is_reported(self):
        merged, stats = merge_mapping({"Trade": 1}, {"sales.Trade": 2}, {"Trade": "sales.Trade"})
        assert merged == {"sales.Trade": 2}
        assert stats.rekeyed == (("Trade", "sales.Trade"),) and stats.replaced == 1

    def test_synonyms_unite_the_tokens(self):
        merged, _ = merge_synonyms({"w": ["a"]}, {"w": ["b"]})
        assert merged == {"w": ["a", "b"]}

    def test_examples_keep_the_order_of_the_old_file(self):
        old = [{"question": f"q{i}", "sql": "s"} for i in range(3)]
        merged, stats = merge_examples(old, [{"question": "Q1", "sql": "new"}, {"question": "", "sql": "x"}])
        assert [e["question"] for e in merged] == ["q0", "Q1", "q2"]
        assert (stats.kept, stats.added, stats.replaced) == (2, 0, 1)

    def test_relationships_of_old_runs_are_renamed_and_deduplicated(self):
        old = [{"from_table": "Trade", "from_column": "p", "to_table": "Party", "to_column": "id", "join_hint": "J1"}]
        fresh = [{"from_table": "sales.Trade", "from_column": "P", "to_table": "Party", "to_column": "ID", "join_hint": "J2"}]
        merged, stats = merge_relationships(old, fresh, {"Trade": "sales.Trade"})
        assert [e["join_hint"] for e in merged] == ["J2"]
        assert stats.rekeyed == (("Trade", "sales.Trade"),)

    def test_an_entry_without_endpoints_is_kept_untouched(self):
        odd = {"note": "hand written"}
        merged, stats = merge_relationships([odd], [], None)
        assert merged == [odd] and stats.kept == 1



class TestHoldBackEmpty:
    def test_an_empty_new_value_is_dropped_only_where_the_old_one_has_content(self):
        kept, held = hold_back_empty({"A": ["x"], "B": []}, {"A": [], "B": [], "C": []}, lambda v: not v)
        assert kept == {"B": [], "C": []} and held == ("A",)

    def test_the_rename_and_case_are_taken_into_account(self):
        kept, held = hold_back_empty({"Trade": "text"}, {"sales.TRADE": ""}, lambda v: not v, {"Trade": "sales.Trade"})
        assert kept == {} and held == ("sales.TRADE",)

    def test_a_held_entry_is_counted_as_kept_by_the_merge(self):
        old = {"A": ["x"]}
        fresh, held = hold_back_empty(old, {"A": []}, lambda v: not v)
        merged, stats = merge_mapping(old, fresh)
        assert merged == old and (stats.kept, stats.replaced) == (1, 0) and held == ("A",)
