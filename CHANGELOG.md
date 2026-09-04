# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [4.0.0] — 2026-09-04

Everything between 3.2.0 and here: authentication, the conversational
session API, the evaluation harness, LLM observability, the separation of
domain data from engine code, multi-dialect support, and two web clients.
Two of those changes are breaking.

### Breaking

- **Every route except `/health` now requires authentication.** Requests
  must carry `Authorization: Bearer <key>`. Keys are hashed with SHA-256
  (deliberately not bcrypt or argon2 — these are high-entropy tokens, not
  passwords) and compared with `hmac.compare_digest`. Startup fails
  closed if `AUTH_REQUIRED` is on and no key is configured. Issue keys
  with `python -m scripts.issue_api_key`.

- **Domain data no longer lives in the repository.** `project_config/` is
  gitignored and mandatory: `aliases.yaml`, `business_rules.yaml`,
  `entities.yaml`, `examples.yaml`, `metrics.yaml`, `schema.yaml`,
  `retrieval_hints.yaml`, `session_policy.yaml`. A missing file raises
  `ConfigNotFoundError` at start-up. There is deliberately **no** silent
  fallback to `project_config.example/` — running a real warehouse on
  sample business rules would produce confidently wrong SQL, which is
  worse than refusing to start. An existing deployment will not boot
  until those files are placed by hand.

- `/health` no longer reports the model name to unauthenticated callers.

### Added

- **Conversational sessions (`/v2/sessions…`)** — the `Turn` contract in
  `docs/api-contract-v2.md`, SSE streaming, declared assumptions with a
  source and an editability flag, `PATCH …/assumptions` to re-run a turn
  under edited assumptions, and CTE-composed refinement so «از بین
  آن‌ها» resolves against the previous turn rather than re-querying the
  warehouse.
- **Evaluation harness (`eval/`)** — golden set, execution accuracy,
  per-tag breakdown, error taxonomy, latency percentiles, an
  order-insensitive result fingerprint so "the same answer" is decidable,
  determinism measurement against a live endpoint, and a baseline
  regression gate with a CI exit code.
- **LLM status block** — 21 fields on every response and audit record:
  token counts, prefix-cache-hit ratio, timings, correction count,
  `finish_reason` read from the response rather than assumed, and
  detection of a model answering on its reasoning channel.
- **Multi-dialect support** — `SQL_DIALECT` selects the target; the model
  still generates T-SQL, which is transpiled and then **re-validated by
  the guard in the dialect it will actually execute in**. `tsql` and
  `sqlite` are verified by end-to-end execution; `postgres` and `mysql`
  transpile and re-validate cleanly but are unverified by execution and
  are not claimed. Per-dialect data lives in a `DialectProfile` registry
  (`security/dialects.py`), not in branches.
- **Static web client (`web/`)** — Persian, RTL, no build step: per-turn
  pipeline view, assumption chips, LLM status panel, result-shape
  selection between chart and table driven by the declared column types,
  chart defaults following Storytelling with Data, and export.
- **Flask web app (`webapp/`)** — bilingual FA/EN, sample-question panel,
  SQL beautifier, result pagination, copy and download.
- **Value resolution from the warehouse** (`retrieval/value_resolver.py`)
  with a stale-while-revalidate prefetch and single-flight refresh,
  replacing per-request lookups.
- **Guard rejection taxonomy** — `CorrectableRejection` versus
  `PolicyRejection`, with refusal tracked as an independent axis, so the
  model is no longer re-prompted for rejections no rewrite could satisfy.
- **One canonical Persian normalizer** (`core/persian.py`), versioned, so
  the cache and the retriever agree on what the same question is.
- **Observability** (`observability/`) — compliance-grade audit records
  that never contain result rows, stage timings, and the status block
  above.
- Operator tooling: `scripts/verify_deployment.py`,
  `scripts/issue_api_key.py`, `scripts/analyze_audit_log.py`.
- Documentation: `docs/api-contract-v2.md`,
  `docs/deployment-runbook.md`, `docs/db-hardening.md`, and bilingual
  tutorials under `docs/fa/` and `docs/en/`.
- Licence provenance notices (`core/provenance.py`, `NOTICE`) — stated
  where the licence is actually encountered rather than only in a file
  nobody opens.
- `tests/test_no_domain_literals.py` — walks the AST of first-party
  source and fails if a warehouse name reappears in an executable
  literal. This is what keeps the separation above from decaying.
- `project_config.example/` and `eval_data.example/` templates, so the
  suite and CI run with no real data present.
- CI across Python 3.11, 3.12 and 3.13, with doctests, coverage and an
  offline evaluation gate; branch protection on `main`.

### Changed

- **Prompt assembly is split into a byte-identical static prefix and a
  variable suffix**, so a local endpoint can reuse the prefix's KV cache.
  Per-turn content — session context, resolved filters, correction text —
  goes only in the suffix. This is enforced by tests, because breaking it
  is silent: the suite stays green and every request pays full prefill.
- All domain knowledge moved from Python modules to YAML loaded through
  `knowledge/config_loader.py`, and the guard's table and column
  allowlists are now derived from `schema.yaml` rather than hardcoded.
- `ensure_top` keeps its byte-identical text-splicing path for T-SQL and
  uses an AST row cap for the other dialects.
- The LLM layer reduced to a single OpenAI-compatible provider
  (`llm/providers.py`) behind a router with fallback and bounded retries.
- Rate limits raised from 60 to 600 requests per window and moved into
  `Settings`.
- The suite is now 2,079 tests, up from the 427 the README badge
  advertised at 3.2.0.

### Fixed

- **The rate-limit bucket keyed on the principal alone, then on the IP
  alone.** Either half by itself collapses distinct callers into one
  bucket — every analyst behind one UI host, or every request from one
  key. It is now the `(principal, ip)` pair.
- **The web UI sent no `Authorization` header**, so every route but
  `/health` returned 401 and the only thing the UI could reach was the
  liveness probe. The suite was green throughout because all of it was
  server-side.
- **`denied_columns` reached the query cache's scope key but never
  `validate_sql`** — the column ACL partitioned the cache without
  enforcing anything. It is now threaded through both the `/query` and
  the `/v2/…/turns` generation paths.
- **T-SQL `N'…'` literals and `'a' + 'b'` concatenation transpiled
  completely unchanged.** SQLite rejects the first as a syntax error and
  silently evaluates the second to `0` — an error and a plausible wrong
  number respectively. Both are now refused for non-T-SQL targets.
- Persian presentation forms and Arabic/Persian letter variants made the
  cache and the retriever disagree about the same question.

### Security

- Per-principal column ACL, enforced in the guard and not merely
  partitioned around in the cache.
- Audit records carry `principal_id` and never carry result rows.
- The guard parses to an AST and works from a closed allowlist; the
  bypass suite is parametrised over every claimed dialect, because a
  guard proven for one dialect and assumed for another has unknown holes.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — engine,
  security layer, conversational sessions, evaluation harness,
  observability, multi-dialect support, static web client, domain
  separation
- [**Melika Bahmanabadi**](https://github.com/MelikaBahmanabadi) — Flask
  web application, bilingual FA/EN system, UI work, schema knowledge

---

## [3.2.0] — 2026-06-12

License, documentation, and attribution release.

### Added

- **`LICENSE`** — Project is now licensed under the **Business Source License 1.1 (BUSL-1.1)**.
  - Free for non-production use.
  - Commercial/production use requires a written agreement with the author (Ali Sadeghi Aghili).
  - Converts automatically to Apache 2.0 on **2029-01-01**.
  - Includes an explicit **Attribution Requirement**: any derivative work must retain
    `LICENSE` and display: *"Based on Local SQL Agent by Ali Sadeghi Aghili —
    https://github.com/alisadeghiaghili/local-sql-agent"*.

- **`docs/tutorial.md`** — Comprehensive Persian-language tutorial covering:
  architecture overview, installation, first query end-to-end, TF-IDF retrieval
  internals, prompt building, SQL security pipeline, adding new tables/synonyms,
  miss-analysis workflow, test writing examples, health check, and troubleshooting.

- **`README.md`** — Updated to reflect BUSL-1.1 license, added license badge,
  attribution notice, contributors table, and link to `docs/tutorial.md`.
  `docs/tutorial.md` added to architecture tree.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — Creator & Lead Maintainer

---

## [3.1.0] — 2026-06-12

Minor release — FastAPI HTTP layer, SQLAgent auto-correct loop, LRU query cache,
tested LLM backend abstraction, and 1 bugfix in cache isolation.

### Added

- **`api/`** — FastAPI HTTP service package:
  - `server.py` — FastAPI app factory with `/query`, `/health`, `/cache/stats`,
    `/cache/invalidate`, `/cache/clear` endpoints. `_system_prompt` module-level
    variable allows test-time injection without environment changes.
  - `runner.py` — `run_query()`: cache-aware orchestrator that consults
    `query_cache` before calling `_agent.run`; populates cache on miss.
  - `query_cache.py` — `QueryCache`: thread-safe TTL + LRU in-process cache;
    `set / get / invalidate / clear / stats / reconfigure` API.
    `reconfigure()` now clears the store when TTL or max-size changes.
  - `models.py` — Pydantic `QueryRequest` / `QueryResponse` request/response
    models; `mode` field validates `"full" | "sql" | "result"`.
  - `errors.py` — `NLQError` hierarchy: `OutOfScopeError` (422),
    `ModelTimeoutError` (504), `ModelUnavailableError` (503),
    `QueryExecutionError` (500); FastAPI exception handlers registered
    for each type.
  - `middleware.py` — `RequestLoggingMiddleware`: per-request correlation ID
    (`X-Request-Id`), latency header (`X-Response-Time-Ms`), structured log.
  - `health.py` — `/health` endpoint: probes SQL Server connectivity and
    Ollama reachability; returns per-component status dict.

- **`llm/base.py`** — `LLMBackend` abstract base class + `SQLGenerationResult`
  dataclass (`sql`, `raw_response`, `attempt`, `correction_prompts`).

- **`llm/sql_agent.py`** — `SQLAgent`: wraps any `LLMBackend`; runs the
  generate → `clean_sql` → `validate_sql` loop with up to N correction
  attempts, feeding validation errors back into the prompt.

- **`llm/ollama_backend.py`** — `OllamaBackend(LLMBackend)`: HTTP client
  extracted from the old monolithic `ollama_client.py`; exponential back-off
  retry, `OUT_OF_SCOPE` sentinel detection, `ModelUnavailableError` mapping.

- **`config.py`** — `override_settings()` context manager added for
  test-time settings mutation without environment side-effects.

- **Test files added:**
  `test_api_endpoints.py`, `test_api_runner.py`, `test_cache_endpoints.py`,
  `test_errors.py`, `test_middleware.py`, `test_ollama_backend.py`,
  `test_query_cache.py`, `test_runner_cache.py`, `test_sql_agent.py`.
  Total test count: **427+** (up from 231).

### Fixed

- **`api/query_cache.py` — `reconfigure()` did not clear stale entries.**
  After calling `reconfigure(ttl_seconds=60)`, entries stored under the
  previous TTL could survive and be returned as valid hits.
  Fix: `reconfigure()` now calls `self._store.clear()` after updating
  `_ttl` and `_max_size`.

- **`tests/test_api_runner.py` — cache pollution between tests.**
  Added `autouse` fixture that calls `query_cache.clear()` before and after
  each test, preventing cache hits from earlier tests masking failures in
  later ones (e.g. `test_exception_translated` receiving a cached success).

- **`tests/test_query_cache.py` — `TestQueryCacheRunnerIntegration.test_second_call_hits_cache`
  triggered real Ollama connection.**
  The test called `reconfigure()` inside `override_settings()`, which (after
  the fix above) cleared the pre-populated cache entry. `run_query` then fell
  through to the real `OllamaBackend` → `ModelUnavailableError`.
  Fix: removed `reconfigure()` / `override_settings()` from the test; the
  cache is pre-populated with `query_cache.set()`, `_agent` is patched with
  a `MagicMock` that raises on `.run()`, and the returned object is asserted
  to be the exact cached instance. Added companion test
  `test_cache_miss_calls_agent_and_stores_result` for the miss path.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — FastAPI layer, SQLAgent, OllamaBackend, QueryCache, middleware, error taxonomy, test suite expansion

---

## [3.0.1] — 2026-06-11

Bugfix release — 12 failing tests resolved across three independent areas.

### Fixed

- **`schema_data/registry.py`** — `SchemaRegistry.build_context()` alias added;
  `None` and empty-tuple arguments now treated identically to "include all tables".
  Resolves `AttributeError: type object 'SchemaRegistry' has no attribute 'build_context'`
  (7 tests in `test_schema_registry.py`).

- **`schema_data/retriever.py`** — `_IdfDict.get()` now overrides `dict.get` to
  return `_max_idf` for unseen terms instead of the caller-supplied default.
  `dict.__missing__` is only invoked on `[]` access, not `.get()`, so the earlier
  implementation silently returned `0` for any token absent from the corpus.
  Also added `fallback: bool = True` parameter to `retrieve_tables()`; when
  `False`, an empty list is returned instead of the full table list when no
  table scores above `_MIN_SCORE`. Used by `analyse()` to avoid false negatives.
  Resolves `test_rare_term_has_higher_idf` and `test_detects_miss_when_table_not_retrieved`.

- **`schema_data/tables.py`** — Persian translations appended to every description
  so that common Persian terms (e.g. `معامله`, `مشتری`, `عرضه`) appear in the
  IDF corpus as seen terms with a finite IDF, while truly unseen terms receive
  the strictly higher `_max_idf`. This is required for `test_rare_term_has_higher_idf`
  to be meaningful.

- **`scripts/analyze_misses.py`** — `_KNOWN_TOKENS` now built from three sources:
  `TABLE_DESCRIPTIONS` values, `SYNONYMS` keys, **and** `SYNONYMS` values.
  Previously only descriptions were scanned; synonym expansion terms such as
  `مشتری` were therefore not recognised as known and appeared in the candidate
  list. Resolves `test_filters_existing_description_tokens`.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — retrieval bugfixes, IDF override, schema registry alias, miss-analysis token scan

---

## [3.0.0] — 2026-06-11

Full architectural consolidation. All feature branches merged into `main`.
Legacy `schema/` package retired. Modular retrieval pipeline fully activated.

### Added

- **`core/models.py`** — `RetrievalContext` frozen dataclass: single shared contract
  between the retrieval layer and `PromptBuilder`. Fields: `entities`, `facts`,
  `dimensions`, `relationships`, `business_rules`, `examples`, `filters`.
  Convenience properties: `selected_tables` (order-preserving dedup) and `is_empty()`.
- **`core/__init__.py`** — package marker.
- **`knowledge/aliases.py`** — `SYNONYMS` dict (156 entries, 10 categories: temporal,
  trade, customer, offer, ring/hall, commodity, broker, logistics, finance,
  aggregation) added alongside the existing `RING_ALIASES`. Resolves
  `ImportError: cannot import name 'SYNONYMS'` that blocked all 5 test files.
- **`schema_data/retriever.py`** — TF-IDF bigram engine migrated from the retired
  `schema/retriever.py`; now the canonical fallback for `EntityRetriever` and
  `FactRetriever`. Unchanged logic, corrected import path.
- **`knowledge/`** — full knowledge base promoted from `develop` branch:
  `entities.py` (entity catalog), `examples.py` (20+ tagged few-shot SQL examples),
  `metrics.py` (metric definitions), `business_rules.py` (expanded, topic-keyed rules).
- **`retrieval/`** — modular retrieval pipeline promoted from `develop` branch:
  `EntityRetriever`, `FactRetriever`, `RelationshipRetriever`, `RuleRetriever`,
  `ExampleRetriever` (tag-overlap scoring, 20+ bilingual tags), `ValueRetriever`
  (ring canonical lookup + Persian year regex).
- **`schema_data/`** — schema package promoted from `develop` branch:
  `registry.py` (LRU-cached `build_schema_context`), `columns.py`, `tables.py`,
  `relationships.py` (FK edge → JOIN SQL map).
- **`prompt_engine/`** — `PromptBuilder` and `PROMPT_TEMPLATE` promoted from
  `develop` branch; replaces inline string construction in `ollama_client.py`.

### Changed

- **`retrieval/context_retriever.py`** — `from core.models import RetrievalContext`
  now resolves correctly. `selected_tables` uses `dict.fromkeys()` for
  order-preserving deduplication (replaces non-deterministic `list(set())`).
- **`llm/ollama_client.py`** — pipeline comment updated to reflect final
  module paths (`retrieval.context_retriever`, `prompt_engine.builder`).
- **`tests/test_retriever.py`** — import updated to `schema_data.retriever`.
- **`tests/test_schema_registry.py`** — import updated to `schema_data.registry`.
- **`scripts/analyze_misses.py`** — import updated to `schema_data.retriever`.

### Removed

- **`schema/`** package fully retired (8 files deleted):
  `retriever.py`, `schema_registry.py`, `synonyms.py`, `tables.py`,
  `table_schemas.py`, `relationships.py`, `business_rules.py`, `__init__.py`.
  All functionality migrated to `schema_data/` and `knowledge/`.
- **`develop` branch** — merged and deleted.
- **`prompt-accurated` branch** — ancestor of `main`, no unique changes; deleted.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — full architectural consolidation, modular retrieval pipeline, knowledge base, schema migration

---

## [2.0.0] — 2026-06-06

### Added
- `config.py`: typed `Settings` dataclass (frozen, `__slots__`), `validate()` method, singleton via `lru_cache`
- `database/connection.py`: `dispose_engine()` helper for test teardown and hot-reload
- `database/executor.py`: wraps `SQLAlchemyError` in `RuntimeError`; debug-logs row/column counts
- `llm/ollama_client.py`: calls `clean_sql()` on every model response before returning
- `security/sql_guard.py`: **new** `clean_sql()` (strips markdown fences, preamble prose, converts LIMIT→TOP, fixes SELECT TOP n DISTINCT order); **new** `ensure_top()` (inject TOP n when missing); extended `_FORBIDDEN` list (`EXECUTE`, `XP_`, `SP_`); `LIMIT` now blocked in `validate_sql()`
- `logs/query_log.py`: `Literal["SUCCESS", "ERROR", "OUT_OF_SCOPE"]` status type; `as_dict()` serialisation method; `__slots__`
- `logs/logger.py`: uses `settings.log_dir`; catches `OSError` on write failure
- `exporters/excel_exporter.py`: uses `ExcelWriter` context manager; auto-fits column widths (capped at 60)
- `schema_data/retriever.py`: bigram scoring (bigram match counts ×1.5); `_MIN_SCORE` threshold; `_ALWAYS_INCLUDE` forced-table logic
- `schema_data/registry.py`: `lru_cache(maxsize=64)` on `build_schema_context()`; accepts `tuple` for cache-safe API
- `app.py`: structured REPL with emoji indicators; separates `RuntimeError` from `ValueError`; logs elapsed time; prints row-count summary; graceful `KeyboardInterrupt` / `EOFError` shutdown
- `tests/test_config.py`: rewritten for new `Settings` dataclass
- `tests/test_sql_guard.py`: **new** — full coverage of `clean_sql`, `validate_sql`, `ensure_top`

### Changed
- `config.py`: flat module-level constants replaced by `Settings` dataclass + `settings` singleton
- All modules now import `from config import settings` (single import point)
- `schema_data/registry.py`: `build_schema_context` takes `tuple[str, …]` for LRU-cache compatibility

### Removed
- `tests/test_validator.py`: replaced by `tests/test_sql_guard.py`

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — config refactor, SQL guard, exporter improvements, REPL, test coverage

---

## [1.1.0] — 2026-06-06

### Added
- Modular NLQ engine: `database/`, `exporters/`, `llm/`, `logs/`, `prompts/`, `security/`
- `app.py` entry point replacing `main.py`
- `config.py` with env-var-only configuration (no hardcoded credentials)
- `security/sql_guard.py`: read-only SQL validator
- Initial `schema/`: table registry, column schemas, FK relationships, business rules, TF-IDF keyword retriever
- `prompts/`: system prompt, business glossary, few-shot examples
- `.env.example` with all required variables documented
- `README.md` added

### Removed
- Legacy root scripts: `main.py`, `nlq.py`, `langsql.py`, `langchain_sql.py`, `nlq_with_sqlite.py`, `prompt_based_nlq.py`, `simple_nlq.py`, `create_db.py`
- Legacy folders: `agents/`, `src/`
- Old config files: `CHANGELOG` (plain text), `pyproject.toml`, `ruff.toml`

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — initial modular architecture, CLI, security layer, schema package

---

## [1.0.0] — initial

### Added
- Proof-of-concept scripts for NLQ-to-SQL via Ollama + LangChain
- `scripts/create_db.py`: SQLite sample database for local testing
- Initial `agents/`, `runners/`, `tests/` structure

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — proof-of-concept, initial structure
