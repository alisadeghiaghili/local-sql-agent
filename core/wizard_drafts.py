# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Where the setup wizard writes, and how a new run is merged into an old one.

``setup_project.py`` produces *drafts*: aliases, rules and examples suggested
by a model, to be reviewed before they replace anything curated. This module
holds the parts of that rule which need no database and no model, so they can
be tested on their own:

* which directories the wizard refuses to write into without
  ``--overwrite-live`` (:func:`protected_directory`, :func:`refusal_message`),
  and the ``<name>.bak-<stamp>`` copies it makes first (:func:`back_up`);
* how the files of a second run are merged into those of the first, by key,
  without shrinking them (:func:`merge_mapping`, :func:`merge_synonyms`,
  :func:`merge_examples`, :func:`merge_relationships`), with the counts the
  summary prints (:class:`MergeStats`).

Merging never removes an entry. An entry the new run produced replaces the old
one with the same key; every other old entry stays as it was.

Public API
----------
DEFAULT_DRAFT_DIR
    Directory name the wizard writes to by default.
protected_directory(output_dir, live_dir, repo_root) -> str | None
refusal_message(output_dir, label) -> str
back_up(paths, stamp) -> list[Path]
MergeStats
merge_mapping(existing, new, rename) -> (dict, MergeStats)
merge_synonyms(existing, new) -> (dict, MergeStats)
merge_examples(existing, new) -> (list, MergeStats)
merge_relationships(existing, new, rename) -> (list, MergeStats)
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_DRAFT_DIR",
    "EXAMPLE_DIR_NAME",
    "MergeStats",
    "back_up",
    "merge_examples",
    "merge_mapping",
    "merge_relationships",
    "merge_synonyms",
    "protected_directory",
    "refusal_message",
]

#: Directory the wizard writes to unless ``--output`` says otherwise. It is
#: git-ignored, like the drafts of ``database/schema_inspector_cli.py``.
DEFAULT_DRAFT_DIR = "project_config_draft"

#: The committed template directory; never a place for drafts built from a
#: real warehouse.
EXAMPLE_DIR_NAME = "project_config.example"


# ---------------------------------------------------------------------------
# Protected directories and backups
# ---------------------------------------------------------------------------

def protected_directory(output_dir: Path, live_dir: Path, repo_root: Path) -> str | None:
    """Name the protected directory *output_dir* is, or ``None`` when it is not one.

    The live configuration directory and ``project_config.example/`` are
    protected. Paths are compared after ``resolve()``, so a relative spelling,
    a trailing slash or a symbolic link does not get around the check.

    Args:
        output_dir: Where the wizard was asked to write.
        live_dir: The directory the application reads (``PROJECT_CONFIG_DIR``,
            resolved against *repo_root* when relative).
        repo_root: The repository root, where ``project_config.example/`` lives.

    Returns:
        ``"live"`` for the live directory, ``"example"`` for the template
        directory, else ``None``.

    Raises:
        Nothing for ordinary paths; ``resolve()`` is not strict.

    Examples:
        >>> root = Path("/srv/app")
        >>> protected_directory(Path("/srv/app/project_config"), root / "project_config", root)
        'live'
        >>> protected_directory(Path("/srv/app/project_config.example/"), root / "project_config", root)
        'example'
        >>> protected_directory(Path("/srv/app/project_config_draft"), root / "project_config", root) is None
        True
        >>> protected_directory(Path("/srv/app/x/../project_config"), root / "project_config", root)
        'live'
    """
    resolved = output_dir.resolve()
    if resolved == live_dir.resolve():
        return "live"
    if resolved in {(repo_root / EXAMPLE_DIR_NAME).resolve(), Path(EXAMPLE_DIR_NAME).resolve()}:
        return "example"
    return None


def refusal_message(output_dir: Path, label: str) -> str:
    """The one-paragraph message printed when the wizard refuses to write.

    Args:
        output_dir: The directory that was refused.
        label: What :func:`protected_directory` returned for it.

    Returns:
        The message, ending with how to proceed.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> text = refusal_message(Path("project_config"), "live")
        >>> "--overwrite-live" in text and "project_config_draft" in text
        True
        >>> "\\n" in refusal_message(Path("x"), "example")
        False
    """
    what = (
        "the live configuration directory the application reads"
        if label == "live"
        else f"the committed template directory ({EXAMPLE_DIR_NAME}/), which git tracks"
    )
    return (
        f"Refusing to write into {output_dir}: it is {what}. The wizard writes drafts of "
        "aliases, rules and examples suggested by a model, and they must be reviewed before "
        "they replace curated files. Run it with the default output "
        f"({DEFAULT_DRAFT_DIR}/) or another scratch directory (--output), review the files "
        "and copy the ones you accept across yourself. To write here anyway, add "
        "--overwrite-live: every file the wizard replaces is first copied to "
        "<name>.bak-<YYYYmmdd-HHMMSS> next to it, and the copies are listed."
    )


def _backup_path(path: Path, stamp: str) -> Path:
    """``<name>.bak-<stamp>`` next to *path*, made unique if that name is taken."""
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
    return candidate


def back_up(paths: Iterable[Path], stamp: str) -> list[Path]:
    """Copy each existing file in *paths* to ``<name>.bak-<stamp>`` beside it.

    Args:
        paths: Files about to be replaced. One that does not exist is skipped.
        stamp: The timestamp part of the name, ``YYYYmmdd-HHMMSS``.

    Returns:
        The backup files created, in the order of *paths*.

    Raises:
        OSError: When a copy cannot be made; nothing is replaced then.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     f = Path(tmp, "a.yaml"); _ = f.write_text("x")
        ...     made = back_up([f, Path(tmp, "missing.yaml")], "20260101-000000")
        ...     [p.name for p in made], made[0].read_text()
        (['a.yaml.bak-20260101-000000'], 'x')
    """
    made: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        target = _backup_path(path, stamp)
        shutil.copy2(path, target)
        made.append(target)
    return made


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MergeStats:
    """What a merge did to one file.

    Attributes:
        kept: Entries of the old file the new run did not touch.
        added: Entries the old file did not have.
        replaced: Entries the new run produced for a key the old file had.
        rekeyed: ``(old key, new key)`` for old entries whose key changed
            because a bare table name is now shared by several schemas. They
            count under *kept* or *replaced* as well.
    """

    kept: int = 0
    added: int = 0
    replaced: int = 0
    rekeyed: tuple[tuple[str, str], ...] = ()

    def __add__(self, other: "MergeStats") -> "MergeStats":
        """Counts added up, for a file that holds two merged sections.

        Examples:
            >>> (MergeStats(1, 2, 3) + MergeStats(4, 5, 6)).describe()
            'kept 5, added 7, replaced 9'
        """
        return MergeStats(
            self.kept + other.kept,
            self.added + other.added,
            self.replaced + other.replaced,
            self.rekeyed + other.rekeyed,
        )

    def describe(self) -> str:
        """``"kept 2, added 1, replaced 0"``, plus the re-keyed count when there is one.

        Examples:
            >>> MergeStats(2, 1, 0).describe()
            'kept 2, added 1, replaced 0'
            >>> MergeStats(0, 1, 0, (("Trade", "sales.Trade"),)).describe()
            'kept 0, added 1, replaced 0, re-keyed 1'
        """
        text = f"kept {self.kept}, added {self.added}, replaced {self.replaced}"
        if self.rekeyed:
            text += f", re-keyed {len(self.rekeyed)}"
        return text


def _fold(key: str) -> str:
    return key.casefold()


def merge_mapping(
    existing: Mapping[str, Any],
    new: Mapping[str, Any],
    rename: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], MergeStats]:
    """Merge *new* into *existing* by key; keys compare case-insensitively.

    Old entries come first, in their order, under their (possibly renamed)
    key; an entry of *new* with a key already present replaces that value in
    place; the others are appended. Nothing is removed.

    Args:
        existing: The old file's mapping.
        new: The entries this run produced.
        rename: ``{old key: new key}`` for old entries to re-key before the
            merge (a bare name that must now be qualified).

    Returns:
        ``(merged, stats)``.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> merged, stats = merge_mapping({"A": 1, "B": 2}, {"B": 3, "C": 4})
        >>> merged, stats.describe()
        ({'A': 1, 'B': 3, 'C': 4}, 'kept 1, added 1, replaced 1')
        >>> merged, stats = merge_mapping({"Trade": 1}, {"ref.Trade": 2}, {"Trade": "sales.Trade"})
        >>> merged, stats.rekeyed
        ({'sales.Trade': 1, 'ref.Trade': 2}, (('Trade', 'sales.Trade'),))
        >>> merge_mapping({"a": 1}, {"A": 2})[0]
        {'A': 2}
    """
    lowered_rename = {_fold(k): v for k, v in (rename or {}).items()}
    merged: dict[str, Any] = {}
    index: dict[str, str] = {}
    rekeyed: list[tuple[str, str]] = []
    for key, value in existing.items():
        target = lowered_rename.get(_fold(key), key)
        if target != key:
            rekeyed.append((key, target))
        index[_fold(target)] = target
        merged[target] = value
    untouched = set(merged)
    added = replaced = 0
    for key, value in new.items():
        present = index.get(_fold(key))
        if present is None:
            index[_fold(key)] = key
            merged[key] = value
            added += 1
            continue
        if present == key:
            merged[key] = value
        else:
            # Same table, spelled the way this run spells it; keep its place.
            merged = {(key if k == present else k): (value if k == present else v) for k, v in merged.items()}
            index[_fold(key)] = key
        untouched.discard(present)
        replaced += 1
    return merged, MergeStats(len(untouched), added, replaced, tuple(rekeyed))


def merge_synonyms(
    existing: Mapping[str, Sequence[str]],
    new: Mapping[str, Sequence[str]],
) -> tuple[dict[str, list[str]], MergeStats]:
    """Merge the ``synonyms`` of two runs: a word that is in both keeps both token lists.

    Synonyms are keyed by a word, not by a table, so there is nothing for a
    run to replace: the tokens of the old and the new entry are united (old
    first). Such an entry counts as *replaced* when it gained a token.

    Args:
        existing: The old file's ``synonyms``.
        new: This run's ``synonyms``.

    Returns:
        ``(merged, stats)``.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> merged, stats = merge_synonyms({"a": ["x"], "b": ["y"]}, {"a": ["x", "z"], "c": ["w"]})
        >>> merged, stats.describe()
        ({'a': ['x', 'z'], 'b': ['y'], 'c': ['w']}, 'kept 1, added 1, replaced 1')
    """
    merged: dict[str, list[str]] = {k: list(v) for k, v in existing.items()}
    index = {_fold(k): k for k in merged}
    untouched = set(merged)
    added = replaced = 0
    for word, tokens in new.items():
        present = index.get(_fold(word))
        if present is None:
            merged[word] = list(tokens)
            index[_fold(word)] = word
            added += 1
            continue
        gained = [t for t in tokens if t not in merged[present]]
        if gained:
            merged[present].extend(gained)
            untouched.discard(present)
            replaced += 1
    return merged, MergeStats(len(untouched), added, replaced)


def _question_key(entry: Mapping[str, Any]) -> str:
    return " ".join(str(entry.get("question", "")).split()).casefold()


def merge_examples(
    existing: Sequence[Mapping[str, Any]],
    new: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], MergeStats]:
    """Merge example lists, de-duplicated by question text.

    Questions compare case-insensitively with runs of whitespace collapsed.
    A new example whose question the old list has replaces that example in
    place; the others are appended. An example without a question or an SQL
    text is not an example and is ignored on the new side. A question
    repeated within *new* counts once (the first).

    Args:
        existing: The old file's ``examples``.
        new: This run's examples.

    Returns:
        ``(merged, stats)``.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> old = [{"question": "How many?", "sql": "S1"}, {"question": "Top 5", "sql": "S2"}]
        >>> fresh = [{"question": "how  many?", "sql": "S3"}, {"question": "New", "sql": "S4"}]
        >>> merged, stats = merge_examples(old, fresh)
        >>> [e["sql"] for e in merged], stats.describe()
        (['S3', 'S2', 'S4'], 'kept 1, added 1, replaced 1')
    """
    merged: list[dict[str, Any]] = [dict(e) for e in existing]
    index: dict[str, int] = {}
    for position, entry in enumerate(merged):
        index.setdefault(_question_key(entry), position)
    untouched = set(range(len(merged)))
    seen: set[str] = set()
    added = replaced = 0
    for entry in new:
        if not (entry.get("question") and entry.get("sql")):
            continue
        key = _question_key(entry)
        if key in seen:
            continue
        seen.add(key)
        if key in index:
            merged[index[key]] = dict(entry)
            untouched.discard(index[key])
            replaced += 1
        else:
            index[key] = len(merged)
            merged.append(dict(entry))
            added += 1
    return merged, MergeStats(len(untouched), added, replaced)


def _endpoints(entry: Mapping[str, Any], rename: Mapping[str, str]) -> frozenset[tuple[str, str]] | None:
    """The two ends of a relationship entry as ``(table, column)``, lower-cased; ``None`` if incomplete."""
    ends: list[tuple[str, str]] = []
    for side in ("from", "to"):
        table = str(entry.get(f"{side}_table") or "").strip()
        column = str(entry.get(f"{side}_column") or "").strip()
        if not (table and column):
            return None
        ends.append((rename.get(_fold(table), table).casefold(), column.casefold()))
    return frozenset(ends)


def merge_relationships(
    existing: Sequence[Mapping[str, Any]],
    new: Sequence[Mapping[str, Any]],
    rename: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], MergeStats]:
    """Merge relationship lists, de-duplicated by their endpoints.

    Two entries are the same relationship when they join the same two
    ``(table, column)`` ends, in either direction. A new entry replaces the
    old one in place; the others are appended. An entry marked as inferred
    (it has a ``basis``) never replaces one that is not: a person has
    reviewed that one, or the database declares it.

    Args:
        existing: The old file's ``relationships``.
        new: This run's relationships.
        rename: ``{old table key: new key}`` applied to the old entries'
            ``from_table`` and ``to_table`` first.

    Returns:
        ``(merged, stats)``.

    Raises:
        Nothing. This function never raises.

    Examples:
        >>> old = [{"from_table": "A", "from_column": "b_id", "to_table": "B", "to_column": "id", "join_hint": "J1"}]
        >>> fresh = [{"from_table": "B", "from_column": "ID", "to_table": "a", "to_column": "B_ID", "join_hint": "J2"},
        ...          {"from_table": "A", "from_column": "c_id", "to_table": "C", "to_column": "id", "join_hint": "J3"}]
        >>> merged, stats = merge_relationships(old, fresh)
        >>> [e["join_hint"] for e in merged], stats.describe()
        (['J2', 'J3'], 'kept 0, added 1, replaced 1')
        >>> guess = [dict(fresh[0], join_hint="J4", basis="naming convention")]
        >>> merge_relationships(old, guess)[0][0]["join_hint"]
        'J1'
    """
    lowered = {_fold(k): v for k, v in (rename or {}).items()}
    merged: list[dict[str, Any]] = []
    rekeyed: list[tuple[str, str]] = []
    for entry in existing:
        item = dict(entry)
        for side in ("from_table", "to_table"):
            old = str(item.get(side) or "")
            target = lowered.get(_fold(old))
            if target and target != old:
                item[side] = target
                if (old, target) not in rekeyed:
                    rekeyed.append((old, target))
        merged.append(item)
    index: dict[frozenset[tuple[str, str]], int] = {}
    for position, item in enumerate(merged):
        ends = _endpoints(item, {})
        if ends is not None:
            index.setdefault(ends, position)
    untouched = set(range(len(merged)))
    added = replaced = 0
    for entry in new:
        ends = _endpoints(entry, {})
        position = index.get(ends) if ends is not None else None
        if position is None:
            if ends is not None:
                index[ends] = len(merged)
            merged.append(dict(entry))
            added += 1
        elif entry.get("basis") and not merged[position].get("basis"):
            continue
        else:
            merged[position] = dict(entry)
            untouched.discard(position)
            replaced += 1
    return merged, MergeStats(len(untouched), added, replaced, tuple(rekeyed))
