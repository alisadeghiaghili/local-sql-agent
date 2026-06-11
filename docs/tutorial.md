# Local SQL Agent — آموزش کامل

این توتوریال شما را از نصب اولیه تا نوشتن تست، اضافه‌کردن جداول جدید، و بهینه‌سازی بازیابی راهنمایی می‌کند.

---

## فهرست مطالب

1. [معماری پروژه](#1-معماری-پروژه)
2. [نصب و راه‌اندازی](#2-نصب-و-راه‌اندازی)
3. [اولین سوال — end-to-end](#3-اولین-سوال--end-to-end)
4. [لایه بازیابی](#4-لایه-بازیابی)
5. [ساخت پرامپت](#5-ساخت-پرامپت)
6. [لایه امنیت SQL](#6-لایه-امنیت-sql)
7. [اجرای کوئری و خروجی](#7-اجرای-کوئری-و-خروجی)
8. [اضافه‌کردن جدول جدید](#8-اضافه‌کردن-جدول-جدید)
9. [اضافه‌کردن synonym جدید](#9-اضافه‌کردن-synonym-جدید)
10. [آنالیز miss‌های بازیابی](#10-آنالیز-missهای-بازیابی)
11. [نوشتن تست](#11-نوشتن-تست)
12. [بررسی سلامت سرویس](#12-بررسی-سلامت-سرویس)
13. [رفع مشکلات رایج](#13-رفع-مشکلات-رایج)

---

## 1. معماری پروژه

```
local-sql-agent/
├── app.py                   ← FastAPI application & endpoints
├── config.py                ← Settings loaded from .env
│
├── api/                     ← Request/Response models + health check
├── core/
│   └── models.py            ← RetrievalContext dataclass
├── retrieval/
│   └── context_retriever.py ← Orchestrates all sub-retrievers
├── schema_data/
│   ├── tables.py            ← TABLE_DESCRIPTIONS  {name: description}
│   ├── columns.py           ← TABLE_COLUMNS       {name: {col: desc}}
│   ├── relationships.py     ← RELATIONSHIPS       {"A -> B": join_sql}
│   ├── registry.py          ← SchemaRegistry
│   └── retriever.py         ← TF-IDF table retriever
├── knowledge/
│   ├── aliases.py           ← SYNONYMS  {fa_word: [en/fa aliases]}
│   ├── rules.py             ← BUSINESS_RULES
│   └── examples.py          ← FEW_SHOT_EXAMPLES
├── prompt_engine/
├── llm/
├── security/
│   └── sql_guard.py         ← clean_sql / validate_sql / ensure_top
├── database/
├── exporters/
├── logs/
├── scripts/
│   └── analyze_misses.py    ← Offline miss-analysis CLI
└── tests/
```

### جریان یک درخواست

```
کاربر: "فروش ماهانه مشتریان در سال 1402"
         │
         ▼
   [FastAPI /query]
         │
         ▼
   ContextRetriever.retrieve(question)
     ├─ retrieve_tables()        ← TF-IDF روی TABLE_DESCRIPTIONS
     ├─ SchemaRegistry.get_relationships()
     ├─ match business_rules
     ├─ match few-shot examples
     └─ extract filters
         │
         ▼
   PromptBuilder.build()
         │
         ▼
   OllamaBackend.generate(prompt)
         │
         ▼
   clean_sql() → validate_sql() → ensure_top()
         │
         ▼
   execute_sql() → DataFrame → JSON response
```

---

## 2. نصب و راه‌اندازی

### پیش‌نیازها

| ابزار | نسخه حداقل | توضیح |
|---|---|---|
| Python | 3.10+ | |
| Ollama | هر نسخه | باید روی `localhost:11434` باشد |
| SQL Server | 2016+ | از طریق ODBC Driver 17 یا 18 |
| ODBC Driver | 17 یا 18 | `msodbcsql17` / `msodbcsql18` |

### گام 1 — کلون و محیط مجازی

```bash
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent

python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# یا:
.venv\Scripts\activate           # Windows

pip install -r requirements.txt
```

### گام 2 — تنظیم `.env`

```bash
cp .env.example .env
```

فایل `.env` را ویرایش کنید:

```dotenv
# اتصال به SQL Server
DB_CONNECTION_URL=mssql+pyodbc://user:pass@server/Auction_DM?driver=ODBC+Driver+17+for+SQL+Server

# Ollama
OLLAMA_URL=http://localhost:11434/api/generate
OLLAMA_MODEL=llama3

# محدودیت‌ها
MAX_ROWS_RETURNED=500
QUERY_TIMEOUT_SECONDS=30
DEFAULT_TOP_N=100

# لاگ
LOG_PATH=logs/query_log.jsonl
```

### گام 3 — دانلود مدل Ollama

```bash
ollama pull llama3
# یا هر مدلی که در OLLAMA_MODEL تنظیم کردید
ollama pull llama3.1:8b
```

### گام 4 — اجرای سرور

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

- **Swagger UI:** http://localhost:8000/docs  
- **Health check:** http://localhost:8000/health

---

## 3. اولین سوال — end-to-end

### با `curl`

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "فروش ماهانه مشتریان در سال 1402"}'
```

### با Python

```python
import requests

response = requests.post(
    "http://localhost:8000/query",
    json={"question": "فروش ماهانه مشتریان در سال 1402"},
)
data = response.json()

print(data["sql"])        # SQL تولیدشده
print(data["result"])     # نتیجه اجرا
print(data["row_count"])  # تعداد ردیف‌ها
```

### ساختار پاسخ

```json
{
  "question": "فروش ماهانه مشتریان در سال 1402",
  "sql": "SELECT TOP 100 ...",
  "result": [...],
  "row_count": 42,
  "status": "SUCCESS",
  "tables_used": ["Contract", "Customer", "Date"]
}
```

---

## 4. لایه بازیابی

### چگونه جداول انتخاب می‌شوند؟

`retrieve_tables` از TF-IDF روی توضیحات جداول استفاده می‌کند:

```python
from schema_data.retriever import retrieve_tables

# بازیابی جداول مرتبط
tables = retrieve_tables("فروش ماهانه مشتریان")
print(tables)
# ['Contract', 'Customer', 'Date', ...]

# با fallback=False — اگر هیچ match نبود، [] برمی‌گردد
tables = retrieve_tables("xyzzy", fallback=False)
print(tables)  # []
```

### تنظیم پارامترها

در `schema_data/retriever.py`:

```python
_TOP_N      = 6      # حداکثر تعداد جداول بازیابی‌شده
_MIN_SCORE  = 0.01   # حداقل امتیاز TF-IDF
_FORCED_SCORE = 1e9  # امتیاز جداول اجباری
```

### جداول اجباری

برخی جداول مثل `Date` همیشه باید در context باشند:

```python
_ALWAYS_INCLUDE = {
    "سال": ["Date"],
    "ماه": ["Date"],
    "تاریخ": ["Date"],
}
```

---

## 5. ساخت پرامپت

```python
from core.models import RetrievalContext
from prompt_engine.builder import PromptBuilder

context = RetrievalContext(
    entities=["Customer"],
    facts=["Contract"],
    relationships=[
        "JOIN [Auction_Dim].[Customer] "
        "ON [Auction_Fact].[Contract].[CustomerID] = [Auction_Dim].[Customer].[CustomerID]"
    ],
    business_rules=["سال شمسی از فروردین شروع می‌شود."],
    examples=[
        {
            "question": "خرید برتر",
            "sql": "SELECT TOP 10 Name FROM Customer ORDER BY Volume DESC",
        }
    ],
    filters={"PersianYear": 1402},
)

prompt = PromptBuilder.build(
    question="خرید مشتریان در 1402",
    system_prompt="You are a T-SQL expert.",
    context=context,
)
```

### ویرایش قالب پرامپت

فایل `prompt_engine/templates.py`:

```python
PROMPT_TEMPLATE = """
{system_prompt}

## Business Rules
{business_rules}

## Database Schema
{schema}

## Relationships
{relationships}

## Active Filters
{filters}

## Examples
{examples}

## Question
{question}
"""
```

---

## 6. لایه امنیت SQL

هر SQL تولیدشده این pipeline را طی می‌کند:

```python
from security.sql_guard import clean_sql, validate_sql, ensure_top

raw = """
    Here is the SQL:
    ```sql
    SELECT * FROM Contract LIMIT 10
    ```
"""

# گام 1: پاک‌سازی
sql = clean_sql(raw)
print(sql)
# 'SELECT TOP 10 * FROM Contract'

# گام 2: اعتبارسنجی
validate_sql(sql)  # اگر مشکلی باشد ValueError می‌دهد

# گام 3: اطمینان از وجود TOP
sql = ensure_top(sql, n=100)
# تغییری ندارد — TOP قبلاً وجود داشت
```

### کلمات ممنوع

```python
_FORBIDDEN = (
    "DELETE ", "UPDATE ", "INSERT ", "DROP ",
    "ALTER ",  "TRUNCATE ", "MERGE ",  "EXEC ",
    "EXECUTE ", "XP_",     "SP_",
)
```

---

## 7. اجرای کوئری و خروجی

```python
from database.executor import execute_sql

df = execute_sql("SELECT TOP 10 * FROM [Auction_Fact].[Contract]")
print(df.head())
print(f"ردیف: {len(df)}, ستون: {len(df.columns)}")
```

### فرمت‌های خروجی

```python
from exporters.formatter import to_json, to_csv, to_markdown

json_str     = to_json(df)
csv_str      = to_csv(df)
markdown_str = to_markdown(df)
```

### تنظیم محدودیت ردیف

```dotenv
MAX_ROWS_RETURNED=200
QUERY_TIMEOUT_SECONDS=20
```

---

## 8. اضافه‌کردن جدول جدید

فرض کنید می‌خواهیم جدول `Broker` را اضافه کنیم.

### گام 1 — توضیحات جدول

`schema_data/tables.py`:

```python
TABLE_DESCRIPTIONS: dict[str, str] = {
    # ... جداول موجود ...
    "Broker": (
        "Registered brokerage firms (کارگزاری‌ها) licensed to trade on the exchange. "
        "Contains broker code, name, and license status."
    ),
}
```

> **نکته:** توضیحات را **دو زبانه** بنویسید — هم انگلیسی هم فارسی — تا TF-IDF روی هر دو زبان کار کند.

### گام 2 — ستون‌ها

`schema_data/columns.py`:

```python
TABLE_COLUMNS: dict[str, dict[str, str]] = {
    # ... جداول موجود ...
    "Broker": {
        "BrokerID":   "Surrogate primary key",
        "BrokerCode": "Exchange-assigned broker code (کد کارگزاری)",
        "BrokerName": "Full registered name (نام کارگزاری)",
        "IsActive":   "1 = active license, 0 = suspended",
    },
}
```

### گام 3 — روابط (اختیاری)

`schema_data/relationships.py`:

```python
RELATIONSHIPS: dict[str, str] = {
    # ... روابط موجود ...
    "Contract -> Broker": (
        "JOIN [Auction_Dim].[Broker] "
        "ON [Auction_Fact].[Contract].[BrokerID] = [Auction_Dim].[Broker].[BrokerID]"
    ),
}
```

### گام 4 — تست

```bash
python -c "
from schema_data.retriever import retrieve_tables
print(retrieve_tables('فروش کارگزاران'))
"
# باید 'Broker' در نتیجه باشد
```

---

## 9. اضافه‌کردن synonym جدید

اگر کاربران از «عرضه» استفاده می‌کنند ولی جدول `Offer` پیدا نمی‌شود:

```python
# knowledge/aliases.py
SYNONYMS: dict[str, list[str]] = {
    # ... موجود ...
    "عرضه": ["Offer", "offer", "supply", "عرضه کالا"],
    "تقاضا": ["demand", "bid", "Bid"],
}
```

بعد اضافه‌کردن، تست کنید:

```bash
python -c "
from schema_data.retriever import retrieve_tables
print(retrieve_tables('عرضه کالا offer'))
"
# باید 'Offer' در نتیجه باشد
```

---

## 10. آنالیز miss‌های بازیابی

### اجرا

```bash
# با log پیش‌فرض (logs/query_log.jsonl)
python scripts/analyze_misses.py

# با log دلخواه
python scripts/analyze_misses.py /path/to/custom_log.jsonl
```

### نمونه خروجی

```
🔍  3 miss event(s) detected

------------------------------------------------------------
  Table : Broker  (missed 2x)
    candidate token: 'کارگزار'  (freq=2)
    candidate token: 'بورس'     (freq=1)
  Table : Ring    (missed 1x)
    candidate token: 'تالار'    (freq=1)
------------------------------------------------------------
```

### تفسیر خروجی

| فیلد | معنا |
|---|---|
| **Table** | جدولی که در SQL بود ولی retriever پیدا نکرد |
| **missed Nx** | چند بار این اتفاق افتاده |
| **candidate token** | کلماتی که هنوز در KB نیستند → کاندیدای synonym جدید |

### استفاده programmatic

```python
from pathlib import Path
from scripts.analyze_misses import analyse, _build_report

misses = analyse(Path("logs/query_log.jsonl"))
report = _build_report(misses)

for entry in report["tables_ranked_by_miss_count"]:
    print(f"{entry['table']}: {entry['miss_count']} misses")
    for cand in entry["top_candidates"][:3]:
        print(f"  + {cand['token']} ({cand['frequency']}x)")
```

---

## 11. نوشتن تست

### ساختار پوشه tests

```
tests/
├── conftest.py
├── test_schema_registry.py   ← SchemaRegistry
├── test_retriever.py         ← retrieve_tables + IDF
├── test_analyze_misses.py    ← analyse / _candidate_tokens
├── test_sql_guard.py         ← clean_sql / validate_sql / ensure_top
└── test_executor.py          ← execute_sql (با mock)
```

### مثال تست بازیابی

```python
# tests/test_retriever.py
from schema_data.retriever import retrieve_tables

class TestRetrieveTables:
    def test_contract_retrieved_for_sales_question(self):
        result = retrieve_tables("فروش ماهانه")
        assert "Contract" in result

    def test_fallback_false_returns_empty_on_garbage(self):
        result = retrieve_tables("xyzzy gibberish", fallback=False)
        assert result == []

    def test_forced_date_table_on_year_question(self):
        result = retrieve_tables("فروش سالیانه")
        assert "Date" in result
```

### مثال تست SchemaRegistry

```python
# tests/test_schema_registry.py
from schema_data.registry import SchemaRegistry

class TestSchemaRegistry:
    def test_context_is_string(self):
        ctx = SchemaRegistry.build_context(["Contract"])
        assert isinstance(ctx, str)

    def test_none_includes_all_tables(self):
        ctx = SchemaRegistry.build_context(None)
        from schema_data.columns import TABLE_COLUMNS
        for table in TABLE_COLUMNS:
            assert f"Table: {table}" in ctx

    def test_empty_tuple_includes_all_tables(self):
        ctx_none  = SchemaRegistry.build_context(None)
        ctx_empty = SchemaRegistry.build_context(())
        assert ctx_none == ctx_empty

    def test_unknown_table_silently_skipped(self):
        ctx = SchemaRegistry.build_context(["FakeTable"])
        assert ctx == ""
```

### مثال تست sql_guard

```python
# tests/test_sql_guard.py
import pytest
from security.sql_guard import clean_sql, validate_sql, ensure_top

class TestCleanSql:
    def test_strips_markdown_fence(self):
        raw = "```sql\nSELECT * FROM Contract\n```"
        assert clean_sql(raw) == "SELECT * FROM Contract"

    def test_limit_converted_to_top(self):
        assert clean_sql("SELECT * FROM Contract LIMIT 5") == \
               "SELECT TOP 5 * FROM Contract"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            clean_sql("")

class TestValidateSql:
    def test_delete_raises(self):
        with pytest.raises(ValueError, match="DELETE"):
            validate_sql("DELETE FROM Contract")

class TestEnsureTop:
    def test_injects_top_when_missing(self):
        assert ensure_top("SELECT * FROM Contract", n=50) == \
               "SELECT TOP 50 * FROM Contract"

    def test_preserves_existing_top(self):
        sql = "SELECT TOP 10 * FROM Contract"
        assert ensure_top(sql, n=50) == sql
```

### اجرای تست‌ها

```bash
# همه تست‌ها
pytest

# یک فایل خاص
pytest tests/test_sql_guard.py -v

# با پوشش کد
pytest --cov=. --cov-report=html
open htmlcov/index.html
```

---

## 12. بررسی سلامت سرویس

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "ollama": true,
  "database": true,
  "model": "llama3"
}
```

| `status` | معنا |
|---|---|
| `ok` | Ollama و دیتابیس هر دو در دسترس |
| `degraded` | یکی در دسترس نیست |
| `down` | هیچ‌کدام در دسترس نیستند |

---

## 13. رفع مشکلات رایج

### ❌ `AttributeError: SchemaRegistry has no attribute 'build_context'`

**علت:** متد `build_schema_context` بود و تست `build_context` صدا می‌زد.  
**راه‌حل:** نسخه جدید هر دو نام را دارد — `git pull origin main`

---

### ❌ جدول مورد نظر در retrieval نیست

```python
# گام 1 — بررسی جدول
from schema_data.tables import TABLE_DESCRIPTIONS
print("Bank" in TABLE_DESCRIPTIONS)

# گام 2 — بررسی synonym
from knowledge.aliases import SYNONYMS
print(SYNONYMS.get("بانک", "موجود نیست"))

# گام 3 — آنالیز miss
python scripts/analyze_misses.py
```

---

### ❌ `RuntimeError: Database error`

```bash
curl http://localhost:8000/health
# اگر database: false باشد:
python -c "
from database.connection import get_engine
from sqlalchemy import text
with get_engine().connect() as c:
    print(c.execute(text('SELECT 1')).fetchone())
"
```

---

### ❌ `ValueError: Received empty SQL from model`

1. مدل قدرتمندتر استفاده کنید: `ollama pull llama3.1:70b`
2. System prompt را در `prompts/system_prompt.md` بهبود دهید
3. مثال‌های few-shot بیشتری به `knowledge/examples.py` اضافه کنید

---

### ❌ تست `test_filters_existing_description_tokens` fail می‌کند

```
AssertionError: assert 'مشتری' not in ['مشتری']
```

**علت:** `_KNOWN_TOKENS` کلمه فارسی را از synonyms نمی‌بیند.  
**راه‌حل:** `_KNOWN_TOKENS` باید هم از `SYNONYMS.keys()` و هم از مقادیر توکن بگیرد:

```python
_KNOWN_TOKENS = frozenset(
    token
    for text in (
        *TABLES.values(),
        *SYNONYMS.keys(),                             # ← کلیدهای فارسی
        *(v for vs in SYNONYMS.values() for v in vs), # ← مقادیر
    )
    for token in _split_tokens(text)
    if len(token) > 1
)
```

---

## جمع‌بندی گردش کار توسعه

```
سوال جدید fail می‌کند
        │
        ├► جدول در TABLE_DESCRIPTIONS نیست?  → tables.py + columns.py
        ├► کلمه فارسی در SYNONYMS نیست?   → aliases.py
        ├► JOIN اشتباه یا ناقص است?       → relationships.py
        ├► قانون دامنه‌ای نادیده گرفته شد? → rules.py
        └► مدل SQL اشتباه تولید می‌کند?  → examples.py
```
