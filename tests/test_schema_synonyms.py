# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Step 3b of the setup wizard: ``aliases.yaml`` ``synonyms`` drafted from the schema's own words.

Since 6.14.2 the wizard wrote ``synonyms: {}`` unless ``--value-synonyms`` was
given, and since 6.16.3 ``ring_aliases: {}`` as well, so every deployment that
ran it got an empty ``aliases.yaml``. Step 3b fills ``synonyms`` again, without
the two defects that emptied it: no key is a value read from a row (only table
and column names reach the model) and no entry is keyed by a table.

The rules pinned here are the ones the retriever needs for an entry to do
anything: a key is one word that is not already a term, and each value is a word
of a table or non-key column name (``schema_data.retriever._expand``). Every
name is invented.
"""

from __future__ import annotations

import json
import re

import pytest

import setup_project as sp
from core.synonym_drafts import clean_synonyms, schema_vocabulary, synonyms_from_entities
from knowledge.config_loader import AliasesConfig
from tests.test_setup_wizard_drafts import RecordingLLM, read_yaml, run_wizard, snapshot_of, table
from tests.test_setup_wizard_output import lab  # noqa: F401 - the two-source fixture


@pytest.fixture(autouse=True)
def _unwrapped_console(monkeypatch):
    """Keep the wizard's console from wrapping its lines (see test_setup_wizard.py)."""
    if hasattr(sp.console, "_width"):
        monkeypatch.setattr(sp.console, "_width", 10_000)


# ---------------------------------------------------------------------------
# 1. The vocabulary and the rules
# ---------------------------------------------------------------------------

class TestVocabulary:
    def test_key_columns_give_no_term_and_names_are_split_into_words(self):
        vocabulary = schema_vocabulary([("Deal", "ForwardDeal", ["ID", "Hall_ID", "SettlementPrice", "lot_size"])])
        assert vocabulary.terms == ("forward", "deal", "settlement", "price", "lot", "size")
        assert vocabulary.by_table["Deal"] == vocabulary.terms

    def test_a_plural_is_found_under_its_term(self):
        vocabulary = schema_vocabulary([("Deal", "Deal", ["Price"])])
        assert "deals" in vocabulary and vocabulary.canonical("prices") == "price"


class TestCleaning:
    VOCABULARY = schema_vocabulary([("Deal", "Deal", ["Volume", "SettlementPrice"])])

    def test_only_one_word_keys_that_are_not_terms_and_map_to_terms_are_kept(self):
        got = clean_synonyms({"synonyms": {
            "حجم": ["volume"],
            "نرخ تسویه": ["settlement", "price"],   # a phrase: never looked up
            "Volume": ["volume"],                    # a term: already matches by itself
            "مبلغ": ["amount"],                      # no such term
            "1402/01/01": ["deal"],                  # value-shaped
            "12345": ["deal"],
        }}, self.VOCABULARY)
        assert got.synonyms == {"حجم": ["volume"]}
        assert dict(got.dropped) == {"not_one_word": 2, "already_a_term": 1, "no_known_term": 1, "not_a_word": 1}

    def test_values_are_spelled_as_the_vocabulary_spells_them_and_a_phrase_of_terms_stays(self):
        got = clean_synonyms({"بها": ["Prices", "settlement price", "unknown"]}, self.VOCABULARY)
        assert got.synonyms == {"بها": ["price", "settlement price"]}

    def test_a_list_answer_and_a_string_value_are_read(self):
        got = clean_synonyms([{"word": "معامله", "terms": "deal"}, {"word": "x", "terms": []}], self.VOCABULARY)
        assert got.synonyms == {"معامله": ["deal"]}

    def test_a_repeated_key_unites_its_values_up_to_the_limit(self):
        got = clean_synonyms([{"word": "نرخ", "terms": ["price"]}, {"word": "نرخ", "terms": ["settlement", "volume"]}],
                             self.VOCABULARY, max_values=2)
        assert got.synonyms == {"نرخ": ["price", "settlement"]}

    def test_at_most_max_entries_keys(self):
        answer = {f"واژه{chr(0x0628 + i)}": ["deal"] for i in range(5)}
        got = clean_synonyms(answer, self.VOCABULARY, max_entries=3)
        assert len(got.synonyms) == 3 and got.dropped["over_limit"] == 2

    def test_anything_that_is_not_an_answer_gives_nothing(self):
        for answer in (None, "text", 3, {"synonyms": "x"}, []):
            assert clean_synonyms(answer, self.VOCABULARY).synonyms == {}


class TestFromEntities:
    def test_a_one_word_alias_maps_to_the_words_of_its_table(self):
        vocabulary = schema_vocabulary([("ForwardDeal", "ForwardDeal", ["Volume"]), ("Hall", "Hall", ["Name"])])
        entities = {
            "ForwardDeal": {"table": "ForwardDeal", "aliases": ["سلف", "forward deal", "نوع قرارداد"]},
            "Hall": {"table": "Hall", "aliases": ["تالار", "hall"]},
        }
        assert synonyms_from_entities(entities, vocabulary) == {"سلف": ["forward", "deal"], "تالار": ["hall"]}

    def test_an_entry_that_is_not_an_entity_or_names_no_term_gives_nothing(self):
        vocabulary = schema_vocabulary([("Hall", "Hall", ["Name"])])
        entities = {
            "broken": "not a mapping",
            "T1": {"table": "T1", "aliases": ["یک"]},               # no word of two letters in the name
            "Hall": {"table": "Hall", "aliases": [7, None, "تالار"]},
        }
        assert synonyms_from_entities(entities, vocabulary) == {"تالار": ["hall"]}


class TestEdges:
    VOCABULARY = schema_vocabulary([("Deal", "Deal", ["Categories", "Boxes", "Price"])])

    def test_the_plural_forms_the_retriever_stems_meet_their_term(self):
        assert self.VOCABULARY.canonical("category") == "categories"
        assert self.VOCABULARY.canonical("box") == "boxes"
        assert "category" in self.VOCABULARY and "box" in self.VOCABULARY

    def test_only_text_is_ever_in_the_vocabulary(self):
        assert 7 not in self.VOCABULARY and None not in self.VOCABULARY

    def test_keys_and_values_that_are_not_text_are_left_out(self):
        got = clean_synonyms(
            [{"word": 7, "terms": ["price"]}, "not an object", {"word": "بها", "terms": [None, "", "price"]}],
            self.VOCABULARY,
        )
        assert got.synonyms == {"بها": ["price"]} and dict(got.dropped) == {"not_a_word": 1}

    def test_a_one_word_key_shaped_like_a_stored_value_is_left_out(self):
        got = clean_synonyms({"ref123456789": ["price"]}, self.VOCABULARY)
        assert got.synonyms == {} and dict(got.dropped) == {"value_shaped": 1}


# ---------------------------------------------------------------------------
# 2. In the wizard
# ---------------------------------------------------------------------------

class SynonymLLM(RecordingLLM):
    """Answers the synonyms prompt with a word per term it was given, plus entries the rules must drop."""

    def generate(self, prompt: str, expect_json: bool = True):
        if "search vocabulary" not in prompt:
            return super().generate(prompt, expect_json)
        self.prompts.append(prompt)
        terms = re.search(r"Terms \(words of the table and column names\):\n(.*)\n", prompt).group(1).split(", ")
        source = re.sub(r"\W", "", re.search(r"Data source: (.*)\n", prompt).group(1))
        answer = {f"w{source}{term}": [term] for term in terms}
        answer.update({"two words": [terms[0]], terms[0]: [terms[0]], "2024-05-01": [terms[0]], "zz": ["nope"]})
        return {"synonyms": answer}


def _synonym_prompts(llm) -> list[str]:
    return [p for p in llm.prompts if "search vocabulary" in p]


class TestTwoSources:
    def test_each_source_is_asked_about_its_own_names_and_both_end_up_in_the_file(self, lab):  # noqa: F811
        llm = SynonymLLM()
        run = lab.run(llm=llm)

        assert run.code == 0, run.out
        sales, reports = _synonym_prompts(llm)
        assert "sales.Trade" in sales and "rep.Summary" not in sales
        assert "rep.Summary" in reports and "sales.Trade" not in reports
        synonyms = read_yaml(run.directory / "aliases.yaml")["synonyms"]
        assert synonyms["wsalesamount"] == ["amount"] and synonyms["wreportstotal"] == ["total"]
        assert not {"two words", "2024-05-01", "zz"} & set(synonyms)
        assert all(not key.startswith(("customer", "trade", "summary")) for key in synonyms)
        done = run.log()["step3_synonyms"]
        assert set(done["per_source"]) == {"sales", "reports"} and done["synonym_count"] == len(synonyms)
        assert "drafted from table and column names" in run.out

    def test_no_row_value_reaches_the_prompt(self, lab):  # noqa: F811
        llm = SynonymLLM()
        lab.run(llm=llm)
        for prompt in _synonym_prompts(llm):
            assert "Open" not in prompt and "09120000001" not in prompt and "Bank One" not in prompt

    def test_the_file_loads_and_writes_one_line_per_entry(self, lab):  # noqa: F811
        run = lab.run(llm=SynonymLLM())
        text = (run.directory / "aliases.yaml").read_text(encoding="utf-8")
        AliasesConfig(**read_yaml(run.directory / "aliases.yaml"))
        assert "  wsalesamount: [amount]\n" in text
        assert "ring_aliases: {}" in text


class TestOptionsAndRuns:
    SNAP = snapshot_of(table("mkt", "Deal", "ID", "Volume", "Price", kind="fact"), table("mkt", "Hall", "ID", "Name"))

    def test_no_schema_synonyms_asks_nothing_and_writes_none(self, tmp_path):
        llm = SynonymLLM()
        assert run_wizard(tmp_path / "d", self.SNAP, llm, "--no-schema-synonyms") == 0
        assert _synonym_prompts(llm) == []
        assert read_yaml(tmp_path / "d" / "aliases.yaml")["synonyms"] == {}

    def test_a_failing_model_gives_no_synonyms_and_the_run_still_succeeds(self, tmp_path):
        class Failing(RecordingLLM):
            def generate(self, prompt, expect_json=True):
                if "search vocabulary" in prompt:
                    raise RuntimeError("endpoint down")
                return super().generate(prompt, expect_json)

        assert run_wizard(tmp_path / "d", self.SNAP, Failing()) == 0
        assert read_yaml(tmp_path / "d" / "aliases.yaml")["synonyms"] == {}
        assert json.loads((tmp_path / "d" / ".setup_log.json").read_text(encoding="utf-8"))["step3_synonyms"]["failed"]

    def test_earlier_curated_synonyms_are_kept_and_new_ones_added(self, tmp_path):
        out = tmp_path / "d"
        out.mkdir()
        (out / "aliases.yaml").write_text('ring_aliases: {}\nsynonyms:\n  "معامله": ["deal"]\n', encoding="utf-8")
        assert run_wizard(out, self.SNAP, SynonymLLM()) == 0
        synonyms = read_yaml(out / "aliases.yaml")["synonyms"]
        assert synonyms["معامله"] == ["deal"]
        assert synonyms["walltablesvolume"] == ["volume"]

    def test_resume_reuses_the_stored_synonyms_without_asking_again(self, tmp_path):
        out = tmp_path / "d"
        assert run_wizard(out, self.SNAP, SynonymLLM()) == 0
        first = read_yaml(out / "aliases.yaml")["synonyms"]
        (out / "aliases.yaml").unlink()
        llm = SynonymLLM()
        assert run_wizard(out, self.SNAP, llm, "--resume") == 0
        assert _synonym_prompts(llm) == []
        assert read_yaml(out / "aliases.yaml")["synonyms"] == first
