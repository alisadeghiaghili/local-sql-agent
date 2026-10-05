# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for scripts/harvest_golden.py: candidates from fake audit logs.

The audit log is a ``tmp_path`` JSONL file of synthetic records (generic
names only); nothing here reads a real log or touches a database. What is
pinned: classification of records into outcomes, Persian-aware
de-duplication, the stratified sample, the "never overwrite without
--force" rule, and -- most importantly -- that the default console output
is counts only.
"""

from __future__ import annotations

import doctest
import json
import os
import random
from collections import Counter

import pytest

from eval.runner import load_golden_cases
from scripts import harvest_golden as hg
from scripts.harvest_golden import (
    Candidate,
    allocate,
    candidate_to_case,
    classify_record,
    detect_language,
    harvest,
    main,
    normalise_question,
    stratified_sample,
)

SECRET = "محرمانه-۱۲۳"  # stand-in for real user text that must never reach stdout


def _rec(question, *, sql="SELECT 1 AS n", code=None, ds="sales", ts="2026-10-01T10:00:00+00:00",
         rid=None, rows=1, **extra):
    record = {
        "timestamp": ts,
        "request_id": rid or f"req_{abs(hash((question, ts))) % 10**8}",
        "question": question,
        "generated_sql": sql,
        "error_code": code,
        "row_count": rows,
        "datasource": ds,
        "principal_id": "alice-the-analyst",
        "llm": {"model": "openai:real-model"},
    }
    record.update(extra)
    return record


def _write_log(path, records):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                    encoding="utf-8")
    return path


def _mixed_records():
    records = []
    for i in range(30):
        records.append(_rec(f"sales question number {i}", ds="sales", ts=f"2026-10-01T10:{i:02d}:00"))
    for i in range(10):
        records.append(_rec(f"inventory question number {i}", ds="inventory", ts=f"2026-10-02T10:{i:02d}:00"))
    for i in range(6):
        records.append(_rec(f"سؤال فارسی شماره {i}", ds="sales", ts=f"2026-10-03T10:{i:02d}:00"))
    for i in range(5):
        records.append(_rec(f"unrelated thing {i}?", sql="", code="OUT_OF_SCOPE", ds=None,
                            ts=f"2026-10-04T10:{i:02d}:00"))
    for i in range(4):
        records.append(_rec(f"bad sql question {i}", code="FORBIDDEN_SQL", ds=None,
                            ts=f"2026-10-05T10:{i:02d}:00"))
    return records


class TestClassification:
    def test_success_proposes_the_models_sql_and_datasource(self):
        cand, why = classify_record(_rec("how many?", sql="SELECT 1 AS n", ds="inventory"))
        assert why is None
        assert (cand.outcome, cand.proposed_sql, cand.datasource) == ("success", "SELECT 1 AS n", "inventory")

    def test_zero_rows_is_the_empty_outcome(self):
        cand, _ = classify_record(_rec("none?", rows=0))
        assert cand.outcome == "empty"
        assert candidate_to_case(cand).expect == "empty"

    def test_out_of_scope_proposes_no_sql(self):
        cand, _ = classify_record(_rec("weather?", sql="", code="OUT_OF_SCOPE"))
        case = candidate_to_case(cand)
        assert (cand.outcome, case.expect, case.expected_sql, case.datasource) == (
            "out_of_scope", "out_of_scope", None, None)

    def test_other_failures_propose_nothing_and_tag_the_error(self):
        cand, _ = classify_record(_rec("bad?", code="FORBIDDEN_SQL", sql="DROP TABLE x"))
        case = candidate_to_case(cand)
        assert case.expected_sql is None and case.expect == "success"
        assert "outcome:error" in case.tags and "error:FORBIDDEN_SQL" in case.tags

    @pytest.mark.parametrize(
        "code", ["MODEL_UNAVAILABLE", "MODEL_TIMEOUT", "DATABASE_UNAVAILABLE", "QUERY_TIMEOUT",
                 "SERVER_OVERLOAD"],
    )
    def test_transport_failures_are_not_about_the_question(self, code):
        assert classify_record(_rec("q?", code=code)) == (None, "transport_error")

    @pytest.mark.parametrize("question", [None, "", "   ", 42])
    def test_records_without_a_question_are_dropped(self, question):
        assert classify_record({**_rec("x"), "question": question})[1] == "no_question"

    @pytest.mark.parametrize("model", ["mock:stub", "ollama:test", "counting:test"])
    def test_stub_backends_are_dropped_unless_asked_for(self, model):
        record = _rec("q?", llm={"model": model})
        assert classify_record(record) == (None, "test_backend")
        assert classify_record(record, include_test_backends=True)[0] is not None

    def test_a_record_with_no_llm_block_is_kept(self):
        assert classify_record(_rec("cached?", llm=None, tier="T0"))[0] is not None

    def test_datasource_falls_back_to_the_routing_choice(self):
        record = _rec("q?", ds=None, datasource_selection={"chosen": "inventory", "reason": "keyword"})
        assert classify_record(record)[0].datasource == "inventory"

    def test_the_stored_question_is_the_users_wording_with_whitespace_collapsed(self):
        cand, _ = classify_record(_rec("  تعداد   سفارش‌های ۱۴۰۲  "))
        assert cand.question == "تعداد سفارش‌های ۱۴۰۲"  # ZWNJ and Persian digits kept

    @pytest.mark.parametrize(
        "text, lang",
        [("چند مشتری؟", "fa"), ("how many?", "en"), ("تعداد trades امروز", "mixed"),
         ("۱۴۰۲", "other"), ("12 ?", "other"), ("كم", "fa")],
    )
    def test_language_of_the_question(self, text, lang):
        assert detect_language(text) == lang


class TestDeduplication:
    def test_persian_and_arabic_spellings_are_one_question(self):
        variants = ["تعداد سفارش‌های ۱۴۰۲", "تعداد سفارشهای 1402", "تعداد  سفارشهای ١٤٠٢", "تعداد سفارشهاي 1402"]
        assert len({normalise_question(v) for v in variants}) == 1

    def test_case_and_whitespace_are_ignored(self):
        assert normalise_question("How  MANY orders?") == normalise_question("how many orders?")

    def test_the_most_recent_record_of_a_question_wins(self):
        records = [
            _rec("How many?", sql="SELECT 1 AS old", ts="2026-10-01"),
            _rec("how   many?", sql="SELECT 2 AS new", ts="2026-10-09"),
            _rec("HOW MANY?", sql="SELECT 3 AS mid", ts="2026-10-05"),
        ]
        result = harvest(records, n=10)
        assert result.duplicates == 2
        assert [c.expected_sql for c in result.cases] == ["SELECT 2 AS new"]

    def test_questions_already_in_a_golden_set_are_excluded(self):
        keys = frozenset({normalise_question("How many?")})
        result = harvest([_rec("how many?"), _rec("other?")], n=10, exclude_keys=keys)
        assert result.already_known == 1
        assert [c.question for c in result.cases] == ["other?"]


class TestAllocation:
    @pytest.mark.parametrize(
        "sizes, n",
        [({"a": 80, "b": 19, "c": 1}, 10), ({"a": 3, "b": 50}, 10), ({"a": 1, "b": 1, "c": 1}, 2),
         ({"a": 5}, 50), ({}, 5), ({"a": 10, "b": 10}, 0),
         ({str(i): i + 1 for i in range(12)}, 40), ({"x": 7, "y": 7, "z": 7}, 21)],
    )
    def test_invariants(self, sizes, n):
        slots = allocate(sizes, n)
        assert set(slots) == set(sizes)
        assert all(0 <= slots[s] <= sizes[s] for s in sizes)
        assert sum(slots.values()) == min(max(n, 0), sum(sizes.values()))

    def test_invariants_hold_for_arbitrary_strata(self):
        rng = random.Random(2026)
        for _ in range(300):
            sizes = {f"s{i}": rng.randint(1, 500) for i in range(rng.randint(0, 12))}
            n = rng.randint(-3, 800)
            slots = allocate(sizes, n)
            assert all(0 <= slots[s] <= sizes[s] for s in sizes)
            assert sum(slots.values()) == min(max(n, 0), sum(sizes.values()))
            if n >= len(sizes):
                assert all(slots[s] >= 1 for s in sizes)

    def test_every_stratum_is_represented_when_there_is_room(self):
        slots = allocate({"big": 1000, "small": 2, "tiny": 1}, 12)
        assert slots["small"] >= 1 and slots["tiny"] == 1
        assert slots["big"] > slots["small"]

    def test_with_fewer_slots_than_strata_the_largest_win(self):
        slots = allocate({"a": 9, "b": 5, "c": 1}, 2)
        assert (slots["a"], slots["b"], slots["c"]) == (1, 1, 0)


class TestStratifiedSample:
    def _candidates(self):
        return [c for r in _mixed_records() if (c := classify_record(r)[0])]

    def test_every_source_outcome_and_language_is_represented(self):
        sample = stratified_sample(self._candidates(), 20, seed=3)
        assert len(sample) == 20
        assert {c.datasource for c in sample} >= {"sales", "inventory", None}
        assert {c.outcome for c in sample} >= {"success", "out_of_scope", "error"}
        assert {c.lang for c in sample} >= {"en", "fa"}

    def test_same_seed_same_sample_different_seed_differs(self):
        pool = self._candidates()
        first = [c.key for c in stratified_sample(pool, 12, seed=1)]
        assert first == [c.key for c in stratified_sample(pool, 12, seed=1)]
        assert first != [c.key for c in stratified_sample(pool, 12, seed=2)]

    def test_asking_for_more_than_exists_returns_everything_once(self):
        pool = self._candidates()
        sample = stratified_sample(pool, 10_000)
        assert len(sample) == len(pool) == len({c.key for c in sample})

    def test_sample_follows_the_traffic_mix_not_a_flat_split(self):
        counts = Counter(c.datasource for c in stratified_sample(self._candidates(), 20, seed=0))
        assert counts["sales"] > counts["inventory"]


class TestCandidateCases:
    def test_case_is_a_pending_review_candidate_naming_its_source_record_only(self):
        cand, _ = classify_record(_rec("How many?", rid="req_42", ts="2026-10-01T10:00:00"))
        case = candidate_to_case(cand)
        assert case.status == "pending_review" and not case.is_runnable
        assert case.expected_rows is None and case.expected_fingerprint is None
        assert "req_42" in case.notes and "2026-10-01T10:00:00" in case.notes
        assert "alice" not in json.dumps(case.to_dict())
        assert case.tags == ["lang:en", "source:sales", "outcome:success"]

    def test_ids_are_stable_across_runs_and_distinct_across_records(self):
        recs = [_rec("one?", rid="r1"), _rec("two?", rid="r2")]
        ids = [c.id for c in harvest(recs, n=5).cases]
        assert ids == [c.id for c in harvest(recs, n=5).cases]
        assert len(set(ids)) == 2 and all(i.startswith("cand_") for i in ids)


class TestCli:
    def _run(self, tmp_path, records, *args):
        log = _write_log(tmp_path / "audit_log.jsonl", records)
        out = tmp_path / "eval_data" / "candidates.jsonl"
        code = main([str(log), "--out", str(out), *args])
        return code, out

    def test_writes_loadable_pending_review_cases(self, tmp_path):
        code, out = self._run(tmp_path, _mixed_records(), "--n", "15")
        assert code == 0
        cases = load_golden_cases(out)
        assert len(cases) == 15
        assert {c.status for c in cases} == {"pending_review"}
        assert all(c.expected_rows is None for c in cases)

    def test_default_sample_size_is_150(self, tmp_path):
        records = [_rec(f"distinct question {i}", ts=f"2026-10-01T10:00:{i % 60:02d}") for i in range(200)]
        code, out = self._run(tmp_path, records)
        assert code == 0 and len(load_golden_cases(out)) == 150

    def test_default_output_is_counts_only(self, tmp_path, capsys):
        records = [_rec(f"{SECRET} {i}", sql=f"SELECT {i} AS leaked_sql") for i in range(5)]
        records.append(_rec("weather?", sql="", code="OUT_OF_SCOPE", error_message=SECRET))
        code, _ = self._run(tmp_path, records)
        printed = capsys.readouterr()
        assert code == 0
        everything = printed.out + printed.err
        assert SECRET not in everything
        assert "leaked_sql" not in everything and "SELECT" not in everything
        assert "req_" not in everything and "alice" not in everything
        assert "wrote 6 candidate(s)" in printed.out
        assert "THIS OUTPUT INCLUDES VERBATIM" not in printed.out

    def test_include_examples_is_an_explicit_opt_in_and_says_so(self, tmp_path, capsys):
        code, _ = self._run(tmp_path, [_rec(f"{SECRET}?")], "--include-examples")
        text = capsys.readouterr().out
        assert code == 0
        assert text.splitlines()[0].startswith("*** THIS OUTPUT INCLUDES VERBATIM")
        assert SECRET in text

    def test_never_overwrites_without_force(self, tmp_path, capsys):
        code, out = self._run(tmp_path, [_rec("first?")])
        assert code == 0
        before = out.read_bytes()
        log = tmp_path / "audit_log.jsonl"
        _write_log(log, [_rec("second?")])
        assert main([str(log), "--out", str(out)]) == 2
        assert out.read_bytes() == before
        assert "refusing to overwrite" in capsys.readouterr().err

    def test_force_overwrites_and_keeps_a_bak(self, tmp_path):
        code, out = self._run(tmp_path, [_rec("first?")])
        before = out.read_bytes()
        _write_log(tmp_path / "audit_log.jsonl", [_rec("second?")])
        assert main([str(tmp_path / "audit_log.jsonl"), "--out", str(out), "--force"]) == 0
        assert out.read_bytes() != before
        assert (tmp_path / "eval_data" / "candidates.jsonl.bak").read_bytes() == before

    def test_output_file_is_owner_only(self, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX permissions")
        _, out = self._run(tmp_path, [_rec("q?")])
        assert (out.stat().st_mode & 0o777) == 0o600

    def test_reads_rotated_backups_via_a_glob(self, tmp_path):
        _write_log(tmp_path / "audit_log.jsonl", [_rec("recent?", ts="2026-10-09")])
        _write_log(tmp_path / "audit_log.jsonl.1", [_rec("older?", ts="2026-10-01")])
        out = tmp_path / "c.jsonl"
        assert main([str(tmp_path / "audit_log.jsonl*"), "--out", str(out)]) == 0
        assert {c.question for c in load_golden_cases(out)} == {"recent?", "older?"}

    def test_no_matching_log_is_an_error(self, tmp_path, capsys):
        assert main([str(tmp_path / "nothing*.jsonl"), "--out", str(tmp_path / "c.jsonl")]) == 1
        assert "No log files matched" in capsys.readouterr().err

    def test_exclude_drops_questions_already_in_the_golden_set(self, tmp_path):
        golden = tmp_path / "golden.jsonl"
        golden.write_text(json.dumps({"id": "g", "question": "How MANY?",
                                      "expected_sql": "SELECT 1"}) + "\n", encoding="utf-8")
        log = _write_log(tmp_path / "audit_log.jsonl", [_rec("how many?"), _rec("another?")])
        out = tmp_path / "c.jsonl"
        assert main([str(log), "--out", str(out), "--exclude", str(golden)]) == 0
        assert [c.question for c in load_golden_cases(out)] == ["another?"]

    def test_malformed_lines_are_skipped(self, tmp_path):
        log = tmp_path / "audit_log.jsonl"
        log.write_text('{"truncated": \n' + json.dumps(_rec("ok?")) + "\n[1,2]\n", encoding="utf-8")
        out = tmp_path / "c.jsonl"
        assert main([str(log), "--out", str(out)]) == 0
        assert len(load_golden_cases(out)) == 1

    def test_invalid_n_is_a_usage_error(self, tmp_path):
        assert main([str(tmp_path / "x"), "--n", "0"]) == 2


def test_module_doctests_pass():
    failed, attempted = doctest.testmod(hg, verbose=False)
    assert attempted > 0 and failed == 0
