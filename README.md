# Local SQL Agent

> Ask your database a question in Persian or English.  
> Get back precise SQL — generated locally, executed securely, zero data leaves your machine.

[![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/Tests-2079-green)](tests/)
[![Version](https://img.shields.io/badge/Version-4.0.1-blue)](CHANGELOG.md)

---

Most Text-to-SQL tools assume your data is in the cloud and your questions are in English. This project was built for the opposite: an on-premise warehouse where analysts ask in Persian, the data is sensitive enough that it may not leave the building, and there is no budget for external APIs.

The result is a fully local NLQ engine — a modular retrieval pipeline, an AST-based SQL guard, authentication with a column-level ACL, conversational sessions, an evaluation harness, and a domain knowledge base that lives entirely outside the engine.

It was built for, and runs in production at, the Iran Mercantile Exchange. **None of that domain is in this repository** — the schema, the aliases, the business rules and the examples all live in a gitignored `project_config/`, and `tests/test_no_domain_literals.py` fails the build if a warehouse name reappears in engine source. Point it at your own warehouse and it is your domain, not somebody else's.

---

## In action

> The schema below is a made-up retail example, used here only to show the
> shape of the output. The engine ships with no schema at all — it reads
> yours from `project_config/schema.yaml`.

```bash
python app.py

Question: ۱۰ مشتری برتر از نظر مبلغ خرید در سال ۱۴۰۳ کدام‌اند؟

══════════════════════════════════════════════════════════════
GENERATED SQL
══════════════════════════════════════════════════════════════
SELECT TOP 10
    c.Name,
    SUM(o.TotalAmount) AS PurchaseValue
FROM [Sales_Fact].[Order]     o
JOIN [Sales_Dim].[Customer] c ON o.CustomerID = c.ID
JOIN [Sales_Dim].[Date]     d ON o.DateID     = d.ID
WHERE d.JalaliYear = 1403
GROUP BY c.Name
ORDER BY PurchaseValue DESC

Returned Rows: 10  |  Execution Time: 1.24s  |  Excel: exports/result_20260613_142257.xlsx
```

Or over HTTP:

```bash
curl -X POST http://localhost:8000/query \
  -H 'Authorization: Bearer <your-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{"question": "فروش ماهانه دسته لوازم خانگی در ۱۴۰۳", "mode": "full"}'
```

```json
{
  "question": "فروش ماهانه دسته لوازم خانگی در ۱۴۰۳",
  "sql":    "SELECT TOP 1000 d.JalaliMonthName, SUM(o.TotalAmount) AS SalesValue ...",
  "result": [{"JalaliMonthName": "فروردین", "SalesValue": 48320000000}, ...],
  "row_count": 12,
  "status": "SUCCESS"
}
```

And a follow-up question keeps its context, instead of starting over:

```bash
curl -X POST http://localhost:8000/v2/sessions/$SID/turns \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"question": "از بین آن‌ها کدام بیشترین تعداد سفارش را داشت؟"}'
```

The engine composes that against the previous turn's SQL as a CTE rather
than re-querying the warehouse, and returns every assumption it made —
which measure, which period, which scope — as declared, editable data
alongside the answer.

---

## How it works

Before the LLM sees anything, six retrievers build a scoped context from your question:

```
Question (Persian / English)
    │
    ▼
 ContextRetriever
    ├─ EntityRetriever        alias match → TF-IDF fallback
    ├─ FactRetriever          keyword match → TF-IDF fallback
    ├─ RelationshipRetriever  JOIN clauses for selected tables
    ├─ RuleRetriever          domain business rules
    ├─ ExampleRetriever       tag-scored few-shot SQL examples
    └─ ValueRetriever         resolves named values against the warehouse
    │
    ▼
 PromptBuilder   →  [ static prefix — byte-identical, KV-cached ]
                    [ variable suffix — session, filters, question ]
    │
    ▼
 SQLAgent        →  generate → clean → validate → auto-correct (bounded)
    │
    ▼
 SQLGuard        →  AST allowlist, column ACL, row cap
    │
    ▼
 transpile       →  target dialect, then re-validated in that dialect
    │
    ▼
 Database        →  result set  →  Excel / CSV / JSON
```

Two things make locally-run 8B–20B models accurate enough for production
here. **Scoping the prompt** instead of dumping the whole schema is one.
The other is that the scoped part is confined to a *variable suffix*: the
prefix is byte-identical across every request, so a local endpoint reuses
its KV cache instead of re-reading the schema on every question.

---

## Features

| | Feature | Detail |
|---|---|---|
| 🔒 | **On-premise LLM** | OpenAI-compatible endpoint (vLLM / LM Studio / Ollama /v1). No cloud provider required, no data leaves the host. |
| 🗂️ | **Domain lives outside the engine** | Schema, aliases, metrics, rules and examples are YAML in a gitignored `project_config/`; an AST test fails the build if any of it leaks into source. |
| 🌐 | **Bilingual** | Persian and English questions handled natively. |
| 🧩 | **Modular retrieval** | 6 independent retrievers — swap or extend without touching the engine. |
| 🔍 | **Two-tier retrieval** | Fast alias/pattern matching first; TF-IDF bigram engine as fallback. |
| 🎯 | **Few-shot learning** | Tag-scored example selector injects the most relevant SQL patterns. |
| 📐 | **Business rule injection** | Domain rules injected per question topic at prompt-build time. |
| 🛡️ | **SQL security guard** | AST-based (sqlglot), closed table/column allowlist; blocks DDL, DML, injection; converts LIMIT→TOP. |
| 🔄 | **Auto-correct loop** | Retries with error feedback when SQL fails validation or execution — bounded, and never re-prompted for a rejection no rewrite could satisfy. |
| 💬 | **Conversational sessions** | `/v2/sessions*` — follow-up questions resolve «از بین آن‌ها» against the previous turn via CTE composition, with every assumption declared. |
| 🔑 | **Authentication & column ACL** | API keys on every route but `/health`; per-principal `denied_columns` enforced in the guard, not just partitioned in the cache. |
| 🗄️ | **Multi-dialect** | Generates T-SQL, transpiles, then re-validates in the dialect that will execute. T-SQL and SQLite verified by execution. |
| ⚡ | **FastAPI HTTP API** | REST endpoints for query, sessions, cache, and health check. |
| 💾 | **LRU query cache** | Thread-safe TTL + LRU cache, partitioned by visibility scope so two principals never share a result they should not. |
| 📊 | **Evaluation harness** | Golden set, execution accuracy, error taxonomy, latency percentiles, determinism measurement, baseline regression gate. |
| 🔬 | **LLM observability** | 21-field status block per request: tokens, prefix-cache hit, timings, corrections, `finish_reason` read from the response. |
| 📤 | **Structured exports** | Excel, CSV, JSON with timestamped filenames. |
| 📋 | **Audit trail** | Compliance-grade JSONL records with principal, guard verdict and timings — and never result rows. |
| 🧪 | **Test suite** | 2,079 unit + integration tests; GitHub Actions CI on Python 3.11–3.13. |

---

## Quick start

**Requires:** Python 3.11+, an OpenAI-compatible endpoint (vLLM / LM Studio / Ollama `/v1`) reachable via `OPENAI_BASE_URL`, SQL Server + ODBC Driver 17

```bash
# 1. Clone and install
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Set at minimum:
#   DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/DB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
#   OPENAI_BASE_URL=http://your-llm-host:8000/v1
#   OPENAI_MODEL=gpt-oss-20:F16
#   OPENAI_API_KEY=your-key

# 3. Provide the domain config — the server will NOT start without it
cp -r project_config.example project_config
# Then fill in your own schema, aliases, metrics, business rules and
# examples. project_config/ is gitignored on purpose: it is your data,
# not the engine's. There is deliberately no silent fallback to the
# example files.

# 4. Issue an API key (every route but /health requires one)
python -m scripts.issue_api_key

# 5a. CLI
python app.py

# 5b. HTTP API
uvicorn api.server:app --host 0.0.0.0 --port 8000

# 6. Before a real deployment, check the four things that stop a week
python -m scripts.verify_deployment
```

**→ Full tutorial (installation · first query · extending the domain · writing tests · diagnosing misses):**  
**[English](docs/en/tutorial.md) · [فارسی](docs/fa/tutorial.md)**

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (vLLM / LM Studio / Ollama `/v1`) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name served by the endpoint |
| `OPENAI_API_KEY` | *(required)* | API key for the endpoint |
| `DB_CONNECTION_URL` | *(required)* | SQLAlchemy connection string |
| `QUERY_TIMEOUT_SECONDS` | `60` | Max query execution time (seconds) |
| `MAX_ROWS_RETURNED` | `1000` | Hard row cap applied to all queries |
| `CACHE_TTL_SECONDS` | `300` | Query cache TTL in seconds (`0` = disabled) |
| `CACHE_MAX_SIZE` | `256` | Maximum number of cached query results |
| `LOG_DIR` | `logs` | Log file directory (auto-created) |
| `EXPORT_DIR` | `exports` | Export file directory (auto-created) |
| `API_KEYS_JSON` | *(empty)* | JSON array of `{"id","name","key_sha256","denied_columns"?}` — see [Authentication](#authentication-phase-8) |
| `AUTH_REQUIRED` | `true` | Fail-closed auth gate; `false` is a logged escape hatch |
| `APP_DOCS_PUBLIC` | `false` | Serve `/docs` `/redoc` `/openapi.json` without credentials |
| `PROJECT_CONFIG_DIR` | `project_config` | Where the domain YAML lives. No silent fallback to the example directory |
| `SQL_DIALECT` | `tsql` | Target dialect. `tsql` and `sqlite` are verified by execution; others transpile and re-validate but are unverified |
| `SESSION_TTL_SECONDS` | `1800` | Idle expiry for a conversational session |
| `SESSION_MAX_TURNS` | `50` | Transcript cap per session |
| `SESSION_PROMPT_TURNS` | `3` | How many prior turns enter the prompt |

Full list in `config.py` — every setting carries a docstring explaining
what it does and why its default is what it is.

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/query` | Run a natural-language query; returns SQL + result set |
| `POST` | `/v2/sessions` | Start a conversation |
| `GET` | `/v2/sessions/{sid}` | Its transcript |
| `POST` | `/v2/sessions/{sid}/turns` | Ask, in context; add `?stream=1` for SSE |
| `PATCH` | `/v2/sessions/{sid}/turns/{tid}/assumptions` | Re-run under edited assumptions — returns a *new* turn, never mutates the old one |
| `DELETE` | `/v2/sessions/{sid}` | Drop a conversation and free its state |
| `GET` | `/health` | DB + LLM endpoint reachability probe |
| `GET` | `/cache/stats` | Cache size, hits, misses, evictions |
| `POST` | `/cache/invalidate` | Remove a specific cached entry |
| `POST` | `/cache/clear` | Flush the entire cache |

Every route above except `GET /health` requires `Authorization: Bearer <key>`
— see [Authentication](#authentication-phase-8). The conversational contract
is frozen in `docs/api-contract-v2.md`.

**Error taxonomy:**

| Exception | HTTP | When |
|---|---|---|
| `UnauthenticatedError` | 401 | Missing/invalid API key on a protected route |
| `OutOfScopeError` | 422 | Question is outside the domain |
| `ModelTimeoutError` | 504 | LLM request timed out |
| `ModelUnavailableError` | 503 | LLM endpoint unreachable after all retries |
| `QueryExecutionError` | 500 | SQL Server execution failure |

---

## Extending the domain

All domain knowledge lives in `project_config/*.yaml`. No engine code
needs to change — and no engine code *may* contain it:
`tests/test_no_domain_literals.py` walks the AST of first-party source
and fails if a warehouse name reappears in an executable literal.

```yaml
# project_config/aliases.yaml — a new trading-hall alias
ring_aliases:
  "<canonical hall name>": ["<synonym>", "<synonym>", "<synonym>"]

# project_config/business_rules.yaml — a rule injected per question topic
rules:
  - topic: "<topic key>"
    text: "<the rule, in the analyst's own language>"

# project_config/examples.yaml — a tag-scored few-shot example
examples:
  - tags: ["<topic>", "<measure>"]
    question: "<a question an analyst would actually ask>"
    sql: "SELECT ..."

# project_config/schema.yaml — tables, columns, relationships
```

`schema.yaml` is a **security file**: the guard derives its table and
column allowlist from it, so adding a table widens what generated SQL may
touch and a typo silently narrows the allowlist. Run
`tests/test_schema_registry_snapshot.py` after editing it.

Start from `project_config.example/`, which carries the same structure
with placeholder data and is what CI and the test suite run against.

Full step-by-step guide: **[English tutorial](docs/en/tutorial.md)** · **[آموزش فارسی](docs/fa/tutorial.md)**

---

## Project structure

```
local-sql-agent/
├── app.py                    # CLI entry point (REPL)
├── config.py                 # Typed Settings singleton (env-based)
├── api/                      # FastAPI HTTP service
│   ├── server.py             #   app factory + endpoints
│   ├── runner.py             #   cache-aware query orchestrator
│   ├── query_cache.py        #   thread-safe TTL + LRU cache
│   ├── models.py             #   Pydantic request/response models
│   ├── errors.py             #   NLQError hierarchy → HTTP handlers
│   ├── middleware.py         #   correlation ID + latency headers + rate limiting
│   ├── auth.py               #   AuthMiddleware + require_principal (Phase 8)
│   └── health.py             #   /health — DB + LLM endpoint probes
├── project_config/           # ★ YOUR DOMAIN — gitignored, required, not in this repo
│   ├── schema.yaml           #   tables, columns, relationships (the guard's allowlist)
│   ├── aliases.yaml          #   canonical names + user synonyms
│   ├── business_rules.yaml   #   rules injected per question topic
│   ├── entities.yaml         #   entity → table hints
│   ├── examples.yaml         #   tagged few-shot NLQ→SQL pairs
│   ├── metrics.yaml          #   metric definitions + aggregate expressions
│   ├── retrieval_hints.yaml  #   fact tables + trigger phrases
│   └── session_policy.yaml   #   the default scope assumption
├── project_config.example/   # Same structure, placeholder data — what CI runs against
├── knowledge/                # Lazy loaders + validation for the YAML above
│   ├── config_loader.py      #   Pydantic models, fail-closed on a missing file
│   ├── aliases.py            #   (loader, not data)
│   ├── business_rules.py     #   (loader, not data)
│   ├── entities.py           #   (loader, not data)
│   ├── examples.py           #   (loader, not data)
│   ├── metrics.py            #   (loader, not data)
│   ├── retrieval_hints.py    #   (loader, not data)
│   └── session_policy.py     #   (loader, not data)
├── session/                  # Conversational sessions (v2 API)
│   ├── engine.py             #   TurnEngine — one question in session context
│   ├── models.py             #   Turn, Assumption, Basis, GuardVerdict
│   ├── store.py              #   TTL + count + turn-capped session store
│   ├── refinement.py         #   fresh vs refines classification
│   ├── composer.py           #   CTE composition for "among those"
│   └── ambiguity.py          #   declared assumptions + clarifications
├── retrieval/                # Modular retrieval pipeline
│   ├── context_retriever.py  #   orchestrator → RetrievalContext
│   ├── entity_retriever.py   #   dimension table detection
│   ├── fact_retriever.py     #   fact table detection
│   ├── relationship_retriever.py  # JOIN clause selection
│   ├── rule_retriever.py     #   business rule injection
│   ├── value_resolver.py     #   resolves a named value against the warehouse
│   ├── dimension_vocabulary.py  # prefetched vocabulary + background refresh
│   └── example_retriever.py  #   tag-scored few-shot selection
├── schema_data/              # Schema registry, populated from schema.yaml
│   ├── registry.py           #   SchemaRegistry + LRU cache
│   ├── columns.py            #   column allowlist (derived, not authored)
│   ├── relationships.py      #   FK → JOIN SQL map
│   └── retriever.py          #   TF-IDF bigram fallback engine
├── prompt_engine/
│   ├── builder.py            #   PromptBuilder.build()
│   ├── static_prefix.py      #   the byte-identical, KV-cacheable prefix
│   └── templates.py          #   PROMPT_TEMPLATE
├── llm/
│   ├── sql_agent.py          #   generate → clean → auto-correct loop
│   ├── router.py             #   task → endpoint routing, fallback
│   ├── providers.py          #   OpenAI-compatible provider (retries + back-off)
│   └── base.py               #   LLMBackend ABC
├── security/
│   ├── sql_guard.py          #   clean_sql / validate_sql / ensure_top / transpile
│   ├── dialects.py           #   per-dialect profiles (catalogues, timeouts, quoting)
│   └── auth.py               #   Principal, API-key resolution, cache scope key
├── observability/
│   ├── audit.py              #   compliance-grade records — never result rows
│   ├── llm_status.py         #   the 21-field per-request status block
│   └── timing.py             #   per-stage timings
├── eval/                     # Evaluation harness
│   ├── runner.py             #   golden set → CaseResult
│   ├── report.py             #   accuracy, error taxonomy, latency percentiles
│   ├── fingerprint.py        #   order-insensitive result hash
│   ├── determinism.py        #   repeat-and-compare against a live endpoint
│   └── baseline.py           #   regression gate with a CI exit code
├── database/
│   ├── connection.py         #   SQLAlchemy engine singleton
│   └── executor.py           #   timeout + row cap + always-rolled-back transaction
├── web/                      # Static Persian/RTL client (no build step)
├── webapp/                   # Flask web application (bilingual FA/EN)
├── exporters/                # Excel / CSV / JSON exporters
├── scripts/
│   ├── verify_deployment.py  #   pre-flight check for the four things that stop a week
│   ├── issue_api_key.py      #   mint a new API key
│   ├── analyze_audit_log.py  #   aggregate-safe audit analysis
│   └── analyze_misses.py     #   offline retrieval miss diagnostics
├── docs/
│   ├── api-contract-v2.md    #   the frozen conversational-session contract
│   ├── deployment-runbook.md #   ordered deployment steps
│   ├── db-hardening.md       #   server-side hardening for the DBA
│   ├── en/tutorial.md        #   full English tutorial
│   └── fa/tutorial.md        #   full Persian tutorial — آموزش کامل فارسی
└── tests/                    # 2,079 unit + integration tests
```

---

## Tests

```bash
pytest tests/ -v                        # all tests
pytest tests/test_sql_guard.py -v       # one module
pytest --cov=. --cov-report=html        # with coverage report
```

CI runs on every push via GitHub Actions across Python 3.11, 3.12 and
3.13, with doctests, coverage, and an offline evaluation gate. It runs
with `PROJECT_CONFIG_DIR=project_config.example` and no `project_config/`
present, so the suite never depends on real domain data.

---

## Security model

Every generated SQL query passes through `security/sql_guard.py` before execution.
`validate_sql` is **parser-based** (via [sqlglot](https://sqlglot.com/)), not a
string blocklist — see the module's docstring for the full mechanism and
`tests/test_sql_guard_bypass.py` for the bypasses and false-positives this
replaced. When a target dialect other than T-SQL is configured, the query is
transpiled and then **re-validated in the dialect it will actually execute
in**, and refused if its touched-table set changed; the bypass suite is
parametrised over every claimed dialect, because a guard proven for one
dialect and assumed for another has unknown holes.

- **Exactly one statement:** the query is parsed and rejected if it is not a single T-SQL statement — stacked statements are refused as a class, not by recognising each one's keyword
- **Allowlist by AST node, not keyword:** only a `SELECT`/`WITH` root, or a top-level `UNION`/`INTERSECT`/`EXCEPT`, is permitted; `INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, `MERGE`, `TRUNCATE`, `GRANT`, `REVOKE`, `EXEC`/`EXECUTE`, `SELECT ... INTO`, and `xp_*`/`sp_*`/`OPENROWSET`/`OPENQUERY`/`OPENDATASOURCE` are refused by node type or function name, wherever they appear in the tree
- **Table allowlist, strictly enforced:** every table reference must resolve to the allowlist derived from your `project_config/schema.yaml` (case-insensitively, schema/db qualifier ignored) or be a CTE defined earlier in the same query — an unresolvable table (hallucinated, out-of-domain, or malicious) is refused outright, independent of whether the DB login is itself scoped to just these tables (see `docs/db-hardening.md`). This is why `schema.yaml` is a security file: adding a table widens what generated SQL may touch, and a typo silently narrows the allowlist
- **Column allowlist, deliberately lenient:** every resolvable qualified column reference is checked against its table's known columns; an unqualified column, or one qualified by a CTE name or derived-table alias, is allowed rather than risk a false-positive rejection — this leniency applies to *columns* only, not table names
- **Column-level ACL seam:** `validate_sql(sql, denied_columns=...)` refuses any query touching a named column, regardless of table — the foundation for future multi-tenant column policies; `*`/`alias.*` cannot be used to read around an active policy (it is expanded against its resolved table(s) and checked, or refused outright if it can't be resolved with confidence)
- **No SQL comments:** any comment is refused outright because it is present — its content is never inspected for keywords, since scanning comment text would repeat the same substring-matching mistake this module was rewritten to fix, just in a new place
- **System catalogues blocked by AST node, not substring,** per dialect: `INFORMATION_SCHEMA`/`sys.*` for T-SQL, `pg_catalog`/`pg_*` for PostgreSQL, `sqlite_*` for SQLite, and so on. A dialect with no catalogue list configured is refused **at start-up** — an empty blocklist is indistinguishable from "nothing to block", which is the failure direction that loses
- **LIMIT→TOP:** `LIMIT n` is rewritten to `TOP n` for T-SQL before execution; for other targets the row cap is applied on the AST and rendered in that dialect's own syntax
- **Row cap:** `MAX_ROWS_RETURNED` is enforced as a hard ceiling on every result set, and `database/executor.py` streams results rather than materialising the whole set client-side
- **Defense in depth at the database layer:** `database/executor.py` runs every query inside a transaction that is always rolled back (never committed), with both a driver-level query timeout and `SET LOCK_TIMEOUT`; `docs/db-hardening.md` specifies the server-side login/DENY/Resource Governor hardening for the DBA to apply on top of this
- **No hardcoded credentials:** all secrets via environment variables only

### Authentication (Phase 8)

Every route except `GET /health` requires a named API key, sent as
`Authorization: Bearer <key>`. `X-API-Key` and every other transport are
deliberately not supported — one way in is one thing to reason about.

- **Named API keys, not JWT/OIDC:** this is an on-prem tool with no IdP
  dependency; what auth actually needs to provide is a principal identity to
  key the cache on, own a session, and name in the audit trail. See
  `docs/api-contract-v2.md`'s authentication section for the full rationale.
- **Never store raw keys:** `API_KEYS_JSON` holds only each key's SHA-256 hex
  digest (`security/auth.py`). Issue a new key with `python
  scripts/issue_api_key.py --id <id> --name <name>` — it prints the raw key
  **once**, never to a file or log.
- **Fail closed:** with `AUTH_REQUIRED=true` (the default) and no keys
  configured, the server refuses to start rather than run with a front door
  nobody can open. `AUTH_REQUIRED=false` is a deliberate escape hatch that
  logs a `WARNING` on every startup, not just the first.
- **Cache isolation without losing cache sharing:** the query cache
  partitions on a hash of each principal's `denied_columns` (`security.auth.scope_key`),
  not on principal id directly — two principals with identical data
  visibility still share entries (preserving today's hit rates), while two
  with different visibility can never collide.
- **Column-level ACL:** a key's `denied_columns` feeds straight into
  `security/sql_guard.py`'s existing `denied_columns` seam — no new
  enforcement machinery, just the first thing that populates it.
- **Sessions are owned:** a `/v2/sessions` session belongs to the principal
  that created it; a non-owner gets `404`, never `403` — a `403` would itself
  confirm the session exists to a caller who has no business knowing that.
- **Rate limiting keys on principal, not just IP:** behind a shared proxy,
  IP-only bucketing would put the whole organisation in one bucket; an
  authenticated caller gets their own.
- **`/docs` / `/redoc` / `/openapi.json` require auth too** (`APP_DOCS_PUBLIC=false`
  by default) — the generated API documentation describes exactly what an
  authenticated caller can do to production data.

---

## License

**Business Source License 1.1 (BUSL-1.1)** — see [`LICENSE`](LICENSE).

- ✅ Free for non-production, research, and personal use
- ❌ Commercial/production use requires a written agreement with the author
- 🔄 Converts to **Apache 2.0** on **2029-01-01**
- 📌 Derivative works must retain [`LICENSE`](LICENSE) and include:
  > *Based on Local SQL Agent by Ali Sadeghi Aghili — https://github.com/alisadeghiaghili/local-sql-agent*

Read the terms carefully rather than assuming either extreme: BUSL-1.1 is
neither all-rights-reserved nor open source. **Copying, modifying and
redistributing are permitted.** What is not permitted without a written
agreement is **production use of any kind** — including internal production
use inside a company. Deploying this to serve real users or real business
data is production use whether or not money changes hands.

### Where the terms are stated

| File | Audience |
|---|---|
| [`LICENSE`](LICENSE) | The terms themselves |
| [`NOTICE`](NOTICE) | Attribution block a derivative work must carry |
| [`AGENTS.md`](AGENTS.md) | AI coding assistants and agents reading this repo |
| [`llms.txt`](llms.txt) | Crawlers and training pipelines |
| `SPDX-License-Identifier` header | Every `.py` file — travels with a single copied file |
| `core/provenance.py` | The start-up banner, logged on every run |

`tests/test_license_headers.py` fails if a new source file lands without the
header, or if any of those files is deleted. `tests/test_provenance_notice.py`
fails if the start-up notice stops being emitted — an unchecked notice is one
that quietly disappears.

The banner is a log line, not a licence check: it does not refuse to start,
degrade, or phone home when files are missing. A kill switch keyed on a
file's presence is a production outage waiting for the first container build
that excludes `*.md`, and it would land on whoever is on call rather than on
an infringer.

---

## Contributors

### [Ali Sadeghi Aghili](https://github.com/alisadeghiaghili) — System Architecture & Engineering

**Role:** Creator & Lead Engineer

| Area | Modules |
|---|---|
| **Orchestration & CLI** | `app.py` — REPL: question → retrieval → generation → guard → execution → export → structured logging |
| **Configuration** | `config.py` — typed `Settings` singleton, env-based overrides, `override_settings()` test helper, and the tuning-layer rule that keeps knobs out of source |
| **Core layer** | `core/models.py`, `core/persian.py` — frozen dataclasses; the single versioned Persian normalizer the cache and the retriever both agree on |
| **LLM integration** | `llm/sql_agent.py`, `llm/router.py`, `llm/providers.py` — bounded generate/clean/auto-correct loop, task routing with fallback, retries with back-off |
| **Retrieval pipeline** | `retrieval/` — orchestrator plus all six sub-retrievers; warehouse-backed value resolution with stale-while-revalidate prefetch |
| **Schema layer** | `schema_data/`, `knowledge/config_loader.py` — schema registry and allowlists derived from YAML, fail-closed on a missing file |
| **Prompt engineering** | `prompt_engine/` — static prefix / variable suffix split for KV-cache reuse |
| **Validation & security** | `security/` — sqlglot-AST guard (single statement, SELECT-only, table/column allowlist, column ACL), per-dialect profiles, transpile-and-re-verify, API keys |
| **Conversational sessions** | `session/` — `Turn` contract, CTE-composed refinement, declared assumptions |
| **Evaluation & observability** | `eval/`, `observability/` — golden set, execution accuracy, result fingerprinting, determinism, baseline gate; audit records, stage timings, LLM status block |
| **Database** | `database/` — SQLAlchemy engine singleton, query timeout, hard row cap, always-rolled-back transaction |
| **FastAPI service** | `api/` — `/query`, `/v2/sessions*`, `/health`, `/cache`; auth middleware; correlation IDs; LRU + TTL `QueryCache`; typed `NLQError` hierarchy |
| **Static web client** | `web/` — Persian/RTL, no build step: pipeline view, assumption chips, result-shape selection, charts |
| **Exports & logging** | `exporters/`, `logs/` — Excel/CSV/JSON exporters; rotating JSONL logger |
| **Test suite** | `tests/` — 2,079 unit and integration tests; GitHub Actions CI across Python 3.11–3.13 |

---

### [Melika Bahmanabadi](https://github.com/MelikaBahmanabadi) — Domain Knowledge & Web Application

**Role:** Domain Expert & Knowledge Engineer

| Area | Contribution |
|---|---|
| **Domain knowledge base** | The trading-hall alias map, named business metrics with their aggregate expressions, annotated NLQ→SQL few-shot examples, the business rules injected into prompts, and the entity catalog mapping Persian and English concepts to warehouse tables. All of it now lives in `project_config/*.yaml`, outside this repository. |
| **Flask web application** | `webapp/` — the bilingual FA/EN interface: language system, sample-question panel, SQL beautifier, result pagination, copy and download, Persian typography |
| **Schema knowledge** | Table and column semantics, canonical name mappings, and the Persian date-querying rules the model is taught |

---

Contributions welcome — open an issue before submitting a PR.

---

Built with an OpenAI-compatible LLM endpoint (vLLM / LM Studio / Ollama `/v1`) · [FastAPI](https://fastapi.tiangolo.com) · [SQLAlchemy](https://sqlalchemy.org) · [scikit-learn](https://scikit-learn.org)
