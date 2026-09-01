# Local SQL Agent

> Ask your SQL Server database a question in Persian or English.  
> Get back precise T-SQL — generated locally, executed securely, zero data leaves your machine.

[![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/Tests-427%2B-green)](tests/)

---

Most Text-to-SQL tools assume your data is in the cloud and your questions are in English. This project was built for the opposite: an on-premise SQL Server warehouse at the **Iran Mercantile Exchange (IME)**, where analysts ask questions in Persian, data is sensitive, and there is no budget for external APIs.

The result is a fully local NLQ engine — modular retrieval pipeline, SQL security guard, FastAPI service layer, and a knowledge base you can extend without touching engine code.

---

## In action

```bash
python app.py

Question: برترین مشتریان از نظر ارزش خرید در سال 1402 کدامند؟

══════════════════════════════════════════════════════════════
GENERATED SQL
══════════════════════════════════════════════════════════════
SELECT TOP 10
    c.Name,
    SUM(cc.TotalPrice) AS PurchaseValue
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Customer] c ON cc.BuyerCustomer_ID = c.ID
JOIN [Auction_Dim].[Date]     d ON cc.Date_ID = d.ID
WHERE d.PersianYear = 1402
GROUP BY c.Name
ORDER BY PurchaseValue DESC

Returned Rows: 10  |  Execution Time: 1.24s  |  Excel: exports/result_20260613_142257.xlsx
```

Or over HTTP:

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "فروش ماهانه تالار پتروشیمی در 1402", "mode": "full"}'
```

```json
{
  "question": "فروش ماهانه تالار پتروشیمی در 1402",
  "sql":    "SELECT TOP 1000 d.PersianMonthName, SUM(c.TotalPrice) AS TradeValue ...",
  "result": [{"PersianMonthName": "فروردین", "TradeValue": 48320000000}, ...],
  "row_count": 12,
  "status": "SUCCESS"
}
```

---

## How it works

Before the LLM sees anything, six retrievers build a scoped context from your question:

```
Question (Persian / English)
    │
    ▼
 ContextRetriever
    ├─ EntityRetriever        alias match → TF-IDF fallback
    ├─ FactRetriever          keyword match → TF-IDF fallback
    ├─ RelationshipRetriever  JOIN clauses for selected tables
    ├─ RuleRetriever          domain business rules
    ├─ ExampleRetriever       tag-scored few-shot SQL examples
    └─ ValueRetriever         ring canonical name + Persian year
    │
    ▼
 PromptBuilder   →  schema + rules + examples, precisely scoped
    │
    ▼
 SQLAgent        →  generate → clean → validate → auto-correct
    │
    ▼
 SQLGuard        →  blocks DDL/DML, injection, rewrites LIMIT→TOP
    │
    ▼
 SQL Server      →  result set  →  Excel / CSV / JSON
```

Scoping the prompt — instead of dumping your full schema — is what makes locally-run 8B–20B models accurate enough for production.

---

## Features

| | Feature | Detail |
|---|---|---|
| 🔒 | **On-premise LLM** | OpenAI-compatible endpoint (vLLM / LM Studio / Ollama /v1) + SQL Server. No cloud provider required. |
| 🌐 | **Bilingual** | Persian and English questions handled natively. |
| 🧩 | **Modular retrieval** | 6 independent retrievers — swap or extend without touching the engine. |
| 🔍 | **Two-tier retrieval** | Fast alias/pattern matching first; TF-IDF bigram engine as fallback. |
| 🎯 | **Few-shot learning** | Tag-scored example selector injects the most relevant SQL patterns. |
| 📐 | **Business rule injection** | Domain rules injected per question topic at prompt-build time. |
| 🛡️ | **SQL security guard** | Blocks DDL, DML, injection patterns; converts LIMIT→TOP. |
| 🔄 | **Auto-correct loop** | SQLAgent retries with error feedback when SQL fails validation. |
| ⚡ | **FastAPI HTTP API** | REST endpoints for query, cache, and health check. |
| 💾 | **LRU query cache** | Thread-safe TTL + LRU cache; configurable size and expiry. |
| 📤 | **Structured exports** | Excel, CSV, JSON with timestamped filenames. |
| 📋 | **Structured logging** | Rotating JSONL logger with correlation ID, latency, row count. |
| 🧪 | **Test suite** | 470+ unit + integration tests; GitHub Actions CI. |

---

## Quick start

**Requires:** Python 3.11+, an OpenAI-compatible endpoint (vLLM / LM Studio / Ollama `/v1`) reachable via `OPENAI_BASE_URL`, SQL Server + ODBC Driver 17

```bash
# 1. Clone and install
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Set at minimum:
#   DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/DB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
#   OPENAI_BASE_URL=http://your-llm-host:8000/v1
#   OPENAI_MODEL=gpt-oss-20:F16
#   OPENAI_API_KEY=your-key

# 3a. CLI
python app.py

# 3b. HTTP API
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

**→ Full tutorial (installation · first query · extending the domain · writing tests · diagnosing misses):**  
**[English](docs/en/tutorial.md) · [فارسی](docs/fa/tutorial.md)**

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (vLLM / LM Studio / Ollama `/v1`) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name served by the endpoint |
| `OPENAI_API_KEY` | *(required)* | API key for the endpoint |
| `DB_CONNECTION_URL` | *(required)* | SQLAlchemy connection string |
| `QUERY_TIMEOUT_SECONDS` | `60` | Max query execution time (seconds) |
| `MAX_ROWS_RETURNED` | `1000` | Hard row cap applied to all queries |
| `CACHE_TTL_SECONDS` | `300` | Query cache TTL in seconds (`0` = disabled) |
| `CACHE_MAX_SIZE` | `256` | Maximum number of cached query results |
| `LOG_DIR` | `logs` | Log file directory (auto-created) |
| `EXPORT_DIR` | `exports` | Export file directory (auto-created) |
| `API_KEYS_JSON` | *(empty)* | JSON array of `{"id","name","key_sha256","denied_columns"?}` — see [Authentication](#authentication-phase-8) |
| `AUTH_REQUIRED` | `true` | Fail-closed auth gate; `false` is a logged escape hatch |
| `APP_DOCS_PUBLIC` | `false` | Serve `/docs` `/redoc` `/openapi.json` without credentials |

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/query` | Run a natural-language query; returns SQL + result set |
| `GET` | `/health` | DB + LLM endpoint reachability probe |
| `GET` | `/cache/stats` | Cache size, hits, misses, evictions |
| `POST` | `/cache/invalidate` | Remove a specific cached entry |
| `POST` | `/cache/clear` | Flush the entire cache |

All routes above, plus the `/v2/sessions*` conversational endpoints (see
`docs/api-contract-v2.md`), require `Authorization: Bearer <key>` except
`GET /health` — see [Authentication](#authentication-phase-8).

**Error taxonomy:**

| Exception | HTTP | When |
|---|---|---|
| `UnauthenticatedError` | 401 | Missing/invalid API key on a protected route |
| `OutOfScopeError` | 422 | Question is outside the domain |
| `ModelTimeoutError` | 504 | LLM request timed out |
| `ModelUnavailableError` | 503 | LLM endpoint unreachable after all retries |
| `QueryExecutionError` | 500 | SQL Server execution failure |

---

## Extending the domain

All domain knowledge lives in `knowledge/`. No engine code needs to change.

```python
# Add a trading hall alias — knowledge/aliases.py
RING_ALIASES["تالار برق"] = ["برق", "تالار برق", "بازار برق", "رینگ برق"]

# Add a business rule — knowledge/business_rules.py
BUSINESS_RULES["electricity"] = (
    "برق در تالار انرژی معامله می‌شود. "
    "واحد اندازه‌گیری مگاوات‌ساعت است."
)

# Add a few-shot example — knowledge/examples.py
{
    "tags": ["electricity", "trade", "value"],
    "question": "ارزش معاملات برق در ماه گذشته",
    "sql": "SELECT SUM(TotalPrice) AS ElectricityValue FROM [Auction_Fact].[Contract] ..."
}

# Add a new table — schema_data/tables.py + columns.py + relationships.py
TABLE_DESCRIPTIONS["Broker"] = (
    "Registered brokerage firms (کارگزاری‌ها) licensed to trade on the exchange."
)
```

Full step-by-step guide: **[English tutorial](docs/en/tutorial.md)** · **[آموزش فارسی](docs/fa/tutorial.md)**

---

## Project structure

```
local-sql-agent/
├── app.py                    # CLI entry point (REPL)
├── config.py                 # Typed Settings singleton (env-based)
├── api/                      # FastAPI HTTP service
│   ├── server.py             #   app factory + endpoints
│   ├── runner.py             #   cache-aware query orchestrator
│   ├── query_cache.py        #   thread-safe TTL + LRU cache
│   ├── models.py             #   Pydantic request/response models
│   ├── errors.py             #   NLQError hierarchy → HTTP handlers
│   ├── middleware.py         #   correlation ID + latency headers + rate limiting
│   ├── auth.py               #   AuthMiddleware + require_principal (Phase 8)
│   └── health.py             #   /health — DB + LLM endpoint probes
├── knowledge/                # ★ Domain knowledge (edit to extend)
│   ├── aliases.py            #   RING_ALIASES + SYNONYMS
│   ├── business_rules.py     #   rules per topic key
│   ├── entities.py           #   dimension entity catalog
│   ├── examples.py           #   tagged few-shot SQL examples
│   └── metrics.py            #   metric definitions + expressions
├── retrieval/                # Modular retrieval pipeline
│   ├── context_retriever.py  #   orchestrator → RetrievalContext
│   ├── entity_retriever.py   #   dimension table detection
│   ├── fact_retriever.py     #   fact table detection
│   ├── relationship_retriever.py  # JOIN clause selection
│   ├── rule_retriever.py     #   business rule injection
│   ├── value_retriever.py    #   filter extraction
│   └── example_retriever.py  #   tag-scored few-shot selection
├── schema_data/              # Schema definitions (single source of truth)
│   ├── registry.py           #   SchemaRegistry + LRU cache
│   ├── columns.py            #   column allowlist
│   ├── tables.py             #   bilingual table descriptions
│   ├── relationships.py      #   FK → JOIN SQL map
│   └── retriever.py          #   TF-IDF bigram fallback engine
├── prompt_engine/
│   ├── builder.py            #   PromptBuilder.build()
│   └── templates.py          #   PROMPT_TEMPLATE
├── llm/
│   ├── sql_agent.py          #   generate → clean → auto-correct loop
│   ├── wizard_llm.py         #   OpenAI-compatible backend (retries + back-off)
│   └── base.py               #   LLMBackend ABC
├── security/
│   ├── sql_guard.py          #   clean_sql / validate_sql / ensure_top
│   └── auth.py               #   Principal, API-key resolution, cache scope key (Phase 8)
├── database/
│   ├── connection.py         #   SQLAlchemy engine singleton
│   └── executor.py           #   timeout + row cap enforcement
├── exporters/                # Excel / CSV / JSON exporters
├── scripts/
│   ├── analyze_misses.py     #   offline retrieval miss diagnostics
│   └── issue_api_key.py      #   mint a new API key (Phase 8)
├── docs/
│   ├── en/tutorial.md        #   full English tutorial
│   └── fa/tutorial.md        #   full Persian tutorial — آموزش کامل فارسی
└── tests/                    # 470+ unit + integration tests
```

---

## Tests

```bash
pytest tests/ -v                        # all tests
pytest tests/test_sql_guard.py -v       # one module
pytest --cov=. --cov-report=html        # with coverage report
```

CI runs on every push via GitHub Actions (Python 3.13).

---

## Security model

Every generated SQL query passes through `security/sql_guard.py` before execution.
As of Phase 1, `validate_sql` is **parser-based** (via [sqlglot](https://sqlglot.com/),
pinned to the `tsql` dialect), not a string blocklist — see the module's
docstring for the full mechanism and `tests/test_sql_guard_bypass.py` for the
bypasses/false-positives this replaced:

- **Exactly one statement:** the query is parsed and rejected if it is not a single T-SQL statement — stacked statements are refused as a class, not by recognising each one's keyword
- **Allowlist by AST node, not keyword:** only a `SELECT`/`WITH` root, or a top-level `UNION`/`INTERSECT`/`EXCEPT`, is permitted; `INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, `MERGE`, `TRUNCATE`, `GRANT`, `REVOKE`, `EXEC`/`EXECUTE`, `SELECT ... INTO`, and `xp_*`/`sp_*`/`OPENROWSET`/`OPENQUERY`/`OPENDATASOURCE` are refused by node type or function name, wherever they appear in the tree
- **Table allowlist, strictly enforced:** every table reference must resolve to `schema_data/columns.py::TABLE_COLUMNS` (case-insensitively, schema/db qualifier ignored) or be a CTE defined earlier in the same query — an unresolvable table (hallucinated, out-of-domain, or malicious) is refused outright, independent of whether the DB login is itself scoped to just these tables (see `docs/db-hardening.md`)
- **Column allowlist, deliberately lenient:** every resolvable qualified column reference is checked against its table's known columns; an unqualified column, or one qualified by a CTE name or derived-table alias, is allowed rather than risk a false-positive rejection — this leniency applies to *columns* only, not table names
- **Column-level ACL seam:** `validate_sql(sql, denied_columns=...)` refuses any query touching a named column, regardless of table — the foundation for future multi-tenant column policies; `*`/`alias.*` cannot be used to read around an active policy (it is expanded against its resolved table(s) and checked, or refused outright if it can't be resolved with confidence)
- **No SQL comments:** any comment is refused outright because it is present — its content is never inspected for keywords, since scanning comment text would repeat the same substring-matching mistake this module was rewritten to fix, just in a new place
- **Injection guard:** `INFORMATION_SCHEMA` / `SYS.` access blocked by AST node, not substring
- **LIMIT→TOP:** SQL Server-incompatible `LIMIT n` is rewritten to `TOP n` before execution
- **Row cap:** `MAX_ROWS_RETURNED` is enforced as a hard ceiling on every result set, and `database/executor.py` streams results rather than materialising the whole set client-side
- **Defense in depth at the database layer:** `database/executor.py` runs every query inside a transaction that is always rolled back (never committed), with both a driver-level query timeout and `SET LOCK_TIMEOUT`; `docs/db-hardening.md` specifies the server-side login/DENY/Resource Governor hardening for the DBA to apply on top of this
- **No hardcoded credentials:** all secrets via environment variables only

### Authentication (Phase 8)

Every route except `GET /health` requires a named API key, sent as
`Authorization: Bearer <key>`. `X-API-Key` and every other transport are
deliberately not supported — one way in is one thing to reason about.

- **Named API keys, not JWT/OIDC:** this is an on-prem tool with no IdP
  dependency; what auth actually needs to provide is a principal identity to
  key the cache on, own a session, and name in the audit trail. See
  `docs/api-contract-v2.md`'s authentication section for the full rationale.
- **Never store raw keys:** `API_KEYS_JSON` holds only each key's SHA-256 hex
  digest (`security/auth.py`). Issue a new key with `python
  scripts/issue_api_key.py --id <id> --name <name>` — it prints the raw key
  **once**, never to a file or log.
- **Fail closed:** with `AUTH_REQUIRED=true` (the default) and no keys
  configured, the server refuses to start rather than run with a front door
  nobody can open. `AUTH_REQUIRED=false` is a deliberate escape hatch that
  logs a `WARNING` on every startup, not just the first.
- **Cache isolation without losing cache sharing:** the query cache
  partitions on a hash of each principal's `denied_columns` (`security.auth.scope_key`),
  not on principal id directly — two principals with identical data
  visibility still share entries (preserving today's hit rates), while two
  with different visibility can never collide.
- **Column-level ACL:** a key's `denied_columns` feeds straight into
  `security/sql_guard.py`'s existing `denied_columns` seam — no new
  enforcement machinery, just the first thing that populates it.
- **Sessions are owned:** a `/v2/sessions` session belongs to the principal
  that created it; a non-owner gets `404`, never `403` — a `403` would itself
  confirm the session exists to a caller who has no business knowing that.
- **Rate limiting keys on principal, not just IP:** behind a shared proxy,
  IP-only bucketing would put the whole organisation in one bucket; an
  authenticated caller gets their own.
- **`/docs` / `/redoc` / `/openapi.json` require auth too** (`APP_DOCS_PUBLIC=false`
  by default) — the generated API documentation describes exactly what an
  authenticated caller can do to production data.

---

## License

**Business Source License 1.1 (BUSL-1.1)** — see [`LICENSE`](LICENSE).

- ✅ Free for non-production, research, and personal use
- ❌ Commercial/production use requires a written agreement with the author
- 🔄 Converts to **Apache 2.0** on **2029-01-01**
- 📌 Derivative works must retain [`LICENSE`](LICENSE) and include:
  > *Based on Local SQL Agent by Ali Sadeghi Aghili — https://github.com/alisadeghiaghili/local-sql-agent*

Read the terms carefully rather than assuming either extreme: BUSL-1.1 is
neither all-rights-reserved nor open source. **Copying, modifying and
redistributing are permitted.** What is not permitted without a written
agreement is **production use of any kind** — including internal production
use inside a company. Deploying this to serve real users or real business
data is production use whether or not money changes hands.

### Where the terms are stated

| File | Audience |
|---|---|
| [`LICENSE`](LICENSE) | The terms themselves |
| [`NOTICE`](NOTICE) | Attribution block a derivative work must carry |
| [`AGENTS.md`](AGENTS.md) | AI coding assistants and agents reading this repo |
| [`llms.txt`](llms.txt) | Crawlers and training pipelines |
| `SPDX-License-Identifier` header | Every `.py` file — travels with a single copied file |
| `core/provenance.py` | The start-up banner, logged on every run |

`tests/test_license_headers.py` fails if a new source file lands without the
header, or if any of those files is deleted. `tests/test_provenance_notice.py`
fails if the start-up notice stops being emitted — an unchecked notice is one
that quietly disappears.

The banner is a log line, not a licence check: it does not refuse to start,
degrade, or phone home when files are missing. A kill switch keyed on a
file's presence is a production outage waiting for the first container build
that excludes `*.md`, and it would land on whoever is on call rather than on
an infringer.

---

## Contributors

### [Ali Sadeghi Aghili](https://github.com/alisadeghiaghili) — System Architecture & Engineering

**Role:** Creator & Lead Engineer

| Area | Modules |
|---|---|
| **Orchestration & CLI** | `app.py` — main REPL loop: NLQ input → SQL generation → execution → Excel export → structured logging |
| **Configuration** | `config.py` — typed `Settings` singleton, env-based overrides, `override_settings()` test helper |
| **Core layer** | `core/models.py` — `RetrievalContext`, `SQLGenerationResult`, `QueryLogRecord` frozen dataclasses |
| **LLM integration** | `llm/sql_agent.py`, `llm/wizard_llm.py` — generate / clean / auto-correct loop with exponential retry and back-off |
| **Retrieval pipeline** | `retrieval/context_retriever.py` — orchestrates all six sub-retrievers into a single `RetrievalContext`; all six sub-retriever modules |
| **Schema layer** | `schema_data/` — table definitions, column allowlist, FK relationship map, `SchemaRegistry`, TF-IDF bigram fallback retriever |
| **Prompt engineering** | `prompt_engine/builder.py`, `prompt_engine/templates.py` — dynamic, context-aware prompt assembly |
| **Validation & security** | `security/sql_guard.py`, `validation/` — multi-layer, sqlglot-AST-based guard: single-statement + SELECT-only enforcement, forbidden node/function checks, schema table/column allowlist |
| **Database** | `database/connection.py`, `database/executor.py` — SQLAlchemy singleton engine, query timeout, hard row cap |
| **FastAPI service** | `api/` — `/query`, `/health`, `/cache` endpoints; `RequestLoggingMiddleware`; LRU + TTL `QueryCache`; typed `NLQError` hierarchy |
| **Exports & logging** | `exporters/`, `app_logging/` — Excel/CSV/JSON exporters; rotating JSONL logger |
| **Test suite** | `tests/` — 470+ unit and integration tests; GitHub Actions CI |

---

### [Melika Bahmanabadi](https://github.com/MelikaBahmanabadi) — Domain Knowledge & Business Intelligence

**Role:** Domain Expert & Knowledge Engineer

| Area | Modules |
|---|---|
| **Trading hall aliases** | `knowledge/aliases.py` — `RING_ALIASES`: Persian alias map for all 11 trading halls with colloquial and formal variants |
| **Business metrics** | `knowledge/metrics.py` — 35+ named metrics with Persian aliases and SQL aggregate expressions |
| **Few-shot examples** | `knowledge/examples.py` — 22 annotated NLQ→SQL pairs with semantic tags for few-shot prompt injection |
| **Business rules** | `knowledge/business_rules.py` — domain rules injected verbatim into prompts by `RuleRetriever` |
| **Entity catalog** | `knowledge/entities.py` — dimension entity definitions mapping Persian/English concepts to database tables |

---

Contributions welcome — open an issue before submitting a PR.

---

Built with an OpenAI-compatible LLM endpoint (vLLM / LM Studio / Ollama `/v1`) · [FastAPI](https://fastapi.tiangolo.com) · [SQLAlchemy](https://sqlalchemy.org) · [scikit-learn](https://scikit-learn.org)
