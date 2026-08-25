# Local SQL Agent — Tutorial

> **[فارسی](../fa/tutorial.md)**

This tutorial is written in the vignette style used by tidyverse packages: instead of listing every API surface, it walks you through real tasks end-to-end. By the time you finish, you will have installed the engine, run your first Persian-language query, extended the knowledge base with a new table, diagnosed a retrieval miss, and written a test for each layer.

---

## Table of contents

1. [Installation](#1-installation)
2. [Your first query](#2-your-first-query)
3. [Understanding the pipeline](#3-understanding-the-pipeline)
4. [How retrieval works](#4-how-retrieval-works)
5. [How the prompt is built](#5-how-the-prompt-is-built)
6. [The SQL security pipeline](#6-the-sql-security-pipeline)
7. [Exporting results](#7-exporting-results)
8. [Adding a new table](#8-adding-a-new-table)
9. [Adding synonyms and aliases](#9-adding-synonyms-and-aliases)
10. [Adding few-shot examples](#10-adding-few-shot-examples)
11. [Adding business rules](#11-adding-business-rules)
12. [Diagnosing retrieval misses](#12-diagnosing-retrieval-misses)
13. [Using the HTTP API](#13-using-the-http-api)
14. [Query cache](#14-query-cache)
15. [Health check and monitoring](#15-health-check-and-monitoring)
16. [Writing tests](#16-writing-tests)
17. [Configuration reference](#17-configuration-reference)
18. [Troubleshooting](#18-troubleshooting)

---

## 1. Installation

The engine needs three things outside Python: an OpenAI-compatible LLM endpoint (vLLM / LM Studio / Ollama `/v1`), a SQL Server database, and an ODBC driver for the connection.

### What you need

| Dependency | Minimum | Notes |
|---|---|---|
| Python | 3.11 | |
| OpenAI-compatible LLM endpoint | any | e.g. vLLM, LM Studio, or Ollama's `/v1` API, reachable via `OPENAI_BASE_URL` |
| SQL Server | 2016+ | Any edition including Express |
| ODBC Driver | 17 or 18 | `msodbcsql17` / `msodbcsql18` |

### Clone and install

```bash
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent

python -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
```

Open `.env`. The required values are your database URL and the LLM endpoint configuration:

```dotenv
# Required
DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/YourDB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
OPENAI_BASE_URL=http://your-llm-host:8000/v1
OPENAI_MODEL=gpt-oss-20:F16
OPENAI_API_KEY=your-key

# Sensible defaults — override as needed
MAX_ROWS_RETURNED=1000
QUERY_TIMEOUT_SECONDS=60
CACHE_TTL_SECONDS=300
CACHE_MAX_SIZE=256
```

`OPENAI_BASE_URL` must point at any server exposing the OpenAI-compatible chat API (`/chat/completions`) — vLLM, LM Studio, Ollama (`/v1`), etc. The model you name in `OPENAI_MODEL` must be served by that endpoint.

### Model availability

Start with a model the endpoint already serves (e.g. `gpt-oss-20:F16`). Pick a larger model only if you see the engine producing wrong table names or malformed SQL on your real questions.

---

## 2. Your first query

Let's run the engine and see what actually happens.

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

Type a question — Persian, English, or mixed:

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

════════════════════════════════════════════════════════════
QUERY RESULT
════════════════════════════════════════════════════════════
Name                      PurchaseValue
────────────────────────  ─────────────
شرکت آلفا                   4820000000
شرکت بتا                    3910000000
...

Returned Rows: 5  |  Execution Time: 1.38s
Excel saved → exports/result_20260613_142257.xlsx
```

Type `exit` to quit.

### Over HTTP

```bash
# Start the API server first
uvicorn api.server:app --host 0.0.0.0 --port 8000

# In another terminal
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "top 5 customers by purchase value in 1402", "mode": "full"}'
```

```json
{
  "question": "top 5 customers by purchase value in 1402",
  "sql":    "SELECT TOP 5 c.Name, SUM(cc.TotalPrice) AS PurchaseValue ...",
  "result": [
    {"Name": "شرکت آلفا", "PurchaseValue": 4820000000},
    ...
  ],
  "row_count": 5,
  "status": "SUCCESS"
}
```

### From Python

```python
import requests

r = requests.post(
    "http://localhost:8000/query",
    json={"question": "top 5 customers by purchase value in 1402"},
)
data = r.json()
print(data["sql"])        # the generated T-SQL
print(data["row_count"])  # 5
print(data["result"][0])  # first row as dict
```

---

## 3. Understanding the pipeline

The most important thing to understand about this engine: **the LLM never sees your full schema.** Before the model is called, six independent retrievers build a tight, question-specific context. The LLM receives that — not a dump of your entire schema. This scoping is why locally-run 8–20 B models are accurate enough for production use.

```
Question (Persian / English / mixed)
    │
    ▼
 ContextRetriever
    ├─ EntityRetriever        alias match → TF-IDF fallback
    ├─ FactRetriever          keyword match → TF-IDF fallback
    ├─ RelationshipRetriever  JOIN clauses for selected tables
    ├─ RuleRetriever          domain business rules by keyword
    ├─ ExampleRetriever       tag-scored few-shot SQL examples
    └─ ValueRetriever         ring canonical name + Persian year
    │
    ▼
 PromptBuilder  →  schema + rules + examples, precisely scoped
    │
    ▼
 SQLAgent  →  generate → clean → validate → auto-correct (up to N retries)
    │
    ▼
 SQLGuard  →  blocks DDL/DML/injection, rewrites LIMIT→TOP
    │
    ▼
 SQL Server  →  DataFrame  →  Excel / CSV / JSON
```

---

## 4. How retrieval works

### The six retrievers — a traced example

Let's follow a real question through the pipeline:

```
Question: "برترین مشتریان تالار پتروشیمی در 1402"
          │
          ├─ EntityRetriever
          │     "مشتری" is an alias in entities.py  →  ["Customer"]
          │
          ├─ FactRetriever
          │     "خرید" / "مشتری" match FACT_PATTERNS  →  ["CustomerContract"]
          │
          ├─ RelationshipRetriever
          │     selected tables: {Customer, CustomerContract, Date}
          │     → ["JOIN [Auction_Dim].[Customer] ON ..."]
          │       ["JOIN [Auction_Dim].[Date]     ON ..."]
          │
          ├─ RuleRetriever
          │     "مشتری" / "خرید" match rule keys
          │     → ["ارزش خرید از CustomerContract.TotalPrice محاسبه می‌شود."]
          │
          ├─ ExampleRetriever
          │     inferred tags: {customer, top, value, purchase, ring}
          │     → top-3 examples by tag overlap score
          │
          └─ ValueRetriever
                "تالار پتروشیمی" → RING_ALIASES hit → "Petrochemical"
                "1402"           → Persian year regex → {PersianYear: 1402}
```

All six outputs are packaged into a `RetrievalContext` dataclass and handed to `PromptBuilder`.

### Two-tier retrieval

Every retriever uses the same strategy: **fast path first, TF-IDF fallback second**.

- **Fast path:** exact alias or keyword match against `knowledge/entities.py`, `knowledge/aliases.py`, or hardcoded patterns. O(1).
- **TF-IDF fallback:** if the fast path returns nothing, `schema_data/retriever.py` scores all table descriptions against the question using bigram TF-IDF. Handles novel phrasing.

You can call the TF-IDF engine directly to debug retrieval:

```python
from schema_data.retriever import retrieve_tables

print(retrieve_tables("فروش ماهانه مشتریان"))
# ['CustomerContract', 'Customer', 'Date']

# fallback=False → return [] when nothing scores above threshold
print(retrieve_tables("xyzzy nonsense", fallback=False))
# []
```

### Forced tables

Some tables must always appear regardless of TF-IDF score. The `_ALWAYS_INCLUDE` dict handles this:

```python
# schema_data/retriever.py
_ALWAYS_INCLUDE = {
    "سال":   ["Date"],
    "ماه":   ["Date"],
    "تاریخ": ["Date"],
}
```

Any question containing the word `سال` or `ماه` always gets the `Date` table in context.

### Tuning the TF-IDF engine

```python
# schema_data/retriever.py
_TOP_N     = 6      # max tables returned
_MIN_SCORE = 0.01   # discard tables scoring below this
```

Raise `_MIN_SCORE` for stricter retrieval (less noise). Lower it for broader retrieval (more context for the LLM on ambiguous questions).

---

## 5. How the prompt is built

Once `ContextRetriever` has assembled the `RetrievalContext`, `PromptBuilder.build()` turns it into the final prompt string. Call it directly to inspect exactly what the model sees:

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
    business_rules=["ارزش خرید از CustomerContract.TotalPrice محاسبه می‌شود."],
    examples=[
        {
            "question": "برترین مشتریان",
            "sql":      "SELECT TOP 10 c.Name, SUM(cc.TotalPrice) AS PurchaseValue ...",
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
ارزش خرید از CustomerContract.TotalPrice محاسبه می‌شود.

## Schema
Table: CustomerContract
  TotalPrice  Purchase total value
  ...

## Relationships
JOIN [Auction_Dim].[Customer] ON ...

## Filters
PersianYear = 1402

## Examples
Q: برترین مشتریان
A: SELECT TOP 10 c.Name, SUM(cc.TotalPrice) ...

## Question
برترین مشتریان از نظر ارزش خرید
```

This structure — not a raw schema dump — is why small local models produce correct SQL.

---

## 6. The SQL security pipeline

Every SQL string — from the model, from a test, from anywhere — must pass through three functions in `security/sql_guard.py` before it can reach the database.

```python
from security.sql_guard import clean_sql, validate_sql, ensure_top

# ── Step 1: clean ──────────────────────────────────────────────────────────
# Strips markdown fences, preamble prose, converts LIMIT→TOP
raw = """
Here is the SQL query you requested:
```sql
SELECT * FROM [Auction_Fact].[Contract] LIMIT 10
```
"""
sql = clean_sql(raw)
print(sql)
# SELECT TOP 10 * FROM [Auction_Fact].[Contract]

# ── Step 2: validate ───────────────────────────────────────────────────────
# Raises ValueError on any forbidden pattern
validate_sql(sql)  # passes — it's a SELECT

try:
    validate_sql("DROP TABLE [Auction_Fact].[Contract]")
except ValueError as e:
    print(e)
    # Forbidden SQL keyword detected: DROP

# ── Step 3: ensure TOP ─────────────────────────────────────────────────────
# Injects TOP n when absent; leaves existing TOP unchanged
print(ensure_top("SELECT Name FROM Customer", n=500))
# SELECT TOP 500 Name FROM Customer

print(ensure_top("SELECT TOP 10 Name FROM Customer", n=500))
# SELECT TOP 10 Name FROM Customer   ← unchanged
```

### What is blocked

| Category | Keywords |
|---|---|
| DDL | `DROP`, `ALTER`, `CREATE`, `TRUNCATE` |
| DML | `DELETE`, `UPDATE`, `INSERT`, `MERGE` |
| Execution | `EXECUTE`, `EXEC`, `XP_`, `SP_` |
| Schema introspection | `INFORMATION_SCHEMA`, `SYS.` |
| Stacked queries | `;` followed by a new statement |

`LIMIT` is **not blocked** — it is automatically rewritten to `TOP n` for SQL Server compatibility. `validate_sql` also enforces that every statement starts with `SELECT` or `WITH`; anything else is rejected before it reaches the database.

---

## 7. Exporting results

The CLI saves every successful result to Excel automatically. For programmatic use:

```python
from database.executor import execute_sql
from exporters.excel_exporter import export_excel

df = execute_sql("SELECT TOP 100 * FROM [Auction_Fact].[Contract]")
print(f"{len(df)} rows, {len(df.columns)} columns")

# Auto-fitted columns, timestamped filename
path = export_excel(df)
print(path)  # exports/result_20260613_160000.xlsx
```

All exports land in `EXPORT_DIR` (default: `exports/`). The directory is created automatically on first use. Column widths are capped at 60 characters.

---

## 8. Adding a new table

Imagine your exchange starts clearing trades for a new instrument and you need to add a `Broker` dimension table. This is a five-step process — configuration files only, no engine code changes.

### Step 1 — Describe the table (bilingual)

`schema_data/tables.py`:

```python
TABLE_DESCRIPTIONS: dict[str, str] = {
    # ... existing entries ...
    "Broker": (
        "Registered brokerage firms (کارگزاری‌ها) licensed to execute trades on the exchange. "
        "Contains broker code, full registered name, and license status. "
        "برای فیلتر یا گروه‌بندی بر اساس کارگزار یا کارمزد از این جدول استفاده کنید."
    ),
}
```

> **Always write descriptions bilingually.** The TF-IDF engine tokenises both Persian and English. A bilingual description means the fallback retriever finds this table whether the user asks in Persian or English.

### Step 2 — Define columns

`schema_data/columns.py`:

```python
TABLE_COLUMNS: dict[str, dict[str, str]] = {
    # ...
    "Broker": {
        "BrokerID":   "Surrogate primary key",
        "BrokerCode": "Exchange-assigned numeric code (کد کارگزاری)",
        "BrokerName": "Full registered company name (نام کارگزاری)",
        "IsActive":   "1 = active license, 0 = suspended or revoked",
    },
}
```

### Step 3 — Register the JOIN

`schema_data/relationships.py`:

```python
RELATIONSHIPS: dict[str, str] = {
    # ...
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
    "table":   "Broker",
}
```

### Step 5 — Verify

```bash
python -c "
from schema_data.retriever import retrieve_tables
result = retrieve_tables('فروش کارگزاران در 1402')
print(result)
assert 'Broker' in result
print('OK')
"
```

If `Broker` is missing, add more Persian tokens to its description or add synonyms — see the next section.

---

## 9. Adding synonyms and aliases

The retriever misses a table when users phrase a question using a word that appears in neither the table description nor any alias list.

**Scenario:** analysts say `عرضه کالا` but the `Offer` table is never retrieved.

```python
# Diagnose
from schema_data.retriever import retrieve_tables
from knowledge.aliases import SYNONYMS

print(retrieve_tables("عرضه کالا", fallback=False))  # []
print("عرضه" in SYNONYMS)                             # False
```

**Fix:** add to `knowledge/aliases.py`:

```python
SYNONYMS: dict[str, list[str]] = {
    # ...
    "عرضه":  ["Offer", "offer", "supply", "عرضه کالا", "عرضه‌کننده"],
    "تقاضا": ["demand", "bid", "Bid"],
}
```

**Verify:**

```bash
python -c "
from schema_data.retriever import retrieve_tables
result = retrieve_tables('حجم عرضه کالا تالار پتروشیمی')
assert 'Offer' in result
print('OK')
"
```

### Trading hall canonical names

For trading hall names specifically, use `RING_ALIASES`. The `ValueRetriever` maps any variant to the canonical name before injecting it as a SQL filter:

```python
# knowledge/aliases.py
RING_ALIASES["تالار برق"] = [
    "برق", "تالار برق", "رینگ برق", "بازار برق", "انرژی برق"
]
```

---

## 10. Adding few-shot examples

Few-shot examples are the single highest-leverage improvement you can make to SQL accuracy. When `ExampleRetriever` finds examples whose tags overlap with the question's inferred tags, those examples are injected verbatim into the prompt — giving the LLM a concrete SQL pattern to follow.

`knowledge/examples.py`:

```python
EXAMPLES: list[dict] = [
    # ...
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

Use **small, reusable tags** rather than long phrases. The retriever scores by tag-set intersection, so broader tags match more questions.

| Good tags | Why |
|---|---|
| `broker`, `top`, `value` | Reusable across many question patterns |
| `month`, `count`, `customer` | Combine naturally with other tags |

| Avoid | Why |
|---|---|
| `"top 5 brokers by trade value in 1402"` | Too specific — only matches identical question |
| Single-character or stop words | No signal |

---

## 11. Adding business rules

Business rules are injected verbatim into the prompt whenever `RuleRetriever` detects a matching keyword in the question. They correct systematic model errors — wrong column names, wrong tables, wrong aggregation logic — without any fine-tuning.

`knowledge/business_rules.py`:

```python
BUSINESS_RULES: dict[str, str] = {
    # ...
    "broker": (
        "Broker commission (کارمزد کارگزاری) is stored in Contract.BrokerFee, "
        "not in a separate table. "
        "Always join [Auction_Dim].[Broker] via Contract.BuyBroker_ID."
    ),
    "electricity": (
        "Electricity trades in تالار انرژی only. "
        "Unit of measurement is megawatt-hour (مگاوات‌ساعت). "
        "Use Ring.RingName = 'Electricity' as the filter."
    ),
}
```

The key is matched case-insensitively against the full question text. Keep keys short (single English words or short phrases) so they fire broadly across paraphrases.

---

## 12. Diagnosing retrieval misses

A **retrieval miss** is when the model generates SQL referencing a table the retriever never included in context. The model guessed — sometimes correctly, often not. Left unaddressed, misses erode user trust.

```bash
python scripts/analyze_misses.py
# default: logs/query_history.jsonl

python scripts/analyze_misses.py /path/to/other.jsonl
```

Sample output:

```
🔍  3 miss event(s) found

──────────────────────────────────────────────────────────
Table : Broker  (missed 2×)
  candidate token: 'کارگزار'   (freq=2)  ← add to SYNONYMS
  candidate token: 'بورس'      (freq=1)  ← add to TABLE_DESCRIPTIONS

Table : Ring    (missed 1×)
  candidate token: 'تالار'      (freq=1)  ← add to RING_ALIASES
──────────────────────────────────────────────────────────
```

**How to act on the output:**

| What you see | Fix |
|---|---|
| Token not in `SYNONYMS` | Add it to `knowledge/aliases.py` |
| Table not in `TABLE_DESCRIPTIONS` | Add it to `schema_data/tables.py` |
| Hall name variant unrecognised | Add it to `RING_ALIASES` |
| Table retrieved correctly but SQL is wrong | Add a few-shot example |

Programmatic use:

```python
from pathlib import Path
from scripts.analyze_misses import analyse, _build_report

report = _build_report(analyse(Path("logs/query_history.jsonl")))

for entry in report["tables_ranked_by_miss_count"]:
    print(f"{entry['table']}: {entry['miss_count']} misses")
    for cand in entry["top_candidates"][:3]:
        print(f"  → add alias: '{cand['token']}'")
```

---

## 13. Using the HTTP API

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

| `mode` | Behaviour |
|---|---|
| `full` (default) | Generate SQL, execute it, return SQL + rows |
| `sql` | Generate and return SQL only — do not execute |
| `result` | Generate + execute — return rows only |

### Error responses

| Exception | HTTP | When |
|---|---|---|
| `OutOfScopeError` | 422 | Model returns `OUT_OF_SCOPE` sentinel |
| `ModelTimeoutError` | 504 | LLM request exceeded timeout |
| `ModelUnavailableError` | 503 | LLM endpoint unreachable after all retries |
| `QueryExecutionError` | 500 | SQL Server execution failure |
| `ValidationError` | 422 | Malformed request body |

---

## 14. Query cache

Identical `(question, mode)` pairs are served from an in-process LRU + TTL cache — no LLM call, no database hit.

```bash
# Inspect cache state
curl http://localhost:8000/cache/stats
# {"size": 4, "hits": 17, "misses": 4, "evictions": 0}

# Remove one specific entry
curl -X POST http://localhost:8000/cache/invalidate \
  -H 'Content-Type: application/json' \
  -d '{"question": "فروش ماهانه", "mode": "full"}'

# Flush everything
curl -X POST http://localhost:8000/cache/clear
```

Cache behaviour is controlled by two `.env` settings:

```dotenv
CACHE_TTL_SECONDS=300   # entries expire after 5 minutes
CACHE_MAX_SIZE=256      # oldest entry evicted when full (LRU)
```

Set `CACHE_TTL_SECONDS=0` to disable caching entirely — useful during development or when testing new examples.

---

## 15. Health check and monitoring

```bash
curl http://localhost:8000/health
```

```json
{
  "status":   "ok",
  "database": true,
  "openai":   true,
  "model":    "gpt-oss-20:F16"
}
```

| `status` | Meaning |
|---|---|
| `ok` | Both SQL Server and the LLM endpoint are reachable |
| `degraded` | One component is unreachable |
| `down` | Both components are down |

Every query is appended to `logs/query_history.jsonl` as a single JSON line:

```json
{
  "timestamp":              "2026-06-13T14:22:57",
  "question":               "برترین مشتریان در 1402",
  "generated_sql":          "SELECT TOP 10 c.Name ...",
  "tables_retrieved":       ["CustomerContract", "Customer", "Date"],
  "model_name":             "openai:gpt-oss-20:F16",
  "row_count":              10,
  "execution_time_seconds": 1.38,
  "status":                 "SUCCESS",
  "excel_file":             "exports/result_20260613_142257.xlsx"
}
```

Each HTTP request also receives a correlation ID in `X-Request-Id` and execution time in `X-Response-Time-Ms`.

Feed this log to `analyze_misses.py` regularly to catch retrieval gaps before users notice them.

---

## 16. Writing tests

Tests live in `tests/`. The suite uses `pytest`; shared fixtures are in `tests/conftest.py`. The suite has 427+ tests across unit and integration levels.

### Retriever tests

```python
# tests/test_retriever.py
from schema_data.retriever import retrieve_tables

class TestRetrieveTables:
    def test_customer_retrieved_for_buyer_question(self):
        assert "Customer" in retrieve_tables("برترین خریداران")

    def test_date_forced_whenever_year_mentioned(self):
        # _ALWAYS_INCLUDE guarantees Date on any year/month keyword
        assert "Date" in retrieve_tables("فروش سالیانه در 1402")

    def test_fallback_false_returns_empty_on_noise(self):
        assert retrieve_tables("xyzzy nonsense", fallback=False) == []

    def test_bilingual_parity(self):
        fa = retrieve_tables("مشتریان برتر")
        en = retrieve_tables("top customers")
        assert "Customer" in fa
        assert "Customer" in en
```

### SQL guard tests

```python
# tests/test_sql_guard.py
import pytest
from security.sql_guard import clean_sql, validate_sql, ensure_top

class TestCleanSql:
    def test_strips_markdown_fence(self):
        assert clean_sql("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_rewrites_limit_to_top(self):
        assert clean_sql("SELECT * FROM T LIMIT 5") == "SELECT TOP 5 * FROM T"

    def test_strips_preamble_prose(self):
        assert clean_sql("Here is the query:\nSELECT 1") == "SELECT 1"

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

    def test_valid_select_passes(self):
        validate_sql("SELECT TOP 10 Name FROM [Auction_Dim].[Customer]")

class TestEnsureTop:
    def test_injects_top_when_absent(self):
        result = ensure_top("SELECT Name FROM Customer", n=50)
        assert "TOP 50" in result.upper()

    def test_preserves_existing_top(self):
        sql = "SELECT TOP 10 Name FROM Customer"
        assert ensure_top(sql, n=50) == sql
```

### API tests

```python
# tests/test_api_endpoints.py
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from api.server import app

client = TestClient(app)

def test_query_returns_200_with_sql_key():
    mock = MagicMock()
    mock.sql = "SELECT TOP 5 Name FROM Customer"
    mock.df.to_dict.return_value = []
    mock.df.__len__ = lambda s: 0

    with patch("api.runner.run_query", return_value=mock):
        r = client.post("/query", json={"question": "top customers"})

    assert r.status_code == 200
    assert "sql" in r.json()

def test_health_ok_when_both_up():
    with patch("api.health._ping_db",    return_value=True), \
         patch("api.health._ping_openai", return_value=True):
        r = client.get("/health")
    assert r.json()["status"] == "ok"

def test_health_degraded_when_db_down():
    with patch("api.health._ping_db",    return_value=False), \
         patch("api.health._ping_openai", return_value=True):
        r = client.get("/health")
    assert r.json()["status"] == "degraded"
```

### Running the suite

```bash
pytest                                   # all tests
pytest tests/test_sql_guard.py -v        # one module, verbose
pytest -k "retriever" -v                # keyword filter
pytest --cov=. --cov-report=html         # with coverage
open htmlcov/index.html
```

---

## 17. Configuration reference

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (vLLM / LM Studio / Ollama `/v1`) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model served by the endpoint |
| `OPENAI_API_KEY` | *(required)* | API key for the endpoint |
| `DB_CONNECTION_URL` | *(required)* | SQLAlchemy connection string |
| `QUERY_TIMEOUT_SECONDS` | `60` | Abort SQL queries that run longer than this |
| `MAX_ROWS_RETURNED` | `1000` | Hard row cap injected as `TOP n` on every query |
| `CACHE_TTL_SECONDS` | `300` | Cache entry lifetime in seconds (`0` = disabled) |
| `CACHE_MAX_SIZE` | `256` | Max cached entries; oldest evicted on overflow (LRU) |
| `LOG_DIR` | `logs` | Log directory — auto-created on first use |
| `EXPORT_DIR` | `exports` | Export directory — auto-created on first use |
| `DEFAULT_TOP_N` | `100` | Fallback `TOP n` when model omits it |

All settings are read at startup via `config.py → Settings`. To override in tests:

```python
from config import override_settings

with override_settings(MAX_ROWS_RETURNED=10, CACHE_TTL_SECONDS=0):
    result = run_query("top customers")
    # MAX_ROWS_RETURNED=10 is active only inside this block
```

---

## 18. Troubleshooting

### A table is never retrieved

```python
from schema_data.tables import TABLE_DESCRIPTIONS
from knowledge.aliases import SYNONYMS
from schema_data.retriever import retrieve_tables

print("Broker" in TABLE_DESCRIPTIONS)          # False → add to tables.py
print(SYNONYMS.get("کارگزار"))                   # None  → add to aliases.py
print(retrieve_tables("کارگزاران برتر", fallback=True))
```

### Generated SQL references wrong tables or columns

1. Add a few-shot example for that question pattern → `knowledge/examples.py`
2. Add or tighten the business rule → `knowledge/business_rules.py`
3. Upgrade to a larger model served by the endpoint (e.g. `gpt-oss-20:F16`)

### `RuntimeError: Database connection failed`

```bash
# Health endpoint first
curl http://localhost:8000/health

# Test the connection string directly
python -c "
from database.connection import get_engine
from sqlalchemy import text
with get_engine().connect() as c:
    print(c.execute(text('SELECT 1')).fetchone())
"
```

### `503 ModelUnavailableError`

```bash
curl http://your-llm-host:8000/v1/models   # is the LLM endpoint reachable?
# then verify OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY in .env
```

### `OUT_OF_SCOPE` response

The model decided the question is outside the domain. Check the log:

```bash
grep OUT_OF_SCOPE logs/query_history.jsonl | tail -5
```

Fix: add a few-shot example that shows the correct SQL for that question type.

### `ValueError: Received empty SQL from model`

The model returned prose instead of SQL. Common causes:

- Model too small for the join complexity → use a larger model served by the endpoint
- System prompt too restrictive → review `prompts/system_prompt.md`
- No relevant few-shot example → add one to `knowledge/examples.py`
