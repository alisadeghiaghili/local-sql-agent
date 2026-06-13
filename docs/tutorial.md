# Local SQL Agent — Tutorial

**English** | [فارسی ↓](#آموزش-local-sql-agent)

---

This tutorial follows the vignette style: instead of listing every function signature, it walks you through real tasks from start to finish. By the end you will have run your first query, extended the knowledge base for a new domain entity, diagnosed a retrieval miss, and written a test.

## Table of contents

1. [Installation](#1-installation)
2. [Your first query](#2-your-first-query)
3. [How retrieval works](#3-how-retrieval-works)
4. [How the prompt is built](#4-how-the-prompt-is-built)
5. [The SQL security pipeline](#5-the-sql-security-pipeline)
6. [Exporting results](#6-exporting-results)
7. [Adding a new table](#7-adding-a-new-table)
8. [Adding synonyms and aliases](#8-adding-synonyms-and-aliases)
9. [Adding few-shot examples](#9-adding-few-shot-examples)
10. [Diagnosing retrieval misses](#10-diagnosing-retrieval-misses)
11. [Writing tests](#11-writing-tests)
12. [Using the HTTP API](#12-using-the-http-api)
13. [Health check and monitoring](#13-health-check-and-monitoring)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. Installation

### What you need

| Dependency | Minimum version | Notes |
|---|---|---|
| Python | 3.11 | |
| [Ollama](https://ollama.com) | any | Must be running on `localhost:11434` |
| SQL Server | 2016+ | Accessed via ODBC |
| ODBC Driver | 17 or 18 | `msodbcsql17` / `msodbcsql18` |

### Step 1 — Clone and create a virtual environment

```bash
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent

python -m venv .venv
source .venv/bin/activate      # Linux / macOS
# .venv\Scripts\activate       # Windows

pip install -r requirements.txt
```

### Step 2 — Configure

```bash
cp .env.example .env
```

Open `.env` and set at minimum:

```dotenv
# Required
DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/YourDB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
OLLAMA_MODEL=llama3

# Optional tuning
MAX_ROWS_RETURNED=500
QUERY_TIMEOUT_SECONDS=30
CACHE_TTL_SECONDS=300
```

### Step 3 — Pull a model

```bash
ollama pull llama3          # fast, good baseline
ollama pull llama3.1:8b     # better for complex joins
ollama pull llama3.1:70b    # best accuracy, needs ~40 GB RAM
```

---

## 2. Your first query

### CLI

```bash
python app.py
```

You will see:

```
════════════════════════════════════════════════════════════
Auction NLQ Engine Started
════════════════════════════════════════════════════════════

Question:
```

Type a question in Persian or English:

```
Question: top 5 customers by purchase value in 1402

════════════════════════════════════════════════════════════
GENERATED SQL
════════════════════════════════════════════════════════════
SELECT TOP 5
    c.Name,
    SUM(cc.TotalPrice) AS PurchaseValue
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Customer] c ON cc.BuyerCustomer_ID = c.ID
JOIN [Auction_Dim].[Date] d ON cc.Date_ID = d.ID
WHERE d.PersianYear = 1402
GROUP BY c.Name
ORDER BY PurchaseValue DESC

Name                      PurchaseValue
────────────────────────  ─────────────
شرکت آلفا                   4820000000
شرکت بتا                    3910000000
...

Returned Rows: 5  |  Execution Time: 1.38s
Excel Saved: exports/result_20260613_142257.xlsx
```

Type `exit` to quit.

### Python

```python
import requests

response = requests.post(
    "http://localhost:8000/query",
    json={"question": "top 5 customers by purchase value in 1402"},
)
data = response.json()
print(data["sql"])        # generated SQL
print(data["result"])     # list of row dicts
print(data["row_count"])  # 5
```

---

## 3. How retrieval works

Before the LLM is called, `ContextRetriever` runs six independent retrievers and combines their output into a single `RetrievalContext`.

```
Question: "برترین مشتریان تالار پتروشیمی در 1402"
          │
          ├─ EntityRetriever     → ["Customer"]       (alias: "مشتری")
          ├─ FactRetriever       → ["CustomerContract"]  (keyword: "خرید")
          ├─ RelationshipRetriever → ["JOIN [Auction_Dim].[Customer] ON ..."]
          ├─ RuleRetriever       → ["خرید: ..."]      (keyword: "مشتری")
          ├─ ExampleRetriever    → top-3 examples by tag overlap
          └─ ValueRetriever      → {"Ring": "تالار پتروشیمی", "PersianYear": 1402}
```

### Two-tier retrieval

Every retriever first tries a **fast path** (exact alias or keyword match). If that yields nothing, it falls back to the **TF-IDF bigram engine** (`schema_data/retriever.py`) which scores all table descriptions against the question.

You can call the TF-IDF retriever directly:

```python
from schema_data.retriever import retrieve_tables

# Fast path hits → returned immediately
print(retrieve_tables("فروش ماهانه مشتریان"))
# ['Contract', 'Customer', 'Date']

# No match → returns [] when fallback disabled
print(retrieve_tables("xyzzy", fallback=False))
# []
```

### Forced tables

Some tables must always appear when certain keywords are present:

```python
# schema_data/retriever.py
_ALWAYS_INCLUDE = {
    "سال":   ["Date"],
    "ماه":   ["Date"],
    "تاریخ": ["Date"],
}
```

This means any question mentioning a year or month will always receive the `Date` table in context.

---

## 4. How the prompt is built

`PromptBuilder.build()` assembles the final prompt from the `RetrievalContext`:

```python
from core.models import RetrievalContext
from prompt_engine.builder import PromptBuilder

context = RetrievalContext(
    entities=["Customer"],
    facts=["CustomerContract"],
    dimensions=["Customer"],
    relationships=[
        "JOIN [Auction_Dim].[Customer] ON "
        "[Auction_Fact].[CustomerContract].[BuyerCustomer_ID] = [Auction_Dim].[Customer].[ID]"
    ],
    business_rules=["خرید: ارزش خرید از CustomerContract.TotalPrice محاسبه می‌شود."],
    examples=[
        {
            "question": "برترین مشتریان",
            "sql": "SELECT TOP 10 c.Name, SUM(cc.TotalPrice) AS PurchaseValue ...",
        }
    ],
    filters={"PersianYear": 1402},
)

prompt = PromptBuilder.build(
    question="برترین مشتریان از نظر ارزش خرید",
    system_prompt="You are a T-SQL expert for SQL Server.",
    context=context,
)
print(prompt)
```

The prompt has clearly labelled sections — `## Business Rules`, `## Schema`, `## Relationships`, `## Filters`, `## Examples`, `## Question` — which is what allows smaller models to reliably produce correct SQL.

---

## 5. The SQL security pipeline

Every SQL string — whether from the model or anywhere else — passes through three functions in `security/sql_guard.py`:

```python
from security.sql_guard import clean_sql, validate_sql, ensure_top

# Step 1: clean
# Strips markdown fences, preamble prose, converts LIMIT→TOP
raw = """
Here is the SQL you requested:
```sql
SELECT * FROM [Auction_Fact].[Contract] LIMIT 10
```
"""
sql = clean_sql(raw)
print(sql)
# SELECT TOP 10 * FROM [Auction_Fact].[Contract]

# Step 2: validate
# Raises ValueError on any forbidden pattern
validate_sql(sql)   # passes — no DDL/DML

try:
    validate_sql("DROP TABLE Contract")
except ValueError as e:
    print(e)  # Forbidden SQL keyword: DROP

# Step 3: ensure TOP
# Injects TOP n if absent, leaves it alone if already present
sql = ensure_top(sql, n=500)
print(sql)
# SELECT TOP 10 * FROM [Auction_Fact].[Contract]  ← unchanged (already has TOP)
```

### What is blocked

| Category | Keywords |
|---|---|
| DDL | `DROP`, `ALTER`, `CREATE`, `TRUNCATE` |
| DML | `DELETE`, `UPDATE`, `INSERT`, `MERGE` |
| Execution | `EXECUTE`, `EXEC`, `XP_`, `SP_` |
| Schema introspection | `INFORMATION_SCHEMA`, `SYS.` |
| Raw pagination | `LIMIT` (rewritten to `TOP`, not blocked) |

---

## 6. Exporting results

The CLI saves every successful query to Excel automatically. You can also trigger exports programmatically:

```python
from database.executor import execute_sql
from exporters.excel_exporter import export_excel

df = execute_sql("SELECT TOP 20 * FROM [Auction_Fact].[Contract]")

# Excel — auto-fits columns, timestamped filename
path = export_excel(df)
print(path)  # exports/result_20260613_142500.xlsx
```

All export files land in the directory set by `EXPORT_DIR` (default: `exports/`).

---

## 7. Adding a new table

Suppose your exchange now trades in a new instrument and you need to add a `Broker` table.

### Step 1 — Describe the table (bilingual)

`schema_data/tables.py`:

```python
TABLE_DESCRIPTIONS: dict[str, str] = {
    # ... existing tables ...
    "Broker": (
        "Registered brokerage firms (کارگزاری‌ها) licensed to trade on the exchange. "
        "Contains broker code, name, and license status. "
        "برای فیلتر بر اساس کارگزار یا کارمزد از این جدول استفاده کنید."
    ),
}
```

> **Write descriptions bilingually.** The TF-IDF engine tokenises both Persian and English, so bilingual descriptions make the fallback retriever work for both languages.

### Step 2 — Define columns

`schema_data/columns.py`:

```python
TABLE_COLUMNS: dict[str, dict[str, str]] = {
    # ... existing tables ...
    "Broker": {
        "BrokerID":   "Surrogate primary key",
        "BrokerCode": "Exchange-assigned broker code (کد کارگزاری)",
        "BrokerName": "Full registered name (نام کارگزاری)",
        "IsActive":   "1 = active license, 0 = suspended",
    },
}
```

### Step 3 — Add JOIN relationship

`schema_data/relationships.py`:

```python
RELATIONSHIPS: dict[str, str] = {
    # ... existing relationships ...
    "Contract -> Broker": (
        "JOIN [Auction_Dim].[Broker] "
        "ON [Auction_Fact].[Contract].[BuyBroker_ID] = [Auction_Dim].[Broker].[BrokerID]"
    ),
}
```

### Step 4 — Add Persian aliases

`knowledge/entities.py`:

```python
"Broker": {
    "aliases": ["کارگزار", "کارگزاری", "broker", "brokerage"],
    "table": "Broker",
}
```

### Step 5 — Verify

```bash
python -c "
from schema_data.retriever import retrieve_tables
print(retrieve_tables('فروش کارگزاران'))
"
# Should include 'Broker'
```

---

## 8. Adding synonyms and aliases

If the retriever misses a table because users phrase questions differently, add a synonym.

**Scenario:** users say "عرضه کالا" but the `Offer` table is not being retrieved.

`knowledge/aliases.py`:

```python
SYNONYMS: dict[str, list[str]] = {
    # ... existing entries ...
    "عرضه": ["Offer", "offer", "supply", "عرضه کالا", "عرضه‌کننده"],
}
```

Then verify:

```bash
python -c "
from schema_data.retriever import retrieve_tables
print(retrieve_tables('عرضه کالا در تالار پتروشیمی'))
"
# Should include 'Offer'
```

For trading hall names specifically, the canonical mapping lives in `RING_ALIASES`:

```python
RING_ALIASES["تالار برق"] = [
    "برق", "تالار برق", "رینگ برق", "بازار برق", "انرژی برق"
]
```

---

## 9. Adding few-shot examples

Few-shot examples are the highest-leverage way to improve SQL accuracy. When a question's tags overlap with an example's tags, that example is injected into the prompt.

`knowledge/examples.py`:

```python
EXAMPLES = [
    # ... existing examples ...
    {
        "tags": ["broker", "top", "trade", "value"],
        "question": "Top 5 brokers by trade value this year",
        "sql": """
            SELECT TOP 5
                b.BrokerName,
                SUM(c.TotalPrice) AS TradeValue
            FROM [Auction_Fact].[Contract] c
            JOIN [Auction_Dim].[Broker] b
                ON c.BuyBroker_ID = b.BrokerID
            GROUP BY b.BrokerName
            ORDER BY TradeValue DESC
        """
    },
]
```

**Tagging strategy:** use small, reusable tags (`broker`, `top`, `value`, `month`, `year`, `count`) rather than long phrases. The `ExampleRetriever` scores by tag overlap, so broader tags match more questions.

---

## 10. Diagnosing retrieval misses

A retrieval miss happens when the model generates SQL referencing a table that the retriever did not include in context. The model essentially guessed the table name — sometimes correctly, often not.

The `analyze_misses.py` script scans your query log and finds these patterns:

```bash
python scripts/analyze_misses.py
# Uses logs/query_history.jsonl by default

python scripts/analyze_misses.py /path/to/other_log.jsonl
```

Sample output:

```
🔍  3 miss event(s) detected

────────────────────────────────────────────────────────
  Table : Broker  (missed 2×)
    candidate token: 'کارگزار'   (freq=2)
    candidate token: 'بورس'      (freq=1)

  Table : Ring    (missed 1×)
    candidate token: 'تالار'     (freq=1)
────────────────────────────────────────────────────────
```

**How to read this:** `Broker` appeared in generated SQL twice, but the retriever did not put it in context. Users used the word `کارگزار` in those questions — that word is not yet in `SYNONYMS` or `TABLE_DESCRIPTIONS`. The fix: add `"کارگزار"` to `knowledge/aliases.py`.

You can also call `analyse()` programmatically:

```python
from pathlib import Path
from scripts.analyze_misses import analyse, _build_report

misses = analyse(Path("logs/query_history.jsonl"))
report = _build_report(misses)

for entry in report["tables_ranked_by_miss_count"]:
    print(f"{entry['table']}: {entry['miss_count']} misses")
    for cand in entry["top_candidates"][:3]:
        print(f"  → add synonym: '{cand['token']}'")
```

---

## 11. Writing tests

Tests live in `tests/`. The project uses `pytest`; fixtures are in `tests/conftest.py`.

### Testing the retriever

```python
# tests/test_retriever.py
from schema_data.retriever import retrieve_tables

class TestRetrieveTables:
    def test_contract_retrieved_for_trade_question(self):
        result = retrieve_tables("ارزش معاملات")
        assert "Contract" in result

    def test_date_forced_on_year_question(self):
        # _ALWAYS_INCLUDE forces Date whenever 'سال' appears
        result = retrieve_tables("فروش سالیانه")
        assert "Date" in result

    def test_fallback_false_returns_empty_on_garbage(self):
        result = retrieve_tables("xyzzy gibberish", fallback=False)
        assert result == []

    def test_both_languages_work(self):
        fa = retrieve_tables("مشتریان برتر")
        en = retrieve_tables("top customers")
        assert "Customer" in fa
        assert "Customer" in en
```

### Testing the SQL guard

```python
# tests/test_sql_guard.py
import pytest
from security.sql_guard import clean_sql, validate_sql, ensure_top

class TestCleanSql:
    def test_strips_markdown_fence(self):
        raw = "```sql\nSELECT 1\n```"
        assert clean_sql(raw) == "SELECT 1"

    def test_limit_converted_to_top(self):
        assert clean_sql("SELECT * FROM T LIMIT 5") == "SELECT TOP 5 * FROM T"

    def test_empty_input_raises(self):
        with pytest.raises(ValueError, match="empty"):
            clean_sql("")

class TestValidateSql:
    @pytest.mark.parametrize("stmt", [
        "DROP TABLE Contract",
        "DELETE FROM Contract",
        "INSERT INTO Contract VALUES (1)",
        "ALTER TABLE Contract ADD col INT",
    ])
    def test_forbidden_statements_raise(self, stmt):
        with pytest.raises(ValueError):
            validate_sql(stmt)

class TestEnsureTop:
    def test_injects_top_when_absent(self):
        sql = ensure_top("SELECT Name FROM Customer", n=50)
        assert sql.upper().startswith("SELECT TOP 50")

    def test_preserves_existing_top(self):
        sql = "SELECT TOP 10 Name FROM Customer"
        assert ensure_top(sql, n=50) == sql
```

### Testing the API

```python
# tests/test_api_endpoints.py
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from api.server import app

client = TestClient(app)

def test_query_endpoint_returns_sql():
    mock_result = MagicMock()
    mock_result.sql = "SELECT TOP 10 Name FROM Customer"
    mock_result.df.to_dict.return_value = []
    mock_result.df.__len__ = lambda s: 0

    with patch("api.runner.run_query", return_value=mock_result):
        response = client.post("/query", json={"question": "list customers"})

    assert response.status_code == 200
    assert "sql" in response.json()

def test_health_endpoint_returns_ok():
    with patch("api.health.check_db", return_value=True), \
         patch("api.health.check_ollama", return_value=True):
        response = client.get("/health")
    assert response.json()["status"] == "ok"
```

### Running tests

```bash
pytest                                  # all tests
pytest tests/test_sql_guard.py -v       # one module, verbose
pytest -k "retriever" -v               # tests matching a keyword
pytest --cov=. --cov-report=html        # coverage report
open htmlcov/index.html
```

---

## 12. Using the HTTP API

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
```

### POST /query

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
    "question": "فروش ماهانه تالار پتروشیمی در 1402",
    "mode": "full"
  }'
```

`mode` options:
- `full` (default) — returns SQL + executed result set
- `sql` — returns SQL only, does not execute
- `result` — executes and returns rows only

### Query cache

Repeat identical questions are served from the in-process LRU cache:

```bash
# Check cache state
curl http://localhost:8000/cache/stats
# {"size": 4, "hits": 12, "misses": 4, "evictions": 0}

# Remove one entry
curl -X POST http://localhost:8000/cache/invalidate \
  -d '{"question": "فروش ماهانه", "mode": "full"}'

# Clear everything
curl -X POST http://localhost:8000/cache/clear
```

Cache TTL and max size are controlled by `CACHE_TTL_SECONDS` and `CACHE_MAX_SIZE` in `.env`.

---

## 13. Health check and monitoring

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "database": true,
  "ollama": true,
  "model": "llama3"
}
```

| `status` | Meaning |
|---|---|
| `ok` | Both SQL Server and Ollama are reachable |
| `degraded` | One component is unreachable |
| `down` | Neither component is reachable |

Query logs are written to `logs/query_history.jsonl` (one JSON object per line):

```json
{
  "timestamp": "2026-06-13T14:22:57",
  "question": "برترین مشتریان در 1402",
  "generated_sql": "SELECT TOP 10 ...",
  "model_name": "llama3",
  "row_count": 10,
  "execution_time_seconds": 1.38,
  "status": "SUCCESS",
  "excel_file": "exports/result_20260613_142257.xlsx"
}
```

---

## 14. Troubleshooting

### A table is not being retrieved

```python
# Check if the table exists in descriptions
from schema_data.tables import TABLE_DESCRIPTIONS
print("Broker" in TABLE_DESCRIPTIONS)  # False → add it

# Check synonyms
from knowledge.aliases import SYNONYMS
print(SYNONYMS.get("کارگزار"))  # None → add it

# Run miss analysis
# python scripts/analyze_misses.py
```

### SQL is invalid or references wrong tables

1. Add a few-shot example for that question pattern (`knowledge/examples.py`)
2. Add the relevant business rule (`knowledge/business_rules.py`)
3. Use a larger model: `ollama pull llama3.1:70b`

### `RuntimeError: Database error`

```bash
curl http://localhost:8000/health
# If database: false:
python -c "
from database.connection import get_engine
from sqlalchemy import text
with get_engine().connect() as c:
    print(c.execute(text('SELECT 1')).fetchone())
"
```

### `ModelUnavailableError` / `503`

```bash
curl http://localhost:11434/api/tags   # check Ollama is running
ollama list                            # check model is downloaded
```

### `ValueError: Received empty SQL from model`

- The model returned an out-of-scope response. Check `logs/query_history.jsonl` for status `OUT_OF_SCOPE`.
- Add a matching few-shot example to guide the model.
- Try a larger or more capable model.

---

---

# آموزش Local SQL Agent

[English ↑](#local-sql-agent--tutorial) | **فارسی**

---

این آموزش به شیوه vignette نوشته شده: به جای فهرست کردن هر تابع، شما را از طریق وظایف واقعی — از نصب تا تشخیص مشکل — راهنمایی می‌کند. تا پایان اولین کوئری را اجرا کرده، یک entity جدید به knowledge base اضافه کرده، یک retrieval miss