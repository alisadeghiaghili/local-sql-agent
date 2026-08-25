# آموزش Local SQL Agent

[English](../en/tutorial.md) | **فارسی**

---

این آموزش به سبک vignette نوشته شده است: به‌جای فهرست‌کردن امضای تک‌تک توابع، شما را قدم‌به‌قدم از میان کارهای واقعی هدایت می‌کند. در پایان این آموزش، اولین کوئری خود را اجرا کرده‌اید، پایگاه دانش را برای یک entity جدید توسعه داده‌اید، یک خطای بازیابی (retrieval miss) را تشخیص داده‌اید و یک تست نوشته‌اید.

## فهرست مطالب

1. نصب
2. اولین کوئری شما
3. بازیابی (Retrieval) چگونه کار می‌کند
4. پرامپت چگونه ساخته می‌شود
5. خط لوله امنیتی SQL
6. خروجی گرفتن از نتایج
7. افزودن یک جدول جدید
8. افزودن مترادف‌ها و نام‌های مستعار
9. افزودن مثال‌های few-shot
10. تشخیص خطاهای بازیابی
11. نوشتن تست
12. استفاده از HTTP API
13. بررسی سلامت و پایش
14. رفع اشکال

---

## 1. نصب

### چه چیزهایی لازم دارید

| وابستگی | حداقل نسخه | توضیحات |
|---|---|---|
| Python | 3.11 | |
| endpoint سازگار با OpenAI | هر نسخه | برای مثال vLLM، LM Studio یا API «/v1» اولاما که از طریق `OPENAI_BASE_URL` در دسترس باشد |
| SQL Server | 2016+ | دسترسی از طریق ODBC |
| درایور ODBC | 17 یا 18 | `msodbcsql17` / `msodbcsql18` |

### مرحله ۱ — کلون کردن مخزن و ساخت محیط مجازی

```bash
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent

python -m venv .venv
source .venv/bin/activate      # Linux / macOS
# .venv\Scripts\activate       # Windows

pip install -r requirements.txt
```

### مرحله ۲ — پیکربندی

```bash
cp .env.example .env
```

فایل `.env` را باز کنید و حداقل موارد زیر را تنظیم کنید:

```dotenv
# Required
DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/YourDB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
OPENAI_BASE_URL=http://your-llm-host:8000/v1
OPENAI_MODEL=gpt-oss-20:F16
OPENAI_API_KEY=your-key

# Optional tuning
MAX_ROWS_RETURNED=500
QUERY_TIMEOUT_SECONDS=30
CACHE_TTL_SECONDS=300
```

### مرحله ۳ — در دسترس بودن مدل

`OPENAI_BASE_URL` باید به سروری اشاره کند که API گفتگو سازگار با OpenAI (`/chat/completions`) را ارائه می‌دهد — برای مثال vLLM، LM Studio یا اولاما (`/v1`). مدلی که در `OPENAI_MODEL` نام می‌برید باید توسط همان endpoint سرو شود.

---

## 2. اولین کوئری شما

### CLI

```bash
python app.py
```

خروجی زیر را مشاهده می‌کنید:

```
════════════════════════════════════════════════════════════
Auction NLQ Engine Started
════════════════════════════════════════════════════════════

Question:
```

یک سؤال به فارسی یا انگلیسی تایپ کنید:

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

برای خروج عبارت `exit` را تایپ کنید.

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

## 3. بازیابی (Retrieval) چگونه کار می‌کند

پیش از فراخوانی LLM، `ContextRetriever` شش بازیاب مستقل را اجرا می‌کند و خروجی آن‌ها را در یک `RetrievalContext` واحد ترکیب می‌کند.

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

### بازیابی دو مرحله‌ای

هر بازیاب ابتدا از **مسیر سریع** (تطبیق دقیق alias یا کلمه کلیدی) استفاده می‌کند. اگر نتیجه‌ای به دست نیاید، به **موتور bigram با TF-IDF** (`schema_data/retriever.py`) برگشت می‌خورد که همه توضیحات جدول‌ها را با سؤال امتیازدهی می‌کند.

می‌توانید بازیاب TF-IDF را مستقیماً فراخوانی کنید:

```python
from schema_data.retriever import retrieve_tables

# Fast path hits → returned immediately
print(retrieve_tables("فروش ماهانه مشتریان"))
# ['Contract', 'Customer', 'Date']

# No match → returns [] when fallback disabled
print(retrieve_tables("xyzzy", fallback=False))
# []
```

### جدول‌های اجباری

برخی جدول‌ها باید همیشه در حضور کلمات کلیدی مشخص در زمینه (context) حضور داشته باشند:

```python
# schema_data/retriever.py
_ALWAYS_INCLUDE = {
    "سال":   ["Date"],
    "ماه":   ["Date"],
    "تاریخ": ["Date"],
}
```

یعنی هر سؤالی که به سال یا ماه اشاره کند، همیشه جدول `Date` را در context دریافت می‌کند.

---

## 4. پرامپت چگونه ساخته می‌شود

`PromptBuilder.build()` پرامپت نهایی را از روی `RetrievalContext` می‌سازد:

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

پرامپت دارای بخش‌های برچسب‌گذاری‌شده و روشن است — `## Business Rules`، `## Schema`، `## Relationships`، `## Filters`، `## Examples`، `## Question` — و همین ساختار است که به مدل‌های کوچک‌تر امکان می‌دهد SQL صحیح تولید کنند.

---

## 5. خط لوله امنیتی SQL

هر رشته SQL — چه از مدل و چه از هر جای دیگر — از سه تابع در `security/sql_guard.py` عبور می‌کند:

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

### چه چیزهایی مسدود می‌شوند

| دسته | کلمات کلیدی |
|---|---|
| DDL | `DROP`, `ALTER`, `CREATE`, `TRUNCATE` |
| DML | `DELETE`, `UPDATE`, `INSERT`, `MERGE` |
| اجرا | `EXECUTE`, `EXEC`, `XP_`, `SP_` |
| کاوش ساختار (Schema) | `INFORMATION_SCHEMA`, `SYS.` |
| صفحه‌بندی خام | `LIMIT` (به `TOP` تبدیل می‌شود، مسدود نمی‌شود) |

---

## 6. خروجی گرفتن از نتایج

CLI به‌صورت خودکار نتیجه هر کوئری موفق را در Excel ذخیره می‌کند. همچنین می‌توانید خروجی را به‌صورت برنامه‌نویسی‌شده فراخوانی کنید:

```python
from database.executor import execute_sql
from exporters.excel_exporter import export_excel

df = execute_sql("SELECT TOP 20 * FROM [Auction_Fact].[Contract]")

# Excel — auto-fits columns, timestamped filename
path = export_excel(df)
print(path)  # exports/result_20260613_142500.xlsx
```

همه فایل‌های خروجی در پوشه‌ای که با `EXPORT_DIR` تعیین شده ذخیره می‌شوند (پیش‌فرض: `exports/`).

---

## 7. افزودن یک جدول جدید

فرض کنید بورس شما اکنون در یک ابزار مالی جدید معامله می‌کند و باید جدول `Broker` را اضافه کنید.

### مرحله ۱ — توصیف جدول (دوزبانه)

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

> **توصیف‌ها را دوزبانه بنویسید.** موتور TF-IDF هم فارسی و هم انگلیسی را توکن‌سازی می‌کند؛ بنابراین توصیف‌های دوزبانه باعث می‌شوند بازیاب پشتیبان (fallback) برای هر دو زبان کار کند.

### مرحله ۲ — تعریف ستون‌ها

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

### مرحله ۳ — افزودن رابطه JOIN

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

### مرحله ۴ — افزودن نام‌های مستعار فارسی

`knowledge/entities.py`:

```python
"Broker": {
    "aliases": ["کارگزار", "کارگزاری", "broker", "brokerage"],
    "table": "Broker",
}
```

### مرحله ۵ — بررسی

```bash
python -c "
from schema_data.retriever import retrieve_tables
print(retrieve_tables('فروش کارگزاران'))
"
# Should include 'Broker'
```

---

## 8. افزودن مترادف‌ها و نام‌های مستعار

اگر کاربران سؤال را طور دیگری بیان کنند و بازیاب جدول را پیدا نکند، یک مترادف (synonym) اضافه کنید.

**سناریو:** کاربران می‌گویند «عرضه کالا» اما جدول `Offer` بازیابی نمی‌شود.

`knowledge/aliases.py`:

```python
SYNONYMS: dict[str, list[str]] = {
    # ... existing entries ...
    "عرضه": ["Offer", "offer", "supply", "عرضه کالا", "عرضه‌کننده"],
}
```

سپس بررسی کنید:

```bash
python -c "
from schema_data.retriever import retrieve_tables
print(retrieve_tables('عرضه کالا در تالار پتروشیمی'))
"
# Should include 'Offer'
```

برای نام تالارهای معاملاتی، نگاشت متعارف در `RING_ALIASES` نگهداری می‌شود:

```python
RING_ALIASES["تالار برق"] = [
    "برق", "تالار برق", "رینگ برق", "بازار برق", "انرژی برق"
]
```

---

## 9. افزودن مثال‌های few-shot

مثال‌های few-shot پرارزش‌ترین راه برای بهبود دقت SQL هستند. وقتی برچسب‌های یک سؤال با برچسب‌های یک مثال هم‌پوشانی داشته باشد، آن مثال در پرامپت تزریق می‌شود.

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

**راهبرد برچسب‌گذاری:** از برچسب‌های کوچک و قابل‌استفاده مجدد (`broker`, `top`, `value`, `month`, `year`, `count`) به‌جای عبارت‌های طولانی استفاده کنید. `ExampleRetriever` بر اساس هم‌پوشانی برچسب‌ها امتیاز می‌دهد؛ بنابراین برچسب‌های گسترده‌تر با سؤال‌های بیشتری تطبیق می‌خورند.

---

## 10. تشخیص خطاهای بازیابی (retrieval miss)

خطای بازیابی وقتی رخ می‌دهد که مدل SQLای تولید کند که به جدولی اشاره دارد که بازیاب آن را در context قرار نداده است. مدل عملاً نام جدول را حدس زده است — گاهی درست، و اغلب اشتباه.

اسکریپت `analyze_misses.py` لاگ کوئری‌های شما را بررسی می‌کند و این الگوها را پیدا می‌کند:

```bash
python scripts/analyze_misses.py
# Uses logs/query_history.jsonl by default

python scripts/analyze_misses.py /path/to/other_log.jsonl
```

نمونه خروجی:

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

**چگونه این خروجی را بخوانیم:** `Broker` دو بار در SQL تولیدشده ظاهر شده، اما بازیاب آن را در context قرار نداده است. کاربران در آن سؤال‌ها از واژه `کارگزار` استفاده کرده‌اند — واژه‌ای که هنوز در `SYNONYMS` یا `TABLE_DESCRIPTIONS` نیست. راه‌حل: افزودن `"کارگزار"` به `knowledge/aliases.py`.

همچنین می‌توانید `analyse()` را به‌صورت برنامه‌نویسی‌شده فراخوانی کنید:

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

## 11. نوشتن تست

تست‌ها در `tests/` قرار دارند. پروژه از `pytest` استفاده می‌کند و فیکسچرها در `tests/conftest.py` هستند.

### تست بازیاب

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

### تست نگهبان SQL

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

### تست API

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
    with patch("api.health._ping_db", return_value=True), \
         patch("api.health._ping_openai", return_value=True):
        response = client.get("/health")
    assert response.json()["status"] == "ok"
```

### اجرای تست‌ها

```bash
pytest                                  # all tests
pytest tests/test_sql_guard.py -v       # one module, verbose
pytest -k "retriever" -v               # tests matching a keyword
pytest --cov=. --cov-report=html        # coverage report
open htmlcov/index.html
```

---

## 12. استفاده از HTTP API

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

گزینه‌های `mode`:
- `full` (پیش‌فرض) — SQL و نتیجه اجراشده را برمی‌گرداند
- `sql` — فقط SQL را برمی‌گرداند و اجرا نمی‌کند
- `result` — فقط اجرا و ردیف‌ها را برمی‌گرداند

### کش کوئری

سؤال‌های یکسان تکراری از کش LRU درون‌فرآیندی سرو می‌شوند:

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

TTL و حداکثر اندازه کش با `CACHE_TTL_SECONDS` و `CACHE_MAX_SIZE` در `.env` کنترل می‌شوند.

---

## 13. بررسی سلامت و پایش

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "database": true,
  "openai": true,
  "model": "gpt-oss-20:F16"
}
```

| `status` | معنی |
|---|---|
| `ok` | هم SQL Server و هم endpoint مدل در دسترس هستند |
| `degraded` | یکی از مؤلفه‌ها در دسترس نیست |
| `down` | هیچ‌کدام از مؤلفه‌ها در دسترس نیستند |

لاگ کوئری‌ها در `logs/query_history.jsonl` نوشته می‌شود (هر خط یک شیء JSON):

```json
{
  "timestamp": "2026-06-13T14:22:57",
  "question": "برترین مشتریان در 1402",
  "generated_sql": "SELECT TOP 10 ...",
  "model_name": "openai:gpt-oss-20:F16",
  "row_count": 10,
  "execution_time_seconds": 1.38,
  "status": "SUCCESS",
  "excel_file": "exports/result_20260613_142257.xlsx"
}
```

---

## 14. رفع اشکال

### جدولی بازیابی نمی‌شود

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

### SQL نامعتبر است یا به جدول اشتباه اشاره می‌کند

1. برای آن الگوی سؤال یک مثال few-shot اضافه کنید (`knowledge/examples.py`)
2. قانون تجاری مرتبط را اضافه یا سخت‌گیرانه‌تر کنید (`knowledge/business_rules.py`)
3. از مدل بزرگتری که توسط endpoint سرو می‌شود استفاده کنید (برای مثال `gpt-oss-20:F16`)

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
curl http://your-llm-host:8000/v1/models   # is the LLM endpoint reachable?
# then verify OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY in .env
```

### `ValueError: Received empty SQL from model`

- مدل پاسخی خارج از دامنه برگردانده است. در `logs/query_history.jsonl` به دنبال وضعیت `OUT_OF_SCOPE` بگردید.
- یک مثال few-shot متناسب برای هدایت مدل اضافه کنید.
- از مدلی بزرگ‌تر یا توانمندتر استفاده کنید.
