"""Interactive REPL for AuctionDB (raw Ollama HTTP, no LangChain).

Usage (from repo root)::

    python -m runners.langsql
"""

from __future__ import annotations

import re
import sys

import pandas as pd
from sqlalchemy import text

from sql_agent.config import Settings, load_env_file
from sql_agent.db import get_engine
from sql_agent.llm import call_ollama
from sql_agent.validator import validate_sql


# ---------------------------------------------------------------------------
# AuctionDB system prompt (domain-specific — kept here, not in prompts.py)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
You are an expert SQL Server generator.
Your ONLY task is to generate SQL Server queries.

IMPORTANT SQL SERVER RULES:
- Output ONLY raw SQL
- Never use LIMIT — use TOP instead
- No explanation, no markdown, no ```sql
- Use SQL Server syntax only
- Always use schema-qualified names
- Never use DELETE, UPDATE, INSERT, DROP, ALTER
- Use TOP 100 unless user specifies another limit
- Never use LIMIT, QUALIFY, ILIKE, DISTINCT ON, SERIAL, RETURNING
- For ranking queries use ROW_NUMBER() with CTE
- Always use SQL Server bracket notation: [schema].[table]
- When DISTINCT and TOP are used together, always write: SELECT DISTINCT TOP N ...
- For Top N per group queries, always use a CTE with ROW_NUMBER() OVER (PARTITION BY ...)

Database schema:

[Auction_Dim].[Customer]
  ID, CreationDate, LastModificationDate, SourceDatabase_ID, PersianDate,
  Customer_OriginalPK, Broker_OriginalPK, Name, TypeID, TypeName,
  NationalID, IsActive

[Auction_Dim].[Ring]
  ID, SourceDatabase_ID, CreationDate, LastModificationDate,
  Ring_OriginalPK, Name, PersianDate

[Auction_Fact].[Contract]
  ID, Price, TotalPrice, CustomerContractCount, Ring_ID, Date_ID,
  Supplier_ID, BuyerBroker_ID, SellerBroker_ID, ContractStatus_ID

[Auction_Fact].[CustomerContract]
  ID, Quantity, TotalPrice, BuyerCustomer_ID, Contract_ID, Date_ID,
  Ring_ID, Supplier_ID, BuyerBroker_ID, SellerBroker_ID, ContractStatus_ID

[general_Dim].[Date]
  ID, RealDate, PersianDate, PersianYear, PersianSeason, PersianSeasonName,
  PersianMonth, PersianMonthName, PersianDayOfMonth, PersianWeekOfYear,
  PersianWeekRange, PersianDayOfWeek, PersianDayOfWeekName

Relationships:
  [Auction_Fact].[Contract].Ring_ID           = [Auction_Dim].[Ring].ID
  [Auction_Fact].[CustomerContract].BuyerCustomer_ID = [Auction_Dim].[Customer].ID
  [Auction_Fact].[CustomerContract].Contract_ID     = [Auction_Fact].[Contract].ID
  [Auction_Fact].[Contract].Date_ID           = [general_Dim].[Date].ID
  [Auction_Fact].[CustomerContract].Date_ID   = [general_Dim].[Date].ID
"""


def _repair_sql(sql: str) -> str:
    sql = re.sub(
        r"(?i)SELECT\s+TOP\s+(\d+)\s+DISTINCT",
        r"SELECT DISTINCT TOP \1",
        sql,
    )
    m = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
    if m:
        n   = m.group(1)
        sql = re.sub(r"LIMIT\s+\d+", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"(?i)^SELECT\s+", f"SELECT TOP {n} ", sql, count=1)
    return sql.strip()


def _execute(sql: str, cfg: Settings) -> pd.DataFrame:
    engine = get_engine(cfg.sqlserver_uri())
    with engine.connect() as conn:
        conn.execute(text("SET LOCK_TIMEOUT 30000"))
        result  = conn.execute(text(sql))
        rows    = result.fetchall()
        columns = list(result.keys())
    return pd.DataFrame(rows, columns=columns)


def _repl(cfg: Settings) -> None:
    print("=" * 60)
    print("Auction NLQ Engine  (type 'exit' to quit)")
    print("=" * 60)
    while True:
        try:
            question = input("\nAsk Question: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question.lower() in ("exit", "quit", ""):
            break
        try:
            full_prompt = f"{_SYSTEM_PROMPT}\n\nUser Question:\n{question}\n\nSQL:\n"
            raw = call_ollama(full_prompt, base_url=cfg.ollama_base_url, model=cfg.ollama_model)
            print("\nRAW MODEL RESPONSE:")
            print(raw)
            sql = _repair_sql(raw)
            sql = re.sub(r"```sql", "", sql, flags=re.IGNORECASE)
            sql = re.sub(r"```", "", sql).strip()
            upper = sql.upper()
            if upper.startswith("WITH"):
                pass
            elif "SELECT" in upper:
                sql = sql[upper.find("SELECT"):]
            validate_sql(sql)
            print("\nFINAL SQL:")
            print(sql)
            df = _execute(sql, cfg)
            print("\n" + "=" * 60)
            print("QUERY RESULT")
            print("=" * 60)
            if df.empty:
                print("No data found")
            else:
                print(df.head(20).to_string(index=False))
                print(f"\nReturned Rows: {len(df)}")
        except Exception as exc:
            print("\nERROR:", exc)


def main() -> None:
    load_env_file()
    cfg = Settings()
    cfg.validate()
    _repl(cfg)


if __name__ == "__main__":
    main()
