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

## What happens if project_config/ is missing?

Importing `knowledge.*` modules will succeed.
Accessing the variables (e.g. `knowledge.aliases.RING_ALIASES`) will raise
`ConfigNotFoundError` with a clear message telling you which file is missing.

## Validation

All YAML files are validated with Pydantic v2 models defined in
`knowledge/config_loader.py`. If a field is wrong, you will get a clear
error message with the filename and field name.
