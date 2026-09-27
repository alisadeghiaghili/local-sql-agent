# Optional domain-value test fixtures

This directory is documentation only -- it ships no fixture files, and
none are required to run the test suite or to start the application.

A handful of tests need a real deployment's real values (table/column
names, exact SQL text, exact row/column counts, real Persian alias
mappings) to mean anything. Those tests are marked
`@pytest.mark.domain_data` and already auto-skip whenever
`PROJECT_CONFIG_DIR` points at `project_config.example/` (this directory)
-- see the repo-root `conftest.py`. Against a real `project_config/`
(every developer's normal local run, and any real deployment) those tests
still skip individually, and cleanly, until the matching fixture file
below is created at `<PROJECT_CONFIG_DIR>/_test_fixtures/<name>`.

Populating these files is optional and recommended, not required. Nothing
else in the application reads this directory or these files; they exist
only so the five test modules listed below can run for real instead of
skipping. There is no schema validator for these files beyond each
loading test's own assertions -- an incorrectly shaped fixture simply
fails the test that reads it, the same as any other wrong expectation
would.

Do not add real fixture files here, under `project_config.example/`. This
file documents the mechanism only; the fixtures themselves belong under
the real, git-ignored `project_config/` directory (or wherever
`PROJECT_CONFIG_DIR` points for a given deployment), never in the public
tree.

## `schema_registry_snapshot.json`

Read by `tests/test_schema_registry_snapshot.py`. Pins the loaded
`schema_data` registry (derived from `<PROJECT_CONFIG_DIR>/schema.yaml`)
to a known-good snapshot, so a `schema.yaml` edit that silently changes
`security.sql_guard`'s effective table/column allowlist fails a test
instead of passing unnoticed.

```json
{
  "allowlist_tables": ["TableA", "TableB", "..."],
  "allowlist_table_count": 12,
  "columns_per_table": {"TableA": 4, "TableB": 15},
  "total_columns": 87,
  "all_table_names": ["TableA", "TableB", "LookupOnlyTable", "..."],
  "relationship_count": 25,
  "columns_hash": "<sha256 hex, see test_schema_registry_snapshot.py's _hash docstring>",
  "descriptions_hash": "<sha256 hex>",
  "relationships_hash": "<sha256 hex>"
}
```

## `retriever_expectations.json`

Read by `tests/test_retriever.py`. Query/expectation pairs for
`schema_data.retriever.retrieve_tables`/`_expand`/`_build_idf` that only
hold under the real `retrieval_hints.yaml`/`aliases.yaml`/`schema.yaml`.

```json
{
  "returns_at_most_top_n": {"query": "...", "max": 6},
  "membership_cases": [
    {"query": "...", "expect_in": "SomeRealTable"}
  ],
  "complex_query": {"query": "...", "expect_all_in": ["Date", "SomeRealFactTable", "Ring"]},
  "expand_single": {"word": "...", "expect_in": "..."},
  "expand_multiple": {"words": "...", "expect_all_in": ["...", "..."]},
  "idf_comparison": {"rarer": "...", "commoner": "..."}
}
```

## `session_engine_fixtures.json`

Read by `tests/test_session_engine.py`. A complete, self-contained SQLite
fixture (`CREATE TABLE`/`INSERT` statements and their bound values) built
from the real schema, plus the SQL/questions for the module's three-turn
scenario. This is the largest of the four fixtures because the whole
point of this test module is that its generated SQL must resolve against
the real `schema_data.columns.TABLE_COLUMNS` allowlist.

```json
{
  "system_prompt": "...",
  "q1_question": "...",
  "q2_question": "...",
  "q1_sql": "...",
  "q2_outer_sql": "...",
  "explicit_equivalent_sql": "...",
  "fresh_sql": "...",
  "ring_alt_name": "...",
  "create_table_sql": ["CREATE TABLE ...", "..."],
  "insert_customer_sql": "INSERT INTO ... VALUES (?, ?, 1)",
  "insert_customer_rows": [[1, "A"], [2, "B"]],
  "insert_ring_sql": "INSERT INTO ... VALUES (?, ?)",
  "insert_ring_rows": [[1, "..."], [2, "..."]],
  "insert_contract_sql": "INSERT INTO ... VALUES (?, ?, ?, ?, ?)",
  "insert_contract_rows": [[1, 1, 1, 1000.0, 5.0]]
}
```

## `value_retriever_expectations.json`

Read by `tests/test_value_retriever.py`. Persian phrase/expected-filters
pairs that only resolve through the real `aliases.yaml`'s `ring_aliases`.

```json
{
  "ring_year_month_day": {"query": "...", "expected": {"Ring": "...", "PersianYear": 1402}},
  "ring_only": {"query": "...", "expected": {"Ring": "..."}},
  "ring_year_month_day_persian_digits": {"query": "...", "expected": {"...": "..."}},
  "ring_season_year": {"query": "...", "expected": {"...": "..."}},
  "ring_full_date": {"query": "...", "expected": {"...": "..."}}
}
```

## `system_prompt_ring_aliases.json`

Read by `tests/test_prompts.py`'s `TestSystemPromptRingAliases`. Proves the
real `project_config/system_prompt.md` actually enumerates the real
`project_config/aliases.yaml` ring aliases (so the LLM can resolve a
Persian alias phrase straight from the prompt), without hardcoding any
real alias/hall name in tracked test source.

```json
{
  "required_aliases": ["...", "..."],
  "section_header": "...",
  "alias_hall_pairs": [
    {"alias": "...", "hall": "..."}
  ]
}
```
