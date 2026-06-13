# Local SQL Agent — Tutorial

> **فارسی:** [آموزش کامل](../fa/tutorial.md)

This tutorial is written in vignette style: rather than listing every function and parameter, it walks you through real tasks end-to-end. By the end you will have installed the engine, run your first query, understood how every layer works, extended the knowledge base for a new domain entity, diagnosed a retrieval miss, and written tests for all of it.

---

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

The engine needs three things outside Python: a running Ollama instance, a SQL Server database, and an ODBC driver for the connection.

### What you need

| Dependency | Minimum | Notes |
|---|---|---|
| Python | 3.11 | |
| [Ollama](https://ollama.com) | any | Must run on `localhost:11434` |
| SQL Server | 2016+ | Any edition including Express |
| ODBC Driver | 17 or 18 | `msodbcsql17` / `msodbcsql18` |

### Step 1 — Clone and create a virtual environment

```bash
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent

python -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

### Step 2 — Configure

```bash
cp .env.example .env
```

Open `.env`. The only two required values are your database URL and the Ollama model name:

```dotenv
# Required
DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/YourDB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
OLLAMA_MODEL=llama3

# Sensible defaults — override as needed
OLLAMA_URL=http://localhost:11434/api/generate
MAX_ROWS_RETURNED=1000
QUERY_TIMEOUT_SECONDS=60
CACHE_TTL_SECONDS=300
CACHE_MAX_SIZE=256
```

### Step 3 — Pull a model

```bash
ollama pull llama3          # fast, good baseline (~4 GB)
ollama pull llama3.1:8b     # better on complex multi-table joins
ollama pull llama3.1:70b    # best accuracy, requires ~40 GB RAM
```

Start with `llama3`. Switch to a larger model only if you see the engine producing wrong table names or malformed SQL.

---

## 2. Your first query

Let’s run the engine and see what actually happens.

### CLI

```bash
python app.py
```

The REPL starts:

```
════════════════════════════════════════════════════════════
Auction NLQ Engine  —  type 'exit' to quit
════════════════════════════════════════════════════════════

Question:
```

Type a question — Persian or English:

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
JOIN [Auction_Dim].[Date]     d ON cc.Date_ID = d.ID
WHERE d.PersianYear = 1402
GROUP BY c.Name
ORDER BY PurchaseValue DESC

Name                      PurchaseValue
────────────────────────  ─────────────
شرکت آلفا                   4820000000
شرکت بتا                    3910000000
...

Returned Rows: 5  |  Execution Time: 1.38s
Excel saved → exports/result_20260613_142257.xlsx
```

### Python client

Once the HTTP server is running, you can query it from any Python script:

```python
import requests

r = requests.post(
    "http://localhost:8000/query",
    json={"question": "top 5 customers by purchase value in 1402"},
)
data = r.json()
print(data["sql"])        # the generated T-SQL
print(data["result"])     # list of row dicts
print(data["row_count"])  # 5
print(data["status"])     # SUCCESS
```

### Response schema

```json
{
  "question": "top 5 customers by purchase value in 1402",
  "sql": "SELECT TOP 5 c.Name, SUM(cc.TotalPrice) AS PurchaseValue ...",
  "result": [{"Name": "شرکت آلفا", "PurchaseValue": 4820000000}, ...],
  "row_count": 5,
  "status": "SUCCESS"
}
```

---

## 3. How retrieval works

The most important thing to understand about this engine is that **the LLM never sees your full schema**. Before the model is called, six independent retrievers build a tight, question-specific context.

### The six retrievers

```
Question: "برترین مشتریان تالار پتروشیمی در 1402"
          │
          ├─ EntityRetriever     → ["Customer"]              (alias match: "مشتری")
          ├─ FactRetriever       → ["CustomerContract"]      (keyword match: "خرید")
          ├─ RelationshipRetriever → ["JOIN Customer ON ..."] (FK for selected tables)
          ├─ RuleRetriever       → ["خرید: ..."]            (topic match: "مشتری")
          ├─ ExampleRetriever    → top-3 few-shot examples    (tag-overlap score)
          └─ ValueRetriever      → {Ring: "تالار پتروشیمی", PersianYear: 1402}
```

All six outputs are packaged into a `RetrievalContext` dataclass and handed to `PromptBuilder`.

### Two-tier retrieval

Every retriever uses the same strategy: **fast path first, TF-IDF fallback second**.

- **Fast path:** exact alias or keyword match against `knowledge/entities.py`, `knowledge/aliases.py`, or hardcoded patterns. This is O(1).
- **TF-IDF fallback:** if the fast path returns nothing, `schema_data/retriever.py` scores all table descriptions against the question using bigram TF-IDF. Slower but handles novel phrasing.

You can call the TF-IDF engine directly to debug retrieval:

```python
from schema_data.retriever import retrieve_tables

print(retrieve_tables("فروش ماهانه مشتریان"))
# ['Contract', 'Customer', 'Date']

# fallback=False: return [] instead of full list when nothing scores above threshold
print(retrieve_tables("xyzzy nonsense", fallback=False))
# []
```

### Forced tables

Some tables must always appear regardless of TF-IDF score. The `_ALWAYS_INCLUDE` dict in `schema_data/retriever.py` handles this:

```python
_ALWAYS_INCLUDE = {
    "سال":   ["Date"],
    "ماه":   ["Date"],
    "تاریخ": ["Date"],
}
```

Any question containing the word `سال` or `ماه` will always receive the `Date` table in context — even if TF-IDF scores it low.

---

## 4. How the prompt is built

Once `ContextRetriever` has assembled the `RetrievalContext`, `PromptBuilder.build()` turns it into the final prompt string. You can call this directly to inspect what the model sees:

```python
from core.models import RetrievalContext
from prompt_engine.builder import PromptBuilder

context = RetrievalContext(
    entities=["Customer"],
    facts=["CustomerContract"],
    dimensions=["Customer"],
    relationships=[
        "JOIN [Auction_Dim].[Customer] "
        "ON [Auction_Fact].[CustomerContract].[BuyerCustomer_ID] = [Auction_Dim].[Customer].[ID]"
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
    system_prompt="You are a T-SQL expert for SQL Server 2019.",
    context=context,
)
print(prompt)
```

The output is a structured string with clearly labelled sections:

```
## System
You are a T-SQL expert for SQL Server 2019.

## Business Rules
خرید: ارزش خرید از CustomerContract.TotalPrice محاسبه می‌شود.

## Schema
Table: CustomerContract
  TotalPrice  Purchase total value
  ...
Table: Customer
  Name        Customer full name
  ...

## Relationships
JOIN [Auction_Dim].[Customer] ON ...

## Active Filters
PersianYear = 1402

## Examples
Q: برترین مشتریان
A: SELECT TOP 10 c.Name, SUM(cc.TotalPrice) ...

## Question
برترین مشتریان از نظر ارزش خرید
```

This structure — not a raw schema dump — is why small local models produce correct SQL.

---

## 5. The SQL security pipeline

Every SQL string — from the model, from a test, from anywhere — must pass through three functions in `security/sql_guard.py` before it can be executed.

```python
from security.sql_guard import clean_sql, validate_sql, ensure_top

# --- Step 1: clean ---
# Strips markdown fences, preamble prose, and converts LIMIT→TOP
raw = """
Here is the SQL query you requested:
```sql
SELECT * FROM [Auction_Fact].[Contract] LIMIT 10
```
"""
sql = clean_sql(raw)
print(sql)
# SELECT TOP 10 * FROM [Auction_Fact].[Contract]

# --- Step 2: validate ---
# Raises ValueError on any forbidden pattern
validate_sql(sql)  # passes

try:
    validate_sql("DROP TABLE Contract")
except ValueError as e:
    print(e)
    # Forbidden SQL keyword detected: DROP

# --- Step 3: ensure TOP ---
# Injects TOP n when missing; leaves existing TOP unchanged
sql_with_top    = ensure_top("SELECT Name FROM Customer", n=500)
sql_already_top = ensure_top("SELECT TOP 10 Name FROM Customer", n=500)

print(sql_with_top)     # SELECT TOP 500 Name FROM Customer
print(sql_already_top)  # SELECT TOP 10 Name FROM Customer  (unchanged)
```

### What is blocked

| Category | Keywords |
|---|---|
| DDL | `DROP`, `ALTER`, `CREATE`, `TRUNCATE` |
| DML | `DELETE`, `UPDATE`, `INSERT`, `MERGE` |
| Execution | `EXECUTE`, `EXEC`, `XP_`, `SP_` |
| Schema introspection | `INFORMATION_SCHEMA`, `SYS.` |
| Stacked queries | `;` followed by a new statement |

`LIMIT` is not blocked — it is automatically rewritten to `TOP n` for SQL Server compatibility.

---

## 6. Exporting results

The CLI saves every successful result to Excel automatically. For programmatic use:

```python
from database.executor import execute_sql
from exporters.excel_exporter import export_excel

df = execute_sql("SELECT TOP 100 * FROM [Auction_Fact].[Contract]")
print(f"{len(df)} rows, {len(df.columns)} columns")

# Excel with auto-fitted columns and a timestamped filename
path = export_excel(df)
print(path)  # exports/result_20260613_142500.xlsx
```

All exports land in the directory specified by `EXPORT_DIR` (default: `exports/`). The directory is created automatically on first use.

---

## 7. Adding a new table

Imagine your exchange starts clearing trades for a new instrument category and you need to add a `Broker` dimension table.

This is a five-step process, entirely within configuration files — no engine code changes.

### Step 1 — Describe the table (bilingual)

`schema_data/tables.py`:

```python
TABLE_DESCRIPTIONS: dict[str, str] = {
    # ... existing entries ...
    "Broker": (
        "Registered brokerage firms (\u06a9\u0627\u0631\u06af\u0632\u0627\u0631\u06cc\u200c\u0647\u0627) licensed to execute trades on the exchange. "
        "Contains broker code, full name, and license status. "
        "برای فیلتر کردن بر اساس کارگزار یا کارمزد کارگزاری از این جدول استفاده کنید."
    ),
}
```

> **Always write table descriptions bilingually.** The TF-IDF engine tokenises both Persian and English. A bilingual description means the fallback retriever will find this table whether the user asks in Persian or English.

### Step 2 — Define columns

`schema_data/columns.py`:

```python
TABLE_COLUMNS: dict[str, dict[str, str]] = {
    # ... existing entries ...
    "Broker": {
        "BrokerID":   "Surrogate primary key",
        "BrokerCode": "Exchange-assigned numeric code (کد کارگزاری)",
        "BrokerName": "Full registered company name (نام کارگزاری)",
        "IsActive":   "1 = active license, 0 = suspended or revoked",
    },
}
```

### Step 3 — Register the JOIN relationship

`schema_data/relationships.py`:

```python
RELATIONSHIPS: dict[str, str] = {
    # ... existing entries ...
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
    "aliases": ["کارگزار", "کارگزاری", "معامله‌گر", "broker", "brokerage"],
    "table": "Broker",
}
```

### Step 5 — Verify

```bash
python -c "
from schema_data.retriever import retrieve_tables
result = retrieve_tables('فروش کارگزاران در 1402')
print(result)
assert 'Broker' in result, 'Broker not retrieved!'
print('OK')
"
```

---

## 8. Adding synonyms and aliases

The retriever misses a table when users phrase a question using a word that doesn’t appear in any table description, entity alias, or synonym list.

**Scenario:** analysts say `عرضه کالا` but the `Offer` table isn’t being retrieved.

### Check what’s happening

```python
from schema_data.retriever import retrieve_tables
from knowledge.aliases import SYNONYMS

print(retrieve_tables("عرضه کالا", fallback=False))
# []  ← nothing found on fast path

print("عرضه" in SYNONYMS)  # False ← not in synonyms either
```

### Fix: add the synonym

`knowledge/aliases.py`:

```python
SYNONYMS: dict[str, list[str]] = {
    # ... existing entries ...
    "عرضه": ["Offer", "offer", "supply", "عرضه کالا", "عرضه‌کننده"],
}
```

### Verify

```bash
python -c "
from schema_data.retriever import retrieve_tables
result = retrieve_tables('عرضه کالا تالار پتروشیمی')
print(result)
assert 'Offer' in result
print('OK')
"
```

### Trading hall canonical names

For trading hall names specifically, use `RING_ALIASES` in `knowledge/aliases.py`. The `ValueRetriever` maps any alias to the canonical hall name before injecting it as a filter:

```python
RING_ALIASES["تالار برق"] = [
    "برق", "تالار برق", "رینگ برق", "بازار برق", "انرژی برق"
]
```

---

## 9. Adding few-shot examples

Few-shot examples are the single highest-leverage improvement you can make to SQL accuracy. When the `ExampleRetriever` finds examples whose tags overlap with the question’s detected tags, those examples are injected verbatim into the prompt.

### Adding an example

`knowledge/examples.py`:

```python
EXAMPLES = [
    # ... existing entries ...
    {
        "tags": ["broker", "top", "trade", "value", "year"],
        "question": "top 5 brokers by trade value in 1402",
        "sql": """\
SELECT TOP 5
    b.BrokerName,
    SUM(c.TotalPrice) AS TradeValue
FROM [Auction_Fact].[Contract] c
JOIN [Auction_Dim].[Broker] b
    ON c.BuyBroker_ID = b.BrokerID
JOIN [Auction_Dim].[Date] d
    ON c.Date_ID = d.ID
WHERE d.PersianYear = 1402
GROUP BY b.BrokerName
ORDER BY TradeValue DESC""",
    },
]
```

### Tagging strategy

Use **small, reusable tags** rather than long question phrases. The `ExampleRetriever` scores by tag set intersection, so broader tags match more questions.

| Good tags | Why |
|---|---|
| `broker`, `top`, `value`, `year` | Each reusable across many questions |
| `month`, `count`, `customer` | Combine naturally with others |

| Avoid | Why |
|---|---|
| `"top 5 brokers by trade value in 1402"` | Too specific — only matches identical question |
| Single-character or stop words | Noise, no signal |

---

## 10. Diagnosing retrieval misses

A retrieval miss is when the model generates SQL that references a table the retriever never included in context. The model guessed — sometimes correctly, often not.

The `analyze_misses.py` script compares your query log’s `tables_used` against the `tables_retrieved` recorded at retrieval time:

```bash
python scripts/analyze_misses.py
# reads logs/query_history.jsonl by default

python scripts/analyze_misses.py /path/to/other.jsonl
```

Sample output:

```
🔍  3 miss event(s) found

──────────────────────────────────────────────────────────
Table : Broker  (missed 2×)
  candidate: 'کارگزار'  (freq=2)  ← add to SYNONYMS
  candidate: 'بورس'     (freq=1)  ← add to TABLE_DESCRIPTIONS

Table : Ring    (missed 1×)
  candidate: 'تالار'     (freq=1)  ← add to RING_ALIASES
──────────────────────────────────────────────────────────
```

**Reading the output:** `Broker` appeared in generated SQL twice, but the retriever never put it in context. Users used the word `کارگزار` in those questions. That word is not in `SYNONYMS` or `TABLE_DESCRIPTIONS` — so add it (see section 8).

### Programmatic use

```python
from pathlib import Path
from scripts.analyze_misses import analyse, _build_report

misses = analyse(Path("logs/query_history.jsonl"))
report = _build_report(misses)

for entry in report["tables_ranked_by_miss_count"]:
    print(f"{entry['table']}: {entry['miss_count']} misses")
    for cand in entry["top_candidates"][:3]:
        print(f"  → consider adding synonym: '{cand['token']}'")
```

---

## 11. Writing tests

Tests live in `tests/`. The suite uses `pytest`; shared fixtures are in `tests/conftest.py`.

### Testing retrieval

```python
# tests/test_retriever.py
from schema_data.retriever import retrieve_tables

class TestRetrieveTables:
    def test_contract_retrieved_for_trade_question(self):
        assert "Contract" in retrieve_tables("ارزش معاملات")

    def test_customer_retrieved_for_customer_question(self):
        assert "Customer" in retrieve_tables("برترین مشتریان")

    def test_date_forced_on_year_keyword(self):
        # _ALWAYS_INCLUDE triggers on 'سال'
        assert "Date" in retrieve_tables("فروش سالیانه")

    def test_fallback_false_on_garbage(self):
        assert retrieve_tables("xyzzy nonsense", fallback=False) == []

    def test_english_question_works(self):
        assert "Customer" in retrieve_tables("top customers")
```

### Testing the SQL guard

```python
# tests/test_sql_guard.py
import pytest
from security.sql_guard import clean_sql, validate_sql, ensure_top

class TestCleanSql:
    def test_strips_markdown_fence(self):
        assert clean_sql("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_limit_to_top(self):
        assert clean_sql("SELECT * FROM T LIMIT 5") == "SELECT TOP 5 * FROM T"

    def test_strips_preamble_prose(self):
        raw = "Here is the query:\nSELECT 1"
        assert clean_sql(raw) == "SELECT 1"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            clean_sql("")

class TestValidateSql:
    @pytest.mark.parametrize("bad", [
        "DROP TABLE Contract",
        "DELETE FROM Contract WHERE 1=1",
        "INSERT INTO Contract VALUES (1, 2)",
        "ALTER TABLE Contract ADD x INT",
        "EXEC xp_cmdshell 'dir'",
    ])
    def test_forbidden_raises(self, bad):
        with pytest.raises(ValueError):
            validate_sql(bad)

    def test_select_passes(self):
        validate_sql("SELECT TOP 10 Name FROM Customer")  # no exception

class TestEnsureTop:
    def test_injects_top(self):
        result = ensure_top("SELECT Name FROM Customer", n=50)
        assert "TOP 50" in result.upper()

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

def test_query_returns_sql_field():
    mock = MagicMock()
    mock.sql = "SELECT TOP 5 Name FROM Customer"
    mock.df.to_dict.return_value = []
    mock.df.__len__ = lambda s: 0

    with patch("api.runner.run_query", return_value=mock):
        r = client.post("/query", json={"question": "list customers"})

    assert r.status_code == 200
    assert "sql" in r.json()

def test_health_ok_when_both_reachable():
    with patch("api.health.check_db", return_value=True), \
         patch("api.health.check_ollama", return_value=True):
        r = client.get("/health")
    assert r.json()["status"] == "ok"

def test_health_degraded_when_db_down():
    with patch("api.health.check_db", return_value=False), \
         patch("api.health.check_ollama", return_value=True):
        r = client.get("/health")
    assert r.json()["status"] == "degraded"
```

### Running tests

```bash
pytest                                   # all tests
pytest tests/test_sql_guard.py -v        # one module, verbose
pytest -k "retriever" -v                # filter by keyword
pytest --cov=. --cov-report=html         # coverage report
open htmlcov/index.html
```

---

## 12. Using the HTTP API

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
# Swagger UI: http://localhost:8000/docs
```

### POST /query

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "فروش ماهانه تالار پتروشیمی در 1402", "mode": "full"}'
```

`mode` values:

| Value | Behaviour |
|---|---|
| `full` (default) | Generate SQL, execute it, return SQL + rows |
| `sql` | Generate and return SQL only — do not execute |
| `result` | Execute a previously generated SQL, return rows only |

### Managing the query cache

Identical questions (same text + same mode) are served from the LRU cache:

```bash
# Inspect cache state
curl http://localhost:8000/cache/stats
# {"size": 4, "hits": 17, "misses": 4, "evictions": 0}

# Invalidate one entry
curl -X POST http://localhost:8000/cache/invalidate \
  -H 'Content-Type: application/json' \
  -d '{"question": "فروش ماهانه", "mode": "full"}'

# Flush everything
curl -X POST http://localhost:8000/cache/clear
```

TTL and max size are set by `CACHE_TTL_SECONDS` and `CACHE_MAX_SIZE` in `.env`.

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

Every query is logged to `logs/query_history.jsonl`:

```json
{
  "timestamp": "2026-06-13T14:22:57",
  "question": "برترین مشتریان در 1402",
  "generated_sql": "SELECT TOP 10 ...",
  "tables_retrieved": ["CustomerContract", "Customer", "Date"],
  "model_name": "llama3",
  "row_count": 10,
  "execution_time_seconds": 1.38,
  "status": "SUCCESS",
  "excel_file": "exports/result_20260613_142257.xlsx"
}
```

Feed this log to `analyze_misses.py` regularly to catch retrieval gaps before users notice them.

---

## 14. Troubleshooting

### A table is missing from retrieval results

```python
from schema_data.tables import TABLE_DESCRIPTIONS
from knowledge.aliases import SYNONYMS
from schema_data.retriever import retrieve_tables

# Is the table even registered?
print("Broker" in TABLE_DESCRIPTIONS)        # False → add it to tables.py

# Is the user's word in synonyms?
print(SYNONYMS.get("کارگزار"))                  # None → add it to aliases.py

# What does TF-IDF return for the question?
print(retrieve_tables("کارگزاران برتر", fallback=True))
```

### Generated SQL references wrong tables or columns

1. Add a few-shot example that demonstrates the correct pattern (`knowledge/examples.py`)
2. Add or sharpen the business rule (`knowledge/business_rules.py`)
3. Upgrade to a larger model: `ollama pull llama3.1:70b`

### `RuntimeError: Database connection failed`

```bash
# Check the health endpoint first
curl http://localhost:8000/health

# Test the connection string directly
python -c "
from database.connection import get_engine
from sqlalchemy import text
with get_engine().connect() as c:
    print(c.execute(text('SELECT 1')).fetchone())
"
```

### `ModelUnavailableError` (HTTP 503)

```bash
curl http://localhost:11434/api/tags    # is Ollama running?
ollama list                            # is the model downloaded?
ollama pull llama3                     # re-pull if missing
```

### `ValueError: Received empty SQL from model`

The model returned an out-of-scope signal (`OUT_OF_SCOPE` sentinel). Check the log:

```bash
grep OUT_OF_SCOPE logs/query_history.jsonl | tail -5
```

If the question is legitimately in-domain, add a few-shot example that covers it.
