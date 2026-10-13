# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Harvest candidate golden cases from real usage -- on the server, without
letting the audit log (which holds real user questions) leave it.

Usage (from the repo root, on the machine that produced the log)::

    python scripts/harvest_golden.py
    python scripts/harvest_golden.py --n 150 --out eval_data/candidates.jsonl
    python scripts/harvest_golden.py "logs/audit_log.jsonl*" --force
    python scripts/harvest_golden.py --include-examples      # opt in, see below

It reads the audit log(s) the same way ``scripts/analyze_audit_log.py``
does (the active file plus every rotated ``.1``, ``.2``, ... backup, or the
paths/globs given), turns the records into :class:`~eval.models.GoldenCase`
candidates and writes a stratified sample of ``--n`` of them (default 150)
to ``eval_data/candidates.jsonl``. Nothing in the file is a verdict: every
case has ``status = "pending_review"``, and the model's own SQL is only a
*proposed* ``expected_sql``. The next steps are
``scripts/golden_sheet.py export`` (a spreadsheet for the analysts),
``import`` and ``python -m eval.cli verify --accept``.

Privacy -- the same two modes as ``analyze_audit_log.py``
-------------------------------------------------------------
The candidates file necessarily contains real question text, so it is
written only into ``eval_data/`` (git-ignored, server-only; the file is
created owner-only) and what this script **prints** is counts: records
read, how many were usable, why the rest were dropped, how the sample is
spread over data sources, outcomes and languages. No question, no SQL, no
error message appears on the terminal. ``--include-examples`` is the
opt-in escape hatch: it additionally prints a few verbatim candidates, and
its output says so on its first line, so a transcript of it is never
mistaken for the safe default. Row data is never read (an audit record has
none) and never written (a ``pending_review`` case may not carry any). The
case notes name the source record (``request_id`` and timestamp) so a
reviewer can find it in the log, and never the user (``principal_id`` is
not copied).

How candidates are chosen
-------------------------
* **Facts only.** Tags are derived from the record: ``lang:fa`` /
  ``lang:en`` / ``lang:mixed`` / ``lang:other`` from the script of the
  question, ``source:<name>`` from the record's ``datasource`` (or the
  source routing's choice), ``outcome:success`` / ``outcome:empty`` /
  ``outcome:out_of_scope`` / ``outcome:error`` (plus ``error:<code>``).
* **Normalised, de-duplicated.** Two questions that differ only in digit
  script, Arabic vs Persian letters, ZWNJ, whitespace or ASCII case are one
  question (:func:`core.persian.normalize_for_matching`); the most recent
  record wins. The stored question is the user's own wording (whitespace
  collapsed), because the pipeline under test should see what users type.
  ``--exclude`` drops questions already in a golden set.
* **Not about the question.** Records that failed for transport reasons
  (model/database unavailable, timeout, overload), records without a
  question, and records from stub/test backends (``mock:``, ``:test``) say
  nothing about correctness and are dropped (counted in the summary).
* **Stratified sample.** Strata are (data source, outcome, language).
  Each stratum gets one slot, and the rest are shared in proportion to the
  strata's size (largest remainder), so the sample follows the traffic mix
  while rare sources, error kinds and languages are still represented.
  Within a stratum the choice is random but seeded (``--seed``), so a
  rerun on the same log gives the same file.
* **Proposals, not answers.** A successful record proposes its
  ``generated_sql`` (and its ``datasource``); an out-of-scope record is
  proposed as ``expect = "out_of_scope"``; any other failure proposes
  nothing -- a reviewer supplies the SQL.

Confirmed answers come first (on by default, ``--no-confirmations`` opts out)
-------------------------------------------------------------------------------
Analysts confirm a correct answer with one click ("پاسخ درست است",
``POST /v2/sessions/{sid}/turns/{tid}/confirmation``, stored by
:mod:`appdb.confirmations`). Each confirmed turn is the cheapest evidence
there is, so it is harvested *before* the stratified sample:

* The confirmation is joined to its audit record by ``(session_id,
  turn_id)``; the question and the SQL that ran come from that record (the
  confirmation row holds only identifiers). A confirmation whose record has
  rotated out of the log, or whose record does not show a successful
  execution, is counted and skipped.
* The case is ``status = "reviewed"``, not ``"pending_review"``:
  ``"reviewed"`` is "a person confirmed the ``expected_sql`` and
  ``eval.cli verify`` has not run it yet" (:data:`eval.models.GoldenCaseStatus`),
  and a confirmation is exactly that -- the analyst saw the answer that SQL
  produced and said it was right. ``pending_review`` would ask a second
  person to re-judge what the first has already judged, and ``active``
  would skip the gate: ``python -m eval.cli verify --accept`` still runs the
  SQL against the database and is what activates the case.
* ``expected_sql`` is the SQL that ran; ``expect`` is ``"empty"`` for a
  zero-row result, else ``"success"``.
* Provenance lives in ``notes`` (the case model has no dedicated field, and
  ``notes`` is where the harvest already names the source record): the
  confirming analyst's principal id, the confirmation time, and the audit
  record. The case is tagged :data:`CONFIRMED_TAG`.
* Two confirmations of the same normalised question are one case; the most
  recent confirmation wins. A question already in a ``--exclude`` golden
  set is skipped like any other.
* Confirmed cases are never counted against ``--n``: the sample is drawn
  from the remaining questions, and the file holds the confirmed cases plus
  up to ``--n`` sampled ones.
* ``scripts/golden_sheet.py export`` lists them with the verdict already
  ``correct``, so analysts see them as confirmed and need only skim.

The file is never overwritten without ``--force`` (which keeps the previous
one as ``.bak``).
"""

from __future__ import annotations

import argparse
import hashlib
import random
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.console import use_utf8_console
from core.persian import normalize_for_matching
from eval.models import GoldenCase
from eval.store import dump_cases, load_cases_or_empty, write_text_atomic
from observability.audit import turn_executed_successfully
from scripts.analyze_audit_log import (
    _TRANSPORT_ERROR_CODES,
    iter_records,
    resolve_log_paths,
)

_DEFAULT_GLOB = "logs/audit_log.jsonl*"

_ARABIC_SCRIPT_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_WHITESPACE_RE = re.compile(r"\s+")

#: Substrings that mark an ``llm.model`` as a stub/test backend (see
#: ``analyze_audit_log.records_by_model``): their records are not real traffic.
_TEST_BACKEND_MARKERS: tuple[str, ...] = ("mock:", ":test", "stub")

#: Tag carried by every case harvested from an analyst's confirmation.
#: ``scripts/golden_sheet.py export`` recognises a case by it and pre-fills
#: its verdict as ``correct``.
CONFIRMED_TAG = "origin:confirmed"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One usable audit record, reduced to what a candidate case needs.

    Parameters
    ----------
    key:
        The normalised question (the de-duplication key).
    question:
        The user's wording, whitespace collapsed.
    lang:
        ``"fa"``, ``"en"``, ``"mixed"`` or ``"other"``.
    outcome:
        ``"success"``, ``"empty"``, ``"out_of_scope"`` or ``"error"``.
    datasource:
        The data source the record names, ``None`` when it names none.
    proposed_sql:
        The model's SQL for a successful record, else ``None``.
    error_code:
        The record's ``error_code`` for an ``"error"`` outcome, else ``None``.
    request_id, timestamp:
        Identify the source record (never the user).
    confirmed_by, confirmed_at:
        For a candidate that comes from an analyst's confirmation: the
        confirming principal's id and the confirmation time. ``None`` for
        every sampled candidate.
    """

    key: str
    question: str
    lang: str
    outcome: str
    datasource: str | None
    proposed_sql: str | None
    error_code: str | None
    request_id: str
    timestamp: str
    confirmed_by: str | None = None
    confirmed_at: str | None = None

    @property
    def is_confirmed(self) -> bool:
        """True for a candidate that comes from an analyst's confirmation."""
        return self.confirmed_by is not None

    @property
    def stratum(self) -> tuple[str, str, str]:
        """``(data source, outcome, language)``."""
        return (self.datasource or "(none)", self.outcome, self.lang)


def normalise_question(text: str) -> str:
    """The de-duplication key of *text*: the project's Persian/Arabic folding.

    Examples
    --------
    >>> normalise_question("  تعداد   سفارش‌های ۱۴۰۲ ") == normalise_question("تعداد سفارشهای 1402")
    True
    >>> normalise_question("How MANY orders?") == normalise_question("how many   orders?")
    True
    """
    return normalize_for_matching(text)


def detect_language(text: str) -> str:
    """The script of *text*: ``"fa"``, ``"en"``, ``"mixed"`` or ``"other"``.

    ``"fa"`` means Arabic-script letters only (Persian and Arabic are not
    told apart), ``"en"`` Latin letters only, ``"mixed"`` both, ``"other"``
    neither (digits and punctuation only).

    Examples
    --------
    >>> detect_language("چند مشتری داریم؟"), detect_language("how many?")
    ('fa', 'en')
    >>> detect_language("تعداد trades امروز"), detect_language("۱۴۰۲ ?")
    ('mixed', 'other')
    """
    letters = [ch for ch in text if ch.isalpha()]  # Persian digits are not letters
    arabic = any(_ARABIC_SCRIPT_RE.match(ch) for ch in letters)
    latin = any(_LATIN_RE.match(ch) for ch in letters)
    if arabic and latin:
        return "mixed"
    if arabic:
        return "fa"
    if latin:
        return "en"
    return "other"


def _is_test_backend(record: dict[str, Any]) -> bool:
    llm = record.get("llm")
    model = llm.get("model") if isinstance(llm, dict) else None
    return isinstance(model, str) and any(m in model.lower() for m in _TEST_BACKEND_MARKERS)


def _record_datasource(record: dict[str, Any]) -> str | None:
    value = record.get("datasource")
    if isinstance(value, str) and value.strip():
        return value
    selection = record.get("datasource_selection")
    if isinstance(selection, dict):
        chosen = selection.get("chosen")
        if isinstance(chosen, str) and chosen.strip():
            return chosen
    return None


def classify_record(
    record: dict[str, Any], *, include_test_backends: bool = False
) -> tuple[Candidate | None, str | None]:
    """Turn one audit record into a :class:`Candidate`, or say why it is dropped.

    Parameters
    ----------
    record:
        One parsed audit-log line.
    include_test_backends:
        Keep records from stub/test backends instead of dropping them.

    Returns
    -------
    tuple[Candidate | None, str | None]
        ``(candidate, None)`` or ``(None, reason)`` where *reason* is one of
        ``"no_question"``, ``"transport_error"``, ``"test_backend"``.

    Examples
    --------
    >>> rec = {"question": "How many customers?", "generated_sql": "SELECT 1",
    ...        "error_code": None, "row_count": 1, "datasource": "sales",
    ...        "request_id": "r1", "timestamp": "2026-01-02T03:04:05"}
    >>> cand, why = classify_record(rec)
    >>> (cand.outcome, cand.lang, cand.datasource, cand.proposed_sql, why)
    ('success', 'en', 'sales', 'SELECT 1', None)
    >>> classify_record({**rec, "error_code": "MODEL_TIMEOUT"})
    (None, 'transport_error')
    >>> classify_record({**rec, "error_code": "OUT_OF_SCOPE"})[0].outcome
    'out_of_scope'
    >>> classify_record({**rec, "error_code": "FORBIDDEN_SQL"})[0].proposed_sql is None
    True
    >>> classify_record({**rec, "question": "  "})
    (None, 'no_question')
    """
    question = record.get("question")
    if not isinstance(question, str) or not question.strip():
        return None, "no_question"
    if not include_test_backends and _is_test_backend(record):
        return None, "test_backend"
    code = record.get("error_code")
    code = code if isinstance(code, str) and code else None
    if code in _TRANSPORT_ERROR_CODES:
        return None, "transport_error"

    sql = record.get("generated_sql")
    sql = sql.strip() if isinstance(sql, str) and sql.strip() else None
    proposed: str | None = None
    if code is None:
        outcome = "empty" if record.get("row_count") == 0 else "success"
        proposed = sql
    elif code == "OUT_OF_SCOPE":
        outcome = "out_of_scope"
    else:
        outcome = "error"

    collapsed = _WHITESPACE_RE.sub(" ", question).strip()
    return (
        Candidate(
            key=normalise_question(collapsed),
            question=collapsed,
            lang=detect_language(collapsed),
            outcome=outcome,
            datasource=_record_datasource(record),
            proposed_sql=proposed,
            error_code=code if outcome == "error" else None,
            request_id=str(record.get("request_id") or ""),
            timestamp=str(record.get("timestamp") or ""),
        ),
        None,
    )


def allocate(sizes: dict[Any, int], n: int) -> dict[Any, int]:
    """Share *n* slots over strata: one each, the rest in proportion to size.

    Parameters
    ----------
    sizes:
        ``{stratum: number of available candidates}``.
    n:
        Total slots wanted.

    Returns
    -------
    dict
        ``{stratum: slots}``, never more than a stratum's size, summing to
        ``min(n, sum(sizes))``. When there are more strata than slots the
        *n* largest strata get one each.

    Examples
    --------
    >>> allocate({"a": 80, "b": 19, "c": 1}, 10)
    {'a': 7, 'b': 2, 'c': 1}
    >>> allocate({"a": 3, "b": 50}, 10)
    {'a': 1, 'b': 9}
    >>> allocate({"a": 5, "b": 5, "c": 5}, 2)
    {'a': 1, 'b': 1, 'c': 0}
    >>> allocate({"a": 2}, 10)
    {'a': 2}
    """
    strata = sorted(sizes, key=lambda s: (-sizes[s], str(s)))
    slots = {s: 0 for s in sorted(sizes, key=str)}
    if n <= 0 or not strata:
        return slots
    if n < len(strata):
        for s in strata[:n]:
            slots[s] = 1
        return slots
    for s in strata:
        slots[s] = 1 if sizes[s] > 0 else 0
    remaining = min(n, sum(sizes.values())) - sum(slots.values())
    while remaining > 0:
        room = {s: sizes[s] - slots[s] for s in strata if sizes[s] - slots[s] > 0}
        if not room:
            break
        total_room = sum(room.values())
        # Largest remainder over the strata that still have candidates.
        quotas = {s: remaining * room[s] / total_room for s in room}
        floors = {s: min(int(quotas[s]), room[s]) for s in room}
        for s, add in floors.items():
            slots[s] += add
        left = remaining - sum(floors.values())
        order = sorted(room, key=lambda s: (-(quotas[s] - int(quotas[s])), -room[s], str(s)))
        for s in order:
            if left <= 0:
                break
            if slots[s] < sizes[s]:
                slots[s] += 1
                left -= 1
        remaining = min(n, sum(sizes.values())) - sum(slots.values())
    return slots


def stratified_sample(
    candidates: Sequence[Candidate], n: int, *, seed: int = 0
) -> list[Candidate]:
    """A seeded stratified sample of *n* candidates (see the module docstring).

    Parameters
    ----------
    candidates:
        De-duplicated candidates.
    n:
        Sample size.
    seed:
        Seed of the within-stratum random choice.

    Returns
    -------
    list[Candidate]
        At most *n* candidates, ordered by stratum then by timestamp.

    Examples
    --------
    >>> mk = lambda i, ds, out: Candidate(f"k{i}", f"q{i}", "en", out, ds, None, None, f"r{i}", f"t{i:02d}")
    >>> pool = [mk(i, "sales", "success") for i in range(8)] + [mk(9, "inventory", "error")]
    >>> picked = stratified_sample(pool, 4, seed=1)
    >>> sorted(c.datasource for c in picked)
    ['inventory', 'sales', 'sales', 'sales']
    >>> [c.key for c in stratified_sample(pool, 4, seed=1)] == [c.key for c in picked]
    True
    """
    by_stratum: dict[tuple[str, str, str], list[Candidate]] = defaultdict(list)
    for cand in candidates:
        by_stratum[cand.stratum].append(cand)
    slots = allocate({s: len(v) for s, v in by_stratum.items()}, n)
    rng = random.Random(seed)
    chosen: list[Candidate] = []
    for stratum in sorted(by_stratum):
        pool = sorted(by_stratum[stratum], key=lambda c: (c.timestamp, c.request_id, c.key))
        picked = rng.sample(pool, slots.get(stratum, 0))
        chosen.extend(sorted(picked, key=lambda c: (c.timestamp, c.request_id, c.key)))
    return chosen


def candidate_to_case(cand: Candidate) -> GoldenCase:
    """The :class:`~eval.models.GoldenCase` for *cand*.

    A sampled candidate becomes ``pending_review``: its SQL is the model's
    unreviewed proposal. A candidate that comes from an analyst's
    confirmation (``cand.is_confirmed``) becomes ``reviewed``: a person has
    seen the answer that SQL produced and said it is correct, which is what
    :data:`eval.models.GoldenCaseStatus` means by ``"reviewed"``;
    ``eval.cli verify --accept`` still has to run it before it counts.

    The id is derived from the source record, so harvesting the same log
    twice gives the same ids. The notes name the record (``request_id`` and
    timestamp); only for a confirmed case do they also name the confirming
    principal and the confirmation time (the provenance the case model
    keeps in ``notes``), never a sampled record's user.

    Examples
    --------
    >>> cand = Candidate("k", "How many?", "en", "success", "sales", "SELECT 1", None, "r1", "2026-01-02")
    >>> case = candidate_to_case(cand)
    >>> case.status, case.expected_sql, case.datasource, case.tags
    ('pending_review', 'SELECT 1', 'sales', ['lang:en', 'source:sales', 'outcome:success'])
    >>> "r1" in case.notes and "2026-01-02" in case.notes
    True
    >>> done = candidate_to_case(replace(cand, confirmed_by="analyst-1", confirmed_at="2026-01-03T00:00:00+00:00"))
    >>> done.status, done.id.startswith("confirmed_"), CONFIRMED_TAG in done.tags
    ('reviewed', True, True)
    >>> "analyst-1" in done.notes and "2026-01-03" in done.notes
    True
    """
    digest = hashlib.sha256(
        f"{cand.request_id}|{cand.timestamp}|{cand.key}".encode("utf-8")
    ).hexdigest()[:10]
    tags = [f"lang:{cand.lang}"]
    if cand.datasource:
        tags.append(f"source:{cand.datasource}")
    tags.append(f"outcome:{cand.outcome}")
    if cand.error_code:
        tags.append(f"error:{cand.error_code}")
    expect = {"empty": "empty", "out_of_scope": "out_of_scope"}.get(cand.outcome, "success")
    expected_sql = None if expect == "out_of_scope" else cand.proposed_sql
    describe = {
        "success": "answered",
        "empty": "answered with no rows",
        "out_of_scope": "declined as out of scope",
        "error": f"failed ({cand.error_code})",
    }[cand.outcome]
    if cand.is_confirmed:
        tags.append(CONFIRMED_TAG)
        notes = (
            f"Confirmed correct by analyst {cand.confirmed_by} at "
            f"{cand.confirmed_at or '(no time)'}: the system {describe} in audit record "
            f"{cand.request_id or '(no request id)'} at {cand.timestamp or '(no timestamp)'}. "
            "expected_sql is the SQL that ran."
        )
        status = "reviewed"
    else:
        notes = (
            f"Harvested from audit record {cand.request_id or '(no request id)'} "
            f"at {cand.timestamp or '(no timestamp)'}: the system {describe}. "
            + (
                "expected_sql is the model's own, unreviewed proposal."
                if expected_sql
                else "No SQL is proposed; the reviewer supplies it."
            )
        )
        status = "pending_review"
    return GoldenCase(
        id=f"{'confirmed' if cand.is_confirmed else 'cand'}_{digest}",
        question=cand.question,
        tags=tags,
        expected_sql=expected_sql,
        expect=expect,  # type: ignore[arg-type]
        notes=notes,
        status=status,  # type: ignore[arg-type]
        datasource=cand.datasource if cand.outcome in ("success", "empty") else None,
    )


@dataclass(frozen=True, slots=True)
class HarvestResult:
    """What :func:`harvest` produced.

    Parameters
    ----------
    cases:
        The sampled candidates as cases.
    records_read:
        Audit records read.
    dropped:
        ``{reason: count}`` for records that were not usable.
    duplicates:
        Records dropped as a repeat of a more recent question.
    already_known:
        Distinct questions dropped because ``--exclude`` already has them.
    pool:
        Distinct usable questions the sample was drawn from.
    confirmed:
        How many of *cases* come from analyst confirmations (they come
        first, ahead of the sample, and are not counted against ``n``).
    confirmation_stats:
        ``{reason: count}`` for the confirmations read: ``"read"``,
        ``"included"``, ``"duplicate"`` (an older confirmation of a question
        a newer one covers), ``"no_audit_record"``, ``"not_successful"``,
        ``"unusable"`` (a record with no question, or from a test backend)
        and ``"already_known"`` (the question is in an ``--exclude`` set).
        ``None`` when confirmations were not harvested.
    """

    cases: list[GoldenCase]
    records_read: int
    dropped: dict[str, int]
    duplicates: int
    already_known: int
    pool: int
    confirmed: int = 0
    confirmation_stats: dict[str, int] | None = None


def confirmed_candidates(
    confirmations: Iterable[dict[str, Any]],
    turn_records: dict[tuple[str, str], dict[str, Any]],
    *,
    include_test_backends: bool = False,
    exclude_keys: frozenset[str] = frozenset(),
) -> tuple[dict[str, Candidate], dict[str, int]]:
    """Turn analyst confirmations into candidates, one per normalised question.

    Parameters
    ----------
    confirmations:
        Rows of :func:`appdb.confirmations.list_confirmations` (identifiers
        only: ``session_id``, ``turn_id``, ``principal_id``, ``created_at``).
    turn_records:
        ``{(session_id, turn_id): audit record}`` for the turns the
        confirmations name; the question and the SQL come from here.
    include_test_backends:
        See :func:`classify_record`.
    exclude_keys:
        Normalised questions already in a golden set; a confirmed question
        among them is skipped and counted as ``"already_known"``.

    Returns
    -------
    tuple[dict[str, Candidate], dict[str, int]]
        ``({normalised question: candidate}, stats)`` -- see
        :attr:`HarvestResult.confirmation_stats` for the stats keys. When
        two confirmations name the same normalised question the most recent
        one (by ``created_at``, then by id) wins.

    Examples
    --------
    >>> rec = {"question": "How many customers?", "generated_sql": "SELECT 1", "columns": ["n"],
    ...        "row_count": 1, "request_id": "r1", "timestamp": "2026-01-01", "datasource": "sales"}
    >>> older = {"confirmation_id": 1, "session_id": "s", "turn_id": "t1",
    ...          "principal_id": "analyst-1", "created_at": "2026-01-02"}
    >>> newer = {"confirmation_id": 2, "session_id": "s", "turn_id": "t2",
    ...          "principal_id": "analyst-2", "created_at": "2026-01-03"}
    >>> recs = {("s", "t1"): rec, ("s", "t2"): {**rec, "request_id": "r2"}}
    >>> found, stats = confirmed_candidates([older, newer], recs)
    >>> [(c.confirmed_by, c.request_id) for c in found.values()]
    [('analyst-2', 'r2')]
    >>> stats["read"], stats["included"], stats["duplicate"]
    (2, 1, 1)
    >>> confirmed_candidates([older], {})[1]["no_audit_record"]
    1
    """
    stats: Counter[str] = Counter()
    found: dict[str, Candidate] = {}
    ordered = sorted(
        confirmations,
        key=lambda c: (str(c.get("created_at") or ""), int(c.get("confirmation_id") or 0)),
    )
    for conf in ordered:
        stats["read"] += 1
        record = turn_records.get((str(conf.get("session_id")), str(conf.get("turn_id"))))
        if record is None:
            stats["no_audit_record"] += 1
            continue
        if not turn_executed_successfully(record):
            stats["not_successful"] += 1
            continue
        cand, _reason = classify_record(record, include_test_backends=include_test_backends)
        if cand is None or cand.outcome not in ("success", "empty"):
            stats["unusable"] += 1
            continue
        if cand.key in exclude_keys:
            stats["already_known"] += 1
            continue
        cand = replace(
            cand,
            confirmed_by=str(conf.get("principal_id") or ""),
            confirmed_at=str(conf.get("created_at") or ""),
        )
        if cand.key in found:
            stats["duplicate"] += 1
        found[cand.key] = cand
    stats["included"] = len(found)
    return found, dict(stats)


def harvest(
    records: Iterable[dict[str, Any]],
    *,
    n: int,
    seed: int = 0,
    exclude_keys: frozenset[str] = frozenset(),
    include_test_backends: bool = False,
    confirmations: Iterable[dict[str, Any]] | None = None,
) -> HarvestResult:
    """Classify, de-duplicate and sample *records* into candidate cases.

    Parameters
    ----------
    records:
        Parsed audit records.
    n:
        Sample size. Confirmed cases are *not* part of it (see below).
    seed:
        See :func:`stratified_sample`.
    exclude_keys:
        Normalised questions already in a golden set.
    include_test_backends:
        See :func:`classify_record`.
    confirmations:
        Analyst confirmations to harvest first (see
        :func:`confirmed_candidates`), or ``None`` to harvest none. Each
        confirmed question becomes a ``reviewed`` case ahead of the sample;
        the sample is drawn from the other questions, so a confirmed
        question is never in the file twice and ``n`` never drops one.

    Returns
    -------
    HarvestResult

    Examples
    --------
    >>> recs = [
    ...     {"question": "How many customers?", "generated_sql": "SELECT 1", "request_id": "r1",
    ...      "timestamp": "2026-01-01", "datasource": "sales"},
    ...     {"question": "how many   customers?", "generated_sql": "SELECT 2", "request_id": "r2",
    ...      "timestamp": "2026-01-02", "datasource": "sales"},
    ...     {"question": "weather?", "error_code": "OUT_OF_SCOPE", "request_id": "r3",
    ...      "timestamp": "2026-01-03"},
    ...     {"question": "x", "error_code": "MODEL_TIMEOUT"},
    ... ]
    >>> res = harvest(recs, n=10)
    >>> len(res.cases), res.duplicates, res.dropped
    (2, 1, {'transport_error': 1})
    >>> sorted(c.expected_sql or "-" for c in res.cases)
    ['-', 'SELECT 2']

    A confirmed question comes first, as a reviewed case, and is not
    sampled a second time:

    >>> turn = {**recs[1], "session_id": "s", "turn_id": "t", "columns": ["n"]}
    >>> conf = [{"confirmation_id": 1, "session_id": "s", "turn_id": "t",
    ...          "principal_id": "analyst-1", "created_at": "2026-01-04"}]
    >>> res = harvest([recs[0], turn, recs[2]], n=1, confirmations=conf)
    >>> [(c.status, c.expected_sql) for c in res.cases], res.confirmed
    ([('reviewed', 'SELECT 2'), ('pending_review', None)], 1)
    """
    dropped: Counter[str] = Counter()
    latest: dict[str, Candidate] = {}
    records_read = 0
    usable = 0
    confirmation_rows = list(confirmations) if confirmations is not None else None
    wanted = (
        {(str(c.get("session_id")), str(c.get("turn_id"))) for c in confirmation_rows}
        if confirmation_rows else set()
    )
    turn_records: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        records_read += 1
        if wanted:
            turn_key = (str(record.get("session_id")), str(record.get("turn_id")))
            if turn_key in wanted:
                turn_records[turn_key] = record  # the last record for a turn wins
        cand, reason = classify_record(record, include_test_backends=include_test_backends)
        if cand is None:
            dropped[reason or "unusable"] += 1
            continue
        usable += 1
        previous = latest.get(cand.key)
        if previous is None or cand.timestamp >= previous.timestamp:
            latest[cand.key] = cand
    duplicates = usable - len(latest)

    confirmed: dict[str, Candidate] = {}
    confirmation_stats: dict[str, int] | None = None
    if confirmation_rows is not None:
        confirmed, confirmation_stats = confirmed_candidates(
            confirmation_rows, turn_records,
            include_test_backends=include_test_backends, exclude_keys=exclude_keys,
        )

    known = [k for k in latest if k in exclude_keys]
    pool = [c for k, c in latest.items() if k not in exclude_keys and k not in confirmed]
    sample = stratified_sample(pool, n, seed=seed)
    confirmed_in_order = sorted(confirmed.values(), key=lambda c: (c.confirmed_at or "", c.key))
    return HarvestResult(
        cases=[candidate_to_case(c) for c in [*confirmed_in_order, *sample]],
        records_read=records_read,
        dropped=dict(dropped),
        duplicates=duplicates,
        already_known=len(known),
        pool=len(pool),
        confirmed=len(confirmed_in_order),
        confirmation_stats=confirmation_stats,
    )


def render_summary(
    result: HarvestResult,
    out: str,
    *,
    include_examples: bool = False,
    confirmation_note: str = "not harvested",
) -> str:
    """The text the script prints: counts only, unless *include_examples*.

    Parameters
    ----------
    result:
        From :func:`harvest`.
    out:
        Where the candidates were written (shown as is).
    include_examples:
        Opt in to printing up to five verbatim candidates (question and
        proposed SQL). The first line then says so.
    confirmation_note:
        What to print on the ``confirmations`` line when no confirmations
        were harvested (``--no-confirmations``, or the application database
        could not be read), so the reader knows why.

    Returns
    -------
    str

    Examples
    --------
    >>> res = harvest([{"question": "secret question?", "generated_sql": "SELECT 1",
    ...                 "request_id": "r1", "timestamp": "t", "datasource": "sales"}], n=5)
    >>> text = render_summary(res, "eval_data/candidates.jsonl")
    >>> "secret" in text, "SELECT 1" in text, "wrote 1 candidate(s)" in text
    (False, False, True)
    >>> "0 confirmed + 1 sampled" in text
    True
    >>> "secret question?" in render_summary(res, "x", include_examples=True)
    True
    """
    lines: list[str] = []
    if include_examples:
        lines.append(
            "*** THIS OUTPUT INCLUDES VERBATIM EXAMPLE QUESTIONS AND SQL. "
            "Do not send it anywhere without checking that is acceptable. ***"
        )
    lines.append(f"records read           : {result.records_read}")
    for reason, count in sorted(result.dropped.items()):
        lines.append(f"  dropped ({reason}) : {count}")
    lines.append(f"  duplicate questions  : {result.duplicates}")
    lines.append(f"  already in golden set: {result.already_known}")
    lines.append(f"distinct candidates    : {result.pool}")
    stats = result.confirmation_stats
    if stats is None:
        lines.append(f"confirmations          : {confirmation_note}")
    else:
        detail = ", ".join(
            f"{label} {stats[key]}"
            for key, label in (
                ("duplicate", "older duplicates"),
                ("no_audit_record", "no audit record"),
                ("not_successful", "not a successful answer"),
                ("unusable", "unusable"),
                ("already_known", "already in golden set"),
            )
            if stats.get(key)
        )
        lines.append(
            f"confirmations          : read {stats.get('read', 0)}, "
            f"included {result.confirmed}" + (f" ({detail})" if detail else "")
        )
    sampled = len(result.cases) - result.confirmed
    lines.append(
        f"wrote {len(result.cases)} candidate(s) to {out} "
        f"({result.confirmed} confirmed + {sampled} sampled)"
    )
    for label, extract in (
        ("by data source", lambda c: c.datasource or "(none)"),
        ("by outcome", lambda c: next(t[8:] for t in c.tags if t.startswith("outcome:"))),
        ("by language", lambda c: next(t[5:] for t in c.tags if t.startswith("lang:"))),
    ):
        counts = Counter(extract(c) for c in result.cases)
        if counts:
            lines.append(f"  {label}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if include_examples:
        lines.append("examples (verbatim):")
        for case in result.cases[:5]:
            lines.append(f"  [{case.id}] {case.question}")
            if case.expected_sql:
                lines.append(f"      proposed: {_WHITESPACE_RE.sub(' ', case.expected_sql)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Returns the process exit code.

    ``0`` on success, ``1`` when no log file matched, ``2`` when the output
    exists and ``--force`` was not given.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Harvest a stratified sample of candidate golden cases from the audit log "
            "(run on the server; prints counts only -- see --include-examples)."
        ),
    )
    parser.add_argument(
        "paths", nargs="*", default=[_DEFAULT_GLOB],
        help=f"Audit log file(s) and/or glob pattern(s). Default: '{_DEFAULT_GLOB}'.",
    )
    parser.add_argument("--n", type=int, default=150, help="Sample size (default 150).")
    parser.add_argument(
        "--out", default="eval_data/candidates.jsonl",
        help="Where to write the candidates (default eval_data/candidates.jsonl).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite --out if it exists (the previous file is kept as .bak).",
    )
    parser.add_argument("--seed", type=int, default=0, help="Sampling seed (default 0).")
    parser.add_argument(
        "--exclude", action="append", default=[],
        help="A golden/candidates .jsonl whose questions are not harvested again (repeatable).",
    )
    parser.add_argument(
        "--include-test-backends", action="store_true",
        help="Keep records from stub/test backends (dropped by default).",
    )
    parser.add_argument(
        "--no-confirmations", action="store_true",
        help=(
            "Do not harvest analysts' confirmations (\"this answer is correct\"). By default "
            "each confirmed answer becomes a 'reviewed' case ahead of the sample and does not "
            "count against --n."
        ),
    )
    parser.add_argument(
        "--include-examples", action="store_true",
        help=(
            "Opt-in: also print a few verbatim candidates. NOT the default -- the default "
            "output is counts only, safe to copy off the server."
        ),
    )
    args = parser.parse_args(argv)

    if args.n < 1:
        print("--n must be at least 1", file=sys.stderr)
        return 2
    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        print(
            f"{out_path} already exists; refusing to overwrite it (use --force; the "
            "previous file is kept as .bak).",
            file=sys.stderr,
        )
        return 2

    paths = resolve_log_paths(args.paths)
    if not paths:
        print(f"No log files matched: {args.paths}", file=sys.stderr)
        return 1

    exclude_keys = frozenset(
        normalise_question(c.question)
        for path in args.exclude
        for c in load_cases_or_empty(path)
    )
    confirmations: list[dict[str, Any]] | None = None
    confirmation_note = "skipped (--no-confirmations)"
    if not args.no_confirmations:
        # Imported here: it reaches the application database, which a run
        # with --no-confirmations must not touch.
        from observability.quality import load_ratings

        ratings = load_ratings()
        confirmations = ratings.confirmations
        if confirmations is None:
            confirmation_note = (
                f"unavailable (application database not read: {ratings.error}); "
                "fix it and run again, or pass --no-confirmations"
            )
    result = harvest(
        iter_records(paths),
        n=args.n,
        seed=args.seed,
        exclude_keys=exclude_keys,
        include_test_backends=args.include_test_backends,
        confirmations=confirmations,
    )
    write_text_atomic(out_path, dump_cases(result.cases), backup=True)
    print(
        render_summary(
            result, str(out_path), include_examples=args.include_examples,
            confirmation_note=confirmation_note,
        )
    )
    return 0


if __name__ == "__main__":
    use_utf8_console()
    sys.exit(main())
