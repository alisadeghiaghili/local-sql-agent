# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pydantic-backed schema for Phase 2 task 3's constrained SQL-generation output.

With deterministic decoding in place (Phase 2 task 2), the next latency/
quality lever is asking the model for one schema-shaped JSON object
instead of free text plus :func:`security.sql_guard.clean_sql`'s
fence-stripping / preamble-removal regex surgery and the ``OUT_OF_SCOPE``
string sentinel. Whether that trade is actually *worth* it is an empirical
question the golden-set evaluation answers (see ``eval/cli.py``'s
``--structured`` flag) — this module only defines the schema and the
small amount of glue needed to evaluate both paths; it does not decide
which one wins, and the feature stays off by default (see
``config.Settings.llm_structured_output``) until that evaluation says
otherwise.

One model, not two definitions of the same contract
------------------------------------------------------
:data:`SqlGeneration` is the single source of truth: the same Pydantic
model both generates the JSON Schema sent to the endpoint
(:data:`SQL_GENERATION_SCHEMA`, via :meth:`~pydantic.BaseModel.model_json_schema`)
and validates what comes back (:func:`sql_from_structured`, via
:meth:`~pydantic.BaseModel.model_validate`). A hand-written schema dict
alongside separate hand-written parsing code can drift silently — the
model returns something the schema permitted and the parser then
disagrees about it — which is exactly the failure mode a single
generate-and-validate model closes by construction.

Two details in :func:`_to_strict_json_schema` matter enough to call out
explicitly:

1. OpenAI-compatible *strict* ``json_schema`` mode requires
   ``additionalProperties: false`` on every object **and** every property
   listed in ``required`` — Pydantic's ``model_config = {"extra":
   "forbid"}`` already emits the former, but Pydantic only lists a field
   in ``required`` when it has no default, so a model whose fields all
   have defaults (this one) needs its ``required`` list built explicitly
   from every property name, not taken from Pydantic's own output as-is.
2. Pydantic emits ``$ref``/``$defs`` for nested models; grammar backends
   behind an OpenAI-compatible server vary in whether they resolve those
   (llama.cpp's GBNF conversion in particular has historically not).
   :data:`SqlGeneration` is kept deliberately flat (no nested model
   fields) to sidestep the question entirely rather than inline
   ``$defs`` after the fact.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_OUT_OF_SCOPE_SENTINEL = "OUT_OF_SCOPE"


class SqlGeneration(BaseModel):
    """The single source of truth for the constrained SQL-generation output.

    Deliberately flat — no nested model fields — so the JSON Schema this
    generates has no ``$defs``/``$ref`` for a grammar backend to (maybe)
    not resolve. See the module docstring.

    Examples
    --------
    >>> SqlGeneration(sql="SELECT 1", out_of_scope=False).sql
    'SELECT 1'
    >>> SqlGeneration().out_of_scope
    False
    """

    model_config = ConfigDict(extra="forbid")

    sql: str = Field(
        default="", description="The generated T-SQL query. Empty when out_of_scope is true."
    )
    out_of_scope: bool = Field(
        default=False, description="True if the question cannot be answered from the known schema."
    )
    confidence: float = Field(
        default=1.0, description="The model's own confidence in this SQL, 0.0-1.0."
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Any assumptions the model made to resolve ambiguity in the question.",
    )


def _to_strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Post-process ``model.model_json_schema()`` for OpenAI strict mode.

    Strict ``json_schema`` mode requires ``additionalProperties: false``
    (Pydantic's ``extra="forbid"`` already emits this) **and** every
    property name listed under ``required``, even ones with a default —
    Pydantic only lists genuinely-mandatory fields there by default, so
    this rebuilds ``required`` from every property name explicitly rather
    than trusting Pydantic's own list.

    Examples
    --------
    >>> schema = _to_strict_json_schema(SqlGeneration)
    >>> schema["additionalProperties"]
    False
    >>> sorted(schema["required"]) == sorted(schema["properties"])
    True
    >>> "$defs" in schema
    False
    """
    schema = model.model_json_schema()
    schema.pop("$defs", None)  # belt-and-braces: SqlGeneration is flat by design
    # The class docstring ends up in "description" and every field's own
    # name in "title" -- neither is needed by the endpoint (the field
    # "description"s already carry the guidance that matters) and both
    # just add tokens to a payload sent on every call.
    schema.pop("description", None)
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}))
    return schema


#: The schema every structured SQL-generation call asks for — generated
#: from, and validated against, :class:`SqlGeneration`.
SQL_GENERATION_SCHEMA: dict[str, Any] = _to_strict_json_schema(SqlGeneration)


class SchemaViolationError(ValueError):
    """A response was valid JSON matching the wire schema but failed the model.

    This is a real, expected outcome — not a bug — for a structured-output
    call: an endpoint's constrained decoding guarantees the *shape* named
    in the JSON Schema (right keys, right JSON types), not the additional
    constraints :class:`SqlGeneration` enforces via Pydantic (e.g.
    ``extra="forbid"`` rejecting a stray key the schema's own
    ``additionalProperties: false`` should have already blocked, but a
    lenient backend didn't). A caller building the ``llm`` status block
    (``docs/api-contract-v2.md`` §6) should map this to
    ``finish_reason: "schema_violation"`` — the contract already names
    that value for exactly this state — rather than letting the
    underlying :class:`pydantic.ValidationError` escape untranslated.
    """


def sql_from_structured(obj: dict[str, Any]) -> str:
    """Validate *obj* against :class:`SqlGeneration` and extract its SQL.

    Parameters
    ----------
    obj:
        A dict shaped like :data:`SQL_GENERATION_SCHEMA`, e.g. from
        :meth:`~llm.base.LLMBackend.generate_structured`.

    Returns
    -------
    str
        The validated ``sql`` field.

    Raises
    ------
    ValueError("OUT_OF_SCOPE")
        If ``out_of_scope`` is true — the same sentinel
        :class:`~llm.base.LLMBackend` raises on the text path, so callers
        (:class:`~llm.sql_agent.SQLAgent`, ``eval/runner.py``) don't need a
        second out-of-scope contract to handle.
    SchemaViolationError
        If *obj* does not validate against :class:`SqlGeneration` (a
        missing/extra/wrong-typed field) — a real, distinguishable state,
        not a :class:`KeyError` or a bare :class:`pydantic.ValidationError`
        escaping untranslated. See that class's docstring.

    Examples
    --------
    >>> sql_from_structured({"sql": "SELECT 1", "out_of_scope": False})
    'SELECT 1'
    >>> sql_from_structured({"sql": "", "out_of_scope": True})
    Traceback (most recent call last):
        ...
    ValueError: OUT_OF_SCOPE
    >>> sql_from_structured(  # doctest: +ELLIPSIS
    ...     {"sql": "SELECT 1", "out_of_scope": False, "extra_key": 1}
    ... )
    Traceback (most recent call last):
        ...
    llm.structured_schema.SchemaViolationError: ...
    """
    try:
        parsed = SqlGeneration.model_validate(obj)
    except ValidationError as exc:
        raise SchemaViolationError(
            f"Structured response did not match SqlGeneration: {exc}"
        ) from exc
    if parsed.out_of_scope:
        raise ValueError(_OUT_OF_SCOPE_SENTINEL)
    return parsed.sql
