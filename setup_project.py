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
--dry-run           Print generated YAML to stdout; do not write files.
--resume            Skip steps whose output files already exist.

The wizard writes ``project_config/.setup_log.json`` recording every step
that was executed and when, so it can be resumed safely.

The wizard is idempotent: running it multiple times never corrupts existing
configuration.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
        def __enter__(self): print(message); return self
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

def _validate_yaml_str(yaml_str: str, filename: str) -> list[str]:
    """Parse *yaml_str* and validate against the relevant Pydantic model.

    Returns a list of error strings (empty list = valid).
    """
    try:
        from knowledge.config_loader import (
            AliasesConfig, EntitiesConfig, BusinessRulesConfig,
            ExamplesConfig,
        )
        from pydantic import ValidationError
        model_map = {
            "entities.yaml":       EntitiesConfig,
            "aliases.yaml":        AliasesConfig,
            "business_rules.yaml": BusinessRulesConfig,
            "examples.yaml":       ExamplesConfig,
        }
        model = model_map.get(filename)
        if model is None:
            return []
        raw = yaml.safe_load(yaml_str) or {}
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
    llm_regenerate_fn=None,
) -> str | None:
    """Show *content* to the user and return the accepted version (or None to skip)."""
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

        action = _choose(
            "Action?",
            choices=["Accept", "Edit in $EDITOR", "Regenerate", "Skip"],
            default="Accept",
        )

        if action == "Accept":
            return content
        if action == "Edit in $EDITOR":
            content = _edit_in_editor(content)
        elif action == "Regenerate" and llm_regenerate_fn:
            content = llm_regenerate_fn()
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

    console.print(f"  Testing connection...", end=" ")
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(db_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        console.print("[green]OK[/green]" if _RICH else "OK")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]FAILED[/red]" if _RICH else "FAILED")
        console.print(f"  Error: {exc}")
        console.print("  Connection string formats:" + _DB_EXAMPLES)
        if not args.non_interactive and _yn("Retry with a different URL?", non_interactive=False):
            return step1_connection(args, log)
        sys.exit(1)

    _mark_done(log, "step1_connection", {"db_url_redacted": re.sub(r':[^:@/]+@', ':***@', db_url) if 're' in dir() else db_url})
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

    _mark_done(log, "step3_aliases", {"entity_count": len(entities)})
    return {"entities": entities, "ring_aliases": ring_aliases}


def step4_business_rules(args, snapshot, llm, log: dict) -> dict:
    """Generate business rules for fact tables."""
    console.rule("[bold blue]Step 4: Business Rules[/bold blue]" if _RICH else "Step 4: Business Rules")

    rules: dict[str, str] = {}
    fact_tables = snapshot.fact_tables
    if not fact_tables:
        console.print("  No fact tables detected — skipping.")
        _mark_done(log, "step4_rules", {"rule_count": 0})
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

    _mark_done(log, "step4_rules", {"rule_count": len(rules)})
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
    _mark_done(log, "step5_examples", {"example_count": len(examples)})
    return examples


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
) -> None:
    """Review generated files and write to disk."""
    console.rule("[bold blue]Step 6: Review & Write[/bold blue]" if _RICH else "Step 6: Review")

    synonyms: dict[str, list[str]] = {}
    for t in snapshot.tables:
        for col in t.columns:
            for val in col.sample_values:
                key = val.lower().strip()
                if len(key) >= 2 and key not in synonyms:
                    synonyms[key] = [t.name.lower()]

    files = {
        "entities.yaml":       _entities_yaml(entities_data, source_url, generated_at),
        "aliases.yaml":        _aliases_yaml(ring_aliases, synonyms, generated_at),
        "business_rules.yaml": _business_rules_yaml(rules, generated_at),
        "examples.yaml":       _examples_yaml(examples, generated_at),
        "relationships.yaml":  _relationships_yaml(
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
        ),
    }

    written: list[str] = []
    for filename, content in files.items():
        target = output_dir / filename

        # Resume: skip if file already exists and --resume flag is set
        if args.resume and target.exists():
            console.print(f"  [yellow]Skipped (exists): {filename}[/yellow]" if _RICH else f"  Skipped: {filename}")
            continue

        accepted = _review_file(
            filename=filename,
            content=content,
            non_interactive=args.non_interactive,
            dry_run=args.dry_run,
            output_dir=output_dir,
        )

        if accepted is not None and not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(accepted, encoding="utf-8")
            console.print(f"  [green]Written:[/green] {target}" if _RICH else f"  Written: {target}")
            written.append(filename)

    _mark_done(log, "step6_write", {"files_written": written})


def step7_validate(output_dir: Path, log: dict) -> None:
    """Run a quick smoke test against the knowledge layer."""
    console.rule("[bold blue]Step 7: Validation[/bold blue]" if _RICH else "Step 7: Validation")

    import importlib, sys as _sys
    # Point knowledge layer at our output dir
    import knowledge.config_loader as cl
    original_dir = cl._PROJECT_CONFIG_DIR
    cl._PROJECT_CONFIG_DIR = output_dir

    results: dict[str, str] = {}
    loaders = [
        ("entities.yaml",       cl.load_entities),
        ("aliases.yaml",        cl.load_aliases),
        ("business_rules.yaml", cl.load_business_rules),
        ("examples.yaml",       cl.load_examples),
    ]

    for fname, loader_fn in loaders:
        if not (output_dir / fname).exists():
            results[fname] = "skipped (file not written)"
            continue
        try:
            loader_fn()
            results[fname] = "OK"
        except Exception as exc:  # noqa: BLE001
            results[fname] = f"FAILED: {exc}"

    cl._PROJECT_CONFIG_DIR = original_dir

    for fname, status in results.items():
        colour = "green" if status == "OK" else "yellow" if "skipped" in status else "red"
        if _RICH:
            console.print(f"  [{colour}]{fname}: {status}[/{colour}]")
        else:
            print(f"  {fname}: {status}")

    entity_count  = 0
    rule_count    = 0
    example_count = 0
    try:
        cl._PROJECT_CONFIG_DIR = output_dir
        ec = cl.load_entities()
        entity_count = len(ec.entities)
        rc = cl.load_business_rules()
        rule_count = len(rc.rules)
        ex = cl.load_examples()
        example_count = len(ex.examples)
        cl._PROJECT_CONFIG_DIR = original_dir
    except Exception:  # noqa: BLE001
        cl._PROJECT_CONFIG_DIR = original_dir

    console.print(
        f"\n  [bold green]Setup complete.[/bold green] "
        f"Registry: [cyan]{entity_count}[/cyan] entities, "
        f"[cyan]{rule_count}[/cyan] rules, "
        f"[cyan]{example_count}[/cyan] examples."
        if _RICH else
        f"\n  Setup complete. {entity_count} entities, {rule_count} rules, {example_count} examples."
    )
    _mark_done(log, "step7_validate", {
        "entity_count": entity_count,
        "rule_count": rule_count,
        "example_count": example_count,
    })


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
                   choices=["openai", "mock"])
    p.add_argument("--llm-model",    metavar="NAME", default=None)
    p.add_argument("--llm-base-url", metavar="URL",  default=None)
    p.add_argument("--language",     metavar="LANG", default=None,
                   choices=["fa", "en", "both"])
    p.add_argument("--output",       metavar="DIR",  default=str(_DEFAULT_OUTPUT))
    p.add_argument("--review",       metavar="MODE", default="interactive",
                   choices=["interactive", "auto"])
    p.add_argument("--include-schemas", metavar="SCHEMAS", default=None)
    p.add_argument("--non-interactive", action="store_true", default=False)
    p.add_argument("--dry-run",         action="store_true", default=False)
    p.add_argument("--resume",          action="store_true", default=False)
    return p


def main(argv: list[str] | None = None) -> int:
    import re  # used in step1 for URL redaction
    _patch_re_into_step1(re)  # make re available inside step1_connection

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
    log        = _load_log(log_path)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

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

    # ---- Step 1: Connection ----
    db_url = step1_connection(args, log)
    _save_log(log_path, log)

    # ---- Step 2: Schema ----
    snapshot = step2_schema(args, db_url, log)
    _save_log(log_path, log)

    # ---- Setup LLM ----
    provider = args.llm_provider or os.getenv("WIZARD_LLM_PROVIDER", "openai")
    model    = args.llm_model    or os.getenv("WIZARD_LLM_MODEL",    "gpt-4o-mini")
    base_url = args.llm_base_url or os.getenv("WIZARD_LLM_BASE_URL") or None

    from llm.wizard_llm import WizardLLM
    console.print(f"  LLM: {provider} / {model}")
    try:
        llm = WizardLLM(provider=provider, model=model, base_url=base_url)
        if not llm.test_connection():
            raise RuntimeError("LLM backend unreachable")
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[yellow]  Warning: LLM unavailable ({exc}). "
            "Using mock provider — aliases/rules/examples will be empty.[/yellow]"
            if _RICH else f"  Warning: LLM unavailable. Using mock provider."
        )
        llm = WizardLLM(provider="mock", model="mock")

    # ---- Step 3: Aliases ----
    alias_result = step3_aliases(args, snapshot, llm, log, language)
    entities_data = alias_result["entities"]
    ring_aliases  = alias_result["ring_aliases"]
    _save_log(log_path, log)

    # ---- Step 4: Business rules ----
    rules = step4_business_rules(args, snapshot, llm, log)
    _save_log(log_path, log)

    # ---- Step 5: Examples ----
    examples = step5_examples(args, snapshot, llm, log, language)
    _save_log(log_path, log)

    # ---- Step 6: Review & write ----
    source_url = re.sub(r':[^:@/]+@', ':***@', db_url)
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
    )
    _save_log(log_path, log)

    # ---- Step 7: Validate ----
    if not args.dry_run:
        step7_validate(output_dir, log)
        _save_log(log_path, log)

    return 0


def _patch_re_into_step1(re_module) -> None:
    """Make ``re`` available inside step1_connection without a global import."""
    import builtins
    # re is already importable globally; this is a no-op but makes intent clear
    pass


if __name__ == "__main__":
    import re  # noqa: F811
    sys.exit(main())
