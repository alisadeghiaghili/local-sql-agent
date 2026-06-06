import warnings
warnings.filterwarnings("ignore")

import argparse
import logging
import os
import re
import sys

from sqlalchemy import create_engine, text
from langchain_ollama import ChatOllama
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_core.messages import HumanMessage, SystemMessage


# =========================================================
# Logging
# =========================================================
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)


# =========================================================
# Environment Variables
# No hardcoded fallbacks for sensitive values.
# Copy .env.example to .env and fill in your values.
# =========================================================
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL")
DB_USER         = os.getenv("DB_USER")
DB_PASS         = os.getenv("DB_PASSWORD")
DB_HOST         = os.getenv("DB_SERVER")
DB_PORT         = os.getenv("DB_PORT", "1433")
DB_NAME         = os.getenv("DB_NAME")


def _validate_env() -> None:
    """Raise ValueError if any required environment variable is missing."""
    missing = [
        name for name, val in [
            ("OLLAMA_MODEL", OLLAMA_MODEL),
            ("DB_USER",      DB_USER),
            ("DB_PASSWORD",  DB_PASS),
            ("DB_SERVER",    DB_HOST),
            ("DB_NAME",      DB_NAME),
        ]
        if not val
    ]
    if missing:
        raise ValueError(
            f"❌ Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in your values."
        )


# =========================================================
# Global Cache
# =========================================================
_engine = None
_db     = None
_llm    = None


# =========================================================
# Allowed Tables
# =========================================================
ALLOWED_TABLES = [
    "[BI].[App].[Dictionary]",
    "[BI].[App].[Message]",
    "[BI].[App].[Permission]",
    "[BI].[App].[PermissionType]",
    "[BI].[App].[ReportLog]",
    "[BI].[App].[Sessions]",
    "[BI].[App].[SimaForms]",
    "[BI].[App].[StoredProcedureMapping]",
    "[BI].[App].[TaskLog]",
    "[BI].[App].[UserPermission]",
    "[BI].[App].[UserRecordLimit]",
    "[BI].[App].[Users]",
    "[BI].[App].[UserSystem]",
    "[BI].[Base].[Brokers]",
    "[BI].[Base].[City]",
    "[BI].[Base].[County]",
    "[BI].[Base].[Date]",
    "[BI].[Base].[DimDate]",
    "[BI].[Base].[Province]",
    "[BI].[Base].[RuralDistrict]",
    "[BI].[Base].[Section]",
    "[BI].[Base].[Village]",
    "[BI].[Compare].[Auction]",
    "[BI].[Csd].[Product]",
    "[BI].[Csd].[SymbolType]",
    "[BI].[Csd].[Warehouse]",
    "[BI].[dbo].[___BrokerDeleted_Karizma]",
    "[BI].[dbo].[__CSDSymbol]",
    "[BI].[dbo].[acBroker]",
    "[BI].[dbo].[RayvazCustomer]",
    "[BI].[dbo].[simadw]",
    "[BI].[Inv].[CustomerAssetsAggrigated]",
    "[BI].[Inv].[CustomerAssetsDetail]",
    "[BI].[Inv].[CustomerReceiptAggrigated]",
    "[BI].[Inv].[CustomerReceiptDetail]",
    "[BI].[Inx].[BrokerAuditIndexs]",
    "[BI].[Inx].[BrokerIndexValues]",
    "[BI].[MoneyLaund].[CustomerContract]",
    "[BI].[Phi].[FinanceForecast]",
    "[BI].[Phi].[ObligationOffers]",
    "[BI].[risk].[FinalTotalRisk]",
    "[BI].[Scrape].[IceAsset]",
    "[BI].[Scrape].[TgjuAsset]",
    "[BI].[Scrape].[TgjuDim]",
    "[BI].[Trading].[OptionTradesDetail]",
]


# =========================================================
# System Prompt
# =========================================================
SYSTEM_PROMPT = """You are a SQL Server query generator.

STRICT RULES:
1. Output ONLY a single SQL query. Nothing else.
2. No explanations. No markdown fences. No repetition.
3. Always use 3-part fully qualified table names: [BI].[Schema].[Table]
4. Always use SELECT TOP 100.
5. Only use tables from the AVAILABLE TABLES list below.
6. Use square brackets around all identifiers.
7. SQL Server syntax only. Never use LIMIT.
8. Never use DROP, DELETE, TRUNCATE, ALTER, UPDATE, INSERT, EXEC, MERGE.
9. Output the query ONCE only. Stop immediately after the query ends.

AVAILABLE TABLES:
[BI].[App].[Dictionary]
[BI].[App].[Message]
[BI].[App].[Permission]
[BI].[App].[PermissionType]
[BI].[App].[ReportLog]
[BI].[App].[Sessions]
[BI].[App].[SimaForms]
[BI].[App].[StoredProcedureMapping]
[BI].[App].[TaskLog]
[BI].[App].[UserPermission]
[BI].[App].[UserRecordLimit]
[BI].[App].[Users]
[BI].[App].[UserSystem]
[BI].[Base].[Brokers]
[BI].[Base].[City]
[BI].[Base].[County]
[BI].[Base].[Date]
[BI].[Base].[DimDate]
[BI].[Base].[Province]
[BI].[Base].[RuralDistrict]
[BI].[Base].[Section]
[BI].[Base].[Village]
[BI].[Compare].[Auction]
[BI].[Csd].[Product]
[BI].[Csd].[SymbolType]
[BI].[Csd].[Warehouse]
[BI].[dbo].[___BrokerDeleted_Karizma]
[BI].[dbo].[__CSDSymbol]
[BI].[dbo].[acBroker]
[BI].[dbo].[RayvazCustomer]
[BI].[dbo].[simadw]
[BI].[Inv].[CustomerAssetsAggrigated]
[BI].[Inv].[CustomerAssetsDetail]
[BI].[Inv].[CustomerReceiptAggrigated]
[BI].[Inv].[CustomerReceiptDetail]
[BI].[Inx].[BrokerAuditIndexs]
[BI].[Inx].[BrokerIndexValues]
[BI].[MoneyLaund].[CustomerContract]
[BI].[Phi].[FinanceForecast]
[BI].[Phi].[ObligationOffers]
[BI].[risk].[FinalTotalRisk]
[BI].[Scrape].[IceAsset]
[BI].[Scrape].[TgjuAsset]
[BI].[Scrape].[TgjuDim]
[BI].[Trading].[OptionTradesDetail]

EXAMPLE OUTPUT (output ONLY this format, nothing else):
SELECT TOP 100 [Description] FROM [BI].[App].[Users] WHERE [username] = 'aghazadeh'
"""


# =========================================================
# Build LLM
# =========================================================
def build_llm():
    global _llm
    if _llm is not None:
        return _llm
    logger.info("Loading LLM...")
    _llm = ChatOllama(
        model=OLLAMA_MODEL,
        base_url=OLLAMA_BASE_URL,
        temperature=0.0,
        top_p=0.8,
        num_predict=300,
        streaming=False,
    )
    logger.info(f"LLM loaded: {OLLAMA_MODEL}")
    return _llm


# =========================================================
# Build Engine
# =========================================================
def build_engine():
    global _engine
    if _engine is not None:
        return _engine
    logger.info("Creating SQLAlchemy engine...")
    from urllib.parse import quote_plus
    uri = (
        f"mssql+pyodbc://{quote_plus(DB_USER)}:{quote_plus(DB_PASS)}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        f"?driver=ODBC+Driver+17+for+SQL+Server"
    )
    _engine = create_engine(
        uri,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=3600,
        fast_executemany=True,
        echo=False,
    )
    logger.info("Engine created.")
    return _engine


# =========================================================
# Build DB
# =========================================================
def build_db():
    global _db
    if _db is not None:
        return _db
    logger.info("Initializing SQLDatabase...")
    engine = build_engine()
    _db = SQLDatabase(
        engine,
        sample_rows_in_table_info=1,
        view_support=False,
        max_string_length=200,
    )
    logger.info("Database initialized.")
    return _db


# =========================================================
# Extract SQL
# =========================================================
def extract_sql(raw: str) -> str:
    if not raw:
        return ""

    logger.debug(f"extract_sql raw (first 300): {repr(raw[:300])}")

    sql = ""

    fenced = re.search(
        r"```sql\s*(.*?)```",
        raw,
        flags=re.IGNORECASE | re.DOTALL
    )
    if fenced:
        sql = fenced.group(1).strip()
        logger.debug("extract_sql: Used fenced block strategy.")

    if not sql:
        match = re.search(
            r"(?i)(SELECT[\s\S]+?)(?=\n\s*(?:SELECT|```)|$)",
            raw
        )
        if match:
            sql = match.group(1).strip()
            logger.debug("extract_sql: Used first-SELECT strategy.")

    if not sql:
        sql = raw.strip()
        logger.debug("extract_sql: Used fallback strategy.")

    sql = re.sub(r"```sql", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"```",    "", sql)
    sql = sql.strip()

    parts = [p.strip() for p in sql.split(";") if p.strip()]
    sql = parts[0] if parts else sql

    single = re.match(
        r"(?i)(SELECT\s+(?:(?!SELECT).)*)",
        sql,
        re.DOTALL
    )
    if single:
        sql = single.group(1).strip()

    sql = " ".join(sql.split())

    sql = re.sub(
        r"(?i)^SELECT\s+(?!TOP\s+\d+)",
        "SELECT TOP 100 ",
        sql
    )

    logger.debug(f"extract_sql result: {sql}")
    return sql.strip()


# =========================================================
# Normalize Table References
# =========================================================
def normalize_table_references(sql: str) -> str:
    pattern = r"\[BI\]\.\[([A-Za-z0-9_]+)\](?!\.\[)"

    def replace_match(m):
        table_name = m.group(1)
        for allowed in ALLOWED_TABLES:
            inner = allowed.replace("[", "").replace("]", "")
            parts = inner.split(".")
            if len(parts) == 3:
                db, schema, tbl = parts
                if tbl.upper() == table_name.upper():
                    logger.debug(
                        f"normalize: [BI].[{table_name}] "
                        f"→ [{db}].[{schema}].[{tbl}]"
                    )
                    return f"[{db}].[{schema}].[{tbl}]"
        logger.warning(
            f"normalize: [{table_name}] not found in ALLOWED_TABLES."
        )
        return m.group(0)

    return re.sub(pattern, replace_match, sql, flags=re.IGNORECASE)


# =========================================================
# Validate SQL
# =========================================================
def validate_sql(sql: str) -> bool:
    if not sql:
        logger.warning("validate_sql FAIL: Empty SQL.")
        return False

    sql_norm  = " ".join(sql.split())
    upper_sql = sql_norm.upper()

    logger.debug(f"validate_sql input: {sql_norm}")

    if not upper_sql.startswith("SELECT"):
        logger.warning(
            f"validate_sql FAIL: Not SELECT → '{upper_sql[:80]}'"
        )
        return False

    if " FROM " not in upper_sql:
        logger.warning("validate_sql FAIL: No FROM clause.")
        return False

    dangerous = [
        "DROP ", "DELETE ", "TRUNCATE ", "ALTER ",
        "UPDATE ", "INSERT ", "EXEC ", "MERGE "
    ]
    for kw in dangerous:
        if kw in upper_sql:
            logger.warning(
                f"validate_sql FAIL: Dangerous keyword → '{kw}'"
            )
            return False

    if " LIMIT " in upper_sql:
        logger.warning("validate_sql FAIL: MySQL LIMIT detected.")
        return False

    pattern = r"\[[A-Za-z0-9_]+\]\.\[[A-Za-z0-9_]+\]\.\[[A-Za-z0-9_]+\]"
    found = re.findall(pattern, sql_norm, flags=re.IGNORECASE)

    logger.info(f"validate_sql: Found table refs → {found}")

    if not found:
        logger.warning(
            "validate_sql FAIL: No 3-part table references found."
        )
        return False

    allowed_set = {t.upper() for t in ALLOWED_TABLES}
    matched     = [t for t in found if t.upper() in allowed_set]

    logger.info(f"validate_sql: Matched tables → {matched}")

    if not matched:
        logger.warning(
            f"validate_sql FAIL: No tables matched ALLOWED_TABLES.\n"
            f"  Found:   {[t.upper() for t in found]}\n"
            f"  Allowed: {sorted(allowed_set)[:8]} ..."
        )
        return False

    logger.info("validate_sql: PASSED ✓")
    return True


# =========================================================
# Generate SQL
# =========================================================
def generate_sql(question: str) -> str:
    llm = build_llm()
    logger.info("Generating SQL query...")

    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=question),
    ]

    response = llm.invoke(
        messages,
        stop=["\n\n", "```\n\n", "SELECT\n"]
    )

    logger.debug(f"Raw response type : {type(response)}")

    if hasattr(response, "content"):
        raw = response.content
    elif isinstance(response, str):
        raw = response
    else:
        raw = str(response)

    logger.debug(f"Raw response value: {repr(raw[:500])}")

    sql = extract_sql(raw)
    sql = normalize_table_references(sql)

    logger.info(f"Final SQL: {sql}")
    return sql


# =========================================================
# Execute SQL
# =========================================================
def execute_sql(sql: str):
    engine = build_engine()
    logger.info("Executing SQL query...")
    with engine.connect() as conn:
        result  = conn.execute(text(sql))
        rows    = result.fetchall()
        columns = list(result.keys())
    logger.info(f"Query returned {len(rows)} rows.")
    return columns, rows


# =========================================================
# Pretty Print
# =========================================================
def print_results(columns, rows):
    if not rows:
        print("\nNo results found.")
        return

    col_widths = [
        max(
            len(str(col)),
            max((len(str(row[i])) for row in rows), default=0)
        )
        for i, col in enumerate(columns)
    ]

    separator = "-+-".join("-" * w for w in col_widths)
    header    = " | ".join(
        str(col).ljust(col_widths[i])
        for i, col in enumerate(columns)
    )

    print("\n" + "=" * 80)
    print("RESULTS")
    print("=" * 80)
    print(header)
    print(separator)

    for row in rows:
        print(" | ".join(
            str(val).ljust(col_widths[i])
            for i, val in enumerate(row)
        ))

    print("=" * 80)
    print(f"Total rows returned: {len(rows)}")
    print("=" * 80)


# =========================================================
# CLI
# =========================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="NLQ SQL Assistant - Natural Language to SQL"
    )
    parser.add_argument(
        "--prompt",
        required=True,
        type=str,
        help="Natural language question"
    )
    return parser.parse_args()


# =========================================================
# Main
# =========================================================
def main():
    _validate_env()

    args = parse_args()

    try:
        logger.info("Starting NLQ SQL Assistant...")

        llm  = build_llm()
        test = llm.invoke([HumanMessage(content="Reply with OK only.")])

        warmup = test.content if hasattr(test, "content") else str(test)
        logger.info(f"LLM warmup: {warmup[:80]}")

        logger.info(f"User question: {args.prompt}")
        sql = generate_sql(args.prompt)

        if not validate_sql(sql):
            logger.error("SQL validation failed. Blocking execution.")
            print("\n" + "=" * 80)
            print("[BLOCKED] Unsafe or invalid SQL generated.")
            print("=" * 80)
            print(f"Rejected SQL:\n{sql}")
            print("=" * 80)
            sys.exit(1)

        print("\n" + "=" * 80)
        print("GENERATED SQL")
        print("=" * 80)
        print(sql)
        print("=" * 80)

        columns, rows = execute_sql(sql)
        print_results(columns, rows)

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(0)

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


# =========================================================
# Entry Point
# =========================================================
if __name__ == "__main__":
    main()
