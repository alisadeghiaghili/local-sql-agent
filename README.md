# local-sql-agent

A fully local natural language SQL agent powered by Gemma 3 (Ollama) and LangChain — query your SQL Server database in plain English, no API key required.

---

## Project Structure

```
local-sql-agent/
├── src/
│   └── sql_agent/              # Core library (shared by all agents & runners)
│       ├── __init__.py
│       ├── config.py           # Settings dataclass, .env loader, URI builder
│       ├── db.py               # Shared SQLAlchemy engine factory + execute_sql()
│       ├── llm.py              # Shared Ollama HTTP client with retry
│       ├── validator.py        # clean_sql(), validate_sql(), ensure_top()
│       └── prompts.py          # Canonical system prompts (simple / prompt / full)
├── agents/                     # LangChain-based entry-points
│   ├── main.py             # SQL Server + LangChain Agent
│   ├── langchain_sql.py    # BI DB + strict whitelist validation
│   └── nlq_with_sqlite.py  # SQLite + LangChain Agent (local testing)
├── runners/                    # Raw Ollama HTTP runners (no LangChain)
│   ├── nlq.py              # SQL Server, --mode simple/prompt/full
│   └── langsql.py          # AuctionDB interactive REPL
├── scripts/
│   └── create_db.py        # Generate sample.db for local testing
├── tests/
│   ├── test_validator.py   # Unit tests for validator.py
│   └── test_config.py      # Unit tests for config.py / Settings
├── config.py                   # Backwards-compat shim → src/sql_agent/config.py
├── pyproject.toml              # Package definition + dev dependencies
├── requirements.txt            # Pinned dependencies
├── .env.example                # Template — copy to .env and fill in values
├── .gitignore
├── README.md
└── CHANGELOG
```

---

## Prerequisites

### 1. Python 3.9+

### 2. Ollama

Download and install from [https://ollama.com](https://ollama.com), then pull the model:

```bash
ollama pull gemma3:12b
```

Verify it is available:

```bash
ollama list
```

### 3. ODBC Driver for SQL Server

Download from Microsoft:
[https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server)

The default driver name used in this project is `ODBC Driver 17 for SQL Server`.
If you have a different version installed, update `DB_DRIVER` in your `.env` accordingly.

---

## Installation

```bash
# Option A — editable install (recommended for development)
pip install -e ".[dev]"

# Option B — plain requirements
pip install -r requirements.txt
```

---

## Configuration

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Then open `.env` and set the following:

```ini
# Ollama
OLLAMA_MODEL=gemma3:12b
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_TEMPERATURE=0.1
OLLAMA_TOP_P=0.9

# SQL Server
DB_SERVER=your_server_address
DB_NAME=your_database_name
DB_USER=your_username
DB_PASSWORD=your_password
DB_PORT=1433
DB_DRIVER=ODBC Driver 17 for SQL Server
DB_TRUSTED_CONNECTION=

# SQLite (local testing only)
SQLITE_DB_PATH=sample.db
```

> **Windows Authentication:** If your SQL Server uses Windows Auth instead of username/password, set `DB_TRUSTED_CONNECTION=yes` and leave `DB_PASSWORD` empty.

---

## Usage

### LangChain Agents (recommended for SQL Server)

```bash
# General-purpose LangChain SQL agent
python -m agents.main --prompt "How many albums are in the database?"

# BI database agent with strict table whitelist
python -m agents.langchain_sql --prompt "top 10 users by session count"
```

### Raw Ollama Runners (faster, no LangChain overhead)

```bash
# SQL Server — three prompt modes
python -m runners.nlq --prompt "top 10 customers by revenue"
python -m runners.nlq --prompt "..." --mode simple
python -m runners.nlq --prompt "..." --mode prompt
python -m runners.nlq --prompt "..." --mode full     # default

# AuctionDB interactive REPL
python -m runners.langsql
```

### SQLite (local testing, no SQL Server needed)

```bash
# Step 1 — create the sample database
python scripts/create_db.py

# Step 2 — run queries against it
python -m agents.nlq_with_sqlite --prompt "How many albums are in the database?"
```

### Console entry-points (after `pip install -e .`)

```bash
sql-agent           --prompt "..."
sql-agent-langchain --prompt "..."
sql-agent-sqlite    --prompt "..."
```

---

## Running Tests

```bash
# Run all tests
pytest

# With coverage report
pytest --cov=src --cov-report=term-missing
```

Tests live in `tests/` and cover `validator.py` and `config.py` without needing a live Ollama or SQL Server connection.

---

## How It Works

```
.env
 └─► src/sql_agent/config.py  (Settings dataclass)
       ├─► src/sql_agent/db.py      →  SQLAlchemy engine  →  SQL Server / SQLite
       ├─► src/sql_agent/llm.py     →  Ollama HTTP API    →  Gemma 3 (local)
       ├─► src/sql_agent/validator.py → clean_sql() → validate_sql()
       └─► src/sql_agent/prompts.py   → system prompt by mode

agents/
  main.py            →  LangChain SQL Agent  →  agent.invoke(prompt)
  langchain_sql.py   →  ChatOllama + whitelist guard  →  execute_sql()
  nlq_with_sqlite.py →  LangChain SQL Agent  →  SQLite

runners/
  nlq.py             →  call_ollama() → clean_sql() → execute_sql()
  langsql.py         →  call_ollama() → REPL loop   → pandas DataFrame
```

1. `Settings.from_env()` reads all credentials from `.env` at startup (no import side-effects).
2. `get_engine()` returns a cached `SQLAlchemy` engine per URI — connection pool reused across calls.
3. `call_ollama()` retries up to 3 times with exponential back-off on connection errors.
4. `clean_sql()` strips markdown, fixes `LIMIT → TOP`, and corrects `DISTINCT/TOP` ordering.
5. `validate_sql()` blocks `DELETE`, `DROP`, `UPDATE` and other destructive keywords before any query reaches the database.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `OLLAMA_MODEL` not found | Run `ollama list` and make sure the model name matches exactly |
| Ollama connection refused | Make sure Ollama is running: `ollama serve` |
| ODBC error | Check that the ODBC driver is installed and `DB_DRIVER` in `.env` matches the installed version |
| Login failed for SQL Server | Double-check `DB_USER` and `DB_PASSWORD` in `.env` |
| `ModuleNotFoundError: sql_agent` | Run `pip install -e .` from the repo root |
| `ModuleNotFoundError` (other) | Run `pip install -r requirements.txt` |

---

## Security Notes

- `.env` is listed in `.gitignore` and will never be committed to version control.
- `.env.example` contains only placeholder values — it is safe to commit.
- Passwords are URL-encoded inside `config.py` before being passed to SQLAlchemy, so special characters are handled correctly.
- `Settings.validate()` raises an error if any placeholder value (e.g. `your_password_here`) is left in `.env`.
- `validate_sql()` blocks `DELETE`, `DROP`, `UPDATE`, `INSERT`, `TRUNCATE`, `EXEC`, and `MERGE` before any query reaches the database.

---

## Changing the Model

To use a different local model, update a single line in `.env`:

```ini
OLLAMA_MODEL=llama3.2:latest
```

No code changes required.
