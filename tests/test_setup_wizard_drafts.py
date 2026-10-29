# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``setup_project.py`` writes drafts, merges runs and keys tables like the inspector.

Five defects of the setup wizard seen on a real two-database deployment, each
pinned by a class below:

* it wrote straight into the live ``project_config/`` and overwrote curated
  files (``TestDraftDirectory``, ``TestOverwriteLive``);
* a second run, for the second database, replaced the first run's entries
  instead of merging into them (``TestMerge``, ``TestFresh``);
* it keyed entities, aliases and rules by bare table name, so a name shared by
  several schemas collapsed into one entry (``TestSchemaKeys``);
* it could not leave schemas out, and asked the model about every staging and
  change-capture table (``TestSkippedTables``);
* rules and relationships depended on declared foreign keys
  (``TestInferredRelationships``).

The database is a stand-in: the table listing and ``SchemaInspector`` answer from
a snapshot built here (and, like the real inspector, read only the schemas and
tables they are asked for), so the wizard's own filtering, keying and merging
run for real. The model is a
fake that records every prompt, so "never reaches the model" is checked on the
prompts themselves. The warehouse-shaped fixture runs the whole wizard against
real SQLite schemas (``TestOnTheWarehouseShape``).
"""

from __future__ import annotations

import copy
import json
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config as cfg
import database.schema_inspector as inspector_module
import setup_project as sp
from core.yaml_loading import safe_load_strict
from database.schema_inspector import (
    ColumnInfo,
    ForeignKeyInfo,
    RelationshipInfo,
    SchemaInspector,
    SchemaSnapshot,
    TableInfo,
    schema_is_excluded,
)
from tests._wizard_fakes import AliasesViaGenerate

_URL = "sqlite:///wizard-fixture.db"


@pytest.fixture(autouse=True)
def _unwrapped_console(monkeypatch):
    """Keep the wizard's console from wrapping its lines (see test_setup_wizard.py)."""
    if hasattr(sp.console, "_width"):
        monkeypatch.setattr(sp.console, "_width", 10_000)


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------

class RecordingLLM(AliasesViaGenerate):
    """A wizard model that answers per prompt and keeps every prompt it was sent.

    Aliases name the table they were asked about and rule texts are numbered,
    so no two answers are identical (the repeated-answer guard stays quiet).
    """

    provider = "fake"
    model = "fake"

    def __init__(self, example_question: str = "How many rows?") -> None:
        self.prompts: list[str] = []
        self.example_question = example_question
        self._rules = 0

    def test_connection(self) -> bool:
        return True

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def generate(self, prompt: str, expect_json: bool = True):
        self.prompts.append(prompt)
        if "business rules expert" in prompt:
            self._rules += 1
            return {"rules": {"value_col": "Amount", "volume_col": "Qty", "rule_text": f"RULE {self._rules}"}}
        if "NLQ-to-SQL example pairs" in prompt:
            return [{"tags": ["count"], "question": self.example_question, "sql": "SELECT 1"}]
        name = re.search(r"Table name: (\S+)", prompt).group(1)
        return {"aliases": [f"alias of {name}"], "description": "d"}

    def asked_about(self, text: str) -> bool:
        return any(text in prompt for prompt in self.prompts)


def table(schema: str, name: str, *columns: str, kind: str = "dim", pk: str | None = None) -> TableInfo:
    """A table with the given columns (``int`` typed); *pk* names the primary key column.

    A fact table with no columns given has an ``Amount`` beside its ``ID``: a rule needs
    a numeric column that is not a key to name as the value.
    """
    cols = [ColumnInfo(c, "int", is_pk=(c == pk)) for c in (columns or (("ID", "Amount") if kind == "fact" else ("ID",)))]
    return TableInfo(name=name, schema=schema, columns=cols, classification=kind)


def snapshot_of(*tables: TableInfo, foreign_keys: tuple[tuple[str, ...], ...] = ()) -> SchemaSnapshot:
    """A snapshot; each foreign key is ``(schema, table, column, referred table, referred column)``."""
    snap = SchemaSnapshot(tables=list(tables), source_url="sqlite://")
    by_ref = {(t.schema, t.name): t for t in tables}
    for schema, name, column, referred, referred_column, *rest in foreign_keys:
        source = by_ref[(schema, name)]
        target_schema = rest[0] if rest else schema
        source.foreign_keys.append(ForeignKeyInfo(column, target_schema, referred, referred_column))
        snap.relationships.append(RelationshipInfo(name, schema, column, referred, target_schema, referred_column))
    return snap


class FakeDatabase:
    """The database behind a snapshot: a names-only listing and an inspector that reads what it is told to.

    ``inspections`` holds the keyword arguments of every ``inspect()`` call, so
    a test can see which schemas and tables the wizard asked to read.
    """

    def __init__(self, snapshot: SchemaSnapshot) -> None:
        self.snapshot = snapshot
        self.inspections: list[dict] = []

    def listing(self, _db_url: str) -> dict[str, list[str]]:
        names: dict[str, list[str]] = {}
        for t in self.snapshot.tables:
            names.setdefault(t.schema, []).append(t.name)
        return names

    def inspect(self, include_schemas=None, exclude_schemas=None, exclude_tables=None, fetch_row_counts=True):
        self.inspections.append(
            {"include_schemas": include_schemas, "exclude_schemas": exclude_schemas, "exclude_tables": exclude_tables}
        )
        snap = copy.deepcopy(self.snapshot)
        drop = set()
        for t in snap.tables:
            if (
                (include_schemas and t.schema not in include_schemas)
                or (exclude_schemas and schema_is_excluded(t.schema, exclude_schemas))
                or (exclude_tables and t.name in exclude_tables)
            ):
                drop.add(sp._identity(t.schema, t.name))
        sp._drop_tables(snap, drop)
        return snap

    @property
    def schemas_read(self) -> set[str]:
        return {s for call in self.inspections for s in (call["include_schemas"] or [])}


@contextmanager
def database_of(snapshot: SchemaSnapshot):
    """Patch the wizard's database access so it sees *snapshot*; yields the :class:`FakeDatabase`."""
    fake = FakeDatabase(snapshot)
    inspector = MagicMock()
    inspector.inspect.side_effect = fake.inspect
    with patch("sqlalchemy.create_engine", return_value=MagicMock()), \
         patch("database.schema_inspector.SchemaInspector", return_value=inspector), \
         patch.object(sp, "_list_schema_tables", side_effect=fake.listing):
        yield fake


def run_wizard(out: Path, snapshot: SchemaSnapshot, llm: RecordingLLM | None = None, *extra: str) -> int:
    """Run ``setup_project.main`` non-interactively on *snapshot* and return its status."""
    llm = llm or RecordingLLM()
    with database_of(snapshot), patch.object(sp, "setup_wizard_llm", return_value=llm):
        return sp.main([
            "--db-url", _URL, "--language", "en", "--non-interactive", "--output", str(out), *extra,
        ])


def read_yaml(path: Path):
    return safe_load_strict(path.read_text(encoding="utf-8"))


def entity_keys(out: Path) -> list[str]:
    return list(read_yaml(out / "entities.yaml")["entities"])


@pytest.fixture()
def live(tmp_path):
    """A directory that is the live configuration for the duration of a test."""
    directory = tmp_path / "live_config"
    with cfg.override_settings(project_config_dir=str(directory)):
        yield directory


# ---------------------------------------------------------------------------
# 1. Drafts go to project_config_draft/, not into the live directory
# ---------------------------------------------------------------------------

class TestDraftDirectory:
    def test_the_default_output_is_the_draft_directory(self):
        assert sp._build_parser().parse_args([]).output == "project_config_draft"

    def test_a_run_without_output_writes_there(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with database_of(snapshot_of(table("sales", "Customer"))), \
             patch.object(sp, "setup_wizard_llm", return_value=RecordingLLM()):
            assert sp.main(["--db-url", _URL, "--language", "en", "--non-interactive"]) == 0
        assert (tmp_path / "project_config_draft" / "entities.yaml").is_file()
        assert not (tmp_path / "project_config").exists()

    def test_the_live_directory_is_refused_before_anything_happens(self, live, capsys):
        llm = RecordingLLM()
        code = run_wizard(live, snapshot_of(table("sales", "Customer")), llm)
        out = capsys.readouterr().out
        assert code == 2
        assert not live.exists()
        assert llm.calls == 0
        assert "--overwrite-live" in out and "project_config_draft" in out

    def test_a_different_spelling_of_the_live_directory_is_refused_too(self, live, tmp_path):
        spelled = tmp_path / "elsewhere" / ".." / "live_config" / "."
        assert run_wizard(spelled, snapshot_of(table("sales", "Customer"))) == 2
        assert not live.exists()

    def test_the_example_directory_is_refused(self, live, capsys):
        example = sp._REPO_ROOT / "project_config.example"
        before = {p.name: p.read_bytes() for p in example.iterdir() if p.is_file()}
        assert run_wizard(example, snapshot_of(table("sales", "Customer"))) == 2
        assert {p.name: p.read_bytes() for p in example.iterdir() if p.is_file()} == before
        assert "project_config.example" in capsys.readouterr().out

    def test_a_dry_run_in_the_live_directory_writes_nothing_and_is_not_refused(self, live):
        assert run_wizard(live, snapshot_of(table("sales", "Customer")), None, "--dry-run") == 0
        assert not live.exists()


# ---------------------------------------------------------------------------
# --overwrite-live: backups of exactly the replaced files
# ---------------------------------------------------------------------------

class TestOverwriteLive:
    def _curated(self, live: Path) -> dict[str, str]:
        live.mkdir()
        files = {
            "entities.yaml": "entities: {Curated: {table: Curated, aliases: [mine]}}\n",
            "business_rules.yaml": "rules: {Curated: {rule_text: mine}}\n",
            "metrics.yaml": "metrics: {}\n",  # the wizard never produces this one
            "schema.yaml": "tables: {}\n",
            ".setup_log.json": "{}",
        }
        for name, text in files.items():
            (live / name).write_text(text, encoding="utf-8")
        return files

    def test_each_replaced_file_is_copied_aside_first(self, live, capsys):
        files = self._curated(live)
        code = run_wizard(live, snapshot_of(table("sales", "Customer")), None, "--overwrite-live", "--fresh")
        assert code == 0
        backups = {p.name: p for p in live.iterdir() if ".bak-" in p.name}
        # Replaced: the two the wizard wrote over, and its log. Not produced
        # before (aliases, examples, relationships): nothing to copy.
        assert sorted(re.sub(r"\.bak-.*", "", n) for n in backups) == [
            ".setup_log.json", "business_rules.yaml", "entities.yaml",
        ]
        for name, backup in backups.items():
            assert re.fullmatch(r".+\.bak-\d{8}-\d{6}", name)
            assert backup.read_text(encoding="utf-8") == files[re.sub(r"\.bak-.*", "", name)]
        assert "Curated" not in (live / "entities.yaml").read_text(encoding="utf-8")
        out = capsys.readouterr().out
        assert "Backups made:" in out and "entities.yaml.bak-" in out

    def test_files_the_wizard_does_not_produce_are_never_touched(self, live):
        files = self._curated(live)
        run_wizard(live, snapshot_of(table("sales", "Customer")), None, "--overwrite-live", "--fresh")
        for name in ("metrics.yaml", "schema.yaml"):
            assert (live / name).read_text(encoding="utf-8") == files[name]
            assert not list(live.glob(f"{name}.bak-*"))

    def test_without_the_flag_nothing_is_replaced(self, live):
        files = self._curated(live)
        assert run_wizard(live, snapshot_of(table("sales", "Customer"))) == 2
        assert (live / "entities.yaml").read_text(encoding="utf-8") == files["entities.yaml"]
        assert not list(live.glob("*.bak-*"))

    def test_a_draft_directory_gets_no_backups(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Customer")))
        run_wizard(out, snapshot_of(table("sales", "Customer")))
        assert not list(out.glob("*.bak-*"))


# ---------------------------------------------------------------------------
# 2. Merge, never shrink
# ---------------------------------------------------------------------------

class TestMerge:
    def _first(self, out: Path) -> None:
        run_wizard(out, snapshot_of(
            table("sales", "Customer"),
            table("sales", "Orders", "ID", "Amount", kind="fact"),
        ), RecordingLLM("Orders per customer?"))

    def test_a_second_run_over_other_schemas_keeps_the_first_runs_entries(self, tmp_path, capsys):
        out = tmp_path / "draft"
        self._first(out)
        capsys.readouterr()
        run_wizard(out, snapshot_of(
            table("ref", "Region"),
            table("ref", "Payments", "ID", "Amount", kind="fact"),
        ), RecordingLLM("Payments per region?"))
        assert sorted(entity_keys(out)) == ["Customer", "Orders", "Payments", "Region"]
        assert read_yaml(out / "aliases.yaml")["ring_aliases"] == {}
        assert sorted(read_yaml(out / "business_rules.yaml")["rules"]) == ["Orders", "Payments"]
        questions = [e["question"] for e in read_yaml(out / "examples.yaml")["examples"]]
        assert questions == ["Orders per customer?", "Payments per region?"]
        summary = capsys.readouterr().out
        assert "entities.yaml: kept 2, added 2, replaced 0" in summary

    def test_a_table_of_the_new_run_replaces_its_old_entry(self, tmp_path, capsys):
        out = tmp_path / "draft"
        self._first(out)
        (out / "entities.yaml").write_text(
            (out / "entities.yaml").read_text(encoding="utf-8").replace("alias of sales.Customer", "stale"),
            encoding="utf-8",
        )
        capsys.readouterr()
        run_wizard(out, snapshot_of(table("sales", "Customer"), table("sales", "Region")))
        entities = read_yaml(out / "entities.yaml")["entities"]
        assert entities["Customer"]["aliases"] == ["alias of sales.Customer"]
        assert sorted(entities) == ["Customer", "Orders", "Region"]
        assert "entities.yaml: kept 1, added 1, replaced 1" in capsys.readouterr().out

    def test_examples_are_deduplicated_by_question_and_relationships_by_endpoints(self, tmp_path):
        out = tmp_path / "draft"
        snap = snapshot_of(
            table("sales", "Customer", "ID", pk="ID"),
            table("sales", "Orders", "ID", "CustomerID", kind="fact"),
            foreign_keys=(("sales", "Orders", "CustomerID", "Customer", "ID"),),
        )
        run_wizard(out, snap, RecordingLLM("same question"))
        run_wizard(out, snap, RecordingLLM("Same   question"))
        assert len(read_yaml(out / "examples.yaml")["examples"]) == 1
        assert len(read_yaml(out / "relationships.yaml")["relationships"]) == 1

    def test_curated_entity_labels_survive_a_replacement(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Customer")))
        path = out / "entities.yaml"
        path.write_text(path.read_text(encoding="utf-8") + "    label: Client\n", encoding="utf-8")
        run_wizard(out, snapshot_of(table("sales", "Customer")))
        assert read_yaml(path)["entities"]["Customer"]["label"] == "Client"

    def test_an_unreadable_file_stops_the_run_before_any_model_call(self, tmp_path, capsys):
        out = tmp_path / "draft"
        out.mkdir()
        (out / "examples.yaml").write_text("examples: [1, 2]\n", encoding="utf-8")
        llm = RecordingLLM()
        assert run_wizard(out, snapshot_of(table("sales", "Customer")), llm) == 2
        assert llm.calls == 0
        message = capsys.readouterr().out
        assert "examples.yaml" in message and "--fresh" in message
        assert run_wizard(out, snapshot_of(table("sales", "Customer")), llm, "--fresh") == 0


class TestFresh:
    def test_fresh_starts_from_scratch(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Customer")))
        run_wizard(out, snapshot_of(table("ref", "Region")), None, "--fresh")
        assert entity_keys(out) == ["Region"]

    def test_fresh_is_still_subject_to_the_protected_directory_rule(self, live):
        assert run_wizard(live, snapshot_of(table("sales", "Customer")), None, "--fresh") == 2

    def test_without_fresh_the_same_run_merges(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Customer")))
        run_wizard(out, snapshot_of(table("ref", "Region")))
        assert sorted(entity_keys(out)) == ["Customer", "Region"]


# ---------------------------------------------------------------------------
# 3. Keys: bare name when unique, Schema.Table when shared
# ---------------------------------------------------------------------------

class TestSchemaKeys:
    def test_a_name_shared_by_two_schemas_gives_two_entities(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(
            table("sales", "Trade", kind="fact"), table("ref", "Trade", kind="fact"), table("sales", "Customer"),
        ))
        assert entity_keys(out) == ["sales.Trade", "ref.Trade", "Customer"]
        entities = read_yaml(out / "entities.yaml")["entities"]
        assert entities["ref.Trade"]["table"] == "Trade" and entities["ref.Trade"]["schema_name"] == "ref"
        assert read_yaml(out / "aliases.yaml")["ring_aliases"] == {}
        assert sorted(read_yaml(out / "business_rules.yaml")["rules"]) == ["ref.Trade", "sales.Trade"]

    def test_the_model_is_told_the_schema_of_each_table(self, tmp_path):
        llm = RecordingLLM()
        run_wizard(tmp_path / "draft", snapshot_of(table("sales", "Trade"), table("ref", "Trade")), llm)
        assert llm.asked_about("Table name: sales.Trade") and llm.asked_about("Table name: ref.Trade")

    def test_278_tables_give_278_entities(self, tmp_path):
        # Names repeat across schemas the way a warehouse's do.
        tables = [table(f"s{schema}", f"T{number}") for schema in range(4) for number in range(69)]
        tables += [table("extra", "Only0"), table("extra", "Only1")]
        assert len(tables) == 278
        out = tmp_path / "draft"
        assert run_wizard(out, snapshot_of(*tables), None, "--allow-repeated-answers") == 0
        keys = entity_keys(out)
        assert len(keys) == 278 and len(set(keys)) == 278

    def test_a_bare_entry_that_must_now_be_qualified_is_rekeyed_and_reported(self, tmp_path, capsys):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Trade", kind="fact")))
        assert entity_keys(out) == ["Trade"]
        capsys.readouterr()
        run_wizard(out, snapshot_of(table("ref", "Trade", kind="fact")))
        assert sorted(entity_keys(out)) == ["ref.Trade", "sales.Trade"]
        entities = read_yaml(out / "entities.yaml")["entities"]
        assert entities["sales.Trade"]["schema_name"] == "sales"
        assert sorted(read_yaml(out / "business_rules.yaml")["rules"]) == ["ref.Trade", "sales.Trade"]
        assert read_yaml(out / "aliases.yaml")["ring_aliases"] == {}
        summary = capsys.readouterr().out
        assert "Re-keyed in entities.yaml (1)" in summary and "Trade -> sales.Trade" in summary

    def test_relationship_ends_use_the_same_keys(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(
            table("sales", "Trade", "ID", "PartyID", kind="fact"),
            table("sales", "Party", "ID", pk="ID"),
            table("ref", "Party", "ID", pk="ID"),
            foreign_keys=(("sales", "Trade", "PartyID", "Party", "ID"),),
        ))
        (entry,) = read_yaml(out / "relationships.yaml")["relationships"]
        assert (entry["from_table"], entry["to_table"]) == ("Trade", "sales.Party")


# ---------------------------------------------------------------------------
# 4. Leaving schemas and staging tables out
# ---------------------------------------------------------------------------

def _staged() -> SchemaSnapshot:
    return snapshot_of(
        table("sales", "Customer"),
        table("sales", "Orders", "ID", "Amount", kind="fact"),
        table("Sales_Stg", "OrdersLoad", "ID", "Amount", kind="fact"),
        table("sales", "Orders_Inserts", "ID"),
        table("Replay", "ReplayedEvents", "ID"),
        table("ref", "Region"),
    )


class TestSkippedTables:
    def test_staging_and_change_capture_tables_are_skipped_by_default(self, tmp_path, capsys):
        out, llm = tmp_path / "draft", RecordingLLM()
        run_wizard(out, _staged(), llm)
        assert sorted(entity_keys(out)) == ["Customer", "Orders", "Region", "ReplayedEvents"]
        assert not llm.asked_about("OrdersLoad") and not llm.asked_about("Orders_Inserts")
        assert "Skipped 2 of 6 tables: 2 staging or change-capture" in capsys.readouterr().out

    def test_include_staging_keeps_them(self, tmp_path):
        out, llm = tmp_path / "draft", RecordingLLM()
        run_wizard(out, _staged(), llm, "--include-staging")
        assert len(entity_keys(out)) == 6
        assert llm.asked_about("OrdersLoad") and llm.asked_about("Orders_Inserts")

    def test_excluded_schemas_never_reach_the_model(self, tmp_path, capsys):
        out, llm = tmp_path / "draft", RecordingLLM()
        run_wizard(out, _staged(), llm, "--exclude-schemas", "replay, REF")
        assert sorted(entity_keys(out)) == ["Customer", "Orders"]
        assert not llm.asked_about("ReplayedEvents") and not llm.asked_about("Region")
        # 3 aliases + 1 rule + 1 examples request: nothing for the 4 skipped tables.
        assert llm.calls == 2 + 1 + 1
        assert "Skipped 4 of 6 tables: 2 in excluded schemas, 2 staging" in capsys.readouterr().out

    def test_exclusion_is_an_fnmatch_pattern_and_case_insensitive(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, _staged(), None, "--exclude-schemas", "*_STG,r*", "--include-staging")
        assert sorted(entity_keys(out)) == ["Customer", "Orders", "Orders_Inserts"]

    def test_a_relationship_to_a_skipped_table_is_dropped(self, tmp_path):
        out = tmp_path / "draft"
        snap = snapshot_of(
            table("sales", "Customer", pk="ID"), table("ref", "Region", pk="ID"),
            table("sales", "Orders", "ID", "CustomerID", "RegionID", kind="fact"),
            foreign_keys=(("sales", "Orders", "CustomerID", "Customer", "ID"),
                          ("sales", "Orders", "RegionID", "Region", "ID", "ref")),
        )
        run_wizard(out, snap, None, "--exclude-schemas", "ref")
        (entry,) = read_yaml(out / "relationships.yaml")["relationships"]
        assert entry["to_table"] == "Customer"

    def test_the_resume_fingerprint_reflects_the_exclusions(self, tmp_path):
        out = tmp_path / "draft"
        snap = _staged()
        run_wizard(out, snap, None, "--exclude-schemas", "ref")
        first = json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))
        run_wizard(out, snap, None, "--exclude-schemas", "replay")
        second = json.loads((out / ".setup_log.json").read_text(encoding="utf-8"))
        assert first["step2_schema"]["schema_fingerprint"] != second["step2_schema"]["schema_fingerprint"]

    def test_resume_generates_again_when_the_exclusions_changed(self, tmp_path):
        out, snap = tmp_path / "draft", _staged()
        interrupted = RecordingLLM()
        with patch.object(sp, "step6_review_and_write", side_effect=RuntimeError("interrupted")):
            with pytest.raises(RuntimeError):
                run_wizard(out, snap, interrupted, "--exclude-schemas", "ref")
        same = RecordingLLM()
        run_wizard(out, snap, same, "--exclude-schemas", "ref", "--resume")
        assert same.calls == 0
        other = RecordingLLM()
        (out / "entities.yaml").unlink()
        run_wizard(out, snap, other, "--exclude-schemas", "replay", "--resume")
        assert other.calls > 0

    def test_interactive_mode_shows_the_schemas_and_asks_before_any_model_call(self, tmp_path, capsys):
        out, llm = tmp_path / "draft", RecordingLLM()
        asked: list[str] = []

        with database_of(_staged()) as db:
            def ask(question, default="", non_interactive=False):
                asked.append(question)
                if question.startswith("Schemas to leave out"):
                    # Asked before anything is inspected or sent to the model.
                    assert db.inspections == [] and llm.calls == 0
                    return "ref"
                return ""

            with patch.object(sp, "setup_wizard_llm", return_value=llm), \
                 patch.object(sp, "_ask", side_effect=ask), \
                 patch.object(sp, "_choose", side_effect=lambda q, choices, default, non_interactive=False: "Accept"):
                code = sp.main(["--db-url", _URL, "--language", "en", "--output", str(out)])
        assert code == 0
        shown = capsys.readouterr().out
        assert "Schemas and the tables that would be read:" in shown and "sales: 2" in shown and "ref: 1" in shown
        assert any(q.startswith("Schemas to leave out") for q in asked)
        # The answer narrowed the inspection itself, not only its result.
        assert "ref" not in db.schemas_read and "Replay" in db.schemas_read
        assert "Region" not in " ".join(llm.prompts)
        assert "Region" not in entity_keys(out)

    def test_non_interactive_mode_never_prompts(self, tmp_path):
        def refuse(*_a, **_k):
            raise AssertionError("prompted in non-interactive mode")

        with patch.object(sp, "_ask", side_effect=refuse), patch.object(sp, "_choose", side_effect=refuse), \
             patch.object(sp, "_yn", side_effect=refuse), patch("builtins.input", side_effect=refuse):
            assert run_wizard(tmp_path / "draft", _staged()) == 0

    def test_the_staging_setting_can_be_changed(self, tmp_path):
        out = tmp_path / "draft"
        with cfg.override_settings(staging_patterns="replay"):
            run_wizard(out, _staged())
        assert "ReplayedEvents" not in entity_keys(out) and "OrdersLoad" in entity_keys(out)


# ---------------------------------------------------------------------------
# 5. Relationships inferred from names when keys are not declared
# ---------------------------------------------------------------------------

class TestInferredRelationships:
    def _no_keys(self) -> SchemaSnapshot:
        return snapshot_of(
            table("sales", "Customer", "ID", "Name", pk="ID"),
            table("sales", "Orders", "ID", "Customer_ID", "Amount", kind="fact", pk="ID"),
        )

    def test_a_warehouse_without_foreign_keys_still_gets_relationships(self, tmp_path, capsys):
        out = tmp_path / "draft"
        run_wizard(out, self._no_keys())
        text = (out / "relationships.yaml").read_text(encoding="utf-8")
        (entry,) = read_yaml(out / "relationships.yaml")["relationships"]
        assert (entry["from_table"], entry["from_column"], entry["to_table"], entry["to_column"]) == (
            "Orders", "Customer_ID", "Customer", "ID",
        )
        assert entry["basis"].startswith("naming convention") and entry["confidence"] == "high"
        assert "# REVIEW: inferred from column names" in text
        assert "1 relationship(s) in relationships.yaml are inferred" in capsys.readouterr().out

    def test_the_business_rules_prompt_sees_the_inferred_join(self, tmp_path):
        llm = RecordingLLM()
        run_wizard(tmp_path / "draft", self._no_keys(), llm)
        (prompt,) = [p for p in llm.prompts if "business rules expert" in p]
        assert "JOIN [sales].[Customer] ON [sales].[Orders].[Customer_ID] = [sales].[Customer].[ID]" in prompt
        assert "Related dimension tables: Customer" in prompt

    def test_declared_keys_are_not_marked_and_enough_of_them_suppress_the_guessing(self, tmp_path):
        out = tmp_path / "draft"
        snap = snapshot_of(
            table("sales", "Customer", "ID", pk="ID"),
            table("sales", "Orders", "ID", "Customer_ID", "Region_ID", kind="fact"),
            table("sales", "Region", "ID", pk="ID"),
            foreign_keys=(("sales", "Orders", "Customer_ID", "Customer", "ID"),),
        )
        run_wizard(out, snap)
        text = (out / "relationships.yaml").read_text(encoding="utf-8")
        assert "REVIEW" not in text
        assert len(read_yaml(out / "relationships.yaml")["relationships"]) == 1

    def test_a_reviewed_entry_is_not_replaced_by_a_guess(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, self._no_keys())
        path = out / "relationships.yaml"
        reviewed = re.sub(r"\n\s+basis: .*|\n\s+confidence: .*|\n\s+# REVIEW.*", "", path.read_text(encoding="utf-8"))
        path.write_text(reviewed.replace("JOIN", "JOIN /* checked */"), encoding="utf-8")
        run_wizard(out, self._no_keys())
        (entry,) = read_yaml(path)["relationships"]
        assert "checked" in entry["join_hint"] and "basis" not in entry

    def test_the_inferred_entries_load_through_the_application_loader(self, tmp_path):
        from database import relationship_map

        out = tmp_path / "draft"
        run_wizard(out, self._no_keys())
        relationship_map.reset()
        try:
            with cfg.override_settings(project_config_dir=str(out)):
                assert relationship_map.get_join_path("Orders", "Customer")
        finally:
            relationship_map.reset()


# ---------------------------------------------------------------------------
# The whole wizard on the warehouse-shaped fixture
# ---------------------------------------------------------------------------

class TestOnTheWarehouseShape:
    def _run(self, warehouse, out: Path, *extra: str) -> tuple[int, RecordingLLM]:
        from tests._warehouse_shape import LARGE, attached

        llm = RecordingLLM()
        with attached(warehouse), patch.object(sp, "setup_wizard_llm", return_value=llm):
            code = sp.main([
                "--db-url", warehouse.sources[LARGE].url, "--language", "en", "--non-interactive",
                "--output", str(out), "--allow-repeated-answers", *extra,
            ])
        return code, llm

    def test_every_table_is_an_entity_when_nothing_is_skipped(self, warehouse_shape, tmp_path):
        from tests._warehouse_shape import LARGE

        out = tmp_path / "draft"
        code, _ = self._run(warehouse_shape, out, "--include-staging")
        tables = warehouse_shape.sources[LARGE].tables
        keys = entity_keys(out)
        assert code == 0
        assert len(keys) == len(set(keys)) == len(tables)
        shared = {t.name.lower() for t in tables if sum(u.name.lower() == t.name.lower() for u in tables) > 1}
        assert any(k.lower().endswith(".invoice") for k in keys)
        assert not [k for k in keys if k.lower() in shared]

    def test_staging_and_change_capture_never_reach_the_model_and_exclusion_wins(self, warehouse_shape, tmp_path):
        from tests._warehouse_shape import LARGE

        out = tmp_path / "draft"
        code, llm = self._run(
            warehouse_shape, out,
            "--include-schemas", "sales_Fact,sales_Stg,common", "--exclude-schemas", "common",
        )
        assert code == 0
        wanted = [t for t in warehouse_shape.sources[LARGE].tables
                  if t.schema == "sales_Fact" and not t.name.endswith(("_Inserts", "_Updates", "_Deletes"))]
        assert len(entity_keys(out)) == len(wanted)
        assert not llm.asked_about("sales_Stg.") and not llm.asked_about("common.")
        assert not [p for p in llm.prompts if re.search(r"_(Inserts|Updates|Deletes)\b", p)]

    def test_a_second_run_over_other_schemas_does_not_shrink_the_files(self, warehouse_shape, tmp_path):
        out = tmp_path / "draft"

        def tables() -> set[tuple[str, str]]:
            entities = read_yaml(out / "entities.yaml")["entities"].values()
            return {(e["schema_name"], e["table"]) for e in entities}

        self._run(warehouse_shape, out, "--include-schemas", "sales_Fact")
        first_keys, first = set(entity_keys(out)), tables()
        self._run(warehouse_shape, out, "--include-schemas", "orders_Fact")
        assert first < tables()
        # `Invoice` was unique in the first run and is shared now: re-keyed, not duplicated.
        assert "Invoice" in first_keys and "Invoice" not in entity_keys(out)
        assert {"sales_Fact.Invoice", "orders_Fact.Invoice"} <= set(entity_keys(out))


# ---------------------------------------------------------------------------
# Schemas that are left out are never read
# ---------------------------------------------------------------------------

class TestExcludedSchemasAreNeverRead:
    def test_the_inspection_is_asked_for_the_remaining_schemas_only(self, tmp_path):
        with database_of(_staged()) as db:
            run_wizard_on(db, tmp_path / "draft", "--exclude-schemas", "ref,replay")
        (call,) = db.inspections
        assert call["include_schemas"] == ["sales"]
        assert call["exclude_schemas"] == ["ref", "replay"]
        assert db.schemas_read == {"sales"}

    def test_a_staging_table_with_a_unique_name_is_not_read_and_a_shared_name_is_read_then_dropped(self, tmp_path):
        snap = snapshot_of(
            table("sales", "Orders", kind="fact"), table("sales", "Orders_Inserts"),
            table("ref", "Region"), table("ref", "Region_Inserts"), table("sales", "Region_Inserts"),
        )
        with database_of(snap) as db:
            run_wizard_on(db, tmp_path / "draft")
        (call,) = db.inspections
        # `Orders_Inserts` is unique in the listing: never read. `Region_Inserts` is
        # in two schemas, so the bare name cannot be passed; both are dropped after the read.
        assert call["exclude_tables"] == ["Orders_Inserts"]
        assert sorted(entity_keys(tmp_path / "draft")) == ["Orders", "Region"]

    def test_a_schema_that_is_all_staging_is_not_read(self, tmp_path):
        with database_of(_staged()) as db:
            run_wizard_on(db, tmp_path / "draft")
        assert "Sales_Stg" not in db.schemas_read

    def test_a_same_named_table_in_another_schema_is_not_lost_to_the_staging_rule(self, tmp_path):
        snap = snapshot_of(table("sales", "Orders_Inserts"), table("ref", "Orders_Inserts"), table("ref", "Keep"))
        out = tmp_path / "draft"
        with cfg.override_settings(staging_patterns="table:*_Inserts"), database_of(snap) as db:
            run_wizard_on(db, out)
        assert db.inspections[0]["exclude_tables"] is None
        assert entity_keys(out) == ["Keep"]

    def test_nothing_left_to_read_means_no_inspection_at_all(self, tmp_path):
        # An empty include list means "every schema" to the inspector: it must not run.
        with database_of(_staged()) as db:
            code = run_wizard_on(db, tmp_path / "draft", "--exclude-schemas", "*")
        assert code == 0 and db.inspections == []
        assert entity_keys(tmp_path / "draft") == []

    def test_include_and_exclude_together(self, tmp_path):
        with database_of(_staged()) as db:
            run_wizard_on(db, tmp_path / "draft", "--include-schemas", "sales,ref", "--exclude-schemas", "sales")
        assert db.schemas_read == {"ref"}

    def test_a_listing_failure_stops_the_run_without_the_password(self, tmp_path, capsys):
        secret = "hunter2-distinctive"
        with patch.object(sp, "_list_schema_tables", side_effect=RuntimeError(f"cannot open {_URL} {secret}")), \
             patch.object(sp, "step1_connection", return_value=f"mssql+pyodbc://u:{secret}@h/d"), \
             patch.object(sp, "setup_wizard_llm", return_value=RecordingLLM()):
            with pytest.raises(SystemExit):
                sp.main(["--db-url", _URL, "--language", "en", "--non-interactive", "--output", str(tmp_path / "d")])
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err


def run_wizard_on(db: FakeDatabase, out: Path, *extra: str) -> int:
    """Run the wizard against a database already patched in by :func:`database_of`."""
    with patch.object(sp, "setup_wizard_llm", return_value=RecordingLLM()):
        return sp.main([
            "--db-url", _URL, "--language", "en", "--non-interactive", "--output", str(out), *extra,
        ])


class TestOnRealSchemasNothingExcludedIsRead:
    """The same, on the warehouse fixture's SQLite schemas, with a spy on the table reader."""

    def _run(self, warehouse, out: Path, *extra: str, interactive_answer: str | None = None):
        from tests._warehouse_shape import LARGE, attached

        read: list[tuple[str, str]] = []
        sampled: list[str] = []
        original_table = SchemaInspector._inspect_table
        original_samples = inspector_module._fetch_samples

        def spy_table(self, engine, inspector, schema, table_name, fetch_row_counts):
            read.append((schema, table_name))
            return original_table(self, engine, inspector, schema, table_name, fetch_row_counts)

        def spy_samples(engine, schema, table_name, column, rows):
            sampled.append(schema)
            return original_samples(engine, schema, table_name, column, rows)

        order: list[str] = []
        argv = ["--db-url", warehouse.sources[LARGE].url, "--language", "en", "--output", str(out),
                "--allow-repeated-answers", *extra]
        patches = [
            patch.object(SchemaInspector, "_inspect_table", spy_table),
            patch("database.schema_inspector._fetch_samples", spy_samples),
            patch.object(sp, "setup_wizard_llm", return_value=RecordingLLM()),
        ]
        if interactive_answer is None:
            argv.append("--non-interactive")
        else:
            def ask(question, default="", non_interactive=False):
                if question.startswith("Schemas to leave out"):
                    order.append(f"asked after {len(read)} tables read")
                    return interactive_answer
                return ""

            patches += [
                patch.object(sp, "_ask", side_effect=ask),
                patch.object(sp, "_choose", side_effect=lambda q, choices, default, non_interactive=False: "Accept"),
            ]
        with attached(warehouse), ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            code = sp.main(argv)
        return code, read, sampled, order

    def test_an_excluded_schema_is_never_inspected_or_sampled(self, warehouse_shape, tmp_path):
        code, read, sampled, _ = self._run(
            warehouse_shape, tmp_path / "draft", "--include-schemas", "sales_Fact,orders_Fact,common",
            "--exclude-schemas", "orders_*",
        )
        assert code == 0
        schemas = {schema for schema, _ in read}
        assert schemas == {"sales_Fact", "common"}
        assert "orders_Fact" not in set(sampled)

    def test_change_capture_tables_with_unique_names_are_never_read(self, warehouse_shape, tmp_path):
        code, read, _, _ = self._run(warehouse_shape, tmp_path / "draft", "--include-schemas", "sales_Fact,Replay")
        assert code == 0
        assert not [name for _, name in read if re.search(r"_(Inserts|Updates|Deletes)$", name)]
        assert "Replay" not in {schema for schema, _ in read}

    def test_the_interactive_answer_decides_what_is_read(self, warehouse_shape, tmp_path):
        code, read, sampled, order = self._run(
            warehouse_shape, tmp_path / "draft", "--include-schemas", "sales_Fact,orders_Fact",
            interactive_answer="orders_Fact",
        )
        assert code == 0
        assert order == ["asked after 0 tables read"]
        assert {schema for schema, _ in read} == {"sales_Fact"}
        assert "orders_Fact" not in set(sampled)


# ---------------------------------------------------------------------------
# An empty new answer never wipes an earlier one
# ---------------------------------------------------------------------------

class SilentLLM(RecordingLLM):
    """A model with nothing to say (or an endpoint that fails) for the named tables.

    *aliases* and *rules* hold schema-qualified table names (``sales.Customer``).
    """

    def __init__(self, aliases: tuple[str, ...] = (), rules: tuple[str, ...] = (), fail: bool = False) -> None:
        super().__init__()
        self.aliases, self.rules, self.fail = aliases, rules, fail

    def generate(self, prompt: str, expect_json: bool = True):
        silent = (
            any(f"Fact table: {n}\n" in prompt for n in self.rules) if "business rules expert" in prompt
            else any(f"Table name: {n}\n" in prompt for n in self.aliases) if "Table name:" in prompt
            else False
        )
        if not silent:
            return super().generate(prompt, expect_json)
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("endpoint down")
        return {"rules": {"rule_text": ""}} if "business rules expert" in prompt else {"aliases": []}


class TestEmptyAnswersDoNotWipeEarlierEntries:
    def _curated(self, out: Path) -> SchemaSnapshot:
        snap = snapshot_of(
            table("sales", "Customer"), table("sales", "Region"),
            table("sales", "Orders", "ID", "Amount", kind="fact"),
            table("sales", "Payments", "ID", "Amount", kind="fact"),
        )
        run_wizard(out, snap)
        return snap

    @pytest.mark.parametrize("fail", [False, True], ids=["empty answer", "failing endpoint"])
    def test_a_table_without_a_new_answer_keeps_its_aliases_and_rule(self, tmp_path, capsys, fail):
        out = tmp_path / "draft"
        snap = self._curated(out)
        rules_before = read_yaml(out / "business_rules.yaml")["rules"]
        capsys.readouterr()
        run_wizard(out, snap, SilentLLM(aliases=("sales.Customer",), rules=("sales.Orders",), fail=fail))
        entities = read_yaml(out / "entities.yaml")["entities"]
        assert entities["Customer"]["aliases"] == ["alias of sales.Customer"]
        assert entities["Region"]["aliases"] == ["alias of sales.Region"]
        rules = read_yaml(out / "business_rules.yaml")["rules"]
        assert rules["Orders"] == rules_before["Orders"]
        assert not rules["Orders"]["rule_text"].startswith("Primary fact table")
        assert rules["Payments"] != rules_before["Payments"]  # a real new answer still replaces
        summary = capsys.readouterr().out
        assert "entities.yaml: 1 earlier entry kept because this run gave no answer for it: Customer" in summary
        assert "business_rules.yaml: 1 earlier entry kept because this run gave no answer for it: Orders" in summary
        assert "(1 of the kept have no new answer from the model)" in summary

    def test_a_first_run_still_writes_the_placeholder_rule(self, tmp_path):
        out = tmp_path / "draft"
        run_wizard(out, snapshot_of(table("sales", "Orders", "ID", "Amount", kind="fact")),
                   SilentLLM(rules=("sales.Orders",)))
        assert read_yaml(out / "business_rules.yaml")["rules"]["Orders"]["rule_text"].startswith("Primary fact table")

    def test_a_real_answer_replaces_an_earlier_placeholder(self, tmp_path):
        out = tmp_path / "draft"
        snap = snapshot_of(table("sales", "Orders", "ID", "Amount", kind="fact"))
        run_wizard(out, snap, SilentLLM(rules=("sales.Orders",)))
        run_wizard(out, snap)
        assert read_yaml(out / "business_rules.yaml")["rules"]["Orders"]["rule_text"].startswith("Value column")
