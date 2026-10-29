# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Draft the real values of ``memory_policy.yaml`` and ``session_policy.yaml`` for review.

Usage (from the repo root, with the server's environment active)::

    python scripts/draft_policies.py                              # the dimension the files already name
    python scripts/draft_policies.py --table Region --column Name # pick the dimension
    python scripts/draft_policies.py --dry-run                    # counts only; nothing is written
    python scripts/draft_policies.py --dry-run --show-values      # ... and the first few values
    python scripts/draft_policies.py --max-values 100             # refuse a column with more values
    python scripts/draft_policies.py --overwrite-live             # write into project_config/ (with .bak)

Why it exists
-------------
``session_policy.yaml`` (``default_scope``: the scope a ranking question gets
when it names none, and the options its clarification offers) and
``memory_policy.yaml`` (the closed set of values an analyst may pin with the
pin on an assumption chip) are two of the ten required files, and no setup tool
wrote them: the wizard drafts entities, aliases, rules and examples, the
inspector and the sync write ``schema.yaml``, ``draft_descriptions.py`` the
descriptions. On a first install both are the shipped template, whose options
are made-up samples, so every real value an analyst tries to pin is refused and
the clarification offers values the warehouse does not have. The preflight check
``Session and memory policies use real values`` says so; this command writes the
real ones. It is a separate command, like ``draft_descriptions.py``, and not a
step of the wizard: the wizard reads every table and asks a model, this reads one
small column and asks nothing, and it has to be run again whenever the dimension
gains a value, which should not mean running the wizard.

What it does
------------
* Picks the dimension: ``--table`` and ``--column``, else the ``filter_key`` of
  ``session_policy.yaml`` and the ``column`` of the memory key(s) that share it,
  when they name a real table and column of ``schema.yaml``. A table is found as
  the application finds a configured table name (``Customer`` or
  ``sales.Customer``, case and brackets ignored); a name that fits several
  tables is refused. A column named like personal or contact data, or a ``Name``
  of a table of people, is never read (:mod:`core.value_shapes`).
* Reads the column's distinct values with **one bounded query**
  (``SELECT DISTINCT TOP (N+1) ... WHERE col IS NOT NULL ORDER BY col``,
  ``--max-values N``, default 200) through the application's own engine and
  login (:func:`database.executor.execute_sql_params`: read-only, rolled back,
  timeout and row cap), on the data source of the table's ``datasource:`` (the
  default source without one). A column with more than N distinct values is
  refused: a scope must be a small dimension.
* Keeps the values the wizard's rules accept (no date, number, code, phone
  number, address, masked value or error text; at most 40 characters and the
  memory key's ``max_length``), without the session policy's ``default_label``,
  one spelling of each value under the project's Persian and Arabic folding,
  sorted.
* Writes ``project_config_draft/memory_policy.yaml`` and
  ``project_config_draft/session_policy.yaml``: the current files with only
  ``options`` replaced (and ``filter_key`` / ``column`` when ``--table`` /
  ``--column`` moved the dimension). Comments, ``max_length``, ``field_name``,
  ``default_label``, ``clarification_prompt`` and every other field stay as they
  were; the script proves it by loading the result with the application's
  validators. The memory options never contain the ``default_label``; the session
  options are the ``default_label`` followed by the values. A memory key with no
  ``options`` (any value up to ``max_length``) is left as it is unless
  ``--memory-key`` names it.
* Never writes into ``project_config/`` or ``project_config.example/`` unless
  ``--overwrite-live`` is given; then each file it replaces is first copied to
  ``<name>.bak-<YYYYmmdd-HHMMSS>`` next to it, as the wizard does. An existing
  draft is not replaced without ``--force``.

The values are warehouse data. The run prints counts only; ``--show-values``
also prints the first few. The files it writes hold them, as they must.

Exit codes
----------
0 -- done (or a dry run); 2 -- nothing could be decided or written: a file does
not load, the dimension is not a real table and column, the column has too many
values or none usable, the output is protected or exists, or the database cannot
be read.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

# Run as `python scripts/draft_policies.py` from the repo root: Python puts only
# this script's own directory on sys.path, so the repo root is added for
# `import config` and the packages below -- same as scripts/draft_descriptions.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.console import use_utf8_console  # noqa: E402
import config as cfg  # noqa: E402 - loads .env, like every entry point
from core import config_doctor as doctor  # noqa: E402
from core.policy_drafts import choose_memory_keys, draft_policies, usable_values  # noqa: E402
from core.policy_values import MEMORY_FILE, SESSION_FILE, read_policies  # noqa: E402
from core.redaction import redact_error_text  # noqa: E402
from core.value_shapes import is_personal_column  # noqa: E402
from core.wizard_drafts import DEFAULT_DRAFT_DIR, back_up, protected_directory  # noqa: E402
from schema_data.registry import (  # noqa: E402
    SchemaConfig,
    bare_table_name,
    effective_qualifier,
    table_reference_sql,
    validate_schema_yaml_text,
)

__all__ = [
    "DEFAULT_MAX_VALUES",
    "EXIT_ERROR",
    "EXIT_OK",
    "ValueReader",
    "default_reader",
    "distinct_values_query",
    "main",
]

EXIT_OK = 0
EXIT_ERROR = 2

#: Most distinct values a column may have to be a scope: more is not a small dimension.
DEFAULT_MAX_VALUES = 200

#: How many values ``--show-values`` prints.
_SHOWN = 10

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: ``read(source, qualifier, table key, column, limit) -> values``: at most *limit* distinct,
#: non-NULL values of the column, sorted. The real reader queries the warehouse; tests inject a fake.
ValueReader = Callable[[str, str, str, str, int], Sequence[object]]


def distinct_values_query(key: str, qualifier: str, column: str, dialect: str = "tsql") -> str:
    """The one statement that reads a dimension column: ``TOP (?)`` distinct non-NULL values, sorted.

    Authored in T-SQL and transpiled for any other dialect, the way
    ``retrieval.dimension_vocabulary`` builds its prefetch statement; the limit
    is a bound parameter and the names are quoted by the dialect's own rule.

    Parameters
    ----------
    key:
        The table's ``schema.yaml`` key.
    qualifier:
        Its effective schema qualifier (``"ref"``, ``"OtherDb.dbo"`` or ``""``).
    column:
        The column.
    dialect:
        The target dialect (``Settings.sql_dialect``).

    Examples
    --------
    >>> distinct_values_query("ref.Region", "ref", "Name")
    'SELECT DISTINCT TOP (?) [Name] FROM [ref].[Region] WHERE [Name] IS NOT NULL ORDER BY [Name]'
    >>> distinct_values_query("Region", "", "Na]me")
    'SELECT DISTINCT TOP (?) [Na]]me] FROM [Region] WHERE [Na]]me] IS NOT NULL ORDER BY [Na]]me]'
    """
    from security.dialects import get_dialect_profile, quote_tsql_identifier
    from security.sql_guard import transpile_sql

    if get_dialect_profile(dialect).schema_qualification == "none":
        table = quote_tsql_identifier(bare_table_name(key))
    else:
        table = table_reference_sql(key, qualifier)
    name = quote_tsql_identifier(column)
    tsql = f"SELECT DISTINCT TOP (?) {name} FROM {table} WHERE {name} IS NOT NULL ORDER BY {name}"
    return tsql if dialect == "tsql" else transpile_sql(tsql, source_dialect="tsql", target_dialect=dialect)


def default_reader() -> ValueReader:
    """The reader a real run uses: the application's own engine and login, read-only.

    It goes through :func:`database.executor.execute_sql_params`, the path every
    query of the application takes: a transaction that is always rolled back, the
    query timeout, ``WITH (NOLOCK)`` where the source asks for it, the row cap.
    """

    def read(source: str, qualifier: str, key: str, column: str, limit: int) -> Sequence[object]:
        from database.executor import execute_sql_params

        sql = distinct_values_query(key, qualifier, column, cfg.settings.sql_dialect)
        frame = execute_sql_params(sql, (limit,), datasource=source)
        return [] if frame is None or frame.empty else frame.iloc[:, 0].tolist()

    return read


def _live_directory() -> Path:
    """The directory the application reads (``PROJECT_CONFIG_DIR``; a relative one is under the repo root)."""
    configured = Path(cfg.settings.project_config_dir)
    return configured if configured.is_absolute() else _REPO_ROOT / configured


def _describe(exc: BaseException) -> str:
    """One short line about *exc*, without credentials."""
    first = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {redact_error_text(first)[:160]}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="draft_policies.py",
        description=(
            f"Draft the real option values of {MEMORY_FILE} (what an analyst may pin) and {SESSION_FILE} "
            "(what a clarification offers) from the distinct values of one small dimension column, "
            f"for review. Writes {DEFAULT_DRAFT_DIR}/ by default; replaces only `options` (and `filter_key` / "
            "`column` when the dimension is moved)."
        ),
    )
    parser.add_argument(
        "--table", metavar="NAME",
        help="The dimension table, as a schema.yaml key or a bare name. Default: the filter_key of session_policy.yaml.",
    )
    parser.add_argument(
        "--column", metavar="NAME",
        help="The column whose distinct values are the options. Default: the column of the memory key.",
    )
    parser.add_argument(
        "--memory-key", metavar="KEY", default=None,
        help=(
            f"The key of {MEMORY_FILE} to draft. Default: the keys that share the filter_key of "
            f"{SESSION_FILE} and have a closed set of options."
        ),
    )
    parser.add_argument(
        "--max-values", type=int, default=DEFAULT_MAX_VALUES, metavar="N",
        help=f"Refuse a column with more than N distinct values (default {DEFAULT_MAX_VALUES}).",
    )
    parser.add_argument(
        "--config-dir", type=Path, metavar="DIR",
        help="Where schema.yaml and the two policy files are read from (default: PROJECT_CONFIG_DIR).",
    )
    parser.add_argument(
        "--output", type=Path, default=Path(DEFAULT_DRAFT_DIR), metavar="DIR",
        help=(
            f"Where the drafts are written (default {DEFAULT_DRAFT_DIR}/). The live configuration directory and "
            "project_config.example/ are refused unless --overwrite-live is given."
        ),
    )
    parser.add_argument(
        "--overwrite-live", action="store_true",
        help="Allow --output to be the live configuration directory; each file replaced is first copied to <name>.bak-<stamp>.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Replace drafts that already exist in --output (not needed with --overwrite-live, which backs them up).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read the column and print the counts; write nothing.",
    )
    parser.add_argument(
        "--show-values", action="store_true",
        help=f"Also print the first {_SHOWN} values. They are warehouse data, so they are not printed otherwise.",
    )
    return parser


def _error(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return EXIT_ERROR


def _dimension(
    args: argparse.Namespace,
    policies,
    view: doctor.SchemaView,
    schema: SchemaConfig,
    chosen: Sequence[str],
) -> tuple[str, str] | str:
    """``(table key, column as schema.yaml spells it)`` for the run, or the reason it cannot be decided."""
    scope = policies.session.default_scope
    reference = args.table or scope.filter_key
    keys = doctor.resolve_table_keys(view, reference)
    if not keys:
        return (
            f"{reference!r} names no table of schema.yaml"
            + ("" if args.table else f" (it is the filter_key of {SESSION_FILE} default_scope): name the dimension with --table and --column")
        )
    if len(keys) > 1:
        return f"{reference!r} fits {len(keys)} tables of schema.yaml ({', '.join(keys)}); give the key of one with --table"
    key = keys[0]
    columns = {c.casefold() for c in (schema.tables[key].columns or {})}
    if not columns:
        return f"schema.yaml lists no column of {key}; the column cannot be checked, so nothing is read"
    wanted = {policies.memory.keys[name].column for name in chosen}
    column = args.column
    if column is None:
        if len({c.casefold() for c in wanted}) != 1:
            return f"the memory keys name different columns ({', '.join(sorted(wanted))}); choose one with --column"
        column = next(iter(wanted))
    spelled = next((name for name in schema.tables[key].columns or {} if name.casefold() == column.strip().casefold()), "")
    if not spelled:
        listed = ", ".join(schema.tables[key].columns or {})
        return f"{column!r} is not a column of {key} in schema.yaml (columns: {listed}); name the dimension with --column"
    return key, spelled


def _write_atomically(path: Path, text: str) -> None:
    """Write *text* to *path* through a temporary file and a rename."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(text.encode("utf-8"))
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None, *, read_values: ValueReader | None = None) -> int:
    """Run the script; returns the process exit code.

    Parameters
    ----------
    argv:
        Command-line arguments; ``sys.argv[1:]`` when ``None``.
    read_values:
        Reads the column (:data:`ValueReader`). Defaults to :func:`default_reader`;
        tests inject a fake. Called once, after every check that needs no database.

    Returns
    -------
    int
        ``EXIT_OK`` or ``EXIT_ERROR`` (see the module docstring).
    """
    args = _build_parser().parse_args(argv)
    directory = args.config_dir or _live_directory()
    limit = args.max_values
    if limit < 1:
        return _error("--max-values must be a positive number")
    if limit + 1 > cfg.settings.max_rows_returned:
        return _error(
            f"--max-values {limit} needs {limit + 1} rows and the application returns at most "
            f"{cfg.settings.max_rows_returned} (MAX_ROWS_RETURNED); choose a smaller number"
        )

    protected = protected_directory(args.output, _live_directory(), _REPO_ROOT)
    if protected and not args.overwrite_live and not args.dry_run:
        where = "the live configuration directory" if protected == "live" else "the committed template directory (project_config.example/)"
        return _error(
            f"{args.output} is {where}. The drafts are for review: write them to {DEFAULT_DRAFT_DIR}/ (the default) "
            "or another directory, read them and copy them across yourself. To write here anyway add "
            "--overwrite-live: each file replaced is first copied to <name>.bak-<YYYYmmdd-HHMMSS> next to it."
        )

    print(f"configuration: {directory}")
    try:
        policies = read_policies(directory)
    except ValueError as exc:
        cause = exc.__cause__
        return _error(f"{exc}" + (f": {cause}" if cause is not None else ""))
    schema_path = directory / "schema.yaml"
    try:
        schema_text = schema_path.read_bytes().decode("utf-8-sig")
        schema = validate_schema_yaml_text(schema_text)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _error(f"cannot use {schema_path}: {_describe(exc)}")
    view = doctor.read_schema_text(schema_text)

    try:
        chosen = choose_memory_keys(policies, args.memory_key)
    except ValueError as exc:
        return _error(str(exc))
    found = _dimension(args, policies, view, schema, chosen)
    if isinstance(found, str):
        return _error(found)
    key, column = found
    definition = schema.tables[key]
    table_name = bare_table_name(key)
    if is_personal_column(table_name, column):
        return _error(
            f"{key}.{column} looks like personal or contact data (by its name, or because {table_name} is a table of "
            "people); a scope is a small dimension such as a region, a channel or a kind. Nothing was read."
        )
    from database.datasources import default_datasource_name

    source = definition.datasource[0] if definition.datasource else default_datasource_name()
    qualifier = ".".join(effective_qualifier(key, definition.db_schema))
    print(f"dimension: {key}.{column} (data source {source!r}); memory key(s): {', '.join(chosen)}")

    reader = read_values or default_reader()
    try:
        raw = list(reader(source, qualifier, key, column, limit + 1))
    except Exception as exc:  # noqa: BLE001 - a warehouse that cannot be read is one error, reported without its text
        return _error(f"cannot read the values of {key}.{column}: {_describe(exc)}")
    if len(raw) > limit:
        return _error(
            f"{key}.{column} has more than {limit} distinct values: a scope must be a small dimension (a region, a "
            "channel, a kind). Choose another column or table, or raise --max-values if this really is the "
            f"dimension (at most {cfg.settings.max_rows_returned - 1})."
        )
    max_length = min(policies.memory.keys[name].max_length for name in chosen)
    kept = usable_values(raw, policies.session.default_scope.default_label, max_length)
    left_out = ", ".join(f"{count} {reason}" for reason, count in sorted(kept.dropped.items()))
    print(f"read: {kept.read} distinct value(s); kept: {len(kept.values)} option(s)" + (f"; left out: {left_out}" if left_out else ""))
    if args.show_values:
        shown = ", ".join(kept.values[:_SHOWN]) + (f", ... ({len(kept.values) - _SHOWN} more)" if len(kept.values) > _SHOWN else "")
        print(f"values: {shown}")
    if not kept.values:
        return _error(f"no usable value in {key}.{column}; nothing to draft")

    moved = args.table is not None or args.column is not None
    try:
        drafted = draft_policies(
            policies, kept.values,
            filter_key=key if args.table is not None else None,
            column=column if moved else None,
            memory_keys=chosen,
        )
    except ValueError as exc:
        return _error(f"cannot draft the files: {exc}")
    print(f"{MEMORY_FILE}: key(s) {', '.join(drafted.memory_keys)} -> {len(kept.values)} option(s)")
    print(f"{SESSION_FILE}: default_scope -> the default label and {len(kept.values)} option(s)")
    if drafted.changed_dimension:
        print(
            "  the dimension moved: field_name, default_label and clarification_prompt are unchanged; "
            "check that they still describe it"
        )
    if args.dry_run:
        print("\ndry run: nothing was written")
        return EXIT_OK

    targets = {args.output / MEMORY_FILE: drafted.memory_text, args.output / SESSION_FILE: drafted.session_text}
    existing = [path for path in targets if path.exists()]
    if existing and not (protected or args.force):
        names = ", ".join(path.name for path in existing)
        return _error(f"{args.output} already holds {names}: --force replaces them, or choose another --output; nothing was written")
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        if protected:
            for copy in back_up(existing, datetime.now().strftime("%Y%m%d-%H%M%S")):
                print(f"backed up: {copy}")
        for path, text in targets.items():
            _write_atomically(path, text)
    except OSError as exc:
        return _error(f"cannot write {args.output}: {_describe(exc)}")
    for path in targets:
        print(f"written: {path}")
    print(
        "\nRead both files, then copy them into the configuration directory yourself"
        + (" (they are already there)" if protected else "")
        + "; `python -m scripts.verify_deployment` then checks them."
    )
    return EXIT_OK


if __name__ == "__main__":
    use_utf8_console()
    sys.exit(main())
