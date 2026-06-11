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
    ├─ EntityRetriever    (dimension tables via alias matching + TF-IDF fallback)
    ├─ FactRetriever      (fact tables via keyword patterns + TF-IDF fallback)
    ├─ RelationshipRetriever  (relevant JOIN clauses)
    ├─ RuleRetriever      (domain business rules)
    ├─ ExampleRetriever   (tag-scored few-shot examples)
    └─ ValueRetriever     (ring aliases, Persian year extraction)
    ↓
 PromptBuilder  →  structured context-aware prompt
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
| **Few-shot learning** | Tag-scored example selector injects relevant SQL patterns. |
| **Business rule injection** | Domain rules injected per question topic. |
| **SQL security guard** | Blocks DDL, DML, and dangerous patterns before execution. |
| **Retry with back-off** | Automatic retry on Ollama transient failures. |
| **Structured exports** | Excel, CSV, JSON output with timestamped filenames. |
| **Thread-safe logging** | Rotating file logger, configurable via environment. |
| **Test suite** | Unit + integration tests with `override_settings` fixture. |

---

## Architecture

```
local-sql-agent/
├── config.py                  # Settings singleton (env-based, testable)
├── main.py                    # CLI entry point
├── core/
│   └── models.py              # RetrievalContext dataclass
├── knowledge/
│   ├── aliases.py             # Canonical ring/hall aliases
│   ├── business_rules.py      # Domain business rules (per topic)
│   ├── entities.py            # Dimension entity catalog with aliases
│   ├── examples.py            # Tagged few-shot SQL examples
│   └── metrics.py             # Metric definitions and expressions
├── retrieval/
│   ├── context_retriever.py   # Orchestrator — calls all sub-retrievers
│   ├── entity_retriever.py    # Dimension table detection
│   ├── fact_retriever.py      # Fact table detection
│   ├── relationship_retriever.py  # JOIN clause selection
│   ├── rule_retriever.py      # Business rule injection
│   ├── value_retriever.py     # Filter value extraction (ring, year)
│   └── example_retriever.py   # Tag-scored few-shot selection
├── schema_data/
│   ├── registry.py            # SchemaRegistry — builds schema context string
│   ├── columns.py             # Column-level schema with FK annotations
│   ├── tables.py              # Table descriptions
│   └── relationships.py       # JOIN relationship map
├── schema/
│   └── retriever.py           # TF-IDF table retriever (fallback engine)
├── prompt_engine/
│   ├── builder.py             # PromptBuilder — assembles final prompt
│   └── templates.py           # PROMPT_TEMPLATE — structured sections
├── llm/
│   └── ollama_client.py       # Ollama HTTP client with retry logic
├── security/
│   └── sql_guard.py           # SQL sanitisation and injection prevention
├── exporters/             # Excel / CSV / JSON export modules
├── logs/                  # Rotating log files (auto-created)
└── tests/                 # Unit and integration tests
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
python main.py
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434/api/generate` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3` | Model name (e.g. `llama3`, `mistral`, `codellama`) |
| `DB_CONNECTION_URL` | *(required)* | SQLAlchemy connection string |
| `QUERY_TIMEOUT_SECONDS` | `60` | Max query execution time |
| `MAX_ROWS_RETURNED` | `1000` | Row cap for all queries |
| `LOG_DIR` | `logs` | Log file directory |
| `EXPORT_DIR` | `exports` | Export file directory |

---

## Extending the Domain

**Add new entities** — edit `knowledge/entities.py`:
```python
"NewEntity": {
    "aliases": ["اسم فارسی", "english alias"],
    "table": "NewEntity"
}
```

**Add new examples** — edit `knowledge/examples.py`:
```python
{
    "tags": ["customer", "top", "value"],
    "question": "Top customers by value",
    "sql": "SELECT TOP 10 ..."
}
```

**Add new business rules** — edit `knowledge/business_rules.py`:
```python
"new_topic": "Your rule text here"
```

**Add new tables to schema** — edit `schema_data/columns.py`, `schema_data/tables.py`, `schema_data/relationships.py`.

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Security

- All generated SQL passes through `security/sql_guard.py` before execution
- DDL statements (`DROP`, `ALTER`, `CREATE`, `TRUNCATE`) are blocked
- DML statements (`INSERT`, `UPDATE`, `DELETE`) are blocked  
- Stacked query injection patterns are detected and rejected
- `MAX_ROWS_RETURNED` enforces a hard cap on all result sets
- Credentials are **never** hardcoded — environment variables only

---

## License

Proprietary. All rights reserved.
