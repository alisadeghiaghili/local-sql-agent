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
 Ollama (local LLM)  →  raw SQL
    ↓
 SQLGuard  →  sanitised, safe SQL
    ↓
 SQL Server  →  result set  →  export (Excel / CSV / JSON)
```

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
| **Retry with back-off** | Automatic exponential retry on Ollama transient failures. |
| **Structured exports** | Excel, CSV, JSON output with timestamped filenames. |
| **Thread-safe logging** | Rotating file logger, configurable via environment variables. |
| **Test suite** | 130 unit + integration tests, CI via GitHub Actions. |

---

## Architecture

```
local-sql-agent/
├── config.py                      # Typed Settings singleton (env-based, frozen)
├── app.py                         # Interactive CLI entry point (REPL)
├── core/
│   └── models.py                  # RetrievalContext — frozen dataclass shared by all layers
├── knowledge/                     # Domain knowledge base (edit to extend the domain)
│   ├── aliases.py                 # RING_ALIASES (hall → surface forms) + SYNONYMS (TF-IDF expansion)
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
│   ├── registry.py                # SchemaRegistry.build_schema_context() — LRU-cached
│   ├── columns.py                 # Column-level schema with FK annotations
│   ├── tables.py                  # Table descriptions (used by TF-IDF engine)
│   ├── relationships.py           # FK relationship map (JOIN SQL per edge)
│   └── retriever.py               # TF-IDF bigram retriever — fallback for all sub-retrievers
├── prompt_engine/
│   ├── builder.py                 # PromptBuilder.build() — assembles final structured prompt
│   └── templates.py               # PROMPT_TEMPLATE with labelled sections
├── llm/
│   └── ollama_client.py           # Ollama HTTP client (retry + back-off, calls ContextRetriever)
├── security/
│   └── sql_guard.py               # SQL sanitisation: clean_sql, validate_sql, ensure_top
├── database/
│   ├── connection.py              # SQLAlchemy engine (singleton + dispose helper)
│   └── executor.py                # Query execution with timeout and row cap
├── exporters/                     # Excel / CSV / JSON export modules
├── logs/                          # Rotating log files (auto-created at runtime)
├── scripts/
│   └── analyze_misses.py          # Offline miss-analysis tool for retrieval diagnostics
└── tests/                         # 130 unit + integration tests
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

### 4. Run

```bash
python app.py
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
| `LOG_DIR` | `logs` | Log file directory (auto-created) |
| `EXPORT_DIR` | `exports` | Export file directory (auto-created) |

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
