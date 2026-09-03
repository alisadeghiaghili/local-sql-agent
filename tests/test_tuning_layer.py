# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Guard the third configuration layer: tuning belongs in config.Settings.

``config.py``'s module docstring ("Three layers, not two") states the
rule this codebase now follows: engine code carries no domain names and
no tuning literals; domain data lives in ``project_config/``; tuning
knobs (retrieval aggressiveness, cache sizes, timeouts, retry/backoff,
regression thresholds, ...) live on :class:`config.Settings`,
env-overridable, read through ``cfg.settings`` at call time.

A rule nobody checks decays. This module is the check: it walks the same
first-party source tree ``tests/test_persian_normalization.py``'s
``TestSingleTranslationTable`` already walks (and the same package list
CI's ``pytest --doctest-modules`` invocation covers) looking for a bare,
module-level *numeric* constant -- an ``int``/``float`` literal, or a
simple arithmetic expression of numeric literals (``10 * 1024 * 1024``),
assigned directly to a name at module scope. Anything found outside
``_ALLOWED_MODULE_CONSTANTS`` below fails the build: a new tuning-shaped
constant has appeared outside ``Settings`` and needs to be sorted, the
same way the audit behind this test sorted the ones that existed when it
was written.

Why this shape, not "every number"
-----------------------------------
Scanning for *every* numeric literal in the source (array indices, HTTP
status codes, version numbers, buffer sizes used once inline, a
``for i in range(3)`` loop bound, ...) would flag hundreds of numbers
that are not configuration at all and never will be -- a test that cries
wolf gets disabled within a month, which is worse than no test. Two
restrictions keep this test's population small and load-bearing instead:

1. **Module scope only.** A number has to be lifted out to a bare
   ``NAME = <number>`` assignment at the top of a file -- not buried
   inside a function body, a class body, a dict/list literal, or an
   f-string -- before it is even a *candidate* to be a tuning knob.
   Nobody promotes an HTTP status code or a slice index to a named
   module constant; the constants that get this treatment are already,
   by construction, the ones an author considered worth naming and
   reusing, which is exactly the population "should this be tunable?"
   needs to be asked of.
2. **Numeric literals only,** not strings/tuples/dicts/booleans -- a
   tuning *knob* in this codebase's own vocabulary (see
   ``config.Settings``) is overwhelmingly a number (a timeout, a size, a
   percentage, a count). String constants (regexes, template text,
   sentinel tags) are a different audit with a different false-positive
   shape and are out of scope here.

Every existing hit was individually triaged (see ``config.py``'s "Three
layers, not two" section) into tuning (moved to ``Settings``), invariant
(left as a source constant with a docstring explaining why it must not be
tunable), or implementation detail (left alone, nobody outside the module
cares). A handful of additional constants of the same tuning shape were
found during that triage but are explicitly OUT OF SCOPE for this phase
-- see each allowlist entry's comment below -- and are recorded here as
follow-up, not as an endorsement that they should never move.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Mirrors the package list in CI's ``pytest --doctest-modules`` invocation
#: (see ``.github/workflows``) -- the first-party source, as opposed to
#: tests/ or third-party code. Kept identical to
#: ``tests/test_persian_normalization.py``'s ``TestSingleTranslationTable``
#: on purpose: both tests walk "the engine," just looking for different
#: things.
_FIRST_PARTY_DIRS = (
    "api", "llm", "security", "session", "database", "core", "knowledge",
    "prompt_engine", "retrieval", "schema_data", "logs", "exporters",
    "observability", "eval",
)
_FIRST_PARTY_FILES = ("config.py",)

#: Module-level numeric constants that are allowed to exist outside
#: ``config.Settings``, keyed as ``"relative/path.py::NAME"``. Every entry
#: is triaged, not just grandfathered -- see the comment on each.
_ALLOWED_MODULE_CONSTANTS = {
    # ── Invariants: part of the design's correctness, must not be tunable.
    # See config.py's "Three layers, not two" section for the full
    # reasoning on each.
    "security/auth.py::MIN_KEY_LENGTH": (
        "security invariant -- lowering it weakens structural entropy "
        "enforced once at key-issue time; must not be env-overridable"
    ),
    "eval/determinism.py::MIN_REPEATS": (
        "statistical floor -- fewer than 2 repeats cannot measure "
        "determinism at all, it is not a 'less thorough' setting"
    ),
    "eval/fingerprint.py::DEFAULT_FLOAT_PRECISION": (
        "a golden set's expected_fingerprint values are hashed at this "
        "precision; env-tunable would silently desync a deployment from "
        "its own golden set's pinned hashes"
    ),

    # ── Implementation details: nobody outside the module cares, or the
    # only meaningful comparison is against a value already tunable
    # elsewhere.
    "prompt_engine/static_prefix.py::_CHARS_PER_TOKEN": (
        "rough chars-per-token heuristic compared only against itself and "
        "against the already-tunable prompt_retrieval_token_budget"
    ),
    "database/schema_inspector.py::_MAX_SAMPLE_LEN": (
        "display-truncation length inside the interactive, untested, "
        "coverage-excluded setup wizard; never read by the running engine"
    ),

    # ── Tuning-shaped, but with a narrower, more appropriate configuration
    # surface than a process-wide environment variable -- deliberately not
    # duplicated into Settings. See config.py's "Three layers, not two"
    # section and eval/determinism.py's own comment on DEFAULT_REPEATS.
    "eval/determinism.py::DEFAULT_REPEATS": (
        "eval.cli's own --determinism-repeats flag default; unlike a "
        "comparison threshold this multiplies real LLM-endpoint calls, so "
        "a silent env-var default would risk an unexpectedly slow/costly "
        "run -- the cost decision stays explicit at the call site"
    ),

    # ── Tuning-shaped, found during this phase's audit but explicitly OUT
    # OF SCOPE: retrieval/dimension_vocabulary.py belongs to a concurrent
    # branch (phase4-externalise-vocab) this phase must not touch. Left as
    # follow-up for whichever phase sorts that file's own constants.
    "retrieval/dimension_vocabulary.py::MIN_MATCH_LENGTH": (
        "out of scope: file owned by a concurrent branch this phase does "
        "not touch (see phase4-tuning-layer's task notes)"
    ),
    "retrieval/dimension_vocabulary.py::_BACKGROUND_REFRESH_BACKOFF_SECONDS": (
        "out of scope: file owned by a concurrent branch this phase does "
        "not touch (see phase4-tuning-layer's task notes)"
    ),

    # ── Tuning-shaped, found during this phase's audit but not part of
    # its brief (a specific, pre-enumerated list of 14 constants) --
    # recorded here as follow-up rather than silently left unguarded.
    "llm/providers.py::_TIMEOUT": (
        "out of scope: LLM HTTP timeout, same tuning shape as this phase's "
        "constants but not in this phase's brief -- follow-up"
    ),
    "llm/providers.py::_RETRIES": (
        "out of scope: LLM call retry count -- follow-up"
    ),
    "llm/providers.py::_BACKOFF_BASE": (
        "out of scope: LLM retry backoff base -- follow-up"
    ),
    "llm/sql_agent.py::MAX_CORRECTION_ATTEMPTS": (
        "out of scope: SQL self-correction retry cap, duplicated (see "
        "session/engine.py's own copy below) rather than shared -- "
        "follow-up should resolve both the tuning move and the duplication"
    ),
    "session/engine.py::MAX_CORRECTION_ATTEMPTS": (
        "out of scope: mirrors llm/sql_agent.py's constant of the same "
        "name -- follow-up"
    ),
    "schema_data/retriever.py::_TOP_N": (
        "out of scope: TF-IDF retrieval result cap -- follow-up"
    ),
    "schema_data/retriever.py::_MIN_SCORE": (
        "out of scope: TF-IDF retrieval score floor -- follow-up"
    ),
    "schema_data/retriever.py::_FORCED_SCORE": (
        "out of scope: TF-IDF always-include sentinel score -- not "
        "obviously tuning (a sentinel, not a threshold); needs its own "
        "triage -- follow-up"
    ),
    "schema_data/retriever.py::_BIGRAM_MULTIPLIER": (
        "out of scope: TF-IDF bigram scoring weight -- follow-up"
    ),
}


def _is_numeric_literal_expr(node: ast.expr) -> bool:
    """True for an int/float literal, or +/-/arithmetic of such literals.

    Deliberately excludes ``bool`` (a subclass of ``int`` in Python) --
    ``DEBUG = True`` at module scope is a flag, not a tuning number.
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.BinOp):
        return _is_numeric_literal_expr(node.left) and _is_numeric_literal_expr(node.right)
    if isinstance(node, ast.UnaryOp):
        return _is_numeric_literal_expr(node.operand)
    return False


def _find_module_level_numeric_constants(repo_root: Path) -> dict[str, int]:
    """Return ``{"relative/path.py::NAME": lineno}`` for every module-level
    ``NAME = <numeric literal expression>`` (or annotated equivalent) found
    under the first-party source tree."""
    candidate_paths: list[Path] = [repo_root / f for f in _FIRST_PARTY_FILES]
    for d in _FIRST_PARTY_DIRS:
        for path in sorted((repo_root / d).rglob("*.py")):
            rel_parts = path.relative_to(repo_root).parts
            if "tests" in rel_parts:
                continue
            candidate_paths.append(path)

    hits: dict[str, int] = {}
    for path in candidate_paths:
        rel = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module scope only -- not generic_visit/walk
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if not _is_numeric_literal_expr(value):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    hits[f"{rel}::{target.id}"] = node.lineno
    return hits


class TestTuningConstantsStayInSettings:
    def test_no_new_module_level_numeric_constant_outside_allowlist(self):
        repo_root = Path(__file__).resolve().parent.parent
        hits = _find_module_level_numeric_constants(repo_root)

        unexpected = sorted(set(hits) - set(_ALLOWED_MODULE_CONSTANTS))
        assert unexpected == [], (
            "New module-level numeric constant(s) found outside "
            "config.Settings and outside tests/test_tuning_layer.py's "
            "allowlist: "
            f"{[(name, hits[name]) for name in unexpected]}. Sort each "
            "into tuning (move to config.Settings, env-overridable, read "
            "at call time), invariant (leave as a source constant, "
            "document why it must not be tunable), or implementation "
            "detail (leave alone) -- see config.py's 'Three layers, not "
            "two' section -- then add it to _ALLOWED_MODULE_CONSTANTS "
            "with that reasoning if it is not moving to Settings."
        )

    def test_allowlist_has_no_stale_entries(self):
        """Every allowlisted constant must still exist -- otherwise the
        allowlist is silently permitting nothing and would mask a real new
        hit sharing a coincidentally-allowlisted name at a different path."""
        repo_root = Path(__file__).resolve().parent.parent
        hits = _find_module_level_numeric_constants(repo_root)

        stale = sorted(set(_ALLOWED_MODULE_CONSTANTS) - set(hits))
        assert stale == [], (
            f"Allowlist entries no longer found in source: {stale}. "
            "Remove them from _ALLOWED_MODULE_CONSTANTS."
        )
