# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""One-time project setup wizard for local-sql-agent.

Usage
-----
::

    python setup_project.py

    python setup_project.py \\
        --db-url  "mssql+pyodbc://server/db?driver=ODBC+Driver+17+for+SQL+Server" \\
        --llm-provider openai \\
        --llm-model    gpt-oss-20b \\
        --language     fa \\
        --output       project_config/ \\
        --review       interactive

Flags
-----
--non-interactive   Accept all LLM suggestions without prompting (CI mode).
--dry-run           Print generated YAML to stdout; do not write files (the
                    setup log included).
--resume            Continue an interrupted run from the first step that
                    ``.setup_log.json`` does not record as completed. Steps 1
                    and 2 (connection, schema) run again, because the
                    connection string is never stored; the aliases, rules and
                    examples of completed LLM steps are reused rather than
                    generated again, unless the schema or language changed; a
                    file that already exists is not rewritten; when steps 1-6
                    are all done and every file exists only the validation
                    runs. Without ``--resume`` a new log is started.

LLM endpoint
------------
The model that suggests aliases, rules and examples is chosen, for each of
endpoint / model / key, from the command-line flag, then ``WIZARD_LLM_BASE_URL``
/ ``WIZARD_LLM_MODEL``, then the application's own ``OPENAI_BASE_URL`` /
``OPENAI_MODEL`` / ``OPENAI_API_KEY`` (an empty ``WIZARD_*`` value counts as
unset). The wizard prints one line naming the provider, model and endpoint
(never the key). It sends table and column names and sample column values to
that endpoint, so it obeys the application's data-governance rule: an endpoint
that is not local (see ``llm/trust.py``, ``LLM_TRUSTED``) is refused unless
``LLM_ALLOW_REMOTE=true``. If the endpoint does not answer, the wizard says so
loudly and continues with the mock LLM, which generates nothing.

The wizard writes ``project_config/.setup_log.json`` recording every step
that was executed and when (and the aliases, rules and examples the model
produced), so it can be resumed safely. The connection string is stored only
with its password masked.

The wizard is idempotent: running it multiple times never corrupts existing
configuration.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

# ---------------------------------------------------------------------------
# Rich / questionary — graceful fallback if not installed
# ---------------------------------------------------------------------------
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich import print as rprint
    _RICH = True
except ImportError:  # pragma: no cover
    _RICH = False
    class Console:  # type: ignore[no-redef]
        def print(self, *a, **kw): print(*a)
        def rule(self, *a, **kw): print("-" * 60)
    rprint = print  # type: ignore[assignment]

try:
    import questionary
    _QUESTIONARY = True
except ImportError:  # pragma: no cover
    _QUESTIONARY = False

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

from core.yaml_loading import safe_load_strict

console = Console()
logger  = logging.getLogger(__name__)

_DEFAULT_OUTPUT = Path("project_config")
_SETUP_LOG      = _DEFAULT_OUTPUT / ".setup_log.json"

# DB type help text shown on connection failure
_DB_EXAMPLES = """
  MSSQL  : mssql+pyodbc://user:pass@server/db?driver=ODBC+Driver+17+for+SQL+Server
  PostgreSQL: postgresql+psycopg2://user:pass@host:5432/db
  MySQL  : mysql+pymysql://user:pass@host:3306/db
  SQLite : sqlite:///path/to/file.db
"""


# ===========================================================================
# Credential redaction
# ===========================================================================

#: Placeholder shown when a connection string cannot be parsed at all.
_UNPARSEABLE_URL = "<unparseable connection URL>"

#: Mask substituted for every credential.
_MASK = "***"

#: Query-string keys whose value is a credential, compared lower-cased.
_SECRET_QUERY_KEYS = frozenset(
    {"pwd", "password", "passwd", "secret", "token", "access_token", "api_key", "apikey"}
)

#: ``PWD=...`` / ``Password=...`` inside an ODBC connection string (the
#: ``odbc_connect`` query value of an ``mssql+pyodbc`` URL carries the whole
#: string, password included).
_ODBC_SECRET_RE = re.compile(r"(?i)(\b(?:pwd|password|passwd)\s*=\s*)[^;&]*")

#: ``://user:password@`` in a string SQLAlchemy could not parse. Greedy up to
#: the last ``@`` before the first ``/``, because a password may itself
#: contain ``@`` or ``:``.
_RAW_USERINFO_RE = re.compile(r"(?<=://)([^:/@\s]*):([^/\s]*)@")


def _redact_db_url(url: str) -> str:
    """Return *url* with every credential masked, safe to print or write.

    The URL is parsed by SQLAlchemy (``make_url(...).render_as_string(
    hide_password=True)``) rather than matched with a pattern, so a password
    containing ``@``, ``:`` or ``/`` is still masked. Credentials carried in
    the query string (``?PWD=...``, or inside ``odbc_connect``) are masked as
    well. A string SQLAlchemy cannot parse is never returned as it came: a
    string that does not look like a URL becomes a placeholder, and one that
    does has its ``user:password@`` and ``PWD=`` parts masked by pattern.

    Args:
        url: A SQLAlchemy connection string, possibly with credentials.

    Returns:
        The same URL with the password (and any secret query value) replaced
        by ``***``, or a placeholder when nothing safe can be shown.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> _redact_db_url("mssql+pyodbc://nlq:s3cret@db1/Sales")
        'mssql+pyodbc://nlq:***@db1/Sales'
        >>> "s3cret" in _redact_db_url("mssql+pyodbc://nlq:p@s3cret@db1/Sales")
        False
        >>> "s3cret" in _redact_db_url("mssql+pyodbc:///?odbc_connect=UID=a;PWD=s3cret")
        False
        >>> _redact_db_url("not a url")
        '<unparseable connection URL>'
    """
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(url)
        if "@" in (parsed.host or ""):
            # An unescaped "@" inside the password: SQLAlchemy ended the
            # password early and put the rest in the host. Mask by pattern.
            raise ValueError("ambiguous user information")
        query: dict[str, Any] = {}
        for key, value in parsed.query.items():
            lowered = key.lower()
            values = value if isinstance(value, tuple) else (value,)
            if lowered in _SECRET_QUERY_KEYS:
                masked = tuple(_MASK for _ in values)
            else:
                masked = tuple(_ODBC_SECRET_RE.sub(rf"\1{_MASK}", v) for v in values)
            query[key] = masked if isinstance(value, tuple) else masked[0]
        return parsed.set(query=query).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - never fall back to the raw string
        if "://" not in url:
            return _UNPARSEABLE_URL
        masked_raw = _RAW_USERINFO_RE.sub(rf"\1:{_MASK}@", url)
        return _ODBC_SECRET_RE.sub(rf"\1{_MASK}", masked_raw)


def _url_secrets(url: str) -> list[str]:
    """Return every credential value found in *url*, longest first.

    Used to scrub third-party error text (a driver may echo part of the
    connection string). Includes the URL-encoded spelling of the password,
    because SQLAlchemy renders it that way.

    Args:
        url: A SQLAlchemy connection string.

    Returns:
        Distinct non-empty secret strings, longest first. Empty when *url*
        cannot be parsed or carries no credential.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> _url_secrets("mssql+pyodbc://nlq:s3cret@db1/Sales")
        ['s3cret']
        >>> _url_secrets("sqlite:///x.db")
        []
    """
    from urllib.parse import quote, quote_plus

    from sqlalchemy.engine import make_url

    found: set[str] = {m.group(2) for m in _RAW_USERINFO_RE.finditer(url)}
    try:
        parsed = make_url(url)
        if parsed.password and "@" not in (parsed.host or ""):
            found.update({parsed.password, quote(parsed.password, safe=""),
                          quote_plus(parsed.password)})
        for key, value in parsed.query.items():
            for item in (value if isinstance(value, tuple) else (value,)):
                if key.lower() in _SECRET_QUERY_KEYS:
                    found.add(item)
                else:
                    found.update(m.group(0).split("=", 1)[1].strip()
                                 for m in _ODBC_SECRET_RE.finditer(item))
    except Exception:  # noqa: BLE001
        pass
    return sorted((s for s in found if s), key=len, reverse=True)


def _scrub_secrets(text: str, db_url: str) -> str:
    """Return *text* with *db_url* and any credential from it masked.

    Args:
        text: Text about to be printed, e.g. a driver's exception message.
        db_url: The connection string the text may have come from.

    Returns:
        *text* with the raw URL replaced by its redacted form and every
        credential value replaced by ``***``.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> _scrub_secrets("login failed for nlq:s3cret", "mssql+pyodbc://nlq:s3cret@db1/Sales")
        'login failed for nlq:***'
    """
    if db_url:
        text = text.replace(db_url, _redact_db_url(db_url))
    for secret in _url_secrets(db_url):
        text = text.replace(secret, _MASK)
    return text


# ===========================================================================
# Helpers
# ===========================================================================

def _yn(question: str, default: bool = True, non_interactive: bool = False) -> bool:
    """Ask a yes/no question; return default when non-interactive."""
    if non_interactive:
        return default
    if _QUESTIONARY:
        return questionary.confirm(question, default=default).ask()
    ans = input(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    if not ans:
        return default
    return ans.startswith("y")


def _ask(question: str, default: str = "", non_interactive: bool = False) -> str:
    """Ask a text question; return default when non_interactive."""
    if non_interactive:
        return default
    if _QUESTIONARY:
        return questionary.text(question, default=default).ask() or default
    ans = input(f"{question} [{default}]: ").strip()
    return ans or default


def _choose(
    question: str,
    choices: list[str],
    default: str,
    non_interactive: bool = False,
) -> str:
    if non_interactive:
        return default
    if _QUESTIONARY:
        return questionary.select(question, choices=choices, default=default).ask()
    for i, c in enumerate(choices, 1):
        print(f"  {i}. {c}")
    idx = input(f"{question} [default: {default}]: ").strip()
    if not idx:
        return default
    try:
        return choices[int(idx) - 1]
    except (ValueError, IndexError):
        return default


def _edit_in_editor(content: str) -> str:
    """Open *content* in $EDITOR and return the saved result."""
    import tempfile
    editor = os.environ.get("EDITOR", "nano")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
        fh.write(content)
        tmp = fh.name
    subprocess.call([editor, tmp])
    result = Path(tmp).read_text(encoding="utf-8")
    Path(tmp).unlink(missing_ok=True)
    return result


def _spinner(message: str):
    """Context manager: show a spinner if rich is available, else a plain message."""
    if _RICH:
        return Progress(
            SpinnerColumn(),
            TextColumn(message),
            transient=True,
        )
    class _NoOp:
        def __enter__(self):
            print(message)
            return self
        def __exit__(self, *_): pass
        def add_task(self, *a, **kw): return None
    return _NoOp()


# ===========================================================================
# Setup log
# ===========================================================================

def _load_log(log_path: Path) -> dict:
    if log_path.exists():
        try:
            return json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_log(log_path: Path, log: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(log, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _mark_done(log: dict, step: str, meta: dict | None = None) -> None:
    log[step] = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        **(meta or {}),
    }


#: The wizard's steps in execution order, as keyed in ``.setup_log.json``.
_STEP_KEYS = (
    "step1_connection",
    "step2_schema",
    "step3_aliases",
    "step4_rules",
    "step5_examples",
    "step6_write",
    "step7_validate",
)

#: The files step 6 writes.
_WIZARD_OUTPUT_FILES = (
    "entities.yaml",
    "aliases.yaml",
    "business_rules.yaml",
    "examples.yaml",
    "relationships.yaml",
)


def _step_done(log: dict, step: str) -> bool:
    """Whether *step* has a completion record in *log*.

    Args:
        log: The parsed ``.setup_log.json``.
        step: A key from ``_STEP_KEYS``.

    Returns:
        True when the step's entry is a mapping with a ``completed_at``.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> _step_done({"step1_connection": {"completed_at": "2026-01-01T00:00:00+00:00"}}, "step1_connection")
        True
        >>> _step_done({}, "step1_connection")
        False
        >>> _step_done({"step1_connection": "garbage"}, "step1_connection")
        False
    """
    entry = log.get(step)
    return isinstance(entry, dict) and bool(entry.get("completed_at"))


def _schema_fingerprint(snapshot) -> str:
    """Return a digest of the tables and columns in *snapshot*.

    Stored with step 2 so that a resumed run can tell whether the aliases,
    rules and examples generated earlier still describe the schema it just
    read.

    Args:
        snapshot: A schema snapshot with ``tables`` (each with ``full_name``
            and ``columns``).

    Returns:
        A hex SHA-256 digest, independent of table and column order.

    Raises:
        Nothing for a well-formed snapshot.

    Examples:
        >>> from types import SimpleNamespace as NS
        >>> t = NS(full_name="dbo.A", columns=[NS(name="x"), NS(name="y")])
        >>> u = NS(full_name="dbo.A", columns=[NS(name="y"), NS(name="x")])
        >>> _schema_fingerprint(NS(tables=[t])) == _schema_fingerprint(NS(tables=[u]))
        True
        >>> _schema_fingerprint(NS(tables=[t])) == _schema_fingerprint(NS(tables=[]))
        False
    """
    import hashlib

    lines = sorted(
        f"{t.full_name}:{','.join(sorted(c.name for c in t.columns))}" for t in snapshot.tables
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _stored_result(log: dict, step: str, *, language: str | None = None) -> Any:
    """Return the result a previous run stored for *step*, or ``None``.

    Args:
        log: The parsed ``.setup_log.json``.
        step: One of the LLM steps (``step3_aliases``, ``step4_rules``,
            ``step5_examples``).
        language: When given, the stored result is only returned if it was
            generated for this language.

    Returns:
        The stored result, or ``None`` when the step did not complete, kept no
        result (a log written by an older version), was generated for another
        language, or was produced by the mock LLM.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> log = {"step5_examples": {"completed_at": "t", "language": "en", "result": [1]}}
        >>> _stored_result(log, "step5_examples", language="en")
        [1]
        >>> _stored_result(log, "step5_examples", language="fa") is None
        True
        >>> _stored_result({}, "step5_examples") is None
        True
        >>> mocked = {"step5_examples": {"completed_at": "t", "provider": "mock", "result": []}}
        >>> _stored_result(mocked, "step5_examples") is None
        True
    """
    if not _step_done(log, step):
        return None
    entry = log[step]
    if "result" not in entry:
        return None
    if language is not None and entry.get("language") != language:
        return None
    if entry.get("provider") == "mock":
        # Generated by the stub, i.e. empty: a later run with a working
        # model must not be handed that as if it were a result.
        return None
    return entry["result"]


# ===========================================================================
# YAML helpers — produce strings that pass config_loader validators
# ===========================================================================

def _entities_yaml(
    entities: dict[str, dict],
    source_url: str = "",
    generated_at: str = "",
) -> str:
    """Serialise entities dict to YAML matching EntitiesConfig schema.

    Expected structure per entity::

        {"table": str, "schema": str|None, "aliases": list[str]}
    """
    header = (
        "# AUTO-GENERATED by setup_project.py — review before use\n"
        f"# Generated: {generated_at or datetime.utcnow().isoformat(timespec='seconds')}\n"
        f"# Source: {source_url}\n\n"
    )
    data: dict[str, Any] = {"entities": {}}
    for name, info in entities.items():
        entry: dict[str, Any] = {
            "aliases": info.get("aliases", []),
            "table": info["table"],
        }
        if info.get("schema"):
            entry["schema_name"] = info["schema"]
        data["entities"][name] = entry
    return header + yaml.dump(data, allow_unicode=True, sort_keys=False)


def _aliases_yaml(ring_aliases: dict, synonyms: dict, generated_at: str = "") -> str:
    header = (
        "# AUTO-GENERATED by setup_project.py — review before use\n"
        f"# Generated: {generated_at or datetime.utcnow().isoformat(timespec='seconds')}\n\n"
    )
    data = {"ring_aliases": ring_aliases, "synonyms": synonyms}
    return header + yaml.dump(data, allow_unicode=True, sort_keys=False)


def _business_rules_yaml(rules: dict[str, str], generated_at: str = "") -> str:
    header = (
        "# AUTO-GENERATED by setup_project.py — review before use\n"
        f"# Generated: {generated_at or datetime.utcnow().isoformat(timespec='seconds')}\n\n"
    )
    data: dict[str, Any] = {
        "rules": {k: {"rule_text": v} for k, v in rules.items()}
    }
    return header + yaml.dump(data, allow_unicode=True, sort_keys=False)


def _examples_yaml(examples: list[dict], generated_at: str = "") -> str:
    header = (
        "# AUTO-GENERATED by setup_project.py — review before use\n"
        f"# Generated: {generated_at or datetime.utcnow().isoformat(timespec='seconds')}\n\n"
    )
    clean = [
        {
            "tags":     ex.get("tags", []),
            "question": ex.get("question", ""),
            "sql":      ex.get("sql", ""),
        }
        for ex in examples
        if ex.get("question") and ex.get("sql")
    ]
    data: dict[str, Any] = {"examples": clean}
    return header + yaml.dump(data, allow_unicode=True, sort_keys=False)


def _relationships_yaml(relationships: list[dict], generated_at: str = "") -> str:
    header = (
        "# AUTO-GENERATED by setup_project.py — review before use\n"
        f"# Generated: {generated_at or datetime.utcnow().isoformat(timespec='seconds')}\n\n"
    )
    data: dict[str, Any] = {"relationships": relationships}
    return header + yaml.dump(data, allow_unicode=True, sort_keys=False)


# ===========================================================================
# Validation against Pydantic models
# ===========================================================================

_REPO_ROOT = Path(__file__).resolve().parent
_EXAMPLE_CONFIG_DIR = _REPO_ROOT / "project_config.example"


def _config_loader():
    """Return the ``knowledge.config_loader`` module, importable on a fresh checkout.

    Importing anything under ``knowledge`` runs ``knowledge/__init__.py``,
    which reads five files of the *default* ``project_config/`` (aliases,
    business rules, entities, examples, metrics) on the spot. On a fresh
    checkout, or a ``project_config/`` the wizard has only half filled, that
    raises ``ConfigNotFoundError`` before the wizard can validate or even name
    what is missing. The import is therefore made once with the loaders
    pointed at the committed ``project_config.example/``, which always
    complete; the wizard never reads those values, only the loader functions
    and models, and re-points them at the output directory before use.

    Returns:
        The imported ``knowledge.config_loader`` module.

    Raises:
        ImportError: When the module cannot be imported at all, e.g. the
            wizard is run outside a full checkout.
    """
    import sys

    module = sys.modules.get("knowledge.config_loader")
    if module is not None:
        return module
    from config import override_settings

    try:
        with override_settings(project_config_dir=str(_EXAMPLE_CONFIG_DIR)):
            import knowledge.config_loader as module
    except Exception as exc:  # noqa: BLE001 - ConfigNotFoundError, ValueError, ...
        raise ImportError(
            f"could not import knowledge.config_loader ({type(exc).__name__}: {exc}); "
            "run the wizard from a complete checkout"
        ) from exc
    return module


def _validate_yaml_str(yaml_str: str, filename: str) -> list[str]:
    """Parse *yaml_str* and validate against the relevant Pydantic model.

    Args:
        yaml_str: The generated file content.
        filename: Which project_config file it is (selects the model). Files
            without a model here are not checked and count as valid.

    Returns:
        A list of error strings (empty list = valid).

    Raises:
        Nothing. Any failure, including an unimportable loader, is returned
        as an error string.

    Examples:
        >>> _validate_yaml_str("entities: {}", "entities.yaml")
        []
        >>> _validate_yaml_str("anything", "relationships.yaml")
        []
        >>> bool(_validate_yaml_str("entities: [1, 2]", "entities.yaml"))
        True
    """
    try:
        loader = _config_loader()
        model_map = {
            "entities.yaml":       loader.EntitiesConfig,
            "aliases.yaml":        loader.AliasesConfig,
            "business_rules.yaml": loader.BusinessRulesConfig,
            "examples.yaml":       loader.ExamplesConfig,
        }
        model = model_map.get(filename)
        if model is None:
            return []
        raw = safe_load_strict(yaml_str) or {}
        model.model_validate(raw)
        return []
    except Exception as exc:  # noqa: BLE001
        return [str(exc)]


# ===========================================================================
# Interactive review for a single file
# ===========================================================================

def _review_file(
    filename: str,
    content: str,
    non_interactive: bool,
    dry_run: bool,
    output_dir: Path,
    llm_regenerate_fn: Callable[[], str] | None = None,
) -> str | None:
    """Show *content* to the user and return the accepted version (or None to skip).

    Args:
        filename: Name of the file under review (shown in the panel and used
            to pick the validation model).
        content: The generated file content.
        non_interactive: Accept *content* as it is, without prompting.
        dry_run: Print *content* and return ``None``; nothing is accepted.
        output_dir: Directory the file will be written to (not used here).
        llm_regenerate_fn: Produces a fresh version of the whole file by
            asking the LLM again. When ``None`` the "Regenerate" choice is not
            offered, because there is nothing it could do (e.g. the file is
            built from the schema alone, or there is no LLM).

    Returns:
        The accepted content, or ``None`` when the file is skipped (or on a
        dry run).

    Raises:
        Nothing from regeneration: a failing *llm_regenerate_fn* is reported
        and the current content is kept.
    """
    if dry_run:
        console.print(f"\n[bold cyan]--- DRY RUN: {filename} ---[/bold cyan]" if _RICH else f"\n--- {filename} ---")
        console.print(content)
        return None

    if non_interactive:
        return content

    while True:
        if _RICH:
            console.print(Panel(content[:3000] + ("\n...(truncated)" if len(content) > 3000 else ""),
                                title=f"[bold]{filename}[/bold]", border_style="blue"))
        else:
            print(f"\n--- {filename} ---")
            print(content[:3000])

        errs = _validate_yaml_str(content, filename)
        if errs:
            console.print(f"[red]Validation warnings: {errs}[/red]" if _RICH else f"Validation: {errs}")

        choices = ["Accept", "Edit in $EDITOR"]
        if llm_regenerate_fn is not None:
            choices.append("Regenerate")
        choices.append("Skip")
        action = _choose("Action?", choices=choices, default="Accept")

        if action == "Accept":
            return content
        if action == "Edit in $EDITOR":
            content = _edit_in_editor(content)
        elif action == "Regenerate" and llm_regenerate_fn is not None:
            try:
                content = llm_regenerate_fn()
            except Exception as exc:  # noqa: BLE001
                console.print(f"  Regeneration failed ({exc}); keeping the current version.", markup=False)
        elif action == "Skip":
            return None


# ===========================================================================
# LLM prompt builders + callers
# ===========================================================================

def _prompt_aliases(table_name: str, columns: list[str], samples: list[str], language: str) -> str:
    samples_str = ", ".join(f'"{s}"' for s in samples[:10]) if samples else "(none)"
    return textwrap.dedent(f"""\
        You are a database labelling expert.
        Table name: {table_name}
        Columns: {', '.join(columns)}
        Sample values: {samples_str}
        User question language: {language}

        Generate natural language aliases a business user might use when referring to this table.
        Consider abbreviations, domain jargon, and {'Persian' if language == 'fa' else 'English'} terms.

        Return ONLY this JSON (no markdown):
        {{"aliases": ["alias1", "alias2"], "description": "one-sentence description"}}
    """)


def _prompt_business_rules(
    table_name: str,
    columns: list[dict],
    dim_tables: list[str],
    fk_joins: list[str],
) -> str:
    col_str = "\n".join(f"  {c['name']} ({c['type']})" for c in columns)
    join_str = "\n".join(f"  {j}" for j in fk_joins) if fk_joins else "  (none)"
    dims = ", ".join(dim_tables) if dim_tables else "(none)"
    return textwrap.dedent(f"""\
        You are a SQL business rules expert.
        Fact table: {table_name}
        Columns:
        {col_str}
        Related dimension tables: {dims}
        FK join hints:
        {join_str}

        Generate business rules for a natural-language-to-SQL agent.
        Identify:
        - Which column to use for value/amount queries (e.g. TotalPrice, Amount)
        - Which column to use for volume/count queries (e.g. Quantity, Count)
        - A plain-language rule text summarising query patterns

        Return ONLY this JSON (no markdown):
        {{"rules": {{"value_col": "ColumnName", "volume_col": "ColumnName", "rule_text": "..."}}}}
    """)


def _prompt_examples(schema_summary: str, language: str) -> str:
    return textwrap.dedent(f"""\
        You are an expert at writing natural language database queries.
        Database schema summary:
        {schema_summary}
        User question language: {'Persian (Farsi)' if language == 'fa' else 'English'}

        Generate 10 diverse NLQ-to-SQL example pairs covering:
        - Simple counts
        - Aggregations (SUM, AVG)
        - Top-N queries
        - Date/time filtering
        - Multi-table JOINs

        Return ONLY this JSON array (no markdown):
        [
          {{"tags": ["count"], "question": "...", "sql": "SELECT ..."}},
          ...
        ]
    """)


# ===========================================================================
# Schema summary helper
# ===========================================================================

def _build_schema_summary(snapshot) -> str:
    lines = []
    for t in snapshot.tables[:20]:  # cap for prompt length
        col_names = ", ".join(c.name for c in t.columns[:10])
        lines.append(f"{t.full_name} ({t.classification}): {col_names}")
    return "\n".join(lines)


# ===========================================================================
# Wizard LLM: which endpoint, and whether it may see the schema
# ===========================================================================

_WIZARD_PROVIDERS = ("openai", "mock")


class RemoteLLMNotAllowedError(RuntimeError):
    """The wizard's LLM endpoint is remote and ``LLM_ALLOW_REMOTE`` is not true."""


@dataclass(frozen=True)
class WizardLLMConfig:
    """The LLM endpoint the wizard will use, and where each value came from.

    Attributes:
        provider: ``"openai"`` (any OpenAI-compatible endpoint) or ``"mock"``.
        model: Model tag sent to the endpoint.
        base_url: Endpoint base URL. Empty for the mock provider.
        api_key: Bearer token, possibly empty (many local servers need none).
            Never printed or logged.
        trusted: Whether the endpoint counts as local under the main
            application's rule (see :func:`resolve_wizard_llm`).
        sources: Where ``provider``, ``model`` and ``base_url`` were taken
            from, e.g. ``{"model": "OPENAI_MODEL"}``.
    """

    provider: str
    model: str
    base_url: str
    api_key: str
    trusted: bool
    sources: dict[str, str]

    @property
    def remote(self) -> bool:
        """Whether this endpoint is outside the deployment's own infrastructure."""
        return self.provider != "mock" and not self.trusted

    def describe(self) -> str:
        """One line naming the provider, model, endpoint and their origin.

        The API key is never part of it.

        Returns:
            A single line of text.

        Examples:
            >>> WizardLLMConfig("openai", "m", "http://localhost:8000/v1", "k", True,
            ...                 {"model": "OPENAI_MODEL", "base_url": "OPENAI_BASE_URL"}).describe()
            'LLM: openai, model m (from OPENAI_MODEL), endpoint http://localhost:8000/v1 (from OPENAI_BASE_URL), local'
            >>> WizardLLMConfig("mock", "mock", "", "", True, {}).describe()
            'LLM: mock (no model is called; aliases, rules and examples will be empty)'
        """
        if self.provider == "mock":
            return "LLM: mock (no model is called; aliases, rules and examples will be empty)"
        where = "remote" if self.remote else "local"
        return (
            f"LLM: {self.provider}, model {self.model} (from {self.sources.get('model', '?')}), "
            f"endpoint {_strip_url_userinfo(self.base_url)} (from {self.sources.get('base_url', '?')}), "
            f"{where}"
        )


def _strip_url_userinfo(url: str) -> str:
    """Return an HTTP(S) *url* without any ``user:password@`` part.

    Args:
        url: An endpoint URL.

    Returns:
        *url* with the userinfo removed; unchanged when there is none.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> _strip_url_userinfo("https://u:p@host:8000/v1")
        'https://host:8000/v1'
        >>> _strip_url_userinfo("http://localhost:8000/v1")
        'http://localhost:8000/v1'
    """
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
        if "@" not in parts.netloc:
            return url
        return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))
    except ValueError:
        return "<unparseable endpoint URL>"


def resolve_wizard_llm(
    args: argparse.Namespace,
    environ: Mapping[str, str] | None = None,
) -> WizardLLMConfig:
    """Work out which LLM endpoint the wizard uses.

    For the endpoint, the model and the key the order is: the command-line
    flag, then the ``WIZARD_LLM_*`` variable, then the main application's own
    ``OPENAI_BASE_URL`` / ``OPENAI_MODEL`` / ``OPENAI_API_KEY`` settings. An
    empty ``WIZARD_*`` value counts as unset, so a copied ``.env.example``
    with blank lines behaves as "use the application's endpoint". The key is
    only ever the application's ``OPENAI_API_KEY``.

    Whether the endpoint is *remote* follows the main application: its trust
    is the explicit ``LLM_TRUSTED`` override when the endpoint is the
    application's own ``OPENAI_BASE_URL``, otherwise
    :func:`llm.trust.default_trust_for_url` (loopback, private-network and
    ``*.local`` hosts are local, everything else is remote).

    Args:
        args: Parsed command line (``llm_provider``, ``llm_model``,
            ``llm_base_url``).
        environ: Environment to read ``WIZARD_*`` variables from. Defaults to
            ``os.environ``.

    Returns:
        The resolved configuration.

    Raises:
        ValueError: When the provider is not ``openai`` or ``mock``.

    Examples:
        >>> import config as cfg
        >>> ns = argparse.Namespace(llm_provider=None, llm_model=None, llm_base_url=None)
        >>> with cfg.override_settings(openai_base_url="http://localhost:8000/v1",
        ...                            openai_model="local-model", openai_api_key=""):
        ...     resolved = resolve_wizard_llm(ns, {"WIZARD_LLM_MODEL": ""})
        >>> resolved.model, resolved.sources["model"], resolved.remote
        ('local-model', 'OPENAI_MODEL', False)
    """
    import config as cfg
    from llm.trust import default_trust_for_url

    env = os.environ if environ is None else environ

    def pick(flag_value: str | None, flag: str, name: str, fallback: str, fallback_name: str) -> tuple[str, str]:
        if flag_value and flag_value.strip():
            return flag_value.strip(), flag
        value = (env.get(name) or "").strip()
        if value:
            return value, name
        return (fallback or "").strip(), fallback_name

    provider, provider_src = pick(args.llm_provider, "--llm-provider", "WIZARD_LLM_PROVIDER", "openai", "default")
    provider = provider.lower()
    if provider not in _WIZARD_PROVIDERS:
        raise ValueError(
            f"Unsupported wizard LLM provider {provider!r} (from {provider_src}). "
            f"Choose one of: {', '.join(_WIZARD_PROVIDERS)}."
        )
    if provider == "mock":
        return WizardLLMConfig("mock", "mock", "", "", True, {"provider": provider_src})

    model, model_src = pick(args.llm_model, "--llm-model", "WIZARD_LLM_MODEL",
                            cfg.settings.openai_model, "OPENAI_MODEL")
    base_url, url_src = pick(args.llm_base_url, "--llm-base-url", "WIZARD_LLM_BASE_URL",
                             cfg.settings.openai_base_url, "OPENAI_BASE_URL")

    app_url = (cfg.settings.openai_base_url or "").rstrip("/")
    if cfg.settings.llm_trusted is not None and base_url.rstrip("/") == app_url:
        trusted = cfg.settings.llm_trusted
    else:
        trusted = default_trust_for_url(base_url)

    return WizardLLMConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=cfg.settings.openai_api_key or "",
        trusted=trusted,
        sources={"provider": provider_src, "model": model_src, "base_url": url_src},
    )


def enforce_remote_policy(llm_config: WizardLLMConfig) -> None:
    """Refuse to use a remote endpoint unless ``LLM_ALLOW_REMOTE`` is true.

    The wizard sends table and column names, sample column values and a
    schema summary to the model, which is exactly the data the main
    application's governance gate (``LLM_ALLOW_REMOTE``, see
    ``llm/router.py``) keeps off hosted providers. Same rule here, same
    definition of "remote" (see :func:`resolve_wizard_llm`).

    Args:
        llm_config: The resolved wizard LLM configuration.

    Returns:
        None. Returns normally for the mock provider, for a local endpoint, and
        for a remote one when ``LLM_ALLOW_REMOTE`` is true.

    Raises:
        RemoteLLMNotAllowedError: When the endpoint is remote and
            ``cfg.settings.llm_allow_remote`` is false.

    Examples:
        >>> import config as cfg
        >>> remote = WizardLLMConfig("openai", "m", "https://api.openai.com/v1", "k", False, {})
        >>> with cfg.override_settings(llm_allow_remote=False):
        ...     enforce_remote_policy(remote)
        Traceback (most recent call last):
            ...
        setup_project.RemoteLLMNotAllowedError: Refusing to send ...
    """
    import config as cfg

    if not llm_config.remote or cfg.settings.llm_allow_remote:
        return
    raise RemoteLLMNotAllowedError(
        f"Refusing to send schema data to the remote LLM endpoint "
        f"{_strip_url_userinfo(llm_config.base_url)} (from {llm_config.sources.get('base_url', '?')}): "
        "the wizard sends table and column names and sample column values to the model, "
        "and LLM_ALLOW_REMOTE is not true. Either point WIZARD_LLM_BASE_URL (or OPENAI_BASE_URL) "
        "at a local endpoint, set LLM_TRUSTED=true if this endpoint is on your own infrastructure, "
        "set LLM_ALLOW_REMOTE=true to opt in deliberately, or run with --llm-provider mock."
    )


def _build_wizard_llm(llm_config: WizardLLMConfig):
    """Return a ``WizardLLM`` talking to the endpoint in *llm_config*.

    Built from the already-resolved values, so the endpoint, key and trust
    flag are exactly the ones that were checked and announced, instead of
    being re-read from the environment by ``WizardLLM`` (which would use its
    own default endpoint and insist on a key even for a local server).

    Args:
        llm_config: The resolved wizard LLM configuration.

    Returns:
        A ``llm.wizard_llm.WizardLLM`` instance.

    Raises:
        ValueError: When the provider is unsupported.
    """
    from llm.providers import OpenAIBackend
    from llm.wizard_llm import WizardLLM

    class _ResolvedWizardLLM(WizardLLM):
        def _build_backend(self, provider, model, base_url):  # type: ignore[override]
            if provider == "openai":
                return OpenAIBackend(
                    model=model,
                    api_key=llm_config.api_key,
                    base_url=llm_config.base_url,
                    trusted=llm_config.trusted,
                )
            return WizardLLM._build_backend(provider, model, base_url)

    return _ResolvedWizardLLM(
        provider=llm_config.provider, model=llm_config.model, base_url=llm_config.base_url or None
    )


def setup_wizard_llm(args: argparse.Namespace):
    """Resolve, vet, announce and connect the wizard's LLM.

    Prints one line saying which provider, model and endpoint is used (never
    the key). A remote endpoint without ``LLM_ALLOW_REMOTE`` is refused
    before anything is sent. An endpoint that does not answer falls back to
    the mock LLM with a loud notice, because the generated files will then
    hold no aliases, rules or examples.

    Args:
        args: Parsed command line.

    Returns:
        A ``WizardLLM`` ready for ``generate()``.

    Raises:
        ValueError: When the configured provider is unsupported.
        RemoteLLMNotAllowedError: When the endpoint is remote and not allowed.
    """
    from llm.wizard_llm import WizardLLM

    llm_config = resolve_wizard_llm(args)
    enforce_remote_policy(llm_config)
    console.print(f"  {llm_config.describe()}")
    if llm_config.provider == "mock":
        return WizardLLM(provider="mock", model="mock")
    if llm_config.remote:
        console.print(
            "  NOTICE: LLM_ALLOW_REMOTE=true. Table and column names and sample column values "
            "are sent to the remote endpoint above."
        )

    reason = ""
    try:
        llm = _build_wizard_llm(llm_config)
        if llm.test_connection():
            return llm
        reason = "the endpoint did not answer a model-list request"
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
    if llm_config.api_key:
        reason = reason.replace(llm_config.api_key, "***")
    console.print(
        f"\n  !!! WARNING: the LLM is unavailable ({reason}). FALLING BACK TO THE MOCK LLM. !!!\n"
        "  !!! Aliases, business rules and examples will be EMPTY. Fix the endpoint "
        "(WIZARD_LLM_BASE_URL / OPENAI_BASE_URL) and run the wizard again. !!!\n"
    )
    return WizardLLM(provider="mock", model="mock")


# ===========================================================================
# Step implementations
# ===========================================================================

def step1_connection(args, log: dict) -> str:
    """Return a validated DB URL."""
    console.rule("[bold blue]Step 1: Database Connection[/bold blue]" if _RICH else "Step 1: Database Connection")

    db_url = (
        args.db_url
        or os.getenv("DB_CONNECTION_URL")
        or os.getenv("DATABASE_URL")
    )

    if not db_url:
        if args.non_interactive:
            sys.exit("ERROR: --db-url is required in non-interactive mode.")
        db_url = _ask(
            "Enter SQLAlchemy connection string",
            default="mssql+pyodbc://server/db?driver=ODBC+Driver+17+for+SQL+Server",
        )

    console.print("  Testing connection...", end=" ")
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(db_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        console.print("[green]OK[/green]" if _RICH else "OK")
    except Exception as exc:  # noqa: BLE001
        console.print("[red]FAILED[/red]" if _RICH else "FAILED")
        console.print(f"  Error: {_scrub_secrets(str(exc), db_url)}")
        console.print("  Connection string formats:" + _DB_EXAMPLES)
        if not args.non_interactive and _yn("Retry with a different URL?", non_interactive=False):
            return step1_connection(args, log)
        sys.exit(1)

    _mark_done(log, "step1_connection", {"db_url_redacted": _redact_db_url(db_url)})
    return db_url


def step2_schema(args, db_url: str, log: dict):
    """Run schema inspection and return a SchemaSnapshot."""
    console.rule("[bold blue]Step 2: Schema Discovery[/bold blue]" if _RICH else "Step 2: Schema Discovery")

    include_schemas = (
        [s.strip() for s in args.include_schemas.split(",") if s.strip()]
        if getattr(args, "include_schemas", None)
        else None
    )

    from database.schema_inspector import SchemaInspector
    inspector = SchemaInspector(db_url, sample_rows=5)

    with _spinner("Inspecting schema..."):
        try:
            snapshot = inspector.inspect(
                include_schemas=include_schemas,
                fetch_row_counts=False,
            )
        except ConnectionError as exc:
            console.print(f"[red]Schema inspection failed: {exc}[/red]" if _RICH else str(exc))
            sys.exit(1)
        finally:
            inspector.close()

    console.print(
        f"  Found [bold]{len(snapshot.tables)}[/bold] tables: "
        f"[cyan]{len(snapshot.fact_tables)}[/cyan] fact, "
        f"[green]{len(snapshot.dim_tables)}[/green] dim"
        if _RICH else
        f"  Found {len(snapshot.tables)} tables: {len(snapshot.fact_tables)} fact, {len(snapshot.dim_tables)} dim"
    )

    # Show table list in a table widget
    if _RICH:
        tbl = Table(title="Discovered Tables", show_lines=False)
        tbl.add_column("Table", style="cyan")
        tbl.add_column("Schema")
        tbl.add_column("Class", style="yellow")
        tbl.add_column("Cols", justify="right")
        tbl.add_column("FKs", justify="right")
        for t in snapshot.tables:
            tbl.add_row(t.name, t.schema or "", t.classification, str(len(t.columns)), str(len(t.foreign_keys)))
        console.print(tbl)
    else:
        for t in snapshot.tables:
            print(f"  {t.full_name:40s} {t.classification:10s} cols={len(t.columns)} fks={len(t.foreign_keys)}")

    # Ask to exclude tables
    if not args.non_interactive:
        excl = _ask("Tables to exclude (comma-separated, blank=none)", default="")
        if excl.strip():
            excluded = {e.strip() for e in excl.split(",") if e.strip()}
            snapshot.tables = [t for t in snapshot.tables if t.name not in excluded]
            snapshot.relationships = [
                r for r in snapshot.relationships
                if r.from_table not in excluded and r.to_table not in excluded
            ]
            console.print(f"  Excluded {len(excluded)} table(s).")

    _mark_done(log, "step2_schema", {
        "schema_fingerprint": _schema_fingerprint(snapshot),
        "table_count": len(snapshot.tables),
        "fact_count": len(snapshot.fact_tables),
        "dim_count": len(snapshot.dim_tables),
    })
    return snapshot


def step3_aliases(args, snapshot, llm, log: dict, language: str) -> dict:
    """Generate entity aliases via LLM and return entities dict."""
    console.rule("[bold blue]Step 3: LLM Alias Generation[/bold blue]" if _RICH else "Step 3: Alias Generation")

    entities: dict[str, dict] = {}
    ring_aliases: dict[str, list[str]] = {}
    total = len(snapshot.tables)

    for idx, table in enumerate(snapshot.tables, 1):
        console.print(f"  [{idx}/{total}] {table.full_name} ...", end=" ")

        col_names   = [c.name for c in table.columns]
        all_samples = [s for c in table.columns for s in c.sample_values]

        try:
            prompt = _prompt_aliases(table.name, col_names, all_samples, language)
            result = llm.generate(prompt, expect_json=True)
            aliases     = result.get("aliases", []) if isinstance(result, dict) else []
            description = result.get("description", "") if isinstance(result, dict) else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alias generation failed for %s: %s", table.name, exc)
            aliases, description = [], ""

        console.print(f"[green]{len(aliases)} aliases[/green]" if _RICH else f"{len(aliases)} aliases")

        if not args.non_interactive and not args.dry_run:
            aliases_str = ", ".join(f'"{a}"' for a in aliases) or "(none)"
            console.print(f"    Aliases: {aliases_str}")
            if description:
                console.print(f"    Desc   : {description}")
            action = _choose(
                f"  Accept aliases for {table.name}?",
                choices=["Accept", "Edit", "Clear"],
                default="Accept",
            )
            if action == "Edit":
                edited = _ask("Enter aliases (comma-separated)", default=", ".join(aliases))
                aliases = [a.strip().strip('"') for a in edited.split(",") if a.strip()]
            elif action == "Clear":
                aliases = []

        entities[table.name] = {
            "table":   table.name,
            "schema":  table.schema,
            "aliases": aliases,
        }
        if aliases:
            ring_aliases[table.name] = aliases

    _mark_done(log, "step3_aliases", {
        "entity_count": len(entities),
        "language": language,
        "provider": llm.provider,
        "result": {"entities": entities, "ring_aliases": ring_aliases},
    })
    return {"entities": entities, "ring_aliases": ring_aliases}


def step4_business_rules(args, snapshot, llm, log: dict) -> dict:
    """Generate business rules for fact tables."""
    console.rule("[bold blue]Step 4: Business Rules[/bold blue]" if _RICH else "Step 4: Business Rules")

    rules: dict[str, str] = {}
    fact_tables = snapshot.fact_tables
    if not fact_tables:
        console.print("  No fact tables detected — skipping.")
        _mark_done(log, "step4_rules", {"rule_count": 0, "provider": llm.provider, "result": {}})
        return rules

    # Build FK → dim table map
    fk_map: dict[str, list[str]] = {}
    for t in fact_tables:
        fk_map[t.name] = [fk.referred_table for fk in t.foreign_keys]

    for table in fact_tables:
        console.print(f"  {table.name} ...", end=" ")
        col_dicts = [{"name": c.name, "type": c.type} for c in table.columns]
        dim_tables = fk_map.get(table.name, [])
        fk_joins   = [r.join_hint for r in snapshot.relationships if r.from_table == table.name]

        try:
            prompt = _prompt_business_rules(table.name, col_dicts, dim_tables, fk_joins)
            result = llm.generate(prompt, expect_json=True)
            rule_data = result.get("rules", {}) if isinstance(result, dict) else {}
            rule_text = rule_data.get("rule_text", "") if isinstance(rule_data, dict) else ""
            value_col  = rule_data.get("value_col", "") if isinstance(rule_data, dict) else ""
            volume_col = rule_data.get("volume_col", "") if isinstance(rule_data, dict) else ""
            if value_col:
                rule_text = f"Value column: {value_col}. Volume column: {volume_col}. " + rule_text
        except Exception as exc:  # noqa: BLE001
            logger.warning("Rule generation failed for %s: %s", table.name, exc)
            rule_text = ""

        if not rule_text:
            rule_text = f"Primary fact table: {table.name}. Columns: {', '.join(c.name for c in table.columns[:8])}."

        console.print("[green]done[/green]" if _RICH else "done")
        rules[table.name] = rule_text

    _mark_done(log, "step4_rules", {
        "rule_count": len(rules),
        "provider": llm.provider,
        "result": rules,
    })
    return rules


def step5_examples(args, snapshot, llm, log: dict, language: str) -> list:
    """Generate NLQ→SQL example pairs."""
    console.rule("[bold blue]Step 5: Example Generation[/bold blue]" if _RICH else "Step 5: Examples")

    schema_summary = _build_schema_summary(snapshot)
    try:
        prompt  = _prompt_examples(schema_summary, language)
        result  = llm.generate(prompt, expect_json=True)
        examples = result if isinstance(result, list) else result.get("examples", []) if isinstance(result, dict) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Example generation failed: %s", exc)
        examples = []

    console.print(f"  Generated [bold]{len(examples)}[/bold] examples." if _RICH else f"  Generated {len(examples)} examples.")
    _mark_done(log, "step5_examples", {
        "example_count": len(examples),
        "language": language,
        "provider": llm.provider,
        "result": examples,
    })
    return examples


def _quiet(args: argparse.Namespace) -> argparse.Namespace:
    """Return a copy of *args* that never prompts.

    Used to re-run an LLM step from the review screen without asking the
    per-table questions again.

    Args:
        args: Parsed command line.

    Returns:
        A new namespace equal to *args* except ``non_interactive`` is true.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> ns = argparse.Namespace(non_interactive=False, dry_run=False)
        >>> _quiet(ns).non_interactive, ns.non_interactive
        (True, False)
    """
    return argparse.Namespace(**{**vars(args), "non_interactive": True})


def step6_review_and_write(
    args,
    output_dir: Path,
    generated_at: str,
    source_url: str,
    entities_data: dict,
    ring_aliases: dict,
    rules: dict,
    examples: list,
    snapshot,
    log: dict,
    llm=None,
    language: str = "en",
) -> None:
    """Review generated files and write to disk.

    Each file is shown for review (accept, edit, regenerate, skip). With
    ``--resume``, a file that already exists is left alone.

    "Regenerate" asks the LLM again and shows the new version: for
    ``business_rules.yaml`` and ``examples.yaml`` that re-runs step 4 or 5;
    for ``entities.yaml`` and ``aliases.yaml``, which are both built from the
    step 3 aliases, it re-runs step 3 for every table without the per-table
    questions and re-renders both files (one already written earlier in this
    run is not rewritten, and the screen says so). ``relationships.yaml`` comes
    from the schema, not the model, so it has no "Regenerate". Without *llm*
    (or on a dry run) the choice is not offered at all.

    Args:
        args: Parsed command line.
        output_dir: Directory the files are written to.
        generated_at: Timestamp for the file headers.
        source_url: Redacted connection string for the ``entities.yaml`` header.
        entities_data: Step 3 entities.
        ring_aliases: Step 3 aliases per table.
        rules: Step 4 business rules.
        examples: Step 5 examples.
        snapshot: The schema snapshot.
        log: The setup log, updated in place.
        llm: The wizard LLM used to regenerate a file, or ``None``.
        language: Language the examples and aliases are generated in.

    Returns:
        None. Marks ``step6_write`` in *log* with the files written and the
        files not written.

    Raises:
        Nothing for an LLM failure while regenerating; it is reported and the
        current version is kept.
    """
    console.rule("[bold blue]Step 6: Review & Write[/bold blue]" if _RICH else "Step 6: Review")

    synonyms: dict[str, list[str]] = {}
    for t in snapshot.tables:
        for col in t.columns:
            for val in col.sample_values:
                key = val.lower().strip()
                if len(key) >= 2 and key not in synonyms:
                    synonyms[key] = [t.name.lower()]

    # The generated data, which a regeneration replaces.
    state: dict[str, Any] = {
        "entities_data": entities_data,
        "ring_aliases": ring_aliases,
        "rules": rules,
        "examples": examples,
    }
    written: list[str] = []

    def render(filename: str) -> str:
        if filename == "entities.yaml":
            return _entities_yaml(state["entities_data"], source_url, generated_at)
        if filename == "aliases.yaml":
            return _aliases_yaml(state["ring_aliases"], synonyms, generated_at)
        if filename == "business_rules.yaml":
            return _business_rules_yaml(state["rules"], generated_at)
        if filename == "examples.yaml":
            return _examples_yaml(state["examples"], generated_at)
        return _relationships_yaml(
            [
                {
                    "from_table":  r.from_table,
                    "from_column": r.from_column,
                    "to_table":    r.to_table,
                    "to_column":   r.to_column,
                    "join_hint":   r.join_hint,
                }
                for r in snapshot.relationships
            ],
            generated_at,
        )

    def regenerate(filename: str) -> str:
        quiet = _quiet(args)
        if filename in ("entities.yaml", "aliases.yaml"):
            result = step3_aliases(quiet, snapshot, llm, log, language)
            state["entities_data"], state["ring_aliases"] = result["entities"], result["ring_aliases"]
            other = "aliases.yaml" if filename == "entities.yaml" else "entities.yaml"
            if other in written:
                console.print(
                    f"  Note: {other} was written earlier in this run from the previous aliases "
                    "and is not rewritten; review it again or run the wizard again.",
                    markup=False,
                )
        elif filename == "business_rules.yaml":
            state["rules"] = step4_business_rules(quiet, snapshot, llm, log)
        else:
            state["examples"] = step5_examples(quiet, snapshot, llm, log, language)
        return render(filename)

    can_regenerate = llm is not None and not args.dry_run
    regenerable = ("entities.yaml", "aliases.yaml", "business_rules.yaml", "examples.yaml")

    not_written: list[str] = []
    for filename in _WIZARD_OUTPUT_FILES:
        target = output_dir / filename

        # Resume: skip if file already exists and --resume flag is set
        if args.resume and target.exists():
            console.print(f"  [yellow]Skipped (exists): {filename}[/yellow]" if _RICH else f"  Skipped: {filename}")
            continue

        fn = (lambda name=filename: regenerate(name)) if can_regenerate and filename in regenerable else None
        accepted = _review_file(
            filename=filename,
            content=render(filename),
            non_interactive=args.non_interactive,
            dry_run=args.dry_run,
            output_dir=output_dir,
            llm_regenerate_fn=fn,
        )

        if accepted is not None and not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(accepted, encoding="utf-8")
            console.print(f"  [green]Written:[/green] {target}" if _RICH else f"  Written: {target}")
            written.append(filename)
        elif not args.dry_run:
            not_written.append(filename)

    _mark_done(log, "step6_write", {"files_written": written, "files_not_written": not_written})


def _copy_hints(missing: list[str], output_dir: Path) -> list[str]:
    """Return one shell command per missing file that copies its template.

    Args:
        missing: Names from ``core.project_config_files.REQUIRED_PROJECT_CONFIG_FILES``.
        output_dir: Directory the files belong in.

    Returns:
        Commands in the syntax of the current platform (``cp`` or ``copy``).

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> hints = _copy_hints(["metrics.yaml"], Path("project_config"))
        >>> len(hints), "metrics.yaml" in hints[0]
        (1, True)
    """
    verb = "copy" if os.name == "nt" else "cp"
    return [
        f'{verb} "{Path("project_config.example") / name}" "{output_dir / name}"'
        for name in missing
    ]


def step7_validate(output_dir: Path, log: dict) -> list[str]:
    """Check the files the wizard wrote, and name the required ones still missing.

    The wizard writes five files. A working ``project_config/`` needs ten
    (``core.project_config_files.REQUIRED_PROJECT_CONFIG_FILES``); the rest
    come from ``project_config.example/``. They are never copied in
    automatically: running the application on sample aliases and business
    rules against a real warehouse would give confidently wrong SQL, which is
    why the application has no such fall-back either (see
    ``config.Settings.project_config_dir``). Instead this step lists what is
    missing, with the command that copies each template, and does not claim
    that setup is complete. It never raises for a missing file.

    Args:
        output_dir: The directory the wizard wrote to.
        log: The setup log, updated in place.

    Returns:
        The names of required files that are still missing (empty when the
        directory is complete).

    Raises:
        Nothing for missing or invalid configuration files; each is reported.
    """
    from core.project_config_files import (
        REQUIRED_PROJECT_CONFIG_FILES,
        missing_project_config_files,
    )

    console.rule("[bold blue]Step 7: Validation[/bold blue]" if _RICH else "Step 7: Validation")

    results: dict[str, str] = {}
    entity_count = rule_count = example_count = 0

    try:
        cl = _config_loader()
    except ImportError as exc:
        cl = None
        results["(config loader)"] = f"FAILED: {exc}"

    if cl is not None:
        from config import override_settings

        loaders = [
            ("entities.yaml",       cl.load_entities),
            ("aliases.yaml",        cl.load_aliases),
            ("business_rules.yaml", cl.load_business_rules),
            ("examples.yaml",       cl.load_examples),
        ]
        loaded: dict[str, Any] = {}
        # Point the knowledge layer at our output dir for the duration of this
        # step, via the same Settings.project_config_dir seam every other
        # consumer reads through (config.override_settings). Absolute, because
        # the loaders resolve a relative path against the repository root,
        # not against the directory the wizard was started from.
        with override_settings(project_config_dir=str(output_dir.resolve())):
            for fname, loader_fn in loaders:
                if not (output_dir / fname).exists():
                    results[fname] = "skipped (file not written)"
                    continue
                try:
                    loaded[fname] = loader_fn()
                    results[fname] = "OK"
                except Exception as exc:  # noqa: BLE001
                    results[fname] = f"FAILED: {exc}"
        if "entities.yaml" in loaded:
            entity_count = len(loaded["entities.yaml"].entities)
        if "business_rules.yaml" in loaded:
            rule_count = len(loaded["business_rules.yaml"].rules)
        if "examples.yaml" in loaded:
            example_count = len(loaded["examples.yaml"].examples)

    for fname, status in results.items():
        colour = "green" if status == "OK" else "yellow" if "skipped" in status else "red"
        if _RICH:
            console.print(f"  {fname}: {status}", style=colour, markup=False)
        else:
            print(f"  {fname}: {status}")

    missing = missing_project_config_files(output_dir)
    failed = [f for f, status in results.items() if status.startswith("FAILED")]
    if missing:
        console.print(
            f"\n  The wizard wrote the files above, but {len(missing)} of the "
            f"{len(REQUIRED_PROJECT_CONFIG_FILES)} files a deployment needs are still "
            f"missing from {output_dir}:"
        )
        for name in missing:
            console.print(f"    - {name}")
        console.print(
            "  They are not generated: copy each template from project_config.example/ "
            "and edit it for your data, e.g.:"
        )
        for hint in _copy_hints(missing, output_dir):
            console.print(f"    {hint}")
        console.print(
            "  Then run python scripts/verify_deployment.py. Setup is NOT complete until "
            "those files exist."
        )
    elif failed:
        console.print("\n  Setup is NOT complete: fix the files reported FAILED above.")
    else:
        console.print(
            f"\n  Setup complete. Registry: {entity_count} entities, "
            f"{rule_count} rules, {example_count} examples."
        )

    _mark_done(log, "step7_validate", {
        "entity_count": entity_count,
        "rule_count": rule_count,
        "example_count": example_count,
        "missing_files": missing,
    })
    return missing


# ===========================================================================
# CLI
# ===========================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python setup_project.py",
        description="One-time project setup wizard for local-sql-agent.",
    )
    p.add_argument("--db-url",       metavar="URL",  default=None)
    p.add_argument("--llm-provider", metavar="NAME", default=None,
                   choices=["openai", "mock"],
                   help="openai (any OpenAI-compatible endpoint) or mock. "
                        "Default: WIZARD_LLM_PROVIDER, else openai.")
    p.add_argument("--llm-model",    metavar="NAME", default=None,
                   help="Model tag. Default: WIZARD_LLM_MODEL, else OPENAI_MODEL.")
    p.add_argument("--llm-base-url", metavar="URL",  default=None,
                   help="Endpoint base URL. Default: WIZARD_LLM_BASE_URL, else "
                        "OPENAI_BASE_URL. A remote endpoint is refused unless "
                        "LLM_ALLOW_REMOTE=true, as in the application itself.")
    p.add_argument("--language",     metavar="LANG", default=None,
                   choices=["fa", "en", "both"])
    p.add_argument("--output",       metavar="DIR",  default=str(_DEFAULT_OUTPUT))
    p.add_argument("--review",       metavar="MODE", default="interactive",
                   choices=["interactive", "auto"])
    p.add_argument("--include-schemas", metavar="SCHEMAS", default=None)
    p.add_argument("--non-interactive", action="store_true", default=False)
    p.add_argument("--dry-run",         action="store_true", default=False,
                   help="Print the generated files; write nothing, the setup log included.")
    p.add_argument("--resume",          action="store_true", default=False,
                   help="Continue an interrupted run from the first step "
                        ".setup_log.json does not record as completed. The "
                        "connection and schema steps run again (the connection "
                        "string is not stored); aliases, rules and examples of "
                        "completed steps are reused if the schema and language "
                        "are unchanged; existing files are not rewritten; when "
                        "steps 1-6 are all done only the validation runs.")
    return p


def _restorable_results(
    log: dict,
    language: str,
) -> tuple[dict | None, dict | None, list | None]:
    """Return the step 3, 4 and 5 results a previous run stored, as far as usable.

    Args:
        log: The parsed ``.setup_log.json`` of the previous run.
        language: The language of this run; results generated for another
            language are not reused.

    Returns:
        ``(aliases, rules, examples)``. Each is ``None`` when it was not
        stored, has the wrong shape, or was generated for another language.
        *aliases* is ``{"entities": ..., "ring_aliases": ...}``.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> _restorable_results({}, "en")
        (None, None, None)
    """
    aliases = _stored_result(log, "step3_aliases", language=language)
    if not (isinstance(aliases, dict) and isinstance(aliases.get("entities"), dict)
            and isinstance(aliases.get("ring_aliases"), dict)):
        aliases = None
    rules = _stored_result(log, "step4_rules")
    if not isinstance(rules, dict):
        rules = None
    examples = _stored_result(log, "step5_examples", language=language)
    if not isinstance(examples, list):
        examples = None
    return aliases, rules, examples


def main(argv: list[str] | None = None) -> int:
    """Run the wizard.

    With ``--resume`` the run continues from the first step that
    ``.setup_log.json`` does not record as completed: when steps 1 to 6 are
    all recorded and every file exists it goes straight to validation, without
    touching the database or the LLM; otherwise it reconnects and re-reads the
    schema (the connection string is never stored), reuses the stored aliases,
    rules and examples of every completed LLM step (so the model is not asked
    again) as long as the schema and language are unchanged, and writes only
    the files that do not exist yet. Without ``--resume`` a fresh log is
    started.

    Args:
        argv: Command-line arguments; ``sys.argv[1:]`` when ``None``.

    Returns:
        0 on success, 2 when the LLM configuration is refused or invalid.
        A failed database connection exits the process with status 1.

    Raises:
        SystemExit: On a bad command line or a failed database connection.
    """
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = _build_parser()
    args   = parser.parse_args(argv)

    if args.review == "auto":
        args.non_interactive = True

    output_dir = Path(args.output)
    log_path   = output_dir / ".setup_log.json"
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.dry_run and args.resume:
        console.print("  Note: --resume is ignored with --dry-run (nothing is written).", markup=False)
        args.resume = False
    # A fresh run starts a fresh log: leftover entries of an earlier run would
    # otherwise look like completed steps to a later --resume.
    log: dict = _load_log(log_path) if args.resume else {}

    def save() -> None:
        # A dry run writes no files, the log included.
        if not args.dry_run:
            _save_log(log_path, log)

    if _RICH:
        console.print(Panel(
            "[bold]local-sql-agent[/bold] — Project Setup Wizard",
            subtitle=f"output: {output_dir}",
            border_style="bold blue",
        ))
    else:
        print("=" * 60)
        print("local-sql-agent — Project Setup Wizard")
        print(f"Output: {output_dir}")
        print("=" * 60)

    # ---- Resume: everything through step 6 is already done ----
    if (
        args.resume
        and all(_step_done(log, step) for step in _STEP_KEYS[:6])
        and not [n for n in _WIZARD_OUTPUT_FILES if not (output_dir / n).exists()]
    ):
        console.print(
            "  Resuming: steps 1-6 are recorded as completed in "
            f"{log_path.name} and every file exists. Running validation only.",
            markup=False,
        )
        step7_validate(output_dir, log)
        save()
        return 0
    if args.resume and log:
        done = [step for step in _STEP_KEYS if _step_done(log, step)]
        console.print(f"  Resuming: recorded as completed: {', '.join(done) or 'nothing'}.", markup=False)

    # ---- Determine language ----
    language = (
        args.language
        or os.getenv("WIZARD_LANGUAGE", "")
        or _choose(
            "Primary language for user questions?",
            choices=["en", "fa", "both"],
            default="en",
            non_interactive=args.non_interactive,
        )
    )

    # What an earlier run stored, taken before the steps below overwrite it.
    stored_aliases, stored_rules, stored_examples = (
        _restorable_results(log, language) if args.resume else (None, None, None)
    )
    stored_fingerprint = log.get("step2_schema", {}).get("schema_fingerprint") if args.resume else None
    everything_stored = None not in (stored_aliases, stored_rules, stored_examples)

    # ---- LLM: resolved and vetted before any step touches the database ----
    llm = None
    if not everything_stored:
        try:
            llm = setup_wizard_llm(args)
        except (ValueError, RemoteLLMNotAllowedError) as exc:
            console.print(f"ERROR: {exc}", markup=False)
            return 2

    # ---- Step 1: Connection ----
    db_url = step1_connection(args, log)
    save()

    # ---- Step 2: Schema ----
    snapshot = step2_schema(args, db_url, log)
    save()

    reuse = args.resume and stored_fingerprint == _schema_fingerprint(snapshot)
    if args.resume and not reuse and any(x is not None for x in (stored_aliases, stored_rules, stored_examples)):
        console.print(
            "  The schema differs from the one the earlier run generated for: "
            "aliases, rules and examples are generated again.",
            markup=False,
        )
    if not reuse:
        stored_aliases = stored_rules = stored_examples = None

    def need_llm():
        """The LLM, set up on first use when the early check was skipped."""
        nonlocal llm
        if llm is None:
            llm = setup_wizard_llm(args)
        return llm

    try:
        # ---- Step 3: Aliases ----
        if stored_aliases is not None:
            console.print("  Step 3: reusing the aliases generated by the earlier run.", markup=False)
            alias_result = stored_aliases
        else:
            alias_result = step3_aliases(args, snapshot, need_llm(), log, language)
        entities_data = alias_result["entities"]
        ring_aliases  = alias_result["ring_aliases"]
        save()

        # ---- Step 4: Business rules ----
        if stored_rules is not None:
            console.print("  Step 4: reusing the business rules generated by the earlier run.", markup=False)
            rules = stored_rules
        else:
            rules = step4_business_rules(args, snapshot, need_llm(), log)
        save()

        # ---- Step 5: Examples ----
        if stored_examples is not None:
            console.print("  Step 5: reusing the examples generated by the earlier run.", markup=False)
            examples = stored_examples
        else:
            examples = step5_examples(args, snapshot, need_llm(), log, language)
        save()
    except (ValueError, RemoteLLMNotAllowedError) as exc:
        console.print(f"ERROR: {exc}", markup=False)
        return 2

    # ---- Step 6: Review & write ----
    source_url = _redact_db_url(db_url)
    step6_review_and_write(
        args=args,
        output_dir=output_dir,
        generated_at=generated_at,
        source_url=source_url,
        entities_data=entities_data,
        ring_aliases=ring_aliases,
        rules=rules,
        examples=examples,
        snapshot=snapshot,
        log=log,
        llm=llm,
        language=language,
    )
    save()

    # ---- Step 7: Validate ----
    if not args.dry_run:
        step7_validate(output_dir, log)
        save()

    return 0


if __name__ == "__main__":
    sys.exit(main())
