# Local SQL Agent

> **A production-grade, privacy-first Text-to-SQL engine for Persian-language business intelligence.**
> Runs entirely on your infrastructure — no cloud, no data leakage.

---

## Overview

Local SQL Agent converts natural language questions (Persian or English) into precise T-SQL queries against your SQL Server data warehouse. It is purpose-built for commodity exchanges and capital markets, but the architecture is domain-agnostic.

```
User question (Persian / English)
    ↓
 ContextRetriever
    ├─ EntityRetriever       → dimension tables  (alias match → TF-IDF fallback)
    ├─ FactRetriever         → fact tables       (keyword match → TF-IDF fallback)
    ├─ RelationshipRetriever → JOIN clauses for selected tables
    ├─ RuleRetriever         → domain business rules (injected verbatim)
    ├─ ExampleRetriever      → few-shot SQL examples (tag-overlap scoring)
    └─ ValueRetriever        → concrete filters (ring canonical name, Persian year)
    ↓
 PromptBuilder  →  structured, context-aware prompt
    ↓
 SQLAgent (llm/sql_agent.py)  →  generate + clean + auto-correct loop
    ↓
 OllamaBackend (llm/ollama_backend.py)  →  HTTP call with retry/back-off
    ↓
 SQLGuard  →  sanitised, safe SQL
    ↓
 SQL Server  →  result set  →  export (Excel / CSV / JSON)
```

The engine is also exposed as a **FastAPI HTTP service** (`api/server.py`) with LRU query caching, typed error responses, and request-scoped logging middleware.

---

## Key Features

| Feature | Detail |
|---|---|
| **100% local** | Ollama + SQL Server on-premise. Zero external API calls. |
| **Bilingual** | Persian (Farsi) and English questions handled natively. |
| **Modular retrieval** | 6 independent retrievers — easy to extend per domain. |
| **Two-tier retrieval** | Fast alias/pattern matching first; TF-IDF bigram engine as fallback. |
| **Few-shot learning** | Tag-scored example selector injects the most relevant SQL patterns. |
| **Business rule injection** | Domain rules injected per question topic at prompt-build time. |
| **SQL security guard** | Blocks DDL, DML, injection patterns, and converts LIMIT→TOP. |
| **Auto-correct loop** | SQLAgent retries up to N times with error feedback when SQL is invalid. |
| **FastAPI HTTP API** | REST endpoints for query, cache management, and health check. |
| **LRU query cache** | Thread-safe TTL + LRU cache; configurable size and expiry. |
| **Typed error taxonomy** | `NLQError` hierarchy: `OutOfScopeError`, `ModelTimeoutError`, `ModelUnavailableError`, `QueryExecutionError`. |
| **Retry with back-off** | Automatic exponential retry on Ollama transient failures. |
| **Structured exports** | Excel, CSV, JSON output with timestamped filenames. |
| **Thread-safe logging** | Rotating file logger + request-scoped middleware correlation ID. |
| **Test suite** | 427+ unit + integration tests, CI via GitHub Actions. |

---

## Architecture

```
local-sql-agent/
├── config.py                      # Typed Settings singleton (env-based, frozen) + override_settings()
├── app.py                         # Interactive CLI entry point (REPL)
├── api/                           # FastAPI HTTP service
│   ├── server.py                  # FastAPI app factory + /query, /health, /cache endpoints
│   ├── runner.py                  # run_query() — cache-aware orchestrator, calls SQLAgent
│   ├── query_cache.py             # QueryCache — thread-safe TTL + LRU cache singleton
│   ├── models.py                  # Pydantic request/response models (QueryRequest, QueryResponse)
│   ├── errors.py                  # NLQError hierarchy → FastAPI exception handlers
│   ├── middleware.py              # RequestLoggingMiddleware — correlation ID, latency headers
│   └── health.py                  # /health endpoint — DB + Ollama reachability probes
├── core/
│   └── models.py                  # RetrievalContext — frozen dataclass shared by all layers
├── knowledge/                     # Domain knowledge base (edit to extend the domain)
│   ├── aliases.py                 # RING_ALIASES + SYNONYMS (156 entries, 10 categories)
│   ├── business_rules.py          # Business rules per topic key
│   ├── entities.py                # Dimension entity catalog with Persian/English aliases
│   ├── examples.py                # Tagged few-shot SQL examples (question + sql + tags)
│   └── metrics.py                 # Metric definitions and computed expressions
├── retrieval/                     # Modular retrieval pipeline
│   ├── context_retriever.py       # Orchestrator — runs all sub-retrievers, returns RetrievalContext
│   ├── entity_retriever.py        # Dimension table detection (alias → TF-IDF fallback)
│   ├── fact_retriever.py          # Fact table detection (pattern → TF-IDF fallback)
│   ├── relationship_retriever.py  # JOIN clause selection from schema_data.relationships
│   ├── rule_retriever.py          # Business rule injection by keyword
│   ├── value_retriever.py         # Filter extraction (ring canonical name, Persian year)
│   └── example_retriever.py       # Tag-scored few-shot selection (top-3 by overlap)
├── schema_data/                   # Schema definitions (single source of truth)
│   ├── registry.py                # SchemaRegistry — build_schema_context() + build_context() alias
│   ├── columns.py                 # Column-level schema with FK annotations
│   ├── tables.py                  # Table descriptions (bilingual — used by TF-IDF engine)
│   ├── relationships.py           # FK relationship map (JOIN SQL per edge)
│   └── retriever.py               # TF-IDF bigram retriever — fallback for all sub-retrievers
├── prompt_engine/
│   ├── builder.py                 # PromptBuilder.build() — assembles final structured prompt
│   └── templates.py               # PROMPT_TEMPLATE with labelled sections
├── llm/
│   ├── base.py                    # LLMBackend ABC + SQLGenerationResult dataclass
│   ├── sql_agent.py               # SQLAgent — generate/clean/auto-correct loop
│   ├── ollama_backend.py          # OllamaBackend — HTTP client (retry + back-off)
│   └── ollama_client.py           # Legacy thin client (kept for CLI compatibility)
├── security/
│   └── sql_guard.py               # SQL sanitisation: clean_sql, validate_sql, ensure_top
├── database/
│   ├── connection.py              # SQLAlchemy engine (singleton + dispose helper)
│   └── executor.py                # Query execution with timeout and row cap
├── exporters/                     # Excel / CSV / JSON export modules
├── logs/                          # Rotating log files (auto-created at runtime)
├── scripts/
│   └── analyze_misses.py          # Offline miss-analysis tool for retrieval diagnostics
└── tests/                         # 427+ unit + integration tests
```

---

## Quick Start

### 1. Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally with a supported model
- SQL Server with ODBC Driver 17

### 2. Installation

```bash
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent
pip install -r requirements.txt
```

### 3. Configuration

```bash
cp .env.example .env
# Edit .env with your SQL Server connection and Ollama settings
```

```env
OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=llama3
DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/YourDB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
```

### 4a. Run as CLI

```bash
python app.py
```

### 4b. Run as HTTP API

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

#### Example request

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "فروش ماهانه مشتریان برتر", "mode": "full"}'
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434/api/generate` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3` | Model name (e.g. `llama3`, `mistral`, `codellama`) |
| `DB_CONNECTION_URL` | *(required)* | SQLAlchemy connection string |
| `QUERY_TIMEOUT_SECONDS` | `60` | Max query execution time (seconds) |
| `MAX_ROWS_RETURNED` | `1000` | Hard row cap applied to all queries |
| `CACHE_TTL_SECONDS` | `300` | Query cache TTL in seconds (0 = disabled) |
| `CACHE_MAX_SIZE` | `256` | Maximum number of cached query results |
| `LOG_DIR` | `logs` | Log file directory (auto-created) |
| `EXPORT_DIR` | `exports` | Export file directory (auto-created) |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/query` | Run a natural-language query; returns SQL + result set |
| `GET` | `/health` | DB + Ollama reachability probe |
| `GET` | `/cache/stats` | Current cache size, hits, misses, evictions |
| `POST` | `/cache/invalidate` | Remove a specific (question, mode) entry |
| `POST` | `/cache/clear` | Flush the entire cache |

---

## Error Taxonomy

| Exception | HTTP status | When raised |
|---|---|---|
| `OutOfScopeError` | 422 | Model returns `OUT_OF_SCOPE` sentinel |
| `ModelTimeoutError` | 504 | Ollama request exceeds timeout |
| `ModelUnavailableError` | 503 | Ollama unreachable after all retries |
| `QueryExecutionError` | 500 | SQL Server execution failure |
| `ValidationError` (Pydantic) | 422 | Malformed request body |

---

## Extending the Domain

All domain knowledge lives in `knowledge/`. No code changes required for most extensions.

**Add new dimension entities** — `knowledge/entities.py`:
```python
"NewEntity": {
    "aliases": ["اسم فارسی", "english alias"],
    "table": "NewEntity"
}
```

**Add new few-shot examples** — `knowledge/examples.py`:
```python
{
    "tags": ["customer", "top", "value"],
    "question": "Top customers by purchase value",
    "sql": "SELECT TOP 10 CustomerName, SUM(Value) AS TotalValue FROM ..."
}
```

**Add new business rules** — `knowledge/business_rules.py`:
```python
"new_topic": "Your domain rule injected verbatim into the prompt."
```

**Add new synonym expansions** — `knowledge/aliases.py` → `SYNONYMS` dict.

**Add new tables** — update `schema_data/columns.py`, `schema_data/tables.py`, `schema_data/relationships.py`.

---

## Running Tests

```bash
pytest tests/ -v
```

Tests run automatically on every push via GitHub Actions (Python 3.13, `pytest-cov`).

---

## Security

- All generated SQL passes through `security/sql_guard.py` before execution
- DDL statements (`DROP`, `ALTER`, `CREATE`, `TRUNCATE`) are blocked
- DML statements (`INSERT`, `UPDATE`, `DELETE`) are blocked
- Stacked query injection patterns are detected and rejected
- Dangerous stored procedure calls (`EXECUTE`, `XP_`, `SP_`) are blocked
- `LIMIT` is converted to `TOP n` for SQL Server compatibility (never executed raw)
- `MAX_ROWS_RETURNED` enforces a hard cap on all result sets
- Credentials are **never** hardcoded — environment variables only

---

## License

Proprietary. All rights reserved.
