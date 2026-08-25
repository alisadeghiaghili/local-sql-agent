"""Bridge between the Flask app and the repo's NLQ engine.

Imports the parent project (``api.runner.run_query``) by adding the repo
root to ``sys.path`` and loading its ``.env``, so the web app runs the exact
same generation → validation → execution pipeline as the CLI / API.

Every successful query also exports the result rows to
``webapp/outputs/output_<YYYYMMDD_HHMMSS>.csv`` (UTF-8 with BOM so Excel
opens Persian text correctly).
"""

from __future__ import annotations

import csv
import sys
import time
from datetime import datetime
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEBAPP_DIR.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from api.errors import NLQError  # noqa: E402
from api.runner import run_query  # noqa: E402

SYSTEM_PROMPT_PATH = REPO_ROOT / "prompts" / "system_prompt.md"
OUTPUT_DIR = WEBAPP_DIR / "outputs"

_system_prompt: str | None = None


def system_prompt() -> str:
    """Load and cache the repo's system prompt."""
    global _system_prompt
    if _system_prompt is None:
        _system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return _system_prompt


def _export_csv(rows: list[dict]) -> str:
    """Write result rows to outputs/ with a current-datetime filename."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0].keys())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"output_{stamp}.csv"
    suffix = 1
    while path.exists():
        path = OUTPUT_DIR / f"output_{stamp}_{suffix}.csv"
        suffix += 1
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def answer_question(
    question: str,
    interpret: bool = True,
) -> dict:
    """Run one question through the full pipeline; never raises.

    Uses the shared OpenAI-compatible LLM backend.

    Returns a dict with ``status`` ("SUCCESS" or "ERROR"), the generated
    SQL, result rows/columns, plain-language interpretation, output file
    path and timing.  On failure ``error_message`` holds the reason.
    """
    start = time.perf_counter()
    try:
        resp = run_query(
            question, system_prompt(), mode="full", interpret=interpret,
        )
        rows = resp.result or []
        output_file = _export_csv(rows) if rows else None
        return {
            "status": "SUCCESS",
            "question": question,
            "sql": resp.sql,
            "interpretation": resp.interpretation,
            "rows": rows,
            "columns": list(rows[0].keys()) if rows else [],
            "row_count": len(rows),
            "model": resp.model,
            "output_file": output_file,
            "error_message": None,
            "elapsed_seconds": round(time.perf_counter() - start, 3),
        }
    except NLQError as exc:
        return _error_result(str(exc), start, question)
    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        return _error_result(f"Unexpected error: {exc}", start, question)


def _error_result(message: str, start: float, question: str = "") -> dict:
    return {
        "status": "ERROR",
        "question": question,
        "sql": None,
        "interpretation": None,
        "rows": [],
        "columns": [],
        "row_count": 0,
        "model": None,
        "output_file": None,
        "error_message": message,
        "elapsed_seconds": round(time.perf_counter() - start, 3),
    }
