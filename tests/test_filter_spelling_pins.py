# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pins: what a request does today when a filter literal is spelled with other letter forms.

The warehouse may store a value with the Arabic letters ``ي``/``ك`` while the
model writes the Persian ``ی``/``ک``. A deterministic spelling fix for that
(``FILTER_VALUE_SPELLING``) is an addition; these tests describe the behaviour
a request has without it, and were written against the code as it stood before
the fix existed: they must pass there and keep passing, byte for byte, after
it. They cover

* the ``/query`` path (``llm.sql_agent.SQLAgent``) and the session path
  (``session.engine.TurnEngine``): the statement that is executed, the result's
  fields, the number of model calls and the audit line's key list, for a
  literal that is not a stored value only by its letter forms, with the fix
  off (a context manager that is a no-op where the setting does not exist yet);
* the cases the fix must never touch, with the setting at its default: a
  literal that is a stored value, a column with no cached vocabulary, a
  literal two stored values could be, a ``LIKE`` over a column that stores both
  forms, and a denied column;
* how the 6.16.0 filter value check sees such a literal (as a known value, so no
  model round), which is what the fix runs ahead of;
* :func:`core.persian.normalize_for_matching`, its version and the query cache's
  key, which the fix and the plural matching must leave alone.
"""

from __future__ import annotations

import contextlib
import dataclasses
from unittest.mock import patch

import pandas as pd
import pytest

import config as cfg
from api.query_cache import QueryCache
from config import override_settings
from core.models import RetrievalContext
from core.persian import NORMALIZER_VERSION, normalize_compact, normalize_for_matching
from llm.router import LLMRouter, PromptSegments
from llm.sql_agent import SQLAgent
from observability.audit import AuditRecord
from retrieval import dimension_vocabulary as dv
from security.sql_guard import PolicyRejection
from session.engine import TurnEngine
from session.store import SessionStore

SYSTEM_PROMPT = "You are a T-SQL expert."
QUESTION = "total value in the small export hall"
CONTEXT = RetrievalContext(entities=["Ring"], filters={})
RESULT = pd.DataFrame({"Name": ["x"]})
META = {"raw": {}, "endpoint_status": 200, "attempts": 1}

#: The letters that differ: Persian YEH / KAF, then the Arabic ones.
PERSIAN_YEH, PERSIAN_KAF = "ی", "ک"
ARABIC_YEH, ARABIC_KAF = "ي", "ك"


def arabic(text: str) -> str:
    """*text* with its Persian ``ی``/``ک`` written as the Arabic ``ي``/``ك``."""
    return text.replace(PERSIAN_YEH, ARABIC_YEH).replace(PERSIAN_KAF, ARABIC_KAF)


#: A hall as the model writes it, and as the warehouse stores it.
HALL = "تالار صادراتی کوچک"
HALL_STORED = arabic(HALL)
OTHER = "تالار صنعتی"
assert HALL != HALL_STORED


def eq_sql(value: str) -> str:
    return f"SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] = N'{value}'"


def like_sql(pattern: str) -> str:
    return f"SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] LIKE N'{pattern}'"


#: The keys of one audit line, in the order they are written.
AUDIT_KEYS = [
    "timestamp", "request_id", "question", "generated_sql", "guard", "row_count", "tier",
    "error_code", "error_message", "timings", "llm", "columns", "principal_id",
    "resolved_columns", "session_id", "turn_id", "config_version_id", "assumptions",
    "datasource", "datasource_selection",
]


def spelling_fix_off():
    """Switch the spelling fix off where it exists; where it does not yet, change nothing."""
    if "filter_value_spelling" in {f.name for f in dataclasses.fields(cfg.Settings)}:
        return override_settings(filter_value_spelling=False)
    return contextlib.nullcontext()


class _Recorder:
    name = "recorder"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.segments: list[PromptSegments] = []

    @property
    def calls(self) -> int:
        return len(self.segments)

    def generate_with_meta_segments(self, segments: PromptSegments):
        self.segments.append(segments)
        return self._responses[len(self.segments) - 1], dict(META)


def seed(values: list[str] | None, column: str = "Name") -> None:
    dv.clear_vocabulary_cache()
    if values is not None:
        dv.refresh_vocabulary(
            "Ring", column, execute_fn=lambda sql, params: pd.DataFrame({column: values}),
        )


@pytest.fixture(autouse=True)
def _cold_cache():
    seed(None)
    yield
    dv.clear_vocabulary_cache()


def run_query_path(sql: str, *, denied_columns=None):
    executed: list[str] = []

    def execute(statement: str) -> pd.DataFrame:
        executed.append(statement)
        return RESULT.copy()

    backend = _Recorder([sql])
    agent = SQLAgent(backend=backend, execute_fn=execute, max_corrections=2)
    with patch("llm.sql_agent.ContextRetriever.retrieve", return_value=CONTEXT):
        df, result = agent.run(QUESTION, SYSTEM_PROMPT, denied_columns=denied_columns)
    return backend, executed, df, result


def run_session_path(sql: str, *, denied_columns=None):
    executed: list[str] = []
    records: list[AuditRecord] = []

    def execute(statement: str) -> pd.DataFrame:
        executed.append(statement)
        return RESULT.copy()

    backend = _Recorder([sql])
    engine = TurnEngine(router=LLMRouter(default_chain=[backend]), execute_fn=execute, max_corrections=2)
    record = SessionStore(ttl_seconds=60, max_size=10, max_turns=10).create()
    with patch("session.engine.ContextRetriever.retrieve", return_value=CONTEXT), patch(
        "session.engine.save_audit_record", records.append,
    ):
        turn = engine.ask(record, QUESTION, SYSTEM_PROMPT, denied_columns=denied_columns)
    return backend, executed, turn, records


# ---------------------------------------------------------------------------
# A literal that differs from the stored value only by its letter forms
# ---------------------------------------------------------------------------


class TestTheSpellingMismatchTodayQueryPath:
    def test_the_statement_runs_as_the_model_wrote_it(self):
        seed([HALL_STORED, OTHER])
        sql = eq_sql(HALL)

        with spelling_fix_off():
            backend, executed, df, result = run_query_path(sql)

        assert backend.calls == 1
        assert executed == [sql]
        assert result.sql == sql
        assert result.raw_response == sql
        assert result.attempt == 1
        assert result.correction_prompts == []
        assert result.value_check is None
        assert df.equals(RESULT)

    def test_an_in_list_and_a_not_equal_run_as_written(self):
        seed([HALL_STORED, OTHER])
        for sql in (
            f"SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] IN (N'{HALL}', N'{OTHER}')",
            f"SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] <> N'{HALL}'",
            f"SELECT TOP 5 r.[Name] FROM [ref].[Ring] AS r WHERE r.[Name] NOT IN (N'{HALL}')",
        ):
            with spelling_fix_off():
                _backend, executed, _df, result = run_query_path(sql)
            assert executed == [sql]
            assert result.sql == sql

    def test_a_like_pattern_runs_as_written(self):
        seed([HALL_STORED, OTHER])
        sql = like_sql(f"%{PERSIAN_KAF}وچ%")

        with spelling_fix_off():
            _backend, executed, _df, result = run_query_path(sql)

        assert executed == [sql]
        assert result.sql == sql

    def test_with_the_value_check_on_the_literal_is_a_known_value_and_no_model_round_is_spent(self):
        seed([HALL_STORED, OTHER])
        sql = eq_sql(HALL)

        with spelling_fix_off(), override_settings(filter_value_check=True):
            backend, executed, _df, result = run_query_path(sql)

        assert backend.calls == 1
        assert executed == [sql]
        assert result.sql == sql
        assert result.attempt == 1
        assert result.value_check == {"checked": 1, "unknown": [], "corrected": False, "fallback": False}


class TestTheSpellingMismatchTodaySessionPath:
    def test_the_statement_runs_as_the_model_wrote_it(self):
        seed([HALL_STORED, OTHER])
        sql = eq_sql(HALL)

        with spelling_fix_off():
            backend, executed, turn, records = run_session_path(sql)

        assert turn.error is None
        assert backend.calls == 1
        assert executed == [sql]
        assert turn.sql == sql
        assert turn.warnings == []
        assert turn.llm["corrections"] == 0
        assert len(records) == 1
        assert list(records[0].as_dict()) == AUDIT_KEYS
        assert records[0].generated_sql == sql

    def test_with_the_value_check_on_the_literal_is_a_known_value_and_no_model_round_is_spent(self):
        seed([HALL_STORED, OTHER])
        sql = eq_sql(HALL)

        with spelling_fix_off(), override_settings(filter_value_check=True):
            backend, executed, turn, records = run_session_path(sql)

        assert backend.calls == 1
        assert executed == [sql]
        assert turn.sql == sql
        assert turn.llm["corrections"] == 0
        assert records[0].value_check == {"checked": 1, "unknown": [], "corrected": False, "fallback": False}


# ---------------------------------------------------------------------------
# What the fix must never touch (true today, and with the fix on)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runner", [run_query_path, run_session_path], ids=["query", "session"])
class TestNeverTouchedOnEitherPath:
    def test_a_literal_that_is_a_stored_value_runs_as_written(self, runner):
        seed([HALL_STORED, OTHER])
        for value in (HALL_STORED, OTHER):
            sql = eq_sql(value)
            _backend, executed, _result, _extra = runner(sql)
            assert executed == [sql]

    def test_a_column_with_no_cached_vocabulary_runs_as_written(self, runner):
        sql = eq_sql(HALL)
        _backend, executed, _result, _extra = runner(sql)
        assert executed == [sql]

    def test_a_vocabulary_cached_for_another_column_does_not_count(self, runner):
        seed([HALL_STORED, OTHER], column="Code")
        sql = eq_sql(HALL)
        _backend, executed, _result, _extra = runner(sql)
        assert executed == [sql]

    def test_a_literal_two_stored_values_could_be_runs_as_written(self, runner):
        seed([HALL_STORED, HALL.replace(PERSIAN_KAF, ARABIC_KAF), OTHER])
        sql = eq_sql(HALL)
        _backend, executed, _result, _extra = runner(sql)
        assert executed == [sql]

    def test_a_like_over_a_column_that_stores_both_forms_runs_as_written(self, runner):
        seed([HALL_STORED, HALL, OTHER])
        sql = like_sql(f"%{PERSIAN_KAF}وچ%")
        _backend, executed, _result, _extra = runner(sql)
        assert executed == [sql]

    def test_an_unrelated_literal_runs_as_written(self, runner):
        seed([HALL_STORED, OTHER])
        for value in ("Gamma Hall", "تالار دیگر"):
            sql = eq_sql(value)
            _backend, executed, _result, _extra = runner(sql)
            assert executed == [sql]


class TestADeniedColumnIsStillRefused:
    def test_the_guard_refuses_a_denied_column_whatever_the_literal(self):
        seed([HALL_STORED, OTHER])
        with pytest.raises(PolicyRejection):
            run_query_path(eq_sql(HALL), denied_columns=["Name"])


# ---------------------------------------------------------------------------
# The folding rules and the cache key, which the fix and the plural matching leave alone
# ---------------------------------------------------------------------------


class TestNormalizerIsUntouched:
    def test_the_version_is_the_one_the_cache_key_carries(self):
        assert NORMALIZER_VERSION == "2"
        assert QueryCache._question_key("x", "full", "p1", "s")[0] == "p1|persian-norm-v2"

    @pytest.mark.parametrize(
        ("raw", "folded", "compact"),
        [
            ("تالارها کدامند؟", "تالارها کدامند؟", "تالارهاکدامند؟"),
            ("تالار‌ها", "تالارها", "تالارها"),
            ("تالار ها", "تالار ها", "تالارها"),
            ("مشتری‌های فعال", "مشتریهای فعال", "مشتریهایفعال"),
            ("كارگزار ها و تهران",
             "کارگزار ها و تهران", "کارگزارهاوتهران"),
            ("حجم معاملات ۱۴۰۲ در تالارهای صادراتی", "حجم معاملات 1402 در تالارهای صادراتی",
             "حجممعاملات1402درتالارهایصادراتی"),
            ("ﻱﻛﺍ", "یکا", "یکا"),
            ("Top  Customers ", "top customers", "topcustomers"),
        ],
    )
    def test_the_folded_forms_are_unchanged(self, raw, folded, compact):
        assert normalize_for_matching(raw) == folded
        assert normalize_compact(raw) == compact

    def test_the_arabic_and_the_persian_letters_fold_to_the_persian_ones(self):
        assert normalize_for_matching(HALL_STORED) == normalize_for_matching(HALL) == HALL

    @pytest.mark.parametrize(
        ("question", "key"),
        [
            ("تالار‌ها", ("p1|persian-norm-v2", "تالارها", "full", "s")),
            ("تالار ها", ("p1|persian-norm-v2", "تالار ها", "full", "s")),
            ("تالارهای صادراتی؟", ("p1|persian-norm-v2", "تالارهای صادراتی؟", "full", "s")),
            ("كارگزار ها", ("p1|persian-norm-v2", "کارگزار ها", "full", "s")),
        ],
    )
    def test_the_cache_key_of_a_question_is_unchanged(self, question, key):
        assert QueryCache._question_key(question, "full", "p1", "s") == key
