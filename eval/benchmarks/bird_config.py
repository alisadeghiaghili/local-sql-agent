# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A ``project_config/`` for each benchmark database, drafted with the 6.13 tools.

For every ``db_id`` (and once for all of them together) the importer runs the
schema inspector (``python -m database.schema_inspector_cli --include-schemas
<db_id>``) through the application's own read-only login, so the draft lists
exactly what the application can query in that schema. The inspector writes
``schema.yaml``, ``entities.yaml``, ``aliases.yaml`` and ``relationships.yaml``;
this module completes the directory so it can be used as ``PROJECT_CONFIG_DIR``:

* ``system_prompt.md``, ``session_policy.yaml`` and ``memory_policy.yaml`` are
  copied from a base configuration (``project_config.example/`` by default; a
  deployment can pass its live ``project_config/`` to run the benchmark with
  its own prompt);
* ``business_rules.yaml``, ``examples.yaml``, ``metrics.yaml`` and
  ``retrieval_hints.yaml`` are written **empty**: the example files describe
  another domain, and a benchmark must not be helped by hand-written hints.

With ``descriptions`` the dataset's ``database_description/*.csv`` text is added
to each column's description in ``schema.yaml``. That models a curated
configuration; without it the configuration is the raw draft an operator gets
on the first day.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml

__all__ = [
    "BASE_COPIED_FILES",
    "NEUTRAL_FILES",
    "InspectFn",
    "apply_descriptions",
    "default_base_config",
    "draft_config",
    "inspector_runner",
    "write_neutral_files",
]

#: ``inspect(schemas, output_dir) -> exit code``; the inspector, or a stand-in in tests.
InspectFn = Callable[[Sequence[str], Path], int]

#: Copied from the base configuration when the draft lacks them.
BASE_COPIED_FILES: tuple[str, ...] = ("system_prompt.md", "session_policy.yaml", "memory_policy.yaml")

#: Written empty: hand-written help of a deployment that a benchmark must not get.
NEUTRAL_FILES: dict[str, str] = {
    "business_rules.yaml": "# Empty on purpose: the benchmark gets no hand-written rules.\nrules: {}\n",
    "examples.yaml": "# Empty on purpose: the benchmark gets no few-shot examples.\nexamples: []\n",
    "metrics.yaml": "# Empty on purpose: the benchmark gets no metric definitions.\nmetrics: {}\n",
    "retrieval_hints.yaml": (
        "# Empty on purpose: the benchmark gets no retrieval hints.\n"
        "fact_tables: []\nalways_include: {}\nfact_patterns: {}\n"
    ),
}


def default_base_config() -> Path:
    """``project_config.example/`` of this checkout."""
    return Path(__file__).resolve().parents[2] / "project_config.example"


def inspector_runner(datasource: str | None = None) -> InspectFn:
    """The schema inspector as an :data:`InspectFn`, reading through the application's login.

    Parameters
    ----------
    datasource:
        A data source of ``datasources.yaml`` (``--datasource``); omitted, the
        application's default source.
    """

    def run(schemas: Sequence[str], output_dir: Path) -> int:
        from database import schema_inspector_cli

        argv = ["--output-dir", str(output_dir), "--include-schemas", ",".join(schemas)]
        if datasource:
            argv += ["--datasource", datasource]
        try:
            return int(schema_inspector_cli.main(argv))
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 1

    return run


def apply_descriptions(
    schema_yaml: str, descriptions: Mapping[str, Mapping[str, Mapping[str, str]]]
) -> tuple[str, int]:
    """Add the dataset's column descriptions to a drafted ``schema.yaml``.

    Parameters
    ----------
    schema_yaml:
        The text the inspector wrote.
    descriptions:
        ``{schema: {table: {column: text}}}`` with lower-cased table and column
        names (:func:`eval.benchmarks.bird.read_descriptions` per schema).

    Returns
    -------
    tuple[str, int]
        The new text and the number of columns that gained a description. Each
        becomes ``"<drafted text> -- <dataset text>"``; tables and columns the
        dataset does not describe are untouched.

    Examples
    --------
    >>> text = '''tables:
    ...   frpm:
    ...     db_schema: ca
    ...     description: ca.frpm
    ...     columns:
    ...       CDSCode: BIGINT column; primary key
    ...       County Name: NVARCHAR column
    ... relationships: []
    ... '''
    >>> new, count = apply_descriptions(text, {"ca": {"frpm": {"cdscode": "unique id (text)", "county name": "county"}}})
    >>> count
    2
    >>> yaml.safe_load(new)["tables"]["frpm"]["columns"]["CDSCode"]
    'BIGINT column; primary key -- unique id (text)'
    """
    data = yaml.safe_load(schema_yaml) or {}
    changed = 0
    for key, entry in (data.get("tables") or {}).items():
        if not isinstance(entry, dict) or not isinstance(entry.get("columns"), dict):
            continue
        parts = [p.strip("[]") for p in str(key).split(".")]
        table = parts[-1].lower()
        schema = str(entry.get("db_schema") or (parts[-2] if len(parts) > 1 else ""))
        by_column = descriptions.get(schema, {}).get(table)
        if not by_column:
            continue
        for column, old in list(entry["columns"].items()):
            text = by_column.get(str(column).strip().lower())
            if text:
                entry["columns"][column] = f"{old} -- {text}"
                changed += 1
    header = (
        "# Column descriptions below were added from the benchmark's database_description files.\n"
    )
    dumped = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100000)
    return header + dumped, changed


def write_neutral_files(directory: Path, base_config: Path) -> list[str]:
    """Complete *directory* so it can be a ``PROJECT_CONFIG_DIR``.

    Copies :data:`BASE_COPIED_FILES` from *base_config* when missing and writes
    :data:`NEUTRAL_FILES`. Returns the names written, in order.

    Raises
    ------
    FileNotFoundError
        If a file to copy is not in *base_config*.
    """
    written: list[str] = []
    directory.mkdir(parents=True, exist_ok=True)
    for name in BASE_COPIED_FILES:
        target = directory / name
        if not target.exists():
            source = base_config / name
            if not source.is_file():
                raise FileNotFoundError(f"base configuration lacks {name}: {source}")
            shutil.copyfile(source, target)
            written.append(name)
    for name, content in NEUTRAL_FILES.items():
        (directory / name).write_text(content, encoding="utf-8")
        written.append(name)
    return written


def draft_config(
    directory: Path,
    schemas: Sequence[str],
    *,
    inspect: InspectFn,
    base_config: Path,
    descriptions: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
) -> dict[str, object]:
    """Draft one ``project_config/`` for *schemas* into *directory*.

    Runs *inspect*, optionally adds the dataset's descriptions to
    ``schema.yaml``, then completes the directory (:func:`write_neutral_files`).

    Returns
    -------
    dict[str, object]
        ``path``, ``schemas``, ``inspector_exit`` and, with descriptions,
        ``described_columns``. A non-zero ``inspector_exit`` means the draft is
        incomplete; the directory is then not completed.
    """
    result: dict[str, object] = {"path": str(directory), "schemas": list(schemas)}
    code = inspect(schemas, directory)
    result["inspector_exit"] = code
    if code != 0:
        return result
    if descriptions is not None:
        schema_file = directory / "schema.yaml"
        text, count = apply_descriptions(schema_file.read_text(encoding="utf-8"), descriptions)
        schema_file.write_text(text, encoding="utf-8")
        result["described_columns"] = count
    result["completed_with"] = write_neutral_files(directory, base_config)
    return result
