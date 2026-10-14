# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``eval.cli compare`` and ``eval.cli split``.

``compare`` reads two rounds (share bundles or ``--out`` reports) by
``case_id``; ``split`` writes the deterministic dev / test halves that
``compare --split`` limits a comparison to.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_rounds.py -q
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from eval import cli
from eval.models import GoldenCase
from eval.rounds import compare_runs, load_run, read_case_ids
from eval.share import FORMAT_VERSION
from eval.split import split_cases

REPO_ROOT = Path(__file__).resolve().parents[2]


def _record(case_id, passed, tags=("count",), cls=None, status=None):
    return {
        "case_id": case_id, "tags": list(tags), "passed": passed,
        "status": status or ("pass" if passed else "fingerprint_mismatch"),
        "expected_datasource": "sales",
        "diagnosis": {"mismatch_class": cls or ("match" if passed else "values_differ_same_shape")},
    }


def _bundle(path: Path, records, sha="g1", reference="live"):
    path.write_text(json.dumps({
        "format_version": FORMAT_VERSION, "cases": records, "golden": {"sha256": sha},
        "run": {"reference": reference}, "app_version": "1", "model": "m", "prefix_version": "p",
    }, ensure_ascii=False), encoding="utf-8")
    return path


OLD = [
    _record("a", False, ("count", "x"), "actual_empty"),
    _record("b", True, ("count",)),
    _record("c", False, ("rank",), "actual_is_subset"),
    _record("d", True, ("rank",)),
    _record("gone", True, ("rank",)),
]
NEW = [
    _record("a", True, ("count", "x")),                       # fixed
    _record("b", False, ("count",), "row_count_differs"),     # regressed
    _record("c", False, ("rank",), "actual_is_subset"),       # still failing
    _record("d", True, ("rank",)),                            # still passing
    _record("fresh_pass", True, ("rank",)),                   # added
    _record("fresh_fail", False, ("rank",), "actual_empty"),  # added
]


@pytest.fixture
def rounds(tmp_path):
    return _bundle(tmp_path / "old.json", OLD), _bundle(tmp_path / "new.json", NEW)


class TestCompareRuns:
    def test_fixed_regressed_still_failing_added_removed(self, rounds):
        c = compare_runs(load_run(rounds[0]), load_run(rounds[1]))
        assert c.fixed == ["a"]
        assert c.regressed == ["b"]
        assert c.still_failing == ["c"]
        assert c.still_passing == 1
        assert c.added == ["fresh_fail", "fresh_pass"] and c.added_passed == ["fresh_pass"]
        assert c.removed == ["gone"]

    def test_accuracy_overall_per_tag_and_on_common_cases(self, rounds):
        c = compare_runs(load_run(rounds[0]), load_run(rounds[1]))
        assert c.old_overall == (3, 5) and c.new_overall == (3, 6)
        assert c.accuracy_delta == pytest.approx(50.0 - 60.0)
        assert c.old_common == (2, 4) and c.new_common == (2, 4)
        assert c.common_delta == 0
        assert c.per_tag["count"] == {"old": (1, 2), "new": (1, 2)}
        assert c.per_tag["rank"] == {"old": (2, 3), "new": (2, 4)}
        assert c.per_tag["x"] == {"old": (0, 1), "new": (1, 1)}

    def test_the_shift_in_mismatch_classes(self, rounds):
        c = compare_runs(load_run(rounds[0]), load_run(rounds[1]))
        assert c.classes["actual_empty"] == (1, 1)
        assert c.classes["row_count_differs"] == (0, 1)
        assert c.classes["match"] == (3, 3)
        assert c.classes["actual_is_subset"] == (1, 1)

    def test_notes_flag_a_changed_golden_file_and_reference(self, tmp_path):
        old = _bundle(tmp_path / "o.json", OLD, sha="g1", reference="live")
        new = _bundle(tmp_path / "n.json", NEW, sha="g2", reference="stored")
        notes = " ".join(compare_runs(load_run(old), load_run(new)).notes)
        assert "golden file differs" in notes and "judged differently" in notes
        assert "added" in notes

    def test_a_report_written_by_out_is_read_too(self, tmp_path):
        report = tmp_path / "report.json"
        report.write_text(json.dumps({"mode": "live", "reference": "live", "results": [
            {"case_id": "a", "tags": ["t"], "status": "pass", "passed": True,
             "diagnosis": {"mismatch_class": "match"}},
            {"case_id": "b", "tags": ["t"], "status": "fingerprint_mismatch", "passed": False,
             "diagnosis": {"mismatch_class": "actual_empty"}},
            {"case_id": "c", "tags": ["t"], "status": "guard_rejected", "passed": False},
            {"case_id": "d", "tags": ["t"], "status": "fingerprint_mismatch", "passed": False},
        ]}), encoding="utf-8")
        run = load_run(report)
        assert run.kind == "report"
        assert [run.cases[i].mismatch_class for i in "abcd"] == [
            "match", "actual_empty", "not_executed", "unclassified"]

    def test_unreadable_inputs_say_what_is_wrong(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_run(bad)
        other = tmp_path / "other.json"
        other.write_text('{"something": 1}', encoding="utf-8")
        with pytest.raises(ValueError, match="neither a share bundle"):
            load_run(other)
        newer = tmp_path / "newer.json"
        newer.write_text(json.dumps({"format_version": 99, "cases": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="newer than this tool"):
            load_run(newer)
        dup = _bundle(tmp_path / "dup.json", [_record("a", True), _record("a", False)])
        with pytest.raises(ValueError, match="appears twice"):
            load_run(dup)


class TestSplitFilter:
    def test_only_the_ids_of_the_half_take_part(self, rounds):
        c = compare_runs(load_run(rounds[0]), load_run(rounds[1]), only_ids={"b", "c", "d"},
                         split="test")
        assert c.fixed == [] and c.regressed == ["b"] and c.still_failing == ["c"]
        assert c.added == [] and c.removed == []
        assert c.old_overall == (2, 3)
        assert c.split == "test"

    def test_read_case_ids_accepts_text_json_list_and_json_object(self, tmp_path):
        text = tmp_path / "test_ids.txt"
        text.write_bytes("﻿# held out\r\nb\r\n\r\n  c  \r\n".encode("utf-8"))
        assert read_case_ids(text) == ["b", "c"]
        as_list = tmp_path / "ids.json"
        as_list.write_text('["b", "c"]', encoding="utf-8")
        assert read_case_ids(as_list, "test") == ["b", "c"]
        as_object = tmp_path / "split.json"
        as_object.write_text('{"dev_ids": ["a"], "test": ["b", "c"]}', encoding="utf-8")
        assert read_case_ids(as_object, "test") == ["b", "c"]
        assert read_case_ids(as_object, "dev") == ["a"]
        with pytest.raises(ValueError, match="no list of ids"):
            read_case_ids(tmp_path / "split.json", "nothing")
        empty = tmp_path / "empty.txt"
        empty.write_text("# nothing\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no case ids"):
            read_case_ids(empty)


class TestCompareCommand:
    def test_prints_ids_counts_and_no_question_text(self, rounds, capsys):
        assert cli.main(["compare", str(rounds[0]), str(rounds[1])]) == 0
        out = capsys.readouterr().out
        assert "Fixed (1): a" in out
        assert "Regressed (1): b" in out
        assert "Still failing (1): c" in out
        assert "Added (2): fresh_fail, fresh_pass, 1 passing" in out
        assert "Removed (1): gone" in out
        assert "60.00% (3/5) -> 50.00% (3/6)" in out
        assert "row_count_differs" in out

    def test_json_output(self, rounds, capsys):
        assert cli.main(["compare", str(rounds[0]), str(rounds[1]), "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["fixed"] == ["a"] and data["regressed"] == ["b"]
        assert data["accuracy"]["delta_points"] == pytest.approx(-10.0)
        assert data["mismatch_classes"]["row_count_differs"] == {"old": 0, "new": 1, "delta": 1}

    def test_exit_zero_without_the_flag_even_with_a_regression(self, rounds):
        assert cli.main(["compare", str(rounds[0]), str(rounds[1])]) == 0

    def test_fail_on_regression_in_test_exits_one(self, rounds, tmp_path, capsys):
        ids = tmp_path / "test_ids.txt"
        ids.write_text("b\nc\n", encoding="utf-8")
        code = cli.main(["compare", str(rounds[0]), str(rounds[1]), "--split", "test",
                         "--split-file", str(ids), "--fail-on-regression"])
        assert code == 1
        captured = capsys.readouterr()
        assert "Limited to the test split" in captured.out
        assert "REGRESSION in test: 1 case(s)" in captured.err

    def test_no_regression_in_the_half_exits_zero_even_if_the_other_half_regressed(
        self, rounds, tmp_path
    ):
        ids = tmp_path / "test_ids.txt"
        ids.write_text("a\nc\nd\n", encoding="utf-8")  # 'b' (the regression) is in dev
        assert cli.main(["compare", str(rounds[0]), str(rounds[1]), "--split", "test",
                         "--split-file", str(ids), "--fail-on-regression"]) == 0

    def test_dev_split_is_limited_the_same_way(self, rounds, tmp_path, capsys):
        ids = tmp_path / "dev_ids.txt"
        ids.write_text("a\nb\n", encoding="utf-8")
        cli.main(["compare", str(rounds[0]), str(rounds[1]), "--split", "dev",
                  "--split-file", str(ids), "--json"])
        data = json.loads(capsys.readouterr().out)
        assert data["split"] == "dev"
        assert data["fixed"] == ["a"] and data["regressed"] == ["b"] and data["still_failing"] == []

    def test_split_and_split_file_go_together(self, rounds, tmp_path, capsys):
        ids = tmp_path / "ids.txt"
        ids.write_text("a\n", encoding="utf-8")
        assert cli.main(["compare", str(rounds[0]), str(rounds[1]), "--split", "test"]) == 2
        assert cli.main(["compare", str(rounds[0]), str(rounds[1]), "--split-file", str(ids)]) == 2
        assert "go together" in capsys.readouterr().err

    def test_a_missing_or_malformed_input_is_exit_two_with_one_line(self, rounds, tmp_path, capsys):
        assert cli.main(["compare", str(tmp_path / "nope.json"), str(rounds[1])]) == 2
        err = capsys.readouterr().err
        assert err.startswith("error: ") and "Traceback" not in err

    def test_needs_no_project_config(self, rounds, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("compare must not look at project_config")

        monkeypatch.setattr(cli, "exit_if_project_config_unusable", refuse)
        assert cli.main(["compare", str(rounds[0]), str(rounds[1])]) == 0


def _cases(spec):
    return [GoldenCase(id=i, question=f"q {i}", expected_sql="SELECT 1", tags=[t]) for i, t in spec]


SPEC = [("c3", "count"), ("c1", "count"), ("c2", "count"), ("c4", "count"), ("c5", "count"),
        ("r2", "rank"), ("r1", "rank"), ("u1", "other")]


class TestSplit:
    def test_even_positions_to_dev_odd_to_test_within_each_first_tag(self):
        result = split_cases(_cases(SPEC))
        assert result.dev_ids == ["c1", "c3", "c5", "r1", "u1"]
        assert result.test_ids == ["c2", "c4", "r2"]
        assert result.categories == {
            "count": {"dev": 3, "test": 2}, "other": {"dev": 1, "test": 0},
            "rank": {"dev": 1, "test": 1},
        }
        assert result.counts == {"total": 8, "dev": 5, "test": 3}

    def test_only_the_first_tag_decides_the_category(self):
        cases = [GoldenCase(id=f"x{i}", question=f"q{i}", expected_sql="SELECT 1",
                            tags=["alpha", f"beta{i}"]) for i in range(4)]
        assert split_cases(cases).categories == {"alpha": {"dev": 2, "test": 2}}

    def test_untagged_cases_have_a_category_of_their_own(self):
        cases = [GoldenCase(id=i, question=i, expected_sql="SELECT 1") for i in ("a", "b")]
        assert split_cases(cases).categories == {"(untagged)": {"dev": 1, "test": 1}}

    def test_the_order_of_the_file_does_not_matter(self):
        reference = split_cases(_cases(SPEC))
        for seed in range(5):
            shuffled = list(SPEC)
            random.Random(seed).shuffle(shuffled)
            assert split_cases(_cases(shuffled)) == reference

    def test_non_runnable_cases_are_placed_too(self):
        cases = _cases(SPEC) + [GoldenCase(id="p1", question="q", status="pending_review",
                                            tags=["count"])]
        assert "p1" in split_cases(cases).test_ids + split_cases(cases).dev_ids

    def test_duplicate_ids_are_refused(self):
        with pytest.raises(ValueError, match="duplicate case id"):
            split_cases(_cases([("a", "t"), ("a", "t")]))


class TestSplitCommand:
    def _golden(self, tmp_path, spec=SPEC):
        path = tmp_path / "golden.jsonl"
        path.write_text(
            "".join(json.dumps(c.to_dict(), ensure_ascii=False) + "\n" for c in _cases(spec)),
            encoding="utf-8",
        )
        return path

    def test_writes_the_id_files_and_prints_the_counts(self, tmp_path, capsys):
        golden = self._golden(tmp_path)
        out = tmp_path / "split"
        assert cli.main(["split", "--golden", str(golden), "--out", str(out)]) == 0
        assert (out / "dev_ids.txt").read_text(encoding="utf-8") == "c1\nc3\nc5\nr1\nu1\n"
        assert (out / "test_ids.txt").read_text(encoding="utf-8") == "c2\nc4\nr2\n"
        summary = json.loads((out / "split.json").read_text(encoding="utf-8"))
        assert summary["counts"] == {"total": 8, "dev": 5, "test": 3}
        assert summary["golden"]["file_name"] == "golden.jsonl"
        printed = capsys.readouterr().out
        assert "8 cases -> 5 dev, 3 test" in printed
        assert "count" in printed and "rank" in printed

    def test_the_same_golden_set_always_gives_byte_identical_files(self, tmp_path):
        golden = self._golden(tmp_path)
        shuffled = list(SPEC)
        random.Random(7).shuffle(shuffled)
        other = tmp_path / "other"
        other.mkdir()
        golden_shuffled = self._golden(other, shuffled)
        for name, source in (("a", golden), ("b", golden_shuffled)):
            cli.main(["split", "--golden", str(source), "--out", str(tmp_path / name)])
        for name in ("dev_ids.txt", "test_ids.txt"):
            assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
        assert b"\r\n" not in (tmp_path / "a" / "dev_ids.txt").read_bytes()

    def test_running_it_again_is_fine_but_a_different_set_does_not_replace_the_halves(
        self, tmp_path, capsys
    ):
        golden = self._golden(tmp_path)
        out = tmp_path / "split"
        assert cli.main(["split", "--golden", str(golden), "--out", str(out)]) == 0
        assert cli.main(["split", "--golden", str(golden), "--out", str(out)]) == 0
        grown = self._golden(tmp_path, SPEC + [("c0", "count")])
        capsys.readouterr()
        assert cli.main(["split", "--golden", str(grown), "--out", str(out)]) == 2
        err = capsys.readouterr().err
        assert "must stay fixed" in err and "--force" in err
        assert (out / "test_ids.txt").read_text(encoding="utf-8") == "c2\nc4\nr2\n"
        assert cli.main(["split", "--golden", str(grown), "--out", str(out), "--force"]) == 0
        assert "c0" in (out / "dev_ids.txt").read_text(encoding="utf-8")

    def test_an_output_directory_inside_the_tracked_tree_is_refused(self, tmp_path, capsys):
        golden = self._golden(tmp_path)
        assert cli.main(["split", "--golden", str(golden), "--out", str(REPO_ROOT / "docs" / "s")]) == 2
        assert "tracked tree" in capsys.readouterr().err
        assert not (REPO_ROOT / "docs" / "s").exists()

    def test_an_unreadable_golden_file_is_exit_two_with_one_line(self, tmp_path, capsys):
        bad = tmp_path / "bad.jsonl"
        bad.write_text("{not json}\n", encoding="utf-8")
        assert cli.main(["split", "--golden", str(bad), "--out", str(tmp_path / "s")]) == 2
        err = capsys.readouterr().err
        assert err.startswith("error: ") and "Traceback" not in err
        assert cli.main(["split", "--golden", str(tmp_path / "nope.jsonl"),
                         "--out", str(tmp_path / "s")]) == 2

    def test_the_files_feed_compare_directly(self, tmp_path):
        golden = self._golden(tmp_path, [("a", "t"), ("b", "t"), ("c", "t"), ("d", "t")])
        out = tmp_path / "split"
        cli.main(["split", "--golden", str(golden), "--out", str(out)])
        old = _bundle(tmp_path / "o.json", [_record(i, True) for i in "abcd"])
        new = _bundle(tmp_path / "n.json",
                      [_record("a", True), _record("b", False), _record("c", True), _record("d", True)])
        # dev = a, c ; test = b, d  -> the regression (b) is in test
        assert cli.main(["compare", str(old), str(new), "--split", "test", "--split-file",
                         str(out / "test_ids.txt"), "--fail-on-regression"]) == 1
        assert cli.main(["compare", str(old), str(new), "--split", "dev", "--split-file",
                         str(out / "dev_ids.txt"), "--fail-on-regression"]) == 0
