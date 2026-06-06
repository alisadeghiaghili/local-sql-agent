# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [2.0.0] — 2026-06-06

### Added
- `config.py`: typed `Settings` dataclass (frozen, `__slots__`), `validate()` method, singleton via `lru_cache`
- `database/connection.py`: `dispose_engine()` helper for test teardown and hot-reload
- `database/executor.py`: wraps `SQLAlchemyError` in `RuntimeError`; debug-logs row/column counts
- `llm/ollama_client.py`: calls `clean_sql()` on every model response before returning
- `security/sql_guard.py`: **new** `clean_sql()` (strips markdown fences, preamble prose, converts LIMIT→TOP, fixes SELECT TOP n DISTINCT order); **new** `ensure_top()` (inject TOP n when missing); extended `_FORBIDDEN` list (`EXECUTE`, `XP_`, `SP_`); `LIMIT` now blocked in `validate_sql()`
- `logs/query_log.py`: `Literal["SUCCESS", "ERROR", "OUT_OF_SCOPE"]` status type; `as_dict()` serialisation method; `__slots__`
- `logs/logger.py`: uses `settings.log_dir`; catches `OSError` on write failure
- `exporters/excel_exporter.py`: uses `ExcelWriter` context manager; auto-fits column widths (capped at 60)
- `schema/retriever.py`: bigram scoring (bigram match counts double); `_MIN_SCORE` threshold
- `schema/schema_registry.py`: `lru_cache(maxsize=64)` on `build_schema_context()`; accepts `tuple` for cache-safe API
- `app.py`: structured REPL with emoji indicators; separates `RuntimeError` from `ValueError`; logs elapsed time; prints row-count summary; graceful `KeyboardInterrupt` / `EOFError` shutdown
- `tests/test_config.py`: rewritten for new `Settings` dataclass (no `src/` dependency)
- `tests/test_sql_guard.py`: **new** — full coverage of `clean_sql`, `validate_sql`, `ensure_top`

### Changed
- `config.py`: flat module-level constants replaced by `Settings` dataclass + `settings` singleton
- All modules now import `from config import settings` (single import point)
- `schema/schema_registry.py`: `build_schema_context` now takes `tuple[str, ...]` instead of `list[str]` for LRU-cache compatibility

### Removed
- `tests/test_validator.py`: replaced by `tests/test_sql_guard.py`

---

## [1.1.0] — 2026-06-06

### Added
- Modular NLQ engine: `database/`, `exporters/`, `llm/`, `logs/`, `prompts/`, `schema/`, `security/`
- `app.py` entry point replacing `main.py`
- `config.py` with env-var-only configuration (no hardcoded credentials)
- `security/sql_guard.py`: read-only SQL validator
- `schema/`: full table registry, column schemas, FK relationships, business rules, keyword retriever
- `prompts/`: system prompt, business glossary, few-shots examples
- `.env.example` with all required variables documented
- `requirements.txt` updated
- `README.md` added

### Removed
- Legacy root scripts: `main.py`, `nlq.py`, `langsql.py`, `langchain_sql.py`, `nlq_with_sqlite.py`, `prompt_based_nlq.py`, `simple_nlq.py`, `create_db.py`
- Legacy folders: `agents/`, `src/`
- Old config files: `CHANGELOG` (plain text), `pyproject.toml`, `ruff.toml`

---

## [1.0.0] — initial

### Added
- Proof-of-concept scripts for NLQ-to-SQL via Ollama + LangChain
- `scripts/create_db.py`: SQLite sample database for local testing
- Initial `agents/`, `runners/`, `tests/` structure
