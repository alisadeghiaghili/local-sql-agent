"""Tests for llm/structured_schema.py — the Pydantic-backed structured schema."""

from __future__ import annotations

import pytest

from llm.structured_schema import (
    SQL_GENERATION_SCHEMA,
    SchemaViolationError,
    SqlGeneration,
    sql_from_structured,
)


class TestSchema:
    """Detail 1 from the schema-consolidation brief: OpenAI strict
    ``json_schema`` mode requires ``additionalProperties: false`` on every
    object AND every property listed under ``required`` -- verified here
    directly on the generated schema, not assumed."""

    def test_additional_properties_is_false(self):
        assert SQL_GENERATION_SCHEMA["additionalProperties"] is False

    def test_required_lists_every_property(self):
        assert sorted(SQL_GENERATION_SCHEMA["required"]) == sorted(
            SQL_GENERATION_SCHEMA["properties"]
        )

    def test_properties_present(self):
        props = SQL_GENERATION_SCHEMA["properties"]
        assert set(props) == {"sql", "out_of_scope", "confidence", "assumptions"}

    def test_no_defs_or_refs(self):
        """Detail 2: the model is kept flat so no grammar backend has to
        resolve $defs/$ref -- verified by their absence, not assumed."""
        assert "$defs" not in SQL_GENERATION_SCHEMA
        assert "$ref" not in SQL_GENERATION_SCHEMA


class TestSqlFromStructured:
    def test_returns_sql_when_in_scope(self):
        assert sql_from_structured({"sql": "SELECT 1", "out_of_scope": False}) == "SELECT 1"

    def test_raises_out_of_scope_sentinel(self):
        with pytest.raises(ValueError, match="OUT_OF_SCOPE"):
            sql_from_structured({"sql": "", "out_of_scope": True})

    def test_missing_sql_key_defaults_to_empty_string(self):
        """Every SqlGeneration field has a default, so a response missing
        "sql" entirely is not a validation failure at the Pydantic layer --
        the wire schema's own "required" (see TestSchema) is what asks the
        endpoint not to omit it in the first place."""
        assert sql_from_structured({"out_of_scope": False}) == ""

    def test_missing_out_of_scope_defaults_to_false(self):
        assert sql_from_structured({"sql": "SELECT 1"}) == "SELECT 1"


class TestSchemaViolation:
    """Detail 3: a schema-valid-JSON-but-model-invalid response is a real,
    distinguishable state (mappable to finish_reason="schema_violation"),
    not a bare pydantic.ValidationError (or, before this, a KeyError)
    escaping untranslated."""

    def test_extra_key_raises_schema_violation(self):
        with pytest.raises(SchemaViolationError):
            sql_from_structured({"sql": "SELECT 1", "out_of_scope": False, "extra": 1})

    def test_wrong_type_raises_schema_violation(self):
        with pytest.raises(SchemaViolationError):
            sql_from_structured({"sql": "SELECT 1", "out_of_scope": "not-a-bool"})

    def test_schema_violation_is_a_value_error_but_not_the_out_of_scope_sentinel(self):
        """A SchemaViolationError must never be mistaken for the
        OUT_OF_SCOPE sentinel by callers switching on str(exc) (see
        llm.router._is_out_of_scope)."""
        try:
            sql_from_structured({"sql": "SELECT 1", "out_of_scope": False, "extra": 1})
        except SchemaViolationError as exc:
            assert isinstance(exc, ValueError)
            assert str(exc) != "OUT_OF_SCOPE"
        else:
            pytest.fail("expected SchemaViolationError")


class TestSqlGeneration:
    def test_defaults(self):
        obj = SqlGeneration()
        assert obj.sql == ""
        assert obj.out_of_scope is False
        assert obj.confidence == 1.0
        assert obj.assumptions == []
