"""Offline analysis script: find questions where the retriever misses expected tables.

Usage::

    python scripts/analyze_misses.py [LOG_PATH]

If LOG_PATH is omitted, defaults to ``logs/query_log.jsonl``.

The script reads a JSONL query-log file produced by ``logs/logger.py``,
identifies SUCCESS entries where the generated SQL references a table that
the retriever did *not* surface, and reports which novel question tokens could
be added to ``knowledge/aliases.py`` to close the gap.

Pipeline overview
-----------------
1. :func:`_tables_in_sql`   — extract table names referenced by the SQL.
2. :func:`retrieve_tables`  — re-run retrieval with ``fallback=False`` to
   find what the retriever *would* have chosen at query time.
3. Compare the two sets: any table in SQL but not in retrieved is a **miss**.
4. :func:`_candidate_tokens` — identify question tokens not yet in the KB
   that might serve as new synonym keys.
5. :func:`_build_report`    — aggregate misses by table and rank candidate
   tokens by frequency.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema_data.retriever import retrieve_tables
from knowledge.aliases import SYNONYMS
from schema_data.tables import TABLE_DESCRIPTIONS as TABLES

_STOP: frozenset[str] = frozenset(
    "در از به با که این آن را برای تا هم هر چه یا اما ولی چون"
    " اگر پس بر روی زیر بین بالا پایین همه هیچ خود مثل مانند"
    " باید شاید شد شده است بود بودن کردن کرد دارد داشت داشتن"
    " می‌شود می‌کند می‌دهد می‌شد می‌کرد بر اساس نسبت به طور".split()
)


def _split_tokens(text: str) -> list[str]:
    """Tokenise *text* by splitting on punctuation, whitespace, and Persian separators.

    Splits on any run of the following characters:

    * ASCII whitespace
    * Persian comma (U+060C ``،``)
    * ``,  ;  /  \\  |  (  )  [  ]  {  }  :  !  ?  .  -``
    * Em-dash (U+2014 ``—``), en-dash (U+2013 ``–``), and ``—``
    * Ampersand ``&``

    Empty tokens produced by consecutive delimiters are discarded.
    Zero-Width Non-Joiners (U+200C) that appear *within* a token (e.g.
    ``می‌کند``) are **not** stripped here; use
    :func:`~schema_data.retriever._normalise` if you need ZWNJ removal.

    Parameters
    ----------
    text:
        Any Unicode string (Persian, English, or mixed).

    Returns
    -------
    list[str]
        Non-empty token strings in the order they appeared in *text*.

    Examples
    --------
    >>> _split_tokens("فروش ماهانه مشتریان")
    ['فروش', 'ماهانه', 'مشتریان']

    >>> _split_tokens("Contract (Auction_Fact)")
    ['Contract', 'Auction_Fact']

    >>> _split_tokens("تاریخ: 1402/01/01")
    ['تاریخ', '1402', '01', '01']

    >>> _split_tokens("")
    []
    """
    return [
        token.strip()
        for token in re.split(r"[\s\u060c,;/\\|()\[\]{}:!?.\-\u2014\u2013—&]+", text)
        if token.strip()
    ]


# ---------------------------------------------------------------------------
# Known-token set
# ---------------------------------------------------------------------------
# Include tokens from:
# 1) table descriptions  (English text)
# 2) synonym keys        (Persian words such as 'مشتری')
# 3) synonym values      (Persian / English aliases for each key)
#
# This matters because tests expect words like 'مشتری' to be treated as
# already-known even if they appear only in synonym expansions, not directly in
# English table descriptions.
_KNOWN_TOKENS: frozenset[str] = frozenset(
    token
    for text in (
        *TABLES.values(),
        *SYNONYMS.keys(),
        *(value for values in SYNONYMS.values() for value in values),
    )
    for token in _split_tokens(text)
    if len(token) > 1
)


@dataclass
class Miss:
    """One retrieval miss detected from a query-log entry.

    Attributes
    ----------
    question:
        The original natural-language question that was asked.
    missing:
        Sorted list of table names that appear in the generated SQL but were
        **not** returned by :func:`~schema_data.retriever.retrieve_tables`
        when called with ``fallback=False``.
    candidates:
        Question tokens that are not yet covered by the knowledge-base (not
        in :data:`_KNOWN_TOKENS` and not in :data:`_STOP`).  These are
        potential new synonym keys to add to ``knowledge/aliases.py``.
    sql:
        The generated SQL string that triggered the miss.
    """

    question: str
    missing: list[str]
    candidates: list[str]
    sql: str


def _tables_in_sql(sql: str) -> set[str]:
    """Return the set of *known* table names referenced in *sql*.

    Applies three regex patterns in priority order to handle all common
    T-SQL quoting styles:

    1. ``[schema].[Table]`` — schema-qualified bracketed name; captures
       the table portion (group 1).
    2. ``[Table]``          — unqualified bracketed name (group 2).
    3. ``PascalCaseWord``   — unquoted identifier starting with an uppercase
       letter followed by mixed-case letters (group 3).

    Only names that exist as keys in
    :data:`~schema_data.tables.TABLE_DESCRIPTIONS` are included in the
    result; unrecognised identifiers are silently skipped.

    Parameters
    ----------
    sql:
        A T-SQL query string.  May be empty or ``None``-like (any falsy
        value returns an empty set immediately).

    Returns
    -------
    set[str]
        Table names from the TABLES registry that appear in *sql*.
        Empty set when *sql* is empty or no known tables are found.

    Examples
    --------
    >>> tables = _tables_in_sql("SELECT * FROM [Auction_Dim].[Bank]")
    >>> "Bank" in tables
    True

    >>> tables = _tables_in_sql("SELECT * FROM [Auction_Fact].[Contract] JOIN [Auction_Dim].[Customer]")
    >>> tables == {"Contract", "Customer"}
    True

    >>> _tables_in_sql("")
    set()

    >>> _tables_in_sql("SELECT 1")
    set()
    """
    if not sql:
        return set()

    pattern = re.compile(
        r"\[\w+\]\.\[(\w+)\]"
        r"|\[(\w+)\]"
        r"|(\b[A-Z][A-Za-z]+\b)"
    )
    found: set[str] = set()
    for m in pattern.finditer(sql):
        name = m.group(1) or m.group(2) or m.group(3) or ""
        if name and name in TABLES:
            found.add(name)
    return found


def _candidate_tokens(question: str) -> list[str]:
    """Return question tokens that are not already covered by the knowledge-base.

    A token is considered "already covered" if it appears in
    :data:`_KNOWN_TOKENS` (which includes tokens from table descriptions,
    synonym keys, and synonym values).  Stop-words from :data:`_STOP` and
    single-character tokens are also excluded.

    The returned list contains only the *novel* tokens that could be added
    as new synonym keys or values to ``knowledge/aliases.py`` to improve
    future retrieval.

    Parameters
    ----------
    question:
        The natural-language question (Persian, English, or mixed).  Leading
        and trailing whitespace is stripped before tokenisation.

    Returns
    -------
    list[str]
        Ordered list of novel tokens (in the order they appear in *question*),
        with duplicates **preserved** so that frequency analysis downstream
        (:func:`_build_report`) can use a ``Counter``.

    Examples
    --------
    >>> # 'فروش' is not in any description or synonym → candidate
    >>> "فروش" in _candidate_tokens("فروش ویژه")
    True

    >>> # A stop-word → excluded
    >>> "به" in _candidate_tokens("به چه دلیل")
    False

    >>> # Single character → excluded
    >>> "a" in _candidate_tokens("a b c")
    False
    """
    result: list[str] = []
    for tok in _split_tokens(question.strip()):
        if len(tok) <= 1:
            continue
        if tok in _STOP:
            continue
        if tok in _KNOWN_TOKENS:
            continue
        result.append(tok)
    return result


def analyse(log_path: Path) -> list[Miss]:
    """Read a JSONL query-log file and return all retrieval misses.

    Reads every line of *log_path*, filters to ``status == "SUCCESS"``
    entries, and for each entry:

    1. Extracts table names referenced in ``generated_sql`` via
       :func:`_tables_in_sql`.
    2. Re-runs the retriever on the original ``question`` with
       ``fallback=False`` so that low-confidence fallback results do not
       mask genuine misses.
    3. Computes the set difference: tables in SQL but **not** in retrieved.
    4. If any tables are missing, creates a :class:`Miss` entry.

    Parameters
    ----------
    log_path:
        :class:`~pathlib.Path` to a JSONL file where each line is a JSON
        object with at least the keys ``status``, ``question``, and
        ``generated_sql``.

    Returns
    -------
    list[Miss]
        One :class:`Miss` per log entry that had at least one missed table.
        Returns an empty list when *log_path* does not exist or when no
        misses are found.

    Examples
    --------
    >>> import json
    >>> from pathlib import Path
    >>>
    >>> # Write a minimal log file
    >>> log = Path("/tmp/test_log.jsonl")
    >>> _ = log.write_text(
    ...     json.dumps({
    ...         "status": "SUCCESS",
    ...         "question": "چیز بسیار نامشناس",
    ...         "generated_sql": "SELECT * FROM [Auction_Dim].[Bank]",
    ...     }) + "\n",
    ...     encoding="utf-8",
    ... )
    >>> result = analyse(log)
    >>> any("Bank" in m.missing for m in result)
    True

    >>> # Non-existent file → empty list
    >>> analyse(Path("/tmp/does_not_exist.jsonl"))
    []
    """
    if not log_path.exists():
        return []

    misses: list[Miss] = []
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("status") != "SUCCESS":
                continue

            question = entry.get("question", "")
            sql = entry.get("generated_sql", "")

            tables_in_sql = _tables_in_sql(sql)
            if not tables_in_sql:
                continue

            retrieved = set(retrieve_tables(question, fallback=False))
            missing = sorted(tables_in_sql - retrieved)
            if not missing:
                continue

            misses.append(
                Miss(
                    question=question,
                    missing=missing,
                    candidates=_candidate_tokens(question),
                    sql=sql,
                )
            )

    return misses


def _build_report(misses: list[Miss]) -> dict[str, Any]:
    """Aggregate *misses* into a structured report dictionary.

    For each missed table, counts how many times it was missed and ranks
    the candidate question tokens by frequency across all miss events.  The
    top-10 candidates per table are included so maintainers know which
    tokens to add to ``knowledge/aliases.py`` first.

    Parameters
    ----------
    misses:
        List of :class:`Miss` objects produced by :func:`analyse`.
        May be empty (returns a zeroed report structure).

    Returns
    -------
    dict[str, Any]
        A nested dictionary with the following structure::

            {
                "total_miss_events": int,
                "tables_ranked_by_miss_count": [
                    {
                        "table": str,
                        "miss_count": int,
                        "top_candidates": [
                            {"token": str, "frequency": int},
                            ...,  # up to 10
                        ],
                    },
                    ...,  # tables sorted by miss_count descending
                ],
            }

    Examples
    --------
    >>> m = Miss(
    ...     question="خرید ویژه",
    ...     missing=["Ring"],
    ...     candidates=["ویژه"],
    ...     sql="SELECT * FROM [Auction_Dim].[Ring]",
    ... )
    >>> report = _build_report([m])
    >>> report["total_miss_events"]
    1
    >>> report["tables_ranked_by_miss_count"][0]["table"]
    'Ring'
    >>> report["tables_ranked_by_miss_count"][0]["top_candidates"][0]["token"]
    'ویژه'
    """
    table_counter: Counter[str] = Counter()
    candidate_by_table: dict[str, Counter[str]] = {}

    for miss in misses:
        for table in miss.missing:
            table_counter[table] += 1
            if table not in candidate_by_table:
                candidate_by_table[table] = Counter()
            candidate_by_table[table].update(miss.candidates)

    ranked = []
    for table, count in table_counter.most_common():
        top_cands = [
            {"token": tok, "frequency": freq}
            for tok, freq in candidate_by_table[table].most_common(10)
        ]
        ranked.append(
            {
                "table": table,
                "miss_count": count,
                "top_candidates": top_cands,
            }
        )

    return {
        "total_miss_events": len(misses),
        "tables_ranked_by_miss_count": ranked,
    }


def main() -> None:
    """CLI entry-point: parse arguments, run analysis, and print the report.

    Reads ``sys.argv[1]`` as an optional log-file path.  If omitted,
    defaults to ``logs/query_log.jsonl`` relative to the current working
    directory.

    Output is printed to ``stdout``.

    Examples
    --------
    Run from the repository root::

        python scripts/analyze_misses.py
        python scripts/analyze_misses.py logs/query_log.jsonl
        python scripts/analyze_misses.py /path/to/custom_log.jsonl
    """
    log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("logs/query_log.jsonl")
    misses = analyse(log_path)

    if not misses:
        print("✅  No retrieval misses found.")
        return

    report = _build_report(misses)
    print(f"\n🔍  {report['total_miss_events']} miss event(s) detected\n")
    print("-" * 60)
    for entry in report["tables_ranked_by_miss_count"]:
        print(f"  Table : {entry['table']}  (missed {entry['miss_count']}x)")
        for cand in entry["top_candidates"]:
            print(f"    candidate token: {cand['token']!r}  (freq={cand['frequency']})")
    print("-" * 60)


if __name__ == "__main__":
    main()
