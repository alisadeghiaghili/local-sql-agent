# Local SQL Agent — Technical Documentation

> **Project Name:** Local SQL Agent (Auction NLQ Engine)
>
> **Purpose:** A fully local, on-premise Natural Language Query (NLQ) engine that converts Persian or English questions into Microsoft SQL Server queries, executes them, and returns results — without sending any data to external services.
>
> **Target Users:** Data analysts at the Iran Mercantile Exchange (IME) who need to query the auction database using plain Persian or English language instead of writing SQL manually.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [System Architecture](#2-system-architecture)
3. [End-to-End Workflow — Step by Step](#3-end-to-end-workflow--step-by-step)
4. [Module Breakdown](#4-module-breakdown)
5. [API Endpoints](#5-api-endpoints)
6. [Security Model](#6-security-model)
7. [Configuration](#7-configuration)
8. [How to Run](#8-how-to-run)
9. [Testing](#9-testing)

---

## 1. Project Overview

### 1.1 What It Does — One-Line Summary

```
  Persian/English Question  ──►  SQL Query  ──►  Result Table
        (NLQ)                    (T-SQL)         (DataFrame)
```

### 1.2 The Problem We Solve

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│   BEFORE (Manual Process):                                          │
│                                                                     │
│   Analyst thinks question ──► Analyst writes SQL ──► Runs query     │
│   in Persian                (complex, error-prone)    manually      │
│                                                                     │
│   Problems:                                                         │
│   ✗ Analysts don't know SQL                                        │
│   ✗ SQL requires English syntax                                    │
│   ✗ Complex JOINs across 5+ tables                                 │
│   ✗ Time-consuming, error-prone                                    │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│   AFTER (With Local SQL Agent):                                     │
│                                                                     │
│   Analyst thinks question ──► System generates SQL ──► Returns data │
│   in Persian                automatically            + Excel       │
│                                                                     │
│   Benefits:                                                         │
│   ✓ Zero SQL knowledge required                                    │
│   ✓ Persian language natively supported                            │
│   ✓ Correct JOINs generated automatically                          │
│   ✓ Results in seconds, exported to Excel                          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.3 Why It Must Be Local

```
┌──────────────────────────────────────────────────────────────────┐
│                    SECURITY CONSTRAINTS                          │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     │
│  │  SENSITIVE   │     │   NO CLOUD   │     │   NO EXTERNAL│     │
│  │  FINANCIAL   │     │   PERMISSIONS│     │   API BUDGET │     │
│  │  DATA        │     │              │     │              │     │
│  └──────┬───────┘     └──────┬───────┘     └──────┬───────┘     │
│         │                    │                    │              │
│         └────────────────────┼────────────────────┘              │
│                              │                                   │
│                              ▼                                   │
│                    ┌──────────────────┐                          │
│                    │  MUST RUN ON-    │                          │
│                    │  PREMISE ONLY    │                          │
│                    │                  │                          │
│                    │  OpenAI-compat. │                          │
│                    │  LLM + SQL      │                          │
│                    │  Server on      │                          │
│                    │  company hardware│                          │
│                    └──────────────────┘                          │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 1.4 Key Numbers

```
  ┌─────────────────────────────────────────────────────────────┐
  │                     PROJECT AT A GLANCE                      │
  ├──────────────────┬──────────────────┬───────────────────────┤
  │  LANGUAGES       │  MODELS          │  RETRIEVAL            │
  │  ─────────       │  ──────          │  ─────────            │
  │  Persian +       │  Any Model on   │  6 Independent        │
  │  English         │  the OpenAI-    │  Modules              │
  │                  │  compatible     │                       │
  │                  │  endpoint       │                       │
  │                  │  (gpt-oss, …)   │                       │
  ├──────────────────┼──────────────────┼───────────────────────┤
  │  TESTS           │  DEPLOYMENT      │  CLOUD                │
  │  ──────          │  ──────────      │  ─────                │
  │  5,000+          │  On-Premise      │  OpenAI-compatible   │
  │  Unit +          │  Only            │  LLM Endpoint        │
  │  Integration     │                  │                       │
  └──────────────────┴──────────────────┴───────────────────────┘
```

### 1.5 Database Schema Overview

The system operates on a **Star Schema** data warehouse (`sales`/`ref` in
the shipped example config):

```
                              ┌──────────────┐
                              │    sales     │
                              │    Date      │
                              └──────┬───────┘
                                     │
  ┌──────────────┐   ┌───────────────┼───────────────┐   ┌──────────────┐
  │    sales     │   │               │               │   │     ref      │
  │  Customer    ├───┤               │               ├───┤  Ring        │
  └──────────────┘   │               │               │   └──────────────┘
                     │               │               │
  ┌──────────────┐   │               │               │   ┌──────────────┐
  │     ref      │   │               │               │   │     ref      │
  │  Broker      ├───┤               │               ├───┤  Symbol      │
  └──────────────┘   │               │               │   └──────────────┘
                     │    ┌──────────┴──────────┐    │
                     │    │        sales         │    │
                     │    │   ┌─────────────┐   │    │
                     └────┴───┤   Order     ├───┴────┘
                          │   └─────────────┘   │
                          └─────────────────────┘

  sales = Fact + core dimension tables (Order, Customer, Date, OrderStatus)
  ref   = Shared reference/dimension tables (Broker, Currency, Location, Ring, Symbol, Supplier)
```

---

## 2. System Architecture

The system is built as a **modular pipeline**. Each step is a separate module that can be tested, replaced, or extended independently.

### 2.1 High-Level Architecture — Layered View

```
╔═══════════════════════════════════════════════════════════════════════╗
║                                                                       ║
║  LAYER 1: USER INTERFACE                                              ║
║  ┌─────────────────────────────┐  ┌──────────────────────────────┐   ║
║  │                             │  │                              │   ║
║  │    CLI (Terminal REPL)      │  │    FastAPI HTTP API           │   ║
║  │                             │  │                              │   ║
║  │    app.py                   │  │    api/server.py             │   ║
║  │                             │  │                              │   ║
║  │    Interactive terminal     │  │    REST endpoints:           │   ║
║  │    for single-user use      │  │    POST /query               │   ║
║  │                             │  │    GET  /health              │   ║
║  └──────────────┬──────────────┘  │    GET  /cache/stats         │   ║
║                 │                  └──────────────┬───────────────┘   ║
║                 │                                 │                   ║
╠═════════════════╪═════════════════════════════════╪═══════════════════╣
║                 ▼                                 ▼                   ║
║  LAYER 2: ORCHESTRATION                                               ║
║  ┌────────────────────────────────────────────────────────────────┐   ║
║  │                                                                │   ║
║  │  ┌──────────────────┐          ┌──────────────────────────┐   │   ║
║  │  │  api/runner.py   │          │  llm/sql_agent.py        │   │   ║
║  │  │                  │          │                          │   │   ║
║  │  │  HTTP query      │          │  Core pipeline:          │   │   ║
║  │  │  orchestrator    │          │  retrieve → prompt →     │   │   ║
║  │  │  + cache mgmt    │          │  generate → validate →   │   │   ║
║  │  │                  │          │  execute → correct        │   │   ║
║  │  └────────┬─────────┘          └────────────┬─────────────┘   │   ║
║  │           │                                 │                  │   ║
║  └───────────┼─────────────────────────────────┼──────────────────┘   ║
║              │                                 │                      ║
╠══════════════╪═════════════════════════════════╪══════════════════════╣
║              ▼                                 ▼                      ║
║  LAYER 3: RETRIEVAL PIPELINE                                         ║
║  ┌────────────────────────────────────────────────────────────────┐   ║
║  │                                                                │   ║
║  │         retrieval/context_retriever.py (Orchestrator)          │   ║
║  │                                                                │   ║
║  │   ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │   ║
║  │   │ Entity       │  │ Fact         │  │ Relationship     │   │   ║
║  │   │ Retriever    │  │ Retriever    │  │ Retriever        │   │   ║
║  │   │              │  │              │  │                  │   │   ║
║  │   │ Detects      │  │ Detects      │  │ Generates        │   │   ║
║  │   │ dimension    │  │ fact tables  │  │ JOIN clauses     │   │   ║
║  │   │ tables       │  │              │  │                  │   │   ║
║  │   └──────────────┘  └──────────────┘  └──────────────────┘   │   ║
║  │   ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │   ║
║  │   │ Rule         │  │ Example      │  │ Value            │   │   ║
║  │   │ Retriever    │  │ Retriever    │  │ Retriever        │   │   ║
║  │   │              │  │              │  │                  │   │   ║
║  │   │ Injects      │  │ Selects      │  │ Extracts         │   │   ║
║  │   │ business     │  │ few-shot     │  │ filter values    │   │   ║
║  │   │ rules        │  │ examples     │  │ from question    │   │   ║
║  │   └──────────────┘  └──────────────┘  └──────────────────┘   │   ║
║  │                                                                │   ║
║  └──────────────────────────────┬─────────────────────────────────┘   ║
║                                 │                                     ║
╠═════════════════════════════════╪═════════════════════════════════════╣
║                                 ▼                                     ║
║  LAYER 4: PROMPT ASSEMBLY                                             ║
║  ┌────────────────────────────────────────────────────────────────┐   ║
║  │                                                                │   ║
║  │   prompt_engine/builder.py                                     │   ║
║  │                                                                │   ║
║  │   Static prefix + variable suffix (Step 5):                   │   ║
║  │                                                                │   ║
║  │   ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐        │   ║
║  │   │ System   │ │ Business │ │ Schema   │ │ Relations│        │   ║
║  │   │ Prompt   │ │ Rules    │ │          │ │ hips     │        │   ║
║  │   └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘        │   ║
║  │        │            │            │            │                │   ║
║  │        └────────┬───┴────────┬───┴────────┬───┘                │   ║
║  │                 │            │            │                    │   ║
║  │   ┌──────────┐ ┌──────────┐ ┌──────────┐                     │   ║
║  │   │ Filters  │ │ Examples │ │ Question │  ──► Single Prompt   │   ║
║  │   └────┬─────┘ └────┬─────┘ └────┬─────┘                     │   ║
║  │        └────────┬───┴────────┬───┘                             │   ║
║  │                 └────────┬───┘                                 │   ║
║  └──────────────────────────┼─────────────────────────────────────┘   ║
║                             │                                         ║
╠═════════════════════════════╪═════════════════════════════════════════╣
║                             ▼                                         ║
║  LAYER 5: LLM GENERATION                                             ║
║  ┌────────────────────────────────────────────────────────────────┐   ║
║  │                                                                │   ║
║  │   llm/router.py ──► OpenAI-compat. API                    │   ║
║  │                                                                │   ║
║  │   ┌──────────────────────────────────────────────────────┐    │   ║
║  │   │                                                      │    │   ║
║  │   │   Prompt ──► POST <base_url>/chat/completions         │    │   ║
║  │   │                    │                                 │    │   ║
║  │   │                    ▼                                 │    │   ║
║  │   │             ┌──────────────┐                         │    │   ║
║  │   │             │ OpenAI-compat│                         │    │   ║
║  │   │             │  Model       │  gpt-oss / vLLM /       │    │   ║
║  │   │             │  (Endpoint)  │  LM Studio / ...        │    │   ║
║  │   │             └──────┬───────┘                         │    │   ║
║  │   │                    │                                 │    │   ║
║  │   │                    ▼                                 │    │   ║
║  │   │              Raw SQL Text ◄─── (3 retries + backoff) │    │   ║
║  │   │                                                      │    │   ║
║  │   └──────────────────────────────────────────────────────┘    │   ║
║  │                                                                │   ║
║  └──────────────────────────────┬─────────────────────────────────┘   ║
║                                 │                                     ║
╠═════════════════════════════════╪═════════════════════════════════════╣
║                                 ▼                                     ║
║  LAYER 6: SECURITY VALIDATION                                         ║
║  ┌────────────────────────────────────────────────────────────────┐   ║
║  │                                                                │   ║
║  │   security/sql_guard.py                                       │   ║
║  │                                                                │   ║
║  │   Raw SQL ──► clean_sql ──► validate_sql ──► ensure_top       │   ║
║  │                               │                                │   ║
║  │                               ├── One SELECT only? ──► ERR    │   ║
║  │                               ├── Tables/columns ok? ──► ERR  │   ║
║  │                               └── ACL ok? ──► PASS            │   ║
║  │                                                                │   ║
║  └──────────────────────────────┬─────────────────────────────────┘   ║
║                                 │                                     ║
╠═════════════════════════════════╪═════════════════════════════════════╣
║                                 ▼                                     ║
║  LAYER 7: DATABASE EXECUTION                                          ║
║  ┌────────────────────────────────────────────────────────────────┐   ║
║  │                                                                │   ║
║  │   database/connection.py ──► database/executor.py              │   ║
║  │                                                                │   ║
║  │   ┌──────────────────────────────────────────────────────┐    │   ║
║  │   │  SQLAlchemy Engine (one per data source)              │    │   ║
║  │   │  pool_size=10 | max_overflow=20 | idle ping         │     │   ║
║  │   └──────────────────────┬───────────────────────────────┘    │   ║
║  │                          │                                     │   ║
║  │                          ▼                                     │   ║
║  │   ┌──────────────────────────────────────────────────────┐    │   ║
║  │   │  SQL Server (ODBC Driver 17 or 18)                   │    │   ║
║  │   │  the warehouse database                              │    │   ║
║  │   └──────────────────────┬───────────────────────────────┘    │   ║
║  │                          │                                     │   ║
║  │                          ▼                                     │   ║
║  │                    Result Set                                  │   ║
║  │                    (pandas DataFrame)                          │   ║
║  └──────────────────────────────┬─────────────────────────────────┘   ║
║                                 │                                     ║
╠═════════════════════════════════╪═════════════════════════════════════╣
║                                 ▼                                     ║
║  LAYER 8: OUTPUT                                                      ║
║  ┌────────────────────────────────────────────────────────────────┐   ║
║  │                                                                │   ║
║  │   ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │   ║
║  │   │ Excel Export │  │ JSON Response │  │ Structured Log   │   │   ║
║  │   │              │  │              │  │                  │   │   ║
║  │   │ .xlsx file   │  │ REST API     │  │ JSONL rotating   │   │   ║
║  │   │ auto-fitted  │  │ response     │  │ files            │   │   ║
║  │   │ columns      │  │              │  │                  │   │   ║
║  │   └──────────────┘  └──────────────┘  └──────────────────┘   │   ║
║  │                                                                │   ║
║  └────────────────────────────────────────────────────────────────┘   ║
║                                                                       ║
╚═══════════════════════════════════════════════════════════════════════╝
```

### 2.2 Data Flow Diagram

This diagram shows how data flows through the system from input to output:

```
  ┌───────────┐
  │  User's   │
  │  Question │
  └─────┬─────┘
        │
        ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │                    DATA FLOW PIPELINE                               │
  │                                                                     │
  │  ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐         │
  │  │         │    │         │    │         │    │         │         │
  │  │ Question│───►│Retriever│───►│ Prompt  │───►│  LLM    │         │
  │  │ (NLQ)   │    │Pipeline │    │ Builder │    │Backend  │         │
  │  │         │    │         │    │         │    │         │         │
  │  └─────────┘    └────┬────┘    └─────────┘    └────┬────┘         │
  │                      │                             │               │
  │                      ▼                             ▼               │
  │              ┌──────────────┐              ┌──────────────┐        │
  │              │ Retrieval    │              │ Raw SQL      │        │
  │              │ Context      │              │ Text         │        │
  │              │              │              │              │        │
  │              │ • entities   │              │ May include: │        │
  │              │ • facts      │              │ • Markdown   │        │
  │              │ • joins      │              │ • Prose      │        │
  │              │ • rules      │              │ • LIMIT      │        │
  │              │ • examples   │              └──────┬───────┘        │
  │              │ • filters    │                     │                │
  │              └──────────────┘                     ▼                │
  │                                            ┌──────────────┐       │
  │                                            │ SQL Guard    │       │
  │                                            │              │       │
  │                                            │ clean ──►    │       │
  │                                            │ validate ──► │       │
  │                                            │ ensure_top   │       │
  │                                            └──────┬───────┘       │
  │                                                   │               │
  │                                                   ▼               │
  │                                            ┌──────────────┐       │
  │                                            │ Valid T-SQL  │       │
  │                                            │ Query        │       │
  │                                            └──────┬───────┘       │
  │                                                   │               │
  │                                                   ▼               │
  │  ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐       │
  │  │         │    │         │    │         │    │         │       │
  │  │ Excel   │◄───│ JSON    │◄───│ DataFrame│◄───│ SQL     │       │
  │  │ Export  │    │ Response│    │ Results │    │ Execute │       │
  │  │         │    │         │    │         │    │         │       │
  │  └─────────┘    └─────────┘    └─────────┘    └─────────┘       │
  │       │                                                         │
  │       ▼                                                         │
  │  ┌─────────┐                                                     │
  │  │  User   │                                                     │
  │  │  Gets   │                                                     │
  │  │ Results │                                                     │
  │  └─────────┘                                                     │
  └─────────────────────────────────────────────────────────────────────┘
```

### 2.3 Component Interaction Map

This diagram shows which modules communicate with each other:

```
                         ┌──────────────┐
                         │   app.py     │
                         │   (CLI)      │
                         └──────┬───────┘
                                │
                                ▼
                         ┌──────────────┐
              ┌──────────│  sql_agent    │──────────┐
              │          │  .py          │          │
              │          └──────┬───────┘          │
              │                 │                   │
              ▼                 ▼                   ▼
  ┌───────────────┐  ┌──────────────┐  ┌───────────────────┐
  │  context      │  │  wizard_llm  │  │  sql_guard.py     │
  │  _retriever.py│  │  .py         │  │                   │
  └───────┬───────┘  └──────────────┘  └───────────────────┘
          │
          │  calls 6 sub-retrievers:
          │
          ├──► entity_retriever.py ──► schema_data/retriever.py (TF-IDF)
          ├──► fact_retriever.py   ──► schema_data/retriever.py (TF-IDF)
          ├──► relationship_retriever.py ──► schema_data/relationships.py
          ├──► rule_retriever.py   ──► knowledge/business_rules.py
          ├──► example_retriever.py──► knowledge/examples.py
          └──► value_retriever.py  ──► knowledge/aliases.py

                         ┌──────────────┐
              ┌──────────│  server.py   │──────────┐
              │          │  (FastAPI)   │          │
              │          └──────┬───────┘          │
              │                 │                   │
              ▼                 ▼                   ▼
  ┌───────────────┐  ┌──────────────┐  ┌───────────────────┐
  │  runner.py    │  │  middleware   │  │  query_cache.py   │
  │  (orchestr.)  │  │  .py         │  │  (LRU + TTL)      │
  └───────────────┘  └──────────────┘  └───────────────────┘
```

---

## 3. End-to-End Workflow — Step by Step

This section walks through exactly what happens from the moment a user asks a question to the moment they receive results.

### Master Flow — All 12 Steps

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │   STEP 1    STEP 2    STEP 3    STEP 4    STEP 5    STEP 6    │
  │   ┌───┐     ┌───┐     ┌───┐     ┌───┐     ┌───┐     ┌───┐    │
  │   │ Q │────►│MW │────►│ C │────►│ R │────►│ P │────►│ L │    │
  │   │   │     │   │     │   │     │   │     │   │     │   │    │
  │   └───┘     └───┘     └───┘     └───┘     └───┘     └───┘    │
  │                                                                 │
  │   STEP 7    STEP 8    STEP 9   STEP 10  STEP 11  STEP 12      │
  │   ┌───┐     ┌───┐     ┌───┐     ┌───┐     ┌───┐     ┌───┐    │
  │   │ C │────►│ V │────►│ A │────►│ D │────►│ O │────►│ L │    │
  │   │   │     │   │     │   │     │   │     │   │     │   │    │
  │   └───┘     └───┘     └───┘     └───┘     └───┘     └───┘    │
  │                                                                 │
  │   Q  = Question Input        MW = Middleware                    │
  │   C  = Cache Lookup          R  = Retrieval Pipeline           │
  │   P  = Prompt Assembly       L  = LLM Generation               │
  │   C  = SQL Cleaning          V  = SQL Validation               │
  │   A  = Auto-Correction       D  = Database Execution           │
  │   O  = Output/Export         L  = Logging                      │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

---

### Step 1: User Submits a Question

**Entry Point:** `app.py` (CLI) or `api/server.py` (HTTP API)

```
  ┌─────────────────────────────────────────────────────────┐
  │                                                         │
  │   CLI MODE:                                             │
  │                                                         │
  │   ❓ Question: برترین مشتریان از نظر ارزش خرید در 1402  │
  │                                                         │
  │   (User types Persian or English question at terminal)   │
  │                                                         │
  ├─────────────────────────────────────────────────────────┤
  │                                                         │
  │   HTTP MODE:                                            │
  │                                                         │
  │   POST http://localhost:8000/query                      │
  │   {                                                     │
  │     "question": "فروش ماهانه تالار پتروشیمی در 1402",  │
  │     "mode": "full"                                      │
  │   }                                                     │
  │                                                         │
  │   (Client sends JSON request via REST API)              │
  │                                                         │
  └─────────────────────────────────────────────────────────┘
```

Every HTTP route except `GET /health` requires `Authorization: Bearer <api-key>`.

---

### Step 2: Middleware Processing (HTTP Mode Only)

**Modules:** `api/middleware.py`, `api/auth.py`, `api/concurrency.py`

Every request passes through a stack of middleware before a route runs. From the outermost layer in (`api/server.py` documents the order and why):

| Layer | What it does |
|---|---|
| `SecurityHeadersMiddleware` | Outermost, so an error response carries the same security headers as a 200 |
| `CORSMiddleware` | Answers a browser's preflight; the allowed origins are `CORS_ALLOWED_ORIGINS` (default `http://localhost:8080` and `http://127.0.0.1:8080`) |
| `RequestIDMiddleware` | Assigns `X-Request-ID` (the same id the audit record carries) and adds `X-Response-Time` (seconds, e.g. `0.412s`) |
| `AuthMiddleware` | Resolves the API key (`Authorization: Bearer <key>`) to a principal, 401 otherwise; `GET /health` is the one open route |
| `RateLimitMiddleware` | Token bucket per (principal, IP): `RATE_LIMIT_REQUESTS` (600) per `RATE_LIMIT_WINDOW_SEC` (60) with `RATE_LIMIT_BURST` (40) extra; 429 on excess. Failed authentications have their own small bucket |
| `ConcurrencyMiddleware` | Innermost: at most `MAX_CONCURRENT_REQUESTS` (10) pipeline requests in flight (`/query`, `/query/stream`, v2 turns, the assumptions `PATCH`); 503 `SERVER_OVERLOAD` beyond that. The slot is held until the response body is fully sent |

---

### Step 3: Cache Lookup (HTTP Mode Only)

**Module:** `api/query_cache.py`

```
  Cache key = (normalised question, mode, prompt-prefix version, scope key)

  scope key = a hash of the caller's denied_columns (and of any pinned memory
              that changed the answer): two principals who may see the same
              data share entries, two who may not never do.

  mode "sql" and requests with interpret=true are not cached.
  Max size: CACHE_MAX_SIZE (256) entries     TTL: CACHE_TTL_SECONDS (300; 0 = off)
  Eviction: LRU     Thread safety: a lock around every operation
```

A hit returns the stored result immediately: no model call, no database statement, and no source routing, because a hit never routes anything (`docs/design/DATASOURCES.md`, "Result cache"). A miss continues to Step 4.

---

### Step 4: Retrieval Pipeline — Building Context

**Module:** `retrieval/context_retriever.py` + 6 sub-retrievers

This is the **most critical step**. The retrievers pick the **relevant subset** of the knowledge base for each question. Whether the prompt then shows that subset or, while the whole knowledge base fits `PROMPT_RETRIEVAL_TOKEN_BUDGET`, everything (Step 5), is decided when the prompt is built; the retrieval output also feeds the filters, the value resolution and, with several data sources, the choice of source.

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │              RETRIEVAL PIPELINE — 6 PARALLEL RETRIEVERS         │
  │                                                                 │
  │   Question: "فروش ماهانه تالار پتروشیمی در 1402"               │
  │              (monthly sales of petrochemical hall in 1402)       │
  │                                                                 │
  │   ┌──────────────────────────────────────────────────────────┐ │
  │   │                                                          │ │
  │   │  ┌─────────────────────┐                                 │ │
  │   │  │  4.1 ENTITY RETRIEVER│                                │ │
  │   │  │  ─────────────────── │                                │ │
  │   │  │                      │                                │ │
  │   │  │  Input:  question    │                                │ │
  │   │  │  Output: dim tables  │──► Ring, Customer              │ │
  │   │  │                      │                                │ │
  │   │  │  Method:             │                                │ │
  │   │  │  1. Alias match      │                                │ │
  │   │  │  2. TF-IDF fallback  │                                │ │
  │   │  └─────────────────────┘                                 │ │
  │   │                                                          │ │
  │   │  ┌─────────────────────┐                                 │ │
  │   │  │  4.2 FACT RETRIEVER  │                                │ │
  │   │  │  ──────────────────  │                                │ │
  │   │  │                      │                                │ │
  │   │  │  Input:  question    │                                │ │
  │   │  │  Output: fact tables │──► Order                       │ │
  │   │  │                      │                                │ │
  │   │  │  Method:             │                                │ │
  │   │  │  1. Alias match      │                                │ │
  │   │  │  2. TF-IDF fallback  │                                │ │
  │   │  └─────────────────────┘                                 │ │
  │   │                                                          │ │
  │   │  ┌─────────────────────┐                                 │ │
  │   │  │  4.3 RELATIONSHIP   │                                 │ │
  │   │  │      RETRIEVER      │                                 │ │
  │   │  │  ──────────────────  │                                │ │
  │   │  │                      │                                │ │
  │   │  │  Input:  [Ring,      │                                │ │
  │   │  │   Customer, Order]   │                                │ │
  │   │  │                      │                                │ │
  │   │  │  Output: JOIN clauses│                                │ │
  │   │  │                      │                                │ │
  │   │  │  From: relationships.py                               │ │
  │   │  └─────────────────────┘                                 │ │
  │   │                                                          │ │
  │   │  ┌─────────────────────┐                                 │ │
  │   │  │  4.4 RULE RETRIEVER  │                                │ │
  │   │  │  ──────────────────  │                                │ │
  │   │  │                      │                                │ │
  │   │  │  Input:  question    │                                │ │
  │   │  │  Output: biz rules   │──► "Ring names use full form"  │ │
  │   │  │                      │    "Persian year = Farvardin"  │ │
  │   │  │  From: business_rules.py                              │ │
  │   │  └─────────────────────┘                                 │ │
  │   │                                                          │ │
  │   │  ┌─────────────────────┐                                 │ │
  │   │  │  4.5 EXAMPLE        │                                 │ │
  │   │  │      RETRIEVER      │                                 │ │
  │   │  │  ──────────────────  │                                │ │
  │   │  │                      │                                │ │
  │   │  │  Input:  question    │                                │ │
  │   │  │  Output: 2-3 SQL     │──► Similar NLQ→SQL pairs       │ │
  │   │  │          examples    │    ranked by tag overlap       │ │
  │   │  │                      │                                │ │
  │   │  │  From: examples.py   │                                │ │
  │   │  │  (22+ annotated      │                                │ │
  │   │  │   NLQ→SQL pairs)     │                                │ │
  │   │  └─────────────────────┘                                 │ │
  │   │                                                          │ │
  │   │  ┌─────────────────────┐                                 │ │
  │   │  │  4.6 VALUE RETRIEVER │                                │ │
  │   │  │  ──────────────────  │                                │ │
  │   │  │                      │                                │ │
  │   │  │  Input:  question    │                                │ │
  │   │  │  Output: filters     │──► Ring: تالار پتروشیمی       │ │
  │   │  │                      │    PersianYear: 1402           │ │
  │   │  │                      │                                │ │
  │   │  │  From: aliases.py    │                                │ │
  │   │  └─────────────────────┘                                 │ │
  │   │                                                          │ │
  │   └──────────────────────────────────────────────────────────┘ │
  │                                                                 │
  │   ALL 6 RESULTS COMBINED INTO:                                 │
  │                                                                 │
  │   ┌──────────────────────────────────────────────────────────┐ │
  │   │  RetrievalContext (frozen dataclass)                     │ │
  │   │                                                          │ │
  │   │  .entities      = ["Ring", "Customer"]                   │ │
  │   │  .facts         = ["Order"]                              │ │
  │   │  .relationships = ["JOIN ... ON ...", "JOIN ... ON ..."] │ │
  │   │  .business_rules= ["Ring names...", "Persian year..."]   │ │
  │   │  .examples      = [{question, sql, tags}, ...]           │ │
  │   │  .filters       = {Ring: "تالار پتروشیمی", Year: 1402} │ │
  │   └──────────────────────────────────────────────────────────┘ │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

#### Two-Tier Retrieval Strategy

Each sub-retriever uses a **two-tier matching strategy**:

```
  Question Text
       │
       ▼
  ┌──────────────────────────────────────────────────────────┐
  │                                                          │
  │   TIER 1: Fast Alias/Pattern Match                       │
  │   ─────────────────────────────────                      │
  │                                                          │
  │   ┌────────────────────────────────────────────────┐    │
  │   │  Check if question contains known aliases:      │    │
  │   │                                                 │    │
  │   │  "پتروشیمی" ──► Ring                           │    │
  │   │  "خرید"     ──► Order                            │    │
  │   │  "سال 1402" ──► Date (PersianYear=1402)        │    │
  │   └────────────────────────────────────────────────┘    │
  │                                                          │
  │   Speed: ~1ms  |  Accuracy: High for known terms         │
  │                                                          │
  │   ┌─────────┐                                            │
  │   │ Match?  │                                            │
  │   └────┬────┘                                            │
  │        │                                                  │
  │    YES │    NO                                            │
  │    ────┤    ────                                          │
  │    │   │    │                                             │
  │    ▼   │    ▼                                             │
  │   Done │   ┌─────────────────────────────────────────┐   │
  │         │   │                                         │   │
  │         │   │  TIER 2: TF-IDF Bigram Scoring         │   │
  │         │   │  ───────────────────────────────        │   │
  │         │   │                                         │   │
  │         │   │  Score each table by:                   │   │
  │         │   │  • Term frequency in description        │   │
  │         │   │  • IDF weighting (rare terms = higher)  │   │
  │         │   │  • Bigram matching (1.5x multiplier)    │   │
  │         │   │  • Synonym expansion                    │   │
  │         │   │                                         │   │
  │         │   │  Speed: ~10ms  |  Accuracy: Good        │   │
  │         │   │                                         │   │
  │         │   │  Return top 6 ranked tables             │   │
  │         │   │                                         │   │
  │         │   └─────────────────────────────────────────┘   │
  │                                                          │
  └──────────────────────────────────────────────────────────┘
```

---

### Step 5: Prompt Assembly

**Modules:** `prompt_engine/builder.py`, `prompt_engine/static_prefix.py`, `prompt_engine/templates.py`

`PromptBuilder.build()` assembles one prompt string from a **static prefix** and a **variable suffix**. One gate decides how the prefix is made, `should_use_static_prefix`, which compares the prefix's token estimate (`len(text) // 4`) with `PROMPT_RETRIEVAL_TOKEN_BUDGET` (default 6000):

```
  STATIC PATH (the prefix fits the budget — the default)
  ┌─────────────────────────────────────────────────────────────┐
  │  system prompt        <PROJECT_CONFIG_DIR>/system_prompt.md │
  │  BUSINESS RULES       every rule         (business_rules)   │
  │  METRICS              every metric       (metrics)          │
  │  DATABASE SCHEMA      every table        (schema.yaml)      │  byte-identical for
  │  RELATIONSHIPS        every relationship (schema.yaml)      │  every request, built
  │  EXAMPLES             every example      (examples)         │  once and cached, so
  └─────────────────────────────────────────────────────────────┘  the model server can
  ┌─────────────────────────────────────────────────────────────┐  reuse its KV cache
  │  DETECTED FILTERS            ValueRetriever (Step 4.6)      │
  │  RESOLVED WAREHOUSE VALUES   matched against the warehouse, │  the only part that
  │                              fenced as data, not as orders  │  changes per request
  │  SESSION CONTEXT             the last turns of a session    │
  │  USER QUESTION               the original input             │
  └─────────────────────────────────────────────────────────────┘

  RETRIEVAL PATH (the prefix is over the budget)
  The same sections, built per question from only the tables, relationships,
  rules and examples the six retrievers selected (Step 4).
```

**With several data sources** (`project_config/datasources.yaml`) one source is chosen for the question first, with no model call (`retrieval/source_selector.py`: `keywords:`, then the conversation, then retrieval evidence, then the default), and the prompt describes that source only: its tables (those shared with other sources included), the relationships between them, and the examples whose SQL reads only those tables, under one `Data source: <name> — <description>` line. The gate runs per source, so each source has its own cached prefix and its own budget decision; a source over the budget uses the retrieval path restricted to its tables. If the model answers `OUT_OF_SCOPE`, the request is retried once with the next candidate source (`llm/source_routing.py`). With one source none of this runs and the prompt is exactly what it was. See `docs/design/DATASOURCES.md`.

**Why this matters:** an identical prefix lets a local model server skip prefill for everything but the short suffix, which is the latency win. Retrieval remains the escape hatch that keeps a schema too large for the context window usable.

---

### Step 6: LLM Generation

**Modules:** `llm/router.py` → `llm/providers.py` (the CLI's one-shot path uses `llm/wizard_llm.py`) → OpenAI-compatible API

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    LLM GENERATION PROCESS                       │
  │                                                                 │
  │   Assembled Prompt                                             │
  │        │                                                       │
  │        ▼                                                       │
  │   ┌────────────────────────────────────────────────────────┐   │
  │   │                                                        │   │
  │   │   POST <base_url>/chat/completions                     │   │
  │   │                                                        │   │
  │   │   {                                                    │   │
  │   │     "model": "gpt-oss-20:F16",                         │   │
  │   │     "messages": [{"role": "user",                      │   │
  │   │                   "content": "<prompt>"}],             │   │
  │   │     "stream": false                                    │   │
  │   │   }                                                    │   │
  │   │                                                        │   │
  │   └────────────────────────┬───────────────────────────────┘   │
  │                            │                                    │
  │                            ▼                                    │
  │   ┌────────────────────────────────────────────────────────┐   │
  │   │                                                        │   │
  │   │   RETRY LOGIC (3 attempts, exponential backoff):       │   │
  │   │                                                        │   │
  │   │   Attempt 1 ──► Wait 1s ──► Attempt 2 ──► Wait 2s     │   │
  │   │        │                          │              │      │   │
  │   │        │ Fail                     │ Fail         │      │   │
  │   │        ▼                          ▼              ▼      │   │
  │   │   ┌────────┐               ┌────────┐      ┌────────┐  │   │
  │   │   │ Retry  │               │ Retry  │      │ FAIL   │  │   │
  │   │   └────────┘               └────────┘      └────────┘  │   │
  │   │                                                        │   │
  │   └────────────────────────┬───────────────────────────────┘   │
  │                            │                                    │
  │                            ▼                                    │
  │   ┌────────────────────────────────────────────────────────┐   │
  │   │                                                        │   │
  │   │   RESPONSE HANDLING:                                   │   │
  │   │                                                        │   │
  │   │   ┌─────────────────────┐    ┌──────────────────────┐  │   │
  │   │   │ Response:           │    │ Response:            │  │   │
  │   │   │ "OUT_OF_SCOPE"      │    │ Raw SQL text         │  │   │
  │   │   │                     │    │                      │  │   │
  │   │   │ → Reject question   │    │ → Proceed to Step 7  │  │   │
  │   │   │   (not auction-     │    │                      │  │   │
  │   │   │    related)         │    │ May include:         │  │   │
  │   │   └─────────────────────┘    │ • Markdown fences    │  │   │
  │   │                              │ • Prose preamble     │  │   │
  │   │   ┌─────────────────────┐    │ • LIMIT clause       │  │   │
  │   │   │ Response: EMPTY     │    └──────────────────────┘  │   │
  │   │   │                     │                              │   │
  │   │   │ → Raise error       │                              │   │
  │   │   └─────────────────────┘                              │   │
  │   │                                                        │   │
  │   └────────────────────────────────────────────────────────┘   │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

---

### Step 7: SQL Cleaning

**Module:** `security/sql_guard.py` → `clean_sql()` function

```
  Raw LLM Output
       │
       ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    SQL CLEANING PIPELINE                        │
  │                                                                 │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  INPUT:                                                 │  │
  │   │  ```sql                                                 │  │
  │   │  SELECT TOP 100 c.Name, SUM(cc.TotalPrice) AS Value    │  │
  │   │  FROM [sales].[Order] c                       │  │
  │   │  JOIN [sales].[Customer] cc ON c.CustID = cc.ID   │  │
  │   │  LIMIT 100                                              │  │
  │   │  ```                                                    │  │
  │   └─────────────────────┬───────────────────────────────────┘  │
  │                         │                                       │
  │                         ▼                                       │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  STEP 7.1: Extract from markdown fences                │  │
  │   │                                                         │  │
  │   │  ```sql ... ```  ──►  SELECT TOP 100 c.Name ...        │  │
  │   │                                                         │  │
  │   └─────────────────────┬───────────────────────────────────┘  │
  │                         │                                       │
  │                         ▼                                       │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  STEP 7.2: Remove prose preamble                       │  │
  │   │                                                         │  │
  │   │  "Here is the query: SELECT ..."  ──►  SELECT ...      │  │
  │   │                                                         │  │
  │   └─────────────────────┬───────────────────────────────────┘  │
  │                         │                                       │
  │                         ▼                                       │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  STEP 7.3: Convert LIMIT to TOP                        │  │
  │   │                                                         │  │
  │   │  SELECT * FROM T LIMIT 5                                │  │
  │   │       │                                                 │  │
  │   │       ▼                                                 │  │
  │   │  SELECT TOP 5 * FROM T                                  │  │
  │   │                                                         │  │
  │   │  (If TOP already exists, just strip LIMIT)              │  │
  │   │                                                         │  │
  │   └─────────────────────┬───────────────────────────────────┘  │
  │                         │                                       │
  │                         ▼                                       │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  STEP 7.4: Fix TOP DISTINCT order                      │  │
  │   │                                                         │  │
  │   │  SELECT TOP 10 DISTINCT ...                             │  │
  │   │       │                                                 │  │
  │   │       ▼                                                 │  │
  │   │  SELECT DISTINCT TOP 10 ...                             │  │
  │   │                                                         │  │
  │   └─────────────────────┬───────────────────────────────────┘  │
  │                         │                                       │
  │                         ▼                                       │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  OUTPUT:                                                │  │
  │   │  SELECT TOP 100 c.Name, SUM(cc.TotalPrice) AS Value    │  │
  │   │  FROM [sales].[Order] c                       │  │
  │   │  JOIN [sales].[Customer] cc ON c.CustID = cc.ID   │  │
  │   │                                                         │  │
  │   └─────────────────────────────────────────────────────────┘  │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

---

### Step 8: SQL Validation (Security Check)

**Module:** `security/sql_guard.py` → `validate_sql()` function

`validate_sql` is **parser-based**: it parses the statement with sqlglot and decides from the syntax tree, not from keywords in the text (`tests/test_sql_guard_bypass.py` holds the bypasses and false positives this replaced). A statement is accepted only if all of these hold; each refusal carries a `reason` that clients and the audit trail use (`docs/api-contract-v2.md` §4):

```
  Cleaned SQL
       │
       ▼
  1. exactly one statement                      (stacked statements refused as a class)
  2. a SELECT/WITH root, or a top-level UNION / INTERSECT / EXCEPT
       DDL, DML, EXEC, SELECT ... INTO, xp_* / sp_* / OPENROWSET ... refused
       wherever they appear in the tree
  3. no comments                                (refused because present)
  4. no system catalogue                        (INFORMATION_SCHEMA, sys.*, ...)
  5. every table is on the allowlist derived from schema.yaml (a table with a
       `columns` map), or a CTE of this query; a schema written in front of a
       name must match the table's known schema            → unknown_table,
       a bare name that two schemas share is refused       → ambiguous_table
  6. every qualified column exists on its table; denied_columns refused
       (also through * )                                   → denied_column
  7. function calls: only allowlisted data functions; anything that reads
       server or session state is refused
  8. at least one non-CTE table                 → no_table_reference
  9. one data source can run it: some configured source has EVERY table the
       statement reads (database.routing.choose_datasource, the same
       function the executor uses)                         → cross_datasource
       │
       ▼
  accepted → ensure_top() → execute
```

`LIMIT` is not on the refusal list: `clean_sql` (Step 7) has already rewritten it to `TOP n`.

---

### Step 9: Self-Correction Loop (if needed)

**Module:** `llm/sql_agent.py`

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                 SELF-CORRECTION LOOP                            │
  │                                                                 │
  │   ┌───────────────────────────────────────────────────────┐    │
  │   │  ATTEMPT 1 (Initial Generation)                       │    │
  │   │                                                       │    │
  │   │  question ──► retrieval ──► prompt ──► LLM            │    │
  │   │                                            │          │    │
  │   │                                            ▼          │    │
  │   │                                      clean ──► validate│    │
  │   │                                            │          │    │
  │   │                                            ▼          │    │
  │   │                                         execute       │    │
  │   │                                            │          │    │
  │   │                                    ┌───────┴───────┐  │    │
  │   │                                    │               │  │    │
  │   │                                  SUCCESS         ERROR │    │
  │   │                                    │               │  │    │
  │   │                                    ▼               │  │    │
  │   │                              Return Result        │  │    │
  │   │                                                   │  │    │
  │   └───────────────────────────────────────────────────┼──┘    │
  │                                                       │        │
  │                                                       ▼        │
  │   ┌───────────────────────────────────────────────────────┐    │
  │   │  ATTEMPT 2 (Correction Round 1)                       │    │
  │   │                                                       │    │
  │   │  initial_prompt + error_message                       │    │
  │   │       │                                               │    │
  │   │       ▼                                               │    │
  │   │  LLM ──► clean ──► validate ──► execute              │    │
  │   │                                     │                 │    │
  │   │                             ┌───────┴───────┐        │    │
  │   │                             │               │        │    │
  │   │                           SUCCESS         ERROR      │    │
  │   │                             │               │        │    │
  │   │                             ▼               │        │    │
  │   │                       Return Result        │        │    │
  │   │                                            │        │    │
  │   └────────────────────────────────────────────┼────────┘    │
  │                                                │              │
  │                                                ▼              │
  │   ┌───────────────────────────────────────────────────────┐    │
  │   │  ATTEMPT 3 (Correction Round 2 — Final)               │    │
  │   │                                                       │    │
  │   │  initial_prompt + error_message                       │    │
  │   │       │                                               │    │
  │   │       ▼                                               │    │
  │   │  LLM ──► clean ──► validate ──► execute              │    │
  │   │                                     │                 │    │
  │   │                             ┌───────┴───────┐        │    │
  │   │                             │               │        │    │
  │   │                           SUCCESS         ERROR      │    │
  │   │                             │               │        │    │
  │   │                             ▼               ▼        │    │
  │   │                       Return Result   RAISE FINAL    │    │
  │   │                                       ERROR TO       │    │
  │   │                                       CALLER         │    │
  │   └───────────────────────────────────────────────────────┘    │
  │                                                                 │
  │   MAX_CORRECTION_ATTEMPTS = 2 (configurable)                   │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

A rejection that no rewrite could satisfy (a policy refusal such as a denied column, or `cross_datasource`) is not re-prompted, and a response cut short by `LLM_NUM_PREDICT` before any SQL is not retried either. With several data sources, the one retry on the next source after an `OUT_OF_SCOPE` answer is outside this correction budget.

---

### Step 10: Database Execution

**Module:** `database/connection.py` + `database/executor.py`

```
  Validated SQL Query
       │
       ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    DATABASE EXECUTION                           │
  │                                                                 │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  SQLALCHEMY ENGINE                                      │  │
  │   │                                                         │  │
  │   │  ┌─────────────────────────────────────────────────┐   │  │
  │   │  │  Configuration:                                  │   │  │
  │   │  │                                                   │   │  │
  │   │  │  pool_size      = 10    (persistent connections)  │   │  │
  │   │  │  max_overflow   = 20    (burst connections)       │   │  │
  │   │  │  pool_recycle   = 3600s (recycle every hour)      │   │  │
  │   │  │  idle-aware ping (DB_POOL_PING_IDLE_SECONDS)     │   │  │
  │   │  │  (SELECT-only: no fast_executemany)               │   │  │
  │   │  │                                                   │   │  │
  │   │  └─────────────────────────────────────────────────┘   │  │
  │   │                                                         │  │
  │   └─────────────────────────┬───────────────────────────────┘  │
  │                             │                                    │
  │                             ▼                                    │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  EXECUTION STEPS:                                       │  │
  │   │                                                         │  │
  │   │  1. SET LOCK_TIMEOUT {timeout_ms}                       │  │
  │   │     └─ Prevents indefinite blocking                      │  │
  │   │                                                         │  │
  │   │  2. Execute SQL query                                   │  │
  │   │     └─ Against SQL Server via ODBC 17 or 18             │  │
  │   │                                                         │  │
  │   │  3. fetchmany(MAX_ROWS_RETURNED)                        │  │
  │   │     └─ Hard row cap: 1000 rows max                      │  │
  │   │     └─ Enforced even if TOP clause is present           │  │
  │   │                                                         │  │
  │   │  4. Return as pandas DataFrame                          │  │
  │   │     └─ Column names from cursor description             │  │
  │   │                                                         │  │
  │   └─────────────────────────┬───────────────────────────────┘  │
  │                             │                                    │
  │                             ▼                                    │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │                                                         │  │
  │   │   Result: pandas.DataFrame                              │  │
  │   │                                                         │  │
  │   │   ┌─────────────────────────────────────────────────┐  │  │
  │   │   │  Name        │ Month   │ TradeValue             │  │  │
  │   │   │──────────────│─────────│────────────────────────│  │  │
  │   │   │  PetroCo     │ فروردین  │ 48,320,000,000        │  │  │
  │   │   │  PetroCo     │ اردیبهشت│ 39,210,000,000        │  │  │
  │   │   │  ...         │ ...     │ ...                    │  │  │
  │   │   └─────────────────────────────────────────────────┘  │  │
  │   │                                                         │  │
  │   └─────────────────────────────────────────────────────────┘  │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

One SQLAlchemy engine per configured data source is cached for the life of the process (`database.connection.get_engine(datasource=...)`); a deployment with no `datasources.yaml` has one, exactly as pictured. Which engine a statement uses is derived from its tables, never chosen by the model: the source that has every table it reads (the default source if it qualifies). With `nolock: true` on that source, the executor inserts ` WITH (NOLOCK)` after each table reference just before sending the text. Routing, the shared-table rule and `NOLOCK` are in `docs/design/DATASOURCES.md`; the operator's procedure is `docs/deployment-runbook.md` §16.

---

### Step 11: Result Processing and Output

```
  pandas DataFrame
       │
       ├──────────────────────────────────────────────────────────┐
       │                                                          │
       ▼                                                          ▼
  ┌─────────────────────┐                          ┌─────────────────────┐
  │   CLI MODE          │                          │   HTTP MODE         │
  │   (app.py)          │                          │   (api/runner.py)   │
  ├─────────────────────┤                          ├─────────────────────┤
  │                     │                          │                     │
  │  1. Display in      │                          │  1. Serialize to    │
  │     terminal        │                          │     JSON response   │
  │     (up to 20 rows) │                          │                     │
  │                     │                          │  2. Include:        │
  │  2. Export to       │                          │     • question      │
  │     Excel file      │                          │     • sql (if full) │
  │     auto-fitted     │                          │     • result rows   │
  │     columns         │                          │     • row_count     │
  │                     │                          │     • status        │
  │  3. Log structured  │                          │                     │
  │     JSON entry      │                          │  3. If interpret:   │
  │                     │                          │     LLM generates   │
  │                     │                          │     plain summary   │
  │                     │                          │                     │
  │                     │                          │  4. Cache result    │
  │                     │                          │     for future use  │
  │                     │                          │                     │
  └─────────────────────┘                          └─────────────────────┘
```

---

### Step 12: Logging

**Modules:** `logs/logger.py` + `logs/query_log.py` (the CLI), `observability/audit.py` (the HTTP API)

```
  CLI — one line per question in logs/query_log.jsonl:
  {
    "timestamp": "2026-06-27T14:22:57",
    "question": "top 5 customers by purchase value in 2024",
    "generated_sql": "SELECT TOP 5 c.Name ...",
    "model_name": "openai:gpt-oss-20:F16",
    "status": "SUCCESS",
    "row_count": 5,
    "execution_time_seconds": 3.456,
    "error_message": null,
    "excel_file": "exports/result_20260627_142257.xlsx"
  }

  HTTP API — one record per request in logs/audit_log.jsonl: request and
  session ids, the principal, the question's tier, the guard verdict and
  tables touched, the LLM status block (tokens, prefix-cache hit, finish
  reason), per-stage timings, the configuration version, `datasource` (where
  the SQL ran) and, with several data sources, `datasource_selection` (which
  source the model was shown and why). Never result rows.
```

Both files rotate by size (`LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`). `scripts/analyze_audit_log.py` aggregates the audit log without question text or SQL (`docs/deployment-runbook.md` §8).

---

## 4. Module Breakdown

### 4.1 Directory Structure

The README's "Project structure" section is the maintained file-by-file tree. By layer:

| Directory | Role |
|---|---|
| `app.py` | CLI entry point (interactive REPL) |
| `config.py` | Typed `Settings` singleton, read from the environment |
| `api/` | FastAPI service: `server.py` (app and routes), `runner.py` (cache-aware orchestrator), `v2_routes.py` (conversations), `admin_*.py` (admin panel API), middleware, auth, health, errors |
| `session/` | Conversational engine: `TurnEngine`, refinement, CTE composition, declared assumptions, persistence |
| `retrieval/` | The six retrievers, `context_retriever.py` (orchestrator), value resolution, the dimension vocabulary, `source_selector.py` (which data source) |
| `prompt_engine/` | `builder.py`, `static_prefix.py` (one cacheable prefix per data source), `source_scope.py`, `templates.py` |
| `llm/` | `router.py` (task routing and fallback), `providers.py` (OpenAI-compatible client with retries), `sql_agent.py` (generate, clean, validate, correct), `source_routing.py` (per-question source and the single `OUT_OF_SCOPE` retry), `wizard_llm.py` (the CLI's one-shot path) |
| `security/` | `sql_guard.py` (clean, validate, cap, transpile, `pretty_sql`), `sql_format.py` (house layout of displayed SQL), `dialects.py`, `auth.py` |
| `database/` | `connection.py` (one engine per data source), `datasources.py` (`datasources.yaml`), `routing.py`, `executor.py`, `table_hints.py` (`WITH (NOLOCK)`), `catalogue.py` |
| `schema_data/`, `knowledge/` | Loaders and validation for `project_config/*.yaml` (`schema_data/registry.py` is the schema allowlist; `drift.py` compares it with the live catalogues) |
| `appdb/` | Application database: API keys, role grants, config versions, feedback, access requests |
| `observability/` | Audit records, the LLM status block, stage timings |
| `exporters/`, `logs/` | Excel / CSV / JSON exports; rotating JSONL logger |
| `core/` | Shared models, the Persian normaliser, strict YAML loading, the start-up notice |
| `scripts/` | Operator tools: `verify_deployment.py`, `issue_api_key.py`, `assign_datasources.py`, `prompt_budget.py`, `analyze_audit_log.py`, `analyze_misses.py`, `migrate_app_db.py` |
| `web/` | Static Persian/RTL client and the admin panel (no build step) |
| `project_config/` | Deployment-specific domain data, git-ignored (template: `project_config.example/`) |

### 4.2 Module Dependency Map

```
  app.py ──► llm/wizard_llm.py ──► retrieval ──► prompt_engine ──► llm/providers.py ──► model endpoint
     │                 │
     │                 └────────► security/sql_guard.py
     └──► database/executor.py ──► database/routing.py ──► database/connection.py ──► SQL Server
                                    (source per statement)   (engine per data source)

  api/server.py ──► api/runner.py ──► llm/sql_agent.py ──► llm/router.py ──► llm/providers.py
        │                │                  │
        │                │                  ├──► retrieval (incl. source_selector) ──► prompt_engine
        │                │                  ├──► security/sql_guard.py
        │                │                  └──► database/executor.py ──► ... (as above)
        │                └──► api/query_cache.py
        ├──► api/v2_routes.py ──► session/engine.py ──► (the same stages)
        └──► api/middleware.py, api/auth.py
```

---

## 5. API Endpoints

### 5.1 Endpoint Overview

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    API ENDPOINT MAP                              │
  │                                                                 │
  │   ┌──────────────┐    ┌──────────────────────────────────┐     │
  │   │  POST        │    │  /query                          │     │
  │   │  /query      │───►│  Translate question to SQL       │     │
  │   │              │    │  and/or execute it                │     │
  │   └──────────────┘    └──────────────────────────────────┘     │
  │                                                                 │
  │   ┌──────────────┐    ┌──────────────────────────────────┐     │
  │   │  GET         │    │  /health                         │     │
  │   │  /health     │───►│  Check DB + LLM endpoint         │     │
  │   │             │    │  reachability                     │     │
  │   └──────────────┘    └──────────────────────────────────┘     │
  │                                                                 │
  │   ┌──────────────┐    ┌──────────────────────────────────┐     │
  │   │  GET         │    │  /cache/stats                    │     │
  │   │  /cache/stats│───►│  Return cache metrics            │     │
  │   └──────────────┘    └──────────────────────────────────┘     │
  │                                                                 │
  │   ┌──────────────┐    ┌──────────────────────────────────┐     │
  │   │  POST        │    │  /cache/invalidate               │     │
  │   │  /cache/     │───►│  Evict specific cache entry      │     │
  │   │  invalidate  │    └──────────────────────────────────┘     │
  │   └──────────────┘                                              │
  │                                                                 │
  │   ┌──────────────┐    ┌──────────────────────────────────┐     │
  │   │  POST        │    │  /cache/clear                    │     │
  │   │  /cache/clear│───►│  Flush entire cache              │     │
  │   └──────────────┘    └──────────────────────────────────┘     │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

The map shows the original routes. Since then the service gained `POST /query/stream` (the same request as Server-Sent Events), the conversational routes under `/v2/` (sessions, turns, memory, feedback, access requests; `docs/api-contract-v2.md`) and the admin panel's `/admin/` routes (`docs/admin-panel-architecture.md`); the README's "API endpoints" table lists them. Every route except `GET /health` requires `Authorization: Bearer <api-key>`.

### 5.2 POST /query — Request Modes

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    QUERY MODES                                  │
  │                                                                 │
  │   ┌───────────────────────────────────────────────────────┐    │
  │   │                                                       │    │
  │   │   mode = "sql"                                        │    │
  │   │                                                       │    │
  │   │   ┌─────────┐    ┌─────────┐                          │    │
  │   │   │ Question│───►│  LLM    │───► SQL only             │    │
  │   │   │         │    │         │    (no execution)         │    │
  │   │   └─────────┘    └─────────┘                          │    │
  │   │                                                       │    │
  │   │   Use case: Preview the generated SQL                 │    │
  │   │   Cache: SKIPPED (freshness matters)                  │    │
  │   │                                                       │    │
  │   └───────────────────────────────────────────────────────┘    │
  │                                                                 │
  │   ┌───────────────────────────────────────────────────────┐    │
  │   │                                                       │    │
  │   │   mode = "result"                                     │    │
  │   │                                                       │    │
  │   │   ┌─────────┐    ┌─────────┐    ┌──────────┐         │    │
  │   │   │ Question│───►│  LLM    │───►│ Execute  │──► Data │    │
  │   │   │         │    │         │    │          │         │    │
  │   │   └─────────┘    └─────────┘    └──────────┘         │    │
  │   │                                                       │    │
  │   │   Use case: Get data without seeing SQL               │    │
  │   │   Cache: USED                                         │    │
  │   │                                                       │    │
  │   └───────────────────────────────────────────────────────┘    │
  │                                                                 │
  │   ┌───────────────────────────────────────────────────────┐    │
  │   │                                                       │    │
  │   │   mode = "full"                                       │    │
  │   │                                                       │    │
  │   │   ┌─────────┐    ┌─────────┐    ┌──────────┐         │    │
  │   │   │ Question│───►│  LLM    │───►│ Execute  │──► Data │    │
  │   │   │         │    │         │    │          │         │    │
  │   │   └─────────┘    └─────────┘    └──────────┘         │    │
  │   │                                                       │    │
  │   │   Use case: Full transparency (SQL + results)         │    │
  │   │   Cache: USED                                         │    │
  │   │                                                       │    │
  │   └───────────────────────────────────────────────────────┘    │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

---

## 6. Security Model

Every generated SQL query passes through a **multi-layer security pipeline** before execution:

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    SECURITY PIPELINE                            │
  │                                                                 │
  │   Raw LLM Output                                               │
  │        │                                                       │
  │        ▼                                                       │
  │   LAYER 1: clean_sql()                                         │
  │     • Extract from markdown fences, drop prose preamble        │
  │     • Convert LIMIT → TOP, fix TOP DISTINCT order              │
  │        │                                                       │
  │        ▼                                                       │
  │   LAYER 2: validate_sql()      (parser-based, Step 8)          │
  │     • exactly one SELECT/WITH statement                        │
  │     • table and column allowlist from schema.yaml, schema      │
  │       qualifier checked, column ACL (denied_columns)           │
  │     • no comments, no system catalogues, allowlisted functions │
  │     • one data source can run it (cross_datasource otherwise)  │
  │        │                                                       │
  │        ▼                                                       │
  │   LAYER 3: ensure_top()                                        │
  │     • inject TOP DEFAULT_TOP_N when there is no row limit      │
  │        │                                                       │
  │        ▼                                                       │
  │   LAYER 4: execute_sql()                                       │
  │     • runs on the source that has every table                  │
  │     • SET LOCK_TIMEOUT, driver timeout, always rolled back     │
  │     • fetchmany(MAX_ROWS_RETURNED): the hard row cap           │
  │        │                                                       │
  │        ▼                                                       │
  │   Result set                                                   │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

### Security Guarantees Summary

- Only read-only `SELECT` statements over allowlisted tables and columns are executed; DDL, DML and procedure calls are refused by syntax-tree node type.
- A principal's `denied_columns` are enforced in the guard, not only partitioned in the cache.
- Row limits are enforced at SQL level (`TOP`) and again in the application (`fetchmany`).
- Every route except `GET /health` requires an API key; keys are stored only as SHA-256 digests.
- No credentials in code or in versioned config: secrets come from the environment (`datasources.yaml` names only the variable that holds a password).
- Defence in depth belongs on the server as well: `docs/db-hardening.md` specifies the read-only login, `DENY` grants and Resource Governor group for each warehouse server.
- With a local model endpoint no question, schema or row leaves the network; a remote endpoint is refused unless `LLM_ALLOW_REMOTE` is true.

The README's "Security model" and "Authentication" sections are the full list.

---

## 7. Configuration

All configuration is read from **environment variables** (or a `.env` file); `.env.example` documents every one, and `config.py` carries the reasoning behind each default. The ones most deployments touch:

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (set it to your own for on-premise use) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name served by the endpoint |
| `OPENAI_API_KEY` | *(empty)* | Endpoint credential; a local server usually needs none |
| `DB_CONNECTION_URL` | *(required without `datasources.yaml`)* | SQLAlchemy connection string, without the password |
| `DB_PASSWORD` | *(empty)* | The raw password for `DB_CONNECTION_URL` |
| `QUERY_TIMEOUT_SECONDS` | `60` | Max query time (seconds) |
| `MAX_ROWS_RETURNED` | `1000` | Hard row cap |
| `PROMPT_RETRIEVAL_TOKEN_BUDGET` | `6000` | Static prefix up to this estimate, retrieval path above it; per data source |
| `CACHE_TTL_SECONDS` / `CACHE_MAX_SIZE` | `300` / `256` | Query cache (`0` TTL = disabled) |
| `API_KEYS_JSON` / `API_KEYS_FILE` | *(empty)* | The API key array, inline or in a file; set one |
| `AUTH_REQUIRED` | `true` | Fail-closed authentication |
| `LOG_DIR` / `EXPORT_DIR` | `logs` / `exports` | Log and export directories |

`DB_CONNECTION_URL` is what a deployment with exactly **one** warehouse connection sets, and the password goes in `DB_PASSWORD`, raw, so nothing is percent-encoded by hand (`database.datasources.apply_db_password`). A deployment that queries more than one database instead adds `project_config/datasources.yaml`, whose sources describe the connection (host, port, database, driver, login, options, or `trusted_connection: true`) while `.env` holds only the raw password, one variable per source, named by `password_env`; `DB_CONNECTION_URL` and `DB_PASSWORD` are then unused. The design is `docs/design/DATASOURCES.md` and the ordered procedure (assigning each table's `datasource:`, keywords, the token budget, `nolock`) is `docs/deployment-runbook.md` §16.

---

## 8. How to Run

### 8.1 Prerequisites

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    PREREQUISITES                                │
  │                                                                 │
  │   ┌───────────────┐    ┌───────────────┐    ┌───────────────┐  │
  │   │               │    │               │    │               │  │
  │   │   Python      │    │  OpenAI-      │    │   SQL Server  │  │
  │   │   3.11+       │    │  compatible   │    │   + ODBC      │  │
  │   │               │    │  LLM endpoint │    │   Driver 17   │  │
  │   │               │    │  (vLLM/LM     │    │               │  │
  │   │               │    │   Studio/…)   │    │               │  │
  │   └───────────────┘    └───────────────┘    └───────────────┘  │
  │          │                    │                    │            │
  │          └────────────────────┼────────────────────┘            │
  │                               │                                 │
  │                               ▼                                 │
  │                    ┌─────────────────────┐                      │
  │                    │  Set in .env:       │                      │
  │                    │  OPENAI_BASE_URL,   │                      │
  │                    │  OPENAI_MODEL,      │                      │
  │                    │  OPENAI_API_KEY     │                      │
  │                    └─────────────────────┘                      │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

### 8.2 CLI Mode — Quick Start

```bash
# Step 1: Install dependencies
pip install -r requirements.lock

# Step 2: Configure environment
cp .env.example .env
# Edit .env with your database URL, OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY

# Step 3: Run the interactive CLI
python app.py
```

### 8.3 HTTP API Mode — Quick Start

```bash
# Start the FastAPI server
uvicorn api.server:app --host 0.0.0.0 --port 8000 --no-server-header

# Send a query via HTTP
curl -X POST http://localhost:8000/query \
  -H 'Authorization: Bearer <your-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{"question": "فروش ماهانه تالار پتروشیمی در 1402", "mode": "full"}'
# the key comes from: python -m scripts.issue_api_key --id analyst-1 --name "Jane Analyst"
# (docs/deployment-runbook.md §1-§2); a real deployment also runs
# python -m scripts.verify_deployment first (§3)
```

---

## 9. Testing

```bash
# Run all tests
pytest tests/ -v

# Run a specific test module
pytest tests/test_sql_guard.py -v

# Run with coverage report
pytest --cov=. --cov-report=html
```

**Test Categories:**

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    TEST SUITE (5,000+ tests)                    │
  │                                                                 │
  │   ┌───────────────────┐  ┌───────────────────┐                 │
  │   │  Unit Tests       │  │  Integration      │                 │
  │   │                   │  │  Tests            │                 │
  │   │  • retriever      │  │                   │                 │
  │   │  • sql_guard      │  │  • Full pipeline  │                 │
  │   │  • executor       │  │  • API endpoints  │                 │
  │   │  • cache          │  │  • DB connection  │                 │
  │   │  • middleware      │  │                   │                 │
  │   └───────────────────┘  └───────────────────┘                 │
  │                                                                 │
  │   ┌───────────────────┐  ┌───────────────────┐                 │
  │   │  Stress Tests     │  │  Cache Tests      │                 │
  │   │                   │  │                   │                 │
  │   │  • Concurrent     │  │  • TTL expiry     │                 │
  │   │    requests       │  │  • LRU eviction   │                 │
  │   │  • Rate limiting  │  │  • Thread safety  │                 │
  │   │  • Error recovery │  │  • Invalidation   │                 │
  │   └───────────────────┘  └───────────────────┘                 │
  │                                                                 │
  │   CI: GitHub Actions (3 OSes, Python 3.11-3.13)                │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

---

*This document describes the Local SQL Agent system as of release 6.6.1 (October 2026). Where it and the code disagree, the code wins; `README.md`, `docs/deployment-runbook.md` and `docs/design/DATASOURCES.md` are the maintained references.*
