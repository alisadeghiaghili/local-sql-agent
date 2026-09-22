# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Contract tests for the two additive fields this phase adds (per the PR
this accompanies) to the v2 Turn contract (``docs/api-contract-v2.md`` §4):

* ``session.models.TurnErrorInfo.request_id`` — so a failed turn's UI
  (``web/js/render/turn.js``) has something for an analyst to quote back
  to an operator, matching the id ``session.engine.TurnEngine._write_audit``
  logs the SAME turn against (never a second, independently-minted id).
* ``session.models.GuardVerdict.reason``/``subject`` — structured refusal
  data, captured at the exact ``security.sql_guard`` raise site that
  already knows it, so the web UI can offer
  ``docs/design/DESIGN-INVARIANTS.md`` §8's targeted "Ask without that
  column" action for a denied-column refusal without regexing
  ``GuardVerdict.rule``'s free text (which stays exactly as-is — the audit
  trail and ``tests/test_sql_guard*.py`` depend on its literal wording).

Every test below was run against the pre-phase code (neither field
existed) and failed there — ``TurnErrorInfo``/``GuardVerdict`` rejected
the unknown keyword, and ``SqlGuardRejection`` had no ``reason``/
``subject`` attributes at all (a bare ``AttributeError`` from
``getattr``'s default-less form, or a construction ``TypeError`` for the
keyword-argument tests) — see the PR description for that run's output.

Uses ``project_config.example``'s schema throughout: it defines the same
``Customer(ID, Name, NationalID, IsActive)`` shape the real domain uses,
so these tests run in CI / a fresh clone exactly like
``tests/test_session_engine_error_paths.py`` already does, with no
``domain_data`` marker needed.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError
from unittest.mock import patch

from llm.providers import MockBackend
from llm.router import LLMRouter
from security.sql_guard import (
    CorrectableRejection,
    PolicyRejection,
    SqlGuardRejection,
    validate_sql,
)
from session.engine import TurnEngine
from session.models import GuardVerdict, TurnErrorInfo
from session.store import SessionStore

SYSTEM_PROMPT = "You are a T-SQL expert."


# ---------------------------------------------------------------------------
# security.sql_guard.SqlGuardRejection — the reason/subject carrier itself
# ---------------------------------------------------------------------------


class TestSqlGuardRejectionCarriesStructure:
    def test_reason_and_subject_round_trip(self):
        exc = PolicyRejection("boom", reason="denied_column", subject="NationalID")
        assert exc.reason == "denied_column"
        assert exc.subject == "NationalID"
        assert str(exc) == "boom"  # message untouched by the new attributes

    def test_reason_and_subject_default_to_none(self):
        exc = CorrectableRejection("boom")
        assert exc.reason is None
        assert exc.subject is None

    def test_unknown_reason_is_rejected_at_construction(self):
        """A typo'd reason must fail loudly at the raise site, in the same
        test run that exercises it — not silently produce a `GuardVerdict`
        no UI branch matches in production."""
        with pytest.raises(AssertionError):
            SqlGuardRejection("boom", reason="denied_col")  # not "denied_column"


# ---------------------------------------------------------------------------
# security.sql_guard.validate_sql — reason/subject at each raise site
# ---------------------------------------------------------------------------


class TestValidateSqlReasonSubject:
    def test_denied_column_named_directly(self):
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql("SELECT NationalID FROM [Customer]", denied_columns={"NationalID"})
        assert exc_info.value.reason == "denied_column"
        assert exc_info.value.subject == "NationalID"

    def test_denied_column_via_star_single_exposure_names_the_column(self):
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql("SELECT * FROM [Customer]", denied_columns={"NationalID"})
        assert exc_info.value.reason == "denied_column"
        # Lower-cased, exactly like the free-text message this raise site
        # already builds (`_COLUMNS_BY_TABLE` stores lower-cased column
        # names) -- `subject` mirrors what the guard actually knows here,
        # not a nicer-looking re-casing invented for this attribute.
        assert exc_info.value.subject == "nationalid"

    def test_denied_column_via_star_multiple_exposure_has_no_single_subject(self):
        """More than one denied column exposed at once -- no single column
        whose removal would fix the question, so `subject` stays `None`
        rather than picking one of several arbitrarily."""
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql(
                "SELECT * FROM [Customer]", denied_columns={"NationalID", "Name"},
            )
        assert exc_info.value.reason == "denied_column"
        assert exc_info.value.subject is None

    def test_unresolvable_star_under_a_denied_columns_policy_has_no_subject(self):
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql(
                "SELECT * FROM (SELECT 1 AS x) AS derived",
                denied_columns={"NationalID"},
            )
        assert exc_info.value.reason == "denied_column"
        assert exc_info.value.subject is None

    def test_forbidden_statement_root_type_names_the_keyword(self):
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql("DROP TABLE [Customer]")
        assert exc_info.value.reason == "forbidden_statement"
        assert exc_info.value.subject == "DROP"

    def test_forbidden_statement_in_stacked_query_names_the_keyword(self):
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql("SELECT 1; DROP TABLE [Customer]")
        assert exc_info.value.reason == "forbidden_statement"
        assert exc_info.value.subject == "DROP"

    def test_dangerous_function_is_a_forbidden_statement(self):
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql("SELECT * FROM OPENROWSET('x','y','z')")
        assert exc_info.value.reason == "forbidden_statement"

    def test_system_catalogue_names_the_catalogue(self):
        with pytest.raises(PolicyRejection) as exc_info:
            validate_sql("SELECT * FROM INFORMATION_SCHEMA.TABLES")
        assert exc_info.value.reason == "system_catalogue"
        assert exc_info.value.subject == "INFORMATION_SCHEMA"

    def test_unknown_table_names_the_table(self):
        with pytest.raises(CorrectableRejection) as exc_info:
            validate_sql("SELECT * FROM ThisTableDoesNotExist_zzz")
        assert exc_info.value.reason == "unknown_table"
        assert exc_info.value.subject == "ThisTableDoesNotExist_zzz"
        assert exc_info.value.is_refusal is True  # the other axis, unaffected by this phase

    def test_a_disallowed_comment_is_reason_other(self):
        with pytest.raises(CorrectableRejection) as exc_info:
            validate_sql("SELECT * FROM [Customer] -- a note")
        assert exc_info.value.reason == "other"

    def test_empty_sql_is_reason_other(self):
        with pytest.raises(CorrectableRejection) as exc_info:
            validate_sql("")
        assert exc_info.value.reason == "other"


# ---------------------------------------------------------------------------
# session.models — the two additive fields themselves
# ---------------------------------------------------------------------------


class TestTurnErrorInfoRequestId:
    def test_defaults_to_none(self):
        info = TurnErrorInfo(code="X", message="y")
        assert info.request_id is None

    def test_round_trips_through_model_dump(self):
        info = TurnErrorInfo(code="X", message="y", request_id="r_abc123")
        assert info.model_dump()["request_id"] == "r_abc123"


class TestGuardVerdictReasonSubject:
    def test_default_to_none(self):
        v = GuardVerdict(verdict="allowed")
        assert v.reason is None
        assert v.subject is None

    def test_round_trip_through_model_dump(self):
        v = GuardVerdict(verdict="rejected", rule="...", reason="denied_column", subject="NationalID")
        dumped = v.model_dump()
        assert dumped["reason"] == "denied_column"
        assert dumped["subject"] == "NationalID"

    def test_reason_is_restricted_to_the_closed_set(self):
        with pytest.raises(ValidationError):
            GuardVerdict(verdict="rejected", reason="not_a_real_reason")


# ---------------------------------------------------------------------------
# session.engine.TurnEngine — wiring the two fields end to end
# ---------------------------------------------------------------------------


class TestTurnEngineWiresReasonSubjectOntoGuard:
    def test_denied_column_rejection_carries_reason_and_subject_on_the_turn(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()
        engine = TurnEngine(
            router=LLMRouter(default_chain=[MockBackend(response="SELECT NationalID FROM Customer")]),
            execute_fn=lambda sql: pd.DataFrame(),  # never reached -- the guard rejects first
        )

        turn = engine.ask(
            record, "کد ملی مشتری‌ها را نشان بده", SYSTEM_PROMPT,
            denied_columns=("NationalID",),
        )

        assert turn.guard is not None
        assert turn.guard.verdict == "rejected"
        assert turn.guard.reason == "denied_column"
        assert turn.guard.subject == "NationalID"
        # The free-text `rule` stays exactly as sql_guard built it --
        # unchanged by this phase, still what the audit trail keys off.
        assert "NationalID" in (turn.guard.rule or "")
        # A guard rejection is not a `turn.error` on this path (see
        # web/js/render/turn.js's `isGuardRejected` docstring) -- unaffected
        # by this phase, asserted here so a future change to that shape is
        # caught by the same test that pins the new fields.
        assert turn.error is None


class TestTurnErrorRequestIdMatchesTheAuditedId:
    def test_explicit_request_id_lands_on_turn_error_and_the_audit_record(self):
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()

        class _AlwaysBrokenBackend:
            name = "broken"

            def generate_with_meta_segments(self, segments):
                raise RuntimeError("connection refused")

        engine = TurnEngine(
            router=LLMRouter(default_chain=[_AlwaysBrokenBackend()]),
            execute_fn=lambda sql: pd.DataFrame(),
        )

        with patch("session.engine.save_audit_record") as mock_save:
            turn = engine.ask(record, "چیزی", SYSTEM_PROMPT, request_id="r_explicit_42")

        assert turn.error is not None
        assert turn.error.code == "MODEL_UNAVAILABLE"
        assert turn.error.request_id == "r_explicit_42"
        assert mock_save.call_count == 1
        audited = mock_save.call_args[0][0]
        assert audited.request_id == "r_explicit_42" == turn.error.request_id

    def test_auto_minted_request_id_still_matches_the_audit_record(self):
        """No caller-supplied id -- TurnEngine mints its own, but it must
        still be the SAME id on both `turn.error` and the audit record,
        never two independently-minted ones."""
        store = SessionStore(ttl_seconds=60, max_size=10, max_turns=10)
        record = store.create()

        class _AlwaysBrokenBackend:
            name = "broken"

            def generate_with_meta_segments(self, segments):
                raise RuntimeError("connection refused")

        engine = TurnEngine(
            router=LLMRouter(default_chain=[_AlwaysBrokenBackend()]),
            execute_fn=lambda sql: pd.DataFrame(),
        )

        with patch("session.engine.save_audit_record") as mock_save:
            turn = engine.ask(record, "چیزی", SYSTEM_PROMPT)

        assert turn.error is not None
        assert turn.error.request_id  # non-empty, minted fallback id
        audited = mock_save.call_args[0][0]
        assert audited.request_id == turn.error.request_id


class TestReasonEnumIsDocumented:
    """Every rejection reason the guard can emit must appear in the v2
    contract document's ``reason`` enum.

    ``docs/api-contract-v2.md`` §4 spells the enum out so a client author
    knows the closed set to switch on. Nothing enforced that list against
    the code, so adding a reason to ``security.sql_guard._REASONS`` (and
    to ``session.models.GuardVerdict``'s mirrored ``Literal``) left the
    document silently stale -- which is exactly what happened when
    ``no_table_reference`` was introduced. This closes that drift: the
    two-place registration the guard already requires becomes a
    three-place one, and forgetting the third fails the build rather than
    shipping a contract that under-reports what the API can return.
    """

    def test_every_guard_reason_appears_in_the_contract_document(self):
        from security.sql_guard import _REASONS

        doc = (
            Path(__file__).resolve().parent.parent
            / "docs" / "api-contract-v2.md"
        ).read_text(encoding="utf-8")

        missing = sorted(r for r in _REASONS if f'"{r}"' not in doc)
        assert not missing, (
            "docs/api-contract-v2.md does not document these guard "
            f"rejection reasons: {missing}. Add them to the `reason` enum "
            "in §4 -- a client switching on that enum would not know the "
            "API can return them."
        )

    def test_rejected_sql_field_is_documented(self):
        """``session.models.GuardVerdict.rejected_sql`` (the "See the SQL"
        action DESIGN-INVARIANTS.md §8's failure-anatomy table names for a
        guard rejection) must appear in the §4 `guard` object, same as
        `reason`/`subject` above -- a client reading only the document
        must be able to discover the field exists."""
        doc = (
            Path(__file__).resolve().parent.parent
            / "docs" / "api-contract-v2.md"
        ).read_text(encoding="utf-8")

        assert '"rejected_sql"' in doc, (
            "docs/api-contract-v2.md's §4 `guard` object does not document "
            "`rejected_sql` -- a client would not know the field exists."
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
