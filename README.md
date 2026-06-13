# Local SQL Agent

> Ask your SQL Server database a question in Persian or English.  
> Get back precise T-SQL — generated locally, executed securely, zero data leaves your machine.

[![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/Tests-427%2B-green)](tests/)

---

## Why this exists

Most Text-to-SQL tools assume your data lives in the cloud and that your questions are in English. This project was built for the opposite reality: an on-premise SQL Server data warehouse at the **Iran Mercantile Exchange (IME)**, where analysts ask questions in Persian, data is sensitive, and there is no budget for external API calls.

The result is a fully local NLQ engine with a modular retrieval pipeline, a SQL security guard, a FastAPI service layer, and a knowledge base you can extend without touching engine code.

---

## What it looks like

```bash
# CLI mode
python app.py

Question: برترین مشتریان از نظر ارزش خرید در سال 1402 کدامند؟

══════════════════════════════════════════════════
GENERATED SQL
══════════════════════════════════════════════════
SELECT TOP 10
    c.Name,
    SUM(cc.TotalPrice) AS PurchaseValue
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Customer] c
    ON cc.BuyerCustomer_ID = c.ID
JOIN [Auction_Dim].[Date] d
    ON cc.Date_ID = d.ID
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
  "sql": "SELECT TOP 1000 d.PersianMonthName, SUM(c.TotalPrice) AS TradeValue ...",
  "result": [{"PersianMonthName": "فروردین", "TradeValue": 48320000000}, ...],
  "row_count": 12,
  "status": "SUCCESS"
}
```

---

## How it works

Every question passes through a six-stage retrieval pipeline before the LLM ever sees it:

```
Question (Persian / English)
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  ContextRetriever                                       │
│  ├─ EntityRetriever      alias match → TF-IDF fallback  │
│  ├─ FactRetriever        keyword match → TF-IDF fallback│
│  ├─ RelationshipRetriever  JOIN clauses for found tables │
│  ├─ RuleRetriever          domain business rules        │
│  ├─ ExampleRetriever       tag-scored few-shot examples  │
│  └─ ValueRetriever         ring name + Persian year     │
└─────────────────────────────────────────────────────────┘
    │
    ▼
 PromptBuilder  →  structured prompt with schema + rules + examples
    │
    ▼
 SQLAgent  →  generate → clean → validate → auto-correct (up to N retries)
    │
    ▼
 SQLGuard  →  blocks DDL/DML, injection, converts LIMIT→TOP
    │
    ▼
 SQL Server  →  result set  →  Excel / CSV / JSON
```

The LLM receives a precise, scoped prompt — not a dump of your entire schema. This is what makes locally-run 8B–20B models accurate enough for production use.

---

## Features

| | Feature | Detail |
|---|---|---|
| 🔒 | **100% local** | Ollama + SQL Server on-premise. Zero external API calls. |
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
| 🧪 | **Test suite** | 427+ unit + integration tests; GitHub Actions CI. |

---

## Quick start

**Requirements:** Python 3.11+, [Ollama](https://ollama.com), SQL Server + ODBC Driver 17

```bash
# 1. Clone and install
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — minimum required:
#   DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/YourDB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
#   OLLAMA_MODEL=llama3

# 3a. Run as CLI
python app.py

# 3b. Or run as HTTP API
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

For a complete walkthrough — installation, first query, extending the domain, writing tests, diagnosing misses — see the **[tutorial](docs/tutorial.md)**.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434/api/generate` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3` | Model name (`llama3`, `mistral`, `codellama`, …) |
| `DB_CONNECTION_URL` | *(required)* | SQLAlchemy connection string |
| `QUERY_TIMEOUT_SECONDS` | `60` | Max query execution time (seconds) |
| `MAX_ROWS_RETURNED` | `1000` | Hard row cap applied to all queries |
| `CACHE_TTL_SECONDS` | `300` | Query cache TTL in seconds (`0` = disabled) |
| `CACHE_MAX_SIZE` | `256` | Maximum number of cached query results |
| `LOG_DIR` | `logs` | Log file directory (auto-created) |
| `EXPORT_DIR` | `exports` | Export file directory (auto-created) |

---

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/query` | Run a natural-language query |
| `GET` | `/health` | DB + Ollama reachability probe |
| `GET` | `/cache/stats` | Cache size, hits, misses, evictions |
| `POST` | `/cache/invalidate` | Remove a specific cached entry |
| `POST` | `/cache/clear` | Flush the entire cache |

**Error codes:**

| Exception | HTTP | When |
|---|---|---|
| `OutOfScopeError` | 422 | Question outside the domain |
| `ModelTimeoutError` | 504 | Ollama request timed out |
| `ModelUnavailableError` | 503 | Ollama unreachable after retries |
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

See the **[tutorial](docs/tutorial.md)** for the full step-by-step workflow.

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
│   ├── middleware.py         #   correlation ID + latency headers
│   └── health.py             #   /health — DB + Ollama probes
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
│   ├── ollama_backend.py     #   HTTP client + exponential back-off
│   └── base.py               #   LLMBackend ABC
├── security/
│   └── sql_guard.py          #   clean_sql / validate_sql / ensure_top
├── database/
│   ├── connection.py         #   SQLAlchemy engine singleton
│   └── executor.py           #   timeout + row cap enforcement
├── exporters/                # Excel / CSV / JSON exporters
├── scripts/
│   └── analyze_misses.py     #   offline retrieval miss diagnostics
├── docs/
│   └── tutorial.md           #   full bilingual tutorial (EN + FA)
└── tests/                    # 427+ unit + integration tests
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

Every generated SQL query passes through `security/sql_guard.py` before execution:

- **Allowlist-only:** only `SELECT` and `WITH` are permitted as statement openers
- **Blocklist:** `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `DELETE`, `UPDATE`, `INSERT`, `MERGE`, `EXECUTE`, `XP_`, `SP_` are rejected
- **Injection guard:** stacked queries and `INFORMATION_SCHEMA` / `SYS.` access blocked
- **LIMIT→TOP:** SQL Server–incompatible `LIMIT n` is rewritten to `TOP n` before execution
- **Row cap:** `MAX_ROWS_RETURNED` is enforced as a hard ceiling on every result set
- **No hardcoded credentials:** all secrets via environment variables only

---

## License

**Business Source License 1.1 (BUSL-1.1)** — see [`LICENSE`](LICENSE).

- ✅ Free for non-production, research, and personal use
- ❌ Commercial/production use requires a written agreement with the author
- 🔄 Converts to **Apache 2.0** on **2029-01-01**
- 📌 Derivative works must retain [`LICENSE`](LICENSE) and include:
  > *Based on Local SQL Agent by Ali Sadeghi Aghili — https://github.com/alisadeghiaghili/local-sql-agent*

---

## Contributors

### [Ali Sadeghi Aghili](https://github.com/alisadeghiaghili) — System Architecture & Engineering

**Role:** Creator & Lead Engineer

| Area | Modules |
|---|---|
| **Orchestration & CLI** | `app.py` — main REPL loop: NLQ input → SQL generation → execution → Excel export → structured logging |
| **Configuration** | `config.py` — typed `Settings` singleton, env-based overrides, `override_settings()` test helper |
| **Core layer** | `core/models.py` — `RetrievalContext`, `SQLGenerationResult`, `QueryLogRecord` frozen dataclasses |
| **LLM integration** | `llm/sql_agent.py`, `llm/ollama_backend.py` — generate / clean / auto-correct loop with exponential retry and back-off |
| **Retrieval pipeline** | `retrieval/context_retriever.py` — orchestrates all six sub-retrievers into a single `RetrievalContext`; all six sub-retriever modules |
| **Schema layer** | `schema_data/` — table definitions, column allowlist, FK relationship map, `SchemaRegistry`, TF-IDF bigram fallback retriever |
| **Prompt engineering** | `prompt_engine/builder.py`, `prompt_engine/templates.py` — dynamic, context-aware prompt assembly |
| **Validation & security** | `security/sql_guard.py`, `validation/` — multi-layer guard: SELECT-only enforcement, forbidden keyword blocklist, schema allowlist |
| **Database** | `database/connection.py`, `database/executor.py` — SQLAlchemy singleton engine, query timeout, hard row cap |
| **FastAPI service** | `api/` — `/query`, `/health`, `/cache` endpoints; `RequestLoggingMiddleware`; LRU + TTL `QueryCache`; typed `NLQError` hierarchy |
| **Exports & logging** | `exporters/`, `app_logging/` — Excel/CSV/JSON exporters; rotating JSONL logger |
| **Test suite** | `tests/` — 427+ unit and integration tests; GitHub Actions CI |

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

## Acknowledgements

Built with [Ollama](https://ollama.com), [FastAPI](https://fastapi.tiangolo.com), [SQLAlchemy](https://sqlalchemy.org), and [scikit-learn](https://scikit-learn.org).
