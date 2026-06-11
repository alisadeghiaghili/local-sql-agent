"""Auction NLQ Engine — interactive REPL entry point.

Usage::

    python app.py

Environment::

    Copy .env.example → .env and fill in your values before running.
"""

from __future__ import annotations

import logging
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import config as cfg
from core.models import RetrievalContext
from database.executor import execute_sql
from exporters.excel_exporter import export_excel
from llm.ollama_client import generate_sql
from logs.logger import save_log
from logs.query_log import QueryLog
from prompt_engine.builder import PromptBuilder
from retrieval.context_retriever import ContextRetriever
from security.sql_guard import validate_sql

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — loaded once at startup
# ---------------------------------------------------------------------------
_PROMPT_PATH = Path("prompts/system_prompt.md")


def _load_system_prompt() -> str:
    if not _PROMPT_PATH.exists():
        logger.error("System prompt not found: %s", _PROMPT_PATH)
        sys.exit(1)
    return _PROMPT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Rate-limit / debounce
# ---------------------------------------------------------------------------
_MIN_INTERVAL_SECONDS: float = 2.0
_last_query_time: float = 0.0


def _enforce_rate_limit() -> None:
    global _last_query_time
    elapsed = time.monotonic() - _last_query_time
    remaining = _MIN_INTERVAL_SECONDS - elapsed
    if remaining > 0:
        print(f"\u23f3  Please wait {remaining:.1f}s before the next query...")
        time.sleep(remaining)
    _last_query_time = time.monotonic()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEP = "=" * 60


def _round_half_up(value: float, decimals: int) -> float:
    multiplier = 10 ** decimals
    return math.floor(value * multiplier + 0.5) / multiplier


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
        model_name=cfg.settings.ollama_model,
        status=status,          # type: ignore[arg-type]
        excel_file=excel_file,
        row_count=row_count,
        execution_time_seconds=_round_half_up(elapsed, 3),
        error_message=error,
    )


def _print_sql(sql: str) -> None:
    print(f"\n{_SEP}")
    print("GENERATED SQL")
    print(_SEP)
    print(sql)


def _print_context_summary(context: RetrievalContext) -> None:
    """Print a brief debug summary of what was retrieved."""
    print(f"\n\U0001f9e0  Tables   : {context.selected_tables}")
    print(f"\U0001f4cb  Rules    : {len(context.business_rules)} matched")
    print(f"\U0001f4d6  Examples : {len(context.examples)} matched")
    if context.filters:
        print(f"\U0001f50d  Filters  : {context.filters}")


def _print_results(df) -> None:
    print(f"\n{_SEP}")
    print("QUERY RESULT")
    print(_SEP)
    if df.empty:
        print("No data found.")
    else:
        print(df.head(20).to_string(index=False))
        if len(df) > 20:
            print(f"  ... {len(df) - 20} more rows not shown")
        print(f"\nTotal rows returned: {len(df):,}")


# ---------------------------------------------------------------------------
# Main REPL
# ---------------------------------------------------------------------------

def main() -> None:
    system_prompt = _load_system_prompt()

    print(_SEP)
    print(" Auction NLQ Engine")
    print(f" Model : {cfg.settings.ollama_model}")
    print(f" DB    : {cfg.settings.db_connection_url.split('@')[-1].split('?')[0]}")
    print(_SEP)
    print(" Type your question in Persian or English.")
    print(" Commands: exit | quit | Ctrl+C")
    print(_SEP)

    while True:
        try:
            question = input("\n\u2753 Question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nBye.")
            break

        if not question or question.lower() in ("exit", "quit"):
            print("Bye.")
            break

        _enforce_rate_limit()

        sql   = ""
        start = time.perf_counter()

        try:
            # 1. Retrieve context
            context = ContextRetriever.retrieve(question)
            _print_context_summary(context)

            # 2. Build prompt
            prompt = PromptBuilder.build(
                question=question,
                system_prompt=system_prompt,
                context=context,
            )

            # 3. Generate SQL
            sql = generate_sql(prompt)
            validate_sql(sql)
            _print_sql(sql)

            # 4. Execute
            df      = execute_sql(sql)
            elapsed = time.perf_counter() - start

            excel_file = export_excel(df)
            print(f"\n\U0001f4c1 Excel saved: {excel_file}")
            print(f"\u23f1  Elapsed    : {elapsed:.2f}s")

            _print_results(df)

            save_log(_make_log(
                question, sql, "SUCCESS",
                excel_file=excel_file,
                row_count=len(df),
                elapsed=elapsed,
            ))

        except ValueError as exc:
            elapsed = time.perf_counter() - start
            msg = str(exc)
            if msg == "OUT_OF_SCOPE":
                save_log(_make_log(question, "", "OUT_OF_SCOPE", error=msg, elapsed=elapsed))
                print("\n\u26a0\ufe0f  This system only answers Auction database analytics questions.")
            else:
                save_log(_make_log(question, sql, "ERROR", error=msg, elapsed=elapsed))
                print(f"\n\u274c Validation error: {msg}")

        except RuntimeError as exc:
            elapsed = time.perf_counter() - start
            save_log(_make_log(question, sql, "ERROR", error=str(exc), elapsed=elapsed))
            print(f"\n\u274c Runtime error: {exc}")

        except Exception as exc:  # noqa: BLE001
            elapsed = time.perf_counter() - start
            save_log(_make_log(question, sql, "ERROR", error=str(exc), elapsed=elapsed))
            logger.exception("Unexpected error")
            print(f"\n\u274c Unexpected error: {exc}")


if __name__ == "__main__":
    main()
