# Auction NLQ Engine

Natural Language to SQL (NLQ) engine for Microsoft SQL Server, powered by Ollama.

Converts natural language questions (Persian or English) into SQL Server queries, executes them safely, and exports results to Excel automatically.

---

## Features

- Natural Language → SQL Server Query
- Microsoft SQL Server support via SQLAlchemy + pyodbc
- Ollama integration (any local model: `gpt-oss:20b`, `gemma3:12b`, …)
- SQL validation and protection layer (blocks DELETE, DROP, UPDATE, …)
- Schema-aware prompting (only relevant tables injected per question)
- Few-shot prompting with domain examples
- Persian business glossary + Ring name aliases
- Automatic Excel export to `exports/`
- Structured JSONL audit log in `logs/`
- Support for Persian and English questions
- OUT_OF_SCOPE detection for off-topic questions

---

## Project Structure

```text
.
├── app.py                         # Entry point — interactive REPL
├── config.py                      # All config read from environment / .env
├── requirements.txt
├── .env.example                   # Copy to .env and fill in values
├── .gitignore
├── database/
│   ├── connection.py              # SQLAlchemy engine factory (LRU-cached)
│   └── executor.py                # execute_sql() → pandas DataFrame
├── exporters/
│   └── excel_exporter.py          # export_excel() → exports/result_*.xlsx
├── llm/
│   └── ollama_client.py           # generate_sql() with schema injection + retry
├── logs/
│   ├── logger.py                  # save_log() → logs/query_log.jsonl
│   └── query_log.py               # QueryLog dataclass
├── prompts/
│   ├── system_prompt.md           # Core LLM instruction set
│   ├── business_glossary.md       # Domain terminology + query rules
│   └── few_shots.md               # Example Q&A pairs for in-context learning
├── schema/
│   ├── tables.py                  # TABLES registry with Persian/English descriptions
│   ├── table_schemas.py           # TABLE_SCHEMAS: column lists per table
│   ├── relationships.py           # FK relationships injected into prompt
│   ├── business_rules.py          # Domain rules injected into schema context
│   ├── retriever.py               # Keyword-based table selector (top-6)
│   └── schema_registry.py         # build_schema_context() → assembled prompt block
└── security/
    └── sql_guard.py               # validate_sql() — blocks destructive queries
```

---

## Prerequisites

### 1. Python 3.9+

### 2. Ollama

Install from [https://ollama.com](https://ollama.com), then pull your model:

```bash
ollama pull gpt-oss:20b
# or
ollama pull gemma3:12b
```

### 3. ODBC Driver for SQL Server

Download from Microsoft:
[https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)

Default driver used: `ODBC Driver 17 for SQL Server`.

---

## Installation

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Configuration

```bash
cp .env.example .env
```

Edit `.env`:

```ini
OLLAMA_URL=http://your-ollama-server/api/generate
OLLAMA_MODEL=gpt-oss:20b

DB_CONNECTION_URL=mssql+pyodbc://username@server:1433/Auction_DM?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes

QUERY_TIMEOUT_SECONDS=60
MAX_ROWS_RETURNED=1000
```

> **Windows Auth:** Set `trusted_connection=yes` in `DB_CONNECTION_URL` and omit the password.

---

## Usage

```bash
python app.py
```

Example questions (Persian):

```
تعداد قراردادها چقدر است؟
بیشترین فروش مربوط به کدام رینگ است؟
۱۰ مشتری برتر از نظر ارزش خرید
فروش به تفکیک سال شمسی
```

Example questions (English):

```
How many contracts exist?
Which ring has the highest sales?
Top 10 customers by purchase value
Show total sales by Persian year
```

---

## How It Works

```
Question
  └─► retriever.py        →  selects top-6 relevant tables (keyword match)
       └─► schema_registry.py →  assembles: BUSINESS_RULES + SCHEMA + RELATIONSHIPS
            └─► ollama_client.py  →  sends: system_prompt + schema + question → Ollama
                 └─► sql_guard.py    →  validates: SELECT-only, no forbidden keywords
                      └─► executor.py   →  runs SQL on SQL Server → pandas DataFrame
                           └─► excel_exporter.py → exports/result_*.xlsx
                                └─► logger.py    → logs/query_log.jsonl
```

---

## Output

Every successful query produces:
- Console output with the generated SQL and result rows
- `exports/result_YYYYMMDD_HHMMSS.xlsx` — full result as Excel
- `logs/query_log.jsonl` — structured log entry (question, SQL, status, timing)

---

## Security

| Allowed | Blocked |
|---|---|
| SELECT | DELETE |
| WITH (CTE) | UPDATE |
| | INSERT |
| | DROP |
| | ALTER |
| | TRUNCATE |
| | MERGE |
| | EXEC |
| | INFORMATION_SCHEMA |
| | SYS.* |

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Ollama connection refused | Run `ollama serve` or check `OLLAMA_URL` in `.env` |
| ODBC error | Verify driver installed; check `DB_CONNECTION_URL` |
| Login failed for SQL Server | Check credentials in `DB_CONNECTION_URL` |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` |
| OUT_OF_SCOPE on valid question | Extend `prompts/system_prompt.md` supported topics |

---

## Future Roadmap

- Embedding-based schema retrieval (replace keyword matching)
- Query memory / conversation history
- SQL auto-repair loop (retry on DB error)
- Result summarization in Persian
- FastAPI service layer
- Streamlit / React frontend
- Multi-database support
