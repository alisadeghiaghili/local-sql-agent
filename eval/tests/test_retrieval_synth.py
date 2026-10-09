# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Tests for the synthetic retrieval benchmark generator.

The generator is pure and seeded, so most tests build a small benchmark in
memory. One test writes it to disk and measures it with the real
``eval.cli recall`` in a child process (the project configuration is read once
per process, so the benchmark cannot be loaded into this one), proving that
every generated question's reference SQL resolves against the generated
schema.

Run::

    PROJECT_CONFIG_DIR=project_config.example python -m pytest eval/tests/test_retrieval_synth.py -q
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from database.datasources import validate_datasources_yaml_text
from eval.benchmarks.retrieval_synth import (
    BenchmarkConfig,
    generate,
    run_benchmark,
    summarise,
    write_benchmark,
)
from knowledge.config_loader import (
    AliasesConfig,
    EntitiesConfig,
    RetrievalHintsConfig,
    validate_yaml_text,
)
from schema_data.registry import validate_schema_yaml_text

_SMALL = BenchmarkConfig(tables=80, questions_per_language=24)


@pytest.fixture(scope="module")
def bench():
    return generate(_SMALL)


class TestDeterminism:
    def test_same_seed_gives_identical_files_and_questions(self, bench):
        again = generate(_SMALL)
        assert again.files == bench.files
        assert again.golden == bench.golden

    def test_other_seed_gives_a_different_benchmark(self, bench):
        other = generate(BenchmarkConfig(tables=80, questions_per_language=24, seed=8))
        assert other.files["schema.yaml"] != bench.files["schema.yaml"]
        assert other.golden != bench.golden


class TestShape:
    def test_table_count_is_near_the_target(self):
        assert abs(len(generate(BenchmarkConfig(tables=120, questions_per_language=6)).tables) - 120) <= 3

    def test_default_benchmark_is_a_few_hundred_tables_over_two_sources(self):
        default = generate(BenchmarkConfig(questions_per_language=6))
        assert 300 <= len(default.tables) <= 500
        sources = {s for t in default.tables for s in t.sources}
        assert len(sources) >= 2

    def test_table_names_are_unique_apart_from_the_deliberate_collisions(self, bench):
        keys = [t.key for t in bench.tables]
        assert len(keys) == len(set(keys))
        bare = [t.bare for t in bench.tables]
        duplicated = {b for b in bare if bare.count(b) > 1}
        assert duplicated  # same-name tables exist ...
        for name in duplicated:  # ... and each is a qualified key in two sources
            twins = [t for t in bench.tables if t.bare == name]
            assert len(twins) == 2
            assert all("." in t.key for t in twins)
            assert len({t.schema for t in twins}) == 2

    def test_shared_dimensions_live_in_every_source(self, bench):
        shared = [t for t in bench.tables if t.kind == "shared"]
        assert shared
        assert all(set(t.sources) == set(bench.config.sources) for t in shared)
        schema = yaml.safe_load(bench.files["schema.yaml"])
        assert schema["tables"]["Calendar"]["datasource"] == list(bench.config.sources)

    def test_bridges_come_in_both_styles(self):
        big = generate(BenchmarkConfig(tables=200, questions_per_language=6))
        bridges = [t for t in big.tables if t.kind == "bridge"]
        assert any(t.opaque for t in bridges)
        assert any(not t.opaque for t in bridges)
        opaque = next(t for t in bridges if t.opaque)
        assert re.fullmatch(r"Lnk\d{4}", opaque.bare)
        # an opaque bridge's description names neither side
        assert "cross-reference" in opaque.description

    def test_foreign_keys_are_declared_in_three_ways(self):
        big = generate(BenchmarkConfig(tables=200, questions_per_language=6))
        assert {e.channel for e in big.edges} == {"schema", "relationships", "inferred"}
        for edge in big.edges:
            if edge.channel == "inferred":  # undeclared keys follow the convention
                dst = next(t for t in big.tables if t.key == edge.dst)
                assert re.fullmatch(rf"{dst.bare}_?ID", edge.column.rstrip("x"))

    def test_invalid_options_are_refused(self):
        with pytest.raises(ValueError, match="two data sources"):
            BenchmarkConfig(sources=("only",))
        with pytest.raises(ValueError, match="at least 40"):
            BenchmarkConfig(tables=10)
        with pytest.raises(ValueError, match="between 0 and 1"):
            BenchmarkConfig(alias_coverage=1.5)


class TestConfigFiles:
    def test_every_file_validates_with_the_application_loaders(self, bench):
        schema = validate_schema_yaml_text(bench.files["schema.yaml"])
        assert len(schema.tables) == len(bench.tables)
        validate_datasources_yaml_text(bench.files["datasources.yaml"])
        entities = validate_yaml_text("entities.yaml", bench.files["entities.yaml"], EntitiesConfig)
        hints = validate_yaml_text("retrieval_hints.yaml", bench.files["retrieval_hints.yaml"],
                                   RetrievalHintsConfig)
        validate_yaml_text("aliases.yaml", bench.files["aliases.yaml"], AliasesConfig)
        assert set(hints.fact_tables) <= set(schema.tables)
        assert set(entities.entities) <= set(schema.tables)
        assert set(hints.fact_patterns) <= set(schema.tables)

    def test_relationship_files_name_known_tables(self, bench):
        schema = validate_schema_yaml_text(bench.files["schema.yaml"])
        for rel in schema.relationships:
            assert rel.from_table in schema.tables and rel.to_table in schema.tables
        known = {t.bare for t in bench.tables}
        for entry in yaml.safe_load(bench.files["relationships.yaml"])["relationships"]:
            assert entry["from_table"] in known and entry["to_table"] in known

    def test_only_part_of_the_tables_carry_aliases(self, bench):
        entities = yaml.safe_load(bench.files["entities.yaml"])["entities"]
        dims = [t for t in bench.tables if t.kind in ("dim", "child", "parent", "collision", "shared")]
        assert 0 < len(entities) < len(dims)

    def test_all_ten_required_files_are_present(self, bench):
        from core.project_config_files import REQUIRED_PROJECT_CONFIG_FILES

        assert set(REQUIRED_PROJECT_CONFIG_FILES) <= set(bench.files)


class TestQuestions:
    def test_both_languages_and_every_kind(self, bench):
        tags = {tag for case in bench.golden for tag in case["tags"]}
        assert {"en", "fa", "multihop", "dim_list", "fact_dim", "fact_two", "fact_time",
                "fact_dim_time", "snowflake", "bridge", "bridge_parent", "measure"} <= tags

    def test_question_counts_per_language(self, bench):
        for lang in ("en", "fa"):
            assert sum(1 for c in bench.golden if lang in c["tags"]) == _SMALL.questions_per_language

    def test_questions_are_unique_and_persian_ones_are_persian(self, bench):
        questions = [c["question"] for c in bench.golden]
        assert len(questions) == len(set(questions))
        for case in bench.golden:
            has_persian = bool(re.search(r"[؀-ۿ]", case["question"]))
            assert has_persian == ("fa" in case["tags"])

    def test_every_case_states_its_source_and_a_reference_sql(self, bench):
        for case in bench.golden:
            assert case["datasource"] in bench.config.sources
            assert case["expected_sql"].startswith("SELECT")

    def test_multihop_gold_has_an_intermediate_table(self, bench):
        by_key = {t.key: t for t in bench.tables}
        for case in bench.golden:
            if "multihop" not in case["tags"]:
                continue
            gold = case["notes"].removeprefix("gold tables: ").split(", ")
            kinds = sorted(by_key[k].kind for k in gold)
            if "bridge" in case["tags"] or "bridge_parent" in case["tags"]:
                # fact, bridge, dimension -- and the bridge is never named
                assert "bridge" in kinds, case["id"]
                bridge = next(by_key[k] for k in gold if by_key[k].kind == "bridge")
                label = bridge.en if "en" in case["tags"] else bridge.fa
                assert label not in case["question"]
            else:
                # fact, child dimension, its classification -- only the last is named
                assert kinds.count("parent") == 1 and kinds.count("child") == 1, case["id"]

    def test_golden_lines_load_as_golden_cases(self, bench):
        from eval.models import GoldenCase

        for line in bench.golden_jsonl().splitlines():
            GoldenCase.from_dict(json.loads(line))


class TestMeasuredEndToEnd:
    def test_written_benchmark_scores_every_question(self, tmp_path):
        bench = generate(BenchmarkConfig(tables=70, questions_per_language=12))
        config_dir = write_benchmark(bench, tmp_path)
        assert (config_dir / "schema.yaml").is_file()
        assert (tmp_path / "golden.jsonl").read_text(encoding="utf-8") == bench.golden_jsonl()

        report = run_benchmark(tmp_path)
        assert report["total_cases"] == len(bench.golden)
        assert report["skipped"] == []          # every reference SQL resolved
        assert report["scored"] == len(bench.golden)
        assert 0.0 <= report["mean_recall"] <= 1.0
        assert report["source_selection"]["total"] == len(bench.golden)

        again = run_benchmark(tmp_path)
        assert again == report                    # deterministic across processes

        text = summarise(report)
        assert text.splitlines()[0].startswith("slice")
        assert "English multi-hop" in text
        assert summarise(report, markdown=True).splitlines()[1].startswith("|---")
