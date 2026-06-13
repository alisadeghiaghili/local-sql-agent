# Local SQL Agent

> Ask your SQL Server database a question in Persian or English.  
> Get back precise T-SQL — generated locally, executed securely, zero data leaves your machine.

[![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/Tests-427%2B-green)](tests/)

---

Most Text-to-SQL tools assume your data is in the cloud and your questions are in English. This project was built for the opposite: an on-premise SQL Server warehouse at the **Iran Mercantile Exchange**, where analysts ask questions in Persian, data is sensitive, and there is no budget for external APIs.

The result is a fully local NLQ engine — modular retrieval pipeline, SQL security guard, FastAPI service, and a knowledge base you can extend without touching engine code.

---

## In action

```bash
python app.py

Question: برترین مشتریان از نظر ارزش خرید در سال 1402 کدامند؟

══════════════════════════════════════════════════
GENERATED SQL
══════════════════════════════════════════════════
SELECT TOP 10
    c.Name,
    SUM(cc.TotalPrice) AS PurchaseValue
FROM [Auction_Fact].[CustomerContract] cc
JOIN [Auction_Dim].[Customer] c ON cc.BuyerCustomer_ID = c.ID
JOIN [Auction_Dim].[Date]     d ON cc.Date_ID = d.ID
WHERE d.PersianYear = 1402
GROUP BY c.Name
ORDER BY PurchaseValue DESC

Returned Rows: 10  |  Execution Time: 1.24s  |  Excel: exports/result_20260613_142257.xlsx
```

Or over HTTP:

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "فروش ماهانه تالار پتروشیمی در 1402", "mode": "full"}'
```

```json
{
  "question": "فروش ماهانه تالار پتروشیمی در 1402",
  "sql":    "SELECT TOP 1000 d.PersianMonthName, SUM(c.TotalPrice) AS TradeValue ...",
  "result": [{"PersianMonthName": "فروردین", "TradeValue": 48320000000}, ...],
  "row_count": 12,
  "status": "SUCCESS"
}
```

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
    └─ ValueRetriever         ring canonical name + Persian year
    │
    ▼
 PromptBuilder   →  schema + rules + examples, precisely scoped
    │
    ▼
 SQLAgent        →  generate → clean → validate → auto-correct
    │
    ▼
 SQLGuard        →  blocks DDL/DML, injection, rewrites LIMIT→TOP
    │
    ▼
 SQL Server      →  result set  →  Excel / CSV / JSON
```

Scoping the prompt — instead of dumping your full schema — is what makes locally-run 8B–20B models accurate enough for production.

---

## Quick start

**Requires:** Python 3.11+, [Ollama](https://ollama.com) on `localhost:11434`, SQL Server + ODBC Driver 17

```bash
# 1. Clone and install
git clone https://github.com/alisadeghiaghili/local-sql-agent.git
cd local-sql-agent
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Set at minimum:
#   DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/DB?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=yes
#   OLLAMA_MODEL=llama3

# 3. Pull a model
ollama pull llama3

# 4a. CLI
python app.py

# 4b. HTTP API
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

**→ Full tutorial (installation · first query · extending the domain · writing tests · diagnosing misses):**  
**[docs/tutorial.md](docs/tutorial.md)**

---

## License

**BUSL-1.1** — free for non-production use; converts to Apache 2.0 on 2029-01-01.  
See [`LICENSE`](LICENSE). Derivative works must retain attribution to this repository.

---

## Contributors

| | Role |
|---|---|
| [Ali Sadeghi Aghili](https://github.com/alisadeghiaghili) | Creator & Lead Engineer — engine, retrieval pipeline, API, security, tests |
| [Melika Bahmanabadi](https://github.com/MelikaBahmanabadi) | Domain Expert — trading hall aliases, business rules, metrics, few-shot examples |

Contributions welcome — open an issue before submitting a PR.

---

Built with [Ollama](https://ollama.com) · [FastAPI](https://fastapi.tiangolo.com) · [SQLAlchemy](https://sqlalchemy.org) · [scikit-learn](https://scikit-learn.org)
