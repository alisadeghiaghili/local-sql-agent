# Table retrieval: how it is measured and what was changed

Status: implemented (6.11.0). Scope: which tables go into the prompt on the
retrieval path (the schema is larger than `PROMPT_RETRIEVAL_TOKEN_BUDGET`; with
the static prefix every table is always there). Retrieval stays lexical:
aliases, fact patterns, TF-IDF over descriptions, column names and the
foreign-key graph. No embedding model is used, in line with
`DATASOURCES.md`.

A table that is not in the prompt cannot appear in the SQL, so a miss here is a
wrong answer whatever the model does; an extra table only costs tokens. Recall
therefore has priority, and precision is what is recovered afterwards.

## 1. Measuring it: `python -m eval.cli recall`

`eval/recall.py`, no LLM and no database. For each golden case the tables
`expected_sql` reads are resolved with the SQL guard's own table resolution
(`security.sql_guard.resolve_table_key`, so schema qualifiers and a bare name
shared by two schemas match the `schema.yaml` key), `ContextRetriever.retrieve`
runs, and the report gives:

| Figure | Meaning |
|---|---|
| mean recall | mean over cases of `gold tables retrieved / gold tables` |
| full-recall % | cases with every gold table retrieved |
| tables retrieved | mean, median and maximum per question |
| mean precision | mean of `gold tables retrieved / tables retrieved` |
| in-budget recall | recall with a case counted as 0 when the retrieved tables would not fit `PROMPT_RETRIEVAL_TOKEN_BUDGET` (retrieval falls back to every table for a question it understands nothing of; that scores recall 1.0 and is an unusable prompt) |
| source-selection accuracy | for cases with a `datasource`, how often `select_source_for_question` picks it |
| per tag | the same, per golden-case tag |

A case whose `expected_sql` names a table the loaded `schema.yaml` does not
resolve is listed as skipped, never counted as a miss. Output is deterministic
(sorted tables, no timestamps), as text or `--json`; `--min-recall` makes it a
gate. The dimension-vocabulary background refresh is switched off for the run,
so it cannot reach the warehouse. On the real golden set see the runbook, §18.4.

## 2. The synthetic benchmark

`python -m eval.benchmarks.retrieval_synth run --out DIR [--seed N]` generates,
from a seed, a schema of 400 tables over two data sources (generic English words
with Persian renderings, nothing from a real deployment), its `project_config`
and 480 questions (240 English, 240 Persian) with the reference SQL. It then
measures them with `eval.cli recall` in a child process (the configuration is
read once per process).

The schema: fact tables with `<Dim>_ID` keys, snowflaked classifications
(`Product` -> `ProductCategory`), many-to-many **bridge tables** (half with an
opaque name such as `Lnk0042` and a description that names neither side), three
dimensions shared by both sources, four bare names that exist in both sources as
two tables (`sales.Region` / `inventory.Region`), and decoy `Batch_ID`-style
columns. It is made hard on purpose: only half of the tables have an alias or
fact pattern, a foreign key is declared in `schema.yaml`, in `relationships.yaml`
or nowhere (then it follows the `<Table>_ID` convention) and a declared one
sometimes has a column name no convention explains (`Cust_Ref`); questions are
inflected (English plurals, Persian `-ها` with and without ZWNJ).

The questions, nine kinds: a dimension list; a fact by one dimension; by two; by
a period; by a dimension and a period; by the **classification** of a dimension
the question never names; by a dimension reached through a **bridge** the
question never names; the same through a bridge and a classification (three
joins); and a measure only (`total net weight by customer`, the fact table not
named). English and Persian are tagged separately, so are `multihop`, `bridge`
and `measure`.

Method: parameters were tuned on seed 7 only and checked on seeds 11 and 23,
which share the generator but not a single draw. Everything below is the mean
recall / full-recall % / tables / precision / source accuracy of the whole set
unless it says otherwise. The generator is mine, so the numbers say how much the
mechanisms help on a schema with these properties; they are not a prediction for
a given warehouse. Run `eval.cli recall` on your own golden set for that.

## 3. What changed, in the order it was measured

Seed 7, mean recall / in-budget recall / full-recall % / mean tables / precision
/ source accuracy %:

| Step | Recall | In-budget | Full % | Tables | Precision | Source % |
|---|---|---|---|---|---|---|
| main before this work | 0.735 | 0.708 | 42.1 | 9.2 | 0.608 | 90.8 |
| tokenise on word characters | 0.746 | 0.729 | 42.7 | 6.5 | 0.618 | 92.1 |
| column-name evidence | 0.779 | 0.779 | 49.2 | 3.6 | 0.623 | 92.1 |
| an alias hit no longer ends the search | 0.926 | 0.926 | 79.2 | 4.6 | 0.553 | 93.8 |
| join-path expansion (declared edges) | 0.941 | 0.941 | 84.0 | 4.7 | 0.550 | 93.8 |
| foreign keys inferred from `<Table>_ID` | 0.963 | 0.963 | 91.5 | 4.9 | 0.550 | 93.8 |
| pruning (final) | 0.966 | 0.966 | 92.1 | 4.0 | 0.678 | 95.2 |

1. **Word characters** (`schema_data/retriever.py`). Questions and descriptions
   were split on whitespace, so `customers?` and `سال؟` never matched a word.
2. **Column-name evidence.** A question word (or adjacent pair) that is a word of
   a column name (`NetWeight` -> `net`, `weight`, `net weight`), stemmed on both
   sides, adds to that table's score by its rarity among all columns; key columns
   (`ID`, `*_ID`) are skipped, and a word in most tables adds nothing. This is
   what finds a fact table the question does not name: measure-only questions go
   from 0.525 / 0.450 (English / Persian) to 0.925 / 0.775 recall.
3. **An alias or pattern hit no longer ends the search**
   (`retrieval/table_evidence.py`). `EntityRetriever` and `FactRetriever` returned
   only the aliased tables when any alias matched; the date dimension that
   `always_include` forces was dropped whenever another dimension matched. Up to
   `RETRIEVAL_EXTRA_TABLES` ranked tables scoring at least
   `RETRIEVAL_EXTRA_SCORE_RATIO` of the best are now added. Alias matching also
   folds Persian/Arabic spellings and ZWNJ now. This is the large recall gain and
   also the precision loss that step 6 recovers.
4. **Join-path expansion** (`retrieval/join_paths.py`). The tables that join the
   retrieved ones but are not named, found on the foreign-key graph
   (`schema.yaml` relationships and `relationships.yaml`): shortest path, at most
   `RETRIEVAL_JOIN_MAX_HOPS` (2) keys, ties by fewer inferred edges then table
   names, never through a table more than `RETRIEVAL_JOIN_MAX_HUB_DEGREE` (10)
   others reference, never across data sources, at most
   `RETRIEVAL_JOIN_MAX_ADDED_TABLES` (4) and only while the schema block fits
   `PROMPT_RETRIEVAL_TOKEN_BUDGET`. The additions are `RetrievalContext.join_tables`
   (part of `selected_tables`; `entities` and `facts`, which value matching and
   the "vocabulary unavailable" warning key on, are unchanged). Three and four
   hops found nothing more on the benchmark (+0.26 and +0.53 tables per question
   for the same recall), so two is the default.
5. **Inferred foreign keys.** A column `X_ID` / `XID` of table `T` is taken as a
   key to table `X`'s `ID` when neither file declares the pair; an ambiguous
   target (two schemas hold an `X`) is resolved by the source table's own schema
   or skipped, never guessed, and the data source must be shared. Used for
   retrieval only: nothing is written to configuration and the prompt gets no new
   join hint.
6. **Pruning** (`retrieval/pruning.py`). See section 4.

Steps 1, 2, 4 and 5 each raised recall without lowering precision (precision
moved by at most 0.003). Step 3 raised recall by 0.15 for 0.07 of precision,
which is why step 6 exists.

## 4. Pruning

Candidate generation keeps its recall; this stage runs on its output, before the
final join-path expansion, and takes the extra tables back without a model.

* **Evidence tier**: *strong* (alias, fact pattern or `always_include`), *column*
  (a column-name match) or *lexical* (description only). Strong tables and the
  column tables scoring at least `RETRIEVAL_PRUNE_SCORE_RATIO` (0.85) of the best
  unforced score are *anchors*; with none, the best table is.
* **Connectivity**: any other candidate survives only if it is joined within
  `RETRIEVAL_PRUNE_CONNECT_HOPS` (2) keys to an anchor or to a table already kept
  this way. A correct table with weak evidence is kept because it is connected.
  With `RETRIEVAL_PRUNE_CORROBORATE`, two such tables joined to each other keep
  each other (it saves a question whose strongest alias hit is a false one: the
  alias `turbine` inside "turbine profiles").
* Join-path expansion then runs on what is kept, so a bridge table is added only
  when it lies on a path between two kept tables.

Mean over seeds 7, 11 and 23, one signal removed at a time:

| | Recall | Full % | Tables | Precision | Source % | Multi-hop recall | Bridge recall |
|---|---|---|---|---|---|---|---|
| no pruning | 0.964 | 91.2 | 5.14 | 0.530 | 93.1 | 0.981 | 0.956 |
| all signals | 0.962 | 91.2 | 4.08 | 0.670 | 95.2 | 0.982 | 0.958 |
| no corroboration | 0.957 | 90.7 | 3.90 | 0.696 | 95.8 | 0.975 | 0.942 |
| no connectivity | 0.782 | 57.2 | 2.53 | 0.820 | 95.5 | 0.735 | 0.731 |
| one hop only | 0.943 | 88.3 | 3.70 | 0.702 | 95.2 | 0.937 | 0.862 |
| no score cutoff | 0.964 | 91.4 | 4.81 | 0.568 | 93.8 | 0.982 | 0.958 |

(The table is from the commit that introduced pruning, which still had one more
setting; the corroboration and connectivity rows are the ones that matter.)
Corroboration costs 0.026 of precision and is kept because without it the bridge
and multi-hop slices fall below where they were before pruning. A fourth rule, a
score margin that would let an unconnected description-only table survive,
changed nothing and was removed. Only about one in ten of the lexical-only tables
that were retrieved was a gold table (41 of 434 on seed 7), which is why dropping
the unconnected ones is cheap.

Parameters were chosen by F2 (recall weighted twice) on seed 7: score ratio 0.7 to
1.0 are equivalent (F2 0.888 to 0.891), 0.85 was taken; extra tables 2, 3 and 4
are equivalent once pruning is on, 3 was taken for headroom.

## 5. Final numbers

Baseline is main before any of this; "now" is this branch. Recall / full-recall
% / mean tables / precision / source accuracy %.

Whole set:

| Seed | Baseline | Now |
|---|---|---|
| 7 (tuning) | 0.735 / 42.1 / 9.2 / 0.608 / 90.8 | 0.966 / 92.1 / 4.03 / 0.678 / 95.2 |
| 11 (validation) | 0.712 / 37.3 / 8.2 / 0.609 / 90.0 | 0.951 / 89.2 / 4.00 / 0.680 / 95.6 |
| 23 (validation) | 0.748 / 41.7 / 11.0 / 0.572 / 86.0 | 0.971 / 92.7 / 4.20 / 0.656 / 94.8 |

English and Persian, and the slices that need a table the question never names:

| Seed | Slice | Baseline | Now |
|---|---|---|---|
| 7 | English | 0.722 / 41.2 / 12.9 / 0.593 / 90.4 | 0.967 / 92.1 / 3.86 / 0.702 / 95.4 |
| 7 | Persian | 0.748 / 42.9 / 5.6 / 0.622 / 91.2 | 0.965 / 92.1 / 4.20 / 0.654 / 95.0 |
| 7 | English multi-hop | 0.681 / 22.1 / 3.9 / 0.653 / 95.8 | 0.986 / 95.8 / 3.91 / 0.839 / 100.0 |
| 7 | Persian multi-hop | 0.726 / 24.2 / 3.7 / 0.703 / 97.9 | 0.986 / 96.8 / 4.42 / 0.764 / 96.8 |
| 7 | English bridge | 0.558 / 0.0 / 3.7 / 0.566 / 90.0 | 0.967 / 90.0 / 4.22 / 0.735 / 100.0 |
| 7 | Persian bridge | 0.675 / 12.5 / 3.5 / 0.652 / 100.0 | 0.967 / 92.5 / 4.40 / 0.705 / 100.0 |
| 11 | English | 0.696 / 35.0 / 10.9 / 0.594 / 92.9 | 0.946 / 88.8 / 3.91 / 0.676 / 96.2 |
| 11 | Persian | 0.728 / 39.6 / 5.5 / 0.623 / 87.1 | 0.956 / 89.6 / 4.10 / 0.683 / 95.0 |
| 11 | English multi-hop | 0.697 / 23.2 / 3.5 / 0.709 / 90.5 | 0.975 / 95.8 / 3.95 / 0.820 / 100.0 |
| 11 | Persian multi-hop | 0.693 / 18.9 / 3.4 / 0.718 / 90.5 | 0.965 / 93.7 / 4.13 / 0.799 / 98.9 |
| 11 | English bridge | 0.583 / 0.0 / 3.2 / 0.660 / 87.5 | 0.942 / 90.0 / 4.17 / 0.728 / 100.0 |
| 11 | Persian bridge | 0.608 / 5.0 / 3.1 / 0.682 / 85.0 | 0.917 / 85.0 / 4.12 / 0.730 / 97.5 |
| 23 | English | 0.744 / 42.5 / 16.4 / 0.560 / 87.1 | 0.969 / 92.1 / 4.00 / 0.682 / 95.8 |
| 23 | Persian | 0.752 / 40.8 / 5.7 / 0.583 / 85.0 | 0.973 / 93.3 / 4.40 / 0.629 / 93.8 |
| 23 | English multi-hop | 0.711 / 25.3 / 3.7 / 0.654 / 93.7 | 0.993 / 97.9 / 3.88 / 0.846 / 100.0 |
| 23 | Persian multi-hop | 0.739 / 27.4 / 4.0 / 0.650 / 93.7 | 0.993 / 97.9 / 4.63 / 0.739 / 100.0 |
| 23 | English bridge | 0.608 / 5.0 / 3.4 / 0.612 / 85.0 | 0.983 / 95.0 / 3.88 / 0.800 / 100.0 |
| 23 | Persian bridge | 0.667 / 7.5 / 3.9 / 0.596 / 85.0 | 0.983 / 95.0 / 4.62 / 0.712 / 100.0 |

Acceptance criteria set for the pruning stage, on all three seeds: mean recall
at least 0.92 (0.966, 0.951, 0.971), full recall at least 75% (92.1, 89.2, 92.7),
precision at least 0.62 (0.678, 0.680, 0.656, which is also above the 0.623 held
before the recall work began), source accuracy not lower than before pruning
(95.2, 95.6, 94.8 against 93.3, 93.5, 92.5) and no bridge or multi-hop slice
lower than before pruning. The remaining weak spots are Persian measure-only
questions (0.825, 0.950, 0.925: a Persian measure word that matches no column
word) and Persian bridge questions on seed 11 (0.917).

Source accuracy by slice moves by a case or two in both directions (a Persian
multi-hop slice on seed 7 is 97.9 -> 96.8, English measure-only on seed 11
95.0 -> 90.0); the overall figure rises on every seed.

## 6. Settings

All in `config.Settings`, environment-overridable, read at call time.

| Setting | Default | Meaning |
|---|---|---|
| `RETRIEVAL_EXTRA_TABLES` | 3 | ranked tables added beside alias/pattern hits; 0 restores the old behaviour |
| `RETRIEVAL_EXTRA_SCORE_RATIO` | 0.5 | an extra must score this fraction of the best of its kind |
| `RETRIEVAL_JOIN_EXPANSION` | true | add the tables that join the retrieved ones |
| `RETRIEVAL_JOIN_MAX_HOPS` | 2 | longest join bridged (2 = one intermediate table) |
| `RETRIEVAL_JOIN_MAX_ADDED_TABLES` | 4 | most tables one question may add |
| `RETRIEVAL_JOIN_MAX_HUB_DEGREE` | 10 | a table referenced by more tables is never a stepping stone |
| `RETRIEVAL_INFER_RELATIONSHIPS` | true | infer keys from `X_ID` column names |
| `RETRIEVAL_PRUNE` | true | prune candidates the evidence does not support |
| `RETRIEVAL_PRUNE_SCORE_RATIO` | 0.85 | a column-evidence table below this fraction of the best is not an anchor |
| `RETRIEVAL_PRUNE_CONNECT_HOPS` | 2 | keys allowed between a weak candidate and an anchor |
| `RETRIEVAL_PRUNE_CORROBORATE` | true | two joined weak candidates keep each other |

The scoring weights of the column-name evidence (`_COLUMN_MIN_WEIGHT`,
`_COLUMN_SCALE`, `_COLUMN_DESCRIPTION_SHARE` in `schema_data/retriever.py`) are
module constants next to `_BIGRAM_MULTIPLIER`, listed in the tuning-layer
allowlist.

## 7. What was not changed, and limits

* The static-prefix path is untouched: it reads `context.filters` only.
* The prompt format is unchanged; only which tables are listed differs. The
  relationship hints are still the ones declared in `schema.yaml`: an edge found
  only in `relationships.yaml` or inferred brings its table into the prompt but
  adds no join line.
* "Every table" is still returned for a question nothing matches (existing
  tests encode it); the in-budget recall exposes it, and column evidence made it
  rare on the benchmark (13 cases of 480 on seed 7 before, none after).
* `entities.yaml`, `fact_tables`, `fact_patterns` and `always_include` may name a
  table by its bare name where `schema.yaml` keys it `Schema.Table`; they are
  resolved to the keys (§8.1). A name that matches no key is kept as written.
* The benchmark is synthetic and written by the same hand as the mechanisms; the
  seeds are independent draws, not an independent generator. The real measure is
  `eval.cli recall` on a real golden set.

## 8. What a real question set showed that the benchmark did not

The synthetic benchmark of §2 scored about 0.96 mean recall. A real deployment's
own golden set, measured with `eval.cli recall`, scored far lower, and even a
plain count of one dimension table missed that table. The benchmark writes every
table in the configuration by its full `schema.yaml` key, which a hand-written
configuration does not have to. The real numbers belong to that deployment and
are not in this repository; the
mechanisms are, and the benchmark's numbers in §8.3 show that fixing them did
not cost the cases it already had.

Method. The golden set was split before anyone looked at a failure: within each
first tag, ordered by id, alternate cases went to a development half and a test
half. Failures were diagnosed on the development half only; the test half was
measured once before the work and once after. Each development miss was
attributed to the stage that dropped the table (below), and a fix had to be a
rule that holds for any schema, with a unit test that names no table of the
deployment.

### 8.1 Where the tables were lost, by stage

| Stage | What happened | Fix |
|---|---|---|
| Alias and fact-pattern path | By far the largest cause. The warehouse repeats table names across schemas, so `schema.yaml` keys are `schema.Name`, while `entities.yaml`, `fact_tables`, `fact_patterns` and `always_include` still said `Name`. A bare name that is not a key names no table: the schema block skips it, the ranking cannot score it, and `name in FACT_TABLES` was false for every key, so the fact/dimension split of the extras and of the TF-IDF fallback was wrong too. The aliased table, found correctly, never reached the prompt, and an unrelated table of the same bare name was sometimes retrieved in its place | `retrieval/table_keys.py` resolves a configured name to the keys it can mean, the way the SQL guard resolves a reference in a query (exact key, brackets and case ignored; otherwise the keys with that bare name, narrowed by a written qualifier). Every reader of those files uses it: the alias retrievers, the extras, pruning, join expansion's choice of the fact table, `always_include`, and the context retriever's warning and value-matching gate |
| Alias path, ambiguous name | After the above, `Customer` could mean `shop_Dim.Customer` and `other_Dim.Customer`, and retrieving both for every question halves precision | An ambiguous bare name is read in the schemas the configuration's other, unambiguous names live in (a file whose other tables are all `shop_*` means `shop_Dim.Customer`), or in the data source whose `keywords:` the question contains; when neither narrows it, every candidate is kept |
| Normalisation | Alias matching was a substring test, so `city` never matched `cities` (`-ies` for `-y`) and a plural alias never matched a singular question | Whole words are also compared after the plural stemming the column evidence already uses |
| Alias path, word order | A multi-word alias (`gear tooth pitch`) is rarely written word for word; "pitch of each tooth on the gear" has all its words and no phrase | A multi-word alias also matches when all its stemmed words occur in the question, in any order |
| Forced tables (`always_include`) | A question that says "in 2023" or "in 1401" is about a period but contains no word for one, so the calendar dimension was neither forced nor ranked | A four-digit calendar year (1300-1599 Solar Hijri, 1900-2199 Gregorian) adds the words `year` and `سال` to the expanded question, which is all the configured triggers and the descriptions need to see it |
| TF-IDF and column evidence | Not a cause of any development miss once the above were fixed | unchanged |
| Extras cap, pruning, join expansion | Not the stage that lost a table once the alias path was right: pruning and join expansion kept what the corrected names supported, and the extras cap lost no development table | unchanged, and no `RETRIEVAL_*` default was changed |

What is left unsolved on the development half is a gold table with no lexical
link to the question at all (a fact table the reference SQL reads for a question
that never says what it counts, while a sibling fact table is named by a
pattern). Lexical retrieval cannot find it, and the ADR against embeddings and
model-based linking stands.

### 8.2 Consequences worth knowing

* A configuration that already writes full keys is untouched: an exact key
  resolves to itself, and every test of the previous behaviour passes unchanged.
* On a configuration that wrote bare names for qualified keys, the retrieved
  tables are now the schema keys. `ContextRetriever.entities` therefore holds
  keys, so the dimension vocabulary (`PREFETCH_COLUMNS` is keyed by schema key)
  is consulted for them, which it silently was not before.
* A year-shaped number that is not a year ("orders above 2000") also forces the
  calendar dimension; the cost is one extra table.
* `eval.cli recall` is the way to see it on your own set: retrieved names that
  are not keys of `schema.yaml` were the symptom.

### 8.3 Synthetic benchmark, before and after

The benchmark names its tables by full key, so only the stemming, word-order and
year rules touch it. Mean recall / full-recall % / mean tables / precision /
source accuracy %, whole set:

| Seed | Before | After |
|---|---|---|
| 7 | 0.966 / 92.1 / 4.0 / 0.678 / 95.2 | 0.970 / 93.1 / 4.0 / 0.678 / 95.2 |
| 11 | 0.951 / 89.2 / 4.0 / 0.680 / 95.6 | 0.955 / 90.2 / 4.0 / 0.679 / 95.6 |
| 23 | 0.971 / 92.7 / 4.2 / 0.656 / 94.8 | 0.975 / 93.8 / 4.2 / 0.655 / 94.6 |

English questions gain (+0.007, +0.007 and +0.008 recall on the three seeds, from
the alias rules and the year rule) and Persian questions are unchanged (a
Persian plural was already found by the substring test). Precision moves by at most 0.002 on a slice, except the
English measure-only slice of seed 23 (0.925 to 0.975 recall, precision -0.007).
Source accuracy is unchanged except one case on seed 23. No other slice of §5
changes. The benchmark cannot show the main fix of §8.1, because its
configuration names every table by its full key; that is why a real set was
needed.


## 9. Checking the values the model filters on (`FILTER_VALUE_CHECK`)

`retrieval/dimension_vocabulary.py` caches, for a table's
`prefetchable_columns`, the whole distinct value set of each column, and until
now used it in one direction only: to find a value in the question. The model
also writes values (`WHERE r.[Name] = N'Ring'`), and a value that is in no row
returns an empty or wrong answer the guard has no reason to refuse.
`retrieval/filter_values.py` reads the same cache in the other direction, on the
statement the guard accepted.

**Decisions, and why.**

- **Off by default, and byte for byte absent when off.** The owner's constraint is
  that nothing that works may break. With the setting off the new code is not
  reached; `tests/test_filter_value_check_pins.py` pins the prompts, the executed
  statement, the correction texts, the audit line's keys and the guard's decisions
  of `/query` and of a session turn, and was written before the code.
- **The cache only.** A check that waited on a database read would put the
  warehouse on the request path, which the vocabulary module was built to avoid. A
  column with no cached values is not checked; a stale list is used; a list that
  has the length of the prefetch cap (`SELECT DISTINCT TOP (DEFAULT_TOP_N)`) may be
  cut short and is not used.
- **Resolve the column the way the guard does.** The literal is only meaningful
  against the column it is compared to, so `security.sql_guard.column_origins`
  follows an alias, a CTE or a derived table to the stored column (a plain copy
  only; `UPPER(r.Name)` is not a stored column). A name two tables share is known if
  either has the value and is not checked unless both have a vocabulary. The
  function is new and called by nothing in `validate_sql`.
- **Same normaliser as the question.** `core.persian.normalize_for_matching`: a
  value the question could have found is a value the statement may use.
- **`<>` and `NOT IN` are checked.** An invented value in an exclusion does nothing,
  which is as wrong as one in an inclusion, and the same message fits both.
- **One round, outside the budget, never a refusal.** The correction is a
  side-step in the loop: the corrected statement replaces the original only if it is
  accepted by the guard, has no unknown literal (in a session turn, also drops no
  filter the question named) and runs; every other outcome runs the original
  through the unchanged loop. It adds at most one model call, and none when
  `MAX_CORRECTION_ATTEMPTS` is `0`. A real value that is not cached yet (a new ring)
  costs that call and runs as written; the message says the list may lag and tells
  the model to keep a value the question names that is not listed.
- **The prompt layout is the correction layout.** The text is appended to the
  question segment, the static prefix is not touched, and the real values are fenced
  as warehouse data (`prompt_engine/untrusted.py`). They are the nearest five by
  character trigrams of the normalised forms: the sets are small by design.
- **The audit log keeps names, never values.** The block has the column, a hash and
  the length of each unknown literal, and flags; a block with any other key or type
  is refused when the record is built. The literal is in `generated_sql` when the
  original ran, as it always was; the real values the model was shown are warehouse
  data and are not written.

**Not covered.** Mode `sql` (no correction loop), the `_prev` refinement of a
session turn (its inner statement is the previous turn's), a column that is not in
`prefetchable_columns`, `LIKE`, and a column inside an expression. (§11 adds a
spelling fix that does read the letters of a `LIKE` pattern.)


## 10. A Persian plural written attached to its word

`تالارها`, `تالار‌های` (the normaliser removes the half-space, so this is the same
text) and `تالارهایی` are one token to the lexical matchers, and no description,
trigger phrase, synonym or data-source keyword holds that token; `تالار ها` (with
a space) puts `تالار` in a token of its own and matched. The alias substring test
(`retrieval/table_evidence.py`) already found a one-word alias inside the longer
token, so the table an alias names was usually found; what failed was everything
that compares tokens: the TF-IDF scorer, the `always_include` phrases of several
words, the synonyms, the column evidence for the endings its old stem rule did not
know, the alias of several words, and the data-source `keywords:` (a whole-word
match, so `تالار` never matched `تالارها`, and a question could be routed to the
wrong source or none).

**The rule.** `core.persian.strip_plural_suffix` gives the stem of a normalised word
that ends in `ها`, `های`, `هایی` or `ها` with a possessive pronoun (`هایم هایت هایش
هایمان هایتان هایشان`), when the stem is at least three characters and all of them
letters. Not stripped, on purpose: `ان` (`تهران`, `ایران`, `مشتریان`) and `ات`
(`حیات`, `امکانات`), which end names and ordinary words as often as plurals; a word
in Latin script (the English plural has its own rule in `schema_data/retriever.py`);
`ها` and `های` alone (no stem). A word that ends in `ها` without being a plural, with
a long enough stem, is reduced all the same (`انتها` gives `انت`); that is harmless
only because of the next point.

**Additive.** The stem is an extra term, never a replacement: the scorer adds the
terms and bigrams of the stemmed question to the question's own, the triggers try
the stemmed question as well, a synonym is looked up under the word and under its
stem, the alias of several words is compared a second time with its words and the
question's reduced, and a data-source keyword is looked for in the text as written
and in the text with every such word reduced. A question without such a word takes
the same code path as before and scores the same (`tests/test_plural_matching_pins.py`
pins numbers on a Persian corpus, and `tests/test_plural_matching.py` checks every
change against the matching with the stems switched off); with one, every table that
scored still scores and no lower, so what can change is the order, and with more than
six candidates the top-six cut.

**Not changed: the vocabulary matcher.** `retrieval/dimension_vocabulary.py` compares
the question with the values of a column by substring, which finds `تالار` inside
`تالارها` already, and its token fallback splits the normalised text on spaces, not
with this tokenizer. An extra term there could make two values tie on score and turn
a clear match into a clarification.

**Measured.** `python -m eval.benchmarks.retrieval_synth run --seed N`, 480 questions,
the Persian half inflected with `-ها` with and without the half-space; mean recall of
the Persian questions, before and after: seed 7 0.965 and 0.977, seed 11 0.956 and
0.978, seed 23 0.973 and 0.985. The English half is identical on all three seeds, and
mean precision does not drop (seed 7 0.678 and 0.681, seed 11 0.679 and 0.687, seed 23
0.655 and 0.663). The generator is the same as in §2; the numbers show the mechanism,
not a prediction for a given warehouse.

## 11. A filter literal written with other letter forms (`FILTER_VALUE_SPELLING`)

The warehouse stores some values with the Arabic `ي`/`ك`; the model writes the
Persian `ی`/`ک`. Under the deployment's collation they are different strings, so
`WHERE r.[Name] = N'<Persian spelling>'` returns nothing while the Arabic one
returns rows, and §9's check, which compares normalised forms, calls the Persian
spelling a known value. `retrieval/filter_spelling.py` fixes the statement itself,
with no model call, from the same cached vocabularies and the same column
resolution as §9 (`usable_vocabularies`, `security.sql_guard.column_origins`).

**Decisions, and why.**

- **On by default; one switch.** It acts only when a literal is not a stored value
  and exactly one stored value is its spelling variant, and it writes the stored
  value, so it is the same filter the model meant, spelled as the column has it.
  `FILTER_VALUE_SPELLING=false` makes everything byte for byte as before
  (`tests/test_filter_spelling_pins.py`, written before the code).
- **The cache is read, nothing starts.** No query, and not even the background
  refresh §9's check starts for a cold or stale column: a deployment that never
  turned the check on gets no read it did not have. A stale list is used; a value
  added since the last refresh is simply not known, and a literal that does not
  match one stored value stays as written. A list as long as the prefetch cap is not
  trusted. A denied or join-only column is never read.
- **A narrow fold.** `core.persian.letter_form_key`: `ي ى` with `ی`, `ك` with `ک`,
  presentation forms and ligatures by NFKC (applied to those characters only), NFC,
  and ZWNJ with none. Not `ة`/`ه` (different letters, and `ه` is in nearly every
  word), not `ۀ`/`هٔ` (a letter and an optional mark), not the alefs, not digits (a
  year or a code is not a spelling), not case or spaces. `normalize_for_matching` is
  unchanged, so the query cache keys are.
- **Exactly one candidate.** Two stored values that fold to the same key leave the
  literal alone, and so does another stored value that only differs from the literal
  by what `normalize_for_matching` folds (a trailing space, case): the comparison may
  already find it.
- **`LIKE` is rewritten by group, not expanded.** For each group (`ی ي ى`, `ک ك`),
  when every stored value that holds a letter of the group holds the same one, the
  pattern's letters of the group become that one. A column that stores both forms is
  left alone: finding every row would take an `OR` of patterns at that place of the
  condition (inside `NOT`, under `OR`, one copy per letter), which changes the shape
  of the statement; that case stays the model's and §9's.
- **The edit is a splice, checked twice.** The literal's token is found through the
  position sqlglot records on the node, only that span is replaced (prefix and quoting
  kept, a quote in the value doubled), and the result is parsed again and must read as
  the original with just those literals changed. Then the guard validates it again in
  the dialect it runs in. Either refusal runs the original statement.
- **First, so the check sees the fixed statement.** With `FILTER_VALUE_CHECK` on, the
  fix runs before it: a spelling difference is a known value and costs no correction
  round, and the corrected statement of §9 is respelled before it runs too.
- **The audit log keeps names, never values.** `value_spelling`: `{"fixed": n,
  "columns": ["Table.Column"]}`, absent when nothing was rewritten.

**Not covered.** The same cases as §9: mode `sql`, the `_prev` refinement, a column
that is not in `prefetchable_columns`, and a column inside an expression.

## 12. A `ring_aliases` key is a stored value, never a table key (6.16.3)

`aliases.yaml`'s `ring_aliases` is the static first tier of value resolution:

- **What the map holds.** Each key is the **exact stored value** of the
  default-scope dimension's name column (a hall, in the production deployment).
  Its list holds the forms analysts type for it.
- **How it is used.** `ValueRetriever.extract_ring` finds a form in the
  question and returns the key. The key becomes the default-scope filter
  (`WHERE r.[Name] = N'<key>'`), and it wins over every other tier.

**What went wrong.**

- **The wizard wrote tables, not values.** From 6.13 to 6.16.2 the setup wizard
  wrote one entry per **table** into this map: the key was the table's key and
  the list was the table's aliases.
- **Questions then matched a table.** A question that used a table's alias
  ("currency", "halls", "trades") matched one of those entries and was filtered
  on the table's own name (`N'Currency'`, `N'Ring'`).
- **The answer was empty.** No row matched, and nothing in the turn said why.

**The rule since 6.16.3.**

- **At load** (`core/ring_names.py`, `knowledge/aliases.py`), an entry is left out
  when its key equals a key of `schema.yaml`'s `tables` as a whole, letter case
  ignored. One WARNING names how many and which. Only that exact match counts: a
  hall called `North Hall` is untouched because a table is called `Hall`, and a
  bare name that merely resolves to a table key is not a match either. Without a
  readable `schema.yaml`, every entry is used as written.
- **The preflight** check `Aliases are curated` fails on such entries and names
  them.
- **The wizard** no longer writes the map. On a re-run it drops a table-keyed
  entry from an earlier file and moves the entry's aliases to that table's
  entity in `entities.yaml`, where table retrieval reads them, so no alias is
  lost.
- **Drafting.** `python scripts/draft_policies.py --ring-aliases [--generic-word
  W]` drafts entries from the stored values for review. Each draft holds the
  value itself, and the value without a leading generic word when that form is
  unique. Drafts are written longest value first, because the first entry with a
  form in the question wins.

**Not covered.** A real stored value that is exactly a table key, letter case
aside, cannot be told apart from a wizard entry: it is ignored and the preflight
fails. There is no setting to switch the rule off.
