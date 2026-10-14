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
        --output       project_config_draft/ \\
        --review       interactive

Flags
-----
--output DIR        Where the draft files are written. Default:
                    ``project_config_draft/`` (ignored by version control).
                    The wizard refuses to write into the live configuration
                    directory (``PROJECT_CONFIG_DIR``, by default
                    ``project_config/``) or ``project_config.example/`` unless
                    ``--overwrite-live`` is given; review the drafts and copy
                    the files you accept into ``project_config/`` yourself.
--overwrite-live    Allow ``--output`` to be a protected directory. Every file
                    the wizard is about to replace is first copied to
                    ``<name>.bak-<YYYYmmdd-HHMMSS>`` next to it, and the
                    copies are listed. Files the wizard does not produce are
                    never touched.
--fresh             Start from scratch: ignore the files already in the
                    output directory instead of merging into them.
--include-schemas   Comma-separated schemas to read (default: all).
--exclude-schemas   Comma-separated ``fnmatch`` patterns (case-insensitive) of
                    schemas to leave out; wins over ``--include-schemas``.
--include-staging   Keep the staging and change-capture tables that
                    ``STAGING_PATTERNS`` names; by default they are skipped.
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

--allow-repeated-answers
                    Do not stop a per-table step (3 aliases, 4 business rules)
                    when 3 tables in a row get the identical answer. By
                    default the wizard stops there, writes no file and leaves
                    the run resumable, because identical answers for different
                    tables mean a response cache in front of the model is
                    returning one stored reply (see "Repeated answers" below).
                    For a genuinely tiny schema whose tables really are alike.

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

Repeated answers
----------------
Steps 3 and 4 ask the model one question per table. A gateway in front of the
model server that keeps a *response cache* (semantic or prefix) can answer all
of them with the reply to the first, and nothing in the reply says so. The
wizard compares the model's parsed answers as they arrive (the alias list in
step 3; the rule text, with the table names taken out, in step 4). When 3
consecutive tables get the identical non-empty answer it stops that step, says
what it suspects and how to confirm it
(``python scripts/verify_deployment.py``, the check ``Model endpoint answers
each prompt itself``), writes no file and exits with status 3; steps already
completed stay recorded, so ``--resume`` continues after the endpoint is
fixed. ``--allow-repeated-answers`` turns the stop into a warning. Each of
these steps ends with a line giving the number of distinct answers against the
number of tables. Step 5 asks one question for the whole schema, so there is
nothing to compare.

Merging
-------
Run once per database, the wizard merges into the files already in the output
directory instead of replacing them. Entries are keyed like the schema
inspector's (``Table`` when the name is unique, ``Schema.Table`` when several
schemas share it); an entry for a table of this run replaces the old one with
the same key (unless the model gave nothing for it and the old entry has
aliases or a rule: that one is kept), every other old entry stays, examples are
de-duplicated by question and relationships by their endpoints. An old entry whose bare name
must now be qualified is re-keyed and reported. The summary says how many
entries were kept, added and replaced per file. ``--fresh`` skips the merge.

Tables
------
The wizard first lists schema and table names (nothing else is read), applies
``--include-schemas``, ``--exclude-schemas`` and, in interactive mode, the
schemas the operator picks after seeing them with their table counts, and only
then inspects what remains: a schema that is left out is never scanned for
columns or sample values. Staging and change-capture tables
(``STAGING_PATTERNS``) are left out too (a staging table whose name no other
table shares is not read; one whose name is shared is read and dropped), so
none of them reaches the model; the summary says how many were skipped and why. When the database declares few
foreign keys, relationships are inferred from column names
(``schema_data.relationship_proposals``) and marked ``REVIEW`` in
``relationships.yaml``; the business-rules prompt sees them as join hints.

The wizard writes ``.setup_log.json`` into the output directory recording
every step that was executed and when (and the aliases, rules and examples the
model produced), so it can be resumed safely. The connection string is stored
only with its password masked.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field, replace
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

from core.console import use_utf8_console
from core.wizard_drafts import (
    DEFAULT_DRAFT_DIR,
    MergeStats,
    back_up,
    hold_back_empty,
    merge_examples,
    merge_mapping,
    merge_relationships,
    merge_synonyms,
    protected_directory,
    refusal_message,
)
from core.yaml_loading import safe_load_strict

# Credential redaction lives in core/redaction.py (shared with the schema
# inspector and the application-database messages). The wizard keeps its
# historical underscore names for the two helpers it and its tests use.
from core.redaction import redact_db_url as _redact_db_url
from core.redaction import scrub_secrets as _scrub_secrets

console = Console()
logger  = logging.getLogger(__name__)

_DEFAULT_OUTPUT = Path(DEFAULT_DRAFT_DIR)
_SETUP_LOG      = _DEFAULT_OUTPUT / ".setup_log.json"

# DB type help text shown on connection failure
_DB_EXAMPLES = """
  MSSQL  : mssql+pyodbc://user:pass@server/db?driver=ODBC+Driver+17+for+SQL+Server
  PostgreSQL: postgresql+psycopg2://user:pass@host:5432/db
  MySQL  : mysql+pymysql://user:pass@host:3306/db
  SQLite : sqlite:///path/to/file.db
"""


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


def _schema_fingerprint(snapshot, namer=None) -> str:
    """Return a digest of the tables and columns in *snapshot*.

    Stored with step 2 so that a resumed run can tell whether the aliases,
    rules and examples generated earlier still describe the schema it just
    read. The snapshot is the one left after schemas and staging tables were
    skipped, so a different exclusion gives a different digest.

    Args:
        snapshot: A schema snapshot with ``tables`` (each with ``full_name``
            and ``columns``).
        namer: When given, each table's key is part of the digest, so a run
            whose keys differ (a name that became shared) does not reuse
            results keyed the old way.

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
        >>> class Keys:
        ...     key = staticmethod(lambda table: "A")
        >>> _schema_fingerprint(NS(tables=[t])) == _schema_fingerprint(NS(tables=[t]), Keys())
        False
    """
    import hashlib

    lines = sorted(
        f"{t.full_name}{'@' + namer.key(t) if namer is not None else ''}"
        f":{','.join(sorted(c.name for c in t.columns))}"
        for t in snapshot.tables
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
        if info.get("label"):
            entry["label"] = info["label"]
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
    """Serialise relationship entries; an entry with a ``basis`` is marked for review.

    Args:
        relationships: Entries with ``from_table``, ``from_column``,
            ``to_table``, ``to_column`` and ``join_hint``. One inferred from
            column names also has ``basis`` (and ``confidence``) and gets a
            ``# REVIEW`` comment line above it.
        generated_at: Timestamp for the header.

    Returns:
        The file content.

    Raises:
        Nothing for well-formed entries.

    Examples:
        >>> text = _relationships_yaml([
        ...     {"from_table": "A", "from_column": "b_id", "to_table": "B", "to_column": "id",
        ...      "join_hint": "JOIN B ON A.b_id = B.id", "basis": "naming convention: b_id -> B.id"}], "t")
        >>> "# REVIEW" in text and "- from_table: A" in text
        True
        >>> _relationships_yaml([], "t").endswith("relationships: []\\n")
        True
    """
    inferred = sum(1 for r in relationships if r.get("basis"))
    header = (
        "# AUTO-GENERATED by setup_project.py — review before use\n"
        f"# Generated: {generated_at or datetime.utcnow().isoformat(timespec='seconds')}\n"
    )
    if inferred:
        header += (
            f"# {inferred} entr{'y is' if inferred == 1 else 'ies are'} marked REVIEW: guessed from column "
            "names (X_ID -> X.ID), not declared foreign keys. Check each, delete the wrong ones.\n"
        )
    header += "\n"
    if not relationships:
        return header + "relationships: []\n"
    lines = ["relationships:"]
    for entry in relationships:
        if entry.get("basis"):
            lines.append(f"  # REVIEW: inferred from column names ({entry['basis']}); not a declared foreign key")
        block = yaml.dump([entry], allow_unicode=True, sort_keys=False, width=10_000)
        lines.extend(f"  {line}" for line in block.splitlines())
    return header + "\n".join(lines) + "\n"


# ===========================================================================
# Validation against Pydantic models
# ===========================================================================

_REPO_ROOT = Path(__file__).resolve().parent


def _config_loader():
    """Return the ``knowledge.config_loader`` module, importable on a fresh checkout.

    ``knowledge/__init__.py`` re-exports its names lazily, so importing the
    loaders reads no file of ``project_config/``: on a fresh checkout, or a
    ``project_config/`` the wizard has only half filled, the import succeeds
    and the wizard can validate and name what is missing. (It used to read
    five files at import time, and the import was made once with the loaders
    pointed at ``project_config.example/`` to survive that.)

    Returns:
        The imported ``knowledge.config_loader`` module.

    Raises:
        ImportError: When the module cannot be imported at all, e.g. the
            wizard is run outside a full checkout.
    """
    try:
        import knowledge.config_loader as module
    except Exception as exc:  # noqa: BLE001 - ImportError, a broken install, ...
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
# Repeated answers: a response cache in front of the model
# ===========================================================================

#: Consecutive tables with an identical non-empty answer that stop a step.
_REPEATED_ANSWER_LIMIT = 3


class CachedAnswersError(RuntimeError):
    """Three tables in a row got the identical answer: a response cache is suspected."""


def _normalize_rule_text(text: str, table_names: list[str]) -> str:
    """*text* with the given table names replaced by ``<table>`` and spaces collapsed.

    Two rules that differ only in the name of the table they are about are the
    same answer.

    Args:
        text: The rule text the model returned.
        table_names: Names to take out (the fact table and its dimension tables).

    Returns:
        The normalised text; ``""`` when *text* has no content.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> _normalize_rule_text("Use  Sales for totals; join Items.", ["Sales", "Items"])
        'Use <table> for totals; join <table>.'
        >>> _normalize_rule_text("   ", ["Sales"])
        ''
    """
    import re

    out = " ".join((text or "").split())
    for name in sorted({n for n in table_names if n}, key=len, reverse=True):
        out = re.sub(re.escape(name), "<table>", out, flags=re.IGNORECASE)
    return out


class _AnswerGuard:
    """Watches the model's answers, table by table, for one repeated verbatim.

    Args:
        step: Label used in messages, such as ``"Step 3"``.
        what: What an answer is, such as ``"alias list"``.
        allow: When true the stop becomes a one-time warning.
        limit: Consecutive identical non-empty answers that count as a repeat.

    Examples:
        >>> g = _AnswerGuard("Step 3", "alias list")
        >>> g.observe("A", "x"); g.observe("B", "x")
        >>> g.observe("C", "x")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        CachedAnswersError: Step 3 stopped: ...
        >>> g = _AnswerGuard("Step 3", "alias list")
        >>> for t, a in [("A", "x"), ("B", "y"), ("C", "x"), ("D", None)]:
        ...     g.observe(t, a)
        >>> g.summary()
        '2 distinct alias lists for 4 tables (1 without an answer)'
    """

    def __init__(self, step: str, what: str, *, allow: bool = False,
                 limit: int = _REPEATED_ANSWER_LIMIT) -> None:
        self.step = step
        self.what = what
        self.allow = allow
        self.limit = limit
        self._tables = 0
        self._answers: set[str] = set()
        self._empty = 0
        self._last: str | None = None
        self._streak: list[str] = []
        self._warned = False
        self.repeated = False

    def observe(self, table: str, answer: str | None) -> None:
        """Record the model's answer for *table*; ``None`` or ``""`` means no answer.

        Raises:
            CachedAnswersError: When this is the *limit*-th table in a row with
                the same non-empty answer and repeats are not allowed.
        """
        self._tables += 1
        if not answer:
            self._empty += 1
            self._last, self._streak = None, []
            return
        self._answers.add(answer)
        if answer == self._last:
            self._streak.append(table)
        else:
            self._last, self._streak = answer, [table]
        if len(self._streak) < self.limit:
            return
        self.repeated = True
        if self.allow:
            if not self._warned:
                self._warned = True
                console.print(
                    f"  Warning: {self.limit} tables in a row got the identical {self.what} "
                    f"({', '.join(self._streak)}); continuing because of --allow-repeated-answers.",
                    markup=False,
                )
            return
        raise CachedAnswersError(self._stop_message())

    def _stop_message(self) -> str:
        shown = ", ".join(self._streak[-self.limit:])
        return (
            f"{self.step} stopped: the model endpoint returned the identical {self.what} for "
            f"{self.limit} different tables in a row ({shown}). That is not an answer per table: "
            "a response cache in front of the model (a gateway with semantic or prefix caching) "
            "is answering similar prompts with one stored reply. No file was written and the run "
            "can be resumed.\n"
            "  Check it: python scripts/verify_deployment.py "
            "(the check 'Model endpoint answers each prompt itself').\n"
            "  Fix it: disable response caching for this client at the gateway, or point "
            "OPENAI_BASE_URL (or WIZARD_LLM_BASE_URL) at the model server directly; then run "
            "again with --resume.\n"
            "  A genuinely tiny schema whose tables really are alike: --allow-repeated-answers."
        )

    def summary(self) -> str:
        """``"N distinct answers for M tables"``, plus how many got no answer."""
        distinct = len(self._answers)
        text = (
            f"{distinct} distinct {self.what}{'' if distinct == 1 else 's'} "
            f"for {self._tables} table{'' if self._tables == 1 else 's'}"
        )
        if self._empty:
            text += f" ({self._empty} without an answer)"
        return text


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
# Table keys, skipped tables, relationships, files of an earlier run
# ===========================================================================

#: Relationships are inferred from column names when the database declares
#: fewer foreign keys than one per this many tables (and always when it
#: declares none).
_FEW_FK_TABLES_PER_FK = 10


def _identity(schema: str | None, name: str) -> tuple[str, str]:
    """``(schema, table)`` lower-cased: what makes two tables the same table.

    Args:
        schema: The schema, or ``None``/``""`` when unknown.
        name: The bare table name.

    Returns:
        The pair used to compare tables across runs and snapshots.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> _identity("Sales", "Trade")
        ('sales', 'trade')
        >>> _identity(None, "Trade")
        ('', 'trade')
    """
    return ((schema or "").lower(), name.lower())


def _table_namer(snapshot, existing: Mapping[str, Mapping[str, Any]] | None = None):
    """Return the inspector's ``_TableNamer`` for *snapshot* and an earlier run's entities.

    A table is keyed by its bare name when no other table shares it and by
    ``Schema.Table`` when several schemas do (``docs/design/TABLE-NAMES.md``).
    The tables of *existing* (the ``entities.yaml`` of an earlier run, in the
    wizard's internal form) take part, so a name that this run sees once but
    the earlier run saw in another schema is qualified for both.

    Args:
        snapshot: A schema snapshot with ``tables``.
        existing: Entities of an earlier run, ``{key: {"table", "schema", ...}}``.

    Returns:
        A ``database.schema_inspector._TableNamer``.

    Raises:
        Nothing for a well-formed snapshot.

    Examples:
        >>> from types import SimpleNamespace as NS
        >>> snap = NS(tables=[NS(name="Trade", schema="sales"), NS(name="Customer", schema="sales")])
        >>> namer = _table_namer(snap)
        >>> [namer.key(t) for t in snap.tables]
        ['Trade', 'Customer']
        >>> namer = _table_namer(snap, {"Trade": {"table": "Trade", "schema": "ref"}})
        >>> [namer.key(t) for t in snap.tables]
        ['sales.Trade', 'Customer']
    """
    from database.schema_inspector import SchemaSnapshot, TableInfo, _TableNamer

    tables = list(snapshot.tables)
    known = {_identity(t.schema, t.name) for t in tables}
    for entry in (existing or {}).values():
        name = entry.get("table")
        schema = entry.get("schema") or None
        if name and _identity(schema, str(name)) not in known:
            known.add(_identity(schema, str(name)))
            tables.append(TableInfo(name=str(name), schema=schema))
    return _TableNamer(SchemaSnapshot(tables=tables))


def _rekey_map(namer, existing: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """``{old key: new key}`` for earlier entities whose default key changed.

    Only a key that is the table's default spelling (the bare name, or
    ``Schema.Table``) is renamed; an entity name an operator chose is left
    alone.

    Args:
        namer: The namer from :func:`_table_namer`.
        existing: Entities of an earlier run, ``{key: {"table", "schema", ...}}``.

    Returns:
        The renames, empty when every key is still right.

    Raises:
        Nothing for well-formed entities.

    Examples:
        >>> from types import SimpleNamespace as NS
        >>> snap = NS(tables=[NS(name="Trade", schema="sales")])
        >>> old = {"Trade": {"table": "Trade", "schema": "sales"}, "Mine": {"table": "Trade", "schema": "sales"}}
        >>> _rekey_map(_table_namer(snap, {**old, "x": {"table": "Trade", "schema": "ref"}}), old)
        {'Trade': 'sales.Trade'}
    """
    from database.schema_inspector import TableInfo, _render_key

    renames: dict[str, str] = {}
    for key, entry in existing.items():
        name = entry.get("table")
        if not name:
            continue
        schema = entry.get("schema") or None
        defaults = {_render_key((str(name),)).casefold()}
        if schema:
            defaults.add(_render_key((str(schema), str(name))).casefold())
        new = namer.key(TableInfo(name=str(name), schema=schema))
        if new.casefold() != key.casefold() and key.casefold() in defaults:
            renames[key] = new
    return renames


def _split_csv(value: str | None) -> list[str]:
    """Split a comma-separated option into trimmed, non-empty items.

    Examples:
        >>> _split_csv(" a, b,,c ")
        ['a', 'b', 'c']
        >>> _split_csv(None)
        []
    """
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _is_staging(table, patterns) -> bool:
    """Whether *table* matches a ``STAGING_PATTERNS`` pattern.

    Same rule as ``core.config_doctor.staging_tables``: a bare pattern is
    matched against the last part of the schema name, a ``table:`` pattern
    against the table name, both case-insensitively.

    Args:
        table: Anything with ``schema`` and ``name``.
        patterns: The result of ``core.config_doctor.parse_staging_patterns``.

    Returns:
        True for a staging or change-capture table.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> from types import SimpleNamespace as NS
        >>> from core.config_doctor import parse_staging_patterns
        >>> pats = parse_staging_patterns("*_Stg,table:*_Inserts")
        >>> _is_staging(NS(schema="Sales_Stg", name="Trade"), pats)
        True
        >>> _is_staging(NS(schema="sales", name="Trade_Inserts"), pats)
        True
        >>> _is_staging(NS(schema="sales", name="Trade"), pats)
        False
    """
    schema = (table.schema or "").rsplit(".", 1)[-1].lower()
    name = table.name.lower()
    return any(
        fnmatch.fnmatchcase(name if p.on_table else schema, p.pattern) for p in patterns
    )


@dataclass
class SkipReport:
    """Tables the wizard left out before any model call, and why.

    Attributes:
        found: Tables the inspection returned.
        excluded_schemas: Tables in schemas named by ``--exclude-schemas``.
        staging: Staging and change-capture tables (``STAGING_PATTERNS``).
        chosen: Tables in schemas or tables the operator left out interactively.
    """

    found: int = 0
    excluded_schemas: int = 0
    staging: int = 0
    chosen: int = 0

    @property
    def total(self) -> int:
        """All skipped tables."""
        return self.excluded_schemas + self.staging + self.chosen

    def describe(self) -> str:
        """One sentence: how many tables were skipped and for which reason.

        Examples:
            >>> SkipReport(278, 40, 26, 0).describe()
            'Skipped 66 of 278 tables: 40 in excluded schemas, 26 staging or change-capture (STAGING_PATTERNS; --include-staging keeps them).'
            >>> SkipReport(5).describe()
            'Skipped none of 5 tables.'
        """
        if not self.total:
            return f"Skipped none of {self.found} tables."
        parts = []
        if self.excluded_schemas:
            parts.append(f"{self.excluded_schemas} in excluded schemas")
        if self.chosen:
            parts.append(f"{self.chosen} left out interactively")
        if self.staging:
            parts.append(
                f"{self.staging} staging or change-capture "
                "(STAGING_PATTERNS; --include-staging keeps them)"
            )
        return f"Skipped {self.total} of {self.found} tables: {', '.join(parts)}."


def _drop_tables(snapshot, drop: set[tuple[str, str]]) -> None:
    """Remove the tables whose identity is in *drop*, and every relationship touching them.

    Args:
        snapshot: The schema snapshot, changed in place.
        drop: ``_identity`` pairs to remove.

    Returns:
        None.

    Raises:
        Nothing for a well-formed snapshot.

    Examples:
        >>> from types import SimpleNamespace as NS
        >>> t = [NS(name="A", schema="s"), NS(name="B", schema="s")]
        >>> r = [NS(from_table="A", from_schema="s", to_table="B", to_schema=None)]
        >>> snap = NS(tables=t, relationships=r)
        >>> _drop_tables(snap, {("s", "b")})
        >>> [x.name for x in snap.tables], snap.relationships
        (['A'], [])
    """
    if not drop:
        return
    snapshot.tables = [t for t in snapshot.tables if _identity(t.schema, t.name) not in drop]
    kept = []
    for rel in snapshot.relationships:
        from_schema = getattr(rel, "from_schema", None)
        to_schema = getattr(rel, "to_schema", None) or from_schema
        if (
            _identity(from_schema, rel.from_table) in drop
            or _identity(to_schema, rel.to_table) in drop
        ):
            continue
        kept.append(rel)
    snapshot.relationships = kept


def _schema_counts(snapshot) -> dict[str, int]:
    """``{schema: number of tables}`` in schema-name order; no schema is ``"(none)"``.

    Examples:
        >>> from types import SimpleNamespace as NS
        >>> _schema_counts(NS(tables=[NS(schema="b"), NS(schema="a"), NS(schema="b"), NS(schema="")]))
        {'(none)': 1, 'a': 1, 'b': 2}
    """
    counts: dict[str, int] = {}
    for table in snapshot.tables:
        name = table.schema or "(none)"
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0].lower()))


def _skip_tables(
    snapshot,
    exclude_patterns: list[str],
    staging_patterns,
    report: SkipReport,
    *,
    chosen: bool = False,
) -> None:
    """Drop the tables in schemas matching *exclude_patterns* or matching a staging pattern.

    Args:
        snapshot: The schema snapshot, changed in place.
        exclude_patterns: ``fnmatch`` patterns for schema names (case-insensitive).
        staging_patterns: Parsed ``STAGING_PATTERNS``; empty to keep staging tables.
        report: Counts are added to it (``chosen`` when *chosen* is true).
        chosen: Whether the patterns come from the operator's interactive answer.

    Returns:
        None.

    Raises:
        Nothing for a well-formed snapshot.

    Examples:
        >>> from types import SimpleNamespace as NS
        >>> from core.config_doctor import parse_staging_patterns
        >>> snap = NS(tables=[NS(name="T", schema="sales"), NS(name="T", schema="ref"),
        ...                   NS(name="T", schema="sales_Stg")], relationships=[])
        >>> report = SkipReport(found=3)
        >>> _skip_tables(snap, ["ref"], parse_staging_patterns("*_Stg"), report)
        >>> [t.schema for t in snap.tables], report.excluded_schemas, report.staging
        (['sales'], 1, 1)
    """
    from database.schema_inspector import schema_is_excluded

    excluded: set[tuple[str, str]] = set()
    staging: set[tuple[str, str]] = set()
    for table in snapshot.tables:
        if exclude_patterns and schema_is_excluded(table.schema or "", exclude_patterns):
            excluded.add(_identity(table.schema, table.name))
        elif staging_patterns and _is_staging(table, staging_patterns):
            staging.add(_identity(table.schema, table.name))
    if chosen:
        report.chosen += len(excluded)
    else:
        report.excluded_schemas += len(excluded)
    report.staging += len(staging)
    _drop_tables(snapshot, excluded | staging)


# ---- what to read, decided from names alone ---------------------------------

#: Schemas that are never described (the schema inspector skips the same ones).
_SYSTEM_SCHEMAS = ("information_schema", "sys", "pg_catalog", "pg_toast")


def _list_schema_tables(db_url: str) -> dict[str, list[str]]:
    """``{schema: [table names]}`` from the catalogue: names only, no column, no row.

    Uses SQLAlchemy's inspector with the schema inspector's own system-schema
    filter. This is the first look at the database; what to read is decided
    from it (see :func:`_plan_reading`).

    Args:
        db_url: The SQLAlchemy connection string.

    Returns:
        Schemas in the database's order, each with its table names.

    Raises:
        Exception: Whatever SQLAlchemy raises when the database cannot be
            listed; the caller reports it with the credentials scrubbed.
    """
    from sqlalchemy import create_engine, inspect as sa_inspect

    engine = create_engine(db_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
    try:
        inspector = sa_inspect(engine)
        listing: dict[str, list[str]] = {}
        for schema in inspector.get_schema_names():
            if schema in _SYSTEM_SCHEMAS or schema.startswith("pg_"):
                continue
            listing[schema] = list(inspector.get_table_names(schema=schema))
        return listing
    finally:
        engine.dispose()


@dataclass
class _ReadPlan:
    """What the inspection will read, and what was left out before it.

    Attributes:
        schemas: Schemas to inspect (their real spelling).
        exclude_tables: Bare table names the inspection is told to skip: staging
            tables whose name no other listed table shares.
        report: The skip counts so far.
        per_schema: ``{schema: tables to be read}`` for the interactive question.
    """

    schemas: list[str] = field(default_factory=list)
    exclude_tables: list[str] = field(default_factory=list)
    report: SkipReport = field(default_factory=SkipReport)
    per_schema: dict[str, int] = field(default_factory=dict)


def _plan_reading(
    listing: Mapping[str, list[str]],
    include_schemas: list[str] | None,
    exclude_patterns: list[str],
    staging_patterns,
    chosen_patterns: list[str] | None = None,
) -> _ReadPlan:
    """Decide from table names alone which schemas and tables the inspection reads.

    ``--include-schemas`` narrows the listing, then ``--exclude-schemas`` (and the
    schemas the operator chose) remove schemas, so a schema matching both is not
    read. A staging table is left out too: a schema whose every table is staging
    (a bare ``STAGING_PATTERNS`` pattern matches the schema name) is not read at
    all; a ``table:`` pattern match is passed as an excluded name when no other
    listed table shares its bare name. One whose name is shared is read and
    dropped afterwards (:func:`_skip_tables`), so a same-named table of another
    schema is not lost; it is not counted here.

    Args:
        listing: ``{schema: [table names]}`` from :func:`_list_schema_tables`.
        include_schemas: Schemas to read (case-insensitive), or ``None`` for all.
        exclude_patterns: ``--exclude-schemas`` ``fnmatch`` patterns.
        staging_patterns: Parsed ``STAGING_PATTERNS``; empty keeps staging tables.
        chosen_patterns: Schema patterns the operator chose interactively.

    Returns:
        The plan. ``schemas`` is empty when nothing is left to read, in which
        case the inspection must not run (an empty include list means "all").

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> from core.config_doctor import parse_staging_patterns
        >>> listing = {"sales": ["Trade", "Trade_Inserts"], "sales_Stg": ["Trade"], "ref": ["Region"]}
        >>> plan = _plan_reading(listing, None, ["ref"], parse_staging_patterns("*_Stg,table:*_Inserts"))
        >>> plan.schemas, plan.exclude_tables
        (['sales'], ['Trade_Inserts'])
        >>> plan.report.describe()
        'Skipped 3 of 4 tables: 1 in excluded schemas, 2 staging or change-capture (STAGING_PATTERNS; --include-staging keeps them).'
        >>> _plan_reading(listing, ["REF"], [], []).schemas
        ['ref']
        >>> _plan_reading(listing, ["ref"], ["r*"], []).schemas
        []
    """
    from database.schema_inspector import schema_is_excluded

    wanted = {i.casefold() for i in include_schemas} if include_schemas else None
    selected = {
        schema: list(names) for schema, names in listing.items()
        if wanted is None or schema.casefold() in wanted
    }
    plan = _ReadPlan(report=SkipReport(found=sum(len(n) for n in selected.values())))

    # Bare names across everything that could be read, to tell a unique name from a shared one.
    remaining: dict[str, list[str]] = {}
    for schema, names in selected.items():
        if exclude_patterns and schema_is_excluded(schema, exclude_patterns):
            plan.report.excluded_schemas += len(names)
        elif chosen_patterns and schema_is_excluded(schema, chosen_patterns):
            plan.report.chosen += len(names)
        else:
            remaining[schema] = names
    name_count: dict[str, int] = {}
    for names in remaining.values():
        for name in names:
            name_count[name.casefold()] = name_count.get(name.casefold(), 0) + 1

    for schema, names in remaining.items():
        staged = [
            name for name in names
            if staging_patterns and _is_staging(_Listed(schema, name), staging_patterns)
        ]
        if names and len(staged) == len(names):
            plan.report.staging += len(names)      # the whole schema is staging: not read
            continue
        read = len(names)
        for name in staged:
            if name_count[name.casefold()] == 1:
                plan.exclude_tables.append(name)
                plan.report.staging += 1
                read -= 1
        plan.per_schema[schema] = read
        plan.schemas.append(schema)
    return plan


class _Listed:
    """A table known by name only: what :func:`_is_staging` needs."""

    def __init__(self, schema: str, name: str) -> None:
        self.schema = schema
        self.name = name


# ---- relationships ---------------------------------------------------------

@dataclass(frozen=True)
class _Link:
    """One relationship between two tables, keyed like the entities.

    Attributes:
        from_key, to_key: The tables' keys (``Table`` or ``Schema.Table``).
        from_column, to_column: The joined columns.
        join_hint: The ``JOIN ... ON ...`` clause.
        basis: Empty for a declared foreign key; for an inferred one, why it
            was guessed (``naming convention: ...``).
        confidence: ``high`` or ``medium`` for an inferred one, else empty.
    """

    from_key: str
    from_column: str
    to_key: str
    to_column: str
    join_hint: str
    basis: str = ""
    confidence: str = ""

    @property
    def inferred(self) -> bool:
        """Whether this was guessed from column names rather than declared."""
        return bool(self.basis)

    def as_entry(self) -> dict[str, str]:
        """The ``relationships.yaml`` entry; ``basis`` and ``confidence`` only for an inferred one."""
        entry = {
            "from_table": self.from_key,
            "from_column": self.from_column,
            "to_table": self.to_key,
            "to_column": self.to_column,
            "join_hint": self.join_hint,
        }
        if self.inferred:
            entry["basis"] = self.basis
            entry["confidence"] = self.confidence
        return entry

    def endpoints(self) -> frozenset[tuple[str, str]]:
        """The two ``(table key, column)`` ends, lower-cased, direction ignored."""
        return frozenset({
            (self.from_key.casefold(), self.from_column.casefold()),
            (self.to_key.casefold(), self.to_column.casefold()),
        })


def _declared_links(snapshot, namer) -> list[_Link]:
    """The snapshot's foreign keys as links, with keyed table names.

    Args:
        snapshot: A schema snapshot with ``relationships``.
        namer: The key rule from :func:`_table_namer`.

    Returns:
        One link per relationship, in snapshot order.

    Raises:
        Nothing for a well-formed snapshot.
    """
    links = []
    for rel in snapshot.relationships:
        from_schema = getattr(rel, "from_schema", None)
        links.append(_Link(
            from_key=namer.ref(from_schema, rel.from_table),
            from_column=rel.from_column,
            to_key=namer.ref(getattr(rel, "to_schema", None), rel.to_table, default_schema=from_schema),
            to_column=rel.to_column,
            join_hint=rel.join_hint,
        ))
    return links


def _inferred_links(snapshot, namer, declared: list[_Link]) -> tuple[list[_Link], str]:
    """Relationships guessed from column names, for tables the foreign keys do not explain.

    Builds the ``schema.yaml`` model and catalogue that
    ``schema_data.relationship_proposals.propose_relationships`` reads from
    the snapshot, and keeps what it infers (``<Name>_ID`` or ``<Name>ID``
    pointing at the table ``Name``'s key). Declared foreign keys are given to
    it too, so a column a constraint already explains is not guessed again.

    Args:
        snapshot: A schema snapshot with ``tables``.
        namer: The key rule from :func:`_table_namer`.
        declared: The links from :func:`_declared_links`; a guess with the
            same ends is dropped.

    Returns:
        ``(links, problem)``: the inferred links, and a sentence saying why
        none could be inferred (``""`` when nothing went wrong).

    Raises:
        Nothing. A snapshot the proposal code cannot model yields no links
        and a *problem*.
    """
    try:
        from database.catalogue import ColumnInfo, ForeignKey, TableInfo, table_location
        from schema_data.registry import SchemaConfig, TableDefinition
        from schema_data.relationship_proposals import propose_relationships

        located = {}
        definitions = {}
        for table in snapshot.tables:
            key = namer.key(table)
            schema = table.schema or ""
            db_schema = schema if schema and "." not in schema else ""
            columns = {c.name: "" for c in table.columns}
            definitions[key] = TableDefinition(columns=columns or None, db_schema=db_schema)
            located[_identity(table.schema, table.name)] = table_location(key, db_schema, "dbo")
        catalogue = {}
        for table in snapshot.tables:
            key = namer.key(table)
            own = located[_identity(table.schema, table.name)]
            foreign_keys = []
            for fk in getattr(table, "foreign_keys", []):
                ref = located.get(
                    _identity(getattr(fk, "referred_schema", None) or table.schema, fk.referred_table),
                    ((getattr(fk, "referred_schema", None) or table.schema or "dbo").lower(),
                     fk.referred_table.lower()),
                )
                foreign_keys.append(ForeignKey("", (fk.column,), ref, (fk.referred_column,)))
            catalogue[own] = TableInfo(
                schema=table.schema or "",
                name=table.name,
                is_view=False,
                columns=tuple(ColumnInfo(c.name, c.type, True) for c in table.columns),
                primary_key=tuple(getattr(table, "pk_columns", ())),
                foreign_keys=tuple(foreign_keys),
            )
        schema_config = SchemaConfig(tables=definitions)
        result = propose_relationships(schema_config, ["wizard"], {"wizard": catalogue})
    except Exception as exc:  # noqa: BLE001 - a snapshot the proposal code cannot model
        return [], f"Relationships were not inferred from column names ({type(exc).__name__}: {exc})."

    taken = {link.endpoints() for link in declared}
    links: list[_Link] = []
    for proposal in result.proposals:
        if proposal.confidence == "declared":
            continue
        link = _Link(
            from_key=namer.ref(proposal.from_schema or None, proposal.from_table),
            from_column=proposal.from_column,
            to_key=namer.ref(proposal.to_schema or None, proposal.to_table),
            to_column=proposal.to_column,
            join_hint=proposal.join_hint,
            basis=proposal.basis,
            confidence=proposal.confidence,
        )
        if link.endpoints() not in taken:
            taken.add(link.endpoints())
            links.append(link)
    return links, ""


def _collect_links(snapshot, namer) -> tuple[list[_Link], str]:
    """The declared foreign keys, plus inferred relationships when there are few of them.

    Args:
        snapshot: A schema snapshot with ``tables`` and ``relationships``.
        namer: The key rule from :func:`_table_namer`.

    Returns:
        ``(links, problem)``: declared links first, then inferred ones (only
        when the database declares fewer than one foreign key per
        ``_FEW_FK_TABLES_PER_FK`` tables); *problem* is a sentence when
        inference was wanted but failed, else ``""``.

    Raises:
        Nothing for a well-formed snapshot.

    Examples:
        >>> from types import SimpleNamespace as NS
        >>> snap = NS(tables=[], relationships=[])
        >>> _collect_links(snap, _table_namer(snap))
        ([], '')
    """
    declared = _declared_links(snapshot, namer)
    tables = len(snapshot.tables)
    if not tables or len(declared) >= max(1, tables // _FEW_FK_TABLES_PER_FK):
        return declared, ""
    inferred, problem = _inferred_links(snapshot, namer, declared)
    return declared + inferred, problem


# ---- files of an earlier run -----------------------------------------------

@dataclass
class _ExistingDraft:
    """What the output directory already holds, in the wizard's internal shapes.

    ``entities`` is ``{key: {"table", "schema", "aliases", "label"}}`` and
    ``rules`` is ``{key: rule text}``; the rest is as in the YAML files.
    """

    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    ring_aliases: dict[str, list[str]] = field(default_factory=dict)
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    rules: dict[str, str] = field(default_factory=dict)
    examples: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)


def _section(doc: Any, name: str, kind: type, filename: str) -> Any:
    """``doc[name]`` checked to be a *kind*; a missing or null section is empty.

    Raises:
        ValueError: When the file is not a mapping or the section has another type.

    Examples:
        >>> _section({"a": None}, "a", dict, "f.yaml")
        {}
        >>> _section({"a": [1]}, "a", list, "f.yaml")
        [1]
    """
    if not isinstance(doc, dict):
        raise ValueError(f"{filename}: the top level is not a mapping")
    value = doc.get(name)
    if value is None:
        return kind()
    if not isinstance(value, kind):
        raise ValueError(f"{filename}: `{name}` is not a {'mapping' if kind is dict else 'list'}")
    return value


def _parse_existing(filename: str, text: str, into: _ExistingDraft) -> None:
    """Parse one wizard file's *text* into *into*.

    Raises:
        ValueError: When the text is not a file of the shape the wizard writes.
        yaml.YAMLError: When it is not YAML (or repeats a key).
    """
    doc = safe_load_strict(text) or {}
    if filename == "entities.yaml":
        for key, entry in _section(doc, "entities", dict, filename).items():
            if not isinstance(entry, dict) or not entry.get("table"):
                raise ValueError(f"{filename}: entity {key!r} has no `table`")
            into.entities[str(key)] = {
                "table": str(entry["table"]),
                "schema": entry.get("schema_name"),
                "aliases": list(entry.get("aliases") or []),
                "label": entry.get("label"),
            }
    elif filename == "aliases.yaml":
        for name, target in (("ring_aliases", into.ring_aliases), ("synonyms", into.synonyms)):
            for key, value in _section(doc, name, dict, filename).items():
                if not isinstance(value, list):
                    raise ValueError(f"{filename}: `{name}` entry {key!r} is not a list")
                target[str(key)] = [str(v) for v in value]
    elif filename == "business_rules.yaml":
        for key, entry in _section(doc, "rules", dict, filename).items():
            text_value = entry.get("rule_text") if isinstance(entry, dict) else entry
            if not isinstance(text_value, str):
                raise ValueError(f"{filename}: rule {key!r} has no `rule_text`")
            into.rules[str(key)] = text_value
    elif filename == "examples.yaml":
        items = _section(doc, "examples", list, filename)
        if not all(isinstance(e, dict) for e in items):
            raise ValueError(f"{filename}: an example is not a mapping")
        into.examples = [dict(e) for e in items]
    elif filename == "relationships.yaml":
        items = _section(doc, "relationships", list, filename)
        if not all(isinstance(e, dict) for e in items):
            raise ValueError(f"{filename}: a relationship is not a mapping")
        into.relationships = [dict(e) for e in items]


def _read_existing(
    output_dir: Path,
    filenames: tuple[str, ...] = (),
) -> tuple[_ExistingDraft, dict[str, str]]:
    """Read the wizard files that *output_dir* already holds.

    Args:
        output_dir: The output directory; it need not exist.
        filenames: Which files to read; those that are absent are skipped.

    Returns:
        ``(existing, problems)``: what could be read, and ``{filename: why}``
        for each file that exists but is not a valid wizard file.

    Raises:
        Nothing. Unreadable files are returned as problems.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     _ = Path(tmp, "entities.yaml").write_text("entities:\\n  A: {table: A, aliases: [x]}\\n")
        ...     _ = Path(tmp, "examples.yaml").write_text("examples: [1]\\n")
        ...     found, problems = _read_existing(Path(tmp), ("entities.yaml", "examples.yaml", "aliases.yaml"))
        >>> list(found.entities), list(problems)
        (['A'], ['examples.yaml'])
    """
    existing = _ExistingDraft()
    problems: dict[str, str] = {}
    for filename in filenames:
        path = output_dir / filename
        if not path.is_file():
            continue
        try:
            _parse_existing(filename, path.read_text(encoding="utf-8"), existing)
        except Exception as exc:  # noqa: BLE001 - YAMLError, ValueError, OSError, ...
            problems[filename] = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return existing, problems


def _live_config_dir() -> Path:
    """The directory the application reads its configuration from.

    ``PROJECT_CONFIG_DIR`` (``Settings.project_config_dir``); a relative value
    is resolved against the repository root, as every loader does.
    """
    import config as cfg

    configured = Path(cfg.settings.project_config_dir)
    return configured if configured.is_absolute() else _REPO_ROOT / configured


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
    """Run schema inspection and return a SchemaSnapshot.

    Two phases. First the table names are listed (nothing else is read), the
    schemas to leave out are applied (``--include-schemas``,
    ``--exclude-schemas``, staging tables, and in interactive mode the
    operator's answer) and only the remaining schemas are inspected, so a
    schema that is left out is never scanned for columns or sample values.

    Args:
        args: Parsed command line.
        db_url: The connection string.
        log: The setup log, updated in place.

    Returns:
        The snapshot of the tables that remain.

    Raises:
        SystemExit: When the database cannot be listed or inspected.
    """
    console.rule("[bold blue]Step 2: Schema Discovery[/bold blue]" if _RICH else "Step 2: Schema Discovery")

    include_schemas = (
        [s.strip() for s in args.include_schemas.split(",") if s.strip()]
        if getattr(args, "include_schemas", None)
        else None
    )

    import config as cfg
    from core.config_doctor import parse_staging_patterns

    # Phase 1: names only. Which schemas are read is decided here, before any
    # column or sample value is fetched, so a schema that is left out is
    # never scanned.
    try:
        listing = _list_schema_tables(db_url)
    except Exception as exc:  # noqa: BLE001 - any driver or connection error
        reason = _scrub_secrets(str(exc), db_url)
        console.print(f"[red]Schema listing failed: {reason}[/red]" if _RICH else f"Schema listing failed: {reason}")
        sys.exit(1)

    staging_patterns = (
        [] if getattr(args, "include_staging", False)
        else parse_staging_patterns(cfg.settings.staging_patterns)
    )
    exclude_patterns = _split_csv(getattr(args, "exclude_schemas", None))
    plan = _plan_reading(listing, include_schemas, exclude_patterns, staging_patterns)
    console.print(
        f"  The database has {sum(len(n) for n in listing.values())} tables in {len(listing)} schemas.",
        markup=False,
    )
    if plan.report.total:
        console.print(f"  {plan.report.describe()}", markup=False)

    chosen_patterns: list[str] = []
    if not args.non_interactive:
        console.print("  Schemas and the tables that would be read:", markup=False)
        for schema, count in sorted(plan.per_schema.items(), key=lambda item: item[0].lower()):
            console.print(f"    {schema}: {count}", markup=False)
        answer = _ask(
            "Schemas to leave out (names or patterns such as *_Stg, comma-separated, blank=none)",
            default="",
        )
        chosen_patterns = _split_csv(answer)
        if chosen_patterns:
            plan = _plan_reading(listing, include_schemas, exclude_patterns, staging_patterns, chosen_patterns)
            console.print(f"  {plan.report.describe()}", markup=False)
    report = plan.report

    # Phase 2: read the schemas that remain. An empty include list would mean
    # "every schema" to the inspector, so with nothing left it does not run.
    if plan.schemas:
        from database.schema_inspector import SchemaInspector
        inspector = SchemaInspector(db_url, sample_rows=5)
        with _spinner("Inspecting schema..."):
            try:
                snapshot = inspector.inspect(
                    include_schemas=plan.schemas,
                    exclude_schemas=[*exclude_patterns, *chosen_patterns] or None,
                    exclude_tables=plan.exclude_tables or None,
                    fetch_row_counts=False,
                )
            except ConnectionError as exc:
                reason = _scrub_secrets(str(exc), db_url)
                console.print(f"[red]Schema inspection failed: {reason}[/red]" if _RICH else reason)
                sys.exit(1)
            finally:
                inspector.close()
    else:
        from database.schema_inspector import SchemaSnapshot
        snapshot = SchemaSnapshot(source_url=_redact_db_url(db_url))

    console.print(
        f"  Read [bold]{len(snapshot.tables)}[/bold] tables: "
        f"[cyan]{len(snapshot.fact_tables)}[/cyan] fact, "
        f"[green]{len(snapshot.dim_tables)}[/green] dim"
        if _RICH else
        f"  Read {len(snapshot.tables)} tables: {len(snapshot.fact_tables)} fact, {len(snapshot.dim_tables)} dim"
    )
    # A staging table whose name another table shares was read; drop it now.
    before = report.staging
    _skip_tables(snapshot, [], staging_patterns, report)
    if report.staging != before:
        console.print(f"  {report.describe()}", markup=False)

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

    # Ask to exclude single tables (a bare name or Schema.Table)
    if not args.non_interactive:
        excl = _ask("Tables to exclude (comma-separated names or Schema.Table, blank=none)", default="")
        if excl.strip():
            wanted = {e.casefold() for e in _split_csv(excl)}
            drop = {
                _identity(t.schema, t.name) for t in snapshot.tables
                if {t.name.casefold(), t.full_name.casefold()} & wanted
            }
            _drop_tables(snapshot, drop)
            report.chosen += len(drop)
            console.print(f"  Excluded {len(drop)} table(s).")

    _mark_done(log, "step2_schema", {
        "schema_fingerprint": _schema_fingerprint(snapshot),
        "skipped": {
            "found": report.found,
            "excluded_schemas": report.excluded_schemas,
            "staging": report.staging,
            "chosen": report.chosen,
        },
        "table_count": len(snapshot.tables),
        "fact_count": len(snapshot.fact_tables),
        "dim_count": len(snapshot.dim_tables),
    })
    return snapshot


def step3_aliases(args, snapshot, llm, log: dict, language: str, namer=None) -> dict:
    """Generate entity aliases via LLM and return entities dict.

    Entities and aliases are keyed by the table's key (``Table`` when the name
    is unique, ``Schema.Table`` when several schemas share it), so tables of
    the same name in different schemas stay separate entries. The model is
    told the schema-qualified name.

    Args:
        args: Parsed command line.
        snapshot: The schema snapshot (skipped tables already removed).
        llm: The wizard LLM.
        log: The setup log, updated in place.
        language: Language the aliases are generated in.
        namer: The key rule; built from *snapshot* when ``None``.

    Returns:
        ``{"entities": {key: {...}}, "ring_aliases": {key: [...]}}``.

    Raises:
        CachedAnswersError: When 3 tables in a row get the identical answer.
    """
    namer = namer or _table_namer(snapshot)
    console.rule("[bold blue]Step 3: LLM Alias Generation[/bold blue]" if _RICH else "Step 3: Alias Generation")

    entities: dict[str, dict] = {}
    ring_aliases: dict[str, list[str]] = {}
    total = len(snapshot.tables)
    guard = _AnswerGuard(
        "Step 3", "alias list", allow=getattr(args, "allow_repeated_answers", False),
    )

    for idx, table in enumerate(snapshot.tables, 1):
        console.print(f"  [{idx}/{total}] {table.full_name} ...", end=" ")

        col_names   = [c.name for c in table.columns]
        all_samples = [s for c in table.columns for s in c.sample_values]

        try:
            prompt = _prompt_aliases(table.full_name, col_names, all_samples, language)
            result = llm.generate(prompt, expect_json=True)
            aliases     = result.get("aliases", []) if isinstance(result, dict) else []
            description = result.get("description", "") if isinstance(result, dict) else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alias generation failed for %s: %s", table.name, exc)
            aliases, description = [], ""

        console.print(f"[green]{len(aliases)} aliases[/green]" if _RICH else f"{len(aliases)} aliases")
        # The model's own answer, before the operator edits it below.
        guard.observe(
            table.full_name,
            "\n".join(str(a) for a in aliases) if isinstance(aliases, list) and aliases else None,
        )

        if not args.non_interactive and not args.dry_run:
            aliases_str = ", ".join(f'"{a}"' for a in aliases) or "(none)"
            console.print(f"    Aliases: {aliases_str}")
            if description:
                console.print(f"    Desc   : {description}")
            action = _choose(
                f"  Accept aliases for {table.full_name}?",
                choices=["Accept", "Edit", "Clear"],
                default="Accept",
            )
            if action == "Edit":
                edited = _ask("Enter aliases (comma-separated)", default=", ".join(aliases))
                aliases = [a.strip().strip('"') for a in edited.split(",") if a.strip()]
            elif action == "Clear":
                aliases = []

        key = namer.key(table)
        entities[key] = {
            "table":   table.name,
            "schema":  table.schema,
            "aliases": aliases,
        }
        if aliases:
            ring_aliases[key] = aliases

    console.print(f"  {guard.summary()}.", markup=False)
    _mark_done(log, "step3_aliases", {
        "entity_count": len(entities),
        "language": language,
        "provider": llm.provider,
        "result": {"entities": entities, "ring_aliases": ring_aliases},
    })
    return {"entities": entities, "ring_aliases": ring_aliases}


#: Start of the placeholder rule written for a fact table the model gave no
#: rule for. A merge treats it as "no answer" and never lets it replace an
#: earlier rule.
_FALLBACK_RULE_PREFIX = "Primary fact table: "


def step4_business_rules(args, snapshot, llm, log: dict, namer=None, links=None) -> dict:
    """Generate business rules for fact tables.

    Rules are keyed like the entities. The join hints the model sees are the
    relationships of the table: declared foreign keys, and, when the database
    declares few, those inferred from column names.

    Args:
        args: Parsed command line.
        snapshot: The schema snapshot (skipped tables already removed).
        llm: The wizard LLM.
        log: The setup log, updated in place.
        namer: The key rule; built from *snapshot* when ``None``.
        links: The relationships (``_collect_links``); collected from
            *snapshot* when ``None``.

    Returns:
        ``{key: rule text}``.

    Raises:
        CachedAnswersError: When 3 tables in a row get the identical answer.
    """
    namer = namer or _table_namer(snapshot)
    if links is None:
        links, _ = _collect_links(snapshot, namer)
    console.rule("[bold blue]Step 4: Business Rules[/bold blue]" if _RICH else "Step 4: Business Rules")

    rules: dict[str, str] = {}
    fact_tables = snapshot.fact_tables
    if not fact_tables:
        console.print("  No fact tables detected — skipping.")
        _mark_done(log, "step4_rules", {"rule_count": 0, "provider": llm.provider, "result": {}})
        return rules

    # Relationship hints and related dimension tables, per fact table key.
    dims_of: dict[str, list[str]] = {}
    joins_of: dict[str, list[str]] = {}
    for link in links:
        if link.to_key not in dims_of.setdefault(link.from_key, []):
            dims_of[link.from_key].append(link.to_key)
        joins_of.setdefault(link.from_key, []).append(link.join_hint)

    guard = _AnswerGuard(
        "Step 4", "rule text", allow=getattr(args, "allow_repeated_answers", False),
    )

    for table in fact_tables:
        key = namer.key(table)
        console.print(f"  {table.full_name} ...", end=" ")
        col_dicts = [{"name": c.name, "type": c.type} for c in table.columns]
        dim_tables = dims_of.get(key, [])
        fk_joins   = joins_of.get(key, [])

        try:
            prompt = _prompt_business_rules(table.full_name, col_dicts, dim_tables, fk_joins)
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

        # The model's own answer (the fallback below is ours), table names taken out.
        model_answer = _normalize_rule_text(rule_text, [table.name, table.full_name, key, *dim_tables])
        if not rule_text:
            rule_text = (
                f"{_FALLBACK_RULE_PREFIX}{table.name}. "
                f"Columns: {', '.join(c.name for c in table.columns[:8])}."
            )

        console.print("[green]done[/green]" if _RICH else "done")
        guard.observe(table.full_name, model_answer or None)
        rules[key] = rule_text

    console.print(f"  {guard.summary()}.", markup=False)
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
    *,
    namer=None,
    links: list[_Link] | None = None,
    existing: _ExistingDraft | None = None,
    rename: Mapping[str, str] | None = None,
    backup_stamp: str | None = None,
    backups: list[Path] | None = None,
) -> None:
    """Review generated files and write to disk.

    Each file is shown for review (accept, edit, regenerate, skip). With
    ``--resume``, a file that already exists is left alone.

    What is shown and written is the merge of this run into *existing* (the
    files the output directory already held): see ``core.wizard_drafts``. The
    counts are printed per file and recorded in the log.

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
        namer: The key rule; built from *snapshot* when ``None``.
        links: The relationships to write; collected from *snapshot* when ``None``.
        existing: What the output directory held before this run; nothing
            (no merge) when ``None``.
        rename: ``{old key: new key}`` for entries of *existing* to re-key.
        backup_stamp: When set, each existing file is copied to
            ``<name>.bak-<stamp>`` just before it is replaced.
        backups: Receives the backup files made.

    Returns:
        None. Marks ``step6_write`` in *log* with the files written, the files
        not written, the merge counts per file and the backups made.

    Raises:
        Nothing for an LLM failure while regenerating; it is reported and the
        current version is kept.
    """
    console.rule("[bold blue]Step 6: Review & Write[/bold blue]" if _RICH else "Step 6: Review")

    namer = namer or _table_namer(snapshot)
    if links is None:
        links, _ = _collect_links(snapshot, namer)
    existing = existing or _ExistingDraft()
    rename = dict(rename or {})
    stats: dict[str, MergeStats] = {}
    made: list[Path] = backups if backups is not None else []

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
        """The file as it would be written: this run merged into the earlier files."""
        if filename == "entities.yaml":
            # A label is curated by hand; the replacement keeps it.
            old_labels = {
                rename.get(k, k).casefold(): v.get("label") for k, v in existing.entities.items()
            }
            fresh = {
                k: ({**v, "label": old_labels[k.casefold()]}
                    if old_labels.get(k.casefold()) and not v.get("label") else v)
                for k, v in state["entities_data"].items()
            }
            # No aliases from the model for a table that has some: keep what is there.
            fresh, held = hold_back_empty(
                existing.entities, fresh, lambda entry: not entry.get("aliases"), rename,
            )
            merged, merge_stats = merge_mapping(existing.entities, fresh, rename)
            stats[filename] = replace(merge_stats, held=held)
            return _entities_yaml(merged, source_url, generated_at)
        if filename == "aliases.yaml":
            ring, ring_stats = merge_mapping(existing.ring_aliases, state["ring_aliases"], rename)
            merged_synonyms, synonym_stats = merge_synonyms(existing.synonyms, synonyms)
            stats[filename] = ring_stats + synonym_stats
            return _aliases_yaml(ring, merged_synonyms, generated_at)
        if filename == "business_rules.yaml":
            # The placeholder written when the model gave no rule never replaces a rule.
            fresh_rules, held = hold_back_empty(
                existing.rules, state["rules"],
                lambda text: not text or text.startswith(_FALLBACK_RULE_PREFIX), rename,
            )
            merged, merge_stats = merge_mapping(existing.rules, fresh_rules, rename)
            stats[filename] = replace(merge_stats, held=held)
            return _business_rules_yaml(merged, generated_at)
        if filename == "examples.yaml":
            merged_examples, stats[filename] = merge_examples(existing.examples, state["examples"])
            return _examples_yaml(merged_examples, generated_at)
        merged_links, stats[filename] = merge_relationships(
            existing.relationships, [link.as_entry() for link in links], rename,
        )
        return _relationships_yaml(merged_links, generated_at)

    def regenerate(filename: str) -> str:
        quiet = _quiet(args)
        if filename in ("entities.yaml", "aliases.yaml"):
            result = step3_aliases(quiet, snapshot, llm, log, language, namer)
            state["entities_data"], state["ring_aliases"] = result["entities"], result["ring_aliases"]
            other = "aliases.yaml" if filename == "entities.yaml" else "entities.yaml"
            if other in written:
                console.print(
                    f"  Note: {other} was written earlier in this run from the previous aliases "
                    "and is not rewritten; review it again or run the wizard again.",
                    markup=False,
                )
        elif filename == "business_rules.yaml":
            state["rules"] = step4_business_rules(quiet, snapshot, llm, log, namer, links)
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
            if backup_stamp:
                for copy in back_up([target], backup_stamp):
                    made.append(copy)
                    console.print(f"  Backed up: {target} -> {copy.name}", markup=False)
            target.write_text(accepted, encoding="utf-8")
            console.print(f"  [green]Written:[/green] {target}" if _RICH else f"  Written: {target}")
            written.append(filename)
        elif not args.dry_run:
            not_written.append(filename)

    _mark_done(log, "step6_write", {
        "files_written": written,
        "files_not_written": not_written,
        "merge": {
            name: {"kept": st.kept, "added": st.added, "replaced": st.replaced, "held": len(st.held)}
            for name, st in stats.items()
        },
        "rekeyed": {name: [list(pair) for pair in st.rekeyed] for name, st in stats.items() if st.rekeyed},
        "held": {name: list(st.held) for name, st in stats.items() if st.held},
        "backups": [str(b) for b in made],
        "inferred_relationships": sum(1 for link in links if link.inferred),
    })


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


def _print_summary(log: dict, output_dir: Path, protected: str | None, args) -> None:
    """Print what the run did: skipped tables, merge counts, backups, where to go next.

    Args:
        log: The setup log of this run.
        output_dir: The directory written to.
        protected: What ``protected_directory`` said about it (``None`` for a draft directory).
        args: Parsed command line.

    Returns:
        None.

    Raises:
        Nothing. Missing log entries are left out.
    """
    console.rule("[bold blue]Summary[/bold blue]" if _RICH else "Summary")
    skipped = log.get("step2_schema", {}).get("skipped")
    if isinstance(skipped, dict):
        console.print(f"  {SkipReport(**skipped).describe()}", markup=False)
    done = log.get("step6_write", {})
    verb = "would be" if args.dry_run else "are"
    merge = done.get("merge", {})
    if merge:
        what = "no merge (--fresh)" if args.fresh else "merged into the files already there"
        console.print(f"  Entries ({what}; counts {verb} per file):", markup=False)
        for name, counts in merge.items():
            held = counts.get("held", 0)
            console.print(
                f"    {name}: kept {counts['kept']}, added {counts['added']}, "
                f"replaced {counts['replaced']}"
                + (f" ({held} of the kept have no new answer from the model)" if held else ""),
                markup=False,
            )
    for name, keys in done.get("held", {}).items():
        shown = ", ".join(keys[:10])
        console.print(
            f"  {name}: {len(keys)} earlier entr{'y' if len(keys) == 1 else 'ies'} kept because this run "
            f"gave no answer for {'it' if len(keys) == 1 else 'them'}: {shown}{' ...' if len(keys) > 10 else ''}",
            markup=False,
        )
    for name, pairs in done.get("rekeyed", {}).items():
        shown = ", ".join(f"{old} -> {new}" for old, new in pairs[:10])
        console.print(
            f"  Re-keyed in {name} ({len(pairs)}): a bare name is now shared by several "
            f"schemas: {shown}{' ...' if len(pairs) > 10 else ''}",
            markup=False,
        )
    inferred = done.get("inferred_relationships", 0)
    if inferred:
        console.print(
            f"  {inferred} relationship(s) in relationships.yaml are inferred from column "
            "names and marked REVIEW.",
            markup=False,
        )
    if done.get("backups"):
        console.print("  Backups made:", markup=False)
        for name in done["backups"]:
            console.print(f"    {name}", markup=False)
    if args.dry_run:
        console.print("  Dry run: nothing was written.", markup=False)
    elif protected:
        console.print(
            f"  Written into {output_dir}, a protected directory (--overwrite-live); "
            "the previous files are in the .bak-* copies.",
            markup=False,
        )
    else:
        console.print(
            f"  These are DRAFTS in {output_dir}. Review them, then copy the files you accept "
            f"into {_live_config_dir().name}/ yourself; nothing there was changed.",
            markup=False,
        )


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
    p.add_argument("--output",       metavar="DIR",  default=str(_DEFAULT_OUTPUT),
                   help="Directory the draft files are written to. Default: "
                        f"{DEFAULT_DRAFT_DIR}/. The live configuration directory and "
                        "project_config.example/ are refused unless --overwrite-live is given.")
    p.add_argument("--overwrite-live", action="store_true", default=False,
                   help="Allow --output to be the live configuration directory or "
                        "project_config.example/. Each file the wizard replaces is first "
                        "copied to <name>.bak-<YYYYmmdd-HHMMSS> next to it.")
    p.add_argument("--fresh", action="store_true", default=False,
                   help="Start from scratch: do not merge into the files already in the "
                        "output directory.")
    p.add_argument("--review",       metavar="MODE", default="interactive",
                   choices=["interactive", "auto"])
    p.add_argument("--include-schemas", metavar="SCHEMAS", default=None,
                   help="Comma-separated schemas to read (default: all).")
    p.add_argument("--exclude-schemas", metavar="PATTERNS", default=None,
                   help="Comma-separated fnmatch patterns of schemas to leave out, "
                        "case-insensitive (for example '*_Stg,Replay'). Wins over "
                        "--include-schemas.")
    p.add_argument("--include-staging", action="store_true", default=False,
                   help="Keep the staging and change-capture tables STAGING_PATTERNS "
                        "names; by default they are skipped and never reach the model.")
    p.add_argument("--non-interactive", action="store_true", default=False)
    p.add_argument("--allow-repeated-answers", action="store_true", default=False,
                   help="Do not stop step 3 or 4 when 3 tables in a row get the identical "
                        "answer from the model (a sign that a response cache in front of the "
                        "model is answering for it). For a genuinely tiny schema.")
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
        0 on success, 2 when the LLM configuration is refused or invalid,
        3 when step 3 or 4 stopped because 3 tables in a row got the identical
        answer (nothing is written; ``--resume`` continues; see
        ``--allow-repeated-answers``). A failed database connection exits the
        process with status 1.

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

    # The live configuration directory and the committed template are not
    # places for drafts: refused before anything is read or asked, unless the
    # operator says so, and then every file replaced is copied aside first.
    protected = protected_directory(output_dir, _live_config_dir(), _REPO_ROOT)
    backup_stamp: str | None = None
    if protected and not args.dry_run:
        if not args.overwrite_live:
            console.print(f"ERROR: {refusal_message(output_dir, protected)}", markup=False)
            return 2
        backup_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups: list[Path] = []

    if args.dry_run and args.resume:
        console.print("  Note: --resume is ignored with --dry-run (nothing is written).", markup=False)
        args.resume = False
    # A fresh run starts a fresh log: leftover entries of an earlier run would
    # otherwise look like completed steps to a later --resume.
    log: dict = _load_log(log_path) if args.resume else {}
    if backup_stamp:
        # The setup log is replaced too.
        for copy in back_up([log_path], backup_stamp):
            backups.append(copy)
            console.print(f"  Backed up: {log_path} -> {copy.name}", markup=False)

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

    # ---- What the output directory already holds, to merge into ----
    existing = _ExistingDraft()
    if not args.fresh:
        # With --resume a file that exists is not rewritten, so it is not read.
        to_read = tuple(
            n for n in _WIZARD_OUTPUT_FILES if not (args.resume and (output_dir / n).exists())
        )
        existing, problems = _read_existing(output_dir, to_read)
        if problems:
            for name, why in problems.items():
                console.print(f"ERROR: {output_dir / name} cannot be merged into: {why}", markup=False)
            console.print(
                "  Fix or move the file, or run with --fresh to start from scratch.", markup=False,
            )
            return 2

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

    # Keys: the inspector's rule, over this run's tables and those of the
    # earlier run, so a bare name that is now shared is qualified for both.
    namer = _table_namer(snapshot, existing.entities)
    rename = _rekey_map(namer, existing.entities)
    links, links_problem = _collect_links(snapshot, namer)
    if links_problem:
        console.print(f"  {links_problem}", markup=False)
    fingerprint = _schema_fingerprint(snapshot, namer)
    log.setdefault("step2_schema", {})["schema_fingerprint"] = fingerprint
    save()

    reuse = args.resume and stored_fingerprint == fingerprint
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
            alias_result = step3_aliases(args, snapshot, need_llm(), log, language, namer)
        entities_data = alias_result["entities"]
        ring_aliases  = alias_result["ring_aliases"]
        save()

        # ---- Step 4: Business rules ----
        if stored_rules is not None:
            console.print("  Step 4: reusing the business rules generated by the earlier run.", markup=False)
            rules = stored_rules
        else:
            rules = step4_business_rules(args, snapshot, need_llm(), log, namer, links)
        save()

        # ---- Step 5: Examples ----
        if stored_examples is not None:
            console.print("  Step 5: reusing the examples generated by the earlier run.", markup=False)
            examples = stored_examples
        else:
            examples = step5_examples(args, snapshot, need_llm(), log, language)
        save()
    except CachedAnswersError as exc:
        # Nothing is written: no project file, and the steps that finished
        # stay recorded in the log, so --resume continues from this one.
        save()
        console.print(f"ERROR: {exc}", markup=False)
        return 3
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
        namer=namer,
        links=links,
        existing=existing,
        rename=rename,
        backup_stamp=backup_stamp,
        backups=backups,
    )
    save()

    # ---- Step 7: Validate ----
    if not args.dry_run:
        step7_validate(output_dir, log)
        save()

    _print_summary(log, output_dir, protected, args)
    return 0


if __name__ == "__main__":
    use_utf8_console()
    sys.exit(main())
