# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Guard against a real warehouse name creeping back into first-party source.

PRs #38-#40 and this phase's own commits moved this warehouse's schema, its
resolver allowlists, its tuning knobs, its retrieval hints and its default
session-scope policy out of Python and into ``project_config/`` — the whole
point being that the *engine* (``api``, ``llm``, ``security``, ``session``,
``database``, ``core``, ``knowledge``, ``prompt_engine``, ``retrieval``,
``schema_data``, ``logs``, ``exporters``, ``observability``, ``eval``,
``config.py``) no longer needs to know this deployment's real table names to
run. A rule nobody checks decays — exactly the reasoning behind
``tests/test_persian_normalization.py``'s ``TestSingleTranslationTable`` and
``tests/test_tuning_layer.py``, which this module is modelled on and walks
the same first-party source tree as (see ``_FIRST_PARTY_DIRS`` below).

What counts as a violation
---------------------------
An **executable string literal** — anything read by running code, not by a
human — that names this warehouse: contains one of ``_FORBIDDEN_IDENTIFIERS``
as a substring.

Deliberately a short, unmistakable identifier list, not a wordlist.
``Order``, ``Customer``, ``Date`` and ``Contract`` are ordinary English
words; project_config.example/'s own generic schema uses several of them
for its placeholder tables. A wordlist broad enough to catch domain
*concepts* would flag those too, produce constant noise, get disabled
within a month, and then protect nothing. ``Auction_Dim``, ``Auction_Fact``,
``General_Dim`` (this warehouse's actual schema/database qualifiers),
``Talar`` (Persian "trading hall", romanised — this warehouse's own
vocabulary, distinct from the generic English "Ring" the schema contract
uses), ``CustomerContract`` and ``DeliveryPlace`` (real table names) are not
ambiguous with anything a generic warehouse would plausibly also be called.

What is exempt, and why
------------------------
* **Docstrings** (module/class/function) and **doctests** (which are just
  ``>>> ...`` text living inside a docstring's own string) are fine —
  documentation needs concrete examples, and a doctest needs a real value
  to assert against. This is the same carve-out the task brief specifies;
  verified by this module's own two tests to still hold.
* **Attribute docstrings** — a bare string-literal statement immediately
  following an ``Assign``/``AnnAssign``, the PEP 257 companion convention
  Sphinx/numpydoc read as a field's documentation (e.g.
  ``core/models.py``'s ``RetrievalContext.facts`` field, followed by a
  bare string literal describing it: "Fact tables (Contract,
  CustomerContract, Offer, ...)"). Not AST-recognised as a docstring by
  the language itself, but exactly as inert — nothing ever reads it at
  runtime.
* **One explicit, individually-justified allowlist entry** —
  ``_ALLOWED_LITERAL_LOCATIONS`` below — for cases that are real string
  literals but are, like a docstring, human-facing documentation rather
  than something the SQL pipeline, the guard, or retrieval ever reads.
  Anything not matching one of these three shapes fails the build.

What this test would catch
----------------------------
A real table/schema name reappearing as a Python string literal anywhere
in first-party source outside a docstring/doctest/attribute-docstring —
e.g. a hardcoded fallback table list, a hand-written example SQL string
used in a default value, a forgotten debug print. Exactly the shape of
literal PRs #38-#40 and this phase removed.

What it would NOT catch
-------------------------
* The same name spelled as an ordinary Python **identifier** (a variable,
  function, or class literally named ``Talar...``) rather than a string
  literal — the brief scopes this to string literals specifically, the
  same way ``test_tuning_layer.py`` scopes its own check to numeric
  literals rather than "every number-shaped thing."
* A real value hidden behind concatenation/computation the AST can't
  statically resolve to a literal (``"Auct" + "ion_Dim"``, a value read
  from an environment variable) — deliberately out of scope, the same
  trade-off ``test_tuning_layer.py`` makes for numeric expressions.
* Any name NOT in ``_FORBIDDEN_IDENTIFIERS`` — a different real table name
  this warehouse has that happens not to be one of the six identifiers
  verified here would slip through undetected. The list is deliberately
  short and unmistakable rather than exhaustive; broadening it is a
  separate, explicit decision, not something this test infers.

``prompts/`` is deliberately excluded (it holds no ``.py`` today, so it is
not part of ``_FIRST_PARTY_DIRS`` below and this test does not need to name
it to skip it) — its ``.md`` files still carry real domain vocabulary
(``system_prompt.md``, ``business_glossary.md``, ``few_shots.md``), a
known, deferred follow-up: PR #38-#40's sibling migration for the prompt
layer, deliberately NOT done in this phase because collapsing it risks the
byte-identical static-prefix invariant ``prompt_engine/static_prefix.py``
and ``tests/test_static_prefix.py`` depend on, and that risk is not worth
taking days before a deployment. Closes when that migration lands and
``prompts/`` gains config-driven content with no domain literals of its own
(at which point, if any ``.py`` prompt-loading code appears under
``prompts/``, it should be added to ``_FIRST_PARTY_DIRS`` below).
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Mirrors the package list in CI's ``pytest --doctest-modules`` invocation
#: (see ``.github/workflows``) and ``tests/test_tuning_layer.py`` /
#: ``tests/test_persian_normalization.py``'s own copy of the same list --
#: the first-party engine source, as opposed to tests/, scripts/, docs/, or
#: third-party code. ``prompts/`` is NOT included -- see the module
#: docstring's last paragraph for why, and what closes that gap.
_FIRST_PARTY_DIRS = (
    "api", "llm", "security", "session", "database", "core", "knowledge",
    "prompt_engine", "retrieval", "schema_data", "logs", "exporters",
    "observability", "eval",
)
_FIRST_PARTY_FILES = ("config.py",)

#: Unmistakable identifiers naming this specific warehouse -- its actual
#: schema/database qualifiers and real table names. Not a wordlist: see
#: the module docstring's "What counts as a violation" section for why
#: each entry was chosen and why ordinary English words are excluded.
_FORBIDDEN_IDENTIFIERS = (
    "Auction_Dim",
    "Auction_Fact",
    "General_Dim",
    "Talar",
    "CustomerContract",
    "DeliveryPlace",
)

#: One real string literal, individually triaged, that is documentation in
#: substance but not in AST-recognised (docstring / attribute-docstring)
#: position -- keyed ``"relative/path.py::lineno"`` (the ``ast.Constant``
#: node's own ``lineno``, i.e. where the literal's text starts). A future
#: edit that moves or rewrites this literal will shift its line number and
#: make ``test_allowlist_entries_still_exist`` fail -- deliberately: that
#: forces a human to re-triage the edited line rather than letting a stale
#: allowlist entry silently keep covering for whatever replaced it.
_ALLOWED_LITERAL_LOCATIONS: dict[str, str] = {
    "database/schema_inspector_cli.py::50": (
        "argparse --help epilog text for the schema auto-discovery CLI -- "
        "the same 'operator-driven wizard, human-facing, never unit-tested' "
        "tool setup.cfg's [coverage:run] section already excludes by policy "
        "(see its comment there). The example command line it shows is "
        "illustrative only: nothing here is read by the SQL pipeline, the "
        "guard, or retrieval -- it exists purely so a human running "
        "--help sees a realistic --include-schemas example, the same job "
        "a docstring's Examples section does one function up."
    ),
}


def _documentation_node_ids(tree: ast.AST) -> set[int]:
    """Return ``id()`` of every ``ast.Constant`` string node this file
    treats as documentation rather than executable text: a real docstring
    (module/class/function, the first statement of its body) or a PEP 257
    "attribute docstring" (a bare string-literal statement immediately
    following an ``Assign``/``AnnAssign`` in the same body) -- see the
    module docstring's "What is exempt" section.
    """
    ids: set[int] = set()
    scopes: list[ast.AST] = [tree] + [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope in scopes:
        body = getattr(scope, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
        for prev, cur in zip(body, body[1:]):
            if (
                isinstance(prev, (ast.Assign, ast.AnnAssign))
                and isinstance(cur, ast.Expr)
                and isinstance(cur.value, ast.Constant)
                and isinstance(cur.value.value, str)
            ):
                ids.add(id(cur.value))
    return ids


def _candidate_paths(repo_root: Path) -> list[Path]:
    paths: list[Path] = [repo_root / f for f in _FIRST_PARTY_FILES]
    for d in _FIRST_PARTY_DIRS:
        for path in sorted((repo_root / d).rglob("*.py")):
            rel_parts = path.relative_to(repo_root).parts
            if "tests" in rel_parts or "__pycache__" in rel_parts:
                continue
            paths.append(path)
    return paths


def _find_domain_literal_hits(repo_root: Path) -> dict[str, str]:
    """Return ``{"relative/path.py::lineno": snippet}`` for every
    non-documentation string literal containing a ``_FORBIDDEN_IDENTIFIERS``
    entry, across the first-party source tree."""
    hits: dict[str, str] = {}
    for path in _candidate_paths(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        doc_ids = _documentation_node_ids(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in doc_ids:
                continue
            if any(identifier in node.value for identifier in _FORBIDDEN_IDENTIFIERS):
                hits[f"{rel}::{node.lineno}"] = node.value[:80]
    return hits


class TestNoDomainLiteralsInFirstPartySource:
    def test_no_unallowed_domain_literal(self):
        repo_root = Path(__file__).resolve().parent.parent
        hits = _find_domain_literal_hits(repo_root)

        unexpected = sorted(set(hits) - set(_ALLOWED_LITERAL_LOCATIONS))
        assert unexpected == [], (
            "Real-warehouse identifier(s) found as an executable string "
            "literal outside a docstring/doctest/attribute-docstring and "
            "outside _ALLOWED_LITERAL_LOCATIONS: "
            f"{[(name, hits[name]) for name in unexpected]}. Either this is "
            "genuinely domain data that belongs in project_config/ (see "
            "PRs #38-#40 and this phase's own commits for the pattern), or "
            "it is documentation-in-substance and belongs in "
            "_ALLOWED_LITERAL_LOCATIONS with a specific justification, the "
            "same way the one existing entry is triaged."
        )

    def test_allowlist_entries_still_exist(self):
        """Every allowlisted location must still contain the literal it was
        triaged for -- otherwise the entry is silently permitting nothing
        (or, worse, a *different* literal that happens to land on the same
        line after an unrelated edit) and would mask a real new hit."""
        repo_root = Path(__file__).resolve().parent.parent
        hits = _find_domain_literal_hits(repo_root)

        stale = sorted(set(_ALLOWED_LITERAL_LOCATIONS) - set(hits))
        assert stale == [], (
            f"Allowlist entries no longer found in source: {stale}. "
            "The literal was moved, rewritten, or removed -- re-triage the "
            "new location (or remove the stale entry) rather than leaving "
            "it in _ALLOWED_LITERAL_LOCATIONS."
        )
