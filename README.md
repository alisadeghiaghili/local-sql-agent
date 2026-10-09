# Local SQL Agent

> Ask your database a question in Persian or English.  
> Get back precise SQL — validated against a closed allowlist before it runs.
> Built to run fully on-premise: point `OPENAI_BASE_URL` at a local model and no question, schema, or row leaves your network.

[![CI](https://github.com/alisadeghiaghili/local-sql-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/alisadeghiaghili/local-sql-agent/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-94%25-brightgreen)](setup.cfg)
[![Tests](https://img.shields.io/badge/tests-5%2C470-brightgreen)](tests/)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://python.org)
[![Release](https://img.shields.io/github/v/release/alisadeghiaghili/local-sql-agent)](https://github.com/alisadeghiaghili/local-sql-agent/releases)
[![License: BUSL-1.1](https://img.shields.io/badge/license-BUSL--1.1-blue.svg)](LICENSE)  
[![SQL guard](https://img.shields.io/badge/SQL%20guard-AST%20allowlist-8A2BE2)](security/sql_guard.py)
[![Domain-free engine](https://img.shields.io/badge/engine-no%20domain%20literals-8A2BE2)](tests/test_no_domain_literals.py)
[![Doctests](https://img.shields.io/badge/doctests-enforced%20in%20CI-8A2BE2)](.github/workflows/ci.yml)

<sub>The CI and release badges read GitHub directly. Coverage is enforced
on every push — the build fails below the 90% gate in
[`setup.cfg`](setup.cfg) — and the coverage and test figures shown were
measured at v6.7.0 (`pytest tests/ eval/tests --cov`);
`tests/test_readme_claims.py` fails the build if the badge ever claims
more than the gate actually holds. The three purple badges are claims a
build step enforces, not aspirations: each links to the guard that makes
it true.</sub>

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

A conversation turn also carries `sql_display`: the same statement laid out
in a fixed house style for reading (`SELECT` alone on its line, aligned
aliases and joins). It is display only. `sql` is the text that was validated,
executed and audited, and `/query` and the CLI return it as it is.

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
 (several data sources: one is chosen for the question first —
  keywords, session, retrieval evidence, default — no model call)
    │
    ▼
 PromptBuilder   →  [ static prefix — byte-identical, KV-cached,
                      one per data source ]
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
| 🔒 | **On-premise LLM** | Any OpenAI-compatible endpoint (vLLM / LM Studio / Ollama `/v1`). No cloud provider is required — but note `OPENAI_BASE_URL` **defaults to OpenAI's hosted API**, so an on-premise deployment must point it at its own endpoint. Nothing leaves the host once it does. |
| 🗂️ | **Domain lives outside the engine** | Schema, aliases, metrics, rules and examples are YAML in a gitignored `project_config/`; an AST test fails the build if any of it leaks into source. |
| 🌐 | **Bilingual** | Persian and English questions handled natively. |
| 🧩 | **Modular retrieval** | 6 independent retrievers — swap or extend without touching the engine. |
| 🔍 | **Two-tier retrieval** | Fast alias/pattern matching first; TF-IDF bigram engine as fallback. |
| 🎯 | **Few-shot learning** | Tag-scored example selector injects the most relevant SQL patterns. |
| 📐 | **Business rule injection** | Domain rules injected per question topic at prompt-build time. |
| 🛡️ | **SQL security guard** | AST-based (sqlglot), closed table/column allowlist; blocks DDL, DML, injection; converts LIMIT→TOP. |
| 🔄 | **Auto-correct loop** | Retries with error feedback when SQL fails validation or execution — bounded, and never re-prompted for a rejection no rewrite could satisfy. |
| 💬 | **Conversational sessions** | `/v2/sessions*` — follow-up questions resolve «از بین آن‌ها» against the previous turn via CTE composition, with every assumption declared. |
| 🗂️ | **Many conversations, kept** | A conversation index that survives a restart: sessions, turns and titles persist for `session_retention_days`. Result **rows never touch the disk** — a stored row could not be re-checked against an ACL that changed after it was written. |
| 📌 | **Cross-session memory** | Standing preferences the analyst *pins* — never inferred from repetition. A closed, config-declared set, surfaced as an editable assumption chip and re-checked against the column ACL on every turn that would apply it. |
| 🔑 | **Authentication & column ACL** | API keys on every route but `/health`; per-principal `denied_columns` enforced in the guard, not just partitioned in the cache. |
| 🧑‍💼 | **Admin panel** | Twelve sections. Read-only diagnostics — audit summary, deployment checks, schema drift (including a table that is in a different data source than `schema.yaml` says), vocabulary freshness, per-analyst usage, failed auth — alongside the narrow writes: maintenance mode, feedback triage, access-request triage, cache control, and key issuance / disable / revoke / column ACLs / role grants. Two admin roles split on one rule: anything that changes *who can see what data* is the security admin's. |
| 📝 | **Plain-language summary** | Opt-in per question, as a toggle each analyst sets for themselves — producing one sends up to twenty result rows to the model, and the governance gate still refuses a *remote* backend without `LLM_ALLOW_REMOTE`. |
| 🖥️ | **Analyst web UI** | Static, no build step. Conversation sidebar, generated SQL in a fixed house layout with highlighting, result table, chart, assumption chips, Excel export — and each analyst's own key in their own browser, never one shared key baked into the page. |
| 🗄️ | **Multi-dialect** | Generates T-SQL, transpiles, then re-validates in the dialect that will execute. T-SQL and SQLite verified by execution. |
| 🏛️ | **Several warehouses** | `datasources.yaml` describes each database or server; every statement runs on the one source that has all its tables (a table may live in several), and each question is routed to one source so the model sees only that source's schema. Per-source `WITH (NOLOCK)` where a DBA requires it. See [`docs/design/DATASOURCES.md`](docs/design/DATASOURCES.md). |
| ⚡ | **FastAPI HTTP API** | REST endpoints for query, sessions, cache, and health check. |
| 💾 | **LRU query cache** | Thread-safe TTL + LRU cache, partitioned by visibility scope so two principals never share a result they should not. |
| 📊 | **Evaluation harness** | A golden set built from your own audit log and reviewed in Excel, execution accuracy against a reference SQL run live on the same data, error taxonomy, latency percentiles, determinism measurement, and a baseline regression gate to run before an upgrade (`docs/deployment-runbook.md` §18). |
| 🔬 | **LLM observability** | 21-field status block per request: tokens, prefix-cache hit, timings, corrections, `finish_reason` read from the response. |
| 📤 | **Structured exports** | Excel, CSV, JSON with timestamped filenames. |
| 📋 | **Audit trail** | Compliance-grade JSONL records with principal, guard verdict and timings — and never result rows. |
| 🧪 | **Test suite** | 5,711 unit + integration tests at 94% coverage, gated at 90%; GitHub Actions CI on Ubuntu, Windows and macOS across Python 3.11–3.13, plus doctests and an offline evaluation gate. |

---

## Quick start

**Requires:** Python 3.11+, an OpenAI-compatible endpoint (vLLM / LM Studio / Ollama `/v1`) reachable via `OPENAI_BASE_URL`, SQL Server + an ODBC driver (17 or 18) and a read-only login ([`docs/db-hardening.md`](docs/db-hardening.md))

The steps below are the short form. The full, ordered first install, with the
command and a "done when" check for every step, is "First install, in order" at
the top of [`docs/deployment-runbook.md`](docs/deployment-runbook.md) (Persian:
[`docs/fa/getting-started.md`](docs/fa/getting-started.md)).

```bash
# 1. Clone and install
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent
pip install -r requirements.lock   # exact, audited pins — see requirements.txt's own
                                    # header and docs/deployment-runbook.md for why this
                                    # is preferred over `pip install -r requirements.txt`
                                    # (floors only) for anything beyond quick local hacking

# 2. Configure
cp .env.example .env
# Set at minimum:
#   DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/DB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
#   DB_PASSWORD=<the raw password -- no URL encoding>
#   OPENAI_BASE_URL=http://your-llm-host:8000/v1
#   OPENAI_MODEL=gpt-oss-20:F16
#   OPENAI_API_KEY=your-key   # may stay empty for a local endpoint that checks none
# Querying more than one database? Describe each source in
# project_config/datasources.yaml (host, database, login; one DB_PASSWORD_*
# variable each) instead of setting DB_CONNECTION_URL, then follow
# docs/deployment-runbook.md §16: it covers `python scripts/assign_datasources.py`
# (writes each table's `datasource:`), `keywords:` for routing,
# `python scripts/prompt_budget.py` (sizes PROMPT_RETRIEVAL_TOKEN_BUDGET) and
# `nolock` (per source). docs/design/DATASOURCES.md explains why it is shaped this way.

# 3. Provide the domain config — the server will NOT start without it
cp -r project_config.example project_config
# Then replace the placeholders with your own schema, aliases, metrics, business
# rules, examples and system_prompt.md (the LLM's system instructions -- the
# only non-YAML file in the directory). project_config/ is gitignored on
# purpose: it is your data, not the engine's. There is deliberately no
# silent fallback to the example files.
# Two optional helpers draft files from the live database (see
# docs/deployment-runbook.md §2.1-§2.3):
#   python -m database.schema_inspector_cli --output-dir project_config_draft
#       drafts schema.yaml (the SQL guard's allowlist) for you to review;
#   python setup_project.py
#       an LLM-assisted wizard that drafts entities.yaml, aliases.yaml,
#       business_rules.yaml and examples.yaml (it overwrites those files, and
#       does not write schema.yaml or set `datasource:`).

# 4. Issue an API key (every route but /health requires one)
python -m scripts.issue_api_key --id analyst-1 --name "Jane Analyst"
# ...and one for yourself, with every admin capability:
python -m scripts.issue_api_key --id admin-1 --name "Admin" --full-admin
# More than one key? Keep the array in project_config/api_keys.json (start from
# project_config.example/api_keys.example.json) and set API_KEYS_FILE; see
# docs/deployment-runbook.md §2.

# 5. Run the preflight (database, read-only login, keys, model, config; once per
#    data source) — it must end with `0 failed`. Every line, and the fix for a
#    [FAIL], is in docs/deployment-runbook.md §3.1.
python -m scripts.verify_deployment
#    Then size the prompt budget and put the line it prints in .env:
python scripts/prompt_budget.py

# 6a. CLI
python app.py

# 6b. HTTP API (--no-server-header: uvicorn adds `Server: uvicorn` at the
#     protocol layer, which the app's middleware cannot strip; drop it here)
uvicorn api.server:app --host 0.0.0.0 --port 8000 --no-server-header
# ...or, once API_HOST/API_PORT are set in .env, the equivalent launcher:
python -m api
```

**→ Step-by-step guide to running the CLI and the web UI:**  
**[راهنمای راه‌اندازی — فارسی](docs/fa/getting-started.md)**

**→ Full tutorial (installation · first query · extending the domain · writing tests · diagnosing misses):**  
**[English](docs/en/tutorial.md) · [فارسی](docs/fa/tutorial.md)**

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (vLLM / LM Studio / Ollama `/v1`) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name served by the endpoint |
| `OPENAI_API_KEY` | *(required)* | API key for the endpoint |
| `API_HOST` | `127.0.0.1` | Interface `python -m api` binds to (loopback until widened on purpose) |
| `API_PORT` | `8000` | Port `python -m api` binds to |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:8080`, `http://127.0.0.1:8080` | Comma-separated browser origins allowed to call this API cross-origin — set this to the UI's own origin whenever the API and the static UI are on different ports/hosts, or every call looks like a dead backend instead of a CORS rejection (see `docs/deployment-runbook.md`) |
| `DB_CONNECTION_URL` | *(required)* | SQLAlchemy connection string — the one warehouse connection, unless `project_config/datasources.yaml` describes the connections (see [`docs/design/DATASOURCES.md`](docs/design/DATASOURCES.md)), in which case it is unused. Leave the password out of it and set `DB_PASSWORD`; a password written inside the URL must be percent-encoded (`@` → `%40`) |
| `DB_PASSWORD` | *(empty)* | The **raw** password for `DB_CONNECTION_URL`, with no URL encoding; applied to the parsed URL. Setting it as well as a password inside the URL is refused. With `datasources.yaml`, each source names its own `password_env` variable instead (e.g. `DB_PASSWORD_SALES`) |
| `QUERY_TIMEOUT_SECONDS` | `60` | Max query execution time (seconds) |
| `MAX_ROWS_RETURNED` | `1000` | Hard row cap applied to all queries |
| `CACHE_TTL_SECONDS` | `300` | Query cache TTL in seconds (`0` = disabled) |
| `CACHE_MAX_SIZE` | `256` | Maximum number of cached query results |
| `LLM_NUM_PREDICT` | `512` | Max tokens the model may generate (`max_tokens`). Too low for a **reasoning** model, which spends this budget thinking before it answers — see `.env.example` |
| `LLM_EXTRA_BODY` | *(empty)* | JSON object merged into every chat-completions request. How you turn a model's reasoning off, since that is not in the OpenAI schema and every server spells it differently |
| `LLM_PREFIX_WARMUP_ON_STARTUP` | `true` | Send the model server each data source's static prompt prefix once at start-up (background thread, `max_tokens=1`) so the first question does not pay the full prefill. Inert unless the static-prefix path is used; needs prefix caching on the model server (`vllm serve --enable-prefix-caching`). `POST /admin/llm/warmup` does it on demand. Runbook §20 |
| `LLM_PREFIX_WARMUP_TIMEOUT_SECONDS` | `180` | Total time budget of one warm-up pass |
| `LLM_STREAM_TIMINGS` | `false` | Stream the model call and reassemble the same response, to record `ttft_ms` (queue + prefill) and `generation_ms` in the audit `llm` block; `reasoning_tokens` is recorded either way. Runbook §20.4 |
| `PROMPT_RETRIEVAL_TOKEN_BUDGET` | `6000` | Estimate (`len(text) // 4`, which undercounts Persian by about 15%) up to which the whole schema goes into the prompt as one cacheable, byte-identical prefix; above it the prompt is built per question from retrieved tables. With several data sources it applies to each source's own prefix, not their sum. `python scripts/prompt_budget.py` measures real tokens and prints the value to set |
| `LOG_DIR` | `logs` | Log file directory (auto-created) |
| `EXPORT_DIR` | `exports` | Export file directory (auto-created) |
| `API_KEYS_JSON` | *(empty)* | JSON array of `{"id","name","key_sha256","denied_columns"?,"admin"?,"operations"?,"security"?}` — see [Authentication](#authentication-phase-8) |
| `API_KEYS_FILE` | *(empty)* | Path to a file holding the same JSON array as `API_KEYS_JSON` (any formatting; recommended: `project_config/api_keys.json`, git-ignored). Relative paths resolve against the repository root. Read once at start-up — restart after editing. Set this **or** `API_KEYS_JSON`, not both |
| `AUTH_REQUIRED` | `true` | Fail-closed auth gate; `false` is a logged escape hatch |
| `APP_DOCS_PUBLIC` | `false` | Serve `/docs` `/redoc` `/openapi.json` without credentials |
| `PROJECT_CONFIG_DIR` | `project_config` | Where the domain YAML lives. No silent fallback to the example directory |
| `SQL_DIALECT` | `tsql` | Target dialect. `tsql` and `sqlite` are verified by execution; others transpile and re-validate but are unverified |
| `SESSION_TTL_SECONDS` | `1800` | Idle expiry for a conversational session |
| `SESSION_MAX_TURNS` | `50` | Transcript cap per session |
| `SESSION_PROMPT_TURNS` | `3` | How many prior turns enter the prompt |
| `SESSION_STORE_PATH` | `logs/sessions.db` | SQLite file for session + memory persistence; empty disables it |
| `SESSION_RETENTION_DAYS` | `30` | How long a conversation stays listable and reopenable |
| `MEMORY_ENABLED` | `true` | Cross-session standing preferences |

Full list in `config.py` — every setting carries a docstring explaining
what it does and why its default is what it is.

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/query` | Run a natural-language query; returns SQL + result set |
| `POST` | `/query/stream` | The same, streamed as Server-Sent Events |
| `POST` | `/v2/sessions` | Start a conversation |
| `GET` | `/v2/sessions/{sid}` | Its transcript |
| `POST` | `/v2/sessions/{sid}/turns` | Ask, in context; add `?stream=1` for SSE |
| `PATCH` | `/v2/sessions/{sid}/turns/{tid}/assumptions` | Re-run under edited assumptions — returns a *new* turn, never mutates the old one |
| `DELETE` | `/v2/sessions/{sid}` | Drop a conversation and free its state |
| `POST` / `GET` | `/v2/sessions/{sid}/turns/{tid}/feedback` | Flag an answer as wrong, or read the flags raised for it |
| `POST` | `/v2/sessions/{sid}/turns/{tid}/access-request` | Ask for access to the column a guard rejection denied (`GET /v2/access-requests` lists the caller's own) |
| `GET` | `/v2/sessions` | The caller's conversation index |
| `PATCH` | `/v2/sessions/{sid}` | Rename a conversation |
| `GET` | `/v2/memory` | Standing preferences, and which fields may be remembered |
| `PUT` | `/v2/memory/{key}` | Pin one preference |
| `DELETE` | `/v2/memory/{key}` | Forget one |
| `DELETE` | `/v2/memory` | Forget all |
| `GET` | `/health` | LLM endpoint reachability, and a `SELECT 1` on every data source |
| `GET` | `/cache/stats` | Cache size, hits, misses, evictions |
| `POST` | `/cache/invalidate` | Remove a specific cached entry |
| `POST` | `/cache/clear` | Flush the entire cache |
| | `/admin/*` | The admin panel's API (`docs/admin-panel-architecture.md`); every route declares the capability it needs |

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

These are the common ones; `api/errors.py` has the full hierarchy, and a
statement the guard refuses carries a `reason` (`docs/api-contract-v2.md` §4).

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
├── setup_project.py          # optional LLM-assisted wizard that drafts entities/aliases/rules/examples (docs/deployment-runbook.md §2.2)
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
│   ├── session_policy.yaml   #   the default scope assumption
│   ├── memory_policy.yaml    #   the closed set of pinnable preferences
│   ├── relationships.yaml    #   optional join paths for database.relationship_map (not imported by the server today)
│   ├── datasources.yaml      #   optional: several warehouse connections (template in project_config.example/)
│   ├── api_keys.json         #   optional: the API_KEYS_FILE array (template in project_config.example/)
│   └── system_prompt.md      #   the LLM's system instructions (not YAML)
├── project_config.example/   # Same structure, placeholder data — what CI runs against
├── appdb/                    # Application database: API keys, role grants, config versions, feedback
├── core/                     # Shared models, the Persian normaliser, strict YAML loading, the start-up notice
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
│   ├── source_selector.py    #   which data source a question is about (several sources)
│   └── example_retriever.py  #   tag-scored few-shot selection
├── schema_data/              # Schema registry, populated from schema.yaml
│   ├── registry.py           #   SchemaRegistry + LRU cache
│   ├── drift.py              #   schema.yaml vs the live catalogues, incl. tables in the wrong data source
│   ├── columns.py            #   column allowlist (derived, not authored)
│   ├── relationships.py      #   FK → JOIN SQL map
│   └── retriever.py          #   TF-IDF bigram fallback engine
├── prompt_engine/
│   ├── builder.py            #   PromptBuilder.build()
│   ├── static_prefix.py      #   the byte-identical, KV-cacheable prefix (one per data source)
│   ├── source_scope.py       #   narrow a prompt's examples to one data source
│   └── templates.py          #   PROMPT_TEMPLATE
├── llm/
│   ├── sql_agent.py          #   generate → clean → auto-correct loop
│   ├── router.py             #   task → endpoint routing, fallback
│   ├── source_routing.py     #   per-question data source + the single OUT_OF_SCOPE retry
│   ├── providers.py          #   OpenAI-compatible provider (retries + back-off)
│   └── base.py               #   LLMBackend ABC
├── security/
│   ├── sql_guard.py          #   clean_sql / validate_sql / ensure_top / transpile / pretty_sql
│   ├── sql_format.py         #   the house layout of the SQL shown to an analyst (display only)
│   ├── dialects.py           #   per-dialect profiles (catalogues, timeouts, quoting, table hints)
│   └── auth.py               #   Principal, API-key resolution, cache scope key
├── observability/
│   ├── audit.py              #   compliance-grade records — never result rows
│   ├── llm_status.py         #   the 21-field per-request status block
│   └── timing.py             #   per-stage timings
├── eval/                     # Evaluation harness (python -m eval.cli run | verify)
│   ├── cli.py                #   run, verify and baseline commands
│   ├── runner.py             #   golden set → CaseResult
│   ├── compare.py            #   execution accuracy against the reference SQL's live result
│   ├── verify.py             #   run reviewed cases read-only and activate the ones that hold up
│   ├── store.py              #   read and atomically rewrite golden-set files
│   ├── models.py             #   GoldenCase and result records
│   ├── report.py             #   accuracy, error taxonomy, latency percentiles
│   ├── fingerprint.py        #   order-insensitive result hash
│   ├── determinism.py        #   repeat-and-compare against a live endpoint
│   └── baseline.py           #   regression gate with a CI exit code
├── database/
│   ├── connection.py         #   cached SQLAlchemy engine per data source
│   ├── datasources.py        #   datasources.yaml — named sources, DB_CONNECTION_URL fallback
│   ├── routing.py            #   which data source a query's tables belong to
│   ├── catalogue.py          #   read-only INFORMATION_SCHEMA table/column lists
│   ├── schema_inspector.py   #   schema discovery behind the drafting tools
│   ├── schema_inspector_cli.py #  python -m database.schema_inspector_cli: drafts schema.yaml into project_config_draft/
│   ├── table_hints.py        #   WITH (NOLOCK) after each table, for sources with nolock: true
│   └── executor.py           #   timeout + row cap + always-rolled-back transaction
├── web/                      # Static Persian/RTL client (no build step)
├── webapp/                   # Flask web application (bilingual FA/EN)
├── exporters/                # Excel / CSV / JSON exporters
├── scripts/
│   ├── verify_deployment.py  #   the preflight: 13 checks (database, read-only login, keys, model, config), once per data source
│   ├── issue_api_key.py      #   mint a new API key
│   ├── assign_datasources.py #   write each schema.yaml table's datasource: from the databases
│   ├── prompt_budget.py      #   each source's prompt size in real tokens; the PROMPT_RETRIEVAL_TOKEN_BUDGET to set
│   ├── migrate_app_db.py     #   move the application database between backends
│   ├── analyze_audit_log.py  #   aggregate-safe audit analysis
│   ├── analyze_misses.py     #   offline retrieval miss diagnostics
│   ├── harvest_golden.py     #   candidate golden cases from the audit log (counts only on screen)
│   ├── golden_sheet.py       #   export/import the golden-case review spreadsheet (CSV for Excel)
│   ├── create_db.py          #   build a small sample SQLite database for local trials
│   ├── dev_v2_demo_server.py #   the real API on in-memory SQLite and a stub model, for UI demos
│   └── release_notes.py      #   version, summary and notes for the release workflow
├── docs/
│   ├── api-contract-v2.md    #   the frozen conversational-session contract
│   ├── admin-panel-architecture.md  # design of the admin panel
│   ├── deployment-runbook.md #   first install in order, preflight reference (§3.1), several data sources (§16), upgrading 6.0 to 6.7 (§17), accuracy gate (§18), sharing diagnostics safely (§19)
│   ├── db-hardening.md       #   server-side hardening for the DBA
│   ├── dba/                  #   read-only diagnostic kit for the DBA
│   ├── design/               #   decision records: DATASOURCES.md, TABLE-NAMES.md, UI design
│   ├── en/tutorial.md        #   full English tutorial
│   ├── fa/getting-started.md #   Persian setup guide — راهنمای راه‌اندازی
│   └── fa/tutorial.md        #   full Persian tutorial — آموزش کامل فارسی
└── tests/                    # 5,711 unit + integration tests
```

---

## Tests

```bash
pytest tests/ -v                        # all tests
pytest tests/test_sql_guard.py -v       # one module
pytest tests/ eval/tests --cov          # exactly what CI measures
```

**5,711 tests at 94% branch coverage**, with the build failing below 90%
(`fail_under` in [`setup.cfg`](setup.cfg)). What that number does *not*
cover is stated in the same file rather than left to be discovered: the
interactive wizards and CLI front-ends are excluded by policy — their
value is in being run by a human — and `database/schema_inspector.py` and
`relationship_map.py` are excluded as a declared ratchet, with the reason
and the condition for their return written next to the exclusion.

CI runs on every pull request to `main` and every push to it, via GitHub
Actions on Ubuntu, Windows and macOS across Python 3.11, 3.12 and 3.13, each
combination once on the newest releases `requirements.txt` allows and once on
the exact pins of `requirements.lock`, with doctests, coverage, a dependency
audit of `requirements.lock` and an offline evaluation gate. It runs
with `PROJECT_CONFIG_DIR=project_config.example` and no `project_config/`
present, so the suite never depends on real domain data.

---

## Releasing

A release is a `chore/release-X.Y.Z` pull request that changes only
`CHANGELOG.md` (a `## [X.Y.Z] — YYYY-MM-DD` section) and `core/version.py`
(`__version__ = "X.Y.Z"`), with the commit subject
`chore(release): X.Y.Z — <summary>`.

**Merging that pull request publishes the release.**
[`.github/workflows/release.yml`](.github/workflows/release.yml) runs when
`core/version.py` changes on `main` and, unless `vX.Y.Z` already exists:

- pushes the annotated tag `vX.Y.Z` on the merge commit, with the message
  `X.Y.Z — <summary>`;
- publishes a GitHub Release titled `X.Y.Z — <summary>`, whose body is that
  version's `CHANGELOG.md` section without its heading line.

The `<summary>` is taken from the release commit's subject, so it is written
once, in the pull request. The run fails, before pushing anything, if that
commit or the changelog section is missing or empty. A repeated run only
does what is still missing.

**A release that was merged but never published** (the workflow did not
exist yet, or a run failed): open *Actions → Release → Run workflow* and give
it the `version` (`6.1.0`, no leading `v`) and the `ref` to tag, which is the
merge commit of the release pull request. The run refuses to continue unless
`core/version.py` at that `ref` declares that version, or if the tag already
exists on a different commit. The same from a terminal:

```bash
gh workflow run release.yml -f version=6.1.0 -f ref=<merge-commit-sha>
```

The logic that reads the version, summary and notes is in
[`scripts/release_notes.py`](scripts/release_notes.py) and is tested by
`tests/test_release_notes.py`.

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
- **Table allowlist, strictly enforced:** every table reference must resolve to the allowlist derived from your `project_config/schema.yaml` (case-insensitively, brackets ignored) or be a CTE defined earlier in the same query — an unresolvable table (hallucinated, out-of-domain, or malicious) is refused outright, independent of whether the DB login is itself scoped to just these tables (see `docs/db-hardening.md`). This is why `schema.yaml` is a security file: adding a table widens what generated SQL may touch, and a typo silently narrows the allowlist. A table's schema/db qualifier is checked too, not ignored: a `schema.yaml` key may itself be qualified (`sales.Customer`) for a warehouse with the same table name in more than one schema, and a query that writes some *other* schema in front of an allowlisted table's bare name is refused (`unknown_table`) rather than silently resolved — see `docs/design/TABLE-NAMES.md`
- **One data source per statement:** with several data sources the guard works out which source has every table the statement reads (`database.routing.choose_datasource`, the same function the executor uses) and refuses the statement as `cross_datasource` when none does; the source is derived from the tables, never taken from the model — see `docs/design/DATASOURCES.md`
- **Column allowlist, deliberately lenient:** every resolvable qualified column reference is checked against its table's known columns; an unqualified column, or one qualified by a CTE name or derived-table alias, is allowed rather than risk a false-positive rejection — this leniency applies to *columns* only, not table names
- **Column-level ACL seam:** `validate_sql(sql, denied_columns=...)` refuses any query touching a named column, regardless of table (an entry written `schema.Table.Col`, `Source:schema.Table.Col` or `Source:Col` instead makes that column join-only: allowed only as a `JOIN ... ON` equality key, refused as `join_only_column` anywhere else) — the foundation for future multi-tenant column policies; `*`/`alias.*` cannot be used to read around an active policy (it is expanded against its resolved table(s) and checked, or refused outright if it can't be resolved with confidence)
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
- **Never store raw keys:** `API_KEYS_JSON` (or the file `API_KEYS_FILE`
  names — recommended for more than one key, since a multi-line value in
  `.env` must be wrapped in single quotes and cannot contain an apostrophe)
  holds only each key's SHA-256 hex digest (`security/auth.py`). Issue a new key with `python -m
  scripts.issue_api_key --id <id> --name <name>` — it prints the raw key
  **once**, never to a file or log. Add `--admin`, `--operations`,
  `--security`, or `--full-admin` for all three, to grant the admin
  capabilities `docs/admin-panel-architecture.md` §2 defines.
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
  enforcement machinery, just the first thing that populates it. A plain name
  denies the column everywhere. A scoped entry (`schema.Table.Col`,
  `Source:schema.Table.Col`, `Source:Col`) makes it *join-only*: usable as a
  `JOIN ... ON a.col = b.col` key and nowhere else. Join-only hides a value,
  not its existence (a join on a filtered foreign key can still probe it), so
  restrict the foreign keys too; see `docs/deployment-runbook.md`.
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
| **Database** | `database/` — one cached SQLAlchemy engine per data source (`datasources.yaml`, `DB_CONNECTION_URL` when absent), tables routed to their source automatically (a table may live in several), query timeout, hard row cap, always-rolled-back transaction |
| **FastAPI service** | `api/` — `/query`, `/v2/sessions*`, `/health`, `/cache`; auth middleware; correlation IDs; LRU + TTL `QueryCache`; typed `NLQError` hierarchy |
| **Static web client** | `web/` — Persian/RTL, no build step: pipeline view, assumption chips, result-shape selection, charts |
| **Exports & logging** | `exporters/`, `logs/` — Excel/CSV/JSON exporters; rotating JSONL logger |
| **Test suite** | `tests/` — 5,711 unit and integration tests at 94% coverage; GitHub Actions CI on three operating systems across Python 3.11–3.13 |

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
