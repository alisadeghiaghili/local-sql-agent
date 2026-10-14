# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Compare two evaluation rounds case by case (``python -m eval.cli compare``).

A round is one ``eval.cli run --live --reference live`` run. Two forms of it
can be compared, read by ``case_id``:

* a **share bundle** (``--share``, :mod:`eval.share`), or
* a plain **report** (``--out``), which carries the same status per case and,
  since the diagnosis was added, the same mismatch class.

The comparison says which cases were **fixed** (failed, now pass),
**regressed** (passed, now fail), are **still failing**, were **added** to or
**removed** from the golden set, how the accuracy moved overall and per tag,
and how the distribution of mismatch classes shifted. It prints case ids,
tags and counts only: no question, no SQL, no value.

A comparison can be limited to a held-out half (``--split test``): the ids
come from a file written by ``eval.cli split``. The accuracy of the ``test``
half is the one number a fix cannot have been tuned to.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eval.diagnosis import mismatch_class_for
from eval.models import MISMATCH_CLASSES
from eval.share import FORMAT_VERSION

__all__ = [
    "Comparison",
    "PipelineMismatch",
    "RunCase",
    "LoadedRun",
    "compare_runs",
    "load_run",
    "read_case_ids",
    "render_comparison",
]

#: Printed in place of a class for a case a report cannot classify.
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class RunCase:
    """What the comparison needs to know about one case of one round."""

    case_id: str
    tags: tuple[str, ...]
    passed: bool
    status: str
    mismatch_class: str
    datasource: str | None = None


@dataclass(frozen=True, slots=True)
class LoadedRun:
    """A round read from disk: its cases by id and a few header facts."""

    name: str
    kind: str
    cases: dict[str, RunCase]
    meta: dict[str, Any] = field(default_factory=dict)


def _case_from_record(record: Mapping[str, Any]) -> RunCase:
    status = str(record.get("status", ""))
    passed = bool(record.get("passed", status == "pass"))
    diagnosis = record.get("diagnosis") or {}
    diagnosed = diagnosis.get("mismatch_class") if isinstance(diagnosis, Mapping) else None
    if diagnosed is None:  # a case reduced by --share-only-ids carries the class at top level
        diagnosed = record.get("mismatch_class")
    name = mismatch_class_for(status, passed, diagnosed) or UNCLASSIFIED
    return RunCase(
        case_id=str(record["case_id"]),
        tags=tuple(str(t) for t in record.get("tags") or ()),
        passed=passed,
        status=status,
        mismatch_class=name,
        datasource=record.get("expected_datasource"),
    )


def load_run(path: str | Path) -> LoadedRun:
    """Read a share bundle or an ``--out`` report.

    Raises
    ------
    ValueError
        If the file is not JSON, is neither shape, has a newer
        ``format_version`` than this tool reads, or repeats a ``case_id``.
    FileNotFoundError
        If *path* does not exist.

    Examples
    --------
    >>> import tempfile
    >>> d = Path(tempfile.mkdtemp())
    >>> _ = (d / "r.json").write_text(json.dumps({"results": [
    ...     {"case_id": "a", "tags": ["t"], "status": "pass", "passed": True}]}), encoding="utf-8")
    >>> run = load_run(d / "r.json")
    >>> run.kind, run.cases["a"].mismatch_class
    ('report', 'match')
    """
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p.name}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{p.name}: expected a JSON object (a share bundle or a report)")

    if isinstance(data.get("cases"), list) and "format_version" in data:
        version = data["format_version"]
        if not isinstance(version, int) or version > FORMAT_VERSION:
            raise ValueError(
                f"{p.name}: format_version {version!r} is newer than this tool reads "
                f"({FORMAT_VERSION}); update the application"
            )
        records, kind = data["cases"], "bundle"
        golden = data.get("golden") or {}
        meta = {
            "golden_sha256": golden.get("sha256"),
            "reference": (data.get("run") or {}).get("reference"),
            "pipeline": (data.get("run") or {}).get("pipeline"),
            "app_version": data.get("app_version"),
            "prefix_version": data.get("prefix_version"),
            "model": data.get("model"),
        }
    elif isinstance(data.get("results"), list):
        records, kind = data["results"], "report"
        meta = {
            "reference": data.get("reference"),
            "mode": data.get("mode"),
            "pipeline": data.get("pipeline"),
        }
    else:
        raise ValueError(
            f"{p.name}: neither a share bundle (format_version + cases) nor a report (results)"
        )

    cases: dict[str, RunCase] = {}
    for record in records:
        try:
            case = _case_from_record(record)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"{p.name}: a case record is malformed ({type(exc).__name__})") from exc
        if case.case_id in cases:
            raise ValueError(f"{p.name}: case id {case.case_id!r} appears twice")
        cases[case.case_id] = case
    return LoadedRun(name=p.name, kind=kind, cases=cases, meta=meta)


def read_case_ids(path: str | Path, split: str | None = None) -> list[str]:
    """Read a file of case ids: a text file, a JSON list, or a JSON object.

    * Text: one id per line; blank lines and lines starting with ``#`` are
      skipped (what ``eval.cli split`` writes as ``dev_ids.txt`` /
      ``test_ids.txt``).
    * JSON list: the ids.
    * JSON object: the list under *split* (``dev``/``test``, or
      ``dev_ids``/``test_ids``).

    Raises
    ------
    ValueError
        If a JSON object has no list for *split*, or the file holds no id.

    Examples
    --------
    >>> import tempfile
    >>> d = Path(tempfile.mkdtemp())
    >>> _ = (d / "ids.txt").write_text("# held out\\na\\r\\n\\nb\\n", encoding="utf-8")
    >>> read_case_ids(d / "ids.txt")
    ['a', 'b']
    >>> _ = (d / "ids.json").write_text('{"dev": ["x"], "test": ["y", "z"]}', encoding="utf-8")
    >>> read_case_ids(d / "ids.json", "test")
    ['y', 'z']
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    ids: list[str]
    if stripped.startswith(("[", "{")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p.name}: not valid JSON: {exc}") from exc
        if isinstance(data, dict):
            if split is None:
                raise ValueError(f"{p.name}: a JSON object needs --split to pick a list")
            chosen = next((data[k] for k in (split, f"{split}_ids") if k in data), None)
            if not isinstance(chosen, list):
                raise ValueError(f"{p.name}: no list of ids under {split!r} or '{split}_ids'")
            data = chosen
        if not isinstance(data, list):
            raise ValueError(f"{p.name}: expected a list of case ids")
        ids = [str(item) for item in data]
    else:
        ids = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    if not ids:
        raise ValueError(f"{p.name}: no case ids found")
    return ids


def _accuracy(cases: Iterable[RunCase]) -> tuple[int, int]:
    items = list(cases)
    return sum(1 for c in items if c.passed), len(items)


def _pct(passed: int, total: int) -> float:
    return 100.0 * passed / total if total else 0.0


@dataclass(frozen=True, slots=True)
class Comparison:
    """The difference between an old and a new round.

    All lists hold case ids, sorted. ``overall`` and ``common`` are
    ``(passed, total)`` pairs; ``per_tag`` maps a tag to ``{"old": (p, t),
    "new": (p, t)}``; ``classes`` maps a mismatch class to ``(old, new)``
    counts.
    """

    old_name: str
    new_name: str
    fixed: list[str]
    regressed: list[str]
    still_failing: list[str]
    still_passing: int
    added: list[str]
    added_passed: list[str]
    removed: list[str]
    old_overall: tuple[int, int]
    new_overall: tuple[int, int]
    old_common: tuple[int, int]
    new_common: tuple[int, int]
    per_tag: dict[str, dict[str, tuple[int, int]]]
    classes: dict[str, tuple[int, int]]
    notes: list[str] = field(default_factory=list)
    split: str | None = None

    @property
    def accuracy_delta(self) -> float:
        """New minus old overall accuracy, in percentage points."""
        return _pct(*self.new_overall) - _pct(*self.old_overall)

    @property
    def common_delta(self) -> float:
        """New minus old accuracy on the cases present in both rounds."""
        return _pct(*self.new_common) - _pct(*self.old_common)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-friendly form (ids, counts and percentages only)."""
        return {
            "old": self.old_name,
            "new": self.new_name,
            "split": self.split,
            "fixed": self.fixed,
            "regressed": self.regressed,
            "still_failing": self.still_failing,
            "still_passing": self.still_passing,
            "added": self.added,
            "added_passed": self.added_passed,
            "removed": self.removed,
            "accuracy": {
                "old": {"passed": self.old_overall[0], "total": self.old_overall[1],
                        "pct": round(_pct(*self.old_overall), 2)},
                "new": {"passed": self.new_overall[0], "total": self.new_overall[1],
                        "pct": round(_pct(*self.new_overall), 2)},
                "delta_points": round(self.accuracy_delta, 2),
                "common": {
                    "old_passed": self.old_common[0], "new_passed": self.new_common[0],
                    "total": self.old_common[1], "delta_points": round(self.common_delta, 2),
                },
            },
            "per_tag": {
                tag: {
                    "old": {"passed": v["old"][0], "total": v["old"][1]},
                    "new": {"passed": v["new"][0], "total": v["new"][1]},
                    "delta_points": round(_pct(*v["new"]) - _pct(*v["old"]), 2),
                }
                for tag, v in self.per_tag.items()
            },
            "mismatch_classes": {
                name: {"old": o, "new": n, "delta": n - o}
                for name, (o, n) in self.classes.items()
            },
            "notes": self.notes,
        }


class PipelineMismatch(ValueError):
    """Two rounds were generated by different pipelines and are not comparable as is."""


def compare_runs(
    old: LoadedRun,
    new: LoadedRun,
    *,
    only_ids: Collection[str] | None = None,
    split: str | None = None,
    allow_mixed: bool = False,
) -> Comparison:
    """Compare *old* with *new* by ``case_id``.

    Parameters
    ----------
    old, new:
        The two rounds, from :func:`load_run`.
    only_ids:
        When given, only these ids take part (a held-out half).
    split:
        The half's name, recorded in the result.
    allow_mixed:
        Compare rounds whose ``run.pipeline`` differs (``production`` against
        ``single_shot``). Refused by default: the production pipeline
        corrects its own mistakes, so the difference between the two is the
        pipeline, not a fix. With it, the comparison carries a note.

    Raises
    ------
    PipelineMismatch
        If both rounds name a pipeline, the names differ and *allow_mixed*
        is false.

    Examples
    --------
    >>> def run(name, passed):
    ...     return LoadedRun(name, "bundle", {
    ...         i: RunCase(i, ("t",), ok, "pass" if ok else "fingerprint_mismatch",
    ...                    "match" if ok else "actual_empty") for i, ok in passed.items()})
    >>> c = compare_runs(run("old", {"a": False, "b": True, "c": False}),
    ...                  run("new", {"a": True, "b": False, "c": False, "d": True}))
    >>> (c.fixed, c.regressed, c.still_failing, c.added)
    (['a'], ['b'], ['c'], ['d'])
    """
    old_pipeline, new_pipeline = old.meta.get("pipeline"), new.meta.get("pipeline")
    mixed = bool(old_pipeline and new_pipeline and old_pipeline != new_pipeline)
    if mixed and not allow_mixed:
        raise PipelineMismatch(
            f"{old.name} was generated by the {old_pipeline!r} pipeline and {new.name} by "
            f"{new_pipeline!r}; the production pipeline corrects its own mistakes, so the "
            "difference would be the pipeline and not a fix. Re-run one of them with the "
            "same setting (--single-shot or not), or pass --allow-mixed to compare anyway."
        )
    keep = None if only_ids is None else set(only_ids)
    old_cases = {k: v for k, v in old.cases.items() if keep is None or k in keep}
    new_cases = {k: v for k, v in new.cases.items() if keep is None or k in keep}

    common = sorted(set(old_cases) & set(new_cases))
    fixed = [i for i in common if new_cases[i].passed and not old_cases[i].passed]
    regressed = [i for i in common if old_cases[i].passed and not new_cases[i].passed]
    still_failing = [i for i in common if not old_cases[i].passed and not new_cases[i].passed]
    still_passing = sum(1 for i in common if old_cases[i].passed and new_cases[i].passed)
    added = sorted(set(new_cases) - set(old_cases))
    removed = sorted(set(old_cases) - set(new_cases))

    def tag_counts(cases: Mapping[str, RunCase]) -> dict[str, tuple[int, int]]:
        totals: Counter[str] = Counter()
        passes: Counter[str] = Counter()
        for case in cases.values():
            for tag in case.tags:
                totals[tag] += 1
                passes[tag] += 1 if case.passed else 0
        return {tag: (passes[tag], totals[tag]) for tag in totals}

    old_tags, new_tags = tag_counts(old_cases), tag_counts(new_cases)
    per_tag = {
        tag: {"old": old_tags.get(tag, (0, 0)), "new": new_tags.get(tag, (0, 0))}
        for tag in sorted(set(old_tags) | set(new_tags))
    }

    old_classes = Counter(c.mismatch_class for c in old_cases.values())
    new_classes = Counter(c.mismatch_class for c in new_cases.values())
    names = [n for n in MISMATCH_CLASSES] + sorted(
        (set(old_classes) | set(new_classes)) - set(MISMATCH_CLASSES)
    )
    classes = {n: (old_classes.get(n, 0), new_classes.get(n, 0)) for n in names}

    notes: list[str] = []
    if mixed:
        notes.append(
            f"MIXED PIPELINES ({old_pipeline!r} against {new_pipeline!r}, --allow-mixed): "
            "the deltas include the effect of the pipeline"
        )
    if keep is not None:
        outside = sorted(keep - set(old.cases) - set(new.cases))
        if outside:
            notes.append(f"{len(outside)} id(s) of the split file are in neither round")
    og, ng = old.meta.get("golden_sha256"), new.meta.get("golden_sha256")
    if og and ng and og != ng:
        notes.append("the golden file differs between the rounds (different content hash)")
    orf, nrf = old.meta.get("reference"), new.meta.get("reference")
    if orf and nrf and orf != nrf:
        notes.append(f"the rounds were judged differently (reference {orf!r} vs {nrf!r})")
    if added or removed:
        notes.append(
            f"{len(added)} case(s) added and {len(removed)} removed: overall accuracy covers "
            "different cases; read the common-case line"
        )

    return Comparison(
        old_name=old.name, new_name=new.name,
        fixed=fixed, regressed=regressed, still_failing=still_failing,
        still_passing=still_passing,
        added=added, added_passed=[i for i in added if new_cases[i].passed],
        removed=removed,
        old_overall=_accuracy(old_cases.values()),
        new_overall=_accuracy(new_cases.values()),
        old_common=_accuracy(old_cases[i] for i in common),
        new_common=_accuracy(new_cases[i] for i in common),
        per_tag=per_tag, classes=classes, notes=notes, split=split,
    )


def _ids_line(label: str, ids: list[str], limit: int = 40) -> str:
    shown = ", ".join(ids[:limit])
    more = f" ... (+{len(ids) - limit} more)" if len(ids) > limit else ""
    return f"{label} ({len(ids)}): {shown}{more}" if ids else f"{label} (0)"


def render_comparison(c: Comparison) -> str:
    """Render *c* as text: ids, tags and counts only.

    Examples
    --------
    >>> c = compare_runs(
    ...     LoadedRun("o.json", "bundle", {"a": RunCase("a", ("t",), False, "x", "actual_empty")}),
    ...     LoadedRun("n.json", "bundle", {"a": RunCase("a", ("t",), True, "pass", "match")}))
    >>> "Fixed (1): a" in render_comparison(c)
    True
    """
    old_p, old_t = c.old_overall
    new_p, new_t = c.new_overall
    lines = [f"Comparing {c.old_name} (old) with {c.new_name} (new)"]
    if c.split:
        lines.append(f"Limited to the {c.split} split.")
    lines.append(
        f"Execution accuracy: {_pct(old_p, old_t):.2f}% ({old_p}/{old_t}) -> "
        f"{_pct(new_p, new_t):.2f}% ({new_p}/{new_t})  [{c.accuracy_delta:+.2f} points]"
    )
    cp_old, cp_total = c.old_common
    cp_new = c.new_common[0]
    lines.append(
        f"On the {cp_total} cases in both: {_pct(cp_old, cp_total):.2f}% -> "
        f"{_pct(cp_new, cp_total):.2f}%  [{c.common_delta:+.2f} points]"
    )
    lines.append("")
    lines.append(_ids_line("Fixed", c.fixed))
    lines.append(_ids_line("Regressed", c.regressed))
    lines.append(_ids_line("Still failing", c.still_failing))
    lines.append(f"Still passing ({c.still_passing})")
    lines.append(_ids_line("Added", c.added) + (f", {len(c.added_passed)} passing" if c.added else ""))
    lines.append(_ids_line("Removed", c.removed))
    lines.append("")
    lines.append("Per-tag accuracy (old -> new):")
    if c.per_tag:
        for tag, v in c.per_tag.items():
            o, n = v["old"], v["new"]
            lines.append(
                f"  {tag:<24s} {o[0]:>3d}/{o[1]:<3d} ({_pct(*o):5.1f}%) -> "
                f"{n[0]:>3d}/{n[1]:<3d} ({_pct(*n):5.1f}%)  [{_pct(*n) - _pct(*o):+.1f}]"
            )
    else:
        lines.append("  (no tagged cases)")
    lines.append("")
    lines.append("Mismatch classes (old -> new):")
    shown = False
    for name, (o, n) in c.classes.items():
        if o or n:
            shown = True
            lines.append(f"  {name:<28s} {o:>4d} -> {n:<4d} [{n - o:+d}]")
    if not shown:
        lines.append("  (none)")
    if c.notes:
        lines.append("")
        lines.extend(f"NOTE: {note}" for note in c.notes)
    return "\n".join(lines)
