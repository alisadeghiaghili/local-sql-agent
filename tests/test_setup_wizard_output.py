# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""What the setup wizard writes: one entity per table, no values from rows, examples that say when they are missing.

Six defects of the 6.13 and 6.14 wizard, seen in the drafts of a two-source SQL
Server deployment and each pinned by a class below:

* ``entities.yaml`` held two entries for most tables, a bare key with no aliases
  and a ``Schema.Table`` key with the aliases, and the second matched no key of
  ``schema.yaml`` (``TestOneEntityPerTable``, ``TestKeysAreTheSyncKeys``);
* ``aliases.yaml`` ``synonyms`` held hundreds of raw values of the rows, dates,
  phone numbers, addresses, masked names (``TestNoValueSynonymsByDefault``,
  ``TestValueSynonymsOptIn``);
* ``examples.yaml`` was written empty without a word (``TestExamples``);
* a dimension table got the rule ``Value column: ID. Volume column: ID.``
  (``TestRulesNeedAMeasure``);
* one table got 22 aliases made of the same three words (``TestAliasList``).

The database is the fake of ``tests/test_setup_wizard_drafts.py`` or, where a
value has to be read, SQLite files attached as schemas. Every name is invented.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import config as cfg
import setup_project as sp
from core.wizard_drafts import tidy_aliases
from database.datasources import reset_datasources_cache
from database.schema_inspector import ColumnInfo, SchemaInspector, TableInfo
from llm.wizard_llm import WizardLLM
from tests._inspector_fixtures import build_database
from tests.test_setup_wizard_datasources import DATASOURCES, World
from tests.test_setup_wizard_drafts import (
    RecordingLLM,
    SilentLLM,
    entity_keys,
    read_yaml,
    run_wizard,
    snapshot_of,
    table,
)


@pytest.fixture(autouse=True)
def _unwrapped_console(monkeypatch):
    """Keep the wizard's console from wrapping its lines (see test_setup_wizard.py)."""
    if hasattr(sp.console, "_width"):
        monkeypatch.setattr(sp.console, "_width", 10_000)


def _write(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. One entity per table
# ---------------------------------------------------------------------------

def _unique_tables():
    """Three tables whose names no other schema shares."""
    return snapshot_of(
        table("Sales_Dim", "ActionType"),
        table("Ref_Dim", "Date"),
        table("Sales_Fact", "Trade", "ID", "Amount", kind="fact"),
    )


class TestOneEntityPerTable:
    def test_an_inspector_draft_of_the_same_tables_is_merged_not_duplicated(self, tmp_path):
        # The draft the inspector writes: bare keys for unique names, `schema:` (not
        # `schema_name:`), no aliases. The wizard read no schema from it, so every
        # bare entry looked like a second schema of its own name.
        out, snap = tmp_path / "draft", _unique_tables()
        _write(out, "entities.yaml", SchemaInspector("sqlite://").draft_entities_yaml(snap))

        assert run_wizard(out, snap) == 0

        entities = read_yaml(out / "entities.yaml")["entities"]
        assert list(entities) == ["ActionType", "Date", "Trade"]
        assert entities["ActionType"]["aliases"] == ["alias of Sales_Dim.ActionType"]
        assert entities["ActionType"]["schema_name"] == "Sales_Dim"
        assert sorted(read_yaml(out / "aliases.yaml")["ring_aliases"]) == ["ActionType", "Date", "Trade"]
        assert sorted(read_yaml(out / "business_rules.yaml")["rules"]) == ["Trade"]

    def test_an_entry_with_no_schema_is_the_table_its_name_says(self, tmp_path):
        out = tmp_path / "draft"
        _write(out, "entities.yaml", "entities:\n  Bank:\n    aliases: [lender]\n    table: Bank\n")

        run_wizard(out, snapshot_of(table("Ref_Dim", "Bank")), SilentLLM(aliases=("Ref_Dim.Bank",)))

        entities = read_yaml(out / "entities.yaml")["entities"]
        assert list(entities) == ["Bank"]
        assert entities["Bank"]["aliases"] == ["lender"]          # the model had nothing: kept
        assert entities["Bank"]["schema_name"] == "Ref_Dim"       # and the schema is now known

    def test_the_bare_and_qualified_entries_of_the_6_14_wizard_become_one_with_the_aliases(self, tmp_path, capsys):
        out = tmp_path / "draft"
        _write(out, "entities.yaml", (
            "entities:\n"
            "  ActionType:\n    aliases: []\n    table: ActionType\n    schema_name: Sales_Dim\n"
            "  Sales_Dim.ActionType:\n    aliases: [action kind]\n    table: ActionType\n    schema_name: Sales_Dim\n"
        ))
        _write(out, "aliases.yaml", (
            "ring_aliases:\n  Sales_Dim.ActionType: [action kind]\nsynonyms: {}\n"
        ))
        _write(out, "business_rules.yaml", (
            "rules:\n  Sales_Fact.Trade:\n    rule_text: Use Amount.\n  Trade:\n    rule_text: 'Primary fact table: Trade. Columns: ID.'\n"
        ))
        snap = snapshot_of(table("Sales_Dim", "ActionType"), table("Sales_Fact", "Trade", "ID", "Amount", kind="fact"))

        run_wizard(out, snap, SilentLLM(aliases=("Sales_Dim.ActionType",), rules=("Sales_Fact.Trade",)))

        entities = read_yaml(out / "entities.yaml")["entities"]
        assert [k for k, v in entities.items() if v["table"] == "ActionType"] == ["ActionType"]
        assert entities["ActionType"]["aliases"] == ["action kind"]
        assert read_yaml(out / "aliases.yaml")["ring_aliases"] == {
            "ActionType": ["action kind"], "Trade": ["alias of Sales_Fact.Trade"],
        }
        rules = read_yaml(out / "business_rules.yaml")["rules"]
        assert list(rules) == ["Trade"] and rules["Trade"]["rule_text"] == "Use Amount."
        summary = capsys.readouterr().out
        assert "Merged 1 duplicate entry of one table in entities.yaml: Sales_Dim.ActionType -> ActionType" in summary
        log = json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))
        assert log["step6_write"]["merged_duplicates"]["entities.yaml"] == [["Sales_Dim.ActionType", "ActionType"]]

    def test_a_second_run_never_leaves_two_entries_for_a_table(self, tmp_path):
        out = tmp_path / "draft"
        first = _unique_tables()
        run_wizard(out, first)
        run_wizard(out, snapshot_of(table("Ref_Dim", "Date"), table("Ref_Dim", "Region")))
        entities = read_yaml(out / "entities.yaml")["entities"]
        tables = [(e["schema_name"], e["table"]) for e in entities.values()]
        assert len(tables) == len(set(tables)) == 4
        assert sorted(entities) == ["ActionType", "Date", "Region", "Trade"]

    def test_a_name_that_is_now_shared_still_qualifies_both_and_leaves_no_bare_entry(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Trade", "ID", "Amount", kind="fact")))
        assert entity_keys(out) == ["Trade"]
        run_wizard(out, snapshot_of(table("ref", "Trade", "ID", "Amount", kind="fact")))
        entities = read_yaml(out / "entities.yaml")["entities"]
        assert sorted(entities) == ["ref.Trade", "sales.Trade"]
        assert entities["sales.Trade"]["schema_name"] == "sales"

    def test_a_name_several_schemas_share_is_not_guessed_for_a_bare_entry(self, tmp_path):
        out = tmp_path / "draft"
        _write(out, "entities.yaml", "entities:\n  Trade:\n    aliases: [deal]\n    table: Trade\n")
        run_wizard(out, snapshot_of(table("sales", "Trade"), table("ref", "Trade")))
        # The earlier entry cannot be matched to one schema: it stays, and the two real ones are added.
        assert sorted(entity_keys(out)) == ["Trade", "ref.Trade", "sales.Trade"]


class TestResolvingEarlierEntities:
    SNAP = snapshot_of(table("sales", "Trade"), table("ref", "Item"), table("sales", "Item"))

    def test_the_schema_comes_from_the_key_or_from_the_only_table_with_that_name(self):
        old = {
            "Trade": {"table": "Trade", "schema": None},
            "ref.Item": {"table": "Item", "schema": None},
            "Item": {"table": "Item", "schema": None},
            "Kept": {"table": "Trade", "schema": "other"},
        }
        resolved = sp._resolve_entity_schemas(self.SNAP, old)
        assert [resolved[k]["schema"] for k in old] == ["sales", "ref", None, "other"]
        assert old["Trade"]["schema"] is None            # the input is not changed

    def test_an_entry_without_a_schema_does_not_make_a_name_look_shared(self):
        old = {"Trade": {"table": "Trade", "schema": None}}
        namer = sp._table_namer(snapshot_of(table("sales", "Trade")), old)
        assert namer.key(table("sales", "Trade")) == "Trade"
        assert sp._rekey_map(namer, sp._resolve_entity_schemas(snapshot_of(table("sales", "Trade")), old)) == {}


# ---------------------------------------------------------------------------
# 2. The keys are the keys of schema.yaml
# ---------------------------------------------------------------------------

class TestKeysAreTheSyncKeys:
    @pytest.mark.parametrize(("schema", "name", "qualified", "key"), [
        ("sales", "Trade", False, "Trade"),
        ("sales", "Trade", True, "sales.Trade"),
        ("sales", "Price List", False, "[Price List]"),
        ("ref", "Run.Log", True, "ref.[Run.Log]"),
        ("Sales_Dim", "1st Table", True, "Sales_Dim.[1st Table]"),
    ])
    def test_the_inspector_namer_spells_a_key_the_way_the_sync_engine_does(self, schema, name, qualified, key):
        from database.schema_inspector import SchemaSnapshot, _TableNamer
        from schema_data.sync import _table_key

        tables = [TableInfo(name=name, schema=schema)]
        if qualified:
            tables.append(TableInfo(name=name, schema="other"))
        assert _TableNamer(SchemaSnapshot(tables=tables)).key(tables[0]) == key == _table_key(schema, name, qualified)

    def test_the_wizard_keys_a_name_with_a_space_or_a_dot_as_schema_yaml_does(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(
            table("ref", "Price List"), table("Metadata", "Run.Log"), table("ref", "Status"), table("sales", "Status"),
        ))
        assert sorted(entity_keys(out)) == ["[Price List]", "[Run.Log]", "ref.Status", "sales.Status"]


# ---------------------------------------------------------------------------
# 3. Two data sources, with values in the rows
# ---------------------------------------------------------------------------

SALES_LAYOUT = {
    "sales": {
        "Customer": "ID INTEGER PRIMARY KEY, Name VARCHAR(40), Phone VARCHAR(20)",
        "Trade": "ID INTEGER PRIMARY KEY, Customer_ID INTEGER, Amount INTEGER, Qty INTEGER",
    },
    "ref": {
        "Status": "ID INTEGER PRIMARY KEY, Name VARCHAR(40), Note VARCHAR(40)",
        "Bank": "ID INTEGER PRIMARY KEY, Name VARCHAR(40), FamilyName VARCHAR(40)",
    },
}
REPORTS_LAYOUT = {"rep": {"Summary": "ID INTEGER PRIMARY KEY, Region VARCHAR(20), Total INTEGER"}}
ROWS = {
    "sales.Customer": [(1, "Sample Person One", "09120000001")],
    "ref.Status": [
        (1, "Open", "n1"), (2, "Closed", "n2"), (3, "2024-05-01", "n3"), (4, "Login failed", "n4"),
        (5, "ab xxxxx cd", "n5"), (6, "10.0.0.7", "n6"), (7, "09120000009", "n7"), (8, "Pending review", "n8"),
    ],
    "ref.Bank": [(1, "Bank One", "Sample Family"), (2, "Bank Two", "Other Family")],
}


@pytest.fixture()
def lab(tmp_path, monkeypatch, capsys):
    """Two SQLite data sources with rows, as ``tests/test_setup_wizard_datasources.py`` has them without."""
    sales = build_database(tmp_path, "sales_db", SALES_LAYOUT, ROWS)
    reports = build_database(tmp_path, "reports_db", REPORTS_LAYOUT)
    project = tmp_path / "project_config"
    project.mkdir()
    (project / "datasources.yaml").write_text(DATASOURCES, encoding="utf-8")
    monkeypatch.setenv("DB_URL_SALES", sales.url)
    monkeypatch.setenv("DB_URL_REPORTS", reports.url)
    for variable in ("EXCLUDE_SCHEMAS", "DB_CONNECTION_URL", "DATABASE_URL"):
        monkeypatch.delenv(variable, raising=False)
    reset_datasources_cache()
    with cfg.override_settings(project_config_dir=str(project), exclude_schemas=""):
        yield World(tmp_path, project, sales, reports, capsys)
    reset_datasources_cache()


def _synonyms(run) -> dict:
    return read_yaml(run.directory / "aliases.yaml")["synonyms"]


class TestNoValueSynonymsByDefault:
    def test_a_run_writes_no_synonym_from_a_row(self, lab):
        run = lab.run()

        assert run.code == 0, run.out
        assert _synonyms(run) == {}
        assert "none built from row values" in run.out
        assert "value_synonyms" not in run.log()

    def test_the_fake_database_run_writes_none_either(self, tmp_path):
        col = ColumnInfo("Name", "varchar", sample_values=["Open", "Closed", "2024-05-01", "Sample Person"])
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(TableInfo(name="Status", schema="ref", columns=[col], classification="dim")))
        assert read_yaml(out / "aliases.yaml")["synonyms"] == {}


class TestValueSynonymsOptIn:
    def test_only_label_values_that_look_like_words_are_taken(self, lab):
        run = lab.run("--value-synonyms")

        assert run.code == 0, run.out
        assert _synonyms(run) == {
            "open": ["status"], "closed": ["status"], "pending review": ["status"],
            "bank one": ["bank"], "bank two": ["bank"],
        }
        assert "--value-synonyms: 5 value(s) from 2 table(s)" in run.out
        assert run.log()["value_synonyms"] == {"enabled": True, "synonyms": 5, "tables": 2}

    def test_no_value_reaches_the_log_or_the_prompts(self, lab):
        llm = RecordingLLM()
        run = lab.run("--value-synonyms", llm=llm)
        log = (run.directory / ".setup_log.json").read_text(encoding="utf-8").lower()
        for value in ("pending review", "bank one", "09120000009", "10.0.0.7"):
            assert value not in log

    def test_a_person_table_and_a_family_column_are_never_read(self, lab):
        run = lab.run("--value-synonyms")
        synonyms = _synonyms(run)
        assert not {"sample person one", "sample family", "other family"} & set(synonyms)

    def test_the_merge_keeps_what_an_earlier_run_wrote(self, lab):
        first = lab.run("--value-synonyms", name="out")
        second = lab.run(name="out")
        assert _synonyms(second) == _synonyms(first) != {}


def _fetch(values: dict[str, list[str]]):
    return lambda tbl, col: values.get(col.name, [])


def _column(name: str, kind: str = "varchar") -> ColumnInfo:
    return ColumnInfo(name, kind)


class TestTheOptInFilters:
    def _taken(self, values: list[str], *, column: str = "Name", table_name: str = "Bank") -> dict:
        snap = snapshot_of(TableInfo(name=table_name, schema="ref", columns=[_column(column)], classification="dim"))
        found, _ = sp._value_synonyms(snap, _fetch({column: values}))
        return found

    @pytest.mark.parametrize("value", [
        "2024-05-01", "1387/03/24", "01/01/2010", "20240131", "2024-05-01 10:30:00",   # dates
        "10:30", "10:30:05",                                                            # times
        "123456", "A1-77/9", "9988-1234-77",                                            # numbers and codes
        "5f4dcc3b5aa765d61d8327deb882cf99",                                             # a hash
        "09120000001", "+1 (555) 010-0100",                                             # phone numbers
        "user@example.test", "10.0.12.200", "fe80::1", "https://example.test/x",        # mail, IP, URL
        "12 Example Street, Building 4, Floor 2",                                       # an address
        "ab xxxxx cd", "****1234", "Sam ***",                                           # masked
        "Login failed", "Connection timeout", "Invalid object name",                    # error text
        "A name that goes on for far longer than forty characters",                     # long
        "x", "",                                                                        # too short
    ])
    def test_an_excluded_shape_is_not_taken(self, value):
        assert self._taken([value, "Open"]) == {"open": ["bank"]}

    @pytest.mark.parametrize("column", [
        "FirstName", "LastName", "FullName", "Family", "FamilyName", "Surname", "MobileName", "Phone",
        "Address", "Email", "NationalCode", "NationalName", "UserName", "BirthName", "PassportName",
    ])
    def test_a_person_or_contact_column_is_never_read(self, column):
        assert self._taken(["Open", "Closed"], column=column) == {}

    @pytest.mark.parametrize("table_name", ["Customer", "Person", "Employee", "UserAccount", "Client"])
    def test_a_name_column_of_a_table_of_people_is_not_read_but_its_status_is(self, table_name):
        assert self._taken(["Sam"], table_name=table_name) == {}
        assert self._taken(["Active"], column="Status", table_name=table_name) == {"active": [table_name.lower()]}

    def test_only_columns_named_like_a_label_are_read(self):
        assert self._taken(["Open"], column="Description") == {}
        assert self._taken(["Open"], column="StatusTitle") == {"open": ["bank"]}
        assert self._taken(["Open"], column="Kind") == {"open": ["bank"]}

    def test_a_column_with_more_than_fifty_distinct_values_is_not_categorical(self):
        assert self._taken([f"Kind {n}" for n in range(51)]) == {}
        assert len(self._taken([f"Kind {n}" for n in range(50)])) == 20           # the cap per table

    def test_the_cap_is_per_table_and_spans_its_columns(self):
        cols = [_column("Name"), _column("Title")]
        snap = snapshot_of(TableInfo(name="Bank", schema="ref", columns=cols, classification="dim"))
        found, tables = sp._value_synonyms(snap, _fetch({
            "Name": [f"Alpha {n}" for n in range(15)], "Title": [f"Beta {n}" for n in range(15)],
        }))
        assert len(found) == 20 and tables == 1
        assert not any(key.startswith("beta 5") for key in found)

    def test_a_non_text_column_is_not_read(self):
        snap = snapshot_of(TableInfo(name="Bank", schema="ref", columns=[_column("Status", "int")], classification="dim"))
        assert sp._value_synonyms(snap, _fetch({"Status": ["Open"]}))[0] == {}

    def test_a_value_is_lower_cased_and_the_first_table_keeps_it(self):
        a = TableInfo(name="Alpha", schema="s", columns=[_column("Name")], classification="dim")
        b = TableInfo(name="Beta", schema="s", columns=[_column("Name")], classification="dim")
        found, _ = sp._value_synonyms(snapshot_of(a, b), _fetch({"Name": ["Open"]}))
        assert found == {"open": ["alpha"]}


# ---------------------------------------------------------------------------
# 4. Examples
# ---------------------------------------------------------------------------

class PerSourceExamples(RecordingLLM):
    """Answers the examples prompt with one pair that names the data source the prompt is about."""

    def generate(self, prompt: str, expect_json: bool = True):
        if "NLQ-to-SQL example pairs" not in prompt:
            return super().generate(prompt, expect_json)
        self.prompts.append(prompt)
        found = re.search(r"data source (\w+);", prompt)
        name = found.group(1) if found else "all"
        return {"examples": [{"tags": ["count"], "question": f"How many rows in {name}?", "sql": f"SELECT COUNT(*) FROM {name}_t"}]}


class AnswersExamplesWith(RecordingLLM):
    """A model that answers the examples prompt with whatever it is given."""

    def __init__(self, answer) -> None:
        super().__init__()
        self.answer = answer

    def generate(self, prompt: str, expect_json: bool = True):
        if "NLQ-to-SQL example pairs" not in prompt:
            return super().generate(prompt, expect_json)
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _examples(run) -> list[dict]:
    return read_yaml(run.directory / "examples.yaml")["examples"]


class TestExamples:
    def test_two_sources_each_get_examples_about_their_own_tables(self, lab):
        llm = PerSourceExamples()

        run = lab.run(llm=llm)

        assert run.code == 0, run.out
        assert {e["question"] for e in _examples(run)} == {"How many rows in sales?", "How many rows in reports?"}
        sales, reports = [p for p in llm.prompts if "NLQ-to-SQL example pairs" in p]
        assert "sales.Customer" in sales and "rep.Summary" not in sales
        assert "rep.Summary" in reports and "sales.Customer" not in reports
        assert run.log()["step5_examples"]["per_source"] == {"sales": 1, "reports": 1}

    def test_one_database_is_one_call(self, tmp_path):
        llm = PerSourceExamples()
        run_wizard(tmp_path / "draft", snapshot_of(table("sales", "Customer")), llm)
        assert len([p for p in llm.prompts if "NLQ-to-SQL example pairs" in p]) == 1
        assert read_yaml(tmp_path / "draft" / "examples.yaml")["examples"][0]["question"] == "How many rows in all?"

    def test_fact_tables_come_first_and_the_list_is_capped(self):
        tables = [table("s", f"D{n}") for n in range(30)] + [table("s", "Orders", "ID", "Amount", kind="fact")]
        summary = sp._build_schema_summary(snapshot_of(*tables))
        lines = summary.splitlines()
        assert len(lines) == 20 and lines[0].startswith("s.Orders (fact)")

    def test_a_pair_is_found_however_the_model_nested_and_named_it(self):
        pair = {"question": "Q?", "sql": "SELECT 1"}
        assert sp._example_pairs([pair])[0][0]["question"] == "Q?"
        assert sp._example_pairs({"examples": [pair]})[1] == 1
        assert sp._example_pairs({"result": [pair]})[0][0]["sql"] == "SELECT 1"
        assert sp._example_pairs({"a": [pair], "b": [pair]}) == ([], 0)
        assert sp._example_pairs([{"nlq": "Q?", "sql_query": "SELECT 1"}])[0][0]["question"] == "Q?"
        assert sp._example_pairs([{"question": "Q?", "sql": "SELECT 1", "tags": "count"}])[0][0]["tags"] == ["count"]
        assert sp._example_pairs([{"question": "Q?", "sql": "DELETE FROM t"}]) == ([], 1)
        assert sp._example_pairs("no list") == ([], 0)

    def test_an_answer_with_nothing_usable_is_asked_again_with_a_smaller_request(self, tmp_path):
        llm = AnswersExamplesWith([{"description": "no question here"}])
        run_wizard(tmp_path / "draft", snapshot_of(table("sales", "Customer")), llm)
        asked = [p for p in llm.prompts if "NLQ-to-SQL example pairs" in p]
        assert len(asked) == 2 and "Generate 10 diverse" in asked[0] and "Generate 5 diverse" in asked[1]

    def test_nothing_usable_is_said_in_the_summary_and_the_file(self, lab):
        run = lab.run(llm=AnswersExamplesWith([{"description": "x"}, "text"]))

        assert run.code == 0
        assert "WARNING: the model gave no usable example" in run.out
        assert "WARNING: examples.yaml has no example from this run: the model gave nothing usable" in run.out
        assert _examples(run) == []
        assert "# NO EXAMPLES" in (run.directory / "examples.yaml").read_text(encoding="utf-8")
        assert run.log()["step5_examples"]["problems"]

    def test_a_failing_endpoint_is_reported_not_swallowed(self, lab):
        run = lab.run(llm=AnswersExamplesWith(RuntimeError("endpoint down")))
        assert "the model call failed (RuntimeError)" in run.out
        assert "WARNING: examples.yaml has no example from this run" in run.out

    def test_the_mock_provider_over_two_sources_says_it_generated_none(self, lab):
        run = lab.run(llm=WizardLLM(provider="mock", model="mock"))
        assert run.code == 0
        assert _examples(run) == []
        assert "WARNING: examples.yaml has no example from this run: the mock provider generates none." in run.out

    def test_items_the_model_got_wrong_are_counted(self, lab):
        good = {"question": "Q?", "sql": "SELECT 1"}
        run = lab.run(llm=AnswersExamplesWith({"examples": [good, {"question": "no sql"}, {"sql": "SELECT 2"}]}))
        assert "4 item(s) of the answers discarded" in run.out          # two per data source
        assert [e["question"] for e in _examples(run)] == ["Q?"]

    def test_resume_does_not_reuse_an_empty_result_and_rewrites_the_empty_file(self, lab):
        first = lab.run(name="out", llm=AnswersExamplesWith([]))
        assert _examples(first) == []

        again = PerSourceExamples()
        second = lab.run("--resume", name="out", llm=again)

        assert second.code == 0, second.out
        assert {e["question"] for e in _examples(second)} == {"How many rows in sales?", "How many rows in reports?"}
        assert len([p for p in again.prompts if "NLQ-to-SQL example pairs" in p]) == 2
        assert again.calls == 2                       # only step 5 asked the model again

    def test_a_non_empty_stored_result_is_still_reused(self, lab):
        lab.run(name="out", llm=PerSourceExamples())
        (lab.tmp_path / "out" / "entities.yaml").unlink()
        again = PerSourceExamples()
        lab.run("--resume", name="out", llm=again)
        assert not [p for p in again.prompts if "NLQ-to-SQL example pairs" in p]

    def test_an_examples_file_with_no_example_does_not_stand_but_another_empty_file_does(self, tmp_path):
        _write(tmp_path, "examples.yaml", "examples: []\n")
        _write(tmp_path, "entities.yaml", "entities: {}\n")
        _write(tmp_path, "business_rules.yaml", "rules:\n  T:\n    rule_text: x\n")
        assert not sp._file_stands(tmp_path / "examples.yaml")
        assert sp._file_stands(tmp_path / "entities.yaml") and sp._file_stands(tmp_path / "business_rules.yaml")
        assert not sp._file_stands(tmp_path / "missing.yaml")
        _write(tmp_path, "examples.yaml", "examples:\n  - {tags: [], question: Q, sql: SELECT 1}\n")
        assert sp._file_stands(tmp_path / "examples.yaml")


# ---------------------------------------------------------------------------
# 5. Business rules need a measure
# ---------------------------------------------------------------------------

class ModelNamesTheKey(RecordingLLM):
    """A model that answers every rule with ``Value column: ID. Volume column: ID.``"""

    def generate(self, prompt: str, expect_json: bool = True):
        if "business rules expert" not in prompt:
            return super().generate(prompt, expect_json)
        self.prompts.append(prompt)
        self._rules += 1
        return {"rules": {"value_col": "ID", "volume_col": "ID", "rule_text": f"RULE {self._rules}"}}


class TestRulesNeedAMeasure:
    def _rules(self, out: Path) -> dict:
        return read_yaml(out / "business_rules.yaml")["rules"]

    def test_a_fact_table_with_no_numeric_non_key_column_gets_no_rule(self, tmp_path, capsys):
        out, llm = tmp_path / "draft", RecordingLLM()
        run_wizard(out, snapshot_of(
            table("sales", "Lookup", "ID", "Customer_ID", "Code", kind="fact"),
            table("sales", "Orders", "ID", "Amount", "Qty", kind="fact"),
        ), llm)

        assert list(self._rules(out)) == ["Orders"]
        assert not llm.asked_about("Fact table: sales.Lookup")
        assert self._rules(out)["Orders"]["rule_text"].startswith("Value column: Amount. Volume column: Qty. ")
        summary = capsys.readouterr().out
        assert "1 fact table(s) got no rule because they have no numeric column that is not a key: sales.Lookup" in summary

    def test_a_key_the_model_names_as_value_or_volume_is_dropped(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Orders", "ID", "Amount", kind="fact")), ModelNamesTheKey())
        text = self._rules(out)["Orders"]["rule_text"]
        assert text == "RULE 1" and "ID" not in text

    def test_only_a_column_of_the_table_can_be_named(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Orders", "ID", "Amount", kind="fact")))
        # The fake names Amount and Qty; the table has no Qty, so only the value column is written.
        assert self._rules(out)["Orders"]["rule_text"] == "Value column: Amount. RULE 1"

    def test_the_prompt_lists_the_measure_columns(self, tmp_path):
        llm = RecordingLLM()
        run_wizard(tmp_path / "draft", snapshot_of(table("sales", "Orders", "ID", "Customer_ID", "Amount", kind="fact")), llm)
        (prompt,) = [p for p in llm.prompts if "business rules expert" in p]
        assert "Numeric measure columns (the only columns value_col and volume_col may name): Amount" in prompt

    def test_what_counts_as_a_measure(self):
        def col(name, kind="int", pk=False):
            return ColumnInfo(name, kind, is_pk=pk)

        columns = [
            col("ID", pk=True), col("TradeKey"), col("ProductCode"), col("Customer_ID"), col("SegmentID"),
            col("Amount", "decimal"), col("Qty"), col("Price", "float"), col("Note", "varchar"), col("Day", "date"),
            col("Flag", "boolean"), col("Region_FK"), col("Linked"),
        ]
        tbl = TableInfo(name="Trade", schema="s", columns=columns, classification="fact")
        assert sp._measure_columns(tbl, ["Linked"]) == ["Amount", "Qty", "Price"]
        assert sp._pick_measure("QTY", ["Amount", "Qty"]) == "Qty" and sp._pick_measure(3, ["Qty"]) == ""

    def test_a_table_that_declares_a_foreign_key_on_a_number_does_not_measure_it(self):
        from database.schema_inspector import ForeignKeyInfo

        tbl = TableInfo(
            name="Trade", schema="s", classification="fact",
            columns=[ColumnInfo("ID", "int", is_pk=True), ColumnInfo("Dept", "int"), ColumnInfo("Amount", "int")],
            foreign_keys=[ForeignKeyInfo("Dept", "s", "Dept", "ID")],
        )
        assert sp._measure_columns(tbl) == ["Amount"]


def _lookup(schema: str, name: str, *, extra: tuple[ColumnInfo, ...] = ()) -> TableInfo:
    """A key and a title, classed as fact by the name heuristic (as ``ContractType`` is)."""
    columns = [ColumnInfo("ID", "int", is_pk=True), ColumnInfo("Title", "varchar"), *extra]
    return TableInfo(name=name, schema=schema, columns=columns, classification="fact")


class TestDimensionsGetNoRule:
    """A table the name heuristic classed as fact but that is a dimension is neither asked about nor listed as a fact table.

    The summary used to say ``N fact table(s) got no rule because they have no
    numeric column that is not a key`` and list dimension and lookup tables:
    schemas named ``..._Dim`` and tables of a key and a title.
    """

    def _run(self, tmp_path, *tables: TableInfo):
        out, llm = tmp_path / "draft", RecordingLLM()
        run_wizard(out, snapshot_of(*tables), llm)
        log = json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))["step4_rules"]
        return out, llm, log

    def test_a_lookup_in_a_dimension_schema_is_left_out_and_not_called_a_fact_table(self, tmp_path, capsys):
        out, llm, log = self._run(
            tmp_path, _lookup("ref_Dim", "Kind"), table("sales", "Trade", "ID", "Amount", kind="fact"),
        )

        assert list(read_yaml(out / "business_rules.yaml")["rules"]) == ["Trade"]
        assert not llm.asked_about("Fact table: ref_Dim.Kind")
        text = capsys.readouterr().out
        assert "fact table(s) got no rule" not in text
        assert "Business rules: 1 table(s) classed as fact by name were left out because they look like dimensions: ref_Dim.Kind." in text
        assert log["skipped_dimension"] == ["ref_Dim.Kind"] and log["skipped_no_measure"] == []

    def test_a_dimension_schema_table_with_a_number_is_not_asked_for_a_value_rule_either(self, tmp_path):
        # Before: SortOrder was a numeric non-key column, so the model was asked for a value and a volume rule.
        out, llm, log = self._run(
            tmp_path, _lookup("ref_Dim", "Kind", extra=(ColumnInfo("SortOrder", "int"),)),
            table("sales", "Trade", "ID", "Amount", kind="fact"),
        )
        assert not llm.asked_about("Fact table: ref_Dim.Kind")
        assert list(read_yaml(out / "business_rules.yaml")["rules"]) == ["Trade"]
        assert log["skipped_dimension"] == ["ref_Dim.Kind"]

    def test_a_key_and_a_title_with_a_fact_like_name_is_a_lookup_in_any_schema(self, tmp_path, capsys):
        out, llm, log = self._run(
            tmp_path, _lookup("sales", "OrderStatus"), table("sales", "Trade", "ID", "Amount", kind="fact"),
        )
        assert not llm.asked_about("Fact table: sales.OrderStatus")
        assert log["skipped_dimension"] == ["sales.OrderStatus"]
        assert "OrderStatus" not in "".join(read_yaml(out / "business_rules.yaml")["rules"])
        assert "fact table(s) got no rule" not in capsys.readouterr().out

    def test_a_real_fact_table_without_a_measure_is_still_listed_and_the_counts_stay_truthful(self, tmp_path, capsys):
        out, llm, log = self._run(
            tmp_path,
            _lookup("ref_Dim", "Kind"),
            _lookup("sales", "OrderStatus"),
            table("sales", "Link", "ID", "Customer_ID", "Product_ID", kind="fact"),
            table("sales", "Trade", "ID", "Amount", kind="fact"),
        )

        assert list(read_yaml(out / "business_rules.yaml")["rules"]) == ["Trade"]
        text = capsys.readouterr().out
        assert "Business rules: 2 table(s) classed as fact by name were left out because they look like dimensions: ref_Dim.Kind, sales.OrderStatus." in text
        assert "Business rules: 1 fact table(s) got no rule because they have no numeric column that is not a key: sales.Link." in text
        assert (log["skipped_dimension"], log["skipped_no_measure"], log["rule_count"]) == (
            ["ref_Dim.Kind", "sales.OrderStatus"], ["sales.Link"], 1,
        )

    @pytest.mark.parametrize(("schema", "name", "columns", "links_out", "expected"), [
        ("ref_Dim", "Kind", [("ID", "int", True), ("Title", "varchar", False)], False, True),
        ("RefDim", "Kind", [("ID", "int", True), ("Title", "varchar", False)], False, True),      # CamelCase schema
        ("Sales_Dims", "Kind", [("ID", "int", True), ("Amount", "decimal", False)], False, True),   # the schema decides
        ("Lookups", "Kind", [("ID", "int", True), ("Amount", "decimal", False)], False, True),
        ("sales", "OrderStatus", [("ID", "int", True), ("Title", "varchar", False)], False, True),   # the shape decides
        ("sales", "OrderStatus", [("StatusKey", "int", False), ("Title", "nvarchar", False), ("Note", "text", False)], False, True),
        ("sales", "Trade", [("ID", "int", True), ("Amount", "decimal", False)], False, False),
        ("Dim_Fact", "Trade", [("ID", "int", True), ("Amount", "decimal", False)], False, False),   # a fact schema is not a dimension
        ("sales", "OrderStatus", [("ID", "int", True), ("Title", "varchar", False)], True, False),   # it points to something
        ("sales", "Link", [("ID", "int", True), ("Customer_ID", "int", False), ("Product_ID", "int", False)], False, False),
        ("sales", "Event", [("ID", "int", True), ("Title", "varchar", False), ("Note", "text", False),
                            ("Memo", "text", False)], False, False),                                   # too much text for a lookup
        ("sales", "Event", [("ID", "int", True), ("Rank", "int", False)], False, False),               # a number is not a title
    ])
    def test_what_reads_as_a_dimension(self, schema, name, columns, links_out, expected):
        tbl = TableInfo(
            name=name, schema=schema, classification="fact",
            columns=[ColumnInfo(n, t, is_pk=pk) for n, t, pk in columns],
        )
        assert sp._looks_like_dimension(tbl, links_out) is expected

    def test_a_table_with_a_declared_foreign_key_is_not_a_lookup(self):
        from database.schema_inspector import ForeignKeyInfo

        tbl = _lookup("sales", "OrderStatus")
        tbl.foreign_keys.append(ForeignKeyInfo("ID", "sales", "Parent", "ID"))
        assert sp._looks_like_dimension(tbl) is False

    def test_the_inspector_s_own_dim_classification_still_counts(self):
        tbl = TableInfo(name="Trade", schema="sales", columns=[ColumnInfo("ID", "int", is_pk=True)], classification="dim")
        assert sp._looks_like_dimension(tbl) is True


# ---------------------------------------------------------------------------
# 6. The alias list
# ---------------------------------------------------------------------------

class CombinatorialAliases(RecordingLLM):
    """A model that answers with every ordering of the table's name, 'name' and 'list'."""

    def generate(self, prompt: str, expect_json: bool = True):
        if "Table name:" not in prompt:
            return super().generate(prompt, expect_json)
        self.prompts.append(prompt)
        base = re.search(r"Table name: (?:\w+\.)?(\w+)", prompt).group(1).lower()
        words = [base, "name", "list"]
        import itertools

        combos = [" ".join(p) for n in (1, 2, 3) for p in itertools.permutations(words, n)]
        combos += [c.upper() for c in combos[:4]] + [f"{c}." for c in combos[:3]]
        return {"aliases": combos, "description": "d"}


class TestAliasList:
    def test_a_degenerate_list_is_cut_to_the_distinct_short_phrases(self, tmp_path, capsys):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Bank"), table("sales", "Broker")), CombinatorialAliases())

        aliases = read_yaml(out / "entities.yaml")["entities"]["Bank"]["aliases"]
        assert aliases == ["bank", "name", "list"]
        assert len(aliases) <= 8
        assert read_yaml(out / "aliases.yaml")["ring_aliases"]["Bank"] == aliases
        assert "2 alias list(s) had repeats or more than 8 entries and were cleaned." in capsys.readouterr().out

    def test_a_normal_list_is_left_alone(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Bank")))
        assert read_yaml(out / "entities.yaml")["entities"]["Bank"]["aliases"] == ["alias of sales.Bank"]

    def test_the_operator_s_own_edit_is_not_cut(self, tmp_path):
        out = tmp_path / "draft"
        edited = ", ".join(f"phrase {n}" for n in range(12))
        from unittest.mock import patch

        args = ["--db-url", "sqlite:///x.db", "--language", "en", "--output", str(out)]
        from tests.test_setup_wizard_drafts import database_of

        with database_of(snapshot_of(table("sales", "Bank"))), patch.object(sp, "setup_wizard_llm", return_value=RecordingLLM()), \
                patch.object(sp, "_ask", side_effect=lambda q, default="", non_interactive=False: edited if "Enter aliases" in q else ""), \
                patch.object(sp, "_choose", side_effect=lambda q, choices, default, non_interactive=False: "Edit" if "Accept aliases" in q else "Accept"), \
                patch.object(sp, "_yn", return_value=False):
            assert sp.main(args) == 0
        assert len(read_yaml(out / "entities.yaml")["entities"]["Bank"]["aliases"]) == 12

    @pytest.mark.parametrize(("aliases", "limit", "expected"), [
        (["Bank", "bank", " BANK ", "Bank."], 8, ["Bank"]),
        (["ي", "ی", "ك", "ک"], 8, ["ي", "ك"]),                      # Arabic and Persian letters are one
        (["Ring ۱", "Ring 1"], 8, ["Ring ۱"]),
        (["a", None, 3, True, 2.5, ["x"], {"y": 1}, ""], 8, ["a", "3", "2.5"]),
        (["bank name", "bank", "name bank", "bank name list"], 3, ["bank"]),
        (["alpha beta", "gamma", "delta epsilon zeta", "eta theta"], 2, ["alpha beta", "gamma"]),
    ])
    def test_tidy_aliases(self, aliases, limit, expected):
        assert tidy_aliases(aliases, limit) == expected

    def test_a_list_within_the_limit_is_only_deduplicated(self):
        assert tidy_aliases(["bank name", "bank name list", "Bank Name"]) == ["bank name", "bank name list"]

    def test_the_limit_is_eight(self):
        from core.wizard_drafts import ALIAS_LIMIT

        assert ALIAS_LIMIT == 8
        assert len(tidy_aliases([f"phrase{chr(97 + n)} x{n}" for n in range(20)])) == 8

    def test_something_other_than_a_list_gives_nothing(self):
        assert tidy_aliases(None) == [] and tidy_aliases([]) == []


# ---------------------------------------------------------------------------
# 7. The header the preflight looks for
# ---------------------------------------------------------------------------

class TestFileHeader:
    def test_every_file_starts_with_the_line_the_preflight_treats_as_a_draft(self, tmp_path):
        from core.config_doctor import WIZARD_DRAFT_HEADER, find_draft_header_kinds

        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Orders", "ID", "Amount", kind="fact"), table("sales", "Bank")))

        for name in sp._WIZARD_OUTPUT_FILES:
            assert (out / name).read_text(encoding="utf-8").startswith(f"# {WIZARD_DRAFT_HEADER} ")
        assert set(find_draft_header_kinds(out)) == set(sp._WIZARD_OUTPUT_FILES)


# ---------------------------------------------------------------------------
# 8. Synonyms an earlier run copied from rows
# ---------------------------------------------------------------------------

#: Invented keys of the shapes 6.13 and 6.14 wrote.
EARLIER_KEYS = {
    "1387/03/24": "date", "2024-05-01 10:00:00": "date", "09120000001": "phone", "10.0.12.200": "ip",
    "ab xxxxx cd": "masked", "1234567890123": "digits", "12 Example Street, Building 4, Floor 2": "address",
    "5f4dcc3b5aa765d61d8327deb882cf99": "hash", "Login failed for user": "error",
}


def _seed_synonyms(out: Path) -> None:
    """An ``aliases.yaml`` of the earlier run: the keys above and two real words."""
    lines = "".join(f"  {key!r}: [bank]\n" for key in EARLIER_KEYS) + "  annual: [year]\n  failed: [status]\n"
    _write(out, "aliases.yaml", "ring_aliases: {}\nsynonyms:\n" + lines)


class TestEarlierValueSynonymsAreDropped:
    def test_a_rerun_drops_the_keys_that_are_row_values_and_keeps_the_words(self, tmp_path, capsys):
        out = tmp_path / "draft"
        _seed_synonyms(out)

        run_wizard(out, snapshot_of(table("sales", "Bank")))

        assert read_yaml(out / "aliases.yaml")["synonyms"] == {"annual": ["year"], "failed": ["status"]}
        summary = capsys.readouterr().out
        assert "9 synonym key(s) from an earlier run were values read from rows" in summary
        assert "<date>: 2" in summary and "<phone>: 1" in summary and "<error>: 1" in summary
        assert not [key for key in EARLIER_KEYS if key in summary]
        done = json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))["step6_write"]
        assert done["dropped_value_synonyms"] == {
            "date": 2, "phone": 1, "ip": 1, "masked": 1, "digits": 1, "address": 1, "hash": 1, "error": 1,
        }
        log_text = (out / ".setup_log.json").read_text(encoding="utf-8")
        assert not [key for key in EARLIER_KEYS if key in log_text]

    def test_keep_existing_synonyms_opts_out(self, tmp_path, capsys):
        out = tmp_path / "draft"
        _seed_synonyms(out)

        run_wizard(out, snapshot_of(table("sales", "Bank")), None, "--keep-existing-synonyms")

        assert set(EARLIER_KEYS) <= set(read_yaml(out / "aliases.yaml")["synonyms"])
        summary = capsys.readouterr().out
        assert "were dropped" not in summary and "and were kept (--keep-existing-synonyms)" in summary
        done = json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))["step6_write"]
        assert done["dropped_value_synonyms"] == {}

    def test_a_clean_run_reports_nothing_dropped(self, tmp_path, capsys):
        run_wizard(tmp_path / "draft", snapshot_of(table("sales", "Bank")))
        assert "were dropped" not in capsys.readouterr().out

    def test_the_helper_counts_shapes_only(self):
        kept, dropped = sp._without_value_synonyms({"09120000001": ["t"], "annual": ["y"]})
        assert kept == {"annual": ["y"]} and dropped == {"phone": 1}


# ---------------------------------------------------------------------------
# 9. What the alias prompt shows the model
# ---------------------------------------------------------------------------

class TestAliasPromptSamples:
    def _prompt_for(self, tmp_path, name: str, columns: list[ColumnInfo]) -> str:
        llm = RecordingLLM()
        snap = snapshot_of(TableInfo(name=name, schema="sales", columns=columns, classification="dim"))
        run_wizard(tmp_path / "draft", snap, llm)
        (prompt,) = [p for p in llm.prompts if "Table name:" in p]
        return prompt

    def test_contact_columns_and_value_shaped_samples_never_reach_the_model(self, tmp_path):
        columns = [
            ColumnInfo("Status", "varchar", sample_values=["Open", "Closed"]),
            ColumnInfo("Phone", "varchar", sample_values=["09120000001"]),
            ColumnInfo("NationalCode", "varchar", sample_values=["0012345001"]),
            ColumnInfo("Address", "varchar", sample_values=["12 Example Street, Building 4, Floor 2"]),
            ColumnInfo("Note", "varchar", sample_values=[
                "Pending review", "ab xxxxx cd", "10.0.12.200", "2024-05-01", "Login failed", "user@example.test",
            ]),
        ]

        prompt = self._prompt_for(tmp_path, "Bank", columns)

        for kept in ("Open", "Closed", "Pending review"):
            assert f'"{kept}"' in prompt
        for gone in (
            "09120000001", "0012345001", "Example Street", "ab xxxxx cd", "10.0.12.200", "2024-05-01",
            "Login failed", "user@example.test",
        ):
            assert gone not in prompt

    def test_the_name_of_a_table_of_people_is_left_out_but_its_status_stays(self, tmp_path):
        columns = [
            ColumnInfo("Name", "varchar", sample_values=["Sample Person One"]),
            ColumnInfo("Status", "varchar", sample_values=["Active"]),
        ]
        prompt = self._prompt_for(tmp_path, "Customer", columns)
        assert "Sample Person One" not in prompt and '"Active"' in prompt

    def test_a_table_with_no_safe_sample_says_none(self, tmp_path):
        prompt = self._prompt_for(tmp_path, "Bank", [ColumnInfo("Phone", "varchar", sample_values=["09120000001"])])
        assert "Sample values: (none)" in prompt
