# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Regular expressions that once backtracked super-linearly stay linear.

Each pattern below was rewritten so that a long run of the characters it
skips cannot make the engine retry the run from every position in it. The
rewrites keep every match exactly as it was (the expected values below are
what the old patterns returned); what changed is the running time on hostile
input, which went from seconds to well under a millisecond:

* ``security.sql_guard._FENCE_RE`` and ``llm.providers._FENCE_RE`` read the
  model's reply. ``"```" + 3,000 spaces`` with no closing fence took about
  ten seconds; the body is now stripped by the caller instead of by ``\\s*``
  on both sides of a lazy ``.*?``.
* ``security.sql_guard._LIMIT_STRIP_RE`` and
  ``database.errors._TRAILING_ODBC_API_RE`` start a match only where a
  whitespace run starts (``(?<!\\s)``).
* ``database.errors._PORT_RE`` no longer has two adjacent optional ``\\s*``.
* ``core.value_shapes._EMAIL`` settles "the rest is allowed characters" with
  a lookahead before the domain backtracks to its last dot.

The time bound is deliberately loose (a second for input that the old
patterns needed tens of seconds or more for at this size), so a slow CI
runner cannot make it flaky while a regression still fails it.
"""

from __future__ import annotations

import time

import pytest

from core import value_shapes
from database import errors
from llm import providers
from security import sql_guard

_LONG = 50_000
_BOUND_SECONDS = 1.0


def _elapsed(fn, *args) -> float:
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start


@pytest.mark.parametrize(
    ("label", "fn", "hostile"),
    [
        ("sql fence", sql_guard._FENCE_RE.search, "```sql" + " " * _LONG + "x"),
        ("json fence", providers._FENCE_RE.search, "```json" + " " * _LONG + "x"),
        ("LIMIT strip", lambda s: sql_guard._LIMIT_STRIP_RE.sub("", s), " " * _LONG + "x"),
        ("ODBC tail", lambda s: errors._TRAILING_ODBC_API_RE.sub("", s), " " * _LONG + "x"),
        ("port", errors._PORT_RE.search, "port" + " " * _LONG + "x"),
        ("email", value_shapes._EMAIL.match, "a@" + "a." * _LONG + " "),
    ],
)
def test_hostile_input_is_handled_in_linear_time(label, fn, hostile):
    assert _elapsed(fn, hostile) < _BOUND_SECONDS, label


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("```sql\n  SELECT 1  \n```", "SELECT 1"),
        ("```SQL SELECT 2```", "SELECT 2"),
        ("Here:\n```\nSELECT 3\n```\nthanks", "SELECT 3"),
        ("```sql   ```", ""),
    ],
)
def test_sql_fence_body_is_what_it_was(raw, expected):
    assert sql_guard._FENCE_RE.search(raw).group(1).strip() == expected


def test_json_fence_body_is_what_it_was():
    assert providers._strip_fences('```json\n {"a": 1} \n```') == '{"a": 1}'
    assert providers._strip_fences("no fence ") == "no fence"


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT a FROM t LIMIT 5", "SELECT a FROM t"),
        ("SELECT a FROM t\n  limit 10;", "SELECT a FROM t;"),
        ("SELECT a FROM t", "SELECT a FROM t"),
    ],
)
def test_limit_strip_removes_the_clause_and_the_space_before_it(sql, expected):
    assert sql_guard._LIMIT_STRIP_RE.sub("", sql) == expected


def test_odbc_tail_is_removed_with_the_space_before_it():
    assert errors._TRAILING_ODBC_API_RE.sub("", "Invalid object name 'x'. (SQLExecDirectW)") == (
        "Invalid object name 'x'."
    )
    assert errors._TRAILING_ODBC_API_RE.sub("", "no tail here") == "no tail here"


@pytest.mark.parametrize(
    ("text", "found"),
    [("port 1433", True), ("port: 1433", True), ("port =  1433", True), ("port  =", False), ("sport 1", False)],
)
def test_port_mention_is_found_as_before(text, found):
    assert bool(errors._PORT_RE.search(text)) is found


@pytest.mark.parametrize(
    ("text", "is_email"),
    [
        ("a@b.c", True),
        ("a@.b.c", True),
        ("a@b.", False),
        ("a@bc", False),
        ("a@b.c d", False),
        ("a@b@c.d", False),
        ("a@b.c\n", True),
    ],
)
def test_email_shape_is_what_it_was(text, is_email):
    assert bool(value_shapes._EMAIL.match(text)) is is_email
