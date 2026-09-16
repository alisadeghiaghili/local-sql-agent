# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
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
import logging
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
from appdb.key_store import _maximally_restrictive_denied_columns  # noqa: E402
from exporters.sanitize import defuse_formula  # noqa: E402
from security.auth import Principal  # noqa: E402

SYSTEM_PROMPT_PATH = REPO_ROOT / "prompts" / "system_prompt.md"
OUTPUT_DIR = WEBAPP_DIR / "outputs"

logger = logging.getLogger(__name__)

_system_prompt: str | None = None

#: Findings 13/14 — this module predates the Phase 8 principal model and
#: was never connected to it (see tests/security_audit/
#: test_webapp_principal_and_acl.py's module docstring): every question
#: asked through the Flask app reached ``run_query`` with no principal at
#: all, and ``api.runner.run_query`` treats "no principal" as "no column
#: restriction" -- deliberately, for the REPL and the test suite, which
#: *are* legitimate principal-less callers. The Flask app is not one of
#: those: it has real, named, logged-in users (``webapp/db.py``'s users
#: table) and is obliged to say who is asking. When a logged-in username
#: cannot be mapped to a configured Phase-8 principal (webapp/app.py has
#: no such mapping today, and may never have every user enrolled), the
#: fail-closed answer is this fallback -- built from the exact same
#: column list a freshly issued, not-yet-widened API key gets
#: (``appdb.key_store._maximally_restrictive_denied_columns``), reused
#: rather than re-derived so the two "I don't know who you are yet"
#: postures in this codebase stay identical by construction instead of by
#: two people remembering to update both.
def _restrictive_fallback_principal(identity: str | None) -> Principal:
    """Build the fail-closed ``Principal`` for a caller this app cannot
    place: every column denied, no administrative capability. See the
    module-level comment above this function for why an *empty*
    ``denied_columns`` (the bug the audit found) is not an acceptable
    default here."""
    return Principal(
        id=identity or "webapp-unmapped",
        name=identity or "unmapped webapp user",
        denied_columns=tuple(_maximally_restrictive_denied_columns()),
    )


def principal_for_username(username: str | None) -> Principal:
    """Resolve a Flask login (``webapp/db.py``'s users table) to the
    Phase-8 ``Principal`` that should govern its queries.

    The two identity systems are not formally linked -- a Flask account
    and an API-key principal are created through entirely different
    flows (``app.py create-user`` vs. ``scripts.issue_api_key`` /
    the admin panel) -- but they share one namespace by convention
    (``docs/fa/getting-started.md`` issues both as e.g. ``analyst-1``),
    so a deployment that wants its Flask users to carry the SAME column
    ACL the admin panel manages only has to issue a key with a matching
    ``principal_id``. Looked up defensively: the application database
    that ``appdb.key_store`` reads from is not something this
    historically DB-agnostic web app has ever depended on, so any
    failure to reach it (unset ``APP_DB_URL``, an unmigrated table, a
    network blip) must fall through to the restrictive default rather
    than surface as a 500 or, worse, silently grant unrestricted access.
    """
    if username:
        try:
            from appdb.key_store import get_active_principals

            for principal in get_active_principals().values():
                if principal.id == username:
                    return principal
        except Exception:  # noqa: BLE001 - see docstring: never let this widen access
            logger.warning(
                "principal_for_username(%r): appdb lookup failed, falling back "
                "to the restrictive default",
                username,
                exc_info=True,
            )
    return _restrictive_fallback_principal(username)


def system_prompt() -> str:
    """Load and cache the repo's system prompt."""
    global _system_prompt
    if _system_prompt is None:
        _system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    return _system_prompt


def _export_csv(rows: list[dict]) -> str:
    """Write result rows to outputs/ with a current-datetime filename.

    Finding 15 (formula injection): this file is written UTF-8-with-BOM
    specifically so Excel opens it -- see the module docstring -- which
    makes it exactly the file format that treats a cell starting with
    ``=``/``+``/``-``/``@`` as a formula to execute rather than text to
    display. CSV's own field quoting (what ``csv.DictWriter`` already
    does) escapes embedded quotes; it does nothing about a cell that
    simply *begins* with one of those characters, which is what a
    warehouse value the analyst never typed can do. Every cell is passed
    through :func:`exporters.sanitize.defuse_formula` for the same reason
    ``exporters/excel_exporter.py`` does -- one shared rule, not two
    copies of it that can drift.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0].keys())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"output_{stamp}.csv"
    suffix = 1
    while path.exists():
        path = OUTPUT_DIR / f"output_{stamp}_{suffix}.csv"
        suffix += 1
    defused_rows = [
        {key: defuse_formula(value) for key, value in row.items()} for row in rows
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(defused_rows)
    return str(path)


def answer_question(
    question: str,
    interpret: bool = False,
    principal: Principal | None = None,
) -> dict:
    """Run one question through the full pipeline; never raises.

    Uses the shared OpenAI-compatible LLM backend.

    Parameters
    ----------
    interpret:
        Whether result rows are sent to the model for a plain-language
        summary. Defaults to ``False`` -- Finding 14: every other entry
        point in this project (``AskTurnRequest.interpret``,
        ``QueryRequest.interpret``) defaults this the same way, because
        it is a data-governance decision (up to twenty rows of real
        results leaving the process) and not something a caller should
        get without asking for it.
    principal:
        The authenticated Flask user, as resolved by
        :func:`principal_for_username` -- or ``None`` for a caller that
        has not identified one (a direct script, a test). Finding 13:
        this module used to call ``run_query`` with no principal at all,
        which ``api.runner.run_query`` reads as "no restriction" -- a
        deliberate default for its OTHER legitimate principal-less
        callers (the REPL, the test suite), but wrong here, because this
        path has real logins. ``None`` is therefore never forwarded
        as-is: it is replaced with the same maximally-restrictive
        fallback an unmapped username gets, so "I forgot to pass a
        principal" and "I could not identify this caller" fail exactly
        the same way -- closed.

    Returns a dict with ``status`` ("SUCCESS" or "ERROR"), the generated
    SQL, result rows/columns, plain-language interpretation, output file
    path and timing.  On failure ``error_message`` holds the reason.
    """
    start = time.perf_counter()
    if principal is None:
        principal = _restrictive_fallback_principal(None)
    try:
        resp = run_query(
            question, system_prompt(), mode="full", interpret=interpret,
            principal=principal,
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
