# Auction NLQ Engine

A **local**, **offline** Natural Language to SQL engine for the **Auction Data Mart** (`Auction_DM`).
Ask questions in Persian or English — get SQL Server queries and Excel exports.

> **No cloud. No API keys. Runs entirely on your own infrastructure.**

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [Database Schema](#database-schema)
- [Security](#security)
- [Logging](#logging)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## Features

- **Bilingual** — accepts questions in Persian (Farsi) and English
- **Local LLM** — powered by [Ollama](https://ollama.com); no data leaves your network
- **SQL Server** — generates T-SQL with bracket notation, TOP, CTEs, ROW_NUMBER()
- **Smart schema injection** — keyword + bigram retriever selects only relevant tables per question
- **SQL guard** — blocks all DML/DDL, system catalogues, and LIMIT clauses before execution
- **Excel export** — every result saved to a timestamped `.xlsx` with auto-fitted columns
- **Structured logging** — every query logged to `logs/query_log.jsonl` (JSONL format)
- **Zero dependencies beyond pip** — no vector DB, no embeddings server

---

## Architecture

```
User Question
     │
     ▼
┌─────────────────────────────────────────┐
│              app.py  (REPL)             │
└──────────────┬──────────────────────────┘
               │
     ┌─────────▼──────────┐
     │  llm/ollama_client │  ← Retry + back-off
     └─────────┬──────────┘
               │  build prompt
     ┌─────────▼──────────────────────┐
     │  schema/retriever              │  ← Keyword + bigram match
     │  schema/schema_registry        │  ← Cached context builder
     │  prompts/system_prompt.md      │
     └─────────┬──────────────────────┘
               │  raw LLM response
     ┌─────────▼──────────┐
     │ security/sql_guard │  ← clean_sql() + validate_sql()
     └─────────┬──────────┘
               │  safe SQL
     ┌─────────▼──────────┐
     │ database/executor  │  ← SQLAlchemy + LOCK_TIMEOUT
     └─────────┬──────────┘
               │  DataFrame
     ┌─────────▼──────────┐   ┌──────────────────┐
     │ exporters/excel    │   │ logs/logger       │
     └────────────────────┘   └──────────────────┘
```

---

## Project Structure

```
local-sql-agent/
├── app.py                    # Entry point — interactive REPL
├── config.py                 # Settings dataclass (env-var driven)
├── requirements.txt          # Python dependencies
├── .env.example              # Environment variable template
├── .gitignore
├── README.md
├── CHANGELOG.md
│
├── database/
│   ├── connection.py         # SQLAlchemy engine factory (cached)
│   └── executor.py           # SQL execution + row-cap + error wrapping
│
├── exporters/
│   └── excel_exporter.py     # DataFrame → timestamped .xlsx
│
├── llm/
│   └── ollama_client.py      # Ollama HTTP client (retry + back-off)
│
├── logs/
│   ├── logger.py             # Append to query_log.jsonl
│   └── query_log.py          # QueryLog dataclass
│
├── prompts/
│   ├── system_prompt.md      # Core LLM instructions + domain rules
│   ├── business_glossary.md  # Business entity definitions + aliases
│   └── few_shots.md          # Example Q→SQL pairs
│
├── schema/
│   ├── tables.py             # Table registry (logical name → SQL table)
│   ├── table_schemas.py      # Column definitions per table
│   ├── relationships.py      # FK relationships injected into prompt
│   ├── business_rules.py     # Domain-specific SQL rules
│   ├── retriever.py          # Keyword + bigram table selector
│   └── schema_registry.py    # Builds + caches schema context string
│
├── security/
│   └── sql_guard.py          # clean_sql() + validate_sql() + ensure_top()
│
├── scripts/
│   └── create_db.py          # Generate sample.db for local testing
│
└── tests/
    ├── test_config.py        # Settings dataclass tests
    └── test_sql_guard.py     # SQL guard tests (clean, validate, ensure_top)
```

---

## Quick Start

### Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Tested on 3.11 and 3.12 |
| [Ollama](https://ollama.com) | Running locally (`ollama serve`) |
| ODBC Driver 17 for SQL Server | [Microsoft download](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server) |
| Access to `Auction_DM` SQL Server database | |

### Installation

```bash
# 1. Clone
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your actual values

# 5. Pull the LLM model
ollama pull gpt-oss:20b             # or whichever model you use

# 6. Run
python app.py
```

---

## Configuration

All settings are controlled via environment variables (or a `.env` file).
**Never commit `.env` to version control.**

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434/api/generate` | Ollama API endpoint |
| `OLLAMA_MODEL` | `gpt-oss:20b` | Model tag to use |
| `DB_CONNECTION_URL` | `mssql+pyodbc://sa@localhost/Auction_DM?...` | Full SQLAlchemy connection string |
| `QUERY_TIMEOUT_SECONDS` | `60` | SQL Server lock timeout |
| `MAX_ROWS_RETURNED` | `1000` | Maximum rows fetched per query |
| `LOG_DIR` | `logs` | Directory for `query_log.jsonl` |
| `EXPORT_DIR` | `exports` | Directory for Excel exports |

### Connection String Examples

**SQL Authentication:**
```
mssql+pyodbc://username:password@server:1433/Auction_DM?driver=ODBC+Driver+17+for+SQL+Server
```

**Windows Authentication:**
```
mssql+pyodbc://@server/Auction_DM?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
```

---

## How It Works

### 1. Table Retrieval

The `schema/retriever.py` module scores each of the 29 registered tables by matching **unigrams and bigrams** from the question against each table's Persian/English description. Only the top-6 most relevant tables are included in the prompt — keeping token count low and accuracy high.

### 2. Prompt Construction

For each query, the prompt is assembled from:
- `prompts/system_prompt.md` — SQL Server rules, domain restrictions, OUT_OF_SCOPE sentinel
- `schema/business_rules.py` — domain-specific SQL patterns (latest period, ranking, etc.)
- Selected table schemas and FK relationships
- The user's question

### 3. SQL Cleaning & Validation

The raw LLM output passes through `security/sql_guard.py`:

1. **`clean_sql()`** — strips markdown fences, drops prose preamble, converts `LIMIT n` → `TOP n`, fixes `SELECT TOP n DISTINCT` → `SELECT DISTINCT TOP n`
2. **`validate_sql()`** — rejects DELETE / UPDATE / INSERT / DROP / ALTER / TRUNCATE / MERGE / EXEC / XP_ / SP_ / INFORMATION_SCHEMA / SYS. / LIMIT

### 4. Execution

`database/executor.py` runs the validated SQL via SQLAlchemy with:
- `SET LOCK_TIMEOUT` to prevent long blocking
- Row cap at `MAX_ROWS_RETURNED`
- All database errors wrapped in `RuntimeError`

### 5. Export & Logging

- Results saved to `exports/result_YYYYMMDD_HHMMSS.xlsx` with auto-fitted column widths
- Every query (success, error, out-of-scope) appended to `logs/query_log.jsonl`

---

## Database Schema

The engine covers **29 tables** across 3 schemas:

| Schema | Tables |
|---|---|
| `Auction_Fact` | `Contract`, `CustomerContract`, `Offer`, `Order`, `TalarLog` |
| `Auction_Dim` | `Customer`, `Broker`, `Supplier`, `Ring`, `Symbol`, `Bank`, `Carrier`, `ContractKind`, `ContractStatus`, `Currency`, `DeliveryPlace`, `OfferStatus`, `OfferKind`, `PaymentDelivery`, `ClearingKind`, `GeneralStatus`, `HallMatchingDeliveryKind`, `OfferItemStatus`, `BuyMethod`, `ActionType`, `Packet`, `TempCustomer`, `TradeCreditTypes` |
| `General_Dim` | `Date` |

### Key Relationships

```
CustomerContract.BuyerCustomer_ID  →  Customer.ID
CustomerContract.Contract_ID       →  Contract.ID
CustomerContract.Symbol_ID         →  Symbol.ID
CustomerContract.Date_ID           →  Date.ID
Contract.Ring_ID                   →  Ring.ID
Offer.Supplier_ID                  →  Supplier.ID
Order.BuyerBroker_ID               →  Broker.ID
```

---

## Security

- **No credentials in code** — all connection details live in `.env` (gitignored)
- **Read-only enforcement** — `validate_sql()` blocks all write operations at the application layer
- **System catalogue protection** — queries against `INFORMATION_SCHEMA` and `SYS.` are rejected
- **Injection mitigation** — forbidden keyword scan on every query before execution

---

## Logging

Every query is appended to `logs/query_log.jsonl` as a single JSON line:

```json
{
  "timestamp": "2026-06-06T14:30:00.123456",
  "question": "top 5 customers by purchase value",
  "generated_sql": "SELECT TOP 5 c.Name, SUM(cc.TotalPrice) ...",
  "model_name": "gpt-oss:20b",
  "status": "SUCCESS",
  "execution_time_seconds": 2.341,
  "row_count": 5,
  "excel_file": "/path/to/exports/result_20260606_143000.xlsx",
  "error_message": null
}
```

**Status values:** `SUCCESS` | `ERROR` | `OUT_OF_SCOPE`

---

## Testing

```bash
pip install pytest
pytest tests/ -v
```

| Test file | What it covers |
|---|---|
| `tests/test_config.py` | `Settings` dataclass — validation, defaults, placeholder detection |
| `tests/test_sql_guard.py` | `clean_sql()`, `validate_sql()`, `ensure_top()` — 20 test cases |

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `Ollama unreachable` | Ensure `ollama serve` is running; check `OLLAMA_URL` |
| `Database error: ...` | Verify `DB_CONNECTION_URL`, ODBC driver version, and network access |
| `OUT_OF_SCOPE` response | Question is outside Auction DB domain — rephrase using auction terms |
| `Forbidden keyword` | Model generated a write query — try rephrasing or check system prompt |
| Empty results | Data may not exist for the requested period; try without date filters |
| Excel not saved | Check `EXPORT_DIR` has write permission |
