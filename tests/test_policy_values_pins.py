# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Pins: what a deployment whose policy files already hold real values gets today.

``memory_policy.yaml`` (the closed set of preferences an analyst may pin) and
``session_policy.yaml`` (the scope a question gets when it names none, and the
options offered to change it) are read by ``knowledge.memory_policy`` and
``knowledge.session_policy``. 6.16.1 adds a command that drafts their values
and a preflight check that says when they are still the template's, and changes
no runtime behaviour. These tests hold that: given a policy file, a pin is
accepted or refused, and a clarification reads, exactly as it did before.

They install the policies by text, through the loaders' own validators, so they
describe a deployment with real values (``Region``, ``Region A``) without a
``project_config/`` of its own.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

import api.server as server_module
import api.v2_routes as v2_routes
import knowledge.memory_policy as memory_policy
import knowledge.session_policy as session_policy
from config import override_settings
from knowledge.config_loader import MemoryPolicyConfig, SessionPolicyConfig, validate_yaml_text
from session import ambiguity
from session.ambiguity import assumptions_for_fresh
from session.memory import MemoryValidationError, validate_memory_value

MEMORY_YAML = """\
keys:
  scope:
    filter_key: "Region"
    field_name: "region"
    column: "Name"
    options:
      - "Region A"
      - "Region B"
    max_length: 20
  free:
    filter_key: "Region"
    field_name: "free_region"
    column: "Name"
    max_length: 12
"""

SESSION_YAML = """\
default_scope:
  filter_key: "Region"
  field_name: "region"
  default_label: "All regions"
  options:
    - "All regions"
    - "Region A"
    - "Region B"
  clarification_prompt: "Which region do you mean?"
"""

def _triple(assumption) -> tuple[str, str, str]:
    return assumption.field, assumption.value, assumption.source


RANKING_QUESTION = "۱۰ مشتری برتر را نشان بده"
TODAY = date(2026, 8, 27)


@pytest.fixture()
def real_policies(monkeypatch):
    """Install the two policies above for the test, and put them back after it.

    ``session.ambiguity`` binds the default-scope names when it is imported, so
    they are replaced there as well as in the loader's cache.
    """
    memory = validate_yaml_text("memory_policy.yaml", MEMORY_YAML, MemoryPolicyConfig)
    session = validate_yaml_text("session_policy.yaml", SESSION_YAML, SessionPolicyConfig).default_scope
    saved_memory = dict(memory_policy._cache)
    saved_session = dict(session_policy._cache)
    memory_policy._cache.clear()
    memory_policy._cache["keys"] = {
        name: memory_policy.MemoryKey(
            entry.filter_key, entry.field_name, entry.column, tuple(entry.options), entry.max_length,
        )
        for name, entry in memory.keys.items()
    }
    session_policy._cache.clear()
    session_policy._cache.update({
        "DEFAULT_SCOPE_FILTER_KEY": session.filter_key,
        "DEFAULT_SCOPE_FIELD_NAME": session.field_name,
        "DEFAULT_SCOPE_LABEL": session.default_label,
        "DEFAULT_SCOPE_OPTIONS": tuple(session.options),
        "DEFAULT_SCOPE_CLARIFICATION_PROMPT": session.clarification_prompt,
        "_loaded": True,
    })
    for name, value in (
        ("DEFAULT_SCOPE_FILTER_KEY", session.filter_key),
        ("DEFAULT_SCOPE_FIELD_NAME", session.field_name),
        ("DEFAULT_SCOPE_LABEL", session.default_label),
        ("DEFAULT_SCOPE_OPTIONS", tuple(session.options)),
        ("DEFAULT_SCOPE_CLARIFICATION_PROMPT", session.clarification_prompt),
    ):
        monkeypatch.setattr(ambiguity, name, value)
    yield
    memory_policy._cache.clear()
    memory_policy._cache.update(saved_memory)
    session_policy._cache.clear()
    session_policy._cache.update(saved_session)


class TestMemoryPinIsJudgedByThePolicyFile:
    def test_a_declared_option_is_accepted(self, real_policies):
        validate_memory_value("scope", "Region A")
        validate_memory_value("scope", "Region B")

    def test_a_value_outside_the_closed_set_is_refused_with_todays_message(self, real_policies):
        with pytest.raises(MemoryValidationError) as caught:
            validate_memory_value("scope", "Region C")
        assert str(caught.value) == "Value 'Region C' for key 'scope' is not one of the declared options."

    def test_the_default_label_is_not_an_option(self, real_policies):
        with pytest.raises(MemoryValidationError):
            validate_memory_value("scope", "All regions")

    def test_the_match_is_exact_not_normalised(self, real_policies):
        for near in ("region a", "Region  A", "Region A ", "Region‌A"):
            with pytest.raises(MemoryValidationError):
                validate_memory_value("scope", near)

    def test_the_length_cap_comes_before_the_option_check(self, real_policies):
        with pytest.raises(MemoryValidationError) as caught:
            validate_memory_value("scope", "x" * 21)
        assert str(caught.value) == "Value for key 'scope' exceeds max_length=20."

    def test_a_key_without_options_takes_any_value_up_to_its_cap(self, real_policies):
        validate_memory_value("free", "any region")
        with pytest.raises(MemoryValidationError) as caught:
            validate_memory_value("free", "x" * 13)
        assert str(caught.value) == "Value for key 'free' exceeds max_length=12."

    def test_an_undeclared_key_and_a_control_character_are_refused(self, real_policies):
        with pytest.raises(MemoryValidationError) as caught:
            validate_memory_value("ring", "Region A")
        assert str(caught.value) == "Unknown memory key: 'ring'"
        with pytest.raises(MemoryValidationError):
            validate_memory_value("scope", "Region A\n")


class TestDefaultScopeIsReadFromThePolicyFile:
    def test_a_ranking_question_naming_no_scope_gets_the_default_label_and_the_options(self, real_policies):
        assumptions, clarifications, ambiguous = assumptions_for_fresh(RANKING_QUESTION, {}, today=TODAY)
        assert ambiguous is True
        assert _triple(assumptions[0]) == ("region", "All regions", "default")
        first = clarifications[0]
        assert (first.field, first.prompt, first.options) == (
            "region", "Which region do you mean?", ["All regions", "Region A", "Region B"],
        )

    def test_a_scope_named_in_the_question_is_not_offered_again(self, real_policies):
        assumptions, clarifications, _ = assumptions_for_fresh(RANKING_QUESTION, {"Region": "Region B"}, today=TODAY)
        assert _triple(assumptions[0]) == ("region", "Region B", "question")
        assert all(c.field != "region" for c in clarifications)

    def test_a_question_that_is_not_a_ranking_has_nothing_to_default(self, real_policies):
        assert assumptions_for_fresh("معاملات را نشان بده", {}, today=TODAY) == ([], [], False)


RAW_KEY = "policy-values-pin-principal-" + "0" * 10


@pytest.fixture()
def client(tmp_path, real_policies):
    """One authenticated caller and a real session store on a temporary file."""
    server_module._system_prompt = "stub system prompt"
    v2_routes._system_prompt = "stub system prompt"
    keys = json.dumps([{
        "id": "analyst-1", "name": "Analyst One",
        "key_sha256": hashlib.sha256(RAW_KEY.encode("utf-8")).hexdigest(),
    }])
    with override_settings(
        auth_required=True, api_keys_json=keys, session_store_path=str(tmp_path / "sessions.db"),
    ):
        v2_routes._reset_for_testing()
        yield TestClient(server_module.app, raise_server_exceptions=False)
        v2_routes._reset_for_testing()


HEADERS = {"Authorization": f"Bearer {RAW_KEY}"}


class TestPutMemoryEndpoint:
    def test_a_declared_option_is_stored_and_listed(self, client):
        reply = client.put("/v2/memory/scope", json={"value": "Region A"}, headers=HEADERS)
        assert reply.status_code == 200
        assert (reply.json()["key"], reply.json()["field"], reply.json()["value"]) == ("scope", "region", "Region A")
        listing = client.get("/v2/memory", headers=HEADERS).json()
        assert [(e["key"], e["value"]) for e in listing["entries"]] == [("scope", "Region A")]
        assert {r["key"]: r["options"] for r in listing["rememberable"]} == {
            "scope": ["Region A", "Region B"], "free": [],
        }

    def test_a_value_outside_the_options_is_422_and_stores_nothing(self, client):
        reply = client.put("/v2/memory/scope", json={"value": "Region C"}, headers=HEADERS)
        assert reply.status_code == 422
        assert reply.json()["detail"] == "Value 'Region C' for key 'scope' is not one of the declared options."
        assert client.get("/v2/memory", headers=HEADERS).json()["entries"] == []
