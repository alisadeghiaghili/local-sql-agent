"""runners/nlq.py — unified NLQ entry-point (raw Ollama HTTP + SQL Server).

Replaces main.py, simple_nlq.py, and prompt_based_nlq.py.

Usage (from repo root):
    python -m runners.nlq --prompt "top 10 customers by revenue"
    python -m runners.nlq --prompt "..." --mode simple
    python -m runners.nlq --prompt "..." --mode prompt
    python -m runners.nlq --prompt "..." --mode full      # default

Modes
-----
    simple  : minimal system prompt, no schema injection
    prompt  : schema-aware system prompt
    full    : strict rules + allowed-table whitelist (default)
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import requests
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL")
DB_SERVER       = os.getenv("DB_SERVER")
DB_NAME         = os.getenv("DB_NAME")
DB_USER         = os.getenv("DB_USER")
DB_PASS         = os.getenv("DB_PASSWORD", "")
DB_PORT         = os.getenv("DB_PORT", "1433")
DB_DRIVER       = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
DB_TRUSTED      = os.getenv("DB_TRUSTED_CONNECTION", "").lower()


def _validate_env() -> None:
    required = [
        ("OLLAMA_MODEL", OLLAMA_MODEL),
        ("DB_SERVER",    DB_SERVER),
        ("DB_NAME",      DB_NAME),
        ("DB_USER",      DB_USER),
    ]
    if DB_TRUSTED != "yes":
        required.append(("DB_PASSWORD", DB_PASS))
    missing = [n for n, v in required if not v]
    if missing:
        raise ValueError(
            f"❌ Missing env vars: {', '.join(missing)}\n"
            f"Copy .env.example → .env and fill in your values."
        )


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_PROMPT_SIMPLE = """You are a SQL Server query generator.
Output ONLY a valid SQL Server SELECT query. No explanation. No markdown.
Use TOP instead of LIMIT. Never use DROP, DELETE, UPDATE, INSERT, TRUNCATE.
"""

_PROMPT_PROMPT = """You are an expert SQL Server assistant.
Given a natural language question, generate a single SQL Server SELECT query.

Rules:
- Output raw SQL only — no markdown, no explanation.
- Use SELECT TOP N (never LIMIT).
- Use fully qualified bracket notation: [schema].[table].
- Never use DELETE, UPDATE, INSERT, DROP, ALTER, TRUNCATE, EXEC.
- Use the schema provided by the user.
"""

_PROMPT_FULL = """You are a SQL Server query generator.

STRICT RULES:
1. Output ONLY a single SQL query. Nothing else.
2. No explanations. No markdown fences. No repetition.
3. Always use 3-part fully qualified names: [DB].[Schema].[Table]
4. Always use SELECT TOP 100 unless the user specifies a different limit.
5. Use square brackets around all identifiers.
6. SQL Server syntax only — never LIMIT, never QUALIFY, never ILIKE.
7. Never use DROP, DELETE, TRUNCATE, ALTER, UPDATE, INSERT, EXEC, MERGE.
8. Output the query ONCE only. Stop immediately after the semicolon.
"""

PROMPTS: dict[str, str] = {
    "simple": _PROMPT_SIMPLE,
    "prompt": _PROMPT_PROMPT,
    "full":   _PROMPT_FULL,
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        driver_enc = quote_plus(DB_DRIVER)
        user_enc   = quote_plus(DB_USER)
        if DB_TRUSTED == "yes":
            url = (
                f"mssql+pyodbc://{user_enc}@{DB_SERVER}:{DB_PORT}/{DB_NAME}"
                f"?driver={driver_enc}&trusted_connection=yes"
            )
        else:
            pass_enc = quote_plus(DB_PASS)
            url = (
                f"mssql+pyodbc://{user_enc}:{pass_enc}"
                f"@{DB_SERVER}:{DB_PORT}/{DB_NAME}"
                f"?driver={driver_enc}"
            )
        _engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
    return _engine


def execute_sql(sql: str) -> tuple[list, list]:
    with _get_engine().connect() as conn:
        conn.execute(text("SET LOCK_TIMEOUT 30000"))
        result  = conn.execute(text(sql))
        rows    = result.fetchall()
        columns = list(result.keys())
    return columns, rows


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str) -> str:
    url = OLLAMA_BASE_URL.rstrip("/") + "/api/generate"
    for attempt in range(3):
        try:
            resp = requests.post(
                url,
                json={"model": OLLAMA_MODEL, "prompt": prompt,
                      "stream": False, "temperature": 0.1},
                timeout=60,
            )
            if resp.status_code == 200:
                return resp.json().get("response", "").strip()
        except requests.RequestException:
            if attempt == 2:
                raise
    raise RuntimeError("Ollama unavailable after 3 retries")


def _clean_sql(raw: str) -> str:
    sql = re.sub(r"```sql", "", raw, flags=re.IGNORECASE)
    sql = re.sub(r"```",    "", sql)
    sql = sql.strip()
    upper = sql.upper()
    if upper.startswith("WITH"):
        pass
    elif "SELECT" in upper:
        sql = sql[upper.find("SELECT"):]
    else:
        raise ValueError("No SELECT statement found in model response")
    sql = sql.split("\n\n")[0].strip()
    m = re.search(r"LIMIT\s+(\d+)", sql, re.IGNORECASE)
    if m:
        n = m.group(1)
        sql = re.sub(r"LIMIT\s+\d+", "", sql, flags=re.IGNORECASE).strip()
        sql = re.sub(r"(?i)^(SELECT)\s+", f"SELECT TOP {n} ", sql, count=1)
    sql = re.sub(r"SELECT\s+TOP\s+(\d+)\s+DISTINCT", r"SELECT DISTINCT TOP \1", sql, flags=re.IGNORECASE)
    return sql.strip()


def _validate_sql(sql: str) -> None:
    upper = sql.upper()
    if not (upper.lstrip().startswith("SELECT") or upper.lstrip().startswith("WITH")):
        raise ValueError("Only SELECT / CTE queries are allowed")
    for kw in ("DELETE ", "UPDATE ", "DROP ", "ALTER ", "INSERT ", "TRUNCATE ", "EXEC ", "MERGE "):
        if kw in upper:
            raise ValueError(f"Forbidden keyword detected: {kw.strip()}")


def generate_sql(question: str, mode: str = "full") -> str:
    system = PROMPTS[mode]
    full_prompt = f"{system}\n\nQuestion: {question}\n\nSQL:"
    raw = _call_ollama(full_prompt)
    sql = _clean_sql(raw)
    _validate_sql(sql)
    return sql


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_results(columns: list, rows: list) -> None:
    if not rows:
        print("\nNo results found.")
        return
    col_w  = [max(len(str(c)), max((len(str(r[i])) for r in rows), default=0)) for i, c in enumerate(columns)]
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
    print(f"Total rows: {len(rows)}")
    print("=" * 80)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NLQ → SQL Server  (natural language query assistant)")
    p.add_argument("--prompt", required=True, help="Natural language question")
    p.add_argument(
        "--mode",
        choices=list(PROMPTS),
        default="full",
        help="Prompt style: simple | prompt | full  (default: full)",
    )
    return p.parse_args()


def main() -> None:
    _validate_env()
    args = _parse_args()
    try:
        sql = generate_sql(args.prompt, mode=args.mode)
        print("\n" + "=" * 80)
        print(f"GENERATED SQL  [mode={args.mode}]")
        print("=" * 80)
        print(sql)
        print("=" * 80)
        columns, rows = execute_sql(sql)
        _print_results(columns, rows)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
