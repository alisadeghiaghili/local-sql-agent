# project_config.example

This directory contains **template files** for `project_config/`.
They show the required format for each YAML file — with placeholder values
and comments explaining every field.

## Setup

1. Copy this directory to `project_config/` (which is git-ignored):
   ```bash
   cp -r project_config.example/ project_config/
   ```
2. Edit each file under `project_config/` with your real domain data.
3. Never commit `project_config/` to git — it contains domain-specific data.

## Files

| File | Purpose | Exposed variable |
|------|---------|------------------|
| `aliases.yaml` | Ring/hall name aliases + TF-IDF synonym expansion | `RING_ALIASES`, `SYNONYMS` |
| `entities.yaml` | Entity name → database table mapping | `ENTITIES` |
| `business_rules.yaml` | LLM instructions per query category | `BUSINESS_RULES` |
| `examples.yaml` | Few-shot NLQ → SQL examples | `EXAMPLES` |
| `metrics.yaml` | Metric name → SQL expression mapping | `METRICS` |
| `schema.yaml` | Warehouse tables/columns/relationships — also the SQL guard's table/column allowlist | `TABLE_DESCRIPTIONS`, `TABLE_COLUMNS`, `RELATIONSHIPS` |
| `relationships.yaml` | Explicit join paths between tables, for `database.relationship_map` | (loaded directly, not through `knowledge.config_loader`) |
| `retrieval_hints.yaml` | Retrieval-ranking overrides (always-include terms, boosts) | consumed by `schema_data.retriever` |
| `memory_policy.yaml` | Session-memory retention policy | consumed by `session.*` |
| `session_policy.yaml` | Session/turn composition policy | consumed by `session.*` |
| `system_prompt.md` | The system prompt sent to the LLM — plain text/Markdown, not YAML | `knowledge.config_loader.load_system_prompt()` |
| `_test_fixtures/` | Optional, deployment-owned test fixture files (real expected values for a handful of tests) — see `_test_fixtures/README.md` | (test-only; nothing in the application reads this) |

Which directory these are read from is controlled by
`PROJECT_CONFIG_DIR` (default `project_config`) — see
`config.Settings.project_config_dir`. Set it to `project_config.example`
to run against this template directory instead (e.g. a fresh clone with no
`project_config/` yet, or CI). There is deliberately no automatic fallback
to this directory: pointing at it only ever happens by explicitly setting
the variable. A **relative** `PROJECT_CONFIG_DIR` is resolved against the
repository root, not against the current working directory the process
happens to be started from.

## What happens if project_config/ is missing?

Importing `knowledge.*` and `schema_data.*` modules will succeed.
Accessing the variables (e.g. `knowledge.aliases.RING_ALIASES`,
`schema_data.columns.TABLE_COLUMNS`) will raise `ConfigNotFoundError` with
a clear message telling you which file is missing.

## The system prompt

`system_prompt.md` is **not** a YAML file and is not validated by a
Pydantic model — it is the literal text sent to the LLM as its system
instructions, loaded once at server startup from
`<PROJECT_CONFIG_DIR>/system_prompt.md` by
`knowledge.config_loader.load_system_prompt()` /
`resolve_system_prompt_path()`. It used to live at the fixed path
`prompts/system_prompt.md`; that location, and the standalone
`prompts/few_shots.md` and `prompts/business_glossary.md` files, no longer
exist — the system prompt is now one more file in the same
deployment-owned config directory as `schema.yaml`, `aliases.yaml`, etc.

**The server refuses to start without it.** `api/server.py`'s `lifespan`
raises, verbatim:

```
RuntimeError: System prompt not found: <resolved path>
```

`app.py`'s REPL logs the equivalent condition and exits with status 1;
`eval.cli` has its own loader and raises a plain built-in
`FileNotFoundError: system prompt not found: <resolved path>` (not
`knowledge.config_loader.ConfigNotFoundError` — it never imports that
loader);
`webapp/agent.py` raises `RuntimeError: System prompt not found: <resolved path>`
the same way `api/server.py` does. None of the four silently falls back to
no system instructions.

Note the one behavioural asymmetry: `webapp/agent.py`'s `system_prompt()`
call is made from inside `answer_question()`'s blanket
`except Exception` handler, so the Flask app itself never refuses to
start on a missing prompt — `create_app()` and login succeed regardless.
The `RuntimeError` above is only raised, and turned into an
`ERROR`-status result, the first time a user actually submits a
question. The other three entry points fail at startup; this one fails
at first use.

To create your own, copy this directory's `system_prompt.md` to
`project_config/system_prompt.md` and rewrite it to describe your real
schema, business rules, and dialect requirements — this file's copy
describes only the generic example schema above and is not meant to be
used as-is in production.

## Validation

All YAML files are validated with Pydantic v2 models defined in
`knowledge/config_loader.py` (`schema.yaml` is validated by
`schema_data/registry.py`). If a field is wrong, you will get a clear
error message with the filename and field name.
