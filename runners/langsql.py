"""runners/langsql.py — interactive REPL for AuctionNlq database (raw Ollama HTTP).

Usage (from repo root):
    python -m runners.langsql
"""

import os
import re
import requests
import pandas as pd
from urllib.parse import quote_plus
from sqlalchemy import create_engine, text


# =========================================================
# CONFIG
# No hardcoded fallbacks for sensitive values.
# Copy .env.example to .env and fill in your values.
# =========================================================

OLLAMA_URL   = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") + "/api/generate"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL")

DB_SERVER  = os.getenv("DB_SERVER")
DB_NAME    = os.getenv("DB_NAME")
DB_USER    = os.getenv("DB_USER")
DB_PORT    = os.getenv("DB_PORT", "1433")
DB_DRIVER  = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
DB_TRUSTED = os.getenv("DB_TRUSTED_CONNECTION", "").lower()
DB_PASS    = os.getenv("DB_PASSWORD", "")


def _validate_env() -> None:
    """Raise ValueError if any required environment variable is missing."""
    required = [
        ("OLLAMA_MODEL", OLLAMA_MODEL),
        ("DB_SERVER",    DB_SERVER),
        ("DB_NAME",      DB_NAME),
        ("DB_USER",      DB_USER),
    ]
    if DB_TRUSTED != "yes":
        required.append(("DB_PASSWORD", DB_PASS))
    missing = [name for name, val in required if not val]
    if missing:
        raise ValueError(
            f"❌ Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in your values."
        )


def _build_connection_url() -> str:
    driver_enc = quote_plus(DB_DRIVER)
    user_enc   = quote_plus(DB_USER)
    if DB_TRUSTED == "yes":
        return (
            f"mssql+pyodbc://{user_enc}@{DB_SERVER}:{DB_PORT}/{DB_NAME}"
            f"?driver={driver_enc}&trusted_connection=yes"
        )
    pass_enc = quote_plus(DB_PASS)
    return (
        f"mssql+pyodbc://{user_enc}:{pass_enc}"
        f"@{DB_SERVER}:{DB_PORT}/{DB_NAME}"
        f"?driver={driver_enc}"
    )


_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            _build_connection_url(),
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return _engine


# =========================================================
# SYSTEM PROMPT
# =========================================================

SYSTEM_PROMPT = """
You are an expert SQL Server generator.

Your ONLY task is to generate SQL Server queries.

IMPORTANT SQL SERVER RULES:

- Output ONLY raw SQL
- Never use LIMIT
- SQL Server does not support LIMIT
- Always use TOP for row limiting
- No explanation
- No markdown
- No ```sql
- No natural language
- Use SQL Server syntax
- Always use schema-qualified names
- Never use DELETE, UPDATE, INSERT, DROP, ALTER
- Use TOP 100 unless user specifies another limit
- Never hallucinate table names
- Never hallucinate column names
- Use proper JOIN conditions
- Use Microsoft SQL Server syntax only.
- Never use LIMIT.
- Always use TOP.
- Always use aliases for tables.
- Never use SELECT *.
- Select only required columns.
- SQL Server only.
    Never use:
    LIMIT
    QUALIFY
    ILIKE
    DISTINCT ON
    SERIAL
    RETURNING
    These are not supported by SQL Server.
    For ranking queries use:
    ROW_NUMBER()
    WITH CTE
    Example:
    WITH Ranked AS
    (
        SELECT
            CustomerID,
            ROW_NUMBER() OVER (
                ORDER BY TotalPrice DESC
            ) AS rn
        FROM ...
    )
    SELECT *
    FROM Ranked
    WHERE rn <= 5
- Always use SQL Server bracket notation.
    Correct:
    [Auction_Fact].[CustomerContract]

    Incorrect:
    Auction_Fact.CustomerContract

- When DISTINCT and TOP are used together, ALWAYS write:

    SELECT DISTINCT TOP N ...

    Correct:
    SELECT DISTINCT TOP 100 d.PersianMonthName
    FROM [general_Dim].[Date] d

    Incorrect:
    SELECT TOP 100 DISTINCT d.PersianMonthName
    FROM [general_Dim].[Date] d

    DISTINCT must always appear before TOP.

    Never write:
    general_Dim.Date.PersianMonthName

    Always write:
    d.PersianMonthName

- For Top N per group queries:
    Always use a CTE.
    Example:
    WITH Ranked AS
    (
        SELECT
            ...,
            ROW_NUMBER() OVER(
                PARTITION BY GroupColumn
                ORDER BY Measure DESC
            ) AS rn
        FROM ...
    )

    SELECT *
    FROM Ranked
    WHERE rn <= N
- Never use QUALIFY.
- Never omit the WITH clause.

Database schema:

[Auction_Dim].[Customer]
Columns:
ID
CreationDate
LastModificationDate
SourceDatabase_ID
PersianDate
Customer_OriginalPK
Broker_OriginalPK
Name
TypeID
TypeName
NationalID
IsActive

[Auction_Dim].[Ring]
Columns:
ID
SourceDatabase_ID
CreationDate
LastModificationDate
Ring_OriginalPK
Name
PersianDate

[Auction_Fact].[Contract]
Columns:
ID
Price
TotalPrice
CustomerContractCount
Ring_ID
Date_ID
Supplier_ID
BuyerBroker_ID
SellerBroker_ID
ContractStatus_ID

[Auction_Fact].[CustomerContract]
Columns:
ID
Quantity
TotalPrice
BuyerCustomer_ID
Contract_ID
Date_ID
Ring_ID
Supplier_ID
BuyerBroker_ID
SellerBroker_ID
ContractStatus_ID

[general_Dim].[Date]
Columns:
ID
RealDate
PersianDate
PersianYear
PersianSeason
PersianSeasonName
PersianMonth
PersianMonthName
PersianDayOfMonth
PersianWeekOfYear
PersianWeekRange
PersianDayOfWeek
PersianDayOfWeekName

Relationships:

[Auction_Fact].[Contract].Ring_ID
= [Auction_Dim].[Ring].ID

[Auction_Fact].[CustomerContract].BuyerCustomer_ID
= [Auction_Dim].[Customer].ID

[Auction_Fact].[CustomerContract].Contract_ID
= [Auction_Fact].[Contract].ID

[Auction_Fact].[Contract].Date_ID
= [general_Dim].[Date].ID

[Auction_Fact].[CustomerContract].Date_ID
= [general_Dim].[Date].ID
"""

# =========================================================
# SQL VALIDATION
# =========================================================

FORBIDDEN_KEYWORDS = ["DELETE", "UPDATE", "DROP", "ALTER", "INSERT", "TRUNCATE", "EXEC", "MERGE"]


def validate_sql(sql: str):
    sql_upper = sql.upper().strip()
    if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
        raise Exception("Only SELECT or CTE-based SELECT statements are allowed")
    for keyword in FORBIDDEN_KEYWORDS:
        if keyword in sql_upper:
            raise Exception(f"Forbidden SQL detected: {keyword}")


def repair_sql(sql: str) -> str:
    sql = re.sub(r"SELECT\s+TOP\s+(\d+)\s+DISTINCT", r"SELECT DISTINCT TOP \1", sql, flags=re.IGNORECASE)
    limit_match = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
    if limit_match:
        limit_value = limit_match.group(1)
        sql = re.sub(r"LIMIT\s+\d+", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"SELECT", f"SELECT TOP {limit_value}", sql, count=1, flags=re.IGNORECASE)
    return sql.strip()


# =========================================================
# OLLAMA REQUEST
# =========================================================

def generate_sql(question: str) -> str:
    full_prompt = f"{SYSTEM_PROMPT}\n\nUser Question:\n{question}\n\nSQL:\n"
    for _ in range(3):
        response = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": full_prompt, "stream": False, "temperature": 0.1},
            timeout=60,
        )
        if response.status_code == 200:
            break
    else:
        raise Exception("Ollama unavailable after 3 retries")
    print("\nSTATUS CODE:", response.status_code)
    data = response.json()
    raw_response = data.get("response", "").strip()
    print("\nRAW MODEL RESPONSE:")
    print(raw_response)
    sql = raw_response
    sql = re.sub(r"```sql", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"```", "", sql)
    sql = sql.strip()
    upper_sql = sql.upper()
    if upper_sql.startswith("WITH"):
        pass
    elif "SELECT" in upper_sql:
        sql = sql[upper_sql.find("SELECT"):]
    else:
        raise Exception("No SELECT statement found in model response")
    sql = repair_sql(sql)
    validate_sql(sql)
    print("\nFINAL SQL SENT TO SQL SERVER:")
    print(sql)
    return sql


# =========================================================
# EXECUTE SQL
# =========================================================

def execute_sql(sql: str):
    with _get_engine().connect() as conn:
        conn.execute(text("SET LOCK_TIMEOUT 30000"))
        result  = conn.execute(text(sql))
        rows    = result.fetchall()
        columns = result.keys()
        return pd.DataFrame(rows, columns=columns)


# =========================================================
# MAIN LOOP
# =========================================================

def main():
    _validate_env()
    print("=" * 60)
    print("Auction NLQ Engine Started")
    print("=" * 60)
    while True:
        question = input("\nAsk Question (or exit): ")
        if question.lower() == "exit":
            break
        try:
            sql = generate_sql(question)
            print("\n" + "=" * 60)
            print("GENERATED SQL")
            print("=" * 60)
            print(sql)
            df = execute_sql(sql)
            print("\n" + "=" * 60)
            print("QUERY RESULT")
            print("=" * 60)
            if df.empty:
                print("No data found")
            else:
                print(df.head(20).to_string(index=False))
                print(f"\nReturned Rows: {len(df)}")
        except Exception as e:
            print("\n" + "=" * 60)
            print("ERROR")
            print("=" * 60)
            print(str(e))


if __name__ == "__main__":
    main()
