"""Raw Ollama HTTP runner — SQL Server (no LangChain).

Usage (from repo root)::

    python -m runners.nlq --prompt "top 10 customers"
    python -m runners.nlq --prompt "..." --mode simple
    python -m runners.nlq --prompt "..." --mode prompt
    python -m runners.nlq --prompt "..." --mode full    # default

Modes
-----
    simple  minimal system prompt
    prompt  schema-aware prompt
    full    strict rules + TOP 100 default (default mode)
"""

from __future__ import annotations

import argparse
import sys

from sql_agent.config import Settings, load_env_file
from sql_agent.db import execute_sql
from sql_agent.llm import call_ollama
from sql_agent.prompts import SYSTEM_PROMPTS
from sql_agent.validator import clean_sql, validate_sql


def generate_sql(question: str, mode: str, cfg: Settings) -> str:
    system      = SYSTEM_PROMPTS[mode]
    full_prompt = f"{system}\n\nQuestion: {question}\n\nSQL:"
    raw = call_ollama(
        full_prompt,
        base_url=cfg.ollama_base_url,
        model=cfg.ollama_model,
        temperature=cfg.ollama_temperature,
    )
    sql = clean_sql(raw)
    validate_sql(sql)
    return sql


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


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NLQ → SQL Server (raw Ollama HTTP)")
    p.add_argument("--prompt", required=True, help="Natural language question")
    p.add_argument("--mode", choices=list(SYSTEM_PROMPTS), default="full",
                   help="Prompt mode: simple | prompt | full  (default: full)")
    return p.parse_args()


def main() -> None:
    load_env_file()
    cfg  = Settings()
    cfg.validate()
    args = _parse_args()
    try:
        sql = generate_sql(args.prompt, mode=args.mode, cfg=cfg)
        print("\n" + "=" * 80)
        print(f"GENERATED SQL  [mode={args.mode}]")
        print("=" * 80)
        print(sql)
        print("=" * 80)
        columns, rows = execute_sql(sql, settings=cfg)
        _print_results(columns, rows)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
