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
  │  470+            │  On-Premise      │  OpenAI-compatible   │
  │  Unit +          │  Only            │  LLM Endpoint        │
  │  Integration     │                  │                       │
  └──────────────────┴──────────────────┴───────────────────────┘
```

### 1.5 Database Schema Overview

The system operates on a **Star Schema** data warehouse called `Auction_DM`:

```
                              ┌──────────────┐
                              │  General_Dim │
                              │    Date      │
                              └──────┬───────┘
                                     │
  ┌──────────────┐   ┌───────────────┼───────────────┐   ┌──────────────┐
  │  Auction_Dim │   │               │               │   │  Auction_Dim │
  │  Customer    ├───┤               │               ├───┤  Ring        │
  └──────────────┘   │               │               │   └──────────────┘
                     │               │               │
  ┌──────────────┐   │               │               │   ┌──────────────┐
  │  Auction_Dim │   │               │               │   │  Auction_Dim │
  │  Broker      ├───┤               │               ├───┤  Symbol      │
  └──────────────┘   │               │               │   └──────────────┘
                     │    ┌──────────┴──────────┐    │
                     │    │                     │    │
                     │    │   Auction_Fact       │    │
                     │    │   ┌─────────────┐   │    │
                     ├───┼───┤  Contract    ├───┼────┤
                     │    │   └─────────────┘   │    │
                     │    │                     │    │
                     │    │   ┌─────────────┐   │    │
                     ├───┼───┤  Customer    ├───┼────┤
                     │    │   │  Contract    │   │    │
                     │    │   └─────────────┘   │    │
                     │    │                     │    │
                     │    │   ┌─────────────┐   │    │
                     │    ├───┤  Offer      ├───┤    │
                     │    │   └─────────────┘   │    │
                     │    │                     │    │
                     │    │   ┌─────────────┐   │    │
                     │    └───┤  Order      ├───┘    │
                     │        └─────────────┘        │
                     └───────────────────────────────┘

  Auction_Fact  = Fact tables (transactions, numbers)
  Auction_Dim   = Dimension tables (customers, rings, brokers)
  General_Dim   = Shared dimensions (date/calendar)
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
║  │   Assembles a single prompt from 7 labeled sections:          │   ║
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
║  │   llm/wizard_llm.py ──► OpenAI-compatible API             │   ║
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
║  │                               ├── Block DDL/DML? ──► ERROR    │   ║
║  │                               ├── Block injection? ──► ERROR  │   ║
║  │                               └── Valid SELECT? ──► PASS      │   ║
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
║  │   │  SQLAlchemy Engine                                    │    │   ║
║  │   │  pool_size=10 | max_overflow=20 | pre_ping=True      │    │   ║
║  │   └──────────────────────┬───────────────────────────────┘    │   ║
║  │                          │                                     │   ║
║  │                          ▼                                     │   ║
║  │   ┌──────────────────────────────────────────────────────┐    │   ║
║  │   │  SQL Server (ODBC Driver 17)                         │    │   ║
║  │   │  Auction_DM database                                 │    │   ║
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

---

### Step 2: Middleware Processing (HTTP Mode Only)

**Module:** `api/middleware.py`

```
  Incoming HTTP Request
        │
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                  MIDDLEWARE STACK                            │
  │                                                             │
  │   ┌─────────────────────────────────────────────────────┐  │
  │   │  LAYER 3 (Outermost): RequestIDMiddleware           │  │
  │   │                                                     │  │
  │   │  • Assigns unique X-Request-ID to every request     │  │
  │   │  • Enables request tracking and correlation         │  │
  │   │  • Adds X-Response-Time header to response          │  │
  │   └─────────────────────────┬───────────────────────────┘  │
  │                             │                               │
  │                             ▼                               │
  │   ┌─────────────────────────────────────────────────────┐  │
  │   │  LAYER 2: RateLimitMiddleware                       │  │
  │   │                                                     │  │
  │   │  Token-bucket algorithm per client IP:              │  │
  │   │                                                     │  │
  │   │  ┌──────────┐    ┌──────────┐    ┌──────────┐     │  │
  │   │  │ 60 req   │    │ 60 sec   │    │ 10 burst │     │  │
  │   │  │ / window │    │ window   │    │ capacity │     │  │
  │   │  └──────────┘    └──────────┘    └──────────┘     │  │
  │   │                                                     │  │
  │   │  If exceeded → HTTP 429 "Too Many Requests"         │  │
  │   └─────────────────────────┬───────────────────────────┘  │
  │                             │                               │
  │                             ▼                               │
  │   ┌─────────────────────────────────────────────────────┐  │
  │   │  LAYER 1 (Innermost): ConcurrencyMiddleware        │  │
  │   │                                                     │  │
  │   │  Semaphore-based concurrency limiter:              │  │
  │   │                                                     │  │
  │   │  ┌──────────────────────────────────────────────┐  │  │
  │   │  │  Slots: [1][2][3][4][5][6][7][8][9][10]     │  │  │
  │   │  │          ▲                                   │  │  │
  │   │  │          │ MAX_CONCURRENT_REQUESTS = 10      │  │  │
  │   │  └──────────────────────────────────────────────┘  │  │
  │   │                                                     │  │
  │   │  If all slots full → HTTP 503 "Server Overload"    │  │
  │   └─────────────────────────┬───────────────────────────┘  │
  │                             │                               │
  └─────────────────────────────┼───────────────────────────────┘
                                │
                                ▼
                          Request proceeds
                          to Step 3
```

---

### Step 3: Cache Lookup (HTTP Mode Only)

**Module:** `api/query_cache.py`

```
  Incoming Question + Mode
        │
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                    CACHE LOOKUP                              │
  │                                                             │
  │   Cache Key = (question_text, mode)                         │
  │                                                             │
  │   ┌──────────────────────────────────────────────────┐     │
  │   │            In-Memory Cache Store                  │     │
  │   │                                                   │     │
  │   │   ┌─────────┬─────────┬─────────┬─────────┐     │     │
  │   │   │ Entry 1 │ Entry 2 │ Entry 3 │ Entry N │     │     │
  │   │   │ Q1+full │ Q2+sql  │ Q3+full │ QN+full │     │     │
  │   │   │ TTL: 5m │ TTL: 5m │ TTL: 5m │ TTL: 5m │     │     │
  │   │   └─────────┴─────────┴─────────┴─────────┘     │     │
  │   │                                                   │     │
  │   │   Max Size: 256 entries                           │     │
  │   │   Eviction: LRU (Least Recently Used)             │     │
  │   │   Thread Safety: threading.Lock                   │     │
  │   └──────────────────────────────────────────────────┘     │
  │                                                             │
  └─────────────────────────┬───────────────────────────────────┘
                            │
                  ┌─────────┴─────────┐
                  │                   │
                  ▼                   ▼
            ┌──────────┐        ┌──────────┐
            │  CACHE   │        │  CACHE   │
            │  HIT     │        │  MISS    │
            │          │        │          │
            │ Return   │        │ Continue │
            │ cached   │        │ to Step  │
            │ result   │        │ 4        │
            │ NOW      │        │          │
            └──────────┘        └──────────┘
```

---

### Step 4: Retrieval Pipeline — Building Context

**Module:** `retrieval/context_retriever.py` + 6 sub-retrievers

This is the **most critical step**. Instead of sending the entire database schema to the LLM (which would overwhelm small local models), the system retrieves only the **relevant subset** of knowledge for each question.

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
  │   │  │  Output: fact tables │──► Contract, CustomerContract  │ │
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
  │   │  │   Customer, Contract,│                                │ │
  │   │  │   CustomerContract]  │                                │ │
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
  │   │  .facts         = ["Contract", "CustomerContract"]       │ │
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
  │   │  "خرید"     ──► CustomerContract                │    │
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

**Module:** `prompt_engine/builder.py` + `prompt_engine/templates.py`

The `PromptBuilder.build()` method assembles a single structured prompt from 7 labeled sections:

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    PROMPT ASSEMBLY PROCESS                      │
  │                                                                 │
  │   ┌─────────────────────────────────────────────────────┐      │
  │   │                                                     │      │
  │   │  Section 1: SYSTEM PROMPT                           │      │
  │   │  ────────────────────────                           │      │
  │   │  "You are an expert Microsoft SQL Server           │      │
  │   │   query generator..."                               │      │
  │   │                                                     │      │
  │   │  Source: prompts/system_prompt.md                   │      │
  │   │                                                     │      │
  │   ├─────────────────────────────────────────────────────┤      │
  │   │                                                     │      │
  │   │  Section 2: BUSINESS RULES                          │      │
  │   │  ───────────────────────                            │      │
  │   │  "Ring names must use full formal name..."          │      │
  │   │  "Persian year starts from Farvardin..."            │      │
  │   │                                                     │      │
  │   │  Source: RuleRetriever (Step 4.4)                   │      │
  │   │                                                     │      │
  │   ├─────────────────────────────────────────────────────┤      │
  │   │                                                     │      │
  │   │  Section 3: DATABASE SCHEMA                         │      │
  │   │  ────────────────────────                           │      │
  │   │  Table: Ring                                        │      │
  │   │    Description: Trading halls on the exchange       │      │
  │   │    Columns:                                         │      │
  │   │      - ID: Surrogate primary key                   │      │
  │   │      - Name: Full trading hall name                 │      │
  │   │                                                     │      │
  │   │  Source: SchemaRegistry.build_schema_context()      │      │
  │   │  (ONLY relevant tables — not full database)         │      │
  │   │                                                     │      │
  │   ├─────────────────────────────────────────────────────┤      │
  │   │                                                     │      │
  │   │  Section 4: RELATIONSHIPS                           │      │
  │   │  ──────────────────────                             │      │
  │   │  JOIN [Auction_Dim].[Ring] ON                       │      │
  │   │    [Auction_Fact].[Contract].[RingID] =             │      │
  │   │    [Auction_Dim].[Ring].[ID]                        │      │
  │   │                                                     │      │
  │   │  Source: RelationshipRetriever (Step 4.3)           │      │
  │   │                                                     │      │
  │   ├─────────────────────────────────────────────────────┤      │
  │   │                                                     │      │
  │   │  Section 5: DETECTED FILTERS                        │      │
  │   │  ─────────────────────────                          │      │
  │   │  Ring: تالار پتروشیمی                              │      │
  │   │  PersianYear: 1402                                  │      │
  │   │                                                     │      │
  │   │  Source: ValueRetriever (Step 4.6)                  │      │
  │   │                                                     │      │
  │   ├─────────────────────────────────────────────────────┤      │
  │   │                                                     │      │
  │   │  Section 6: EXAMPLES                                │      │
  │   │  ────────────────                                   │      │
  │   │  Question: فروش تالار فلزات در ۱۴۰۱                │      │
  │   │  SQL: SELECT TOP 100 SUM(c.TotalPrice) ...          │      │
  │   │                                                     │      │
  │   │  Source: ExampleRetriever (Step 4.5)                │      │
  │   │                                                     │      │
  │   ├─────────────────────────────────────────────────────┤      │
  │   │                                                     │      │
  │   │  Section 7: USER QUESTION                           │      │
  │   │  ───────────────────────                            │      │
  │   │  فروش ماهانه تالار پتروشیمی در 1402                │      │
  │   │                                                     │      │
  │   │  Source: Original user input                        │      │
  │   │                                                     │      │
  │   └─────────────────────────────────────────────────────┘      │
  │                                                                 │
  │                        │                                        │
  │                        ▼                                        │
  │                                                                 │
  │              ┌──────────────────────┐                           │
  │              │   SINGLE PROMPT      │                           │
  │              │   STRING             │                           │
  │              │                      │                           │
  │              │   Sent to LLM in     │                           │
  │              │   Step 6             │                           │
  │              └──────────────────────┘                           │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

**Why this matters:** By scoping the prompt to only relevant tables and rules, even small 8B–20B parameter models can generate accurate SQL. Without scoping, the full schema would overwhelm the model's context window.

---

### Step 6: LLM Generation

**Module:** `llm/wizard_llm.py` → OpenAI-compatible API

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
  │   │  FROM [Auction_Fact].[Contract] c                       │  │
  │   │  JOIN [Auction_Dim].[Customer] cc ON c.CustID = cc.ID   │  │
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
  │   │  FROM [Auction_Fact].[Contract] c                       │  │
  │   │  JOIN [Auction_Dim].[Customer] cc ON c.CustID = cc.ID   │  │
  │   │                                                         │  │
  │   └─────────────────────────────────────────────────────────┘  │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

---

### Step 8: SQL Validation (Security Check)

**Module:** `security/sql_guard.py` → `validate_sql()` function

```
  Cleaned SQL
       │
       ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    SQL VALIDATION PIPELINE                      │
  │                                                                 │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  CHECK 1: SQL is not empty                              │  │
  │   │                                                         │  │
  │   │  ┌─────────┐                                            │  │
  │   │  │ Empty?  │──YES──► RAISE ERROR: "Empty SQL"          │  │
  │   │  └────┬────┘                                            │  │
  │   │       │ NO                                               │  │
  │   │       ▼                                                  │  │
  │   └─────────────────────────────────────────────────────────┘  │
  │                                                                 │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  CHECK 2: No forbidden keywords                        │  │
  │   │                                                         │  │
  │   │  BLOCKED KEYWORDS:                                      │  │
  │   │  ┌─────────────────────────────────────────────────┐   │  │
  │   │  │ DELETE │ UPDATE │ INSERT │ DROP   │ ALTER       │   │  │
  │   │  │ TRUNCATE│ MERGE  │ EXEC   │EXECUTE │ XP_  │ SP_│   │  │
  │   │  └─────────────────────────────────────────────────┘   │  │
  │   │                                                         │  │
  │   │  ┌──────────┐                                           │  │
  │   │  │ Found?   │──YES──► RAISE ERROR: "Forbidden: DELETE" │  │
  │   │  └────┬─────┘                                           │  │
  │   │       │ NO                                               │  │
  │   │       ▼                                                  │  │
  │   └─────────────────────────────────────────────────────────┘  │
  │                                                                 │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  CHECK 3: Starts with SELECT or WITH                   │  │
  │   │                                                         │  │
  │   │  ┌──────────────────────┐                               │  │
  │   │  │ SELECT ... or WITH ? │──NO──► RAISE ERROR:           │  │
  │   │  └──────────┬───────────┘       "Only SELECT/CTE"      │  │
  │   │             │ YES                                        │  │
  │   │             ▼                                            │  │
  │   └─────────────────────────────────────────────────────────┘  │
  │                                                                 │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  CHECK 4: No system catalogue access                   │  │
  │   │                                                         │  │
  │   │  BLOCKED:                                               │  │
  │   │  • INFORMATION_SCHEMA                                   │  │
  │   │  • SYS. references                                      │  │
  │   │                                                         │  │
  │   │  ┌────────────────┐                                      │  │
  │   │  │ Found?         │──YES──► RAISE ERROR                 │  │
  │   │  └───────┬────────┘                                      │  │
  │   │          │ NO                                            │  │
  │   │          ▼                                               │  │
  │   └─────────────────────────────────────────────────────────┘  │
  │                                                                 │
  │   ┌─────────────────────────────────────────────────────────┐  │
  │   │  CHECK 5: No LIMIT clause                              │  │
  │   │                                                         │  │
  │   │  ┌──────────┐                                           │  │
  │   │  │ LIMIT ?  │──YES──► RAISE ERROR:                     │  │
  │   │  └────┬─────┘       "LIMIT is not valid T-SQL"         │  │
  │   │       │ NO                                               │  │
  │   │       ▼                                                  │  │
  │   │  ┌──────────────┐                                        │  │
  │   │  │   ALL PASS   │                                        │  │
  │   │  │   ─────────  │                                        │  │
  │   │  │   Continue   │                                        │  │
  │   │  │   to Step 9  │                                        │  │
  │   │  └──────────────┘                                        │  │
  │   └─────────────────────────────────────────────────────────┘  │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

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
  │   │  │  pool_pre_ping  = True  (verify before use)      │   │  │
  │   │  │  fast_executemany = True (batch optimization)     │   │  │
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
  │   │     └─ Against SQL Server via ODBC Driver 17            │  │
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

**Module:** `logs/logger.py` + `logs/query_log.py`

```
  Every Query Gets Logged:
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │   {                                                            │
  │     "timestamp": "2026-06-27T14:22:57",                       │
  │     "question": "فروش ماهانه تالار پتروشیمی در 1402",         │
  │     "generated_sql": "SELECT TOP 1000 d.PersianMonthName...",  │
  │     "model_name": "openai:gpt-oss-20:F16",                    │
  │     "status": "SUCCESS",                                       │
  │     "row_count": 12,                                           │
  │     "execution_time_seconds": 3.456,                           │
  │     "error_message": null,                                     │
  │     "excel_file": "exports/result_20260627_142257.xlsx"        │
  │   }                                                            │
  │                                                                 │
  │   Written to: logs/query_log_YYYYMMDD.jsonl                    │
  │   Format: Rotating JSONL (auto-rotation)                       │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

---

## 4. Module Breakdown

### 4.1 Directory Structure

```
local-sql-agent/
│
├── app.py                     # CLI entry point (interactive REPL)
├── config.py                  # Environment-based configuration (Settings singleton)
│
├── api/                       # FastAPI HTTP service layer
│   ├── server.py              #   App factory, endpoints, middleware registration
│   ├── runner.py              #   Cache-aware query orchestrator
│   ├── query_cache.py         #   Thread-safe TTL + LRU cache
│   ├── models.py              #   Pydantic request/response models
│   ├── errors.py              #   Error hierarchy → HTTP status mapping
│   ├── middleware.py          #   Request ID, rate limiting, concurrency control
│   └── health.py              #   /health endpoint (DB + LLM endpoint probe)
│
├── retrieval/                 # Modular retrieval pipeline (6 retrievers)
│   ├── context_retriever.py   #   Orchestrator → single RetrievalContext
│   ├── entity_retriever.py    #   Dimension table detection
│   ├── fact_retriever.py      #   Fact table detection
│   ├── relationship_retriever.py  # JOIN clause generation
│   ├── rule_retriever.py      #   Business rule injection
│   ├── value_retriever.py     #   Filter value extraction
│   └── example_retriever.py   #   Tag-scored few-shot selection
│
├── prompt_engine/             # Prompt construction
│   ├── builder.py             #   PromptBuilder.build() — assembles final prompt
│   └── templates.py           #   PROMPT_TEMPLATE — the prompt skeleton
│
├── llm/                       # LLM integration
│   ├── base.py                #   LLMBackend abstract base class
│   ├── wizard_llm.py          #   OpenAI-compatible client with retry + back-off
│   └── sql_agent.py           #   Generate → clean → validate → auto-correct loop
│
├── security/                  # SQL safety
│   └── sql_guard.py           #   clean_sql, validate_sql, ensure_top
│
├── database/                  # Database connectivity
│   ├── connection.py          #   SQLAlchemy engine singleton (lazy init)
│   ├── executor.py            #   Execute SQL → DataFrame (timeout + row cap)
│   ├── schema_inspector.py    #   Introspect live database schema
│   └── schema_inspector_cli.py  # CLI for schema inspection
│
├── knowledge/                 # Domain knowledge (edit to extend)
│   ├── aliases.py             #   Trading hall Persian aliases
│   ├── business_rules.py      #   Domain rules per topic
│   ├── entities.py            #   Dimension entity catalog
│   ├── examples.py            #   22+ few-shot NLQ→SQL pairs
│   ├── metrics.py             #   35+ named metrics with SQL expressions
│   └── config_loader.py       #   YAML config loader
│
├── schema_data/               # Database schema definitions
│   ├── tables.py              #   Table descriptions (bilingual)
│   ├── columns.py             #   Column allowlist per table
│   ├── relationships.py       #   FK → JOIN SQL map
│   ├── registry.py            #   SchemaRegistry (renders schema blocks)
│   └── retriever.py           #   TF-IDF bigram fallback engine
│
├── exporters/                 # Result export
│   └── excel_exporter.py      #   DataFrame → timestamped Excel file
│
├── logs/                      # Structured logging
│   ├── logger.py              #   Rotating JSONL logger
│   └── query_log.py           #   QueryLog data model
│
├── core/                      # Shared data models
│   ├── models.py              #   RetrievalContext, SQLGenerationResult
│   └── analyze_misses.py      #   Offline retrieval miss diagnostics
│
├── prompts/                   # Prompt templates
│   ├── system_prompt.md       #   Core system instructions for the LLM
│   ├── few_shots.md           #   Additional few-shot examples
│   └── business_glossary.md   #   Domain glossary
│
├── scripts/                   # Utility scripts
│   ├── create_db.py           #   Database setup
│   └── analyze_misses.py      #   Retrieval diagnostics
│
├── tests/                     # 427+ unit + integration tests
│
├── .env.example               # Environment variable template
├── requirements.txt           # Python dependencies
└── README.md                  # Project README
```

### 4.2 Module Dependency Map

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    MODULE DEPENDENCY MAP                         │
  │                                                                 │
  │   app.py ──────────► sql_agent.py ──► wizard_llm.py              │
  │      │                    │                  │                   │
  │      │                    │                  └──► OpenAI API     │
  │      │                    │                                       │
  │      │                    ├──► context_retriever.py               │
  │      │                    │         │                             │
  │      │                    │         ├──► entity_retriever.py      │
  │      │                    │         ├──► fact_retriever.py        │
  │      │                    │         ├──► relationship_retriever.py│
  │      │                    │         ├──► rule_retriever.py        │
  │      │                    │         ├──► example_retriever.py     │
  │      │                    │         └──► value_retriever.py       │
  │      │                    │                                       │
  │      │                    ├──► prompt_engine/builder.py           │
  │      │                    │         │                             │
  │      │                    │         └──► schema_data/registry.py  │
  │      │                    │                                       │
  │      │                    ├──► security/sql_guard.py              │
  │      │                    │                                       │
  │      │                    └──► database/executor.py               │
  │      │                              │                             │
  │      │                              └──► database/connection.py   │
  │      │                                        │                   │
  │      │                                        └──► SQL Server     │
  │      │                                                            │
  │      ├──► exporters/excel_exporter.py                             │
  │      └──► logs/logger.py                                          │
  │                                                                 │
  │   server.py ──────► runner.py ──► (same as sql_agent.py chain)   │
  │      │                    │                                      │
  │      │                    └──► query_cache.py                    │
  │      │                                                            │
  │      └──► middleware.py                                           │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
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
  │   ┌─────────────────────────────────────────────────────┐      │
  │   │  LAYER 1: clean_sql()                               │      │
  │   │  ──────────────────                                 │      │
  │   │                                                     │      │
  │   │  • Extract from markdown fences                     │      │
  │   │  • Remove prose preamble                            │      │
  │   │  • Convert LIMIT → TOP                              │      │
  │   │  • Fix TOP DISTINCT order                           │      │
  │   │                                                     │      │
  │   └─────────────────────┬───────────────────────────────┘      │
  │                         │                                       │
  │                         ▼                                       │
  │   ┌─────────────────────────────────────────────────────┐      │
  │   │  LAYER 2: validate_sql()                            │      │
  │   │  ───────────────────                                │      │
  │   │                                                     │      │
  │   │  ┌─────────────────────────────────────────────┐   │      │
  │   │  │  BLOCKED KEYWORDS:                          │   │      │
  │   │  │  DELETE │ UPDATE │ INSERT │ DROP             │   │      │
  │   │  │  ALTER  │TRUNCATE│ MERGE  │ EXEC │EXECUTE   │   │      │
  │   │  │  XP_    │ SP_                                 │   │      │
  │   │  └─────────────────────────────────────────────┘   │      │
  │   │                                                     │      │
  │   │  ┌─────────────────────────────────────────────┐   │      │
  │   │  │  BLOCKED PATTERNS:                          │   │      │
  │   │  │  • INFORMATION_SCHEMA                       │   │      │
  │   │  │  • SYS. references                          │   │      │
  │   │  │  • Stacked queries (SQL injection)           │   │      │
  │   │  │  • LIMIT clause (MySQL syntax)               │   │      │
  │   │  └─────────────────────────────────────────────┘   │      │
  │   │                                                     │      │
  │   │  ALLOWED:                                           │      │
  │   │  • SELECT ... (read-only)                           │      │
  │   │  • WITH ... SELECT (CTE)                            │      │
  │   │                                                     │      │
  │   └─────────────────────┬───────────────────────────────┘      │
  │                         │                                       │
  │                         ▼                                       │
  │   ┌─────────────────────────────────────────────────────┐      │
  │   │  LAYER 3: ensure_top()                              │      │
  │   │  ──────────────────                                 │      │
  │   │                                                     │      │
  │   │  If no TOP clause present:                          │      │
  │   │  Inject TOP 1000 (configurable)                     │      │
  │   │                                                     │      │
  │   │  Safety net: guarantees no unbounded result sets    │      │
  │   │                                                     │      │
  │   └─────────────────────┬───────────────────────────────┘      │
  │                         │                                       │
  │                         ▼                                       │
  │   ┌─────────────────────────────────────────────────────┐      │
  │   │  LAYER 4: execute_sql()                             │      │
  │   │  ───────────────────                                │      │
  │   │                                                     │      │
  │   │  • SET LOCK_TIMEOUT (prevent indefinite blocking)   │      │
  │   │  • fetchmany(MAX_ROWS_RETURNED) — hard row cap      │      │
  │   │  • All DB errors wrapped in RuntimeError            │      │
  │   │                                                     │      │
  │   └─────────────────────┬───────────────────────────────┘      │
  │                         │                                       │
  │                         ▼                                       │
  │                    Safe Result Set                               │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

### Security Guarantees Summary

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │   SECURITY GUARANTEES                                          │
  │                                                                 │
  │   ┌───────────────────────────────────────────────────────┐    │
  │   │                                                       │    │
  │   │   ✓ Only SELECT queries executed (read-only)          │    │
  │   │                                                       │    │
  │   │   ✓ No data modification possible                     │    │
  │   │                                                       │    │
  │   │   ✓ Row limits enforced at SQL level (TOP)            │    │
  │   │     AND application level (fetchmany)                 │    │
  │   │                                                       │    │
  │   │   ✓ No hardcoded credentials (env vars only)          │    │
  │   │                                                       │    │
  │   │   ✓ SQL injection blocked at validation time          │    │
  │   │                                                       │    │
  │   │   ✓ Zero external API calls (100% local)              │    │
  │   │                                                       │    │
  │   └───────────────────────────────────────────────────────┘    │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

---

## 7. Configuration

All configuration is read from **environment variables** (or a `.env` file):

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                                                                 │
  │                    CONFIGURATION MAP                            │
  │                                                                 │
  │   ┌────────────────────┬────────────────┬─────────────────┐   │
  │   │  VARIABLE          │  DEFAULT       │  DESCRIPTION    │   │
  │   ├────────────────────┼────────────────┼─────────────────┤   │
  │   │  OPENAI_BASE_URL│  api.openai.   │  OpenAI-compatible │   │
  │   │                 │  com/v1        │  endpoint          │   │
  │   ├─────────────────┼────────────────┼────────────────────┤   │
  │   │  OPENAI_MODEL   │  gpt-4o-mini   │  Model name        │   │
  │   ├─────────────────┼────────────────┼────────────────────┤   │
  │   │  OPENAI_API_KEY │  (required)    │  Endpoint API key  │   │
  │   ├─────────────────┼────────────────┼────────────────────┤   │
  │   │  DB_CONNECTION_URL │  (required) │  SQLAlchemy        │   │
  │   │                 │                │  connection        │   │
  │   ├────────────────────┼────────────────┼─────────────────┤   │
  │   │  QUERY_TIMEOUT_    │  60            │  Max query time │   │
  │   │  SECONDS           │                │  (seconds)      │   │
  │   ├────────────────────┼────────────────┼─────────────────┤   │
  │   │  MAX_ROWS_RETURNED │  1000          │  Hard row cap   │   │
  │   ├────────────────────┼────────────────┼─────────────────┤   │
  │   │  CACHE_TTL_SECONDS │  300           │  Cache TTL      │   │
  │   │                    │                │  (0=disabled)   │   │
  │   ├────────────────────┼────────────────┼─────────────────┤   │
  │   │  CACHE_MAX_SIZE    │  256           │  Max cached     │   │
  │   │                    │                │  entries        │   │
  │   ├────────────────────┼────────────────┼─────────────────┤   │
  │   │  LOG_DIR           │  logs          │  Log directory  │   │
  │   ├────────────────────┼────────────────┼─────────────────┤   │
  │   │  EXPORT_DIR        │  exports       │  Export dir     │   │
  │   └────────────────────┴────────────────┴─────────────────┘   │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

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
pip install -r requirements.txt

# Step 2: Configure environment
cp .env.example .env
# Edit .env with your database URL, OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY

# Step 3: Run the interactive CLI
python app.py
```

### 8.3 HTTP API Mode — Quick Start

```bash
# Start the FastAPI server
uvicorn api.server:app --host 0.0.0.0 --port 8000

# Send a query via HTTP
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "فروش ماهانه تالار پتروشیمی در 1402", "mode": "full"}'
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
  │                    TEST SUITE (427+ tests)                      │
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
  │   CI: GitHub Actions (Python 3.13)                             │
  │                                                                 │
  └─────────────────────────────────────────────────────────────────┘
```

---

*This document describes the Local SQL Agent system as of June 2026.*
