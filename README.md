  # local-sql-agent

A fully local natural language SQL agent powered by Gemma 3 (Ollama) and LangChain — query your SQL Server database in plain English, no API key required.

---

## Project Structure

```
.
├── main.py            # Entry point — runs the SQL agent
├── config.py          # Reads credentials from .env and builds config objects
├── .env               # Your actual credentials (never commit this!)
├── .env.example       # Template — copy this to .env and fill in values
├── requirements.txt   # Python dependencies
└── .gitignore         # Excludes .env from version control
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
OLLAMA_TEMPERATURE=0.2
OLLAMA_TOP_P=0.95

# SQL Server
DB_SERVER=your_server_address
DB_NAME=your_database_name
DB_USER=your_username
DB_PASSWORD=your_password
DB_PORT=1433
DB_DRIVER=ODBC Driver 17 for SQL Server
DB_TRUSTED_CONNECTION=
```

> **Windows Authentication:** If your SQL Server uses Windows Auth instead of username/password, set `DB_TRUSTED_CONNECTION=yes` and leave `DB_PASSWORD` empty.

---

## Usage

```bash
python main.py --prompt "How many albums are in the database?"
```

```bash
python main.py --prompt "List the top 5 customers by total purchases."
```

If you run without `--prompt`, you will see:

```
Please provide a prompt using --prompt argument
Example: python main.py --prompt "How many albums are in the database?"
```

---

## How It Works

```
.env
 └─► config.py
       ├─► get_ollama_config()   →  ChatOllama (Gemma 3, local)
       └─► get_sqlserver_uri()   →  SQLDatabase (SQL Server)
                                        └─► create_sql_agent()
                                                └─► agent.invoke(prompt)
```

1. `config.py` reads all credentials from `.env` at import time.
2. `main.py` calls `build_llm()` and `build_db()` to initialize the model and database.
3. The LangChain SQL agent receives the natural language prompt, generates SQL, runs it against the database, and returns a human-readable answer.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `OLLAMA_MODEL` not found | Run `ollama list` and make sure the model name matches exactly |
| Ollama connection refused | Make sure Ollama is running: `ollama serve` |
| ODBC error | Check that the ODBC driver is installed and `DB_DRIVER` in `.env` matches the installed version |
| Login failed for SQL Server | Double-check `DB_USER` and `DB_PASSWORD` in `.env` |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` |

---

## Security Notes

- `.env` is listed in `.gitignore` and will never be committed to version control.
- `.env.example` contains only placeholder values — it is safe to commit.
- Passwords are URL-encoded inside `config.py` before being passed to SQLAlchemy, so special characters are handled correctly.
- `config.py` validates that no placeholder values (e.g. `your_password_here`) have been left in `.env`.

---

## Changing the Model

To use a different local model, update a single line in `.env`:

```ini
OLLAMA_MODEL=llama3.2:latest
```

No code changes required.
