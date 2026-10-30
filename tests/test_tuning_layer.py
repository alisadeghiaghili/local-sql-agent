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
    "llm/interpret.py::MAX_PREVIEW_ROWS": (
        "data-governance invariant -- it decides how many rows of real "
        "query results cross the boundary to the language model. An "
        "env-overridable version would let a deployment quietly increase "
        "what leaves the engine by editing one variable, which is exactly "
        "the shape config.py's 'Three layers' section says must not be a "
        "knob. It is also written into INTERPRET_TEMPLATE's own wording "
        "('up to 20 rows shown'), so the two would have to move together"
    ),
    "eval/share.py::FORMAT_VERSION": (
        "file-format invariant -- it names the layout of the share bundle a "
        "reviewer's tooling reads; an env-overridable version would let a "
        "deployment write one layout under another's number"
    ),
    "core/redaction.py::_ERROR_TEXT_LIMIT": (
        "data-governance invariant -- the longest redacted error text that "
        "leaves the server in a share bundle (a driver message can echo the "
        "whole statement); a knob would be a way to export more by editing "
        "one variable"
    ),
    "eval/diagnosis.py::_MAX_COLUMNS": (
        "data-governance invariant -- how many result column names a share "
        "bundle's diagnosis may carry; bounds what a wide query exports"
    ),
    "eval/diagnosis.py::_MAX_NAME_CHARS": (
        "data-governance invariant -- the longest result column name a share "
        "bundle's diagnosis may carry; bounds what an alias that smuggles a "
        "value into a name can export"
    ),
    "api/cors_diagnostics.py::MAX_REMEMBERED_ORIGINS": (
        "abuse bound -- how many distinct rejected origins the log middleware "
        "remembers (and so logs); origins are attacker-supplied text, and a knob "
        "would only be a way to let a request stream grow the set and the log"
    ),
    "api/cors_diagnostics.py::_MAX_PARSED_ORIGIN": (
        "abuse bound -- an Origin header longer than this is never parsed for the "
        "same-origin test (it is not a real origin); it changes no CORS decision"
    ),
    "core/start_examples.py::MAX_LIMIT": (
        "implementation detail -- the most start questions one GET "
        "/v2/examples response carries (the page asks for six). It bounds a "
        "client-supplied `limit`, so a request cannot make the route check "
        "every example's SQL; it is not a behaviour an operator tunes"
    ),
    "core/start_examples.py::MAX_QUESTION_LENGTH": (
        "implementation detail -- an examples.yaml question longer than "
        "this is not a one-click start for the empty conversation, so it is "
        "not offered; nothing else reads it"
    ),
    "core/cors_origins.py::MAX_ORIGIN_LENGTH": (
        "abuse bound -- the most characters of a request-supplied origin that are "
        "ever printed; a longer value is cut, never echoed whole"
    ),
    "eval/compare.py::_CONTAINMENT_ROW_LIMIT": (
        "implementation detail -- the diagnosis' subset/superset check is a "
        "quadratic scan and is skipped past this many rows (the class then "
        "falls back to row_count_differs); it changes no verdict"
    ),
    "eval/fingerprint.py::DEFAULT_FLOAT_PRECISION": (
        "a golden set's expected_fingerprint values are hashed at this "
        "precision; env-tunable would silently desync a deployment from "
        "its own golden set's pinned hashes"
    ),
    "eval/stats.py::Z_95": (
        "mathematical constant -- the 97.5th percentile of the normal distribution, "
        "the multiplier of a two-sided 95% interval; not a setting"
    ),
    "eval/stats.py::MIN_STRATUM_N": (
        "reporting invariant -- below this many cases a stratum's 95% interval spans "
        "tens of points and is reported as not interpretable; if it moved per "
        "deployment, two reports of the same data would disagree on what to trust"
    ),
    "eval/benchmarks/bird.py::DEFAULT_QUERY_TIMEOUT": (
        "default of the bench-import --query-timeout flag; the operator sets it on "
        "the command line, so an environment default would add a second hidden place "
        "to change how long the import waits (same reasoning as DEFAULT_REPEATS)"
    ),
    "eval/benchmarks/sqlite_load.py::DEFAULT_CHUNK": (
        "implementation detail -- rows per insert batch of the benchmark loader, the "
        "default of --chunk-size; it changes no result"
    ),
    "eval/benchmarks/sqlite_load.py::FLOAT_SIGNIFICANT_DIGITS": (
        "definition of 'the same data' -- floats are compared after being written with "
        "12 significant digits, documented in the runbook; a knob would let two imports "
        "of one benchmark disagree on whether the copy is faithful"
    ),
    "eval/benchmarks/sqlite_load.py::_MODULUS": (
        "implementation detail -- the table digest is a sum of 128-bit row hashes, "
        "modulo 2**128"
    ),
    "eval/benchmarks/sqlite_load.py::_PK_MAX_CHARS": (
        "platform invariant -- SQL Server's index key limit is 900 bytes, 450 NVARCHAR "
        "characters; a longer primary key cannot be created, so this is not a knob"
    ),
    "core/fileperms.py::_FILE_MODE": (
        "security invariant -- 0o600 is the whole content of finding 18 "
        "(the Flask session-signing key, the audit trail, the session "
        "store and the password table were all world-readable). A knob "
        "here would let a deployment reopen the finding by setting one "
        "environment variable, and the value it would be set to in a "
        "hurry is exactly the 0o644 the finding was about. There is also "
        "no legitimate second value: a file holding a signing key or "
        "conversation transcripts is owner-only or it is wrong"
    ),
    "core/fileperms.py::_DIR_MODE": (
        "security invariant, same reasoning as _FILE_MODE -- 0o700 on a "
        "directory is what stops another local account listing the "
        "filenames inside it, which for the export directory leaks "
        "timestamps and query cadence even when each file within is "
        "already owner-only"
    ),

    # ── Implementation details: nobody outside the module cares, or the
    # only meaningful comparison is against a value already tunable
    # elsewhere.
    "prompt_engine/static_prefix.py::_CHARS_PER_TOKEN": (
        "rough chars-per-token heuristic compared only against itself and "
        "against the already-tunable prompt_retrieval_token_budget"
    ),
    "llm/providers.py::_CHARS_PER_TOKEN": (
        "the same rough chars-per-token heuristic, used only to label an "
        "*estimated* reasoning-token count when a response carries "
        "reasoning text but no usage figures at all; llm.providers cannot "
        "import prompt_engine (the whole knowledge base) for one constant, "
        "and the estimate is flagged as an estimate"
    ),
    "database/schema_inspector.py::_MAX_SAMPLE_LEN": (
        "display-truncation length inside the interactive, untested, "
        "coverage-excluded setup wizard; never read by the running engine"
    ),
    "core/yaml_loading.py::_MAX_LISTED_DUPLICATES": (
        "how many repeated keys one error message lists before '... and N "
        "more'; a display cap on text read by an operator, not a threshold "
        "any behaviour depends on (the file is refused whatever it is)"
    ),
    "database/schema_inspector_cli.py::LARGE_DRAFT_TABLES": (
        "table count above which the schema-inspector CLI prints an advisory "
        "warning at the end of a run; one-off onboarding tool, never read by "
        "the running engine, and the warning changes no exit code"
    ),
    "database/schema_inspector_cli.py::DEFAULT_SAMPLE_ROWS": (
        "distinct values per string column that --sample-values writes to the "
        "review file; one-off onboarding tool, never read by the running "
        "engine, and --sample-rows overrides it per run"
    ),
    "retrieval/value_resolver.py::_in_flight_resolutions": (
        "not a constant at all -- a mutable counter of running resolution "
        "workers, guarded by _resolution_slots. Its 0 is an initial value, "
        "not a knob; the bound it is compared against is the tunable "
        "config.Settings.resolve_value_max_concurrency"
    ),

    # security/sql_format.py: the house layout's widths. They are the
    # display format itself (the maintainers' SQL style, pinned character
    # for character by tests/test_sql_format.py), not a threshold anything
    # in a deployment's behaviour depends on; an environment variable that
    # changed them would only make two deployments show the same query
    # differently.
    "security/sql_format.py::_INDENT": (
        "layout width of the house SQL style; display only, pinned by "
        "tests/test_sql_format.py"
    ),
    "security/sql_format.py::_FIRST_ITEM_COL": (
        "layout width of the house SQL style; display only"
    ),
    "security/sql_format.py::_GAP": (
        "layout width of the house SQL style; display only"
    ),
    "security/sql_format.py::_ON_SPACES": (
        "layout width of the house SQL style; display only"
    ),
    "security/sql_format.py::_LONG_EXPR": (
        "layout threshold of the house SQL style (when an expression stops "
        "taking part in alias alignment); display only"
    ),
    "security/sql_format.py::_GROUP_INLINE": (
        "layout threshold of the house SQL style (when a parenthesised "
        "AND/OR group breaks over lines); display only"
    ),
    "security/sql_format.py::_EQ_ALIGN_MAX": (
        "layout threshold of the house SQL style (when a join condition's "
        "left side stops taking part in '=' alignment); display only"
    ),

    # security/column_policy.py: the bounds of the look-alike advice of the
    # preflight (core to what the heuristic means, not a threshold a
    # deployment tunes). The advice never changes what a key may read, and a
    # variable that moved them would only make two deployments advise
    # differently about the same schema.
    "security/column_policy.py::_LOOK_ALIKE_MIN_LENGTH": (
        "shortest denied name, or stem plus ending, that says anything about "
        "other columns (`ID` sits inside thousands); advice only"
    ),
    "security/column_policy.py::_LOOK_ALIKE_ENTRY_LIMIT": (
        "entries examined per call, so the advice cannot slow a preflight on "
        "a key with thousands of entries; advice only"
    ),

    # retrieval/filter_values.py: how the filter value check words its one
    # correction message. They decide what the model is TOLD, never what runs
    # or whether a request is answered, so a deployment has nothing to tune.
    "retrieval/filter_values.py::NEAREST_LIMIT": (
        "how many real values the correction message offers per unknown "
        "literal (a prompt-size bound; the message is advice to the model)"
    ),
    "retrieval/filter_values.py::_MIN_SIMILARITY": (
        "trigram similarity below which a value is not offered as near; it "
        "changes which hints the message carries, never what runs"
    ),
    "retrieval/filter_values.py::_QUOTED_MAX": (
        "longest literal quoted in the correction message (a prompt-size "
        "bound)"
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
        "out of scope: SQL self-correction retry cap. The duplication into "
        "session/engine.py has been resolved -- that module now imports "
        "this constant instead of redefining it. Moving it into "
        "config.Settings remains a separate, still out-of-scope decision"
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
    "schema_data/retriever.py::_COLUMN_MIN_WEIGHT": (
        "scoring-model constant of the column-name evidence, the same kind as "
        "_BIGRAM_MULTIPLIER above: the rarity a column word needs before it counts. "
        "Tuned once against the synthetic retrieval benchmark "
        "(docs/design/RETRIEVAL.md), not per deployment"
    ),
    "schema_data/retriever.py::_COLUMN_SCALE": (
        "scoring-model constant of the column-name evidence: how much one rare "
        "column word adds next to a description match (see _COLUMN_MIN_WEIGHT)"
    ),
    "schema_data/retriever.py::_COLUMN_DESCRIPTION_SHARE": (
        "scoring-model constant of the column-name evidence: a word found only "
        "in a column's description counts this share of one in its name"
    ),
    "core/config_doctor.py::DRAFT_DESCRIPTION_SHARE": (
        "invariant: where the preflight draws the line between a curated "
        "schema.yaml and the inspector's raw draft (more than a quarter of the "
        "descriptions are the placeholder). A deployment-tunable line would let "
        "one variable silence the check that exists to catch a draft in place "
        "of the curated files; it is pinned by tests/test_config_doctor.py at "
        "its exact edge"
    ),
    "core/config_doctor.py::NON_WORD_SYNONYM_SHARE": (
        "invariant: the share of non-word synonym keys (timestamps, numbers) "
        "that marks aliases.yaml as the inspector's seeds; the same reasoning "
        "as DRAFT_DESCRIPTION_SHARE, and pinned at its edge by the same tests"
    ),
    "core/config_doctor.py::REVIEW_MARKED_SHARE": (
        "invariant: the share of synonym lines still carrying the inspector's "
        "`# REVIEW` marker that marks aliases.yaml as a draft; the same "
        "reasoning as DRAFT_DESCRIPTION_SHARE"
    ),
    "core/config_doctor.py::_HEADER_LINES": (
        "implementation detail: how many leading lines of a project_config/ "
        "file are searched for the inspector's draft header (it writes it on "
        "line 1); not a deployment tuning knob"
    ),
    "core/config_doctor.py::_ADDRESS_LENGTH": (
        "implementation detail: the length at which a segment of words with "
        "digits looks like a street address to the sample-value advice; a "
        "heuristic of an advice-only check, not a deployment tuning knob"
    ),
    "core/config_doctor.py::SYNC_DRAFT_SHARE": (
        "invariant: the share of table descriptions that only repeat the table's "
        "name, or of column descriptions that are only `<type> column`, above "
        "which schema.yaml is the sync-format draft and not a curated file; the "
        "same reasoning as DRAFT_DESCRIPTION_SHARE, pinned at its edge by "
        "tests/test_config_doctor.py"
    ),
    "core/config_doctor.py::RAW_VALUE_SYNONYM_LIMIT": (
        "invariant: how many synonym keys shaped like a date, phone number or "
        "address aliases.yaml may hold before the preflight fails; a "
        "deployment-tunable limit would let one variable silence the check that "
        "exists to keep row values out of the prompt and out of version control"
    ),
    "core/value_shapes.py::MAX_VALUE_LENGTH": (
        "implementation detail: the longest value the setup wizard's "
        "--value-synonyms takes as a synonym key (a name, title or status is "
        "short, a message is not); a heuristic of an opt-in wizard flag, not a "
        "deployment tuning knob"
    ),
    "core/value_shapes.py::MAX_DISTINCT_VALUES": (
        "implementation detail: a column with more distinct values than this is "
        "not categorical for the wizard's --value-synonyms; an opt-in heuristic, "
        "not a deployment tuning knob"
    ),
    "core/value_shapes.py::VALUES_PER_TABLE": (
        "implementation detail: the most synonym keys --value-synonyms takes "
        "from one table; bounds what an opt-in wizard flag writes"
    ),
    "core/value_shapes.py::_ADDRESS_LENGTH": (
        "implementation detail: the length at which a run of words with digits "
        "looks like an address to the value-shape test; a heuristic, not a "
        "deployment tuning knob"
    ),
    "core/wizard_drafts.py::ALIAS_LIMIT": (
        "implementation detail: aliases kept per table by the setup wizard, whose "
        "model sometimes answers with every concatenation of the same few words; "
        "bounds what a draft file holds, not a runtime behaviour"
    ),
    "schema_data/describe_prompt.py::RELATIONSHIP_LIMIT": (
        "implementation detail: how many relationship lines scripts/draft_descriptions.py "
        "shows the model for one table; bounds the size of an operator tool's prompt, "
        "never reaches a query, the guard or the serving prompt"
    ),
    "schema_data/describe_prompt.py::_ALIAS_LIMIT": (
        "implementation detail: how many entity aliases scripts/draft_descriptions.py "
        "shows the model for one table; bounds an operator tool's prompt only"
    ),
    "schema_data/describe_prompt.py::SAMPLES_PER_COLUMN": (
        "implementation detail: the most label values --samples shows the model for "
        "one column; a privacy bound of an opt-in flag (the wizard's value rules "
        "decide which values are eligible), deliberately not a deployment knob"
    ),
    "schema_data/describe_prompt.py::TABLE_TEXT_LIMIT": (
        "implementation detail: the longest table description kept from a model's "
        "draft (one sentence); bounds what a draft file holds"
    ),
    "schema_data/describe_prompt.py::COLUMN_TEXT_LIMIT": (
        "implementation detail: the longest column description kept from a model's "
        "draft (a few words); bounds what a draft file holds"
    ),
    "schema_data/describe_prompt.py::_TOKENS_BASE": (
        "implementation detail: the fixed part of the max_tokens one "
        "draft_descriptions.py call asks for; the application's LLM_NUM_PREDICT is "
        "the floor and --max-tokens the ceiling, so the operator already controls both ends"
    ),
    "schema_data/describe_prompt.py::_TOKENS_PER_COLUMN": (
        "implementation detail: tokens per column asked about in the max_tokens of "
        "one draft_descriptions.py call (a column name, a few Persian words, JSON "
        "punctuation); the operator controls the floor (LLM_NUM_PREDICT) and the "
        "ceiling (--max-tokens)"
    ),
    "schema_data/describe_prompt.py::TOKEN_CEILING": (
        "implementation detail: the default for draft_descriptions.py --max-tokens, "
        "the most one answer is sized for; the flag overrides it per run"
    ),
    "llm/wizard_llm.py::ALIAS_MAX_TOKENS": (
        "implementation detail: the completion ceiling of the setup wizard's alias "
        "call, sized from the longest answer that call asks for (eight short phrases "
        "and a sentence); it is lifted when the operator has made room for a "
        "reasoning model, and never reaches the SQL path or a running request"
    ),
    "llm/wizard_llm.py::_SQL_SIZED_BUDGET": (
        "implementation detail: the default of LLM_NUM_PREDICT, above which the "
        "alias call takes the operator to have made room for a reasoning model "
        "and sends no cap of its own; a mirror of a setting, not a second knob"
    ),
    "schema_data/sync.py::REPORT_LIST_LIMIT": (
        "implementation detail: how many names one section of the "
        "operator script's printed report lists before '... and N more'. "
        "It shapes terminal output only and never reaches a query, the "
        "prompt or the guard, so it is not a deployment tuning knob"
    ),
    "core/persian.py::_MIN_STEM": (
        "invariant -- the shortest stem the plural-suffix rule accepts is part "
        "of the rule's definition (the endings, this length, letters only), "
        "documented with its examples and pinned by tests; lowering it would "
        "make 'ها' and one- or two-letter words that end in it match as "
        "plurals, so it is not something a deployment tunes"
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
