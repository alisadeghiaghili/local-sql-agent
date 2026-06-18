\# Project Context



\## What this project does

Local SQL agent that answers questions about database using LLM.



\## Architecture decisions made

\- Skills system: reusable prompt modules in skills/ package

\- Refiner layer: post-processing pipeline in refiner/ package

\- LiteLLM proxy on localhost:4000 for model routing



\## Coding conventions

\- Use async/await throughout

\- All LLM calls go through LLMBackend abstraction

\- No magic strings — use enums

\- No exceptions for control flow



\## Current phase

Phase 1: Building skills/ package

---

## Session Log

### [2026-06-18] Initial Code Review

**Critical Issues Found:**
- Two parallel pipelines: `app.py` uses `ollama_client.generate_sql` shim; `api/server.py` uses `SQLAgent` — bugs fixed in one are silently absent in the other
- `PromptBuilder.build()` uses `set()` on `context.entities + context.facts`, destroying insertion order — use `context.selected_tables` instead
- `_safe_generate_sql_only` in `runner.py` duplicates `SQLAgent._generate_and_clean` exactly — already diverged: sql-only mode bypasses the agent entirely
- `runner.py` imports `OperationalError` and `SATimeout` from SQLAlchemy but never catches them — unhandled DB exceptions bypass all typed NLQError machinery and crash as 500
- `InjectionAttemptError` is imported in `runner.py` but never raised — injection detection in `validate_sql` never maps to it
- `validate_sql` forbidden keyword check uses trailing-space tokens (e.g. `"DELETE "`) — `DELETE/*comment*/FROM` or `DELETE\nFROM` bypasses the check entirely
- `validate_sql` does not block stacked queries via semicolons — `SELECT 1; SELECT * FROM sys.tables` passes
- `validate_sql` does not block `OPENROWSET`, `OPENDATASOURCE`, or `BULK INSERT` — T-SQL data exfiltration vectors
- `runner.py` accesses `agent._backend` directly three times — private attribute leak breaks if SQLAgent internals change
- `_interpret` in `runner.py` swallows all exceptions silently — backend failures return empty string with no signal to caller
- `RetrievalContext.dimensions` is a redundant alias for `entities` — same list stored twice, ticking inconsistency bug
- `WizardLLM` and `OllamaBackend` are two completely parallel LLM stacks with no shared code — dual maintenance burden
- `_IdfDict._max_idf` is set outside `__init__` in a classmethod — direct construction crashes with AttributeError
- `clean_sql` calls `_LIMIT_RE.search(sql)` twice — second call is redundant, first result should be stored
- `QueryLog.as_dict()` hardcodes field names — silently omits any newly added fields; use `dataclasses.asdict()`
- `_ALWAYS_INCLUDE` in `schema_data/retriever.py` hardcodes domain data (ring names, Persian terms) — belongs in `project_config/aliases.yaml`
- `requirements.txt` has no version pins — `>=` constraints will silently install breaking changes
- `CONVENTIONS.md` says "use async/await throughout" and "no magic strings — use enums" — neither is implemented anywhere
- `db_connection_url` partial value printed to stdout in `app.py` — server/database name exposed in container logs

**Decisions Made:**
- Skills system will live in `skills/` package with a `Skill` ABC (`is_applicable`, `render`, `name`) and a `SkillRegistry`
- Refiner layer will live in `refiner/` package with a `Refiner` ABC, `RefinementContext` dataclass, and `RefinerPipeline`
- Self-correction loop currently inside `SQLAgent` will be extracted into `CorrectionRefiner` in the refiner layer
- `ensure_top` from `sql_guard.py` will become `EnsureTopRefiner` — first concrete refiner
- `PromptBuilder.build()` will accept an optional `skills: list[Skill]` parameter and inject skill fragments via a new `{skill_instructions}` template placeholder
- `SQLAgent` will accept injected `SkillRegistry` and `RefinerPipeline` via constructor — both default to production instances
- `SQLAgent` will expose a public `backend_name` property so `runner.py` stops accessing `_backend` directly
- `_safe_generate_sql_only` will be deleted — `mode='sql'` will delegate through `SQLAgent` like all other modes
- `ollama_client.py` shim will be deleted once `app.py` REPL is unified onto the `SQLAgent` pipeline
- `DateSkill` will be the first concrete skill implemented — highest value, most questions involve Persian dates

**Next Steps:**
- Fix `PromptBuilder` `set()` bug immediately — one line change, prevents arbitrary schema ordering now
- Catch `OperationalError` and `SATimeout` in `_safe_run` — real crash risk in production
- Map `validate_sql` forbidden keyword check to strip whitespace/comments before matching — close the bypass gap
- Add semicolon blocking to `validate_sql`
- Add `OPENROWSET`, `OPENDATASOURCE`, `BULK INSERT` to `_FORBIDDEN`
- Expose `SQLAgent.backend_name` as a public property
- Create `refiner/base.py`, `refiner/pipeline.py` — scaffold only, no behavior change
- Create `refiner/sql_refiners.py` with `EnsureTopRefiner`
- Create `refiner/correction_refiner.py` — extract correction loop from `SQLAgent`
- Create `skills/base.py`, `skills/registry.py` — scaffold only
- Create `skills/date_skill.py` — first concrete skill
- Wire `SkillRegistry` and `RefinerPipeline` into `SQLAgent` constructor
- Delete `_safe_generate_sql_only` from `runner.py`
- Delete `ollama_client.py` and unify REPL onto `SQLAgent`
- Pin all versions in `requirements.txt`
- Replace all `Literal["sql", "result", "full"]` and status strings with enums

---

### [2026-06-18] Skills Package Implementation

**What Was Built:**
- `skills/__init__.py` — package init, exports `Skill`, `SkillRegistry`
- `skills/base.py` — abstract `Skill` base class with `is_applicable(question, context)`, `render(question, context)`, and `name` abstract members
- `skills/registry.py` — `SkillRegistry` class that holds a list of `Skill` instances and returns active ones via `get_active(question, context)`
- `skills/table_analysis_skill.py` — `TableAnalysisSkill`, first concrete skill; activates when selected tables are present in context and injects table-specific SQL guidance into the prompt
- `PromptBuilder.build()` updated to accept `skills: list[Skill] | None` and inject rendered fragments into a new `{skill_instructions}` section
- `prompt_engine/templates.py` updated to include `SKILL INSTRUCTIONS` section between `DETECTED FILTERS` and `EXAMPLES`

**Decisions Made:**
- `Skill.is_applicable()` receives both `question` and `RetrievalContext` — skills may activate on question signals, table presence, or both
- `Skill.render()` returns a plain string fragment; `PromptBuilder` wraps it with `[SKILL_NAME SKILL]` header automatically
- `SkillRegistry` is a plain class, not a singleton — injected into `SQLAgent` via constructor so tests can swap it freely
- Skills are stateless — all context needed for rendering is passed at call time, never stored on the instance
- `TableAnalysisSkill` is the base concrete skill; domain-specific skills (`DateSkill`, `RankingSkill`, etc.) follow the same pattern

**Next Steps:**
- Implement `DateSkill` — activates on Persian calendar signals and `Date` table presence; injects PersianYear/PersianMonth column guidance
- Implement `RankingSkill` — activates on ranking/top-N signals; injects `TOP n ORDER BY` pattern guidance
- Implement `AggregationSkill` — activates on sum/average/count signals; injects `GROUP BY` and `HAVING` guidance
- Wire `SkillRegistry` with default skills into `SQLAgent` constructor
- Create `refiner/` package scaffold (`base.py`, `pipeline.py`)
- Extract self-correction loop from `SQLAgent` into `CorrectionRefiner`
- Move `ensure_top` into `EnsureTopRefiner`

---

### [2026-06-18] Security Hardening and Bug Fixes

**What Was Built:**
- `security/sql_guard.py` — rewrote `validate_sql` to close three security gaps:
  - Added `_strip_comments()` and `_normalise_for_scan()` helpers that remove block comments (`/* */`) and line comments (`--`) and collapse all whitespace before keyword matching — closes `DELETE/*comment*/FROM` and `DELETE\nFROM` bypass
  - Added explicit semicolon check before all other validation — blocks stacked queries (`SELECT 1; DROP TABLE x`)
  - Added `OPENROWSET`, `OPENDATASOURCE`, `BULK INSERT` to `_FORBIDDEN` — closes T-SQL data exfiltration vectors
  - Switched forbidden keyword matching from substring-with-trailing-space to word-boundary regex (`\bKEYWORD\b`) so no trailing space is needed and no whitespace trick can bypass it
- `security/sql_guard.py` — fixed `clean_sql` double-search bug: `_LIMIT_RE.search(sql)` result is now stored once and reused instead of called twice
- `prompt_engine/builder.py` — fixed `set()` ordering bug: `selected_tables` now uses `context.selected_tables` (order-preserving, deduplicated) instead of `set(context.entities + context.facts)`
- `prompt_engine/builder.py` — added `skills: list | None = None` parameter; active skill fragments are rendered and injected into `{skill_instructions}` placeholder
- `prompt_engine/templates.py` — added `SKILL INSTRUCTIONS` section between `DETECTED FILTERS` and `EXAMPLES`
- `api/runner.py` — added `OperationalError` and `SATimeout` catch blocks in `_safe_run` — these were imported but never caught, causing unhandled 500s on DB timeout/connection failure
- `api/runner.py` — removed `InjectionAttemptError` dead import (was imported, never raised, no injection detection logic existed to raise it)
- `llm/sql_agent.py` — reviewed; `backend_name` property and `generate_sql_only` method flagged as still needed but not yet implemented in this session

**Decisions Made:**
- `validate_sql` normalisation (comment stripping + whitespace collapse + uppercase) is applied to a copy of the SQL — the original string is never mutated, so `clean_sql` output is preserved exactly for execution
- Word-boundary regex matching (`\bKEYWORD\b`) is used for all whole-word forbidden tokens; prefix tokens (`XP_`, `SP_`) are matched with plain `in` since they are intentionally prefix-only
- `BULK INSERT` is matched as a two-word phrase via `re.escape` + `\b` boundaries — the space inside is preserved in the pattern
- Semicolon check is placed first (before comment stripping) because a semicolon in a comment is still a semicolon in the raw SQL and should be rejected
- `OperationalError` and `SATimeout` map to `DatabaseConnectionError` by default; if the message contains "timeout" or "lock" they map to `QueryTimeoutError` instead

**Remaining Issues Not Yet Fixed (carried forward):**
- `runner.py` still accesses `agent._backend` directly in `run_query`, `_safe_generate_sql_only`, and `_interpret` — `SQLAgent.backend_name` property not yet added
- `_safe_generate_sql_only` in `runner.py` still duplicates retrieval + prompt assembly instead of delegating to `SQLAgent` — `SQLAgent.generate_sql_only()` not yet implemented
- `QueryLog.as_dict()` still hardcodes field names — `dataclasses.asdict()` not yet applied
- `ollama_client.py` shim still exists and `app.py` still uses it — two pipelines still live
- `requirements.txt` still has no version pins
- `RetrievalContext.dimensions` redundant alias still present

**Next Steps:**
- Add `backend_name` public property to `SQLAgent`
- Add `generate_sql_only(question, system_prompt)` method to `SQLAgent` that runs retrieval + prompt + generate + clean + validate without executing
- Update `runner.py` to use `agent.backend_name` and `agent.generate_sql_only()` — eliminate all `_backend` direct access
- Delete `_safe_generate_sql_only` from `runner.py` once `SQLAgent.generate_sql_only()` exists
- Fix `QueryLog.as_dict()` to use `dataclasses.asdict()`
- Unify `app.py` REPL onto `SQLAgent` and delete `ollama_client.py`
- Pin all versions in `requirements.txt`
