# آموزش Local SQL Agent

[English](../en/tutorial.md) | **فارسی**

---

این آموزش به سبک vignette نوشته شده است: به‌جای فهرست‌کردن امضای تک‌تک توابع، شما را قدم‌به‌قدم از میان کارهای واقعی هدایت می‌کند. در پایان این آموزش، اولین کوئری خود را اجرا کرده‌اید، پایگاه دانش را برای یک جدول جدید گسترش داده‌اید، یک خطای بازیابی (retrieval miss) را تشخیص داده‌اید و برای هر لایه یک تست نوشته‌اید.

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
10. افزودن قواعد کسب‌وکار
11. تشخیص خطاهای بازیابی
12. نوشتن تست
13. استفاده از HTTP API
14. بررسی سلامت و پایش
15. رفع اشکال

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

pip install -r requirements.lock
```

فایل `requirements.lock` نسخهٔ دقیق و بازبینی‌شدهٔ همهٔ بسته‌ها را دارد؛ `requirements.txt` فقط حداقل نسخه‌ها را می‌گوید و برای امتحان سریع روی لپ‌تاپ کافی است.

### مرحله ۲ — پیکربندی

```bash
cp .env.example .env
```

فایل `.env` را باز کنید و حداقل موارد زیر را تنظیم کنید:

```dotenv
# Required
DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/YourDB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
DB_PASSWORD=your-database-password
OPENAI_BASE_URL=http://your-llm-host:8000/v1
OPENAI_MODEL=gpt-oss-20:F16
OPENAI_API_KEY=your-key

# Optional tuning
MAX_ROWS_RETURNED=500
QUERY_TIMEOUT_SECONDS=30
CACHE_TTL_SECONDS=300
```

`OPENAI_BASE_URL` باید به سروری اشاره کند که API گفتگوی سازگار با OpenAI (`/chat/completions`) را ارائه می‌دهد — برای مثال vLLM، LM Studio یا اولاما (`/v1`). مدلی که در `OPENAI_MODEL` نام می‌برید باید توسط همان endpoint سرو شود. سرور محلی معمولاً اعتبارنامه‌ای چک نمی‌کند و `OPENAI_API_KEY` می‌تواند خالی بماند.

`DB_PASSWORD` رمز عبور دیتابیس است، **همان‌طور که هست و بدون هیچ کدگذاری**: رمزی مثل `p@ss/w:rd#1` را دقیقاً همین‌طور بنویسید و آن را داخل URL نگذارید. برنامه خودش رمز را درست در URL می‌گذارد. (اگر رمز را داخل URL بنویسید، باید دستی کدگذاری شود — `@` می‌شود `%40` — و اگر هم `DB_PASSWORD` و هم رمز داخل URL را بگذارید، سرور بالا نمی‌آید.)

دادهٔ دامنه (شِما، نام‌های مستعار، قواعد، مثال‌ها و system prompt) در `.env` نیست. پوشهٔ الگو را کپی و پر کنید؛ بدون آن سرور بالا نمی‌آید:

```bash
cp -r project_config.example project_config
```

**بیش از یک دیتابیس دارید؟** `DB_CONNECTION_URL` یک اتصال به انبار داده را می‌پوشاند که برای اکثر استقرارها کافی است. برای کوئری روی دیتابیس یا سرور دوم، به‌جای آن هر منبع را در `project_config/datasources.yaml` توصیف کنید (از `project_config.example/datasources.example.yaml` شروع کنید: برای هر منبع host و database و لاگین، و رمز خام در یک متغیر `DB_PASSWORD_*` برای هر منبع). بعد هر جدول در `schema.yaml` با `datasource: <name>` می‌گوید در کدام منبع است (برای جدولی که در چند منبع هست فهرست، مثل `datasource: [sales, inventory]`) و `python scripts/assign_datasources.py` این خط‌ها را برایتان می‌نویسد. با چند منبع، هر پرسش پیش از ساخت پرامپت به یک منبع هدایت می‌شود؛ `description:` و `keywords:` در `datasources.yaml` به این هدایت کمک می‌کنند، `PROMPT_RETRIEVAL_TOKEN_BUDGET` برای هر منبع جداگانه اعمال می‌شود (`python scripts/prompt_budget.py` اندازهٔ مناسبش را می‌گوید) و `nolock: true` جایی که DBA الزام کرده `WITH (NOLOCK)` اضافه می‌کند. گام‌ها به ترتیب و با دستورها در بخش ۱۶ از `docs/deployment-runbook.md` است و دلیل این طراحی در `docs/design/DATASOURCES.md`.

---

## 2. اولین کوئری شما

### CLI

```bash
python app.py
```

خروجی زیر را مشاهده می‌کنید:

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

یک سؤال به فارسی یا انگلیسی تایپ کنید:

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

SQL همان‌طور چاپ می‌شود که `generate_sql` برگردانده است (تمیزشده، اعتبارسنجی‌شده و، اگر مدل سقف ردیف ننوشته باشد، با سقف ردیف)؛ CLI آن را دوباره قالب‌بندی نمی‌کند. CLI کلید API نمی‌خواهد و گفتگو را به خاطر نمی‌سپارد: هر سؤال مستقل است. برای خروج `exit` را تایپ کنید.

### از طریق HTTP

همهٔ مسیرها به‌جز `GET /health` کلید API می‌خواهند. یک کلید صادر کنید و هشی را که چاپ می‌کند در `API_KEYS_JSON` یا در فایلی که `API_KEYS_FILE` نام می‌برد بگذارید (جزئیات در بخش ۱ و ۲ از `docs/deployment-runbook.md`):

```bash
python -m scripts.issue_api_key --id analyst-1 --name "Jane Analyst"
```

برای یک اجرای موقت و محلی می‌توانید به‌جایش `AUTH_REQUIRED=false` بگذارید؛ سرور در هر استارت یک هشدار دربارهٔ آن می‌دهد. بعد:

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000

# در ترمینال دیگر
curl -X POST http://localhost:8000/query \
  -H 'Authorization: Bearer <your-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{"question": "top 5 customers by purchase value in 2024", "mode": "full"}'
```

```python
import requests

response = requests.post(
    "http://localhost:8000/query",
    headers={"Authorization": "Bearer <your-api-key>"},
    json={"question": "top 5 customers by purchase value in 2024"},
)
data = response.json()
print(data["sql"])        # generated SQL
print(data["result"])     # list of row dicts
print(data["row_count"])  # 5
```

`/query` هر بار یک پرسش را جواب می‌دهد. برای پرسش‌های دنباله‌دار که زمینه را نگه می‌دارند («از بین آن‌ها…») از مسیرهای گفتگویی `/v2/sessions` استفاده کنید (`docs/api-contract-v2.md`)؛ هر نوبت آن‌جا `sql_display` هم دارد، یعنی همان دستور که با قالب ثابتی برای خواندن چیده شده است.

---

## 3. بازیابی (Retrieval) چگونه کار می‌کند

پیش از فراخوانی LLM، `ContextRetriever` شش بازیاب مستقل را اجرا می‌کند و خروجی آن‌ها را در یک `RetrievalContext` واحد ترکیب می‌کند. اینکه پرامپت نهایی انتخاب بازیاب‌ها را نشان بدهد یا کل پایگاه دانش را، با یک عدد تعیین می‌شود: `PROMPT_RETRIEVAL_TOKEN_BUDGET` (به‌طور پیش‌فرض ۶۰۰۰ توکن، با برآورد `len(text) // 4`).

- **مسیر ایستا (پیش‌فرض برای شِمایی که جا می‌شود).** پرامپت با کل پایگاه دانش شروع می‌شود: system prompt، همهٔ جدول‌ها، همهٔ رابطه‌ها، قواعد، سنجه‌ها و مثال‌ها. این پیشوند برای همهٔ درخواست‌ها بایت‌به‌بایت یکی است و فقط یک پسوند کوتاه عوض می‌شود (فیلترهای شناسایی‌شده، مقدارهای تطبیق‌خورده با انبار داده، گفتگوی تا اینجا و خود پرسش). سرور مدل محلی می‌تواند کش پیشوند را دوباره به کار ببرد و برای هر پرسش شِما را از نو نخواند؛ سرعت از همین‌جا می‌آید. بازیاب‌ها در این مسیر هم اجرا می‌شوند: خروجی‌شان فیلترها، تشخیص مقدارها و انتخاب منبع داده را تغذیه می‌کند.
- **مسیر بازیابی (راه فرار برای شِمای بزرگ).** وقتی پیشوند از بودجه بزرگ‌تر شود، پرامپت برای هر پرسش فقط از جدول‌ها، رابطه‌ها، قواعد و مثال‌هایی ساخته می‌شود که بازیاب‌ها انتخاب کرده‌اند؛ همین شِمای بزرگ را در context مدل کوچک نگه می‌دارد.

```
Question: "monthly purchase orders per broker in 2024"
          │
          ├─ EntityRetriever       → ["Broker", "Date"]
          ├─ FactRetriever         → ["Order"]
          ├─ RelationshipRetriever → ["JOIN [sales].[Date] d ON ...", "JOIN [ref].[Broker] b ON ..."]
          ├─ RuleRetriever         → قواعد «purchase» و «date»
          ├─ ExampleRetriever      → مثال‌هایی که برچسب‌هایشان هم‌پوشانی دارد
          └─ ValueRetriever        → مقدارهای متعارف (تالار با هر نام مستعار، سال و ماه شمسی)
```

این خروجی را با پیکربندی نمونه (`PROJECT_CONFIG_DIR=project_config.example`) خودتان هم می‌توانید ببینید:

```python
from retrieval.context_retriever import ContextRetriever

ctx = ContextRetriever.retrieve("monthly purchase orders per broker in 2024")
print(ctx.entities, ctx.facts)   # ['Broker', 'Date'] ['Order']
```

هر بخش دانشش را از جای مشخصی در `project_config/` می‌گیرد: نام‌های مستعار موجودیت‌ها از `entities.yaml`، جدول‌ها و الگوهای فکت و `always_include` از `retrieval_hints.yaml`، رابطه‌ها (`relationships`) از `schema.yaml`، قواعد از `business_rules.yaml`، مثال‌ها از `examples.yaml` و مترادف‌ها و نام تالارها از `aliases.yaml`.

### بازیابی دو مرحله‌ای

بازیاب‌های موجودیت و فکت ابتدا از **مسیر سریع** استفاده می‌کنند (تطبیق زیررشته‌ای یک نام مستعار یا کلمهٔ کلیدی با پرسش). اگر نتیجه‌ای به دست نیاید، به **موتور bigram با TF-IDF** (`schema_data/retriever.py`) برمی‌گردند که توضیح همهٔ جدول‌ها را با سؤال امتیازدهی می‌کند.

می‌توانید بازیاب TF-IDF را مستقیماً فراخوانی کنید:

```python
from schema_data.retriever import retrieve_tables

print(sorted(retrieve_tables("monthly purchase orders per broker")))
# ['Broker', 'Date', 'Order']

# بدون fallback، وقتی چیزی امتیاز نیاورد [] برمی‌گردد
print(retrieve_tables("xyzzy", fallback=False))
# []
```

بدون `fallback=False`، پرسشی که هیچ امتیازی نگیرد **همهٔ** جدول‌ها را پس می‌گیرد؛ همین جلوی شِمای خالی برای یک پرسش مبهم را می‌گیرد.

### جدول‌های اجباری

برخی جدول‌ها باید با دیدن کلمه‌های مشخصی همیشه در context باشند، هر امتیازی که TF-IDF بدهد. این فهرست `always_include` در `retrieval_hints.yaml` است:

```yaml
# project_config/retrieval_hints.yaml
always_include:
  Date:
    - "date"
    - "year"
    - "month"
```

یعنی هر سؤالی که `year` یا `month` داشته باشد همیشه جدول `Date` را می‌گیرد.

### انتخاب منبع داده (فقط با چند منبع)

با بیش از یک منبع داده، بین بازیابی و پرامپت یک گام دیگر هست: `retrieval/source_selector.py` منبعی را که پرسش دربارهٔ آن است انتخاب می‌کند (کلیدواژه‌های `datasources.yaml`، بعد ادامهٔ گفتگو، بعد جدول‌هایی که بازیابی پیدا کرده، بعد منبع پیش‌فرض) تا مدل فقط جدول‌های همان منبع را ببیند. این گام هیچ فراخوانی مدلی ندارد؛ اگر مدل باز هم `OUT_OF_SCOPE` بگوید، همان درخواست یک بار با منبع بعدی تکرار می‌شود. قاعده‌ها در بخش «Choosing a source per question» از `docs/design/DATASOURCES.md` است.

---

## 4. پرامپت چگونه ساخته می‌شود

`PromptBuilder.build()` پرامپت نهایی را از روی `RetrievalContext` می‌سازد:

```python
from core.models import RetrievalContext
from prompt_engine.builder import PromptBuilder

context = RetrievalContext(
    entities=["Customer"],
    facts=["Order"],
    dimensions=["Customer"],
    relationships=["JOIN [sales].[Customer] c ON o.CustomerID = c.ID"],
    business_rules=["Purchase value is SUM(Order.TotalAmount)."],
    examples=[
        {
            "question": "Top 10 customers",
            "sql": "SELECT TOP 10 c.Name FROM [sales].[Customer] c",
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

پرامپت بخش‌های برچسب‌گذاری‌شده دارد. شش بخش اول **پیشوند ایستا** هستند: system prompt و بعد `BUSINESS RULES`، `METRICS`، `DATABASE SCHEMA`، `RELATIONSHIPS` و `EXAMPLES`. وقتی شِما زیر بودجه باشد، این‌ها هر چه در `project_config/` هست را کامل دارند (پس `context` که می‌دهید فقط پسوند را عوض می‌کند) و از یک پرسش تا پرسش بعد بایت‌به‌بایت یکسان‌اند. بعد **پسوند متغیر** می‌آید، تنها بخشی که با هر درخواست عوض می‌شود: `DETECTED FILTERS` (فیلترهای شناسایی‌شده)، `RESOLVED WAREHOUSE VALUES` (مقدارهایی که با انبار داده تطبیق خورده‌اند، به‌صورت داده و نه دستور)، `SESSION CONTEXT` (چند نوبت آخر گفتگو) و `USER QUESTION`.

وقتی شِما از بودجه بزرگ‌تر باشد، همین بخش‌ها برای هر پرسش جداگانه و فقط با جدول‌ها، رابطه‌ها، قواعد و مثال‌هایی ساخته می‌شوند که بازیاب‌ها انتخاب کرده‌اند. با چند منبع داده، `PromptBuilder.build(..., source="sales")` فقط همان منبع را توصیف می‌کند: جدول‌هایش (با جدول‌های مشترک با منبع‌های دیگر)، رابطه‌های میان آن‌ها و مثال‌هایی که SQL‌شان فقط همین جدول‌ها را می‌خواند، و `Data source: sales — <description>` یک بار بالای شِما چاپ می‌شود.

همین ساختار، و بیش از همه پیشوندی که تغییر نمی‌کند، دلیل این است که مدل‌های کوچک محلی هم سریع‌اند و هم دقیق.

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
SELECT * FROM [sales].[Order] LIMIT 10
```
"""
sql = clean_sql(raw)
print(sql)
# SELECT TOP 10 * FROM [sales].[Order]

# Step 2: validate
# برای دستور پذیرفته‌شده None برمی‌گرداند و برای دستور ردشده ValueError
validate_sql(sql)   # passes — یک SELECT روی جدول مجاز

try:
    validate_sql("DROP TABLE [sales].[Order]")
except ValueError as e:
    print(e)  # Forbidden keyword detected: DROP

# Step 3: ensure TOP
# Injects TOP n if absent, leaves it alone if already present
print(ensure_top("SELECT Name FROM [sales].[Customer]", n=500))
# SELECT TOP 500 Name FROM [sales].[Customer]
```

### چه چیزهایی بررسی می‌شود

`validate_sql` دستور را با sqlglot تجزیه می‌کند و از روی درخت نحوی تصمیم می‌گیرد، نه از روی کلمه‌های متن:

| قاعده | معنی |
|---|---|
| یک دستور | دستورهای پشت‌سرهم به‌عنوان یک دسته رد می‌شوند |
| شکل فقط‌خواندنی | ریشه باید `SELECT`/`WITH` یا `UNION`/`INTERSECT`/`EXCEPT` سطح بالا باشد؛ DDL، DML، `EXEC`، `SELECT ... INTO` و فراخوانی‌هایی مثل `xp_*`/`OPENROWSET` هر کجا بیایند رد می‌شوند |
| فهرست مجاز جدول‌ها | هر جدول باید در `schema.yaml` باشد و `columns` داشته باشد (یا CTE همان کوئری باشد)؛ شِمایی که جلوی نام نوشته شده باید با شِمای شناخته‌شدهٔ جدول بخواند |
| فهرست مجاز ستون‌ها | ستونِ با پیشوند جدول باید در همان جدول باشد؛ ستون بدون پیشوند عمداً مجاز است تا ردِ اشتباه پیش نیاید |
| ACL ستون‌ها | ستون‌های `denied_columns` کلید فراخواننده رد می‌شوند، حتی از راه `*`. ورودی به شکل `schema.Table.Col` یا `Source:schema.Table.Col` یا `Source:Col` ستون را «فقط برای اتصال» می‌کند: فقط به‌عنوان کلید `JOIN ... ON a.col = b.col` مجاز است و هر جای دیگر با `join_only_column` رد می‌شود |
| بدون کامنت و کاتالوگ | هر کامنتی رد می‌شود؛ `INFORMATION_SCHEMA` و `sys.*` و کاتالوگ دیالکت‌های دیگر هم |
| تابع‌ها | تابعی بیرون فهرست مجاز، یا تابعی که وضعیت سرور یا نشست را می‌خواند، رد می‌شود |
| دست‌کم یک جدول | دستوری که هیچ جدولی نخواند رد می‌شود |
| یک منبع داده | با چند منبع، دستوری که هیچ منبعی همهٔ جدول‌هایش را نداشته باشد با `cross_datasource` رد می‌شود |

هر ردّ یک `reason` دارد (`denied_column`، `join_only_column`، `unknown_table`، `cross_datasource`، …) که کلاینت از روی آن کار بعدی را انتخاب می‌کند (`docs/api-contract-v2.md` بخش ۴). `LIMIT` **رد نمی‌شود**: `clean_sql` پیش از اعتبارسنجی آن را به `TOP n` تبدیل می‌کند. فهرست کامل در بخش «Security model» از README است.

### نمایش SQL: `pretty_sql`

دستوری که اجرا می‌شود هرگز دوباره قالب‌بندی نمی‌شود. فقط برای نمایش، `security.sql_guard.pretty_sql` دستور را با یک قالب ثابت می‌چیند (`security/sql_format.py`: `SELECT` تنها در یک خط، هر آیتم در یک خط با ویرگول اول، نام‌های مستعار و join‌ها در ستون‌های هم‌تراز)، آن هم فقط وقتی نتیجه دوباره به همان درخت ورودی تجزیه شود؛ وگرنه با همان شرط به چاپگر sqlglot و در نهایت به خود ورودی برمی‌گردد. این همان `sql_display` یک نوبت گفتگو و `rejected_sql_display` یک دستور ردشده است.

---

## 6. خروجی گرفتن از نتایج

CLI به‌صورت خودکار نتیجه هر کوئری موفق را در Excel ذخیره می‌کند. همچنین می‌توانید خروجی را به‌صورت برنامه‌نویسی‌شده فراخوانی کنید:

```python
from database.executor import execute_sql
from exporters.excel_exporter import export_excel

df = execute_sql("SELECT TOP 20 * FROM [sales].[Order]")

# Excel — auto-fits columns, timestamped filename
path = export_excel(df)
print(path)  # exports/result_20260613_142500.xlsx
```

همه فایل‌های خروجی در پوشه‌ای که با `EXPORT_DIR` تعیین شده ذخیره می‌شوند (پیش‌فرض: `exports/`).

---

## 7. افزودن یک جدول جدید

فرض کنید انبار داده یک جدول بُعدی تازه به نام `Carrier` (شرکت حمل‌کنندهٔ سفارش) پیدا کرده و می‌خواهید تحلیل‌گرها دربارهٔ آن بپرسند. این کار چهار مرحله دارد و هر مرحله یک فایل YAML زیر `project_config/` است — بدون تغییر در کد موتور. (ماژول‌های `schema_data/*.py` و `knowledge/*.py` فقط بارگذارند و داده‌ای ندارند.)

### مرحله ۱ — توصیف جدول و ستون‌ها (دوزبانه)

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

> **توصیف‌ها را دوزبانه بنویسید.** موتور TF-IDF هم فارسی و هم انگلیسی را توکن‌سازی می‌کند؛ بنابراین توصیف دوزبانه باعث می‌شود بازیاب پشتیبان (fallback) برای هر دو زبان کار کند.

`columns` فقط مستندات نیست: فهرست مجاز ستون‌های نگهبان SQL است. جدولی که کلید `columns` نداشته باشد در پرامپت توصیف می‌شود، اما هر کوئری‌ای که آن را بخواند رد می‌شود. `db_schema` بررسی شِمای نوشته‌شده در کوئری را روشن می‌کند؛ برای هر جدول یکی بگذارید. `schema.yaml` یک فایل امنیتی است، پس تغییرش را مثل یک تغییر امنیتی بازبینی کنید.

### مرحله ۲ — ثبت JOIN و ستون کلید خارجی

باز هم در `schema.yaml`: کلید خارجی تازه را به `columns` جدول فکت اضافه کنید (ستونِ با پیشوند جدول که فهرست مجاز نشناسد رد می‌شود) و join را زیر `relationships` بنویسید:

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

هر کلید خارجی واقعی یک بار نوشته می‌شود؛ این رابطه فقط وقتی به مدل داده می‌شود که هر دو جدول جزو جدول‌های انتخاب‌شده برای پرسش باشند.

### مرحله ۳ — افزودن نام‌های مستعار

`project_config/entities.yaml` واژه‌هایی را که کاربر می‌نویسد به جدول وصل می‌کند:

```yaml
entities:
  # ... existing entities ...
  Carrier:
    aliases: ["carrier", "shipping company", "حمل‌کننده", "شرکت حمل"]
    table: "Carrier"
```

### مرحله ۴ — منبع داده (فقط با چند منبع)

با `datasources.yaml`، جدولی که روی منبع پیش‌فرض نیست زیر کلیدش `datasource: <name>` می‌خواهد. `python scripts/assign_datasources.py` مقدار را از روی دیتابیس‌ها درمی‌آورد و همراه مقدار بقیهٔ جدول‌ها در `schema.with_datasources.yaml` می‌نویسد تا مرور کنید (`docs/deployment-runbook.md` بخش ۱۶.۳).

### بررسی

```bash
python scripts/verify_deployment.py     # چک «project_config/ loads» باید PASS شود
python -c "
from schema_data.retriever import retrieve_tables
result = retrieve_tables('orders per carrier', fallback=False)
print(result)
assert 'Carrier' in result
print('OK')
"
```

تغییر `schema.yaml` در راه‌اندازی بعدی سرور به نگهبان می‌رسد (فهرست مجاز یک بار در شروع ساخته می‌شود)؛ بقیهٔ فایل‌های YAML را می‌شود از پنل ادمین و بدون راه‌اندازی دوباره اعمال کرد. اگر `Carrier` در نتیجه نبود، کلمه‌های فارسی و انگلیسی بیشتری به توصیفش اضافه کنید یا مترادف بنویسید — بخش بعد.

---

## 8. افزودن مترادف‌ها و نام‌های مستعار

اگر کاربران سؤال را طور دیگری بیان کنند و بازیاب جدول را پیدا نکند، یک مترادف (synonym) اضافه کنید.

**سناریو:** کاربران می‌گویند `shipment` اما جدول `Carrier` بازیابی نمی‌شود.

```python
from schema_data.retriever import retrieve_tables
from knowledge.aliases import SYNONYMS

print(retrieve_tables("shipment delays", fallback=False))  # Carrier در فهرست نیست
print("shipment" in SYNONYMS)                              # False
```

**راه‌حل:** به `project_config/aliases.yaml` اضافه کنید (کلیدها و مقدارها باید حروف کوچک باشند):

```yaml
synonyms:
  # ... existing entries ...
  "shipment": ["carrier", "delivery"]
```

هر مقدار یک توکن متعارف است که در توصیف جدول‌ها هست؛ امتیازدهی TF-IDF از راه آن جدول درست را پیدا می‌کند. بررسی کنید (در یک پروسهٔ تازه، چون بارگذارها فایل را کش می‌کنند):

```bash
python -c "
from schema_data.retriever import retrieve_tables
assert 'Carrier' in retrieve_tables('shipment delays', fallback=False)
print('OK')
"
```

برای نام تالارهای معاملاتی (یا هر مقدار نام‌دار دیگر) نگاشت متعارف زیر `ring_aliases` در همان فایل است؛ `ValueRetriever` هر شکل را پیش از تزریق به‌عنوان فیلتر SQL به نام متعارف برمی‌گرداند:

```yaml
ring_aliases:
  "Hall Industrial":
    - "industrial"
    - "hall industrial"
    - "industrial ring"
```

---

## 9. افزودن مثال‌های few-shot

مثال‌های few-shot پرارزش‌ترین راه برای بهبود دقت SQL هستند. روی مسیر ایستا (پیش‌فرض، بخش ۳) همهٔ مثال‌های `examples.yaml` در پیشوند پرامپت‌اند. روی مسیر بازیابی، `ExampleRetriever` حداکثر سه مثال را تزریق می‌کند که برچسب‌هایشان با برچسب‌هایی که از پرسش درمی‌آورد هم‌پوشانی دارد.

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

SQL‌ای بنویسید که نگهبان می‌پذیرد: همان جدول‌ها و ستون‌ها و ارجاع‌های `[schema].[table]` که مدل باید به کار ببرد. با چند منبع داده، مثالی که SQL‌اش فقط جدول‌های یک منبع را بخواند فقط در پرامپت همان منبع می‌آید.

**راهبرد برچسب‌گذاری:** برچسب‌های کوچک و قابل‌استفاده مجدد بگذارید. `ExampleRetriever` برچسب‌های پرسش را از یک واژگان ثابت در `retrieval/example_retriever.py` درمی‌آورد (`customer`, `supplier`, `broker`, `symbol`, `ring`, `purchase`, `trade`, `offer`, `value`, `volume`, `price`, `count`, `top`, `date`, `year`, `month`, `day`, `distinct`, `average`, `active`, `wage`) و هر مثال را به تعداد برچسب‌های مشترک امتیاز می‌دهد. برچسبی بیرون از این واژگان روی مسیر بازیابی هرگز تطبیق نمی‌خورد، هرچند مثال همچنان در پیشوند ایستا هست.

---

## 10. افزودن قواعد کسب‌وکار

قواعد کسب‌وکار خطاهای سیستماتیک مدل را — ستون یا جدول اشتباه، منطق تجمیع غلط — بدون هیچ fine-tuning اصلاح می‌کنند. روی مسیر ایستا همهٔ قاعده‌های `business_rules.yaml` در پیشوند پرامپت‌اند.

`project_config/business_rules.yaml`:

```yaml
rules:
  # ... existing rules ...
  broker:
    rule_text: |
      A broker is the selling agent of an order. Join [ref].[Broker] through
      Order.BrokerID and report [ref].[Broker].PersianName, never the ID.
```

روی مسیر بازیابی، قاعده فقط وقتی انتخاب می‌شود که کلیدش یکی از موضوع‌هایی باشد که `RuleRetriever.RULE_MAPPING` (در `retrieval/rule_retriever.py`) می‌شناسد — `purchase`، `trade`، `offer`، `customer`، `supplier`، `broker`، `symbol`، `date`، `ring` و `topn` — و پرسش یکی از کلمه‌های محرک همان موضوع را داشته باشد (تطبیق زیررشته‌ای بدون حساسیت به بزرگی حروف). قاعده‌ای با کلید دیگر در پیشوند ایستا هست، اما روی مسیر بازیابی هرگز انتخاب نمی‌شود؛ پس در استقراری که شِمایش از بودجه بزرگ‌تر است، قاعده‌ها را به نام همین موضوع‌ها بنویسید.

---

## 11. تشخیص خطاهای بازیابی (retrieval miss)

خطای بازیابی وقتی رخ می‌دهد که مدل SQLای تولید کند که به جدولی اشاره دارد که بازیاب آن را در context قرار نداده است. مدل عملاً نام جدول را حدس زده است — گاهی درست، و اغلب اشتباه.

اسکریپت `analyze_misses.py` لاگ کوئری‌های CLI را بررسی می‌کند و این الگوها را پیدا می‌کند:

```bash
python scripts/analyze_misses.py
# Uses logs/query_log.jsonl by default

python scripts/analyze_misses.py /path/to/other_log.jsonl
```

نمونه خروجی:

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

**چگونه این خروجی را بخوانیم:** `Broker` دو بار در SQL تولیدشده ظاهر شده، اما بازیاب آن را در context قرار نداده است. کاربران در آن سؤال‌ها از واژه `agent` استفاده کرده‌اند — واژه‌ای که هنوز در `synonyms` یا توصیف جدول نیست. راه‌حل: افزودن آن به `synonyms` در `project_config/aliases.yaml` (بخش ۸) یا به `description` جدول در `schema.yaml`. اگر جدول اصلاً توصیف نشده، به `schema.yaml` اضافه‌اش کنید (بخش ۷)؛ اگر جدول درست بازیابی شده ولی SQL غلط است، یک مثال few-shot بنویسید (بخش ۹).

همچنین می‌توانید `analyse()` را به‌صورت برنامه‌نویسی‌شده فراخوانی کنید:

```python
from pathlib import Path
from scripts.analyze_misses import analyse, _build_report

misses = analyse(Path("logs/query_log.jsonl"))
report = _build_report(misses)

for entry in report["tables_ranked_by_miss_count"]:
    print(f"{entry['table']}: {entry['miss_count']} misses")
    for cand in entry["top_candidates"][:3]:
        print(f"  → add synonym: '{cand['token']}'")
```

این اسکریپت `query_log.jsonl` مربوط به CLI را می‌خواند. استقراری که فقط از HTTP API استفاده می‌کند به‌جایش `logs/audit_log.jsonl` را می‌نویسد (رکوردی دیگر و فقط تجمیعی که `scripts/analyze_audit_log.py` می‌خواند).

---

## 12. نوشتن تست

تست‌ها در `tests/` قرار دارند. پروژه از `pytest` استفاده می‌کند و فیکسچرها در `tests/conftest.py` هستند. مجموعه بیش از ۵۰۰۰ تست دارد و روی `project_config.example/` اجرا می‌شود:

```bash
PROJECT_CONFIG_DIR=project_config.example pytest tests/ eval/tests -q
```

### تست بازیاب

```python
# tests/test_retriever.py
from schema_data.retriever import retrieve_tables

class TestRetrieveTables:
    def test_broker_retrieved_for_broker_question(self):
        assert "Broker" in retrieve_tables("monthly purchase orders per broker", fallback=False)

    def test_date_forced_on_year_question(self):
        # always_include در retrieval_hints.yaml با کلمهٔ year جدول Date را اجباری می‌کند
        assert "Date" in retrieve_tables("orders per year", fallback=False)

    def test_fallback_false_returns_empty_on_garbage(self):
        assert retrieve_tables("xyzzy nonsense", fallback=False) == []
```

### تست نگهبان SQL

```python
# tests/test_sql_guard.py
import pytest
from security.sql_guard import clean_sql, validate_sql, ensure_top

class TestCleanSql:
    def test_strips_markdown_fence(self):
        assert clean_sql("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_limit_converted_to_top(self):
        assert clean_sql("SELECT * FROM T LIMIT 5") == "SELECT TOP 5 * FROM T"

    def test_empty_input_raises(self):
        with pytest.raises(ValueError, match="empty"):
            clean_sql("")

class TestValidateSql:
    @pytest.mark.parametrize("stmt", [
        "DROP TABLE [sales].[Order]",
        "DELETE FROM [sales].[Order] WHERE 1=1",
        "INSERT INTO [sales].[Order] VALUES (1, 2)",
        "ALTER TABLE [sales].[Order] ADD x INT",
    ])
    def test_forbidden_statements_raise(self, stmt):
        with pytest.raises(ValueError):
            validate_sql(stmt)

    def test_unknown_table_is_refused(self):
        with pytest.raises(ValueError, match="unknown table"):
            validate_sql("SELECT Name FROM [sales].[Nope]")

class TestEnsureTop:
    def test_injects_top_when_absent(self):
        sql = ensure_top("SELECT Name FROM [sales].[Customer]", n=50)
        assert sql.upper().startswith("SELECT TOP 50")

    def test_preserves_existing_top(self):
        sql = "SELECT TOP 10 Name FROM [sales].[Customer]"
        assert ensure_top(sql, n=50) == sql
```

### تست API

همهٔ مسیرها جز `/health` کلید می‌خواهند؛ پس تست‌های API از فیکسچر `auth_settings` در `tests/conftest.py` استفاده می‌کنند که یک کلید آزمایشی تنظیم می‌کند و هدرهای حامل آن را برمی‌گرداند:

```python
# tests/test_api_endpoints.py
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

@pytest.fixture()
def client(auth_settings):
    import api.server as server
    server._system_prompt = "stub system prompt"   # از بارگذاری فایل می‌گذرد
    return TestClient(server.app, headers=auth_settings)

def test_query_endpoint_returns_sql(client):
    from api.models import QueryResponse
    reply = QueryResponse(
        question="top customers", sql="SELECT TOP 5 Name FROM [sales].[Customer]",
        result=[], row_count=0,
    )
    with patch("api.runner.run_query", return_value=reply):
        response = client.post("/query", json={"question": "top customers"})
    assert response.status_code == 200
    assert "sql" in response.json()

def test_health_endpoint_returns_ok(client):
    from api import health
    with patch("api.health._ping_db", return_value=(True, "SELECT 1 succeeded")), \
         patch("api.health._ping_openai", return_value=(True, "ok")):
        health.reset_health_cache()
        response = client.get("/health")
    assert response.json()["status"] == "ok"
```

### اجرای تست‌ها

```bash
pytest                                  # all tests (testpaths: tests, eval/tests)
pytest tests/test_sql_guard.py -v       # one module, verbose
pytest -k "retriever" -v               # tests matching a keyword
pytest tests/ eval/tests --cov          # coverage — همان چیزی که CI اندازه می‌گیرد
```

---

## 13. استفاده از HTTP API

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload
```

(`--reload` برای توسعه است؛ استقرار از فرمان بخش ۴ در `docs/deployment-runbook.md` استفاده می‌کند.) هر درخواست زیر هدر `Authorization: Bearer <your-api-key>` را دارد (بخش ۲).

### POST /query

```bash
curl -X POST http://localhost:8000/query \
  -H 'Authorization: Bearer <your-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "question": "monthly purchase orders per broker in 2024",
    "mode": "full"
  }'
```

گزینه‌های `mode`:
- `full` (پیش‌فرض) — SQL و نتیجه اجراشده را برمی‌گرداند
- `sql` — فقط SQL را برمی‌گرداند و اجرا نمی‌کند
- `result` — فقط اجرا و ردیف‌ها را برمی‌گرداند

با `"interpret": true` یک خلاصهٔ ساده به زبان طبیعی هم می‌گیرید (تا بیست ردیف نتیجه به مدل فرستاده می‌شود و گیت حاکمیت داده هنوز مدل ریموت را بدون `LLM_ALLOW_REMOTE` رد می‌کند). `POST /query/stream` همین درخواست است که به‌صورت Server-Sent Events پخش می‌شود.

### کش کوئری

سؤال‌های یکسان در یک `mode` از کش LRU درون‌فرآیندی سرو می‌شوند. کلید کش از پرسشِ نرمال‌شده، `mode`، نسخهٔ پیشوند پرامپت و یک scope برگرفته از `denied_columns` کلید ساخته می‌شود؛ پس دو نفر که داده‌های متفاوتی می‌بینند هرگز یک مدخل را به اشتراک نمی‌گذارند. درخواست‌های `mode=sql` و `interpret: true` کش نمی‌شوند.

```bash
# Check cache state
curl -H 'Authorization: Bearer <your-api-key>' http://localhost:8000/cache/stats
# {"hits": 12, "misses": 4, "evictions": 0, "size": 4, ..., "enabled": true}

# Clear everything
curl -X POST -H 'Authorization: Bearer <your-api-key>' http://localhost:8000/cache/clear
```

`POST /cache/invalidate` یک مدخل را حذف می‌کند و وقتی مدخلی پیدا نکند ۴۰۴ می‌دهد؛ ولی مدخل را بدون scope فراخواننده جست‌وجو می‌کند، در حالی که `/query` آن را زیر scope فراخواننده ذخیره می‌کند. پس ممکن است برای پاسخی که واقعاً در کش است ۴۰۴ بدهد؛ `/cache/clear` همیشه کار می‌کند و دکمهٔ پاک‌کردن پنل ادمین هم.

TTL و حداکثر اندازه کش با `CACHE_TTL_SECONDS` و `CACHE_MAX_SIZE` در `.env` کنترل می‌شوند.

---

## 14. بررسی سلامت و پایش

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "openai": true,
  "database": true,
  "model": "gpt-oss-20:F16",
  "database_detail": "SELECT 1 succeeded"
}
```

`/health` کلید نمی‌خواهد. فیلد `model` فقط وقتی می‌آید که فراخواننده کلید معتبر فرستاده باشد. `database` فقط وقتی `true` است که `SELECT 1` روی **همهٔ** منبع‌های داده موفق شده باشد؛ با چند منبع، `database_detail` هر کدام را نام می‌برد (`sales: SELECT 1 succeeded; inventory: ...`). نتیجه به‌اندازهٔ `HEALTH_CACHE_TTL_SECONDS` (پیش‌فرض ۱۵) دوباره استفاده می‌شود.

| `status` | معنی |
|---|---|
| `ok` | هم دیتابیس و هم endpoint مدل در دسترس هستند |
| `degraded` | یکی از این دو در دسترس نیست |
| `down` | هیچ‌کدام در دسترس نیستند |

CLI هر سؤال را در `logs/query_log.jsonl` می‌نویسد (هر خط یک شیء JSON):

```json
{
  "timestamp": "2026-06-13T14:22:57",
  "question": "top 5 customers by purchase value in 2024",
  "generated_sql": "SELECT TOP 5 c.Name ...",
  "model_name": "openai:gpt-oss-20:F16",
  "row_count": 5,
  "execution_time_seconds": 1.38,
  "status": "SUCCESS",
  "error_message": null,
  "excel_file": "exports/result_20260613_142257.xlsx"
}
```

HTTP API به‌جایش `logs/audit_log.jsonl` را می‌نویسد: هویت کلید، حکم نگهبان، زمان‌بندی‌ها، بلوک وضعیت LLM، `datasource` (منبعی که SQL روی آن اجرا شده) و، با چند منبع داده، `datasource_selection`. ردیف‌های نتیجه هرگز در آن ثبت نمی‌شوند. `python scripts/analyze_audit_log.py` آن را تجمیع می‌کند (`docs/deployment-runbook.md` بخش ۸).

---

## 15. رفع اشکال

### جدولی بازیابی نمی‌شود

```python
from schema_data.tables import TABLE_DESCRIPTIONS
from knowledge.aliases import SYNONYMS
from schema_data.retriever import retrieve_tables

print("Carrier" in TABLE_DESCRIPTIONS)   # False → به schema.yaml اضافه کنید
print(SYNONYMS.get("shipment"))          # None  → به aliases.yaml اضافه کنید
print(retrieve_tables("orders per carrier", fallback=False))
```

### SQL نامعتبر است یا به جدول اشتباه اشاره می‌کند

1. برای آن الگوی سؤال یک مثال few-shot اضافه کنید (`project_config/examples.yaml`)
2. قانون تجاری مرتبط را اضافه یا سخت‌گیرانه‌تر کنید (`project_config/business_rules.yaml`)
3. از مدل بزرگتری که توسط endpoint سرو می‌شود استفاده کنید (برای مثال `gpt-oss-20:F16`)

### منبع داده اشتباه جواب می‌دهد (با چند منبع)

در رکورد audit همان پرسش، `datasource_selection` را بخوانید: `reason` می‌گوید کدام نشانه منبع را انتخاب کرده (`keyword`، `session`، `retrieval`، `default`) و `fallback_from` تکرار با منبع بعدی را نشان می‌دهد. واژهٔ جاافتاده را به `keywords:` همان منبع اضافه کنید؛ دستور `grep` در بخش ۱۶.۹ از `docs/deployment-runbook.md` است.

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

(با چند منبع، نام منبع را به `get_engine("<source>")` بدهید.)

### `ModelUnavailableError` / `503`

```bash
curl http://your-llm-host:8000/v1/models   # is the LLM endpoint reachable?
# then verify OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY in .env
```

### جواب خالی یا متن به‌جای SQL

- اگر مدل ریزنینگ دارد و توکن‌هایش را صرف فکر کردن کرده، پاسخ `LLM_OUTPUT_TRUNCATED` است: `LLM_NUM_PREDICT` را بالا ببرید یا ریزنینگ را با `LLM_EXTRA_BODY` خاموش کنید (`.env.example`).
- مدل پاسخی خارج از دامنه برگردانده است: در `logs/query_log.jsonl` (CLI) یا `logs/audit_log.jsonl` (API) به دنبال `OUT_OF_SCOPE` بگردید و یک مثال few-shot متناسب اضافه کنید.
- از مدلی بزرگ‌تر یا توانمندتر استفاده کنید.
