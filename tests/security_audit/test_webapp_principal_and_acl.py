# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Findings 13 and 14 — the Flask app bypasses the column ACL.

``webapp/agent.py`` calls::

    run_query(question, system_prompt(), mode="full", interpret=interpret)

with no ``principal``. And ``api/runner.py`` reads that as *no restriction*::

    # None (no principal) applies no restriction -- pre-Phase-8 behaviour.
    denied_columns = principal.denied_columns if principal is not None else None

Proven live during the audit by sending the same denied-column query down
both paths against the same guard::

    FastAPI, principal with denied_columns=["NationalID"]
      -> guard verdict: REJECTED  (rule: denied column 'NationalID')

    Flask webapp/agent.answer_question(...)
      -> status: SUCCESS, row_count: 2, columns: ['Name', 'NationalID']

Same guard, same SQL, opposite outcome -- the only difference is that one
caller identified itself and the other did not. Every column restriction
the admin panel can set is void on the Flask path, which means the whole
two-role permission model, the panel that manages it and the
security-gated ACL endpoint are all unenforced for anyone logged into that
app.

``webapp/`` predates the Phase 8 principal model and was never connected to
it. It has no tests and is not in CI, which is why nobody noticed.

The fix must be fail-closed. ``run_query`` cannot start defaulting to a
restrictive ACL when no principal is passed -- the REPL (``app.py``) and
the test suite are legitimate principal-less callers, and changing that
default would silently alter behaviour far from here. The *caller* is what
must be fixed: the Flask path has real, named, logged-in users and is
obliged to say who they are.

Finding 14 rides along: ``answer_question`` defaults ``interpret=True``,
sending result rows to the model unless the caller opts out. Every other
entry point in this project defaults that to ``False``
(``AskTurnRequest.interpret``, ``QueryRequest.interpret``) precisely
because it is a data-governance decision.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "webapp"))


@pytest.fixture()
def agent_module():
    import webapp.agent as agent

    return agent


class TestTheFlaskPathIdentifiesItsCaller:
    def test_answer_question_accepts_a_principal(self, agent_module):
        """Without a parameter for it, the logged-in user's identity cannot
        reach the guard at all -- there is nowhere to put it."""
        params = inspect.signature(agent_module.answer_question).parameters
        assert "principal" in params, (
            "webapp/agent.answer_question takes no principal, so every query "
            "from the Flask app reaches run_query anonymous and unrestricted"
        )

    def test_run_query_is_called_with_a_principal(self, agent_module, monkeypatch):
        """The signature existing is not the same as it being used."""
        seen = {}

        def _spy(*args, **kwargs):
            seen["principal"] = kwargs.get("principal", "MISSING")
            raise RuntimeError("stop here -- we only care about the argument")

        monkeypatch.setattr(agent_module, "run_query", _spy)
        agent_module.answer_question("how many customers?", interpret=False)

        assert seen.get("principal") != "MISSING", (
            "answer_question still calls run_query without principal=, so "
            "denied_columns arrives as None and the ACL is not applied"
        )
        assert seen.get("principal") is not None, (
            "answer_question passes principal=None explicitly, which "
            "api/runner.py treats identically to passing nothing"
        )

    def test_the_principal_carries_the_acl(self, agent_module, monkeypatch):
        """A Principal with an empty denied_columns tuple would satisfy the
        two tests above and restrict nothing. What matters is that the
        column restriction actually travels."""
        seen = {}

        def _spy(*args, **kwargs):
            seen["principal"] = kwargs.get("principal")
            raise RuntimeError("stop")

        monkeypatch.setattr(agent_module, "run_query", _spy)
        agent_module.answer_question("q", interpret=False)

        principal = seen.get("principal")
        assert hasattr(principal, "denied_columns"), (
            "whatever answer_question passes as principal is not a Principal"
        )


class TestTheDefaultPostureIsRestrictive:
    """Whatever identity the Flask app supplies for a user it cannot map to
    a configured principal, it must not be a more permissive one than the
    FastAPI path would give the same person."""

    def test_an_unmapped_user_does_not_get_unrestricted_columns(self, agent_module, monkeypatch):
        seen = {}

        def _spy(*args, **kwargs):
            seen["principal"] = kwargs.get("principal")
            raise RuntimeError("stop")

        monkeypatch.setattr(agent_module, "run_query", _spy)
        agent_module.answer_question("q", interpret=False)

        denied = tuple(getattr(seen.get("principal"), "denied_columns", ()) or ())
        assert denied, (
            "the fallback principal restricts no columns, which reproduces the "
            "original finding with extra steps. An unidentified caller on a "
            "path with real logins should get the most restrictive posture, "
            "not the least"
        )


class TestInterpretationIsOptIn:
    """Finding 14. Sending result rows to the model is a governance choice,
    and this project made it opt-in everywhere else."""

    def test_answer_question_defaults_interpret_to_false(self, agent_module):
        default = inspect.signature(agent_module.answer_question).parameters["interpret"].default
        assert default is False, (
            f"webapp/agent.answer_question defaults interpret={default!r}. "
            "AskTurnRequest.interpret and QueryRequest.interpret both default "
            "to False because up to twenty rows of real results go to the "
            "model; this path should not be the exception"
        )

    def test_the_flask_view_does_not_force_it_on(self):
        """The form has a checkbox; the view must honour it rather than
        overriding it."""
        src = (_REPO_ROOT / "webapp" / "app.py").read_text(encoding="utf-8")
        assert "interpret=True" not in src.replace(" ", ""), (
            "webapp/app.py hard-codes interpret=True somewhere, overriding "
            "whatever the user chose"
        )
