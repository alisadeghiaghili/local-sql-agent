# راه‌اندازی — گام به گام

> **این راهنما برای ۴.۵.۰ به بعد است.** سه چیز از ۴.۳.۰ عوض شده و اگر
> نسخهٔ قبلی این سند را دنبال کنید دقیقاً روی همین‌ها گیر می‌کنید:
>
> | چه چیزی | قبلاً | حالا |
> |---|---|---|
> | حالت پیش‌فرض UI | نمایشی، بعد دستی زنده می‌کردید | **زنده** (۴.۴.۰) |
> | نشانی بک‌اند | فیلدی بالای صفحه | فایل `web/js/config.js` (۴.۴.۰) |
> | کلیدها و نقش‌ها | فقط `.env` | دیتابیس اپلیکیشن، `.env` فقط بوت‌استرپ (۴.۵.۰) |
>
> اگر بالای صفحه دنبال فیلد «نشانی بک‌اند» می‌گردید، **حذف شده** — بخش ۲.۵.
>
> و دو تلهٔ رایج که ربطی به نسخه ندارند و هر دو در بخش ۲.۲ توضیح داده
> شده‌اند: **کلید خام را در مرورگر بزنید، نه `key_sha256` را** (۴۳ نویسه
> در برابر ۶۴)، و اگر `API_KEYS_JSON` را چندخطی می‌نویسید کل مقدار باید در
> **کوتیشن تکی** باشد (و آپاستروف نداشته باشد) وگرنه خوانده نمی‌شود. راه
> ساده‌تر: آرایهٔ کلیدها را در یک فایل بگذارید و با `API_KEYS_FILE` معرفی کنید.
>
> **از ۴.۷.۰:** `issue_api_key` برای هر سه نقش فلگ دارد (`--full-admin`
> هر سه را می‌دهد) و کلید خام را درون کادر و با ذکر مقصدش چاپ می‌کند.
> اگر پنل ادمین برایتان نیمه‌۴۰۳ است، بخش ۲.۵.۱ می‌گوید چرا: `--admin`
> به‌تنهایی فقط ۴ بخش از ۱۲ بخش را باز می‌کند.
>
> **از ۴.۹.۰:** اگر مدل زبانی‌تان ریزنینگ دارد (Qwen3، DeepSeek-R1،
> gpt-oss) و UI می‌گوید «جوابی نگرفتم»، بخش ۲.۹ را بخوانید — تقریباً
> همیشه سقف توکن است، نه مدل و نه پرامپت.
>
> **از ۴.۱۰.۰:** بعد از ذخیرهٔ کلید، فیلد ورود **جمع می‌شود** و
> `کلید: ذخیره شده ✓` به‌علاوهٔ دکمهٔ «تغییر کلید» می‌ماند. اگر دنبال
> فیلد می‌گردید و نیست، یعنی کلید ذخیره است. قبلاً فیلد خالی همیشه سر
> جایش می‌ماند و همه فکر می‌کردند از سیستم خارج شده‌اند.

دو راه برای استفاده هست و **کاملاً از هم جدا هستند**:

| | CLI | وب |
|---|---|---|
| اجرا | `python app.py` | دو سرور جدا |
| کلید API لازم دارد؟ | **نه** | **بله** |
| گفتگوی چندنوبتی («از بین آن‌ها…») | ندارد | دارد |
| چند سشنی و مموری | ندارد | دارد |
| خروجی اکسل خودکار | دارد | دکمهٔ دانلود |

اگر فقط می‌خواهید ببینید موتور کار می‌کند، از CLI شروع کنید — سریع‌تر است و
هیچ کلیدی نمی‌خواهد.

---

## نصب از صفر، به ترتیب

این فهرست کل مسیر از یک ماشین خالی تا یک سرور راستی‌آزمایی‌شده و در حال اجراست؛ هر گام دستورش و نشانهٔ تمام‌شدنش را دارد و توضیح کامل در بخشی است که به آن ارجاع شده. گامی که «فقط چند دیتابیس» دارد، برای کسی که یک دیتابیس دارد رد می‌شود. تا نشانهٔ «تمام شد وقتی» را ندیده‌اید به گام بعد نروید. (نسخهٔ انگلیسی همین فهرست، با ارجاع به بخش‌های `docs/deployment-runbook.md`، اول آن سند است.)

۱. **پیش‌نیازها.** Python 3.11 یا بالاتر؛ درایور ODBC برای SQL Server (نسخهٔ ۱۷ یا ۱۸) روی ماشینی که سرور را اجرا می‌کند؛ یک endpoint مدل زبانی سازگار با OpenAI که از آن ماشین دیده شود؛ و یک لاگین فقط‌خواندنی روی هر دیتابیس (`docs/db-hardening.md`، کاری که DBA انجام می‌دهد). *تمام شد وقتی:* `python --version` عدد ۳.۱۱ یا بیشتر نشان دهد و (بعد از گام ۲) `python -c "import pyodbc; print(pyodbc.drivers())"` فهرستی شامل `ODBC Driver 18 for SQL Server` یا `ODBC Driver 17 for SQL Server` چاپ کند.

۲. **نصب وابستگی‌ها** (بخش ۰.۱): در یک virtual environment، از ریشهٔ ریپو، `pip install -r requirements.lock`. *تمام شد وقتی:* pip بدون خطا تمام شود.

۳. **فایل `.env`** (بخش ۰.۲): `copy .env.example .env`، بعد `DB_CONNECTION_URL` و `DB_PASSWORD` (با چند دیتابیس به‌جایشان در گام ۵ برای هر منبع یک `DB_PASSWORD_*`)، `OPENAI_BASE_URL` و `OPENAI_MODEL` را پر کنید؛ `OPENAI_API_KEY` فقط اگر endpoint شما کلید چک می‌کند. *تمام شد وقتی:* `python -c "import config as cfg; cfg.settings.validate(); print('settings ok')"` عبارت `settings ok` را چاپ کند. هر چیز دیگری یک `ValueError` است که نام متغیرِ مشکل‌دار را می‌گوید.

۴. **پوشهٔ `project_config/`** (بخش ۰.۳): `Copy-Item -Recurse project_config.example project_config` و بعد محتوای نمونه را با دامنهٔ خودتان عوض کنید. دو ابزار اختیاری از روی دیتابیس زنده پیش‌نویس می‌سازند: `python -m database.schema_inspector_cli` برای `schema.yaml` (بخش ۰.۳.۲) و ویزارد `python setup_project.py` برای `entities.yaml`، `aliases.yaml`، `business_rules.yaml` و `examples.yaml` (بخش ۰.۳.۱). ویزارد `schema.yaml` نمی‌نویسد و `datasource:` هیچ جدولی را تعیین نمی‌کند. *تمام شد وقتی:* نُه فایل YAML و `system_prompt.md` در `project_config/` باشند، `schema.yaml` جدول‌های شما را فهرست کند و، اگر ویزارد را اجرا کرده‌اید، آخرین خطش `Setup complete.` و شمارش‌ها باشد.

۵. **توصیف دیتابیس‌ها** (*فقط چند دیتابیس*، بخش ۰.۳.۳ گام ۱): `project_config/datasources.yaml` را از `project_config.example/datasources.example.yaml` بنویسید و رمز خام هر منبع را در `.env` زیر نام متغیری که `password_env` آن می‌گوید بگذارید. *تمام شد وقتی:* `python -c "from database.datasources import datasource_names; print(datasource_names())"` نام همهٔ منبع‌ها را چاپ کند و دستور گام ۳ هنوز `settings ok` بدهد.

۶. **همگام‌سازی ساختار `schema.yaml` با دیتابیس‌ها**: `python scripts/sync_schema.py` (با هر تعداد دیتابیس). فهرست جدول‌ها، ستون‌ها، نوع‌ها و کلیدهای هر منبع را فقط می‌خواند (سه view از `INFORMATION_SCHEMA` برای هر منبع؛ هیچ ردیفی خوانده نمی‌شود) و `schema.synced.yaml` را کنار `schema.yaml` می‌نویسد: فایل خودتان با همهٔ توضیح‌ها و کامنت‌ها، به‌اضافهٔ `datasource:` هر جدول (چند دیتابیس)، ستون‌هایی که دیتابیس دارد و فایل ندارد (به‌صورت پیش‌نویس)، نقشهٔ `column_types:` و نشانهٔ روی هر ستون یا جدولی که دیتابیس ندارد (بخش ۰.۳.۳). `relationships.proposed.yaml` را هم برای مرور می‌نویسد. اگر اجرا نشود، پیش‌پرواز گام ۱۱ با چک `Tables are in their data source` (چند دیتابیس) یا `Schema structure matches the databases` خطا می‌دهد (بخش ۰.۴). *تمام شد وقتی:* اجرا با `written: ... -- review it before replacing schema.yaml` تمام شود.

۷. **مرور، جایگزینی `schema.yaml`، و `--check`**: `project_config\schema.synced.yaml` را بخوانید (مقایسه با `schema.yaml` دقیقاً تغییرها را نشان می‌دهد)، `schema.yaml` را به `schema.yaml.bak` کپی کنید، پیشنهاد را جایش بگذارید و `python scripts/sync_schema.py --check` را بزنید. *تمام شد وقتی:* `CHECK OK: schema.yaml's structure matches the databases` چاپ شود (کد خروج ۰).

۸. **آنچه همگام‌سازی نشان کرده را حل کنید.** بخش `== columns in schema.yaml that the database does not have: N ==` ستون‌هایی را می‌آورد که کامنت `# not in database (sync_schema.py)` گرفته‌اند و بخش `== tables in schema.yaml found in no data source: N ==` جدول‌هایی را که `# not found in any data source` دارند. با `python scripts/sync_schema.py --prune` حذفشان کنید (نتیجه را مرور و `schema.yaml` را دوباره جایگزین کنید)، املای ستون را دستی درست کنید، یا اگر ستون هست ولی لاگین آن را نمی‌بیند از DBA دربارهٔ `DENY` بپرسید. توضیح ستون‌ها و جدول‌های افزوده‌شده (`# TO BE FILLED`) را بنویسید. جدولی که می‌خواهید و گزارش زیر `== tables in the database that were not in schema.yaml ==` آورده با `--add-tables 'schema.Table'` اضافه می‌شود، و `relationships.proposed.yaml` را مرور کنید. `--check` برای ستون‌های نشان‌شده شکست نمی‌خورد، پس بخش‌ها را بخوانید. *تمام شد وقتی:* آن بخش‌ها `: 0 ==` بگویند.

۹. **تنظیمات هر منبع** (*فقط چند دیتابیس*): `description:` و `keywords:` تا پرسش به منبع درست برسد، و `nolock: true` فقط روی منبع‌هایی که DBA‌شان `WITH (NOLOCK)` را الزام کرده؛ این کلید **برای هر منبع جداگانه** است (بخش ۰.۳.۳ گام ۶). *تمام شد وقتی:* دستور گام ۵ هنوز نام‌ها را چاپ کند.

۱۰. **صدور کلیدهای اول** (بخش ۲.۲): `python -m scripts.issue_api_key --id admin-1 --name "Admin" --full-admin`، ورودی چاپ‌شده را به `project_config/api_keys.json` اضافه کنید (از `project_config.example/api_keys.example.json` شروع کنید) و `API_KEYS_FILE=project_config/api_keys.json` را در `.env` بگذارید. این گام پیش از پیش‌پرواز است چون چک `API key authentication` تا وقتی هیچ کلیدی نباشد شکست می‌خورد. *تمام شد وقتی:* کلید خام را (یک‌بار چاپ می‌شود) دارید و ورودی‌اش در فایل است. کلید هر تحلیل‌گر در گام ۱۶ می‌آید.

۱۱. **پیش‌پرواز** (بخش ۰.۴): `python -m scripts.verify_deployment` با `VERIFY_API_KEY` برابر کلید خام گام ۱۰. *تمام شد وقتی:* خط آخر `N passed, 0 failed, N skipped` باشد. جدول بخش ۰.۴ می‌گوید هر خط یعنی چه و هر `[FAIL]` چطور رفع می‌شود.

۱۲. **بودجهٔ پرامپت** (بخش ۰.۳.۳ گام ۵): `python scripts/prompt_budget.py` (با یک دیتابیس هم کار می‌کند)، و خط `PROMPT_RETRIEVAL_TOKEN_BUDGET=` را که چاپ می‌کند در `.env` بگذارید. *تمام شد وقتی:* آن خط در `.env` باشد. کد خروج ۱ یعنی منبعی در پنجرهٔ زمینهٔ مدل جا نمی‌شود و خروجی می‌گوید کدام.

۱۳. **راه‌اندازی سرور** (بخش ۲.۳ و ۲.۴): `uvicorn api.server:app --port 8000 --no-server-header` از ریشهٔ ریپو، و از `web/` فایل‌سرور استاتیک `python -m http.server 8080`. *تمام شد وقتی:* در لاگ بنر مالکیت، `CORS allowed origins: ...` و `System prompt loaded (N chars)` (و با چند منبع برای هر منبع یک خط `Prompt path for data source '<name>'`) را ببینید و `curl http://localhost:8000/health` مقدار `status` را `ok` بدهد.

۱۴. **یک پرسش واقعی بپرسید و ثبت‌شدنش را ببینید:** یک `POST /query` احرازشده و بعد `Get-Content logs/audit_log.jsonl -Tail 1`. *تمام شد وقتی:* آن سطر `timestamp` امروز و `request_id` همان پاسخ را داشته باشد (جزئیات: بخش ۶ از `docs/deployment-runbook.md`).

۱۵. **پیش‌پرواز را دوباره بزنید** بعد از تغییر `.env` در گام ۱۲، و بعد از هر تغییر بعدی در `.env` یا `project_config/`. *تمام شد وقتی:* هر بار `0 failed`.

۱۶. **کلید هر تحلیل‌گر** (بخش ۲.۲): برای هر نفر یک کلید صادر کنید، ورودی را به فایل اضافه کنید و سرور را دوباره راه بیندازید (فایل فقط هنگام شروع خوانده می‌شود). *تمام شد وقتی:* هر تحلیل‌گر بتواند UI را باز کند و پرسش بپرسد.

۱۷. **مجموعهٔ طلایی** (بخش ۵ همین راهنما): وقتی پرسش‌های واقعی در audit log جمع شد بسازیدش و برای ارتقاها نگه دارید. *تمام شد وقتی:* `python -m eval.cli verify --golden eval_data/golden.jsonl --accept` موردهای سالم را `active` کرده باشد.

---

## بخش ۰ — پیش‌نیازها (یک‌بار)

### ۰.۱ نصب

```powershell
cd <repo-root>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.lock
```

از `requirements.lock` نصب کنید، نه از `requirements.txt`: فایل lock نسخهٔ دقیق همهٔ بسته‌ها را قفل می‌کند، و `sqlglot` که همهٔ تصمیم‌های نگهبان SQL از روی درخت نحوی آن گرفته می‌شود یکی از همین‌هاست؛ ارتقای بی‌بازبینی آن تنها تغییر وابستگی‌ای است که بی‌هیچ تغییری در این مخزن می‌تواند رفتار امنیتی را عوض کند. بعد از هر `git pull` همین دستور را دوباره بزنید.

### ۰.۲ فایل `.env`

```powershell
copy .env.example .env
```

بعد این موارد را در `.env` پر کنید:

```ini
DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/DB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
DB_PASSWORD=<رمز عبور دیتابیس، همان‌طور که هست>
OPENAI_BASE_URL=http://<llm-host>:<llm-port>/v1
OPENAI_MODEL=<نام مدل روی همان endpoint>
```

> **رمز دیتابیس را «خام» بنویسید، نه داخل URL.** مقدار `DB_PASSWORD` همان
> رمزی است که خودِ دیتابیس می‌شناسد، بدون هیچ کدگذاری: اگر رمز شما
> `p@ss/w:rd#1` است، دقیقاً همین را بنویسید. برنامه رمز را روی URL
> تجزیه‌شده می‌نشاند و هر نویسهٔ خاصی را خودش درست escape می‌کند. `URL`
> باید فقط نام کاربری داشته باشد، نه رمز.
>
> اگر `DB_PASSWORD` را نگذارید، URL همان‌طور که نوشته‌اید استفاده می‌شود؛
> پس رمزِ داخل آن را باید خودتان کدگذاری کنید (`@` می‌شود `%40`، `/` می‌شود
> `%2F`، `%` می‌شود `%25`). برنامه رمزِ نوشته‌شده داخل URL را عمداً حدس
> نمی‌زند و دوباره کدگذاری نمی‌کند، چون رمزی که `/ ? # :` یا حتی خودِ
> `%40` را دارد از رمزِ کدگذاری‌شده قابل تشخیص نیست و حدس زدن
> پیکربندی‌های سالم را بی‌صدا خراب می‌کند. اگر هم `DB_PASSWORD` و هم رمزِ
> داخل URL را بگذارید، سرور بالا نمی‌آید، چون معلوم نیست کدام معتبر است.
> با Windows authentication (`trusted_connection=yes` در URL) نه نام
> کاربری لازم است نه رمز.

و این یکی را **با مدل محلی معمولاً خالی بگذارید**:

```ini
OPENAI_API_KEY=
```

> **با مدل محلی، `OPENAI_API_KEY` تقریباً همیشه خالی می‌ماند.** اکثر
> سرورهای محلی OpenAI-compatible (LM Studio، Ollama، llama.cpp) اصلاً
> اعتبارنامه چک نمی‌کنند، و `Settings.validate()` هم الزامی‌اش نمی‌کند —
> فقط `OPENAI_MODEL` و `DB_CONNECTION_URL` الزامی‌اند.
>
> یعنی در یک استقرار با مدل محلی، **در کل سیستم یک توکن واقعی بیشتر
> ندارید**: کلید تحلیل‌گر. جدول بخش ۲.۲.۱ سه ردیف دارد چون سه *جا* را
> نشان می‌دهد، نه سه راز.
>
> ضمناً از ۴.۱.۲ به بعد، کلیدِ خالی یعنی هدر `Authorization` **اصلاً
> فرستاده نمی‌شود** — نه `Bearer ` خالی. این تفاوت قبلاً باعث می‌شد چراغ
> LLM قرمز شود در حالی که همان endpoint به CLI جواب می‌داد.

**`DB_APPLICATION_NAME` (اختیاری).** نامی که هر اتصال به انبار داده به‌عنوان `program_name` به SQL Server معرفی می‌کند تا DBA بتواند نشست‌های این برنامه را از بقیهٔ کلاینت‌ها تشخیص دهد؛ پیش‌فرض `local-sql-agent` است. فقط به اتصال‌های `mssql+pyodbc` اضافه می‌شود و اگر خود URL یک `APP=` یا `Application Name=` داشته باشد جایش را نمی‌گیرد. هر منبع در `datasources.yaml` می‌تواند با `application_name:` نام خودش را داشته باشد. برای دیدن نشست‌های برنامه و اینکه تراکنش بازی دارند یا نه، DBA (یا شما، اگر اجازهٔ خواندن DMVها را دارید) این پرسش را می‌زند؛ اگر نام دیگری تنظیم کرده‌اید، آن را به‌جای نام پیش‌فرض بگذارید:

```sql
SELECT s.session_id, s.login_name, s.host_name, s.program_name, s.status,
       s.last_request_start_time, s.open_transaction_count
FROM sys.dm_exec_sessions AS s
WHERE s.program_name = N'local-sql-agent'
ORDER BY s.last_request_start_time DESC;
```

این همان پرسش اولِ بخش ۲a در `docs/dba/warehouse-load-diagnostics.sql` با ستون‌های کمتر است؛ بخش ۲b همان‌جا دستورهای در حال اجرا و ۲c بلاک‌شدن را نشان می‌دهد و `docs/dba/README.md` می‌گوید چطور خوانده شوند (در `docs/db-hardening.md` هم به همین `program_name` اشاره شده است). چون executor هر کوئری را در تراکنشی اجرا می‌کند که همیشه rollback می‌شود، نشستِ بی‌کاری که `open_transaction_count`اش بالای صفر بماند جای پرسیدن دارد.

> **از ۴.۵.۰ یک دیتابیس اپلیکیشن هم لازم است — ولی معمولاً کاری ندارید.**
> کلیدهای API، نقش‌های ادمین و سشن‌ها از این نسخه در یک دیتابیس زندگی
> می‌کنند، نه فقط در `.env`. اگر `APP_DB_URL` را خالی بگذارید (پیش‌فرض)،
> خودش یک فایل SQLite در `logs/app.db` می‌سازد و هیچ تنظیمی لازم نیست.
>
> اگر سازمان شما DBA و برنامهٔ پشتیبان دارد، می‌توانید به‌جایش یک دیتابیس
> واقعی بدهید:
>
> ```ini
> APP_DB_URL=postgresql+psycopg://user:pass@host:5432/nlq_app
> ```
>
> دیتابیس باید **از قبل وجود داشته باشد** — این برنامه جدول‌ها را داخلش
> می‌سازد، ولی خود دیتابیس را نه.
>
> **این هرگز نباید همان دیتابیس انبار داده باشد.** سرور موقع بالا آمدن
> چک می‌کند و اگر هر دو به یک جا اشاره کنند بالا نمی‌آید — چون انبار با
> لاگین read-only خوانده می‌شود و نوشتن‌های خودِ ما آن وضعیت را بی‌صدا
> از بین می‌برد.

> **مراقب تداخل پورت باشید.** مقدار پیش‌فرض در `.env.example` برای
> `OPENAI_BASE_URL` روی `localhost:8000` است و بک‌اند خودِ این پروژه هم
> پیش‌فرض روی `8000` بالا می‌آید. اگر LLM شما روی همان ماشین و همان پورت
> است، یکی از این دو را عوض کنید وگرنه سرور بالا نمی‌آید.

### ۰.۳ `project_config/` — بدون این سرور بالا نمی‌آید

این پوشه دادهٔ دامنهٔ شماست و عمداً در گیت نیست. نُه فایل YAML باید سر
جایشان باشند:

```
project_config/
├── aliases.yaml
├── business_rules.yaml
├── entities.yaml
├── examples.yaml
├── memory_policy.yaml      ← جدید در ۴.۱.۰
├── metrics.yaml
├── retrieval_hints.yaml
├── schema.yaml
└── session_policy.yaml
```

نبودِ هرکدام یعنی `ConfigNotFoundError` هنگام استارت. **هیچ fallback بی‌صدایی
به `project_config.example/` وجود ندارد** — این عمدی است: اجرا شدن روی
قواعد نمونه، SQL با اطمینانِ غلط تولید می‌کند که از بالا نیامدن بدتر است.

علاوه بر این فایل‌های YAML، یک فایل متنیِ دیگر هم باید همین‌جا باشد:
`system_prompt.md` (دستورالعمل سیستمیِ مدل زبانی). این فایل دیگر در
`prompts/system_prompt.md` نیست — به همین پوشهٔ `project_config/` منتقل شده.
نبودِ آن هم سرور را بالا نمی‌آورد، دقیقاً به همان شکلِ نبودِ فایل‌های YAML:

```
RuntimeError: System prompt not found: <مسیرِ resolve‌شده>
```

برای ساختنش، `project_config.example/system_prompt.md` را کپی و برای دامنهٔ
واقعیِ خودتان بازنویسی کنید.

**اگر فقط یک انبار داده دارید** — یعنی اکثر استقرارها — همین کافی است و می‌توانید مستقیم بروید سراغ ۰.۴.

### ۰.۳.۱ ویزارد راه‌اندازی `setup_project.py`

ابزاری یک‌باره و **اختیاری** که با کمک یک مدل زبانی، از روی دیتابیس زنده پیش‌نویس چهار فایل از فایل‌های بالا (به‌علاوهٔ `relationships.yaml`) را می‌سازد. هر چه می‌نویسد باید بازبینی شود: هر فایلش با `# AUTO-GENERATED by setup_project.py — review before use` شروع می‌شود. بعد از کپی قالب (بخش ۰.۳) و با `.env` پر، از ریشهٔ ریپو اجرایش کنید (خودش `.env` را می‌خواند):

```powershell
python setup_project.py `
    --db-url "mssql+pyodbc://reader:p%40ss@dbhost:1433/WarehouseDB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes" `
    --llm-base-url http://<llm-host>:<llm-port>/v1 `
    --llm-model <نام مدل روی همان endpoint> `
    --language fa
```

اگر نگویید، در هر گام می‌پرسد. گزینه‌ها:

| گزینه | معنی |
|---|---|
| `--db-url URL` | دیتابیسی که باید توصیف شود. بدون آن: `DB_CONNECTION_URL`، بعد `DATABASE_URL`؛ اگر هیچ‌کدام نبود، حالت تعاملی می‌پرسد و `--non-interactive` متوقف می‌شود. |
| `--llm-provider openai\|mock` | `openai` (پیش‌فرض) هر endpoint سازگار با OpenAI است؛ `mock` مدلی صدا نمی‌زند و نام‌های مستعار، قواعد و مثال‌ها خالی می‌مانند. |
| `--llm-model NAME` | اگر `WIZARD_LLM_MODEL` تنظیم نشده باشد، `gpt-4o-mini`. |
| `--llm-base-url URL` | اگر `WIZARD_LLM_BASE_URL` تنظیم نشده باشد، `https://api.openai.com/v1`. |
| `--language fa\|en\|both` | زبان پرسش تحلیل‌گرها؛ یا `WIZARD_LANGUAGE`. تنظیم‌نشده: تعاملی می‌پرسد (پیش‌فرض `en`) و غیرتعاملی `en`. |
| `--output DIR` | محل نوشتن (پیش‌فرض `project_config`). |
| `--review interactive\|auto` | `auto` همان `--non-interactive` است. |
| `--non-interactive` | همهٔ پیشنهادها را بدون پرسیدن می‌پذیرد؛ برای اسکریپت. |
| `--include-schemas a,b` | فقط این شِماهای دیتابیس. |
| `--dry-run` | فایل‌ها را چاپ می‌کند، چیزی نمی‌نویسد و گام اعتبارسنجی را رد می‌کند. |
| `--resume` | فایلی را که از قبل هست نمی‌نویسد. |

گام‌ها به ترتیب؛ هر گام در `<output>/.setup_log.json` زیر کلید نشان‌داده‌شده ثبت می‌شود:

| کلید | گام | چه می‌شود |
|---|---|---|
| `step1_connection` | اتصال | URL را باز می‌کند و `SELECT 1` می‌زند؛ در شکست خطا را چاپ می‌کند و در حالت تعاملی تلاش دوباره پیشنهاد می‌دهد. |
| `step2_schema` | کشف شِما | جدول‌ها، ستون‌ها، کلیدهای خارجی و تا پنج مقدار نمونه برای هر ستون متنی را می‌خواند و هر جدول را fact یا dim می‌نامد. در حالت تعاملی می‌پرسد کدام جدول‌ها کنار بروند. |
| `step3_aliases` | نام‌های مستعار | برای هر جدول یک فراخوانی مدل برای نام‌های مستعار و توضیح یک‌خطی؛ تعاملی می‌توانید بپذیرید، ویرایش کنید یا پاک کنید. |
| `step4_rules` | قواعد کسب‌وکار | برای هر جدول fact یک فراخوانی مدل برای ستون ارزش و حجم و متن قاعده. |
| `step5_examples` | مثال‌ها | یک فراخوانی مدل که ده جفت پرسش و SQL می‌خواهد. |
| `step6_write` | بازبینی و نوشتن | `entities.yaml`، `aliases.yaml`، `business_rules.yaml`، `examples.yaml` و `relationships.yaml` را در `--output` می‌نویسد. تعاملی هر فایل اول نشان داده می‌شود: Accept، Edit in `$EDITOR`، Regenerate یا Skip. |
| `step7_validate` | اعتبارسنجی | چهار فایلی را که loader دارند با اعتبارسنج‌های خود برنامه می‌خواند و برای هر کدام `OK`، `FAILED: ...` یا `skipped` چاپ می‌کند، بعد `Setup complete.` و تعداد موجودیت‌ها، قواعد و مثال‌ها. |

**آنچه نمی‌کند:**

- **`schema.yaml` را نمی‌نویسد**، پس فهرست مجاز نگهبان SQL را نمی‌سازد؛ برایش بخش ۰.۳.۲ یا دست‌نویس. `metrics.yaml`، `retrieval_hints.yaml`، دو فایل policy، `system_prompt.md`، `datasources.yaml`، `.env` و هیچ کلید API را هم نمی‌نویسد.
- **`datasource:` هیچ جدولی را تعیین نمی‌کند.** در هر اجرا یک دیتابیس را توصیف می‌کند و از `datasources.yaml` خبر ندارد؛ با چند دیتابیس کارش `python scripts/sync_schema.py` است (بخش ۰.۳.۳).
- **`DB_PASSWORD`، `DB_PASSWORD_*` و `datasources.yaml` را نمی‌خواند.** URL که می‌دهید باید خودش رمز را داشته باشد، کدگذاری‌شده (`@` می‌شود `%40`). دادنش با `--db-url` در تاریخچهٔ شل می‌ماند؛ متغیر محیطی `DATABASE_URL` این را ندارد.
- **`OPENAI_BASE_URL` و `OPENAI_MODEL` را نمی‌خواند.** تنظیم‌های خودش را دارد که بر آن‌ها مقدم است: گزینه‌های بالا یا متغیرهای `WIZARD_LLM_PROVIDER`، `WIZARD_LLM_MODEL`، `WIZARD_LLM_BASE_URL` و `WIZARD_LANGUAGE`. اگر هیچ‌کدام نباشند، از `https://api.openai.com/v1` مدل `gpt-4o-mini` را می‌خواهد. `.env.example` هر چهار را تنظیم کرده، پس `.env`ای که از آن کپی شده به ویزارد مدل `gpt-oss-20b`، زبان `fa` و نشانی endpointِ **خالی** (`WIZARD_LLM_BASE_URL=`) می‌دهد؛ نشانی خالی به جایی نمی‌رسد، پس آن متغیر را پر کنید یا `--llm-base-url` را با endpoint خودتان بدهید. `OPENAI_API_KEY` هم باید خالی نباشد. اگر کلید نباشد یا endpoint در دسترس نباشد، `Warning: LLM unavailable (...)` چاپ می‌کند و با ارائه‌دهندهٔ mock ادامه می‌دهد، یعنی نام‌های مستعار، قواعد و مثال‌ها خالی درمی‌آیند. `LLM_ALLOW_REMOTE` را اعمال نمی‌کند. چیزی که به مدل می‌فرستد نام جدول‌ها و ستون‌ها، تا ده مقدار نمونه برای هر جدول (از انبار داده) و خلاصهٔ شِماست؛ اگر این مقدارها نباید از شبکه بیرون بروند، به endpoint راه‌دور وصلش نکنید.

**پیش از اجرا بدانید:**

- **فایل‌های موجود را بازنویسی می‌کند.** اگر `project_config/` را از قالب کپی کرده‌اید (بخش ۰.۳)، آن پنج فایل بدون پشتیبان با خروجی ویزارد عوض می‌شوند؛ در حالت تعاملی هم پس از Accept. اول از پوشه کپی بگیرید یا با `--output project_config_draft` اجرا کنید و آنچه می‌خواهید را منتقل کنید. `--resume` در پوشه‌ای که این فایل‌ها را دارد چیزی نمی‌نویسد، و گام‌های ۱ تا ۵ را هم رد نمی‌کند (مدل باز صدا زده می‌شود): لاگ یک ثبت است، نه نقطهٔ بازگشت.
- **تا زمان نگارش، گام ۷ به یک `project_config/` پر نیاز دارد.** بستهٔ `knowledge` را import می‌کند که `aliases`، `business_rules`، `entities`، `examples` و `metrics` را از `PROJECT_CONFIG_DIR` (نه از `--output`) می‌خواند. روی درختی که قالب در آن کپی نشده ممکن است بعد از آنکه گام ۶ فایل‌ها را نوشته با `ConfigNotFoundError: ... metrics.yaml not found` تمام شود. اول کپی قالب را بزنید تا پیش نیاید.
- **`.setup_log.json` ممکن است URL دیتابیس را، با رمزی که در آن نوشته‌اید، داشته باشد.** آن را با کسی به اشتراک نگذارید (بخش ۶) و بعد از اتمام اجرا پاکش کنید. (کامنت `# Source:` در `entities.yaml` رمز را پوشانده است.)
- **در منوی بازبینی Accept، Edit یا Skip را بزنید؛** تا زمان نگارش Regenerate همان متن را دوباره نشان می‌دهد.

### ۰.۳.۲ پیش‌نویس `schema.yaml`: `database.schema_inspector_cli`

```powershell
python -m database.schema_inspector_cli `
    --db-url "mssql+pyodbc://reader:p%40ss@dbhost:1433/WarehouseDB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes" `
    --output-dir project_config_draft `
    --include-schemas sales,ref
```

بدون `--db-url`، متغیر محیطی `DATABASE_URL` یا `DB_CONNECTION_URL` همان شلی را که در آن اجرا می‌کنید می‌خواند (برخلاف ویزارد، `.env` را نمی‌خواند)؛ و مثل ویزارد `DB_PASSWORD` را اعمال نمی‌کند، پس رمز باید کدگذاری‌شده داخل URL باشد. گزینه‌های دیگر: `--exclude-tables a,b`، `--sample-rows N` (پیش‌فرض ۱۰؛ `0` خواندن مقدار نمونه را رد می‌کند)، `--no-row-counts` (بدون `COUNT(*)` برای هر جدول) و `--dry-run` (به‌جای نوشتن چاپ می‌کند). چهار فایل `schema.yaml`، `entities.yaml`، `aliases.yaml` و `relationships.yaml` را در پوشهٔ پیش‌نویس می‌نویسد، هرگز در `project_config/` یا `project_config.example/` (این دو نام را با کد خروج ۲ رد می‌کند) و در پایان `Done.` چاپ می‌کند. پوشهٔ پیش‌نویس در گیت نادیده گرفته می‌شود.

پیش‌نویس نقطهٔ شروع است، نه `schema.yaml`ای که مستقر کنید: هر توضیحش یک جانگهدار است (`TO BE FILLED`)، یادداشت ستون‌ها ممکن است مقدارهای واقعی ردیف‌های زنده را نقل کند، و هیچ جدولی `db_schema` ندارد. تا وقتی آن یادداشت‌ها را بازنویسی نکرده‌اید پوشه را حساس بدانید، توضیح درست بنویسید، به هر جدول `db_schema` بدهید (بخش ۰.۳.۳ گام ۲ و `docs/design/TABLE-NAMES.md`) و بعد `schema.yaml` را در `project_config/` بگذارید. بعد از ویرایش `tests/test_schema_registry_snapshot.py` را اجرا کنید و `python -m scripts.verify_deployment` (بخش ۰.۴) را برای بارگذاری آن بزنید.

### ۰.۳.۳ اگر باید روی بیش از یک پایگاه داده کوئری بزنید

مثلاً دو دیتابیس روی یک سرور، یا یک سرور SQL Server جدا برای آرشیو. در این حالت یک فایل دهم و اختیاری هم به `project_config/` اضافه می‌شود: `datasources.yaml`. این فایل **خودِ اتصال را توصیف می‌کند** — آدرس سرور، پورت، نام دیتابیس، درایور و نام کاربری — و فقط رمز عبور بیرون از آن، در `.env`، می‌ماند. چون این فایل مثل `schema.yaml` نسخه‌بندی و ریویو می‌شود، رمز هرگز داخلش نمی‌رود؛ فقط *نام* متغیری که رمز را نگه می‌دارد. جزئیات قدم‌به‌قدم، با دستورها، در بخش ۱۶ از `docs/deployment-runbook.md` است و دلیل طراحی در `docs/design/DATASOURCES.md`؛ مسیر کوتاهش این است:

۱. **توصیف منبع‌ها.** `project_config.example/datasources.example.yaml` را به `project_config/datasources.yaml` کپی کنید. مثال: دو دیتابیس روی یک سرور، به‌صورت دو منبع.

```yaml
default: sales
datasources:
  sales:
    description: انبار فروش
    keywords: [revenue, invoice, فروش]
    host: 10.0.0.5
    database: SalesDW
    username: nlq_reader
    password_env: DB_PASSWORD_SALES
    options:
      TrustServerCertificate: true
  inventory:
    description: انبار موجودی
    keywords: [stock level, reorder, موجودی, انبار]
    host: 10.0.0.5
    database: InventoryDW
    username: nlq_reader
    password_env: DB_PASSWORD_INVENTORY
```

و در `.env` فقط رمزها، **خام و بدون هیچ کدگذاری**:

```ini
DB_PASSWORD_SALES=p@ss/w:rd#1
DB_PASSWORD_INVENTORY=another-password
```

پورت پیش‌فرض ۱۴۳۳ و درایور پیش‌فرض `ODBC Driver 18 for SQL Server` است. برای Windows authentication به‌جای نام کاربری و رمز بنویسید `trusted_connection: true`. اگر `datasources.yaml` وجود داشته باشد، `DB_CONNECTION_URL` و `DB_PASSWORD` استفاده نمی‌شوند. دو دیتابیس روی یک سرور، به‌صورت دو منبع، یک پیکربندی کاملاً معتبر است: هر منبع استخر اتصال و لاگین خودش را دارد و هر کوئری فقط روی یکی از آن‌ها اجرا می‌شود. اگر سؤال‌ها باید جدول‌های **هر دو** دیتابیس را با `JOIN` به هم وصل کنند، یک منبع بسازید و برای جدول‌های دیتابیس دوم در `schema.yaml` یک `db_schema` چندبخشی بگذارید (مثلاً `db_schema: "InventoryDW.dbo"`). منبع‌هایی که با ۶.۱ و ۶.۲ نوشته شده‌اند (`url_env`) همچنان کار می‌کنند.

۲. **همگام‌سازی `schema.yaml` با دیتابیس‌ها و نوشتن `datasource:` هر جدول.** جدولی که این کلید را ندارد روی منبع پیش‌فرض اجرا می‌شود؛ جدولی که با همین ساختار در چند منبع هست (مثلاً جدول تاریخ تکرارشده) فهرست می‌گیرد: `datasource: [sales, inventory]` (نام‌ها با همان حروف `datasources.yaml`). `schema.yaml` از جهت‌های دیگر هم از دیتابیس عقب می‌ماند: ستون تازه، ستون حذف‌شده، نوعی که عوض شده. دستی دنبالشان نگردید؛ یک بار از ریشهٔ پروژه اجرا کنید:

```powershell
python scripts/sync_schema.py
```

ساختار هر منبع را فقط می‌خواند (سه پرس‌وجو از `INFORMATION_SCHEMA` برای هر منبع: جدول، view، ستون، نوع داده، nullable و کلیدهای اصلی و خارجی؛ هیچ ردیفی خوانده و چیزی در دیتابیس نوشته نمی‌شود) و `schema.synced.yaml` را کنار `schema.yaml` می‌سازد؛ خودِ `schema.yaml` را هرگز تغییر نمی‌دهد و `--output PATH` مسیر دیگری می‌دهد. پیشنهاد همان فایل شماست با همهٔ کامنت‌ها، توضیح‌ها، پرچم‌ها و ترتیب، و فقط این تغییرهای ساختاری (اسکریپت خروجی خودش را تجزیه و مقایسه می‌کند و اگر چیز دیگری فرق داشته باشد نمی‌نویسد):

| وضعیت | کار پیشنهاد |
|---|---|
| جدول `datasource:` ندارد یا غلط دارد (فقط چند منبع) | آن را می‌نویسد: `datasource: sales` یا `[sales, inventory]` برای جدولی که در هر دو هست، به ترتیب `datasources.yaml` |
| ستون در دیتابیس هست و در `schema.yaml` نیست | آخر `columns:` جدول اضافه‌اش می‌کند: `Name: "<type> column"  # TO BE FILLED (added by sync_schema.py)` و نوعش را ثبت می‌کند |
| ستون در `schema.yaml` هست و در هیچ منبعی نیست | نگهش می‌دارد و بالایش `# not in database (sync_schema.py)` می‌گذارد؛ `--prune` به‌جایش حذفش می‌کند (ستونی که هنوز در `resolvable_columns` یا `prefetchable_columns` است نه) |
| نوع ثبت‌شده با دیتابیس فرق دارد | مدخل نقشهٔ `column_types:` جدول را درست می‌کند و `Table.Column: old -> new` گزارش می‌دهد. نوع‌ها در همین نقشه‌اند، هرگز در توضیح ستون |
| جدول در دیتابیس هست و در `schema.yaml` نیست | فقط گزارش می‌دهد؛ `--add-tables 'sales.*'` (الگوی glob روی `schema.table`، تکرارشدنی) جدول‌های منطبق را با توضیح پیش‌نویس اضافه می‌کند |
| جدول در `schema.yaml` هست و در هیچ منبعی نیست | با `# not found in any data source` نگهش می‌دارد؛ `--prune` حذفش می‌کند (مگر `relationships:` هنوز نامش را ببرد) |
| جدول بدون کلید `columns:` | دست نمی‌خورد (فقط توصیف‌شده، عمداً غیرقابل‌پرس‌وجو) |

`column_types:` نقشهٔ اختیاری تازه‌ای برای هر جدول است، `{ستون: نوع SQL}`، که فقط همین اسکریپت می‌نویسد؛ زمان اجرا چیزی از آن نمی‌خواند، پس پرامپت و فهرست مجاز نگهبان عوض نمی‌شوند.

`relationships.proposed.yaml` را هم کنار `schema.yaml` می‌نویسد: هر کلید خارجی تعریف‌شده بین دو جدول `schema.yaml`، و رابطه‌هایی که از نام ستون حدس می‌زند (`Order.CustomerID` به `Customer.ID`، `Order.OrderDate_ID` به `Date.ID`؛ schema یکسان ترجیح دارد، دو جدول باید منبع مشترک داشته باشند، و حدس وقتی کنار گذاشته می‌شود که هدف کلید مرکب، بی‌کلید، نوع ستون متفاوت یا چند گزینه داشته باشد). هر مدخل `basis:` (قید یا قاعدهٔ نام‌گذاری) و `confidence:` دارد. هیچ چیز این فایل را نمی‌خواند و `relationships.yaml` هرگز لمس نمی‌شود: هر مدخل را مرور و درست‌ها را دستی کپی کنید. `--no-relationships` آن را نمی‌سازد.

`--dry-run` فقط گزارش را چاپ می‌کند. ستونی که لاگین فقط‌خواندنی از طریق `INFORMATION_SCHEMA` نمی‌بیند هم «ناموجود» گزارش می‌شود، پس پیش از `--prune` دربارهٔ `DENY` (در `docs/db-hardening.md`) از DBA بپرسید.

سپس `schema.synced.yaml` را مرور کنید (جدول‌های «پیدا نشده» و هر `[A, B]`، چون فهرست یعنی جدول در هر دو منبع ساختار یکسان دارد) و جای `schema.yaml` بگذارید، اما اول از فایل فعلی نسخهٔ پشتیبان بگیرید:

```powershell
Copy-Item project_config\schema.yaml project_config\schema.yaml.bak
Move-Item -Force project_config\schema.synced.yaml project_config\schema.yaml
```

`schema.yaml` فهرست مجاز نگهبان است، پس هر تغییرش با راه‌اندازی دوبارهٔ سرور اثر می‌کند. ستونی که در `schema.yaml` بماند و در دیتابیس نباشد را نگهبان همچنان مجاز می‌داند، پس پرسشی که از آن استفاده کند هنگام اجرا شکست می‌خورد؛ هر ستون `# not in database` را حذف کنید (یا `--prune` بزنید)، املایش را درست کنید یا از DBA بپرسید، و برای ستون‌ها و جدول‌های `# TO BE FILLED` توضیح واقعی بنویسید (تا آن موقع مدل همان `<type> column` را می‌بیند). در آخر `python scripts/sync_schema.py --check` چیزی نمی‌نویسد؛ اگر اجرای معمولی `schema.yaml` را تغییر می‌داد `CHECK FAILED: ...` چاپ می‌کند و با کد ۱ تمام می‌شود، که برای pipeline استقرار مناسب است، و در حالت سالم `CHECK OK: schema.yaml's structure matches the databases` و کد ۰ است. ستون نشان‌شده با `# not in database` شکست نمی‌دهد (در هر گزارش می‌آید تا حذف شود)؛ `--check --prune` تا چیزی برای حذف مانده شکست می‌خورد و `--check --add-tables PATTERN` تا جدول‌های منطبق در فایل نیامده‌اند. کد ۲ یعنی فهرست یک منبع خوانده نشد، `schema.yaml` نامعتبر است یا چیدمانی دارد که خط‌به‌خط ویرایش نمی‌شود (جدول یا `columns: {...}` به‌سبک flow)، یا خروجی معتبر نبود. اجرای دوباره روی فایل جایگزین‌شده چیزی را تغییر نمی‌دهد.

**`assign_datasources.py` هنوز هست و ابزار محدودتر است.** فقط خط‌های `datasource:` را می‌نویسد (`schema.with_datasources.yaml`) و ستون‌های ناموجود را گزارش می‌دهد؛ ستون اضافه نمی‌کند، نوع ثبت نمی‌کند و چیزی را نشان‌گذاری نمی‌کند، و تطبیق و ویرایش `datasource:` را با `sync_schema.py` به‌اشتراک دارد (همان قاعده، همان کد). وقتی دقیقاً همین را می‌خواهید و هیچ تغییر دیگری در `schema.yaml` نمی‌خواهید از آن استفاده کنید. هرچه آن می‌کند `sync_schema.py` هم می‌کند، پس گام اول توصیه‌شده نیست و برای کارکردن pipeline‌های موجود می‌ماند.

۳. **هدایت پرسش‌ها.** با چند منبع، هر پرسش پیش از ساخت پرامپت به **یک** منبع هدایت می‌شود و مدل فقط جدول‌های همان منبع (به‌اضافهٔ جدول‌های مشترک) را می‌بیند. `description:` بالای جدول‌های منبع در پرامپت چاپ می‌شود و `keywords:` فهرست واژه یا عبارت فارسی یا انگلیسی است. کلیدواژه به‌صورت «کلمهٔ کامل» پس از یکسان‌سازی حرف‌های عربی/فارسی، ارقام، نیم‌فاصله و بزرگی/کوچکی حروف با پرسش مقایسه می‌شود: `stock` داخل `stockholder` پیدا نمی‌شود و شکل جمع یا پیشوندی واژهٔ دیگری است، پس هر شکلی را که انتظار دارید بنویسید. اگر کلیدواژه‌ای پیدا نشود، انتخاب بر اساس ادامهٔ گفتگو، سپس جدول‌هایی که لایهٔ بازیابی پیدا می‌کند و در آخر منبع پیش‌فرض است؛ اگر مدل `OUT_OF_SCOPE` بگوید، پرسش **یک بار** با منبع بعدی تکرار می‌شود (CLI تکرار نمی‌کند).

۴. **پیش‌پرواز و راه‌اندازی دوباره.** `python -m scripts.verify_deployment` (بخش ۰.۴) برای هر منبع یک‌بار چک‌های دیتابیس را اجرا می‌کند و دو چک ویژهٔ چند منبع دارد: «`Tables map to data sources`» و «`Tables are in their data source`» که برای جدولی که همهٔ ستون‌هایش در منبع تعیین‌شده نیست ولی در منبع دیگری هست خطی مثل `stock_dim.Broker: not in sales, found in inventory — set datasource: inventory` را می‌نویسد. اگر هرگز `sync_schema.py` (یا `assign_datasources.py`) را نزده باشید، همین چک با خطی مثل `[FAIL] Tables are in their data source -- 44 table(s): ... not in sales, found in inventory — set datasource: inventory` شکست می‌خورد؛ راهش اجرای آن اسکریپت و جایگزینی `schema.yaml` است (گام ۲ بالا). چک `Schema structure matches the databases` همان آزمون `sync_schema.py --check` است و اگر ساختار `schema.yaml` از دیتابیس عقب باشد خطا می‌دهد. جدول همهٔ چک‌ها در بخش ۰.۴ است. بعد سرور را دوباره راه بیندازید و در لاگ شروع برای هر منبع یک خط `Prompt path for data source '<name>'` ببینید.

۵. **اندازهٔ بودجهٔ پرامپت.** `PROMPT_RETRIEVAL_TOKEN_BUDGET` برای پرامپت هر منبع جداگانه سنجیده می‌شود، نه برای مجموع آن‌ها، و برآوردگر (`len // 4`) برای متن فارسی حدود ۱۵٪ کمتر می‌شمارد. به‌جای حدس زدن، از ریشهٔ پروژه اجرا کنید:

```powershell
python scripts/prompt_budget.py
```

اندازهٔ پیشوند هر منبع را چاپ می‌کند، تعداد واقعی توکن را از مدل می‌پرسد، آن را با طول زمینهٔ مدل می‌سنجد و خط `PROMPT_RETRIEVAL_TOKEN_BUDGET=` را که باید در `.env` بگذارید چاپ می‌کند. با `--no-model` به مدل وصل نمی‌شود (آن‌وقت `--context-length` بدهید)؛ شمارش توکن واقعی کش پیشوند سرور مدل را گرم می‌کند، پس پیش از باز کردن سرویس برای کاربران اجرایش کنید. کد خروج ۱ یعنی منبعی در پنجرهٔ زمینه جا نمی‌شود و باید روی مسیر بازیابی بماند.

۶. **`NOLOCK`، فقط اگر DBA الزام کرده.** در `datasources.yaml` برای همان منبع `nolock: true` بنویسید (هم در شکل ساخت‌یافته و هم در `url_env`؛ پیش‌فرض `false`). اجراکننده درست پیش از ارسال هر دستور به آن منبع، ` WITH (NOLOCK)` را بعد از هر ارجاع به جدول واقعی (بعد از alias) اضافه می‌کند و به بقیهٔ متن دست نمی‌زند؛ `generated_sql` در audit log همان SQL تأییدشدهٔ بدون hint می‌ماند. چون hint جدول مخصوص T-SQL است، با `SQL_DIALECT` غیر از `tsql` سرور بالا نمی‌آید. **`NOLOCK` یعنی dirty read**: ممکن است ردیف‌های commit‌نشده دیده شود و گاهی یک ردیف دو بار یا اصلاً دیده نشود. تصمیم اپراتور است و برای هر منبع جدا گرفته می‌شود: **`nolock: true` روی یک منبع به منبع‌های دیگر نمی‌رسد.** پرچم از تعریف همان منبعی خوانده می‌شود که دستور به آن هدایت شده؛ منبعی که چیزی نگوید `false` است و چیزی از منبع پیش‌فرض ارث نمی‌برد. مثال با دو منبع که فقط اولی hint دارد:

```yaml
default: sales
datasources:
  sales:
    host: 10.0.0.5
    database: SalesDW
    username: nlq_reader
    password_env: DB_PASSWORD_SALES
    nolock: true                    # دستورهای هدایت‌شده به sales بعد از هر جدول WITH (NOLOCK) می‌گیرند
  inventory:
    host: 10.0.0.6
    database: InventoryDW
    username: nlq_reader
    password_env: DB_PASSWORD_INVENTORY
                                    # nolock ندارد: دستورهای هدایت‌شده به اینجا همان‌طور که تأیید شدند می‌روند
```

اگر DBA سرور دوم هم الزام کرده، زیر `inventory` هم `nolock: true` بگذارید. منبعی که دستور روی آن اجرا شده در فیلد `datasource` رکورد audit است. بدون `datasources.yaml` اصلاً نمی‌شود از این hint استفاده کرد، چون تنها منبع ضمنی همیشه `false` خوانده می‌شود: دیتابیس را در یک `datasources.yaml` با یک منبع توصیف کنید (آن‌وقت `default` اختیاری است). فهرست دقیق آنچه hint می‌گیرد و آنچه نمی‌گیرد در بخش ۱۶.۷ از `docs/deployment-runbook.md` است.

۷. **خواندن پنل ادمین** (بخش ۲.۵.۲): کارت‌های «انحراف شِما» و «تازگی واژگان ابعاد» می‌گویند جدولی در منبع اشتباه است یا واژگان یک بُعد کهنه شده.

### ۰.۴ تست پیش‌پرواز

```powershell
python -m scripts.verify_deployment
```

سیزده چک اجرا می‌کند (با چند منبع، چک‌های دیتابیس برای هر منبع جدا): تنظیم‌ها، نگاشت جدول‌ها به منبع‌ها، اتصال به دیتابیس، read-only بودن لاگین،
سقف ردیف و timeout، قرارگرفتن جدول‌ها در منبع درست، وجود مدل روی endpoint، کلید API، قابل نوشتن بودن مسیر audit log و session store، بارگذاری `project_config/`
و سالم بودن rate limit؛ جدول پایین هر کدام را شرح می‌دهد. خط آخر `N passed, N failed, N skipped` است و باید `0 failed` باشد. **قبل از هر استقرار واقعی این را بزنید.**

> **این تست به‌تنهایی ثابت نمی‌کند کلید شما کار می‌کند.** بدون
> `VERIFY_API_KEY` فقط چک می‌کند کلیدهای پیکربندی‌شده (`API_KEYS_FILE` یا
> `API_KEYS_JSON`) پارس می‌شوند و دست‌کم یک کلید دارند — نه اینکه کلیدِ *شما* احراز هویت می‌شود. برای اثبات
> سرتاسری، کلید خام را بدهید:
>
> ```powershell
> $env:VERIFY_API_KEY = "<کلید-خام-۴۳-نویسه>"
> python -m scripts.verify_deployment
> $env:VERIFY_API_KEY = $null   # کلید خام را در شل جا نگذارید
> ```
>
> شکل `VERIFY_API_KEY=... python ...` سینتکس bash است و در PowerShell
> **خطای پارس** می‌دهد (`The term 'VERIFY_API_KEY=...' is not
> recognized`) — که شبیه خرابیِ خود اسکریپت به نظر می‌رسد، نه شبیه
> تفاوت شل. خودِ اسکریپت از ۴.۷.۰ شکل درستِ همان شلی را که در آن اجرا
> شده چاپ می‌کند.

#### هر چک یعنی چه، و با `[FAIL]` چه کنید

هر خط `[PASS]`، `[FAIL]` یا `[SKIP]` است با نام چک و دلیل؛ وضعیت «هشدار» وجود ندارد. `[FAIL]` مشکلی است که پیش از ادامه باید رفع شود و هر `[FAIL]` کد خروج را ۱ می‌کند. `[SKIP]` یعنی چک نتوانست اجرا شود (چیزی برای آزمودن نبود یا عمداً خاموش است) و اجرا را شکست نمی‌دهد. چک‌ها به ترتیب اجرا و با نامِ دقیقِ چاپ‌شده (با چند منبع، چهار چک علامت‌دارِ * برای هر منبع جدا و به شکل `Database connectivity [sales]` چاپ می‌شوند):

| چک | چه می‌کند | معنی `[FAIL]` و راه رفع | معنی `[SKIP]` |
|---|---|---|---|
| `Settings.validate()` | همان اعتبارسنجی‌ای را که سرور هنگام شروع می‌کند اجرا می‌کند: تنظیم‌های الزامی، جانگهدارِ جامانده، خط‌های `.env` که python-dotenv نمی‌تواند بخواند، `SQL_DIALECT` سازگار با اتصال، هر اتصال انبار داده و `LLM_EXTRA_BODY`. | دلیل متن خطاست: `OPENAI_MODEL is not configured`؛ `DB_CONNECTION_URL still has the factory-default placeholder host (username@server)`؛ `SQL_DIALECT=... does not match ...`؛ مشکل `.env` با شمارهٔ خط و نام متغیر؛ منبعی که متغیر `password_env` یا `username_env` آن تنظیم‌نشده یا خالی است (با نام). `.env` را درست کنید (بخش ۰.۲) و دوباره بزنید. | هرگز. |
| `Tables map to data sources` | `datasources.yaml` را می‌خواند و می‌بیند هر `datasource:` در `schema.yaml` نام یک منبع تنظیم‌شده است. `PASS` می‌نویسد `N data source(s): a, b` (بدون `datasources.yaml`: `1 data source(s): default`). | `schema.yaml assigns tables to data sources that are not configured: Order -> elsewhere. Configured sources: [...]`: نامی غلط املایی (حروف باید دقیقاً مثل `datasources.yaml` باشد) یا منبعی که در فایل نیست. یا پیام یک `datasources.yaml` نامعتبر. | هرگز. |
| `Database connectivity`* | با موتور خود برنامه وصل می‌شود و `SELECT 1` می‌زند. `PASS` مقصد را با رمزِ پوشانده نشان می‌دهد. | `could not connect to <target>: <خطای درایور>`. میزبان، پورت، فایروال، نام درایور ODBC، لاگین، رمز، `TrustServerCertificate`. تا این نگذرد به هیچ چک بعدی نمی‌شود اعتماد کرد. | هرگز. |
| `Login is read-only`* | در تراکنشی که همیشه rollback می‌شود `CREATE TABLE` روی یک جدول آزمایشی (`_nlq_agent_deploy_verify_probe`) را امتحان می‌کند و بعد می‌بیند چیزی ماندگار نشده؛ اگر اجرای قبلی چنین جدولی جا گذاشته باشد آن را هم حذف می‌کند. | `... PERSISTED -- the login can write ...`: لاگین فقط‌خواندنی نیست. متوقف شوید و از DBA بخواهید `docs/db-hardening.md` را اعمال کند. یا `could not verify: ...`. اگر `[PASS]` بگوید `CREATE TABLE` خطا نداد ولی rollback نگه داشت، یعنی لاگین *می‌توانست* جدول بسازد و فقط rollback نجاتش داد؛ باز هم از DBA بخواهید سخت‌ترش کند. | اتصال به دیتابیس نیست. |
| `Row cap`* | از طریق executor دستور `SELECT TOP (10 x cap + 10) name FROM sys.all_objects` را اجرا می‌کند و ردیف‌ها را می‌شمارد. | `returned 1500 rows, expected <= 1000`: executor بیشتر از `MAX_ROWS_RETURNED` ردیف برگرداند. تا درست نشود مستقر نکنید؛ تنظیمی برای رفعش نیست، گزارشش دهید. | دیتابیس در دسترس نیست یا پرسش آزمایشی اجرا نمی‌شود (T-SQL است). |
| `Query timeout`* | `WAITFOR DELAY` را با timeout حداکثر ۵ ثانیه اجرا می‌کند و زمان قطع‌شدنش را می‌سنجد. | `took Ns -- longer than the Ms timeout should allow` یا `WAITFOR DELAY completed ... without the timeout firing`: timeout درایور اعمال نمی‌شود یا سرور `WAITFOR` را پشتیبانی نمی‌کند (برخی ردیف‌های serverless در Azure SQL). `QUERY_TIMEOUT_SECONDS` و درایور را ببینید. | دیتابیس در دسترس نیست یا پرسش آزمایشی اجرا نمی‌شود. |
| `Tables are in their data source` | `schema.yaml` را با فهرست جدول‌های هر منبع می‌سنجد و برای جدولی که همهٔ ستون‌هایش در منبع تعیین‌شده نیست ولی منبع دیگری آن را دارد شکست می‌خورد. | `N table(s): <table>: not in <assigned>, found in <other> — set datasource: <other>; ...` (ده نمونه، بعد `and N more`). جدول `datasource:` ندارد (پس روی منبع پیش‌فرض می‌رود) یا غلط دارد. `python scripts/assign_datasources.py` را بزنید و `schema.yaml` را عوض کنید (بخش ۰.۳.۳ گام ۲)؛ این گام ۶ و ۷ فهرست «نصب از صفر» است. یا `could not compare schema.yaml with the data sources: ...` وقتی فهرست جدول‌های یک منبع خوانده نشود. | یک منبع داده (`one data source -- nothing to place`). |
| `Schema structure matches the databases` | همان `python scripts/sync_schema.py --check` در حافظه: از هر منبع سه view کاتالوگ را می‌خواند و اگر همگام‌سازی `schema.yaml` را تغییر می‌داد شکست می‌خورد. | `N column(s) missing from schema.yaml; N column(s) and N table(s) no longer in the database; N column type(s) differ; N datasource: line(s) to set -- run python scripts/sync_schema.py ...`. اسکریپت را بزنید، `schema.synced.yaml` را مرور و `schema.yaml` را جایگزین کنید (بخش ۰.۳.۳ گام ۲). ستونی که قبلاً `# not in database` گرفته شکست نمی‌دهد و در جزئیات `PASS` شمرده می‌شود. | دیتابیسی خوانده نشود (چک اتصال خودش خطا می‌دهد)، `schema.yaml` بار نشود (`project_config/ loads` علت را می‌گوید)، یا چیدمانش خط‌به‌خط قابل‌ویرایش نباشد. |
| `OpenAI-compatible model exists` | از `OPENAI_BASE_URL` مسیر `/models` را می‌خواهد (timeout ۵ ثانیه، `OPENAI_API_KEY` به‌عنوان bearer) و `OPENAI_MODEL` را میان شناسه‌ها می‌جوید. | `could not reach <base>: ...`: نشانی یا پورت غلط، endpoint خاموش، proxy، یا endpoint بدون مسیر `/models`. یا `'<model>' not found among models <base> lists: [...]`: `OPENAI_MODEL` را برابر یکی از شناسه‌های فهرست کنید. | هرگز. |
| `API key authentication` | می‌بیند دست‌کم یک کلید تنظیم شده (`API_KEYS_FILE` یا `API_KEYS_JSON`، به‌علاوهٔ دیتابیس اپلیکیشن) تا سرور بالا بیاید، و با `VERIFY_API_KEY` که همان کلید خام احراز می‌شود. | `API key configuration is invalid: ...` (سرور بالا نمی‌آید)؛ `AUTH_REQUIRED is true but there are no configured keys ...` (کلید صادر کنید، بخش ۲.۲)؛ همهٔ کلیدها در دیتابیس اپلیکیشن revoke یا disable شده‌اند؛ یا `VERIFY_API_KEY was set but did not match any configured key's SHA-256 digest` (کپی ناقص، یا به‌جای کلید خام `key_sha256` را گذاشته‌اید). با `AUTH_REQUIRED=false` می‌گذرد و می‌گوید؛ در تولید استفاده نکنید. | هرگز. |
| `Audit log directory writable` | `LOG_DIR` را در صورت نبودن می‌سازد و یک فایل آزمایشی در آن می‌نویسد و پاک می‌کند؛ قابل نوشتن بودن `audit_log.jsonl` موجود را هم می‌بیند. خودِ `audit_log.jsonl` را هرگز نمی‌نویسد. | `could not create ...` یا `... is not writable`: دسترسی پوشه یا `LOG_DIR` را درست کنید. چون نوشتن ناموفق audit هرگز پرسش کاربر را شکست نمی‌دهد، باید همین‌جا بلند شکست بخورد. | هرگز. |
| `Session store directory writable` | همین آزمون برای پوشهٔ `SESSION_STORE_PATH`. | `could not create ...` یا `... is not writable`. | `SESSION_STORE_PATH` خالی است (ماندگاری عمداً خاموش). |
| `project_config/ loads` | `aliases.yaml`، `entities.yaml`، `business_rules.yaml`، `examples.yaml`، `metrics.yaml` و `schema.yaml` را با مدل‌های کد فعلی می‌خواند. | `<file> not found under '<dir>'` (بخش ۰.۳) یا `<file> failed validation ...` با نام فیلد: فایلی از استقرار قدیمی فیلدی را ندارد که نسخهٔ بعدی لازم کرده. فیلد را درست کنید یا با `project_config.example/` مقایسه کنید. کلید تکراری در YAML رد می‌شود (بخش ۴ گام ۳). | هرگز. |
| `Rate limit sane for deployment` | از `RATE_LIMIT_REQUESTS`، `RATE_LIMIT_WINDOW_SEC` و `RATE_LIMIT_BURST` تعداد درخواست بر ثانیه برای هر تحلیل‌گر را درمی‌آورد، با فرض اینکه `VERIFY_EXPECTED_ANALYSTS` نفر (پیش‌فرض ۱۰) در سطل یک کلید شریک‌اند. | کمتر از ۰٫۱ درخواست بر ثانیه برای هر تحلیل‌گر: `RATE_LIMIT_REQUESTS` را بالا ببرید یا `VERIFY_EXPECTED_ANALYSTS` را برابر تعداد واقعی بگذارید. | هرگز. |

توجه: این چک `project_config/ loads` فقط شش فایل را می‌خواند. `session_policy.yaml`، `memory_policy.yaml`، `retrieval_hints.yaml` و `system_prompt.md` در آن نیستند؛ سرور `system_prompt.md` را هنگام شروع می‌خواند و بقیه را وقتی اولین‌بار لازم شوند. پس مطمئن شوید این فایل‌ها (از کپی قالب، بخش ۰.۳) هستند.

---

## بخش ۱ — CLI

### ۱.۱ اجرا

```powershell
cd <repo-root>
python app.py
```

باید این را ببینید:

```
============================================================
 Auction NLQ Engine
 Model : <مدل شما>
 DB    : <سرور>/<دیتابیس>
============================================================
 Type your question in Persian or English.
 Commands: exit | quit | Ctrl+C
============================================================

❓ Question:
```

### ۱.۲ پرسیدن

سؤال را فارسی یا انگلیسی بنویسید و Enter بزنید:

```
❓ Question: ۱۰ مشتری برتر از نظر مبلغ خرید در سال ۱۴۰۳
```

خروجی به ترتیب:

1. **`GENERATED SQL`** — SQL تولیدشده، بعد از عبور از guard
2. **مسیر فایل اکسل** — خودکار در `exports/` ذخیره می‌شود
3. **زمان اجرا**
4. **`QUERY RESULT`** — ۲۰ سطر اول؛ اگر بیشتر بود می‌گوید چند سطر نشان
   داده نشده و مجموع چند تاست

### ۱.۳ خروج

`exit` یا `quit` یا `Ctrl+C`.

### ۱.۴ چیزهایی که باید بدانید

- **بین دو سؤال ۲ ثانیه فاصله هست.** اگر زودتر Enter بزنید پیام
  `Please wait N s…` می‌گیرید و خودش صبر می‌کند. این عمدی است تا یک
  اشتباهِ نگه‌داشتنِ Enter، endpoint را زیر بار نبرد.
- **CLI کلید API نمی‌خواهد.** اصلاً لایهٔ احراز هویت را import نمی‌کند.
  `AUTH_REQUIRED` فقط روی HTTP اثر دارد.
- **CLI حافظه و سشن ندارد.** هر سؤال مستقل است؛ «از بین آن‌ها…» کار
  نمی‌کند. آن مسیر فقط در وب/`/v2` هست.
- **هر پرسش یک سطر در `logs/query_log.jsonl` می‌نویسد** (رکورد audit مخصوص
  HTTP API است). سطرهای نتیجه هرگز در آن نوشته نمی‌شوند.

### ۱.۵ وقتی خطا می‌گیرید

| پیام | یعنی چه |
|---|---|
| `❌ Configuration error: …` | `.env` ناقص یا نامعتبر است؛ سرور اصلاً شروع نکرده |
| `⚠️ This system only answers … analytics questions` | سؤال خارج از دامنه تشخیص داده شده (`OUT_OF_SCOPE`) |
| `❌ Validation error: …` | guard جلوی SQL را گرفته — جدول/ستون خارج از allowlist، کامنت، DDL/DML |
| `❌ Runtime error: Database error: …` | SQL اجرا شد ولی دیتابیس ردش کرد |

---

## بخش ۲ — وب

### ۲.۱ نکتهٔ اصلی: دو سرور، دو دایرکتوری

`web/` یک کلاینت **استاتیک** است. هیچ پایتونی از این پروژه import نمی‌کند و
هرگز در همان پروسهٔ بک‌اند اجرا نمی‌شود. پس:

| | چه چیزی | از کجا | پورت | در مرورگر باز می‌کنید؟ |
|---|---|---|---|---|
| ترمینال ۱ | بک‌اند FastAPI | **ریشهٔ ریپو** | ۸۰۰۰ | **نه** |
| ترمینال ۲ | فایل‌سرور استاتیک | `web/` | ۸۰۸۰ | **بله ← اینجا** |

> **هر دو لازم‌اند. ترمینال ۲ اختیاری نیست.**
>
> رابط کاربری روی **۸۰۸۰** است، نه ۸۰۰۰. اگر فقط ترمینال ۱ را بالا بیاورید
> و `http://localhost:8000` را باز کنید، هیچ صفحه‌ای نمی‌بینید — آنجا API
> است، نه UI. سروری که درست کار می‌کند هم از آن آدرس چیزی برای نشان دادن
> ندارد.
>
> از نسخهٔ ۴.۱.۱ اگر `http://localhost:8000` را باز کنید یک پاسخ JSON
> می‌گیرید که همین را می‌گوید و شما را به ۸۰۸۰ می‌فرستد. قبل از آن فقط
> `{"detail":"Not Found"}` می‌گرفتید.

اگر بک‌اند را از داخل `web/` اجرا کنید این را می‌گیرید:

```
ModuleNotFoundError: No module named 'api'
```

و `pip install api` هم جواب نمی‌دهد — چنین بسته‌ای روی PyPI وجود ندارد.
`api` که می‌خواهد پکیج خودِ همین ریپوست و فقط از ریشه import می‌شود.

### ۲.۲ صدور کلید API (یک‌بار برای هر تحلیل‌گر)

```powershell
python -m scripts.issue_api_key --id analyst-1 --name "نام تحلیل‌گر"
```

این دستور **دو چیز** چاپ می‌کند و مهم‌ترین نکتهٔ کل این بخش این است که
آن دو **به دو جای متفاوت** می‌روند:

| چه چیزی | طول | کجا می‌رود |
|---|---|---|
| **کلید خام** | ۴۳ نویسه | **مرورگر** — فیلد «کلید API» |
| **ورودی JSON** (شامل `key_sha256`) | هش ۶۴ نویسه هگز | **آرایهٔ کلیدها**: فایل `API_KEYS_FILE` (پیشنهادی) یا `API_KEYS_JSON` در `.env` |

برای یک کلید، یک خط در `.env` کافی است:

```ini
API_KEYS_JSON=[{"id":"analyst-1","name":"...","key_sha256":"<64 هگز>"}]
```

برای چند کلید از فایل استفاده کنید (بخش بعد).

فقط SHA-256 ذخیره می‌شود، نه خود کلید. کلید خام **فقط یک‌بار** چاپ می‌شود
و هیچ‌جا ذخیره نمی‌شود.

> **رایج‌ترین اشتباه: چسباندن هش در مرورگر به‌جای کلید خام.**
>
> این اشتباه طبیعی است و تقصیر شما نیست. ورودی JSON چیزی است که روی صفحه
> می‌ماند و کپی می‌شود؛ کلید خام بالای آن است و زیر خطی که می‌گوید «همین
> حالا کپی کنید، دیگر نشان داده نمی‌شود» رد می‌شود.
>
> ولی نتیجه‌اش **۴۰۱** است: سرور هرچه بفرستید را SHA-256 می‌کند و با
> `key_sha256` مقایسه می‌کند، پس فرستادن هش یعنی سرور **هشِ هش** را حساب
> می‌کند.
>
> **از روی طول تشخیص بدهید:** کلید خام ۴۳ نویسه است، هش ۶۴ نویسهٔ
> `0-9a-f`. اگر چیزی که می‌چسبانید ۶۴ نویسهٔ هگز است، آن هش است.
>
> برای اطمینان — این باید دقیقاً همان `key_sha256` داخل فایل کلید (یا `.env`) را بدهد:
>
> ```powershell
> python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "<کلید-خام>"
> ```
>
> از ۴.۶.۱ پیام ۴۰۱ خودش این را می‌گوید: اگر مقداری که فرستاده‌اید شکل
> SHA-256 داشته باشد، جواب می‌گوید کدام مقدار کجا می‌رود. جهت معکوسش
> (چسباندن کلید خام در `key_sha256`) از قبل موقع استارت‌آپ با خطای بلند
> گرفته می‌شد.

#### چند کلید: فایل `API_KEYS_FILE`

برای چند کلید، آرایه را در یک فایل جدا بگذارید. محتوای فایل دقیقاً همان
چیزی است که در `API_KEYS_JSON` می‌نوشتید، فقط با هر قالب‌بندی و تورفتگی که
دوست دارید. مسیر پیشنهادی `project_config/api_keys.json` است؛ پوشهٔ
`project_config/` در git نادیده گرفته می‌شود، پس هش کلیدها وارد مخزن نمی‌شوند.

قالب آماده در `project_config.example/api_keys.example.json` هست: آن را به
`project_config/api_keys.json` کپی کنید و `key_sha256` هر ورودی را با
هشی که `scripts/issue_api_key.py` چاپ می‌کند عوض کنید. تا مقدار نمونه‌ای
(`<64-hex-char digest ...>`) در فایل مانده، سرور بالا نمی‌آید.

```json
[
  {"id": "admin-1", "name": "ادمین", "key_sha256": "<64 هگز>",
   "admin": true, "operations": true, "security": true},
  {"id": "analyst-1", "name": "تحلیل‌گر", "key_sha256": "<64 هگز>"}
]
```

سپس در `.env` فقط مسیر را بنویسید و `API_KEYS_JSON` را بردارید:

```ini
API_KEYS_FILE=project_config/api_keys.json
```

- مسیر نسبی از **ریشهٔ ریپو** حساب می‌شود، نه از پوشه‌ای که سرور را در آن
  اجرا کرده‌اید (مثل `PROJECT_CONFIG_DIR`).
- فایل **یک‌بار هنگام بالا آمدن** خوانده می‌شود، مثل `.env`. بعد از هر ویرایش
  سرور را ری‌استارت کنید. `verify_deployment` فرایند جدا است و همیشه فایلِ
  فعلی را می‌بیند.
- `API_KEYS_JSON` و `API_KEYS_FILE` را **هم‌زمان** نگذارید؛ سرور بالا نمی‌آید.
- اگر فایل نباشد، JSON نامعتبر باشد (پیام شمارهٔ خط و ستون را می‌دهد)، یا
  یک فیلد داخل یک ورودی دو بار آمده باشد، سرور بالا نمی‌آید.

> **و اگر ترجیح می‌دهید همه‌چیز در `.env` بماند:** تک‌خطی کاملاً درست است.
> چندخطی فقط وقتی کار می‌کند که کل مقدار در **کوتیشن تکی** باشد:
>
> ```ini
> API_KEYS_JSON='[
> {"id":"admin-1","name":"ادمین","key_sha256":"...","admin":true},
> {"id":"analyst-1","name":"تحلیل‌گر","key_sha256":"..."}
> ]'
> ```
>
> بدون کوتیشن کار **نمی‌کند**: `.env` خط‌محور خوانده می‌شود، پس
> `python-dotenv` فقط `[` را به‌عنوان مقدار برمی‌دارد و بقیهٔ خطوط را
> **کلیدهای جدید** می‌گیرد. کوتیشن دوتایی هم JSON را خراب می‌کند، و یک
> آپاستروف داخل مقدار (مثلاً `"name": "Ali's key"`) کل خط را از کار می‌اندازد.
> قبلاً این‌ها فقط یک هشدار یک‌خطی در stderr بودند و سرور با پیام گمراه‌کنندهٔ
> «کلید قابل استفاده‌ای نیست» بالا می‌آمد؛ حالا هر خطِ `.env` که
> `python-dotenv` نتواند بخواند (خط نامعتبر، باقی‌ماندهٔ یک مقدار چندخطیِ
> خراب، یا متغیری که دوبار با مقدار متفاوت آمده) هنگام بالا آمدن رد می‌شود و
> **شمارهٔ خط و نام متغیر** را می‌گوید، هرگز مقدارش را. راه‌حل: کوتیشن تکی
> بگذارید و آپاستروف نداشته باشید، یا کلیدها را به `API_KEYS_FILE` ببرید.

برای محدود کردن ستون‌ها به یک تحلیل‌گر:

```powershell
python -m scripts.issue_api_key --id analyst-2 --name "..." --denied-column NationalID
```

آن ستون در guard رد می‌شود، نه فقط در کش جدا می‌شود.

برای اینکه ستونی فقط در اتصال جدول‌ها (`JOIN ... ON a.col = b.col`) قابل استفاده باشد و مقدارش هرگز نمایش داده نشود یا فیلتر و گروه‌بندی نشود، ورودی را با دامنه بنویسید:

```powershell
python -m scripts.issue_api_key --id analyst-3 --name "..." --denied-column sales.Order.ID
python -m scripts.issue_api_key --id analyst-3 --name "..." --denied-column Warehouse:sales.Order.ID
python -m scripts.issue_api_key --id analyst-3 --name "..." --denied-column Warehouse:ID
```

شکل اول روی آن جدول در هر منبع، شکل دوم فقط وقتی پرس‌وجو روی منبع `Warehouse` اجرا می‌شود، و شکل سوم روی هر جدولِ `Warehouse` که این ستون را دارد اعمال می‌شود. نام منبع، جدول و ستون هنگام راه‌اندازی با `datasources.yaml` و `schema.yaml` سنجیده می‌شود و غلط‌نویسی راه‌اندازی را متوقف می‌کند. محدودیت: «فقط اتصال» مقدار ستون را پنهان می‌کند، نه وجود آن را؛ اگر کلید خارجیِ اشاره‌کننده به آن ستون جای دیگر دیده شود، پنهان کردن کلید اصلی ظاهری است و باید کلید خارجی را هم محدود کرد. ستونِ بدون پیشوند جدول محافظه‌کارانه محدود حساب می‌شود.

> **کلید جدا برای هر تحلیل‌گر بدهید، نه یک کلید مشترک.** دو دلیل عملی:
> رکورد audit `principal_id` را ثبت می‌کند — با یک کلید مشترک، لاگ کل هفته
> همه را یک نفر نشان می‌دهد. و سطل rate limit روی جفتِ (principal, ip) است —
> یک کلید مشترک پشت یک هاست، کل سازمان را در یک سطل جمع می‌کند.

### ۲.۲.۱ چرا این‌همه توکن؟ (و کِی می‌شود هیچ‌کدام را نداشت)

سؤال منصفانه‌ای است. در جریان راه‌اندازی سه جا چیزی شبیه توکن وارد می‌کنید،
ولی **دو راز متفاوت‌اند، نه یکی سه بار**:

| کجا | چه چیزی | بین چه کسی و چه کسی |
|---|---|---|
| `OPENAI_API_KEY` در `.env` | اعتبارنامهٔ **مدل زبانی** | سرورِ شما ← endpoint مدل |
| `API_KEYS_FILE` یا `API_KEYS_JSON` | فقط **هشِ** کلید تحلیل‌گر | چیزی که سرور ذخیره می‌کند |
| فیلد کلید در UI | **خودِ** کلید تحلیل‌گر | مرورگر ← سرورِ شما |

ردیف اول کاملاً بی‌ربط به دو تای دیگر است — رمز عبور شما به مدل زبانی است،
نه به این سیستم. **با مدل محلی معمولاً خالی می‌ماند**، و آن‌وقت تنها توکن
واقعیِ کل سیستم کلید تحلیل‌گر است.

> **هرگز این دو را یک مقدار نگذارید.** دو جهت مخالف‌اند: `OPENAI_API_KEY`
> چیزی است که سرورِ شما **به بیرون** ارائه می‌دهد (به مدل زبانی)، و کلید
> تحلیل‌گر چیزی است که مرورگر **به داخل** ارائه می‌دهد (به سرورِ شما).
> یکی کردنشان یعنی هرکس به پیکربندی یا لاگ سرور مدل زبانی دسترسی دارد —
> و خیلی از آن سرورها هدر `Authorization` را لاگ می‌کنند — می‌تواند خودش
> را جای یک تحلیل‌گر جا بزند.

ردیف دوم و سوم **یک** راز هستند، ولی دوبار وارد کردن نیست: سرور *هش* را
نگه می‌دارد و مرورگر *خودِ* کلید را. این تکرار نیست، همان الگویی است که هر
سیستم رمز عبوری دارد. اگر سرور کلید خام را ذخیره می‌کرد، هرکس `.env` را
می‌خواند می‌توانست خودش را جای هر تحلیل‌گری جا بزند — و رکورد audit که
`principal_id` ثبت می‌کند بی‌ارزش می‌شد.

**ولی برای اجرای تک‌کاربره روی لپ‌تاپ خودتان، هیچ‌کدام لازم نیست:**

```ini
AUTH_REQUIRED=false
```

آن‌وقت `API_KEYS_FILE` / `API_KEYS_JSON` را نمی‌خواهید و UI هم کلید نمی‌پرسد. سرور در هر
استارت یک هشدار می‌دهد که در تولید نباید این کار را بکنید — که درست است:
بدون کلید، لاگ audit نمی‌تواند بگوید چه کسی چه پرسیده، و سطل rate limit
همه را یکی می‌بیند. برای آزمایش محلی مناسب است، برای شنبه نه.

### ۲.۳ ترمینال ۱ — بک‌اند

```powershell
cd <repo-root>
.\.venv\Scripts\Activate.ps1
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

> اگر `API_HOST` و `API_PORT` را در `.env` تنظیم کرده باشید (پیش‌فرض
> `127.0.0.1` و `8000`)، به‌جای فرمان بالا می‌توانید این را هم اجرا کنید —
> همان برنامه، همان `--no-server-header`، ولی هاست/پورت را از `.env`
> می‌خواند نه از خط فرمان:
>
> ```powershell
> python -m api
> ```

سلامتش را چک کنید — **با curl، نه با مرورگر**:

```powershell
curl http://localhost:8000/health
```

باید چیزی شبیه این برگردد:

```json
{"status":"ok","openai":true,"database":true}
```

`status: "degraded"` یعنی یکی از LLM و دیتابیس در دسترس نیست و `"down"` یعنی هیچ‌کدام؛ بک‌اند خودش بالاست — به `.env` نگاه کنید. با چند منبع داده، `database` فقط وقتی `true` است که **همهٔ** منبع‌ها جواب داده باشند و `database_detail` نام هر منبع را می‌آورد.

`/health` و `/` تنها مسیرهایی هستند که کلید نمی‌خواهند.

### ۲.۳.۱ اگر UI را از جای دیگری سرو می‌کنید: CORS

صفحه روی ۸۰۸۰ است و به ۸۰۰۰ درخواست می‌دهد — یعنی cross-origin. بک‌اند
پیش‌فرض `http://localhost:8080` و `http://127.0.0.1:8080` را مجاز می‌داند،
پس حالت محلی بدون کار اضافه کار می‌کند.

اگر UI را از هاست یا پورت دیگری سرو می‌کنید، در `.env` بگذارید و سرور را
ری‌استارت کنید:

```ini
CORS_ALLOWED_ORIGINS=http://your-ui-host:8080
```

> **این خطا شبیه «بک‌اند خراب است» به نظر می‌رسد، نه شبیه یک اشتباه
> پیکربندی.** مرورگر دلیل واقعی رد شدن یک درخواست cross-origin را به صفحه
> نمی‌گوید — همان `Failed to fetch` را می‌دهد که وقتی هیچ‌کس پشت آن پورت
> نیست هم می‌دهد. علامتش این است: **هر سه چراغ API و LLM و DB قرمزند، ولی
> CLI با همان سرور درست کار می‌کند.** CLI مرورگر نیست و اصلاً `Origin`
> نمی‌فرستد. خودِ صفحهٔ وب هم وقتی بررسی سلامت با
> خطای شبکه‌ای مواجه شود، هر دو احتمال (CORS یا اصلاً چیزی بالا نیست) را
> کنار هم و همراه origin دقیق همین صفحه نشان می‌دهد — نیازی به این حدس‌زدن
> دستی نیست.
>
> برای تأیید قطعی، لاگ استارتاپ سرور را نگاه کنید — همان‌جا که پیام
> provenance می‌آید، این خط هم هست:
>
> ```
> CORS allowed origins: http://localhost:8080, http://127.0.0.1:8080
> ```
>
> اگر origin واقعیِ UI شما (پروتکل + هاست + پورت) در این فهرست نیست، همان
> `CORS_ALLOWED_ORIGINS` بالا را تنظیم کنید و سرور را ری‌استارت کنید.

### ۲.۴ ترمینال ۲ — فایل‌های استاتیک

```powershell
cd <repo-root>\web
python -m http.server 8080
```

بعد باز کنید: `http://localhost:8080`

`python -m http.server` فقط فایل به مرورگر می‌دهد؛ هیچ نمی‌داند `/query`
چیست. اتصال به بک‌اند کار مرورگر است، در گام بعد.

### ۲.۵ حالت زنده — از ۴.۴.۰ به بعد پیش‌فرض است

> **اگر راهنمای قدیمی را دنبال می‌کنید، اینجا گیر می‌کنید.** تا ۴.۳.۰ باید
> دکمهٔ «زندهٔ API» را می‌زدید و بعد نشانی بک‌اند را در یک فیلد بالای صفحه
> وارد می‌کردید و «اتصال» را می‌زدید. **آن فیلد دیگر وجود ندارد** و دنبالش
> گشتن بی‌فایده است.

از ۴.۴.۰ صفحه مستقیماً در حالت **زنده** بالا می‌آید و نشانی بک‌اند دیگر
چیزی نیست که تحلیل‌گر ببیند یا دستکاری کند. دلیلش این بود که یک مقدار غلط
در آن فیلد **دقیقاً شبیه بک‌اند مرده** به نظر می‌رسید و از داخل صفحه هیچ
راهی برای تشخیص این دو از هم نبود.

نشانی بک‌اند حالا در یک فایل است — `web/js/config.js`، تنها فایلی که یک
استقرار ویرایش می‌کند، و پنل ادمین (`web/admin/`) هم دقیقاً از همین دو
مقدار و همین ترتیب اولویت (`?base=` > مقدار ذخیره‌شده > این فایل) استفاده
می‌کند — دیگر لازم نیست نشانی را جداگانه در بالای پنل ادمین تایپ کنید:

```js
export const DEFAULT_BASE_URL = "http://localhost:8000";
export const DEFAULT_API_PORT = 8000;
```

اگر بک‌اند شما روی همان `localhost:8000` است، **هیچ کاری لازم نیست**.
اگر جای دیگری است، همین یک خط را عوض کنید — یا `DEFAULT_BASE_URL` را خالی
(`""`) بگذارید تا نشانی بک‌اند خودکار از پروتکل و هاستِ همین صفحه به‌علاوهٔ
`DEFAULT_API_PORT` ساخته شود؛ این یعنی جابه‌جا کردن هاست دیگر نیازی به
ویرایش JS ندارد (تا وقتی بک‌اند روی همان هاستِ UI، فقط پورت دیگری، در
دسترس باشد).

هر نشانی‌ای که وارد می‌شود — این فایل، `?base=`، یا مقدار ذخیره‌شده — پاک‌سازی
و اعتبارسنجی می‌شود: بدون `http://`/`https://` به‌صورت خودکار `http://`
می‌گیرد، مسیر یا `/` انتهایی حذف می‌شود، و مقداری که اصلاً قابل‌فهم نباشد
(مثلاً بدون طرحواره‌ای معتبر) رد می‌شود و پیام روشنی نشان داده می‌شود —
نه اینکه بی‌صدا به‌عنوان یک مسیر نسبی به سرور فایل استاتیک فرستاده شود.

پس تنها کاری که می‌ماند کلید است:

1. صفحه را باز کنید — از قبل زنده است
2. کلید خام گام ۲.۲ را در فیلد **کلید API** بگذارید و **ذخیره کلید**

حالت نمایشی حذف نشده، فقط دیگر پیش‌فرض نیست:

```
http://localhost:8080/?live=0
```

و برای اشکال‌زدایی، `?base=` هنوز کار می‌کند و بر `config.js` اولویت دارد:

```
http://localhost:8080/?base=http://192.168.1.50:8000
```

کلید در `localStorage` همان مرورگر می‌ماند، هرگز در URL نمی‌رود، در لاگ
نمی‌آید و در پیام خطا برنمی‌گردد.

> **یک پیامد صادقانه:** چون حالت زنده پیش‌فرض است، اولین بارگذاری روی
> بک‌اندی که بالا نیست، حالا **خطا** نشان می‌دهد نه یک دموی سالم. این
> عمدی است — قبلاً همان پیام خطا وجود داشت ولی هرگز شلیک نمی‌شد.

### ۲.۵.۱ پنل ادمین — دسترسی و نقش‌ها

پنل روی همان سرور استاتیک ترمینال ۲ سرو می‌شود، در زیرشاخهٔ `admin`:

```
http://localhost:8080/admin/
```

نشانی بک‌اند را از همان `web/js/config.js` می‌خواند، پس اگر UI تحلیل‌گر کار
می‌کند پنل هم کار می‌کند.

**ولی کلید تحلیل‌گر شما آن را باز نمی‌کند** — و این عمدی است. یک کلید
معمولی هیچ سطح ادمینی ندارد.

#### سه نقش، نه یکی

| نقش | چه می‌بیند / چه می‌کند |
|---|---|
| `admin` | فقط خواندن: داشبورد، سلامت، خلاصهٔ لاگ ممیزی. هیچ چیزی را عوض نمی‌کند |
| `operations` | چرخهٔ عمر کلید، دانش دامنه، هشت فایل از نُه فایل کانفیگ، صف triage |
| `security` | `denied_columns`، `schema.yaml`، رشتهٔ اتصال انبار، و اعطای هر دو نقش |

قاعده‌ای که هر مورد آینده را تعیین می‌کند: **هر چیزی که عوض می‌کند چه کسی
چه داده‌ای را می‌بیند، مال ادمین امنیت است.** بقیه مال عملیات.

#### اولین ادمین از `.env` می‌آید، نه از وب

این یک تصمیم است نه یک محدودیت: برای اعطای نقش از طریق API باید از قبل نقش
`security` را داشته باشید. پس اولین‌بار باید از محیط بیاید.

از ۴.۷.۰ هر سه نقش فلگ دارند — `--admin`، `--operations`، `--security` —
و `--full-admin` هر سه را با هم می‌دهد. برای راه‌اندازی تک‌اپراتوری:

```powershell
python -m scripts.issue_api_key --id admin-1 --name "ادمین" --full-admin
```

و وقتی تفکیک نقش‌ها را واقعاً استفاده می‌کنید:

```powershell
python -m scripts.issue_api_key --id ops-1 --name "عملیات" --admin --operations
python -m scripts.issue_api_key --id sec-1 --name "امنیت" --admin --security
```

> **`--admin` به‌تنهایی کافی نیست، و این رایج‌ترین سردرگمی این بخش است.**
>
> دوازده بخش پنل روی چند سطح دسترسی پخش شده‌اند:
>
> | چه چیزی لازم است | کدام بخش‌های پنل |
> |---|---|
> | `admin` | ممیزی، بررسی‌های استقرار، کش پرس‌وجو، پیکربندی دامنه |
> | `operations` **یا** `security` | تعمیر و نگهداری، بازخورد، انحراف شِما، واژگان ابعاد، مصرف هر تحلیل‌گر، تلاش‌های ناموفق احراز هویت |
> | `operations` | کلیدها و دسترسی‌ها |
> | `security` | درخواست‌های دسترسی |
>
> پس کلیدی که فقط `--admin` دارد پنلی را باز می‌کند که **۸ بخش از ۱۲
> بخشش ۴۰۳ می‌دهد** — که شبیه استقرار خراب به نظر می‌رسد، نه شبیه
> تصمیمی که کسی موقع صدور کلید گرفته. خودِ اسکریپت حالا این ترکیب را
> هشدار می‌دهد، پس لازم نیست تا اولین لاگین صبر کنید.

اگر ترجیح می‌دهید دستی بنویسید، شکلش این است:

```ini
API_KEYS_JSON=[{"id":"admin-1","name":"ادمین","key_sha256":"<64 هگز>","admin":true,"operations":true,"security":true}]
```

هر سه باید **بولین JSON** باشند. اگر `"security":"false"` بنویسید (رشته،
نه بولین) سرور بالا نمی‌آید و می‌گوید چرا — عمداً، چون رشتهٔ ناتهی در
پایتون truthy است و آن تایپو در سکوت نقش امنیت را می‌داد.

> **اگر قبلاً یک‌بار سرور را با همین `--id` بالا آورده‌اید،** کلید قدیمی در
> دیتابیس اپ ایمپورت شده و کلید تازه با همان id ولی هشِ متفاوت باعث
> `AmbiguousKeyIdentityError` می‌شود و سرور بالا نمی‌آید. یا `--id` تازه
> بدهید، یا کلید متعارض را ابطال کنید.

سرور را ری‌استارت کنید، بعد در `http://localhost:8080/admin/` **کلید خام**
را بگذارید — نه `key_sha256` را. همان تله‌ای که در بخش ۲.۲ توضیح داده شد،
اینجا هم هست و اینجا حتی محتمل‌تر است، چون ورودی JSON ادمین را تازه ویرایش
کرده‌اید و روی صفحه جلوی چشمتان است.

اگر پنل `۴۰۱` داد، هش زده‌اید. اگر **همهٔ** بخش‌ها `۴۰۳` دادند، کلید
درست است ولی هیچ نقشی ندارد. اگر **بعضی** بخش‌ها آمدند و بعضی `۴۰۳` دادند،
کلید نقش دارد ولی ناقص — جدول بالا می‌گوید کدام نقش کم است.

> **در استقرار واقعی این سه را روی یک کلید جمع نکنید.** دلیل جدا بودنشان
> این است که کسی که مترادف‌ها را ویرایش می‌کند نتواند `denied_columns` را
> هم عوض کند. یک کلید که هر سه را دارد، آن تفکیک را برای کسی که بعداً لاگ
> را می‌خواند نامرئی می‌کند. برای راه‌اندازی اولیه روی ماشین خودتان اشکالی
> ندارد.

#### بعد از اولین ادمین

از آن به بعد لازم نیست به `.env` دست بزنید. از ۴.۸.۰ بخش **«کلیدها و
دسترسی‌ها»** در پنل، همان کارها را بدون ترمینال انجام می‌دهد:

| کار | چه نقشی لازم است |
|---|---|
| صدور کلید تازه | `operations` |
| غیرفعال / فعال کردن کلید | `operations` |
| ابطال دائمی کلید | `operations` |
| تغییر ستون‌های ممنوع یک کلید | `security` |
| اعطا یا سلب نقش | `security` |

کلیدها از ۴.۵.۰ در دیتابیس‌اند — یعنی **ابطال یک کلید فوری است**، نه بعد
از ری‌استارت بعدی. برای کلیدی که نشت کرده، «فردا صبح» جواب نیست.

> **کلیدی که از پنل صادر می‌شود با «همهٔ ستون‌ها ممنوع» شروع می‌کند.**
> احراز هویت می‌شود ولی هیچ پرس‌وجویی برایش کار نمی‌کند تا وقتی یک ادمین
> امنیت با دکمهٔ «ستون‌ها» محدودیتش را باز کند. این عمدی است — صدور کلید
> کار `operations` است و تعیین اینکه چه کسی چه داده‌ای می‌بیند کار
> `security` — ولی اگر ندانید، کلیدی تحویل داده‌اید که خراب به نظر
> می‌رسد. خودِ پنل موقع صدور این را می‌گوید.

> **کلید خام فقط همان یک‌بار، در همان صفحه، نشان داده می‌شود.** نه ذخیره
> می‌شود و نه دوباره قابل دیدن است — دقیقاً مثل CLI.

> **«غیرفعال کردن» برگشت‌پذیر است؛ «ابطال» نیست.** ابطال ردیف را بایگانی
> می‌کند و هرگز حذفش نمی‌کند، تا بازگرداندن دیتابیس به دیروز کلیدی را که
> نشت کرده دوباره زنده نکند. برای همین دکمه‌اش می‌خواهد شناسهٔ کلید را
> تایپ کنید، نه فقط تأیید بزنید.

> **نقشی که از `API_KEYS_FILE` (یا `API_KEYS_JSON`) آمده از پنل قابل سلب نیست.** قابلیت‌های
> محیط در هر بار بارگذاری دوباره اضافه می‌شوند، پس حذف ردیف دیتابیس
> کاری نمی‌کند. تا ۴.۷.۰ این حالت «موفق» گزارش می‌شد و هیچ تغییری
> نمی‌داد؛ حالا ۴۰۹ می‌گیرید و پیام می‌گوید باید فلگ را از همان‌جا (فایل کلید یا `.env`) بردارید
> و سرور را ری‌استارت کنید.

### ۲.۵.۲ خواندن کارت‌های «انحراف شِما» و «تازگی واژگان ابعاد»

این دو کارت فقط‌خواندنی‌اند و خودشان چیزی را اعمال نمی‌کنند؛ هر دو `operations` یا `security` می‌خواهند (دکمهٔ «بازخوانی» واژگان فقط `operations`). کارت انحراف شِما چون کاتالوگ دیتابیس را می‌خواند فقط هنگام باز شدن پنل و با دکمهٔ بروزرسانی خودش بارگذاری می‌شود (نتیجه به‌اندازهٔ `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` کش می‌شود)، نه با زمان‌سنج ۳۰ ثانیه‌ای.

**انحراف شِما.** `schema.yaml` را با کاتالوگ زندهٔ هر منبع مقایسه می‌کند و به این ترتیب نشان می‌دهد:

- *جدول در منبع دادهٔ دیگری است*: جدولی که همهٔ ستون‌هایش در منبعی که برایش تعیین شده نیست ولی منبع دیگری آن را دارد؛ با منبعی که در آن نیست، منبعی که در آن پیدا شد و مقداری که باید بنویسید (`datasource: inventory` یا `datasource: [sales, inventory]`).
- *فقط در انبار داده*: جدول یا ستونی که انبار دارد و `schema.yaml` ندارد؛ فعلاً قابل پرس‌وجو نیست، چون نگهبان هر چه بیرون فهرست مجاز باشد را رد می‌کند.
- *فقط در schema.yaml*: جدول یا ستونی که `schema.yaml` دارد و انبار دیگر ندارد؛ کوئری‌ای که از آن استفاده کند هنگام اجرا شکست می‌خورد.
- *نوع ستون تغییر کرده*: نوع ستون از آخرین اجرای این بررسی تغییر کرده است؛ اجرای اول مبنایی ندارد و همین را می‌گوید.

جدولی که در چند منبع هست روی هر کدام جدا مقایسه می‌شود و ستونِ جاافتاده از یک نسخه به شکل `Table.Column [source]` می‌آید. رفع یافته‌ها ویرایش `schema.yaml` است: کلید `operations` پیش‌نویس می‌دهد، کلید `security` تأیید می‌کند و نگهبان در راه‌اندازی بعدی آن را برمی‌دارد.

**تازگی واژگان ابعاد.** برای هر ستون پیش‌خوان‌شده (`prefetchable_columns` در `schema.yaml`) یک ردیف دارد: `table.column`، وضعیت، تعداد مقدارها، زمان آخرین بروزرسانی و دکمهٔ «بازخوانی».

- *تازه*: در `DIMENSION_VOCABULARY_TTL_SECONDS` (پیش‌فرض ۳۶۰۰ ثانیه) گرفته شده.
- *کهنه*: قدیمی‌تر از آن است؛ همچنان استفاده می‌شود و پرسش بعدی که به آن نیاز دارد یک بازخوانی پس‌زمینه راه می‌اندازد.
- *هرگز*: هیچ‌وقت گرفته نشده. پرسشی که این بُعد را نام ببرد نمی‌تواند با مقدارهایش سنجیده شود، پس پاسخ با آن مقدار فیلتر نمی‌شود و تحلیل‌گر هشداری می‌گیرد، تا وقتی که بازخوانی موفق شود. همان پرسش یک بازخوانی پس‌زمینه هم راه می‌اندازد (بعد از یک شکست، حداکثر یک تلاش خودکار در هر دقیقه)، پس خطای گذرا خودش برطرف می‌شود؛ «بازخوانی» همان لحظه امتحان می‌کند. با `DIMENSION_VOCABULARY_WARM_ON_STARTUP=true` (پیش‌فرض) این وضعیت فقط بعد از یک گرم‌کردن ناموفق دیده می‌شود، که در لاگ می‌آید و جلوی بالا آمدن سرور را نمی‌گیرد.
- *آخرین تلاش ناموفق*: آخرین تلاش، خودکار یا دستی، شکست خورده؛ «بازخوانی» را بزنید و پیام را بخوانید، که خطای خودِ دیتابیسِ منبعِ آن جدول است.

با چند منبع، ستون از یک نسخهٔ جدولش خوانده می‌شود: منبع پیش‌فرض اگر جدول آن‌جا باشد، وگرنه نخستین منبع در `datasources.yaml`. تالار یا کالایی که به انبار اضافه شده و هرگز بازخوانی نشده باشد، تشخیص مقدار را بی‌صدا از دست می‌دهد؛ کارت برای همین هست.

### ۲.۶ چه چیزی در وب دارید که در CLI نیست

- **گفتگوی چندنوبتی.** «از بین آن‌ها کدام بیشترین تعداد سفارش را داشت؟»
  روی نوبت قبلی به‌صورت CTE ساخته می‌شود، نه اینکه دوباره کل انبار را
  بخواند.
- **مفروضه‌های اعلام‌شده.** هر فرضی که سیستم گذاشته (کدام سنجه، کدام دوره،
  کدام دامنه) به‌صورت چیپ نشان داده می‌شود و قابل ویرایش است.
- **فهرست گفتگوها.** سایدبار سمت راست: گفتگوی جدید، جابه‌جایی، تغییر نام،
  حذف. بعد از reload همان‌جایی برمی‌گردید که بودید.
- **حافظه.** روی هر چیپ قابل ویرایش یک 📌 هست: بزنید تا آن ترجیح در
  گفتگوهای بعدی هم اعمال شود. **فقط با پین کردن ساخته می‌شود** — هیچ‌وقت
  از روی تکرار حدس زده نمی‌شود.
- **نمودار یا جدول.** شکل خروجی از روی نوع ستون‌های نتیجه انتخاب می‌شود،
  و جدول همیشه یک کلیک فاصله دارد.

### ۲.۷ گفتگوی قدیمی که باز می‌کنید عدد ندارد

عمدی است. **سطرهای نتیجه هرگز روی دیسک نوشته نمی‌شوند** — پرسش، SQL و نام
ستون‌ها ذخیره می‌شوند، خود سطرها نه.

دلیلش این است که یک سطرِ ذخیره‌شده را نمی‌شود دوباره با ACL تغییریافته چک
کرد: `denied_columns` یک کاربر ممکن است *بعد از* نوشته شدن آن سطر ستونی
اضافه کند، و هیچ کار guard در زمان کوئری این را نمی‌گیرد چون هیچ کوئری‌ای
اجرا نمی‌شود.

پس نوبت بازیابی‌شده پرسش، SQL و تعداد سطر واقعی را نشان می‌دهد با دکمهٔ
**دوباره اجرا کن** به‌جای عددها. اگر نمی‌خواهید این فایل اصلاً وجود داشته
باشد: `SESSION_STORE_PATH=""`.

### ۲.۸ وقتی خطا می‌گیرید

| چه می‌بینید | یعنی چه |
|---|---|
| `ModuleNotFoundError: No module named 'api'` | بک‌اند را از `web/` اجرا کرده‌اید. از ریشهٔ ریپو اجرا کنید |
| `localhost:8000` در مرورگر چیزی نشان نمی‌دهد | درست است — UI روی ۸۰۸۰ است. ترمینال ۲ را بالا بیاورید |
| هر سه چراغ قرمزند ولی CLI کار می‌کند | تقریباً همیشه CORS. بخش ۲.۳.۱ |
| فقط چراغ LLM قرمز است ولی CLI جواب می‌دهد | روی چراغ نگه دارید — tooltip دلیلش را می‌گوید. اگر «does not implement /models» بود، از ۴.۱.۲ به بعد دیگر قرمز نمی‌شود |
| `Failed to fetch` یا `TRANSPORT_ERROR` | یا بک‌اند بالا نیست، یا origin شما در `CORS_ALLOWED_ORIGINS` مجاز نشده |
| UI می‌گوید بک‌اند در دسترس نیست | ترمینال ۱ بالا نیست، یا پورت فرق دارد |
| کلید رد شد (۴۰۱) و چیزی که زدید ۶۴ نویسهٔ هگز است | **هش را زده‌اید نه کلید خام.** کلید خام ۴۳ نویسه است — بخش ۲.۲ |
| کلید رد شد (۴۰۱) | کلید غلط یا هشِ آن در `API_KEYS_FILE` (یا `API_KEYS_JSON`) نیست. بعد از تغییر فایل کلید یا `.env` بک‌اند را ری‌استارت کنید. از ۴.۱۰.۰ کلید ذخیره‌شده **پاک نمی‌شود** — اگر سرور تازه ری‌استارت شده، فقط دوباره بپرسید |
| `LLM_OUTPUT_TRUNCATED` | مدل قبل از تولید SQL به سقف توکن خورد — بخش ۲.۹ |
| مرحلهٔ ۵ («تفسیر») تیک نمی‌خورد | تیک «تفسیر نتیجه» زیر کادر پرسش خاموش است — بخش ۲.۸.۱ |
| «جوابی نگرفتم» / `EMPTY_SQL_RESPONSE` روی مدل ریزنینگ | تقریباً همیشه همان سقف توکن است. اگر نسخه‌تان قدیمی‌تر از ۴.۱۰.۰ است این کد را می‌بینید نه کد بالا را — بخش ۲.۹ |
| سرور با `Invalid API key configuration` بالا نمی‌آید | فایل `API_KEYS_FILE` نیست یا JSON آن خراب است (پیام خط و ستون را می‌دهد)، یا هر دوی `API_KEYS_FILE` و `API_KEYS_JSON` را گذاشته‌اید — بخش ۲.۲ |
| سرور با `Invalid configuration` و فهرستی از «line N» بالا نمی‌آید | `python-dotenv` آن خط‌های `.env` را نتوانسته بخواند یا متغیری دوبار با مقدار متفاوت آمده. اگر `API_KEYS_JSON` را چندخطی نوشته‌اید، کل مقدار باید در کوتیشن تکی و بدون آپاستروف باشد، یا از `API_KEYS_FILE` استفاده کنید — بخش ۲.۲ |
| ۴۲۹ با عدد و زمان | rate limit؛ خودِ پیام سهمیه، پنجره و زمان انتظار را می‌گوید |
| «این بک‌اند از v2 پشتیبانی نمی‌کند» | بک‌اند قدیمی است؛ به نسخهٔ فعلی به‌روزش کنید |
| دنبال فیلد «نشانی بک‌اند» می‌گردم و نیست | از ۴.۴.۰ حذف شده. `web/js/config.js` را ویرایش کنید — بخش ۲.۵ |
| صفحه به‌جای دمو خطا می‌دهد | درست است: از ۴.۴.۰ حالت زنده پیش‌فرض است، پس بک‌اندِ بالا نیامده حالا واقعاً خطا می‌دهد. `?live=0` دموی قبلی را می‌دهد |
| `Application database unreachable ... (APP_DB_URL resolved to ...)` | از ۴.۵.۰. اگر `APP_DB_URL` را ست کرده‌اید، دیتابیس باید از قبل وجود داشته باشد. خالی بگذارید تا خودش SQLite بسازد — بخش ۰.۲ |
| سرور می‌گوید دیتابیس اپلیکیشن و انبار یکی‌اند | همان‌طور که می‌گوید: `APP_DB_URL` و `DB_CONNECTION_URL` (یا، اگر `datasources.yaml` دارید، یکی از منابع آن) به یک جا اشاره می‌کنند. عمداً بالا نمی‌آید |
| `/admin/` می‌گوید ۴۰۳ | کلیدتان نقش ندارد. با `--full-admin` صادر کنید و ری‌استارت — بخش ۲.۵.۱ |
| بعضی بخش‌های `/admin/` می‌آیند و بعضی ۴۰۳ | کلید فقط `admin` دارد. `operations`/`security` هم لازم است — جدول بخش ۲.۵.۱ |
| بخش «کلیدها و دسترسی‌ها» ۴۰۳ می‌دهد | این بخش `operations` می‌خواهد؛ خودِ کارت می‌گوید کدام فلگ را کم دارید |
| کلید تازه‌ای که از پنل دادید هیچ پرس‌وجویی نمی‌تواند بکند | با «همهٔ ستون‌ها ممنوع» صادر شده. با دکمهٔ «ستون‌ها» بازش کنید — بخش ۲.۵.۱ |

### ۲.۸.۱ تفسیر نتیجه — کلید کنار کادر پرسش

زیر کادر پرسش یک تیک هست: **«تفسیر نتیجه»**. روشنش کنید تا هر پاسخ یک
خلاصهٔ ساده‌شده به زبان خودتان هم داشته باشد.

**پیش‌فرض خاموش است، و این عمدی است.** ساختن آن خلاصه یعنی **حداکثر ۲۰
ردیف از نتیجهٔ واقعی به مدل زبانی فرستاده می‌شود**. با مدل محلی این دقیقاً
همان چیزی است که این محصول وعده می‌دهد؛ ولی تصمیمش مال کسی است که سؤال را
پرسیده، نه مال کل استقرار — برای همین یک تیک per-analyst است نه یک تنظیم
در `.env`. انتخابتان در همان مرورگر ذخیره می‌شود.

اگر مدل شما ریموت باشد، گیت حاکمیت داده جلویش را می‌گیرد و بدون
`LLM_ALLOW_REMOTE=true` هیچ ردیفی نمی‌فرستد — و در لاگ با سطح `ERROR`
می‌گوید چند ردیف را نفرستاده و چرا.

> **تا ۴.۱۲.۰ این قابلیت روی مسیر گفتگویی اصلاً وجود نداشت.** مرحلهٔ پنجم
> «تفسیر» در فهرست مراحل همیشه «در انتظار» می‌ماند، چون موتور هیچ‌وقت
> تفسیری تولید نمی‌کرد. تا وقتی هیچ‌کدام از پنج مرحله تیک نمی‌خوردند این
> دیده نمی‌شد؛ از ۴.۱۱.۰ که چهار مرحلهٔ دیگر شروع به تیک خوردن کردند،
> پنجمی تابلو شد.

---

### ۲.۹ مدل ریزنینگ و «جوابی نگرفتم»

اگر UI می‌گوید جوابی نگرفت و مدل شما ریزنینگ دارد — Qwen3، DeepSeek-R1،
gpt-oss و مانند این‌ها — تقریباً همیشه **سقف توکن خروجی** است، نه مدل، نه
پرامپت، نه شِما.

این مدل‌ها **اول فکر می‌کنند، بعد جواب می‌دهند.** سقف پیش‌فرض
`LLM_NUM_PREDICT` برابر ۵۱۲ توکن است. برای یک SQL که چند ده توکن است
سخاوتمندانه است؛ برای مدلی که اول چند صد توکن استدلال می‌نویسد نیست. مدل
کل بودجه را صرف فکر کردن می‌کند و **قبل از نوشتن اولین حرف SQL بریده
می‌شود**.

#### چطور مطمئن شوید همین است

در رکورد audit (`logs/audit_log.jsonl`) دنبال این چهارتا **با هم** بگردید:

| فیلد | مقدار |
|---|---|
| `finish_reason` | `length` |
| `completion_tokens` | دقیقاً برابر `LLM_NUM_PREDICT` |
| `reasoning_detected` | `true` |
| `generated_sql` | `""` |

هر چهارتا با هم یعنی همین. از ۴.۱۰.۰ خودِ سامانه این را تشخیص می‌دهد و
به‌جای `EMPTY_SQL_RESPONSE` کد `LLM_OUTPUT_TRUNCATED` را با پیامی که راه
حل را می‌گوید برمی‌گرداند — و دیگر سه بار بی‌فایده تلاش نمی‌کند (قبلاً
همین تکرار، هر پرسش را حدود ۱۷ ثانیه طول می‌داد تا شکستی که از اول قطعی
بود).

#### دو راه حل

**راه اول — ریزنینگ را خاموش کنید (بهتر).** سریع‌تر و ارزان‌تر است، چون
توکن‌های استدلال در هر حال هزینه و زمان دارند. در `.env`:

```ini
LLM_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}
```

این شکل برای **Qwen3 روی vLLM یا SGLang** است. هر سرور املای خودش را
دارد:

| سرور | مقدار |
|---|---|
| vLLM / SGLang (Qwen3) | `{"chat_template_kwargs":{"enable_thinking":false}}` |
| Ollama | `{"think":false}` |
| OpenAI | `{"reasoning_effort":"low"}` |

هرچه اینجا بگذارید **عیناً** به سرور شما فرستاده می‌شود؛ سامانه معنی‌اش
را نمی‌داند و تغییرش نمی‌دهد.

> **حتماً تأیید کنید که اثر کرده.** سرورها معمولاً فیلدی را که نمی‌شناسند
> **بی‌صدا نادیده می‌گیرند**. بعد از ری‌استارت یک پرسش بزنید و در رکورد
> audit ببینید `reasoning_detected` شده `false` و `completion_tokens`
> آمده پایین. اگر همچنان `512` و `length` است، سرور شما آن فیلد را
> نمی‌شناسد — برو راه دوم.

**راه دوم — سقف را بالا ببرید.** همیشه کار می‌کند، ولی هزینهٔ توکن‌های
استدلال را در هر درخواست می‌پردازید:

```ini
LLM_NUM_PREDICT=2048
```

برای مدل ریزنینگ ۲۰۴۸ تا ۴۰۹۶ بازهٔ معقولی است.

> مقدار خراب در `LLM_EXTRA_BODY` **موقع بالا آمدن سرور** خطا می‌دهد، نه
> سر اولین پرسش — پس `python -m scripts.verify_deployment` قبل از
> تحلیل‌گرها می‌گیردش. باید یک **آبجکت** JSON باشد، و اجازه ندارد
> `model`، `messages`، `temperature`، `top_p`، `seed`، `max_tokens`،
> `stop`، `stream` یا `n` را ست کند — این‌ها تنظیم خودشان را دارند.

---


## بخش ۳ — بدون LLM و دیتابیس واقعی

برای دمو یا تست UI، بدون هیچ زیرساختی:

```powershell
python -m scripts.dev_v2_demo_server
```

همان اپ FastAPI واقعی را روی `http://localhost:8000` بالا می‌آورد، اما
روی یک SQLite درون‌حافظه و یک مدل قلابی. کنارش فایل‌سرور استاتیک را بزنید
و باز کنید:

```
http://localhost:8080/
```

(صفحه از ۴.۴.۰ خودش زنده بالا می‌آید و `web/js/config.js` پیش‌فرض
`http://localhost:8000` را دارد، پس پارامتری لازم نیست.)

این ابزار راستی‌آزمایی دستی است، جایگزین تست روی مدل و دیتابیس واقعی نیست.

## بخش ۴ — ارتقا از ۶.۰ به ۶.۷

یک چک‌لیست برای نصبی که روی ۶.۰.۰ است و به ۶.۷.۰ می‌رود؛ یادداشت‌های «ارتقا» (Upgrading) نسخه‌های ۶.۰.۱ تا ۶.۶.۰ را به همان ترتیبی که باید انجام شوند پشت هم می‌آورد و آنچه ۶.۷.۰ اضافه کرده و می‌خواهید از آن استفاده کنید را هم (نسخه‌های ۶.۶.۱ و ۶.۷.۰ یادداشت «ارتقا»ی جدا ندارند). متن کامل هر نسخه در `CHANGELOG.md` و همین چک‌لیست با جزئیات بیشتر در بخش ۱۷ از `docs/deployment-runbook.md` است. اگر از ۵.x می‌آیید، اول یادداشت‌های ۶.۰.۰ را انجام دهید (`prompts/system_prompt.md` را پیش از اولین اجرا به `<PROJECT_CONFIG_DIR>/system_prompt.md` کپی کنید).

۱. از `.env` و `project_config/` پشتیبان بگیرید.
۲. `git pull` و بعد دوباره `pip install -r requirements.lock` (هر ارتقا با همین شروع می‌شود).
۳. **پیش از راه‌اندازی دوباره** `python scripts/verify_deployment.py` را بزنید. دو نسخه ورودی‌هایی را که قبلاً بی‌صدا می‌پذیرفتند حالا رد می‌کنند: کلید تکراری در فایل YAML (۶.۳.۱؛ پیام نام کلید و هر دو شمارهٔ خط را می‌دهد؛ تکراری را حذف کنید، آنی که دیرتر آمده بود مؤثر بود) و خط غیرقابل‌استفادهٔ `.env` یا فیلد تکراری داخل یک ورودی کلید (۶.۴.۰؛ `API_KEYS_JSON` چندخطی باید در کوتیشن تکی و بدون آپاستروف باشد، یا به `API_KEYS_FILE` بروید).
۴. `schema.yaml` را بررسی کنید (۶.۲.۰): اگر نام یک جدول در چند شِما تکرار شده، هر کدام را با کلید دارای شِما بنویسید (`sales.Customer:`، `ref.Customer:`) و برای هر جدول با کلید ساده `db_schema` بگذارید.
۵. (اختیاری) رمز را خام در `DB_PASSWORD` بگذارید و از `DB_CONNECTION_URL` بردارید (۶.۳.۰).
۶. (اختیاری، برای بیش از یک کلید توصیه می‌شود) کلیدها را به فایل ببرید: `project_config.example/api_keys.example.json` را به `project_config/api_keys.json` کپی کنید، `key_sha256` هر ورودی را با هش چاپ‌شدهٔ `issue_api_key` عوض کنید، `API_KEYS_FILE` را بگذارید و `API_KEYS_JSON` را **بردارید** (۶.۴.۰ و ۶.۶.۱).
۷. (اگر لازم است) `API_HOST=0.0.0.0` و `CORS_ALLOWED_ORIGINS` را برای UI‌ای که جز `localhost:8080` سرو می‌شود تنظیم کنید (۶.۰.۲).
۸. (فقط چند دیتابیس) بخش ۰.۳ همین راهنما را به ترتیب انجام دهید: `datasources.yaml`، `sync_schema.py` (پس از ۶.۸.۰؛ پیش‌تر `assign_datasources.py`)، `keywords:`، `prompt_budget.py` و در صورت نیاز `nolock` برای هر منبعی که DBA‌اش الزام کرده، جداگانه (۶.۱.۰ تا ۶.۶.۰). پیش از جایگزینی `schema.yaml`، نسخهٔ قبلی را کنار بگذارید و ستون‌های «ناموجود» گزارش را درست کنید.
۹. سرور را دوباره راه بیندازید (همهٔ تغییرهای بالا، از جمله `datasources.yaml` و `schema.yaml`، با راه‌اندازی دوباره اثر می‌کنند) و پیش‌پرواز را یک بار دیگر، این بار با `VERIFY_API_KEY`، بزنید.
۱۰. (۶.۷.۰، اختیاری ولی توصیه می‌شود) **سنجش دقت را شروع کنید.** ۶.۷.۰ ابزارهایی اضافه کرده که از استفادهٔ واقعی یک مجموعهٔ ارزیابی و یک دروازهٔ ارتقا می‌سازند: `python scripts/harvest_golden.py` (نامزدها از audit log)، `python scripts/golden_sheet.py export` و `import` (بازبینی تحلیل‌گرها در Excel)، `python -m eval.cli verify --accept` (اجرا و فعال‌کردن موردها) و `python -m eval.cli run --live --reference live` (دقت اجرا در برابر SQL مرجع، در همان اجرا و روی همان داده). این‌ها پیش از ۶.۷.۰ وجود ندارند، پس نخستین baseline روی خود ۶.۷.۰ ثبت می‌شود، وقتی audit log پرسش‌های واقعی دارد؛ از آن به بعد پیش از هر ارتقا روی نسخهٔ فعلی baseline ثبت می‌کنید و بعد از آن مقایسه (بخش ۵ همین راهنما همهٔ دستورها را دارد). baseline نوشته‌شده با نسخهٔ قدیمی‌تر هنوز بارگذاری می‌شود ولی با اجرای `--reference live` قابل مقایسه نیست؛ ابزار رد می‌کند و می‌گوید چطور دوباره ثبتش کنید. غیر از این، ۶.۷.۰ چیزی را در طرز اجرای سرور عوض نمی‌کند، و تغییر CI آن (`requirements.lock` که نصب می‌کنید حالا همان است که CI هم آزمایش می‌کند) هم اقدامی نمی‌خواهد.

از ۶.۵.۰ SQL نمایش‌داده‌شده در گفتگو با یک قالب ثابت چیده می‌شود (فقط نمایش؛ دستورِ اجراشده تغییری نکرده) و رکورد audit فیلد تازهٔ `datasource_selection` دارد که خواننده‌ای که آن را نمی‌شناسد می‌تواند نادیده بگیرد.

---

## بخش ۵ — سنجش دقت روی داده‌های واقعی و دروازهٔ ارتقا

تا وقتی مجموعه‌ای از پرسش‌های واقعی خودتان نداشته باشید، هیچ عددی برای دقت
سیستم روی انبارهٔ داده‌تان ندارید؛ نمونه‌های `eval_data.example/` ساختگی‌اند.
«مجموعهٔ طلایی» (golden set) فهرستی از پرسش‌هاست که پاسخ درست هر کدام را یک
تحلیل‌گر تأیید کرده است. جزئیات کامل در بخش ۱۸ `docs/deployment-runbook.md` است؛
این‌جا خلاصه‌اش.

**مجموعه فقط روی سرور می‌ماند.** پوشهٔ `eval_data/` (در گیت نادیده گرفته
می‌شود) پرسش‌های واقعی، SQL و ردیف‌های نتیجه دارد. فایل‌های آن، و گزارش JSON
اجراها (که پرسش و SQL هر مورد را دارد)، را از سرور بیرون نبرید. دستورهای زیر
فقط **شمارش و شناسهٔ مورد** چاپ می‌کنند و چاپ‌شده‌شان امن است؛ تنها
`--include-examples` در گام ۱ چند پرسش را کلمه‌به‌کلمه چاپ می‌کند و آن را
باید آگاهانه بزنید.

### ۵.۱ ساختن مجموعه

```powershell
# ۱. حدود ۱۵۰ پرسش نامزد از لاگ ممیزی (نمونه‌گیری طبقه‌ای: منبع داده، نتیجه، زبان)
python scripts/harvest_golden.py --n 150

# ۲. برگهٔ بازبینی برای تحلیل‌گرها (CSV با BOM؛ فارسی در Excel درست باز می‌شود)
python scripts/golden_sheet.py export

# ... تحلیل‌گرها eval_data/review.csv را در Excel پر می‌کنند ...

# ۳. برگرداندن حکم‌ها
python scripts/golden_sheet.py import

# ۴. اجرای SQL مرجع روی دیتابیس، ثبت نتیجه، و فعال‌کردن موردهای سالم
python -m eval.cli verify --golden eval_data/golden.jsonl
python -m eval.cli verify --golden eval_data/golden.jsonl --accept
```

- **گام ۱:** پرسش‌ها نرمال و بی‌تکرار می‌شوند (فرق ي/ی، ك/ک، رقم فارسی/عربی/لاتین،
  نیم‌فاصله و فاصله‌ها نادیده است). SQL پیشنهادی مدل فقط یک *پیشنهاد* است و هیچ
  حکمی نیست. فایل `eval_data/candidates.jsonl` بدون `--force` هرگز بازنویسی
  نمی‌شود و ردیف‌های نتیجه هرگز در آن کپی نمی‌شوند.
- **گام ۲ و ۳:** تحلیل‌گر ستون `verdict` را با `correct` (پیشنهاد درست است)،
  `wrong` (به‌جایش SQL ستون `correct_sql` را بگذار) یا `skip` (این پرسش به درد
  نمی‌خورد) پر می‌کند؛ خالی یعنی «هنوز بازبینی نشده». ستون `expect` یکی از
  `success` / `empty` (جواب درست صفر ردیف است) / `out_of_scope` (سیستم باید رد
  کند؛ SQL خالی). نمونهٔ پرشده: `eval_data.example/golden_review_template.csv`.
  هر SQL از نگهبان SQL می‌گذرد و خطاها با **شمارهٔ ردیف برگه** گزارش می‌شوند.
- **گام ۴:** `verify` هر SQL مرجع را فقط‌خواندنی و با همان اجراکنندهٔ برنامه
  (مسیریابی، `NOLOCK`، زمان‌بند) اجرا می‌کند، خطاها را گزارش می‌دهد (رد نگهبان،
  خطای دیتابیس، نتیجهٔ خالی در جایی که `expect` موفقیت می‌خواهد) و تنها با
  `--accept` موردهای بدون مشکل را `active` می‌کند. فایل به‌صورت اتمی بازنویسی
  می‌شود و نسخهٔ قبلی با پسوند `.bak` می‌ماند.

### ۵.۲ پیش از هر ارتقا

داده‌های انبار هر روز عوض می‌شوند؛ اثر انگشتی که ماه پیش ثبت شده جواب «معاملات
دیروز» امروز نیست. برای همین دروازه SQL مرجعِ هر مورد را **در همان اجرا و روی
همان داده** اجرا می‌کند و دو نتیجه را با هم مقایسه می‌کند (`--reference live`):

```powershell
# روی نسخهٔ فعلی، پیش از ارتقا:
python -m eval.cli run --live --reference live --golden eval_data/golden.jsonl --save-baseline eval_data/baseline.json

# ... ارتقا ...

# روی نسخهٔ جدید:
python -m eval.cli run --live --reference live --golden eval_data/golden.jsonl --baseline eval_data/baseline.json
```

کد خروجی `0` یعنی پسرفتی نیست و `1` یعنی پسرفت (دقت، تأخیر یا ردشدن‌های نگهبان از
آستانه گذشته). روی `1` بدون فهمیدن دلیلش به تولید نروید. baseline فقط با اجرایی
مقایسه می‌شود که `--reference` یکسان داشته باشد؛ baseline قدیمی (بدون
`--reference live`) رد می‌شود و پیام می‌گوید چطور دوباره ثبتش کنید.

### ۵.۳ عددها یعنی چه

- **دقت اجرا** (execution accuracy): سهم موردهایی که SQL تولیدشده *همان نتیجهٔ*
  مرجع را داده است. دو SQL متفاوت با نتیجهٔ یکسان هر دو درست‌اند.
- **«همان نتیجه»**: ردیف‌ها به‌صورت چندمجموعه مقایسه می‌شوند (تکرار شمرده می‌شود،
  ترتیب نه)؛ **نام ستون‌ها نادیده است** (alias مهم نیست) ولی ترتیب ستون‌ها همان
  که SELECT شده مهم است؛ ترتیب ردیف‌ها فقط وقتی مهم است که SQL مرجع `ORDER BY`
  سطح‌بالا همراه با `TOP`/`OFFSET` داشته باشد؛ عددها با تلرانس نسبی `1e-6`
  (با `--float-tolerance` عوض می‌شود) و **عددهای صحیح همیشه دقیق**؛ `NULL` فقط با
  `NULL` برابر است.
- **دقت به‌تفکیک برچسب و منبع داده**، و **دقت انتخاب منبع** (چند بار سیستم منبعی را
  برگزید که مورد به آن تعلق دارد) وقتی موردها `datasource` داشته باشند. این‌ها
  توضیحی‌اند و به‌تنهایی دروازه را نمی‌شکنند.
- وضعیت `reference_error` یعنی SQL مرجعِ خودِ مورد امروز رد شده یا خطا داده (مثلاً
  جدولی تغییر نام داده)؛ اشکال از مورد است نه از مدل: `eval.cli verify` را بزنید.
- اجرای بدون `--live` (همان که CI می‌زند) پاسخ‌های ثبت‌شده را بازپخش می‌کند و ۱۰۰٪ آن
  به‌ساختار درست است و دقت نیست؛ `--reference live` بدون `--live` رد می‌شود.

---

## بخش ۶ — به اشتراک‌گذاری امن اطلاعات عیب‌یابی

وقتی چیزی خراب می‌شود از شما لاگ، پیکربندی یا خروجی دستور می‌خواهند. آنچه کمک می‌کند بفرستید و چیزی را که دری باز می‌کند نه. هر فایل را پیش از ضمیمه بخوانید، و به‌جای ضمیمهٔ کل فایل، چند خطِ مهم را بچسبانید.

**این‌ها را هرگز با محتوای واقعی نفرستید:**

- `.env`. رمز دیتابیس (`DB_PASSWORD` و هر `DB_PASSWORD_*` یا متغیری که یک `password_env:` نام می‌برد)، `OPENAI_API_KEY`، `API_KEYS_JSON` و احتمالاً رمزِ نوشته‌شده داخل `DB_CONNECTION_URL`، `APP_DB_URL` یا یک متغیر `url_env` در آن است. اگر کسی باید تنظیم‌هایتان را ببیند، از فایل کپی بگیرید، در **کپی** هر راز را خالی کنید (`DB_PASSWORD=`، `OPENAI_API_KEY=`، بخش رمز هر URL، و کل مقدار `API_KEYS_JSON` که ممکن است چندخطی باشد)، کپی را از اول تا آخر بخوانید و همان را بفرستید.
- `project_config/api_keys.json` (یا مقدار `API_KEYS_JSON`). هش SHA-256 دارد نه کلید، ولی فهرست کسانی است که اجازهٔ استفاده دارند و ستون‌هایی که هر کدام نباید ببینند. خروجی هر export از کلیدها هم همین‌طور.
- کلید خام API و `VERIFY_API_KEY`. اگر کلید خامی در گفتگو یا تیکت رفت، نشت‌کرده حسابش کنید: از پنل ادمین revoke کنید و کلید تازه صادر کنید (بخش ۲.۲).
- `project_config/.setup_log.json` که ممکن است URL دیتابیس را با رمزش داشته باشد (بخش ۰.۳.۱).
- `logs/audit_log.jsonl*` و `logs/query_log.jsonl*` (پرسش‌های واقعی و SQL تولیدشده)، فایل‌های SQLite زیر `logs/` (`app.db`، `sessions.db` و فایل‌های `-wal` و `-shm` آن‌ها)، `exports/` (نتیجه‌ها)، `eval_data/` و هر گزارشی که از آن ساخته شده (بخش ۵)، و `project_config_draft/` (پیش‌نویس‌ها ممکن است مقدار واقعی نقل کنند، بخش ۰.۳.۲).

**این‌ها، بعد از نگاه کردن، امن‌اند:**

- خروجی `python -m scripts.verify_deployment`. هرگز رمز چاپ نمی‌کند: مقصد اتصال با رمز پوشانده نشان داده می‌شود و `VERIFY_API_KEY` تکرار نمی‌شود. اما نام میزبان، دیتابیس و جدول‌ها در آن هست.
- خروجی `python scripts/sync_schema.py`، `python scripts/assign_datasources.py` و `python scripts/prompt_budget.py`: نام جدول‌ها و عددها؛ هیچ اعتبارنامه یا کلید API چاپ نمی‌شود.
- گزارش تجمیعی `python scripts/analyze_audit_log.py` (بخش ۸ از `docs/deployment-runbook.md`)، نه نسخهٔ `--include-examples`.
- `project_config/schema.yaml`: نام جدول و ستون و توضیح‌ها. ببینید توضیحی مقدار واقعی نقل نکرده باشد (پیش‌نویس بخش ۰.۳.۲ می‌تواند).
- `project_config/datasources.yaml`. رمز نمی‌تواند داخلش باشد: کلید `password:`، `pwd:` یا `url:` رد می‌شود و فایل فقط *نام* متغیر نگه‌دارندهٔ راز را دارد (`password_env: DB_PASSWORD_SALES`). اما نام سرور، دیتابیس و لاگین در آن هست؛ اگر چیدمان سرورهایتان محرمانه است آن‌ها را خالی کنید.
- خط‌های شروع سرور (بنر، `CORS allowed origins`، `Prompt path for data source`) و خط `[FAIL]` که دربارهٔ آن می‌پرسید.

هر چه می‌فرستید بنویسید چه چیزی را برداشته‌اید، تا خواننده مقدار خالی‌شده را با مقدار جاافتاده اشتباه نگیرد.

---

## چک‌لیست کوتاه

**CLI**

- [ ] `.env` پر شده
- [ ] نُه فایل `project_config/` و `system_prompt.md` سر جایشان
- [ ] `python -m scripts.verify_deployment` سبز
- [ ] `python app.py`

**وب**

- [ ] همهٔ موارد بالا
- [ ] کلید API صادر شده و در `API_KEYS_FILE` (یا `API_KEYS_JSON`) هست
- [ ] ترمینال ۱: `uvicorn api.server:app --port 8000` **از ریشهٔ ریپو**
- [ ] ترمینال ۲: `python -m http.server 8080` **از `web/`**
- [ ] در مرورگر **`http://localhost:8080`** را باز کنید (نه ۸۰۰۰)
- [ ] صفحه از قبل زنده است — فقط **کلید API** را بگذارید و ذخیره کنید
      (دنبال فیلد نشانی بک‌اند نگردید؛ از ۴.۴.۰ حذف شده و در
      `web/js/config.js` است)

**پنل ادمین**

- [ ] یک کلید با `--full-admin` صادر شده (یا `admin` به‌علاوهٔ دست‌کم یکی
      از `operations`/`security` — با `admin` تنها، ۸ بخش از ۱۲ بخش ۴۰۳ می‌دهد)
- [ ] اگر JSON چندخطی است، کل مقدار در کوتیشن تکی
- [ ] سرور ری‌استارت شده
- [ ] **`http://localhost:8080/admin/`** و **کلید خام** (۴۳ نویسه)، نه هش

**چند منبع داده**

- [ ] `datasources.yaml` و یک `DB_PASSWORD_*` برای هر منبع
- [ ] `python scripts/sync_schema.py` اجرا و `schema.yaml` (با نسخهٔ پشتیبان `schema.yaml.bak`) با `schema.synced.yaml` جایگزین شده و `--check` می‌گوید `CHECK OK`
- [ ] ستون‌ها و جدول‌های نشان‌شده با `# not in database` / `# not found in any data source` صفر شده‌اند
- [ ] `nolock: true` فقط زیر منبع‌هایی است که DBA‌شان خواسته (برای هر منبع جدا)
- [ ] `python -m scripts.verify_deployment` برای هر منبع سبز
- [ ] `python scripts/prompt_budget.py` اجرا و `PROMPT_RETRIEVAL_TOKEN_BUDGET` تنظیم شده
- [ ] کارت‌های «انحراف شِما» و «تازگی واژگان ابعاد» بررسی شده

**پیش از ارتقا** (بخش ۵)

- [ ] روی نسخهٔ فعلی baseline با `--reference live` ثبت شده
- [ ] پس از ارتقا، `python -m eval.cli run --live --reference live --golden eval_data/golden.jsonl --baseline eval_data/baseline.json` کد خروجی `0` داد
