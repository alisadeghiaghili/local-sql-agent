"""Auction NLQ Engine — Entry Point.

Usage::

    python app.py
"""

from __future__ import annotations

import time
from datetime import datetime

from config import OLLAMA_MODEL
from database.executor import execute_sql
from exporters.excel_exporter import export_excel
from llm.ollama_client import generate_sql
from logs.logger import save_log
from logs.query_log import QueryLog
from security.sql_guard import validate_sql

with open("prompts/system_prompt.md", encoding="utf-8") as _f:
    SYSTEM_PROMPT = _f.read()


def _make_log(
    question: str,
    sql: str,
    status: str,
    error: str | None = None,
    excel_file: str | None = None,
    row_count: int = 0,
    elapsed: float = 0.0,
) -> QueryLog:
    return QueryLog(
        timestamp=datetime.now(),
        question=question,
        generated_sql=sql,
        model_name=OLLAMA_MODEL,
        excel_file=excel_file,
        row_count=row_count,
        execution_time_seconds=round(elapsed, 3),
        status=status,
        error_message=error,
    )


def main() -> None:
    print("=" * 60)
    print("Auction NLQ Engine")
    print("=" * 60)

    while True:
        try:
            question = input("\nQuestion: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if question.lower() in ("exit", "quit", ""):
            break

        sql = ""
        start = time.time()

        try:
            sql = generate_sql(question, SYSTEM_PROMPT)
            validate_sql(sql)

            print("\n" + "=" * 60)
            print("GENERATED SQL")
            print("=" * 60)
            print(sql)

            df      = execute_sql(sql)
            elapsed = time.time() - start

            excel_file = export_excel(df)
            print(f"\nExcel Saved: {excel_file}")

            save_log(_make_log(
                question, sql, "SUCCESS",
                excel_file=excel_file,
                row_count=len(df),
                elapsed=elapsed,
            ))

            print("\n" + "=" * 60)
            print("QUERY RESULT")
            print("=" * 60)
            if df.empty:
                print("No data found")
            else:
                print(df.head(20).to_string(index=False))
                print(f"\nReturned Rows: {len(df)}")

        except ValueError as exc:
            if str(exc) == "OUT_OF_SCOPE":
                save_log(_make_log(question, "", "OUT_OF_SCOPE", error="OUT_OF_SCOPE"))
                print("\nThis system only supports Auction database analytics questions.")
            else:
                save_log(_make_log(question, sql, "ERROR", error=str(exc)))
                print(f"\nERROR: {exc}")

        except Exception as exc:  # noqa: BLE001
            save_log(_make_log(question, sql, "ERROR", error=str(exc)))
            print(f"\nERROR: {exc}")


if __name__ == "__main__":
    main()
