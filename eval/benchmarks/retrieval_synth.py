# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Synthetic table-retrieval benchmark: a large schema, its config, and questions.

Table selection is hard to judge on a dozen-table example schema and
impossible to judge on a real one nobody may share. This module generates, from
a seed and nothing else, a warehouse-shaped benchmark that can be committed:

* a **schema** of a few hundred tables (default 400) over two or more data
  sources, built from star schemas -- fact tables with ``<Dim>_ID`` foreign
  keys -- plus snowflaked classifications (``Product`` -> ``ProductCategory``),
  many-to-many **bridge tables**, **shared dimensions** that exist in more than
  one source (a calendar, a currency list) and a few **same-name tables in two
  sources** (``sales.Region`` / ``inventory.Region``);
* a complete **project_config** directory for it (``schema.yaml`` with
  relationships, ``entities.yaml``, ``aliases.yaml``, ``retrieval_hints.yaml``,
  ``relationships.yaml``, ``datasources.yaml`` and the other required files);
* a **golden set** (``golden.jsonl``) of English and Persian questions, each
  with the reference SQL that answers it and so its gold tables, in nine
  kinds: a plain dimension list, a fact by one or two dimensions, a fact by
  period, a fact by a dimension *and* a period, a fact by the **parent** of a
  dimension it joins through (the dimension is never named), a fact by a
  dimension reached through a **bridge table** (the bridge is never named; half
  the bridges have an opaque name and a description that names nothing), the
  same through a bridge *and* a classification (three joins), and a question
  that names only a measure column (``total net weight by customer``) and not
  the fact table.

Everything is generic English words with Persian renderings
(:mod:`eval.benchmarks.vocabulary`); nothing is taken from a real deployment.
The same ``seed`` and options always produce byte-identical files.

How the configuration is made realistic rather than easy:

* only part of the tables (``alias_coverage``) have an entry in
  ``entities.yaml`` / ``fact_patterns``; the rest can only be found from
  ``schema.yaml``'s descriptions;
* a foreign key is declared in ``schema.yaml``, or in ``relationships.yaml``, or
  not at all (then its column follows the ``<Table>_ID`` convention); declared
  keys sometimes use a column name no convention explains (``Cust_Ref``);
* some foreign-key-looking columns point at nothing (``Batch_ID``);
* question words are inflected (English plurals, Persian ``-ها`` with and
  without ZWNJ, a ZWNJ dropped from a stored label).

Usage::

    python -m eval.benchmarks.retrieval_synth generate --out /tmp/bench
    python -m eval.benchmarks.retrieval_synth run --out /tmp/bench
    python -m eval.benchmarks.retrieval_synth run --out /tmp/bench --seed 11 --markdown

``run`` generates the benchmark (when ``--out`` is empty), then measures it with
``python -m eval.cli recall`` in a child process whose ``PROJECT_CONFIG_DIR`` is
the generated directory (the configuration is read once per process, so the
benchmark cannot be loaded into a process that already holds another one).
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.console import use_utf8_console
from eval.benchmarks import vocabulary as vocab

__all__ = [
    "Benchmark",
    "BenchmarkConfig",
    "EdgeSpec",
    "TableSpec",
    "generate",
    "main",
    "run_benchmark",
    "summarise",
    "write_benchmark",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkConfig:
    """Options of :func:`generate`.

    Attributes
    ----------
    seed:
        Seed of the one random generator everything is drawn from.
    tables:
        Approximate number of tables (the exact count differs by a few: pairs
        of classification/child tables and same-name pairs come in twos).
    sources:
        Data source names; the first is the default source. At least two.
    questions_per_language:
        Number of questions generated in each of English and Persian.
    alias_coverage:
        Share of dimension and fact tables that carry aliases in
        ``entities.yaml`` / ``fact_patterns``.
    schema_relationship_share, relationships_yaml_share:
        Share of foreign keys declared in ``schema.yaml``'s ``relationships``,
        and (of the remainder) in ``relationships.yaml``. The rest are
        declared nowhere and follow the ``<Table>_ID`` column convention.
    opaque_bridge_share:
        Share of bridge tables with an opaque name and a description that
        names neither side.
    collisions:
        Number of bare names that exist in two sources as two tables.
    """

    seed: int = 7
    tables: int = 400
    sources: tuple[str, ...] = ("sales", "inventory")
    questions_per_language: int = 240
    alias_coverage: float = 0.5
    schema_relationship_share: float = 0.45
    relationships_yaml_share: float = 0.3
    opaque_bridge_share: float = 0.5
    collisions: int = 4

    def __post_init__(self) -> None:
        if len(self.sources) < 2:
            raise ValueError("the benchmark needs at least two data sources")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("data source names must be distinct")
        if self.tables < 40:
            raise ValueError("tables must be at least 40")
        if self.questions_per_language < 1:
            raise ValueError("questions_per_language must be at least 1")
        for name in ("alias_coverage", "schema_relationship_share",
                     "relationships_yaml_share", "opaque_bridge_share"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass
class TableSpec:
    """One generated table."""

    key: str
    bare: str
    schema: str
    sources: tuple[str, ...]
    kind: str  # fact | dim | child | parent | shared | collision | bridge
    en: str
    fa: str
    description: str = ""
    columns: dict[str, str] = field(default_factory=dict)
    aliased: bool = False
    #: ``(column, english label, persian label)`` of the fact's measures; the
    #: first is shared vocabulary (``Amount``), the second is its signature.
    measures: tuple[tuple[str, str, str], ...] = ()
    opaque: bool = False
    #: For a parent table, the child it classifies.
    child: str | None = None

    @property
    def ref(self) -> str:
        """Bracketed ``[schema].[table]`` reference, as in the reference SQL."""
        return f"[{self.schema}].[{self.bare}]"


@dataclass(frozen=True)
class EdgeSpec:
    """A foreign key ``src.column -> dst.ID``.

    ``channel`` says where it is declared: ``"schema"`` (``schema.yaml``
    ``relationships``), ``"relationships"`` (``relationships.yaml``), or
    ``"inferred"`` (nowhere; the column follows the ``<Table>_ID`` convention).
    """

    src: str
    dst: str
    column: str
    channel: str


@dataclass(frozen=True)
class Benchmark:
    """A generated benchmark: tables, edges, config files and golden cases."""

    config: BenchmarkConfig
    tables: tuple[TableSpec, ...]
    edges: tuple[EdgeSpec, ...]
    files: dict[str, str]
    golden: tuple[dict[str, Any], ...]

    def golden_jsonl(self) -> str:
        """The golden set as ``golden.jsonl`` text."""
        return "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in self.golden)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _words(camel: str) -> str:
    """``ProductCategory`` -> ``product category``."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", camel).lower()


def _plural_en(label: str) -> str:
    head, _, last = label.rpartition(" ")
    if last.endswith("y") and last[-2:-1] not in "aeiou":
        last = last[:-1] + "ies"
    elif last.endswith(("s", "x", "ch", "sh")):
        last += "es"
    else:
        last += "s"
    return f"{head} {last}".strip()


def _drop_zwnj(text: str) -> str:
    return text.replace(vocab.ZWNJ, "")


# ---------------------------------------------------------------------------
# Schema construction
# ---------------------------------------------------------------------------


class _Builder:
    """Builds the tables and foreign keys; everything random goes through ``rng``."""

    def __init__(self, config: BenchmarkConfig) -> None:
        self.cfg = config
        self.rng = random.Random(config.seed)
        self.tables: dict[str, TableSpec] = {}
        self.edges: list[EdgeSpec] = []
        self.used_bare: set[str] = set()
        self._edge_pairs: set[tuple[str, str]] = set()
        self._source_cycle = itertools.cycle(config.sources)
        self.facts: list[str] = []
        self.direct: dict[str, list[str]] = {}
        self.bridged: dict[str, list[tuple[str, str]]] = {}
        self.parent_of: dict[str, str] = {}
        self.shared: list[str] = []
        self.base_dims: dict[str, list[str]] = {s: [] for s in config.sources}

    # -- table factories -----------------------------------------------------

    def _add(self, spec: TableSpec) -> TableSpec:
        if spec.key in self.tables:
            raise ValueError(f"duplicate table key {spec.key}")
        self.tables[spec.key] = spec
        self.used_bare.add(spec.bare.lower())
        spec.columns.setdefault("ID", "Primary key")
        return spec

    def _new_dim(
        self, bare: str, en: str, fa: str, source: str, kind: str, *, qualified: bool = False,
    ) -> TableSpec:
        key = f"{source}.{bare}" if qualified else bare
        spec = TableSpec(key=key, bare=bare, schema=source, sources=(source,), kind=kind, en=en, fa=fa)
        spec.columns["Name"] = f"{en.capitalize()} name / نام {fa}"
        spec.columns["Code"] = f"{en.capitalize()} code / کد {fa}"
        return self._add(spec)

    def plan(self) -> None:
        cfg, rng = self.cfg, self.rng
        n_facts = round(0.15 * cfg.tables)
        n_bridges = round(0.10 * cfg.tables)
        n_shared = len(vocab.SHARED)
        n_dims = cfg.tables - n_facts - n_bridges - n_shared - 2 * cfg.collisions
        n_pairs = round(0.22 * n_dims)
        n_plain = n_dims - 2 * n_pairs
        if n_plain < 0 or n_facts < 1:
            raise ValueError("tables is too small for this configuration")

        nouns = list(vocab.NOUNS)
        rng.shuffle(nouns)

        # Same-name tables: one per source, qualified keys.
        collision_nouns = [n for n in nouns if n[0] in ("Region", "City", "Warehouse", "Branch",
                                                         "Store", "Route")][: cfg.collisions]
        nouns = [n for n in nouns if n not in collision_nouns]
        for en_name, fa_name in collision_nouns:
            for source in cfg.sources[:2]:
                spec = self._new_dim(en_name, _words(en_name), fa_name, source, "collision", qualified=True)
                self.base_dims[source].append(spec.key)

        # Shared dimensions.
        for en_name, fa_name in vocab.SHARED:
            spec = TableSpec(
                key=en_name, bare=en_name, schema="ref", sources=tuple(cfg.sources),
                kind="shared", en=_words(en_name), fa=fa_name,
            )
            spec.columns["Name"] = f"{spec.en.capitalize()} name / نام {fa_name}"
            self._add(spec)
            self.shared.append(spec.key)

        # Classification pairs: Product (child) and ProductCategory (parent).
        pair_nouns = nouns[:n_pairs]
        rest_nouns = nouns[n_pairs:]
        for index, (en_name, fa_name) in enumerate(pair_nouns):
            source = cfg.sources[index % len(cfg.sources)]
            cls_en, cls_fa = vocab.CLASSIFIERS[rng.randrange(len(vocab.CLASSIFIERS))]
            child = self._new_dim(en_name, _words(en_name), fa_name, source, "child")
            parent_bare = en_name + cls_en
            parent = self._new_dim(parent_bare, _words(parent_bare), f"{cls_fa} {fa_name}", source, "parent")
            parent.child = child.key
            self.base_dims[source].append(child.key)
            self.parent_of[child.key] = parent.key
            self._add_edge(child.key, parent.key)

        # Plain dimensions: the unused nouns, then noun + aspect combinations.
        plain: list[tuple[str, str, str]] = [(n, _words(n), f) for n, f in rest_nouns]
        combos = [
            (n + a, _words(n + a), f"{af} {nf}")
            for (n, nf) in nouns for (a, af) in vocab.ASPECTS
            if (n + a).lower() not in self.used_bare
        ]
        rng.shuffle(combos)
        made = 0
        for bare, en_label, fa_label in (plain + combos):
            if made >= n_plain:
                break
            if bare.lower() in self.used_bare:
                continue
            source = next(self._source_cycle)
            spec = self._new_dim(bare, en_label, fa_label, source, "dim")
            self.base_dims[source].append(spec.key)
            made += 1

        self._plan_facts(n_facts)
        self._plan_bridges(n_bridges)

    def _plan_facts(self, n_facts: int) -> None:
        cfg, rng = self.cfg, self.rng
        events = list(vocab.EVENTS)
        names: list[tuple[str, str, str]] = []
        for (ev, ev_fa), (q, q_fa) in itertools.product(events, vocab.FACT_QUALIFIERS):
            bare = ev + q
            fa = f"{q_fa} {ev_fa}".strip()
            names.append((bare, _words(bare), fa))
        rng.shuffle(names)
        # Make sure plain events come first for a few facts, so "order" and
        # "order line" both exist and compete.
        chosen: list[tuple[str, str, str]] = []
        for bare, en_label, fa in names:
            if bare.lower() in self.used_bare:
                continue
            chosen.append((bare, en_label, fa))
            if len(chosen) == n_facts:
                break

        plain_measures = list(vocab.MEASURES)
        # A signature measure is "<Modifier><Measure>": column NetWeight, label
        # "net weight" / "وزن خالص"; each fact gets a different one.
        signatures = [
            (f"{m}{base}", f"{m.lower()} {base.lower()}", f"{base_fa} {m_fa}")
            for (m, m_fa) in vocab.MEASURE_MODIFIERS
            for (base, base_fa) in vocab.MEASURES
        ]
        rng.shuffle(signatures)

        for index, (bare, en_label, fa) in enumerate(chosen):
            source = cfg.sources[index % len(cfg.sources)]
            spec = TableSpec(key=bare, bare=bare, schema=source, sources=(source,),
                             kind="fact", en=en_label, fa=fa)
            plain = plain_measures[rng.randrange(len(plain_measures))]
            sig = signatures[index]
            spec.measures = (
                (plain[0], plain[0].lower(), plain[1]),
                sig,
            )
            for col, label_en, label_fa in spec.measures:
                spec.columns[col] = f"{label_en.capitalize()} / {label_fa}"
            self._add(spec)
            self.facts.append(spec.key)
            self.direct[spec.key] = []

        # Foreign keys: 3-5 dimensions of the fact's source, plus shared ones.
        for key in self.facts:
            fact = self.tables[key]
            source = fact.sources[0]
            pool = sorted(self.base_dims[source])
            count = min(len(pool), rng.randint(3, 5))
            for dim_key in rng.sample(pool, count):
                self._link_fact(key, dim_key)
            for shared_key, probability in zip(self.shared, (0.9, 0.35, 0.2)):
                if rng.random() < probability:
                    self._link_fact(key, shared_key)
            if rng.random() < 0.3:  # a column that looks like a key and points at nothing
                stem = vocab.DECOY_STEMS[rng.randrange(len(vocab.DECOY_STEMS))]
                fact.columns[f"{stem}_ID"] = f"{stem} identifier (no table)"

    def _plan_bridges(self, n_bridges: int) -> None:
        cfg, rng = self.cfg, self.rng
        made = 0
        order = list(self.facts)
        rng.shuffle(order)
        attempts = itertools.cycle(order)
        guard = 0
        while made < n_bridges and guard < 20 * n_bridges + 20:
            guard += 1
            fact_key = next(attempts)
            fact = self.tables[fact_key]
            source = fact.sources[0]
            candidates = [
                d for d in sorted(self.base_dims[source])
                if d not in self.direct[fact_key]
                and d not in [b[1] for b in self.bridged.get(fact_key, [])]
                and self.tables[d].kind != "collision"
            ]
            if not candidates:
                continue
            dim_key = candidates[rng.randrange(len(candidates))]
            dim = self.tables[dim_key]
            opaque = rng.random() < cfg.opaque_bridge_share
            bare = f"Lnk{made + 1:04d}" if opaque else fact.bare + dim.bare
            if bare.lower() in self.used_bare:
                continue
            spec = TableSpec(
                key=bare, bare=bare, schema=source, sources=(source,), kind="bridge",
                en=_words(bare), fa=bare, opaque=opaque,
            )
            self._add(spec)
            self._add_edge(bare, fact_key)
            self._add_edge(bare, dim_key)
            self.bridged.setdefault(fact_key, []).append((bare, dim_key))
            made += 1

    def _link_fact(self, fact_key: str, dim_key: str) -> None:
        self._add_edge(fact_key, dim_key)
        self.direct[fact_key].append(dim_key)

    # -- foreign keys --------------------------------------------------------

    def _add_edge(self, src_key: str, dst_key: str) -> None:
        cfg, rng = self.cfg, self.rng
        if (src_key, dst_key) in self._edge_pairs:
            return
        src, dst = self.tables[src_key], self.tables[dst_key]
        roll = rng.random()
        if roll < cfg.schema_relationship_share:
            channel = "schema"
        elif roll < cfg.schema_relationship_share + (1 - cfg.schema_relationship_share) * cfg.relationships_yaml_share:
            channel = "relationships"
        else:
            channel = "inferred"
        standard = channel == "inferred" or rng.random() < 0.65
        if standard:
            column = f"{dst.bare}_ID" if rng.random() < 0.6 else f"{dst.bare}ID"
        else:
            suffix = vocab.LEGACY_SUFFIXES[rng.randrange(len(vocab.LEGACY_SUFFIXES))]
            column = f"{dst.bare[:4]}_{suffix}"
        while column in src.columns:
            column += "x"
        src.columns[column] = f"FK -> {dst.key}"
        self._edge_pairs.add((src_key, dst_key))
        self.edges.append(EdgeSpec(src_key, dst_key, column, channel))

    # -- descriptions and aliases -------------------------------------------

    def finish(self) -> None:
        rng = self.rng
        phrases_dim = ("reference data", "master data", "lookup table", "catalogue")
        phrases_fact = ("transactions", "records", "event log", "facts")
        for key in sorted(self.tables):
            t = self.tables[key]
            where = f"{t.schema}.{t.bare}"
            if t.kind in ("dim", "child", "collision"):
                phrase = phrases_dim[rng.randrange(len(phrases_dim))]
                t.description = f"{where} — {t.en} {phrase} / اطلاعات پایه {t.fa}"
            elif t.kind == "parent":
                child = self.tables[t.child] if t.child else t
                t.description = f"{where} — classification of {child.en} / طبقه‌بندی {child.fa}"
            elif t.kind == "shared":
                t.description = f"{where} — shared {t.en} dimension in every data source / {t.fa}"
            elif t.kind == "fact":
                phrase = phrases_fact[rng.randrange(len(phrases_fact))]
                t.description = f"{where} — {t.en} {phrase} / {t.fa}"
            elif t.kind == "bridge":
                ends = [e.dst for e in self.edges if e.src == key]
                if t.opaque:
                    t.description = f"{where} — cross-reference table / جدول ارجاع"
                else:
                    a, b = (self.tables[k] for k in ends[:2])
                    t.description = f"{where} — links {a.en} and {b.en} / رابط {a.fa} و {b.fa}"
        # Which tables carry aliases (entities / fact_patterns).
        for key in sorted(self.tables):
            t = self.tables[key]
            if t.kind in ("fact", "dim", "child", "parent", "collision", "shared"):
                t.aliased = t.kind == "shared" or rng.random() < self.cfg.alias_coverage


# ---------------------------------------------------------------------------
# Config files
# ---------------------------------------------------------------------------


def _dump(data: Any, header: str = "") -> str:
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100000)
    return f"# {header}\n{body}" if header else body


def _join_sql(builder: _Builder, edge: EdgeSpec) -> str:
    src, dst = builder.tables[edge.src], builder.tables[edge.dst]
    return f"JOIN {dst.ref} ON {src.ref}.[{edge.column}] = {dst.ref}.[ID]"


def _config_files(builder: _Builder) -> dict[str, str]:
    tables = builder.tables
    schema_tables: dict[str, Any] = {}
    for key in sorted(tables, key=lambda k: (tables[k].kind != "fact", k)):
        t = tables[key]
        entry: dict[str, Any] = {"description": t.description, "db_schema": t.schema}
        if len(t.sources) == 1:
            entry["datasource"] = t.sources[0]
        else:
            entry["datasource"] = list(t.sources)
        entry["columns"] = dict(t.columns)
        schema_tables[key] = entry

    schema_rels = [
        {"from_table": e.src, "to_table": e.dst, "join_sql": _join_sql(builder, e)}
        for e in builder.edges if e.channel == "schema"
    ]
    schema_yaml = _dump(
        {"tables": schema_tables, "relationships": schema_rels},
        "synthetic retrieval benchmark -- generated by eval.benchmarks.retrieval_synth",
    )

    rel_rng = random.Random(builder.cfg.seed + 1)
    rel_entries = []
    for e in builder.edges:
        # Declared in relationships.yaml, or mirrored there from schema.yaml.
        if e.channel == "relationships" or (e.channel == "schema" and rel_rng.random() < 0.3):
            s, d = tables[e.src], tables[e.dst]
            rel_entries.append({
                "from_table": s.bare, "from_schema": s.schema, "from_column": e.column,
                "to_table": d.bare, "to_schema": d.schema, "to_column": "ID",
                "join_hint": _join_sql(builder, e),
            })
    relationships_yaml = _dump({"relationships": rel_entries})

    entities: dict[str, Any] = {}
    fact_patterns: dict[str, list[str]] = {}
    for key in sorted(tables):
        t = tables[key]
        if not t.aliased:
            continue
        aliases = [t.en, t.fa]
        if t.kind == "fact":
            fact_patterns[key] = aliases
        else:
            entities[key] = {"aliases": aliases, "table": t.bare}
    entities_yaml = _dump({"entities": entities})

    hints = {
        "fact_tables": sorted(builder.facts),
        "always_include": {
            "Calendar": list(vocab.TIME_WORDS_EN) + [fa for _en, fa in vocab.TIME_WORDS_FA],
        },
        "fact_patterns": fact_patterns,
    }
    hints_yaml = _dump(hints)

    aliases_yaml = _dump({
        "ring_aliases": {},
        "synonyms": {"annual": ["year"], "monthly": ["month"], "weekly": ["week"]},
    })

    sources = builder.cfg.sources
    datasources = {
        "default": sources[0],
        "datasources": {
            name: {
                "description": f"Synthetic {name} warehouse",
                "host": f"bench-{name}",
                "database": f"{name.capitalize()}DW",
                "trusted_connection": True,
            }
            for name in sources
        },
    }
    datasources_yaml = _dump(datasources)

    files = {
        "schema.yaml": schema_yaml,
        "relationships.yaml": relationships_yaml,
        "entities.yaml": entities_yaml,
        "retrieval_hints.yaml": hints_yaml,
        "aliases.yaml": aliases_yaml,
        "datasources.yaml": datasources_yaml,
        "business_rules.yaml": "rules: {}\n",
        "examples.yaml": "examples: []\n",
        "metrics.yaml": "metrics: {}\n",
    }
    for name in ("session_policy.yaml", "memory_policy.yaml", "system_prompt.md"):
        files[name] = (_REPO_ROOT / "project_config.example" / name).read_text(encoding="utf-8")
    return files


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

_EN_TEMPLATES: dict[str, tuple[str, ...]] = {
    "dim_list": ("List all {d}.", "Show me every {d}.", "How many {d} do we have?", "Give me the {d} list."),
    "fact_dim": (
        "Total {m} of {f} by {d}.", "Show {f} {m} per {d}.",
        "What is the {m} of {f} for each {d}?", "{f} {m} grouped by {d}",
    ),
    "fact_two": (
        "Compare {f} {m} across {d1} and {d2}.", "{f} {m} by {d1} and {d2}",
        "Break down the {m} of {f} by {d1} then {d2}.",
    ),
    "fact_time": ("Total {m} of {f} by {t}.", "How did {f} {m} change per {t}?", "{f} {m} for each {t}"),
    "fact_dim_time": (
        "Total {m} of {f} by {d} per {t}.", "Show {f} {m} for each {d} by {t}.",
        "{f} {m} grouped by {d} and {t}",
    ),
    "snowflake": (
        "Total {m} of {f} by {d}.", "Show {f} {m} per {d}.", "Which {d} has the highest {f} {m}?",
    ),
    "bridge": (
        "Total {m} of {f} by {d}.", "Show {f} {m} per {d}.", "Which {d} has the highest {f} {m}?",
    ),
    "bridge_parent": (
        "Total {m} of {f} by {d}.", "Show {f} {m} per {d}.", "Which {d} has the highest {f} {m}?",
    ),
    "measure": ("Total {mm} by {d}.", "Which {d} has the highest {mm}?", "Show {mm} per {d}."),
}

_FA_TEMPLATES: dict[str, tuple[str, ...]] = {
    "dim_list": ("فهرست همه {d} را نشان بده.", "تعداد {d} چقدر است؟", "همه {d} را لیست کن."),
    "fact_dim": (
        "مجموع {m} {f} به تفکیک {d} چقدر است؟", "{m} {f} را بر حسب {d} نشان بده.",
        "{m} هر {d} در {f} چقدر بوده؟",
    ),
    "fact_two": ("{m} {f} را بر اساس {d1} و {d2} مقایسه کن.", "{m} {f} به تفکیک {d1} و {d2}"),
    "fact_time": ("{m} {f} به تفکیک {t} چقدر است؟", "روند {m} {f} در هر {t} چطور بوده؟"),
    "fact_dim_time": (
        "{m} {f} به تفکیک {d} و {t} چقدر است؟", "{m} {f} را برای هر {d} در هر {t} نشان بده.",
    ),
    "snowflake": ("مجموع {m} {f} به تفکیک {d} چقدر است؟", "{m} {f} را بر حسب {d} نشان بده."),
    "bridge": ("مجموع {m} {f} به تفکیک {d} چقدر است؟", "{m} {f} را بر حسب {d} نشان بده."),
    "bridge_parent": ("مجموع {m} {f} به تفکیک {d} چقدر است؟", "{m} {f} را بر حسب {d} نشان بده."),
    "measure": ("مجموع {mm} به تفکیک {d} چقدر است؟", "کدام {d} بیشترین {mm} را دارد؟"),
}

#: Questions of each kind per language, as weights out of 48.
_KIND_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("dim_list", 4), ("fact_dim", 8), ("fact_two", 6), ("fact_time", 3), ("fact_dim_time", 4),
    ("snowflake", 8), ("bridge", 8), ("bridge_parent", 3), ("measure", 4),
)

#: Kinds whose gold tables include a table the question does not name.
_MULTIHOP_KINDS: tuple[str, ...] = ("snowflake", "bridge", "bridge_parent")


class _Asker:
    """Draws questions of every kind with their gold tables and reference SQL."""

    def __init__(self, builder: _Builder) -> None:
        self.b = builder
        self.rng = random.Random(builder.cfg.seed + 2)
        self.t = builder.tables
        self.edge_by_pair = {(e.src, e.dst): e for e in builder.edges}
        measure_uses: dict[str, int] = {}
        for key in builder.facts:
            for col, _en, _fa in self.t[key].measures[1:]:
                measure_uses[col] = measure_uses.get(col, 0) + 1
        self.unique_signature = {c for c, n in measure_uses.items() if n == 1}

    # -- surface forms -------------------------------------------------------

    def _surface(self, spec: TableSpec, lang: str) -> str:
        rng = self.rng
        if lang == "en":
            return _plural_en(spec.en) if rng.random() < 0.3 else spec.en
        fa = spec.fa
        if vocab.ZWNJ in fa and rng.random() < 0.3:
            fa = _drop_zwnj(fa)
        if " " not in fa and rng.random() < 0.3:
            fa = fa + (vocab.ZWNJ + "ها" if rng.random() < 0.6 else "ها")
        return fa

    def _measure_surface(self, label_en: str, label_fa: str, lang: str) -> str:
        label = label_en if lang == "en" else label_fa
        if lang == "fa" and vocab.ZWNJ in label and self.rng.random() < 0.3:
            return _drop_zwnj(label)
        return label

    # -- reference SQL -------------------------------------------------------

    def _sql(self, order: list[str], measure: str | None, group: str | None) -> tuple[str, list[str]]:
        aliases: dict[str, str] = {order[0]: "t0"}
        lines = [f"FROM {self.t[order[0]].ref} t0"]
        for index, key in enumerate(order[1:], start=1):
            alias = f"t{index}"
            joined = None
            for other, other_alias in aliases.items():
                edge = self.edge_by_pair.get((key, other)) or self.edge_by_pair.get((other, key))
                if edge is not None:
                    joined = (edge, other, other_alias)
                    break
            if joined is None:
                raise ValueError(f"no foreign key connects {key} to {sorted(aliases)}")
            edge, other, other_alias = joined
            if edge.src == key:
                condition = f"{alias}.[{edge.column}] = {other_alias}.[ID]"
            else:
                condition = f"{other_alias}.[{edge.column}] = {alias}.[ID]"
            lines.append(f"JOIN {self.t[key].ref} {alias} ON {condition}")
            aliases[key] = alias
        select = f"SUM(t0.[{measure}]) AS Total" if measure else "COUNT(*) AS N"
        group_alias = aliases[group] if group else None
        head = f"SELECT {group_alias}.[Name], {select}" if group_alias else f"SELECT {select}"
        tail = f" GROUP BY {group_alias}.[Name]" if group_alias else ""
        return " ".join([head] + lines) + tail, list(order)

    # -- the seven kinds -----------------------------------------------------

    def _pick_fact(self, predicate) -> str | None:
        candidates = [k for k in self.b.facts if predicate(k)]
        return candidates[self.rng.randrange(len(candidates))] if candidates else None

    def _ask(self, kind: str, lang: str) -> dict[str, Any] | None:
        rng, b, t = self.rng, self.b, self.t
        templates = (_EN_TEMPLATES if lang == "en" else _FA_TEMPLATES)[kind]
        template = templates[rng.randrange(len(templates))]
        calendar = "Calendar"
        values: dict[str, str] = {}
        datasource: str | None = None

        if kind == "dim_list":
            pool = sorted(k for k, v in t.items() if v.kind in ("dim", "child", "parent"))
            key = pool[rng.randrange(len(pool))]
            values["d"] = self._surface(t[key], lang)
            sql = f"SELECT t0.[Name] FROM {t[key].ref} t0"
            gold, datasource = [key], t[key].sources[0]
        else:
            fact_key = self._pick_fact(
                lambda k: bool([d for d in b.direct[k] if d != calendar])
                and (kind != "snowflake" or any(d in b.parent_of for d in b.direct[k]))
                and (kind != "bridge" or bool(b.bridged.get(k)))
                and (kind != "bridge_parent" or any(d in b.parent_of for _, d in b.bridged.get(k, [])))
                and (kind not in ("fact_time", "fact_dim_time") or calendar in b.direct[k])
                and (kind != "fact_two" or len([d for d in b.direct[k] if d != calendar]) >= 2)
            )
            if fact_key is None:
                return None
            fact = t[fact_key]
            datasource = fact.sources[0]
            plain_col, plain_en, plain_fa = fact.measures[0]
            values["f"] = self._surface(fact, lang) if kind != "measure" else ""
            values["m"] = self._measure_surface(plain_en, plain_fa, lang)
            dims = [d for d in b.direct[fact_key] if d != calendar]
            if kind == "fact_dim":
                d = dims[rng.randrange(len(dims))]
                values["d"] = self._surface(t[d], lang)
                sql, _ = self._sql([fact_key, d], plain_col, d)
                gold = [fact_key, d]
            elif kind == "fact_two":
                d1, d2 = rng.sample(dims, 2)
                values["d1"], values["d2"] = self._surface(t[d1], lang), self._surface(t[d2], lang)
                sql, _ = self._sql([fact_key, d1, d2], plain_col, d1)
                gold = [fact_key, d1, d2]
            elif kind == "fact_time":
                en_word, fa_word = vocab.TIME_WORDS_FA[rng.randrange(len(vocab.TIME_WORDS_FA))]
                values["t"] = en_word if lang == "en" else fa_word
                sql, _ = self._sql([fact_key, calendar], plain_col, calendar)
                gold = [fact_key, calendar]
            elif kind == "fact_dim_time":
                d = dims[rng.randrange(len(dims))]
                en_word, fa_word = vocab.TIME_WORDS_FA[rng.randrange(len(vocab.TIME_WORDS_FA))]
                values["d"] = self._surface(t[d], lang)
                values["t"] = en_word if lang == "en" else fa_word
                sql, _ = self._sql([fact_key, d, calendar], plain_col, d)
                gold = [fact_key, d, calendar]
            elif kind == "snowflake":
                children = [d for d in dims if d in b.parent_of]
                child = children[rng.randrange(len(children))]
                parent = b.parent_of[child]
                values["d"] = self._surface(t[parent], lang)
                sql, _ = self._sql([fact_key, child, parent], plain_col, parent)
                gold = [fact_key, child, parent]
            elif kind == "bridge":
                bridge, dim = b.bridged[fact_key][rng.randrange(len(b.bridged[fact_key]))]
                values["d"] = self._surface(t[dim], lang)
                sql, _ = self._sql([fact_key, bridge, dim], plain_col, dim)
                gold = [fact_key, bridge, dim]
            elif kind == "bridge_parent":
                options = [(br, d) for br, d in b.bridged[fact_key] if d in b.parent_of]
                bridge, child = options[rng.randrange(len(options))]
                parent = b.parent_of[child]
                values["d"] = self._surface(t[parent], lang)
                sql, _ = self._sql([fact_key, bridge, child, parent], plain_col, parent)
                gold = [fact_key, bridge, child, parent]
            else:  # measure: the question names a measure column, not the fact
                sig_col, sig_en, sig_fa = fact.measures[1]
                if sig_col not in self.unique_signature:
                    return None
                d = dims[rng.randrange(len(dims))]
                values["mm"] = self._measure_surface(sig_en, sig_fa, lang)
                values["d"] = self._surface(t[d], lang)
                sql, _ = self._sql([fact_key, d], sig_col, d)
                gold = [fact_key, d]
        question = " ".join(template.format(**values).split())
        return {
            "question": question,
            "expected_sql": sql,
            "gold": gold,
            "datasource": datasource,
        }

    def cases(self) -> list[dict[str, Any]]:
        total = self.b.cfg.questions_per_language
        counts = [(kind, max(1, round(total * weight / 48))) for kind, weight in _KIND_WEIGHTS]
        counts[1] = (counts[1][0], counts[1][1] + (total - sum(n for _, n in counts)))
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for lang in ("en", "fa"):
            serial = 0
            for kind, wanted in counts:
                made = attempts = 0
                while made < wanted and attempts < 50 * wanted + 50:
                    attempts += 1
                    asked = self._ask(kind, lang)
                    if asked is None or asked["question"] in seen:
                        continue
                    seen.add(asked["question"])
                    serial += 1
                    tags = [lang, kind] + (["multihop"] if kind in _MULTIHOP_KINDS else [])
                    out.append({
                        "id": f"{lang}-{serial:04d}-{kind}",
                        "question": asked["question"],
                        "tags": tags,
                        "expected_sql": asked["expected_sql"],
                        "expect": "success",
                        "datasource": asked["datasource"],
                        "notes": "gold tables: " + ", ".join(sorted(asked["gold"])),
                    })
                    made += 1
        return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate(config: BenchmarkConfig | None = None) -> Benchmark:
    """Generate the benchmark for *config* (all defaults when ``None``).

    Parameters
    ----------
    config:
        See :class:`BenchmarkConfig`.

    Returns
    -------
    Benchmark
        The same value for the same *config*, every time.

    Raises
    ------
    ValueError
        If *config* asks for something the generator cannot build (too few
        tables, a single data source).

    Examples
    --------
    >>> bench = generate(BenchmarkConfig(tables=60, questions_per_language=12))
    >>> 55 <= len(bench.tables) <= 65
    True
    >>> sorted(bench.files)[:3]
    ['aliases.yaml', 'business_rules.yaml', 'datasources.yaml']
    >>> generate(BenchmarkConfig(tables=60, questions_per_language=12)).files == bench.files
    True
    """
    config = config or BenchmarkConfig()
    builder = _Builder(config)
    builder.plan()
    builder.finish()
    files = _config_files(builder)
    cases = _Asker(builder).cases()
    return Benchmark(
        config=config,
        tables=tuple(builder.tables[k] for k in sorted(builder.tables)),
        edges=tuple(builder.edges),
        files=files,
        golden=tuple(cases),
    )


def write_benchmark(bench: Benchmark, out_dir: str | Path) -> Path:
    """Write *bench* under *out_dir*: ``project_config/`` and ``golden.jsonl``.

    Parameters
    ----------
    bench:
        The result of :func:`generate`.
    out_dir:
        Target directory; created, and a previous ``project_config/`` in it
        replaced.

    Returns
    -------
    pathlib.Path
        The ``project_config`` directory (what ``PROJECT_CONFIG_DIR`` takes).

    Examples
    --------
    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     cfg_dir = write_benchmark(generate(BenchmarkConfig(tables=60, questions_per_language=6)), tmp)
    ...     (cfg_dir / "schema.yaml").is_file() and (Path(tmp) / "golden.jsonl").is_file()
    True
    """
    root = Path(out_dir)
    config_dir = root / "project_config"
    if config_dir.exists():
        shutil.rmtree(config_dir)
    config_dir.mkdir(parents=True)
    for name, text in bench.files.items():
        (config_dir / name).write_text(text, encoding="utf-8")
    (root / "golden.jsonl").write_text(bench.golden_jsonl(), encoding="utf-8")
    return config_dir


def run_benchmark(out_dir: str | Path, *, bench: Benchmark | None = None) -> dict[str, Any]:
    """Measure the benchmark under *out_dir* with ``eval.cli recall``.

    The configuration is read once per process, so the measurement runs in a
    child interpreter whose ``PROJECT_CONFIG_DIR`` is the benchmark's.

    Parameters
    ----------
    out_dir:
        Directory written by :func:`write_benchmark`.
    bench:
        Unused by the measurement itself; accepted so a caller that already
        holds the benchmark can say so.

    Returns
    -------
    dict
        The recall report (``eval.recall.RecallReport.to_dict()``).

    Raises
    ------
    RuntimeError
        If the child process fails.
    """
    root = Path(out_dir).resolve()
    env = dict(os.environ)
    env["PROJECT_CONFIG_DIR"] = str(root / "project_config")
    env["PYTHONHASHSEED"] = "0"
    result = subprocess.run(
        [sys.executable, "-m", "eval.cli", "recall", "--golden", str(root / "golden.jsonl"), "--json"],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"eval.cli recall failed ({result.returncode}):\n{result.stderr or result.stdout}")
    return json.loads(result.stdout)


def _slice(cases: list[dict[str, Any]], *tags: str) -> dict[str, float]:
    chosen = [c for c in cases if all(t in c["tags"] for t in tags)]
    if not chosen:
        return {"cases": 0, "recall": 0.0, "budget_recall": 0.0, "full_pct": 0.0, "tables": 0.0,
                "median": 0.0, "precision": 0.0, "source_pct": 0.0, "source_n": 0}
    scored_src = [c for c in chosen if c["source_correct"] is not None]
    counts = sorted(len(c["retrieved"]) for c in chosen)
    middle = len(counts) // 2
    median = counts[middle] if len(counts) % 2 else (counts[middle - 1] + counts[middle]) / 2
    return {
        "cases": len(chosen),
        "recall": sum(c["recall"] for c in chosen) / len(chosen),
        "budget_recall": sum(c["recall"] if c["within_budget"] else 0.0 for c in chosen) / len(chosen),
        "full_pct": 100.0 * sum(1 for c in chosen if not c["missed"]) / len(chosen),
        "tables": sum(len(c["retrieved"]) for c in chosen) / len(chosen),
        "median": median,
        "precision": sum(c["precision"] for c in chosen) / len(chosen),
        "source_pct": (
            100.0 * sum(1 for c in scored_src if c["source_correct"]) / len(scored_src)
            if scored_src else 0.0
        ),
        "source_n": len(scored_src),
    }


def summarise(report: dict[str, Any], *, markdown: bool = False) -> str:
    """Render a recall report as the benchmark's headline table.

    Rows: all questions, English, Persian, and the multi-hop (snowflake and
    bridge), bridge-only, and measure-only questions in each language.

    Parameters
    ----------
    report:
        The dict :func:`run_benchmark` returns.
    markdown:
        Emit a Markdown table (for documents) instead of aligned text.

    Returns
    -------
    str

    Examples
    --------
    >>> summarise({"cases": [], "scored": 0}).splitlines()[0].startswith("slice")
    True
    """
    cases = report["cases"]
    rows = [
        ("all", ()), ("English", ("en",)), ("Persian", ("fa",)),
        ("English multi-hop", ("en", "multihop")), ("Persian multi-hop", ("fa", "multihop")),
        ("English bridge", ("en", "bridge")), ("Persian bridge", ("fa", "bridge")),
        ("English measure-only", ("en", "measure")), ("Persian measure-only", ("fa", "measure")),
    ]
    header = ("slice", "n", "mean recall", "in-budget recall", "full recall %",
              "tables mean/median", "precision", "source acc %")
    body = []
    for label, tags in rows:
        s = _slice(cases, *tags)
        body.append((
            label, str(s["cases"]), f"{s['recall']:.3f}", f"{s['budget_recall']:.3f}",
            f"{s['full_pct']:.1f}",
            f"{s['tables']:.1f} / {s['median']:g}", f"{s['precision']:.3f}",
            f"{s['source_pct']:.1f}" if s["source_n"] else "n/a",
        ))
    if markdown:
        lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in body]
        return "\n".join(lines)
    widths = [max(len(h), *(len(r[i]) for r in body)) for i, h in enumerate(header)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join([fmt.format(*header)] + [fmt.format(*r) for r in body])


def _config_from_args(args: argparse.Namespace) -> BenchmarkConfig:
    return BenchmarkConfig(
        seed=args.seed,
        tables=args.tables,
        questions_per_language=args.questions,
        sources=tuple(args.sources),
    )


def _add_generation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", required=True, help="Directory to write project_config/ and golden.jsonl into.")
    parser.add_argument("--seed", type=int, default=BenchmarkConfig.seed)
    parser.add_argument("--tables", type=int, default=BenchmarkConfig.tables, help="Approximate number of tables.")
    parser.add_argument("--questions", type=int, default=BenchmarkConfig.questions_per_language,
                        help="Questions per language.")
    parser.add_argument("--sources", nargs="+", default=list(BenchmarkConfig.sources),
                        help="Data source names (at least two; the first is the default).")


def main(argv: list[str] | None = None) -> int:
    """Command line: ``generate`` writes the benchmark, ``run`` also measures it.

    Parameters
    ----------
    argv:
        Arguments without the program name; ``sys.argv[1:]`` when ``None``.

    Returns
    -------
    int
        Exit code ``0``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m eval.benchmarks.retrieval_synth",
        description="Generate (and measure) the synthetic table-retrieval benchmark.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="Write project_config/ and golden.jsonl.")
    _add_generation_options(gen)
    run = sub.add_parser("run", help="Generate, then measure with `eval.cli recall`.")
    _add_generation_options(run)
    run.add_argument("--markdown", action="store_true", help="Print a Markdown table.")
    run.add_argument("--json", action="store_true", help="Print the full recall report as JSON instead.")
    args = parser.parse_args(argv)

    bench = generate(_config_from_args(args))
    write_benchmark(bench, args.out)
    if args.command == "generate":
        print(f"{len(bench.tables)} tables, {len(bench.golden)} questions -> {args.out}")
        return 0
    report = run_benchmark(args.out)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{len(bench.tables)} tables, {report['scored']} questions scored (seed {args.seed})")
        print(summarise(report, markdown=args.markdown))
    return 0


if __name__ == "__main__":
    use_utf8_console()
    sys.exit(main())
