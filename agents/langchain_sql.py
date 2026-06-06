"""LangChain NLQ agent for BI SQL Server with strict table whitelist.

Usage (from repo root)::

    python -m agents.langchain_sql --prompt "top 10 users by session count"
"""

from __future__ import annotations

import argparse
import logging
import sys

from langchain_ollama import ChatOllama
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_core.messages import HumanMessage, SystemMessage

from sql_agent.config import Settings, load_env_file
from sql_agent.db import get_engine
from sql_agent.validator import clean_sql, validate_sql, ensure_top

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Allowed table whitelist
# ---------------------------------------------------------------------------

ALLOWED_TABLES: list[str] = [
    "[BI].[App].[Dictionary]",    "[BI].[App].[Message]",
    "[BI].[App].[Permission]",    "[BI].[App].[PermissionType]",
    "[BI].[App].[ReportLog]",     "[BI].[App].[Sessions]",
    "[BI].[App].[SimaForms]",     "[BI].[App].[StoredProcedureMapping]",
    "[BI].[App].[TaskLog]",       "[BI].[App].[UserPermission]",
    "[BI].[App].[UserRecordLimit]", "[BI].[App].[Users]",
    "[BI].[App].[UserSystem]",
    "[BI].[Base].[Brokers]",      "[BI].[Base].[City]",
    "[BI].[Base].[County]",       "[BI].[Base].[Date]",
    "[BI].[Base].[DimDate]",      "[BI].[Base].[Province]",
    "[BI].[Base].[RuralDistrict]", "[BI].[Base].[Section]",
    "[BI].[Base].[Village]",
    "[BI].[Compare].[Auction]",
    "[BI].[Csd].[Product]",       "[BI].[Csd].[SymbolType]",
    "[BI].[Csd].[Warehouse]",
    "[BI].[dbo].[___BrokerDeleted_Karizma]", "[BI].[dbo].[__CSDSymbol]",
    "[BI].[dbo].[acBroker]",      "[BI].[dbo].[RayvazCustomer]",
    "[BI].[dbo].[simadw]",
    "[BI].[Inv].[CustomerAssetsAggrigated]", "[BI].[Inv].[CustomerAssetsDetail]",
    "[BI].[Inv].[CustomerReceiptAggrigated]", "[BI].[Inv].[CustomerReceiptDetail]",
    "[BI].[Inx].[BrokerAuditIndexs]", "[BI].[Inx].[BrokerIndexValues]",
    "[BI].[MoneyLaund].[CustomerContract]",
    "[BI].[Phi].[FinanceForecast]", "[BI].[Phi].[ObligationOffers]",
    "[BI].[risk].[FinalTotalRisk]",
    "[BI].[Scrape].[IceAsset]",   "[BI].[Scrape].[TgjuAsset]",
    "[BI].[Scrape].[TgjuDim]",
    "[BI].[Trading].[OptionTradesDetail]",
]

_ALLOWED_SET: set[str] = {t.upper() for t in ALLOWED_TABLES}


SYSTEM_PROMPT = (
    "You are a SQL Server query generator.\n\n"
    "STRICT RULES:\n"
    "1. Output ONLY a single SQL query. Nothing else.\n"
    "2. No explanations. No markdown fences. No repetition.\n"
    "3. Always use 3-part fully qualified table names: [BI].[Schema].[Table]\n"
    "4. Always use SELECT TOP 100.\n"
    "5. Only use tables from the AVAILABLE TABLES list below.\n"
    "6. Use square brackets around all identifiers.\n"
    "7. SQL Server syntax only. Never use LIMIT.\n"
    "8. Never use DROP, DELETE, TRUNCATE, ALTER, UPDATE, INSERT, EXEC, MERGE.\n"
    "9. Output the query ONCE only. Stop immediately after the query ends.\n\n"
    "AVAILABLE TABLES:\n" + "\n".join(t.replace("[", "").replace("]", "") for t in ALLOWED_TABLES)
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_cfg: Settings | None = None
_llm: ChatOllama | None = None
_db:  SQLDatabase | None = None


def _get_cfg() -> Settings:
    global _cfg
    if _cfg is None:
        _cfg = Settings()
        _cfg.validate()
    return _cfg


def _get_llm() -> ChatOllama:
    global _llm
    if _llm is None:
        cfg  = _get_cfg()
        _llm = ChatOllama(
            model=cfg.ollama_model,
            base_url=cfg.ollama_base_url,
            temperature=0.0,
            top_p=0.8,
            num_predict=300,
            streaming=False,
        )
        logger.info("LLM loaded: %s", cfg.ollama_model)
    return _llm


def _get_db() -> SQLDatabase:
    global _db
    if _db is None:
        engine = get_engine(_get_cfg().sqlserver_uri())
        _db = SQLDatabase(
            engine,
            sample_rows_in_table_info=1,
            view_support=False,
            max_string_length=200,
        )
        logger.info("SQLDatabase initialized")
    return _db


def _normalize_refs(sql: str) -> str:
    """Expand 2-part [BI].[Table] refs to the correct 3-part form."""
    pattern = r"\[BI\]\.\[([A-Za-z0-9_]+)\](?!\.\[)"

    def _replace(m: re.Match) -> str:
        name = m.group(1)
        for allowed in ALLOWED_TABLES:
            parts = allowed.replace("[", "").replace("]", "").split(".")
            if len(parts) == 3 and parts[2].upper() == name.upper():
                logger.debug("normalize: [BI].[%s] → %s", name, allowed)
                return allowed
        logger.warning("normalize: [%s] not found in ALLOWED_TABLES", name)
        return m.group(0)

    import re
    return re.sub(pattern, _replace, sql, flags=re.IGNORECASE)


def _whitelist_check(sql: str) -> bool:
    """Return True only if at least one ALLOWED table is referenced."""
    import re
    found = re.findall(
        r"\[[A-Za-z0-9_]+\]\.\[[A-Za-z0-9_]+\]\.\[[A-Za-z0-9_]+\]",
        sql,
        flags=re.IGNORECASE,
    )
    if not found:
        logger.warning("whitelist_check: no 3-part refs found")
        return False
    matched = [t for t in found if t.upper() in _ALLOWED_SET]
    if not matched:
        logger.warning("whitelist_check: tables not in whitelist: %s", found)
        return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_sql(question: str) -> str:
    """Generate, normalise, and validate a SQL query for *question*."""
    llm = _get_llm()
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=question),
    ]
    response = llm.invoke(messages, stop=["\n\n", "```\n\n"])
    raw = response.content if hasattr(response, "content") else str(response)
    logger.debug("Raw LLM response: %s", repr(raw[:300]))

    sql = clean_sql(raw)
    sql = ensure_top(sql)
    sql = _normalize_refs(sql)
    validate_sql(sql)

    if not _whitelist_check(sql):
        raise ValueError("Generated SQL references tables outside the allowed whitelist")

    logger.info("Final SQL: %s", sql)
    return sql


def execute_sql(sql: str):
    from sqlalchemy import text
    engine = get_engine(_get_cfg().sqlserver_uri())
    with engine.connect() as conn:
        result  = conn.execute(text(sql))
        rows    = result.fetchall()
        columns = list(result.keys())
    logger.info("Query returned %d rows.", len(rows))
    return columns, rows


def print_results(columns: list, rows: list) -> None:
    if not rows:
        print("\nNo results found.")
        return
    col_w = [
        max(len(str(c)), max((len(str(r[i])) for r in rows), default=0))
        for i, c in enumerate(columns)
    ]
    sep    = "-+-".join("-" * w for w in col_w)
    header = " | ".join(str(c).ljust(col_w[i]) for i, c in enumerate(columns))
    print("\n" + "=" * 80)
    print("RESULTS")
    print("=" * 80)
    print(header)
    print(sep)
    for row in rows:
        print(" | ".join(str(v).ljust(col_w[i]) for i, v in enumerate(row)))
    print("=" * 80)
    print(f"Total rows returned: {len(rows)}")
    print("=" * 80)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NLQ SQL Assistant — Natural Language to SQL")
    p.add_argument("--prompt", required=True, type=str, help="Natural language question")
    return p.parse_args()


def main() -> None:
    load_env_file()
    _get_cfg()          # validates env early
    args = parse_args()
    try:
        logger.info("User question: %s", args.prompt)
        sql = generate_sql(args.prompt)
        print("\n" + "=" * 80)
        print("GENERATED SQL")
        print("=" * 80)
        print(sql)
        print("=" * 80)
        columns, rows = execute_sql(sql)
        print_results(columns, rows)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
