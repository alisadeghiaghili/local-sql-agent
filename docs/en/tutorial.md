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

pip install -r requirements.lock
```

`requirements.lock` holds the exact, audited pins; `requirements.txt` has only
floors and is fine for a quick local try.

### Configure

```bash
cp .env.example .env
```

Open `.env`. The required values are your database URL and the LLM endpoint configuration:

```dotenv
# Required
DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/YourDB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
DB_PASSWORD=your-database-password
OPENAI_BASE_URL=http://your-llm-host:8000/v1
OPENAI_MODEL=gpt-oss-20:F16
OPENAI_API_KEY=your-key

# Sensible defaults — override as needed
MAX_ROWS_RETURNED=1000
QUERY_TIMEOUT_SECONDS=60
CACHE_TTL_SECONDS=300
CACHE_MAX_SIZE=256
```

`OPENAI_BASE_URL` must point at any server exposing the OpenAI-compatible chat API (`/chat/completions`) — vLLM, LM Studio, Ollama (`/v1`), etc. The model you name in `OPENAI_MODEL` must be served by that endpoint. A local server usually checks no credential, so `OPENAI_API_KEY` may stay empty.

`DB_PASSWORD` is the raw password for `DB_CONNECTION_URL`, exactly as the database knows it and with no URL encoding (`p@ss/w:rd#1` stays that way); leave the password out of the URL. A password written inside the URL must be percent-encoded by hand (`@` becomes `%40`), and giving it in both places is refused at start-up.

The domain itself (schema, aliases, rules, examples, system prompt) is not in `.env`. Copy the template directory and fill it in; the server will not start without it:

```bash
cp -r project_config.example project_config
```

Two optional helpers draft files from the live database: `python -m database.schema_inspector_cli` drafts `schema.yaml`, and the setup wizard `python setup_project.py` drafts `entities.yaml`, `aliases.yaml`, `business_rules.yaml` and `examples.yaml`. Both give drafts to review, not files to deploy as they are (`docs/deployment-runbook.md` §2.1 to §2.3). The whole first install, in order and with a "done when" check for each step, is "First install, in order" at the top of that runbook.

**More than one database?** `DB_CONNECTION_URL` covers one warehouse connection, which is what most deployments need. To query a second database or server, describe each source in `project_config/datasources.yaml` instead (start from `project_config.example/datasources.example.yaml`: host, database and login per source, and the raw password in one `DB_PASSWORD_*` variable per source). Each table in `schema.yaml` then says where it lives with `datasource: <name>` (a list for a table that exists in several sources, such as `datasource: [sales, inventory]`), and `python scripts/assign_datasources.py` writes those lines for you. With several sources each question is routed to one source before the prompt is built; `description:` and `keywords:` in `datasources.yaml` help that routing, `PROMPT_RETRIEVAL_TOKEN_BUDGET` applies per source (`python scripts/prompt_budget.py` sizes it), and `nolock: true` adds `WITH (NOLOCK)` for a source where the DBA requires it (set per source: it does not spread to the others). The steps in order, with the commands, are `docs/deployment-runbook.md` §16; why it works this way is `docs/design/DATASOURCES.md`.

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
============================================================
 Auction NLQ Engine
 Model : gpt-oss-20:F16
 DB    : server:1433/YourDB
============================================================
 Type your question in Persian or English.
 Commands: exit | quit | Ctrl+C
============================================================

❓ Question:
```

Type a question — Persian, English, or mixed:

```
❓ Question: top 5 customers by purchase value in 2024

============================================================
GENERATED SQL
============================================================
SELECT TOP 5
    c.Name,
    SUM(o.TotalAmount) AS PurchaseValue
FROM [sales].[Order] o
JOIN [sales].[Customer] c ON o.CustomerID = c.ID
JOIN [sales].[Date] d ON o.OrderDate_ID = d.ID
WHERE d.Year = 2024
GROUP BY c.Name
ORDER BY PurchaseValue DESC

📁 Excel saved: exports/result_20260613_142257.xlsx
⏱  Elapsed    : 1.38s

============================================================
QUERY RESULT
============================================================
        Name  PurchaseValue
  شرکت آلفا     4820000000
   شرکت بتا     3910000000
...

Total rows returned: 5
```

The SQL is printed as `generate_sql` returned it (cleaned, validated, and given a row cap if the model wrote none); the CLI does not re-lay it out. It needs no API key and keeps no conversation: every question stands alone. Type `exit` to quit.

### Over HTTP

Every route except `GET /health` needs an API key. Issue one, and put the digest it prints in `API_KEYS_JSON` or in the file `API_KEYS_FILE` names (`docs/deployment-runbook.md` §1 and §2 have the details):

```bash
python -m scripts.issue_api_key --id analyst-1 --name "Jane Analyst"
```

For a throwaway local run you can set `AUTH_REQUIRED=false` instead, which the server logs as a warning on every start-up. Then:

```bash
# Start the API server first
uvicorn api.server:app --host 0.0.0.0 --port 8000

# In another terminal
curl -X POST http://localhost:8000/query \
  -H 'Authorization: Bearer <your-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{"question": "top 5 customers by purchase value in 2024", "mode": "full"}'
```

```json
{
  "question": "top 5 customers by purchase value in 2024",
  "sql":    "SELECT TOP 5 c.Name, SUM(o.TotalAmount) AS PurchaseValue ...",
  "result": [
    {"Name": "شرکت آلفا", "PurchaseValue": 4820000000},
    ...
  ],
  "row_count": 5,
  "correction_attempts": 1,
  "elapsed_seconds": 1.38,
  "model": "openai:gpt-oss-20:F16",
  "llm": { ... }
}
```

`/query` answers one question at a time. For follow-ups that keep their context (`among those…`) use the conversational routes, `/v2/sessions` (`docs/api-contract-v2.md`); a turn there also carries `sql_display`, the statement laid out in a fixed style for reading.

### From Python

```python
import requests

r = requests.post(
    "http://localhost:8000/query",
    headers={"Authorization": "Bearer <your-api-key>"},
    json={"question": "top 5 customers by purchase value in 2024"},
)
data = r.json()
print(data["sql"])        # the generated T-SQL
print(data["row_count"])  # 5
print(data["result"][0])  # first row as dict
```

---

## 3. Understanding the pipeline

The most important thing to understand about this engine: **the model sees a prompt assembled from your configuration, and what is in it depends on how big that configuration is.** Before the model is called, six independent retrievers pick the tables, relationships, rules, examples and filter values that fit the question. Whether the prompt then shows their selection or the whole knowledge base is decided by one number, `PROMPT_RETRIEVAL_TOKEN_BUDGET` (6000 tokens by default, estimated as `len(text) // 4`):

- **Static path (the default for a schema that fits).** The prompt starts with the complete knowledge base: system prompt, full schema, every relationship, rule, metric and example. That prefix is byte-identical for every request, and only a short suffix changes (detected filters, resolved warehouse values, the conversation so far, the question). A local model server can reuse its cache for the prefix instead of reading the schema again for every question, which is where the latency win comes from. The retrievers still run: their output feeds the filters, the value resolution and the choice of data source.
- **Retrieval path (the escape hatch for a large schema).** When the prefix is over the budget, the prompt is built per question from only the tables, relationships, rules and examples the retrievers selected, which keeps a large schema inside a small model's context.

With several data sources the same choice is made per source, after one source has been chosen for the question (see below).

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
    └─ ValueRetriever         hall canonical name + Persian date parts
    │
    ▼
 source selection  (several data sources only: keywords, then the
    │               conversation, then retrieval evidence, then the
    │               default; no model call)
    ▼
 PromptBuilder  →  static prefix (cacheable) + variable suffix,
    │              or, over the budget, only the retrieved tables
    ▼
 SQLAgent  →  generate → clean → validate → auto-correct (up to N retries)
    │
    ▼
 SQLGuard  →  one SELECT, allowlisted tables and columns, column ACL, row cap
    │
    ▼
 SQL Server (the source that has every table)  →  DataFrame  →  Excel / CSV / JSON
```

---

## 4. How retrieval works

### The six retrievers — a traced example

Run a question through the retrievers yourself. With the shipped example config (`PROJECT_CONFIG_DIR=project_config.example`):

```python
from retrieval.context_retriever import ContextRetriever

ctx = ContextRetriever.retrieve("monthly purchase orders per broker in 2024")

print(ctx.entities, ctx.facts)   # ['Broker', 'Date'] ['Order']
print(ctx.relationships)
# ['JOIN [sales].[Date] d ON o.OrderDate_ID = d.ID',
#  'JOIN [ref].[Broker] b ON o.BrokerID = b.ID']
print(len(ctx.business_rules), len(ctx.examples))   # 2 1
```

Each part comes from a different place in `project_config/`:

| Retriever | What it returns | Where its knowledge lives |
|---|---|---|
| `EntityRetriever` | dimension tables the question names | `entities.yaml` (aliases per entity), then the TF-IDF fallback |
| `FactRetriever` | fact tables | `retrieval_hints.yaml` (`fact_tables`, `fact_patterns`), then the TF-IDF fallback |
| `RelationshipRetriever` | the JOIN clauses between the selected tables | `relationships` in `schema.yaml` |
| `RuleRetriever` | business rules for the question's topics | `business_rules.yaml` |
| `ExampleRetriever` | up to three few-shot examples with overlapping tags | `examples.yaml` |
| `ValueRetriever` | canonical filter values (a hall by any of its aliases, a Persian year, month, season) | `aliases.yaml` (`ring_aliases`) and built-in Persian calendar names |

All six outputs are packaged into a `RetrievalContext` dataclass and handed to `PromptBuilder`. On the static path (see §3) the rules and examples it holds do not narrow the prompt, because every rule and example is already in the prefix; the context still supplies the filters and, with several sources, the evidence for choosing a source.

### Two-tier retrieval

The entity and fact retrievers use the same strategy: **fast path first, TF-IDF fallback second**.

- **Fast path:** a substring match of an alias or keyword from `entities.yaml` or `retrieval_hints.yaml` against the lower-cased question.
- **TF-IDF fallback:** if the fast path returns nothing, `schema_data/retriever.py` scores all table descriptions against the question using bigram TF-IDF. Handles novel phrasing.

You can call the TF-IDF engine directly to debug retrieval:

```python
from schema_data.retriever import retrieve_tables

print(sorted(retrieve_tables("monthly purchase orders per broker")))
# ['Broker', 'Date', 'Order']

# fallback=False → return [] when nothing scores above threshold
print(retrieve_tables("xyzzy nonsense", fallback=False))
# []
```

Without `fallback=False` a question that scores nothing gets **every** table back, which is what keeps a vague question from getting an empty schema.

### Forced tables

Some tables must always appear when certain words do, whatever their TF-IDF score. That list is `always_include` in `retrieval_hints.yaml`:

```yaml
# project_config/retrieval_hints.yaml
always_include:
  Date:
    - "date"
    - "year"
    - "month"
```

A question containing `year` or `month` always gets the `Date` table.

### Tuning the TF-IDF engine

```python
# schema_data/retriever.py
_TOP_N     = 6      # max tables returned
_MIN_SCORE = 0.01   # discard tables scoring below this
```

Raise `_MIN_SCORE` for stricter retrieval (less noise). Lower it for broader retrieval (more context for the LLM on ambiguous questions). These two only matter on the retrieval path and for the evidence used to pick a data source.

### Which data source (several sources only)

With more than one data source, one more step runs between retrieval and the prompt: `retrieval/source_selector.py` picks the source the question is about (keywords from `datasources.yaml`, then the conversation, then the tables retrieval found, then the default) so the model is shown that source's tables only. It makes no model call; if the model answers `OUT_OF_SCOPE` anyway, the request is retried once with the next source. The rules are in `docs/design/DATASOURCES.md`, "Choosing a source per question".

---

## 5. How the prompt is built

Once `ContextRetriever` has assembled the `RetrievalContext`, `PromptBuilder.build()` turns it into the final prompt string. Call it directly to inspect exactly what the model sees:

```python
from core.models import RetrievalContext
from prompt_engine.builder import PromptBuilder

context = RetrievalContext(
    entities=["Customer"],
    facts=["Order"],
    dimensions=["Customer"],
    relationships=[
        "JOIN [sales].[Customer] c ON o.CustomerID = c.ID"
    ],
    business_rules=["Purchase value is SUM(Order.TotalAmount)."],
    examples=[
        {
            "question": "Top 10 customers",
            "sql":      "SELECT TOP 10 c.Name FROM [sales].[Customer] c",
        }
    ],
    filters={"Year": 2024},
)

prompt = PromptBuilder.build(
    question="Top customers by purchase value",
    system_prompt="You are a T-SQL expert for SQL Server 2019.",
    context=context,
)
print(prompt)
```

The output is a structured string with clearly labelled sections. The first six are the **static prefix**: with a schema under `PROMPT_RETRIEVAL_TOKEN_BUDGET` they hold everything in `project_config/` (so the `context` you pass changes only the suffix), and they are byte-identical from one question to the next:

```
You are a T-SQL expert for SQL Server 2019.        ← the system prompt

==================================================
BUSINESS RULES                                     ← every rule in business_rules.yaml
==================================================
METRICS                                            ← every metric in metrics.yaml
DATABASE SCHEMA                                    ← every table of schema.yaml
RELATIONSHIPS                                      ← every relationship
EXAMPLES                                           ← every few-shot example
```

and the **variable suffix**, the only part that changes per request:

```
DETECTED FILTERS            ← Year: 2024, plus the instruction to use filter values verbatim
RESOLVED WAREHOUSE VALUES   ← values matched against the live warehouse, fenced as data
SESSION CONTEXT             ← the last turns of a conversation (empty here)
USER QUESTION               ← Top customers by purchase value
```

When the schema is over the budget, the same sections are built per question instead, holding only the tables, relationships, rules and examples the retrievers selected (`context`'s own lists). With several data sources, `PromptBuilder.build(..., source="sales")` describes that source alone: its tables (a table shared with other sources included), the relationships between them, and the examples whose SQL reads only those tables, with `Data source: sales — <description>` printed once above the schema.

This structure, and above all a prefix that never changes, is why small local models are both fast and accurate here.

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
SELECT * FROM [sales].[Order] LIMIT 10
```
"""
sql = clean_sql(raw)
print(sql)
# SELECT TOP 10 * FROM [sales].[Order]

# ── Step 2: validate ───────────────────────────────────────────────────────
# Returns None for an accepted statement; raises ValueError (a
# SqlGuardRejection) for a refused one
validate_sql(sql)  # passes — one SELECT over an allowlisted table

try:
    validate_sql("DROP TABLE [sales].[Order]")
except ValueError as e:
    print(e)
    # Forbidden keyword detected: DROP

try:
    validate_sql("SELECT Name FROM [sales].[Nope]")
except ValueError as e:
    print(e)
    # Forbidden keyword detected: unknown table 'Nope' is not in the schema allowlist

# ── Step 3: ensure TOP ─────────────────────────────────────────────────────
# Injects TOP n when absent; leaves existing TOP unchanged
print(ensure_top("SELECT Name FROM [sales].[Customer]", n=500))
# SELECT TOP 500 Name FROM [sales].[Customer]

print(ensure_top("SELECT TOP 10 Name FROM [sales].[Customer]", n=500))
# SELECT TOP 10 Name FROM [sales].[Customer]   ← unchanged
```

### What is checked

`validate_sql` parses the statement with sqlglot and decides from the syntax tree, not from keywords in the text:

| Rule | What it means |
|---|---|
| One statement | Stacked statements are refused as a class |
| Read-only shape | A `SELECT`/`WITH` root, or a top-level `UNION`/`INTERSECT`/`EXCEPT`; DDL, DML, `EXEC`, `SELECT ... INTO` and `xp_*`/`sp_*`/`OPENROWSET`-style calls are refused wherever they appear |
| Table allowlist | Every table must resolve to a table in `schema.yaml` that has a `columns` map (or be a CTE of the same query), and a schema written in front of a name must match the table's known schema |
| Column allowlist | A qualified column must exist on its table; an unqualified one is allowed rather than risk a false refusal |
| Column ACL | Columns in the caller's `denied_columns` are refused, including through `*` |
| No comments, no catalogues | A comment is refused outright; `INFORMATION_SCHEMA`, `sys.*` and the other dialects' catalogues are refused |
| Functions | A function outside the allowlist, or one that reads server or session state, is refused |
| At least one table | A statement that reads no table is refused |
| One data source | With several sources, a statement for which no source has every table is refused as `cross_datasource` |

Each refusal carries a `reason` (`denied_column`, `unknown_table`, `cross_datasource`, ...) that clients use to pick a next step (`docs/api-contract-v2.md` §4). `LIMIT` is **not refused** — `clean_sql` rewrites it to `TOP n` for SQL Server before validation. The README's "Security model" section has the full list.

### Showing the SQL: `pretty_sql`

The statement that runs is never reformatted. For display only, `security.sql_guard.pretty_sql` lays a statement out in a fixed house style (`security/sql_format.py`: `SELECT` alone on its line, one item per line with a leading comma, aliases and joins in aligned columns), and only when the result parses back to the same tree as the input; otherwise it falls back to sqlglot's printer under the same check, and finally to the input. That text is `sql_display` on a conversation turn, and `rejected_sql_display` for a refused statement.

---

## 7. Exporting results

The CLI saves every successful result to Excel automatically. For programmatic use:

```python
from database.executor import execute_sql
from exporters.excel_exporter import export_excel

df = execute_sql("SELECT TOP 100 * FROM [sales].[Order]")
print(f"{len(df)} rows, {len(df.columns)} columns")

# Auto-fitted columns, timestamped filename
path = export_excel(df)
print(path)  # exports/result_20260613_160000.xlsx
```

All exports land in `EXPORT_DIR` (default: `exports/`). The directory is created automatically on first use. Column widths are capped at 60 characters.

---

## 8. Adding a new table

Imagine the warehouse gains a `Carrier` dimension (the shipping company that delivers an order) and you need analysts to ask about it. This is a four-step process, and every step is a YAML file under `project_config/` — no engine code changes. (The engine's `schema_data/*.py` and `knowledge/*.py` modules are loaders; they hold no data.)

### Step 1 — Describe the table and its columns (bilingual)

`project_config/schema.yaml`:

```yaml
tables:
  # ... existing tables ...
  Carrier:
    description: >-
      ref.Carrier — shipping carriers (شرکت‌های حمل) that deliver orders.
      Carrier code, full name and active flag.
      برای فیلتر یا گروه‌بندی بر اساس شرکت حمل از این جدول استفاده کنید.
    db_schema: "ref"
    columns:
      ID: "Primary key"
      CarrierCode: "Carrier code (کد شرکت حمل)"
      CarrierName: "Full carrier name (نام شرکت حمل)"
      IsActive: "1 = active, 0 = suspended"
```

> **Always write descriptions bilingually.** The TF-IDF engine tokenises both Persian and English. A bilingual description means the fallback retriever finds this table whether the user asks in Persian or English.

`columns` is not only documentation: it is the SQL guard's allowlist. A table with no `columns` key is described in the prompt but every query that reads it is refused. `db_schema` turns on the guard's check of the schema written in a query; give every table one. `schema.yaml` is a security file, so review a change to it like one.

### Step 2 — Register the JOIN, and the foreign-key column

Still in `schema.yaml`: add the new foreign key to the fact table's `columns` (a qualified column the allowlist does not know is refused), and list the join under `relationships`:

```yaml
tables:
  Order:
    columns:
      # ... existing columns ...
      CarrierID: "FK → ref.Carrier — carrier that delivered the order"

relationships:
  # ... existing relationships ...
  - from_table: "Order"
    to_table: "Carrier"
    join_sql: "JOIN [ref].[Carrier] k ON o.CarrierID = k.ID"
```

Each real foreign key is listed once; an edge is offered to the model only when both tables are among those selected for the question.

### Step 3 — Add aliases

`project_config/entities.yaml` maps the words a user types to the table:

```yaml
entities:
  # ... existing entities ...
  Carrier:
    aliases: ["carrier", "shipping company", "حمل‌کننده", "شرکت حمل"]
    table: "Carrier"
```

### Step 4 — Say which data source it is in (several sources only)

With `datasources.yaml`, a table that is not on the default source needs `datasource: <name>` under its key. `python scripts/assign_datasources.py` works the value out from the databases and writes it, with every other table's, to `schema.with_datasources.yaml` for review (`docs/deployment-runbook.md` §16.3).

### Verify

```bash
python scripts/verify_deployment.py     # "project_config/ loads" must PASS
python -c "
from schema_data.retriever import retrieve_tables
result = retrieve_tables('orders per carrier', fallback=False)
print(result)
assert 'Carrier' in result
print('OK')
"
```

A change to `schema.yaml` reaches the guard at the next restart (the allowlist is resolved once, at start-up); the other YAML files can be applied from the admin panel without one. If `Carrier` is missing from the result, add more Persian and English words to its description or add synonyms — see the next section.

---

## 9. Adding synonyms and aliases

The retriever misses a table when users phrase a question using a word that appears in neither the table description nor any alias list.

**Scenario:** analysts say `shipment` but the `Carrier` table is never retrieved.

```python
# Diagnose
from schema_data.retriever import retrieve_tables
from knowledge.aliases import SYNONYMS

print(retrieve_tables("shipment delays", fallback=False))  # no Carrier in the list
print("shipment" in SYNONYMS)                              # False
```

**Fix:** add to `project_config/aliases.yaml` (keys and values must be lowercase):

```yaml
synonyms:
  # ... existing entries ...
  "shipment": ["carrier", "delivery"]
  "demand":   ["bid"]
```

Each value is a canonical token used in table descriptions, so the TF-IDF scoring finds the right table through it.

**Verify** (in a fresh process, since the loaders cache the file):

```bash
python -c "
from schema_data.retriever import retrieve_tables
result = retrieve_tables('shipment delays', fallback=False)
assert 'Carrier' in result
print('OK')
"
```

### Hall canonical names

For trading hall (or any named-value) canonical names, use `ring_aliases` in the same file. `ValueRetriever` maps any variant to the canonical name before injecting it as a SQL filter:

```yaml
ring_aliases:
  "Hall Industrial":
    - "industrial"
    - "hall industrial"
    - "industrial ring"
```

---

## 10. Adding few-shot examples

Few-shot examples are the single highest-leverage improvement you can make to SQL accuracy: they give the model a concrete SQL pattern to follow. On the static path (the default, see §3) every example in `examples.yaml` is in the prompt prefix. On the retrieval path, `ExampleRetriever` injects up to three whose tags overlap with the tags it infers from the question.

`project_config/examples.yaml`:

```yaml
examples:
  # ... existing examples ...
  - tags: ["broker", "top", "purchase", "value", "year"]
    question: "top 5 brokers by purchase value in 2024"
    sql: |
      SELECT TOP 5
          b.PersianName,
          SUM(o.TotalAmount) AS PurchaseValue
      FROM [sales].[Order] o
      JOIN [ref].[Broker] b ON o.BrokerID = b.ID
      JOIN [sales].[Date] d ON o.OrderDate_ID = d.ID
      WHERE d.Year = 2024
      GROUP BY b.PersianName
      ORDER BY PurchaseValue DESC
```

Write the SQL the guard would accept: the same tables, columns and `[schema].[table]` references the model must use. With several data sources, an example whose SQL reads tables of only one source is shown only in that source's prompt.

### Tagging strategy

Use **small, reusable tags**. `ExampleRetriever` infers a question's tags from a fixed vocabulary in `retrieval/example_retriever.py` (`customer`, `supplier`, `broker`, `symbol`, `ring`, `purchase`, `trade`, `offer`, `value`, `volume`, `price`, `count`, `top`, `date`, `year`, `month`, `day`, `distinct`, `average`, `active`, `wage`) and scores an example by how many of those tags it shares. A tag outside that vocabulary never matches on the retrieval path, though the example is still in the static prefix.

| Good tags | Why |
|---|---|
| `broker`, `top`, `value` | In the vocabulary, reusable across many question patterns |
| `month`, `count`, `customer` | Combine naturally with other tags |

| Avoid | Why |
|---|---|
| `"top 5 brokers by purchase value in 2024"` | A whole question is not a tag; it can never match |
| Tags outside the vocabulary | Never inferred from a question, so never matched |

---

## 11. Adding business rules

Business rules correct systematic model errors — wrong column names, wrong tables, wrong aggregation logic — without any fine-tuning. On the static path every rule in `business_rules.yaml` is in the prompt prefix.

`project_config/business_rules.yaml`:

```yaml
rules:
  # ... existing rules ...
  broker:
    rule_text: |
      A broker is the selling agent of an order. Join [ref].[Broker] through
      Order.BrokerID and report [ref].[Broker].PersianName, never the ID.
```

On the retrieval path a rule is selected only when its key is one of the topics `RuleRetriever.RULE_MAPPING` (`retrieval/rule_retriever.py`) knows — `purchase`, `trade`, `offer`, `customer`, `supplier`, `broker`, `symbol`, `date`, `ring` and `topn` — and the question contains one of that topic's trigger words (matched case-insensitively as a substring). A rule under any other key is in the static prefix but is never picked on the retrieval path, so for a deployment whose schema is over the budget, name rules after those topics.

---

## 12. Diagnosing retrieval misses

A **retrieval miss** is when the model generates SQL referencing a table the retriever never included in context. The model guessed — sometimes correctly, often not. Left unaddressed, misses erode user trust.

```bash
python scripts/analyze_misses.py
# default: logs/query_log.jsonl (the CLI's log)

python scripts/analyze_misses.py /path/to/other.jsonl
```

Sample output:

```
🔍  3 miss event(s) detected

------------------------------------------------------------
  Table : Broker  (missed 2x)
    candidate token: 'agent'   (freq=2)
    candidate token: 'exchange'   (freq=1)
  Table : Ring  (missed 1x)
    candidate token: 'hall'   (freq=1)
------------------------------------------------------------
```

**How to act on the output:**

| What you see | Fix |
|---|---|
| A candidate token for a table | Add it to `synonyms` in `project_config/aliases.yaml` (§9), or to the table's `description` in `schema.yaml` |
| Table not described at all | Add it to `schema.yaml` (§8) |
| Hall name variant unrecognised | Add it to `ring_aliases` in `aliases.yaml` |
| Table retrieved correctly but SQL is wrong | Add a few-shot example (§10) |

Programmatic use:

```python
from pathlib import Path
from scripts.analyze_misses import analyse, _build_report

report = _build_report(analyse(Path("logs/query_log.jsonl")))

for entry in report["tables_ranked_by_miss_count"]:
    print(f"{entry['table']}: {entry['miss_count']} misses")
    for cand in entry["top_candidates"][:3]:
        print(f"  → add synonym: '{cand['token']}'")
```

The script reads the CLI's `query_log.jsonl`. A deployment that runs only the HTTP API writes `logs/audit_log.jsonl` instead (a different, aggregate-safe record: `scripts/analyze_audit_log.py` reads it).

---

## 13. Using the HTTP API

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
# Swagger UI: http://localhost:8000/docs  (needs a key, like every route but /health)
```

Every request below carries `Authorization: Bearer <your-api-key>` (§2). `--reload` is for development; a deployment uses the command in `docs/deployment-runbook.md` §4.

### POST /query

```bash
curl -X POST http://localhost:8000/query \
  -H 'Authorization: Bearer <your-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{"question": "monthly purchase orders per broker in 2024", "mode": "full"}'
```

| `mode` | Behaviour |
|---|---|
| `full` (default) | Generate SQL, execute it, return SQL + rows |
| `sql` | Generate and return SQL only — do not execute |
| `result` | Generate + execute — return rows only |

Add `"interpret": true` for a plain-language summary of the rows (it sends up to twenty result rows to the model, and the governance gate still refuses a remote backend without `LLM_ALLOW_REMOTE`). `POST /query/stream` is the same request streamed as Server-Sent Events, and `/v2/sessions` is the conversational API (`docs/api-contract-v2.md`).

### Error responses

| Exception | HTTP | When |
|---|---|---|
| `UnauthenticatedError` | 401 | Missing or invalid API key |
| `OutOfScopeError` | 422 | Model returns `OUT_OF_SCOPE` sentinel (with several data sources, only after one retry on the next source) |
| `EmptySQLResponseError` | 502 | Model finished cleanly and returned nothing |
| `TruncatedSQLResponseError` | 502 | Model hit `LLM_NUM_PREDICT` before emitting any SQL — the usual signature of a reasoning model spending its whole budget thinking. Not retried: nothing about it depends on the question. Raise the cap, or turn reasoning off with `LLM_EXTRA_BODY` |
| `ModelTimeoutError` | 504 | LLM request exceeded timeout |
| `ModelUnavailableError` | 503 | LLM endpoint unreachable after all retries |
| `QueryExecutionError` | 500 | SQL Server execution failure |
| `ValidationError` | 422 | Malformed request body |
| (rate limit) | 429 | Too many requests for this principal and address |

A statement the guard refuses is reported with its `reason` (`docs/api-contract-v2.md` §4); `api/errors.py` has the complete hierarchy.

---

## 14. Query cache

Identical questions in the same mode are served from an in-process LRU + TTL cache — no LLM call, no database hit. The key is the normalised question, the mode, the prompt prefix version and a scope derived from the caller's `denied_columns`, so two principals who can see different data never share an entry. `mode=sql` and `interpret: true` requests are not cached.

```bash
# Inspect cache state
curl -H 'Authorization: Bearer <your-api-key>' http://localhost:8000/cache/stats
# {"hits": 17, "misses": 4, "evictions": 0, "size": 4, ..., "enabled": true}

# Remove one specific entry
curl -X POST http://localhost:8000/cache/invalidate \
  -H 'Authorization: Bearer <your-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{"question": "monthly purchase orders per broker in 2024", "mode": "full"}'

# Flush everything
curl -X POST -H 'Authorization: Bearer <your-api-key>' http://localhost:8000/cache/clear
```

`/cache/invalidate` answers 404 when it finds no entry. It looks the entry up without a caller scope, while `/query` stores one under the caller's scope, so it may report 404 for an answer that is cached; `/cache/clear` always works, and so does the admin panel's clear button.

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
  "openai":   true,
  "database": true,
  "model":    "gpt-oss-20:F16",
  "database_detail": "SELECT 1 succeeded"
}
```

`/health` needs no key. `model` appears only when the caller sent a valid key. `database` is `true` only when `SELECT 1` succeeded on **every** data source; with several, `database_detail` names each (`sales: SELECT 1 succeeded; inventory: ...`). The result is reused for `HEALTH_CACHE_TTL_SECONDS` (default 15).

| `status` | Meaning |
|---|---|
| `ok` | Both the database and the LLM endpoint are reachable |
| `degraded` | One of the two is unreachable |
| `down` | Both are down |

The CLI appends each question to `logs/query_log.jsonl` as a single JSON line:

```json
{
  "timestamp":              "2026-06-13T14:22:57",
  "question":               "top 5 customers by purchase value in 2024",
  "generated_sql":          "SELECT TOP 5 c.Name ...",
  "model_name":             "openai:gpt-oss-20:F16",
  "row_count":              5,
  "execution_time_seconds": 1.38,
  "status":                 "SUCCESS",
  "error_message":          null,
  "excel_file":             "exports/result_20260613_142257.xlsx"
}
```

The HTTP API writes `logs/audit_log.jsonl` instead: principal, guard verdict, timings, the LLM status block, `datasource` (where the SQL ran) and, with several data sources, `datasource_selection`; never result rows. `python scripts/analyze_audit_log.py` aggregates it (`docs/deployment-runbook.md` §8).

Each HTTP request also receives a correlation ID in `X-Request-Id` and execution time in `X-Response-Time` (for example `0.412s`).

Feed the CLI log to `analyze_misses.py` regularly to catch retrieval gaps before users notice them.

---

## 16. Writing tests

Tests live in `tests/`. The suite uses `pytest`; shared fixtures are in `tests/conftest.py`. The suite has more than 5,000 tests across unit and integration levels, and runs against `project_config.example/`:

```bash
PROJECT_CONFIG_DIR=project_config.example pytest tests/ eval/tests -q
```

### Retriever tests

```python
# tests/test_retriever.py
from schema_data.retriever import retrieve_tables

class TestRetrieveTables:
    def test_broker_retrieved_for_broker_question(self):
        assert "Broker" in retrieve_tables("monthly purchase orders per broker", fallback=False)

    def test_date_forced_whenever_year_mentioned(self):
        # always_include in retrieval_hints.yaml guarantees Date on a year keyword
        assert "Date" in retrieve_tables("orders per year", fallback=False)

    def test_fallback_false_returns_empty_on_noise(self):
        assert retrieve_tables("xyzzy nonsense", fallback=False) == []
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
        "DROP TABLE [sales].[Order]",
        "DELETE FROM [sales].[Order] WHERE 1=1",
        "INSERT INTO [sales].[Order] VALUES (1, 2)",
        "ALTER TABLE [sales].[Order] ADD x INT",
        "EXEC xp_cmdshell 'dir'",
    ])
    def test_forbidden_raises(self, bad):
        with pytest.raises(ValueError):
            validate_sql(bad)

    def test_unknown_table_is_refused(self):
        with pytest.raises(ValueError, match="unknown table"):
            validate_sql("SELECT Name FROM [sales].[Nope]")

    def test_valid_select_passes(self):
        validate_sql("SELECT TOP 10 Name FROM [sales].[Customer]")

class TestEnsureTop:
    def test_injects_top_when_absent(self):
        result = ensure_top("SELECT Name FROM [sales].[Customer]", n=50)
        assert "TOP 50" in result.upper()

    def test_preserves_existing_top(self):
        sql = "SELECT TOP 10 Name FROM [sales].[Customer]"
        assert ensure_top(sql, n=50) == sql
```

### API tests

Every route but `/health` needs a key, so the API tests use the `auth_settings` fixture from `tests/conftest.py`, which configures a test key and returns the headers that carry it:

```python
# tests/test_api_endpoints.py
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

@pytest.fixture()
def client(auth_settings):
    import api.server as server
    server._system_prompt = "stub system prompt"   # skip the file load
    return TestClient(server.app, headers=auth_settings)

def test_query_returns_200_with_sql_key(client):
    from api.models import QueryResponse
    reply = QueryResponse(
        question="top customers", sql="SELECT TOP 5 Name FROM [sales].[Customer]",
        result=[], row_count=0,
    )
    with patch("api.runner.run_query", return_value=reply):
        r = client.post("/query", json={"question": "top customers"})
    assert r.status_code == 200
    assert "sql" in r.json()

def test_health_ok_when_both_up(client):
    from api import health
    with patch("api.health._ping_db", return_value=(True, "SELECT 1 succeeded")), \
         patch("api.health._ping_openai", return_value=(True, "ok")):
        health.reset_health_cache()
        r = client.get("/health")
    assert r.json()["status"] == "ok"
```

### Running the suite

```bash
pytest                                   # all tests (testpaths: tests, eval/tests)
pytest tests/test_sql_guard.py -v        # one module, verbose
pytest -k "retriever" -v                # keyword filter
pytest tests/ eval/tests --cov           # with coverage — what CI measures
```

---

## 17. Configuration reference

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (vLLM / LM Studio / Ollama `/v1`) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model served by the endpoint |
| `OPENAI_API_KEY` | *(empty)* | API key for the endpoint; a local server usually needs none |
| `DB_CONNECTION_URL` | *(required)* | SQLAlchemy connection string, without the password — unused, and not required, once `project_config/datasources.yaml` exists (see below) |
| `DB_PASSWORD` | *(empty)* | The raw password for `DB_CONNECTION_URL`, no URL encoding. A password inside the URL must be percent-encoded instead, and setting both is refused |
| `QUERY_TIMEOUT_SECONDS` | `60` | Abort SQL queries that run longer than this |
| `MAX_ROWS_RETURNED` | `1000` | Hard row cap on every result set |
| `DEFAULT_TOP_N` | `MAX_ROWS_RETURNED` | `TOP n` injected when the model's SQL has no row limit |
| `PROMPT_RETRIEVAL_TOKEN_BUDGET` | `6000` | Largest estimated prompt prefix sent whole; larger ones are built from retrieved tables. Per data source when there are several (`python scripts/prompt_budget.py` sizes it) |
| `CACHE_TTL_SECONDS` | `300` | Cache entry lifetime in seconds (`0` = disabled) |
| `CACHE_MAX_SIZE` | `256` | Max cached entries; oldest evicted on overflow (LRU) |
| `API_KEYS_JSON` / `API_KEYS_FILE` | *(empty)* | The API key array, inline or in a file; set one, not both (`docs/deployment-runbook.md` §2) |
| `AUTH_REQUIRED` | `true` | Fail-closed authentication; `false` is a logged escape hatch |
| `LOG_DIR` | `logs` | Log directory — auto-created on first use |
| `EXPORT_DIR` | `exports` | Export directory — auto-created on first use |

Querying more than one database at once? `DB_CONNECTION_URL` above covers exactly one. `project_config/datasources.yaml` (optional; absent means the single-source table above, unchanged) describes each source's connection (host, database, driver, login) and names the `DB_PASSWORD_<NAME>` environment variable holding its raw password; the older `url_env` form still works. Two databases on one server are two sources. A table's `datasource:` in `schema.yaml` names its source, or lists several for a table that exists in each of them (`datasource: [sales, inventory]`); `python scripts/assign_datasources.py` works the values out from the databases and writes them to `schema.with_datasources.yaml` for review. The ordered procedure is `docs/deployment-runbook.md` §16 and the design is `docs/design/DATASOURCES.md`.

`.env.example` documents every setting, and `config.py` carries the reasoning behind each default. All settings are read at start-up via `config.py → Settings`. To override in tests:

```python
from config import override_settings

with override_settings(max_rows_returned=10, cache_ttl_seconds=0):
    ...   # these values are active only inside this block
```

---

## 18. Troubleshooting

### A table is never retrieved

```python
from schema_data.tables import TABLE_DESCRIPTIONS
from knowledge.aliases import SYNONYMS
from schema_data.retriever import retrieve_tables

print("Carrier" in TABLE_DESCRIPTIONS)          # False → add to schema.yaml
print(SYNONYMS.get("shipment"))                 # None  → add to aliases.yaml
print(retrieve_tables("orders per carrier", fallback=False))
```

### Generated SQL references wrong tables or columns

1. Add a few-shot example for that question pattern → `project_config/examples.yaml`
2. Add or tighten the business rule → `project_config/business_rules.yaml`
3. Upgrade to a larger model served by the endpoint (e.g. `gpt-oss-20:F16`)

### The wrong data source answers (several sources)

Read `datasource_selection` in the audit record for that question: `reason` says which signal chose the source (`keyword`, `session`, `retrieval`, `default`) and `fallback_from` shows a retry. Add the missing word to that source's `keywords:`; `docs/deployment-runbook.md` §16.9 has the `grep`.

### `RuntimeError: Database connection failed`

```bash
# Health endpoint first
curl http://localhost:8000/health

# Test the connection of the one source (name it with get_engine("<source>") when there are several)
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
grep OUT_OF_SCOPE logs/query_log.jsonl | tail -5     # the CLI's log
grep OUT_OF_SCOPE logs/audit_log.jsonl | tail -5     # the HTTP API's audit log
```

Fix: add a few-shot example that shows the correct SQL for that question type.

### An empty or prose answer instead of SQL

The model returned prose, or nothing. Common causes:

- A reasoning model spent its token budget thinking: the response is `LLM_OUTPUT_TRUNCATED`. Raise `LLM_NUM_PREDICT` or turn reasoning off with `LLM_EXTRA_BODY` (`.env.example`).
- Model too small for the join complexity → use a larger model served by the endpoint
- System prompt too restrictive → review `<PROJECT_CONFIG_DIR>/system_prompt.md`
- No relevant few-shot example → add one to `project_config/examples.yaml`
