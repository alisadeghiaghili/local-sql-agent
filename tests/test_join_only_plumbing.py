# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Join-only restrictions outside the guard: the prompt hint, access requests,
the value lookups and the key-store writer."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest

import config as cfg
from appdb.access_requests import approve_request, submit_request
from appdb.engine import dispose_app_engine
from appdb.key_store import issue_key, list_keys, update_denied_columns
import pandas as pd

from core.models import RetrievalContext
from llm.base import LLMBackend
from llm.router import build_prompt_segments
from llm.sql_agent import SQLAgent
from observability.audit import AuditRecord, save_audit_record
from security.auth import Principal
from security.column_policy import ColumnPolicyError
from security.sql_guard import CorrectableRejection
from tests._source_fixtures import routed_sources

_SYSTEM_PROMPT = "You are a T-SQL expert."
_ENTRY = "sales.Order.ID"

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _segments(denied=None, source=None):
    return build_prompt_segments(
        "how many orders?", _SYSTEM_PROMPT, RetrievalContext(), source=source,
        denied_columns=denied,
    )


class TestPromptHint:
    def test_the_hint_is_in_the_per_question_segment_only(self):
        with_hint = _segments([_ENTRY])
        assert _ENTRY in with_hint.question
        assert "COLUMN ACCESS" in with_hint.question
        assert _ENTRY not in with_hint.static_prefix
        assert with_hint.static_prefix  # the static path is in use

    def test_the_static_prefix_is_byte_identical_across_principals(self):
        plain = _segments(None)
        restricted = _segments([_ENTRY, "NationalID"])
        other = _segments(["main:ID"])
        assert plain.static_prefix == restricted.static_prefix == other.static_prefix

    def test_no_scoped_entry_means_a_byte_identical_prompt(self):
        plain = _segments(None)
        assert _segments([]) == plain
        assert _segments(["NationalID"]) == plain

    def test_it_sits_between_the_session_context_and_the_question(self):
        text = _segments([_ENTRY]).question
        assert text.index("SESSION CONTEXT") < text.index("COLUMN ACCESS") < text.index("USER QUESTION")

    def test_the_retrieval_path_carries_it_too(self):
        with cfg.override_settings(prompt_retrieval_token_budget=1):
            segments = _segments([_ENTRY])
        assert segments.static_prefix == ""
        assert _ENTRY in segments.question and "COLUMN ACCESS" in segments.question

    def test_only_entries_active_on_the_prompt_source_are_listed(self):
        with routed_sources({"Order": ("main", "archive")}):
            entries = ["archive:sales.Order.ID", "main:sales.Order.TotalAmount"]
            assert "sales.Order.ID" in _segments(entries, source="archive").question
            assert "TotalAmount" not in _segments(entries, source="archive").question
            assert "sales.Order.TotalAmount" in _segments(entries, source="main").question


class _ScriptedBackend(LLMBackend):
    """Answers with each scripted SQL in turn and keeps every prompt it was shown."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "scripted:test"

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._answers.pop(0)


class TestSqlAgentLoop:
    """The hint reaches the first prompt, and a join-only rejection is retried."""

    _BAD = "SELECT o.ID FROM sales.[Order] o"
    _GOOD = "SELECT c.Name FROM sales.[Order] o JOIN sales.Customer c ON o.ID = c.ID"

    def _agent(self, backend):
        return SQLAgent(
            backend=backend, execute_fn=lambda sql: pd.DataFrame({"Name": ["x"]}),
            max_corrections=2,
        )

    def test_first_prompt_has_the_hint_and_the_retry_succeeds(self):
        backend = _ScriptedBackend(self._BAD, self._GOOD)
        df, result = self._agent(backend).run(
            "q", _SYSTEM_PROMPT, denied_columns=(_ENTRY, "NationalID"),
        )
        assert len(backend.prompts) == 2, "a join-only rejection is correctable, so it is retried"
        assert "COLUMN ACCESS" in backend.prompts[0] and _ENTRY in backend.prompts[0]
        assert "JOIN ... ON a.col = b.col" in backend.prompts[1], "the correction names the rule"
        assert list(df["Name"]) == ["x"]

    def test_a_principal_without_scoped_entries_sees_no_hint(self):
        backend = _ScriptedBackend(self._GOOD)
        self._agent(backend).run("q", _SYSTEM_PROMPT, denied_columns=("NationalID",))
        assert "COLUMN ACCESS" not in backend.prompts[0]

    def test_when_the_budget_runs_out_it_is_a_refusal(self):
        backend = _ScriptedBackend(self._BAD, self._BAD, self._BAD)
        with pytest.raises(CorrectableRejection) as excinfo:
            self._agent(backend).run("q", _SYSTEM_PROMPT, denied_columns=(_ENTRY,))
        assert excinfo.value.reason == "join_only_column" and excinfo.value.is_refusal
        assert len(backend.prompts) == 3


@pytest.fixture()
def app_env(tmp_path):
    db_path = tmp_path / "app.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    project_dir = tmp_path / "project_config"
    shutil.copytree(_REPO_ROOT / "project_config.example", project_dir)
    with cfg.override_settings(
        app_db_url=f"sqlite:///{db_path}",
        log_dir=str(log_dir),
        api_keys_json="[]",
        project_config_dir=str(project_dir),
    ):
        dispose_app_engine()
        yield
    dispose_app_engine()


def _audit(session_id: str, turn_id: str, reason: str, subject: str) -> None:
    save_audit_record(AuditRecord(
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        request_id="r_1",
        question="q",
        generated_sql="SELECT 1",
        guard={"verdict": "rejected", "reason": reason, "subject": subject},
        row_count=0,
        session_id=session_id,
        turn_id=turn_id,
    ))


class TestAccessRequestsForJoinOnlyEntries:
    def test_the_request_stores_the_whole_entry(self, app_env):
        _audit("s1", "t1", "join_only_column", _ENTRY)
        row, created = submit_request(session_id="s1", turn_id="t1", requester_principal_id="p")
        assert created and row["column_name"] == _ENTRY

    def test_approval_removes_exactly_that_entry(self, app_env):
        _, key = issue_key("analyst-1", "Analyst")
        update_denied_columns(key["key_sha256"], ["ID", _ENTRY, "default:ID", "sales.Customer.ID"])
        _audit("s2", "t2", "join_only_column", _ENTRY)
        row, _ = submit_request(session_id="s2", turn_id="t2", requester_principal_id="analyst-1")
        _, updated = approve_request(row["request_id"], actor_principal_id="security-1")
        assert updated == 1
        stored = next(k for k in list_keys() if k["key_sha256"] == key["key_sha256"])
        assert stored["denied_columns"] == ["ID", "default:ID", "sales.Customer.ID"]

    def test_a_denied_column_request_still_works(self, app_env):
        _audit("s3", "t3", "denied_column", "NationalID")
        row, created = submit_request(session_id="s3", turn_id="t3", requester_principal_id="p")
        assert created and row["column_name"] == "NationalID"

    def test_other_reasons_are_still_refused(self, app_env):
        from appdb.access_requests import NotDeniedColumnError

        _audit("s4", "t4", "unknown_table", "Nope")
        with pytest.raises(NotDeniedColumnError):
            submit_request(session_id="s4", turn_id="t4", requester_principal_id="p")


class TestKeyStoreWriter:
    def test_a_bad_scoped_entry_is_refused_before_anything_is_written(self, app_env):
        _, key = issue_key("analyst-1", "Analyst")
        before = next(k for k in list_keys() if k["key_sha256"] == key["key_sha256"])["denied_columns"]
        with pytest.raises(ColumnPolicyError, match="sales.Nope.ID"):
            update_denied_columns(key["key_sha256"], ["sales.Nope.ID"])
        after = next(k for k in list_keys() if k["key_sha256"] == key["key_sha256"])["denied_columns"]
        assert after == before


class TestValueLookupsHideJoinOnlyColumns:
    def test_the_resolver_skips_a_join_only_resolvable_column(self, app_env):
        import pandas as pd

        from retrieval.value_resolver import resolve_value
        from schema_data.registry import get_resolvable_columns, get_table_schema_qualifiers

        table = next(iter(get_resolvable_columns()))
        column = get_resolvable_columns()[table][0]
        schema = get_table_schema_qualifiers().get(table, "")
        entry = f"{schema}.{table}.{column}" if schema else f"{table}.{column}"
        calls: list[str] = []

        def execute(sql, params):
            calls.append(sql)
            return pd.DataFrame({column: []})

        resolve_value("x", [table], principal=Principal(id="p", name="P"), execute_fn=execute)
        assert calls, "the unrestricted principal's lookup must reach the database"

        calls.clear()
        restricted = Principal(id="p", name="P", denied_columns=(entry,))
        result = resolve_value("x", [table], principal=restricted, execute_fn=execute)
        assert not calls
        assert result.status == "no_match" and result.miss_reason == "denied_by_acl"
