# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Fixed

- **A refused CORS origin is named in the server log (`api/cors_diagnostics.py`).** In 6.14.0 `verify_deployment` passed fully yet the web UI showed all three status lights red, and the log held only `OPTIONS /health`, `OPTIONS /v2/sessions` and `OPTIONS /v2/access-requests`, each `400 Bad Request`: the allowlist held `http://172.16.101.42:8077` and the page had been opened as `http://localhost:8077`. Nothing said so. A small ASGI layer registered outside `CORSMiddleware` now logs one `WARNING` per distinct rejected origin for the life of the process, for a preflight and for an ordinary request alike: `CORS: origin '<origin>' is not allowed (allowed: <list>). Browsers show this as 'Failed to fetch'. To allow it add this line to .env and restart: CORS_ALLOWED_ORIGINS=<current>,<origin> -- or open the page with exactly an allowed address (scheme, host and port). Logged once per origin.` At most 64 origins are remembered, then one line says further ones are not logged; the origin is stripped of control characters, cut at about 200 characters and never put into a response. The CORS policy is unchanged (no wildcard, no new default; the preflight is still answered `400`), and a request to the server's own address (`Origin` equal to `Host`) is same-origin and not reported.
- **The web UI says what is wrong with the API address, once, in Persian (`web/js/diagnosis.js`).** The status page showed red lights and a tooltip nobody reads; every admin card showed its own 404 when the address saved in the browser was the static file server's. A failed call to the configured address is now classified as a wrong address (it is the page's own origin, or `GET <address>/health` answers a 404, an HTML page or JSON that is not the API's health response) or as refused-or-unreachable (the call got no response; a browser cannot tell the two apart, so the message names both with their remedies and the page's own origin as the value to add to `CORS_ALLOWED_ORIGINS`). The analyst status view and the admin panel show it as one banner at the top; each admin card then says only «به API نرسید؛ پیام بالای صفحه را ببینید.» instead of repeating it. A 401, 403, 429 or 5xx is the API answering and keeps its own message, and the capability-specific 403 messages are unchanged. The analyst status view also no longer treats a 200 whose body is not the API's health response as a healthy API.

### Added

- **A preflight check, `Web UI origin is allowed by CORS` (`scripts/verify_deployment.py`, `core/cors_origins.py`).** With `VERIFY_UI_ORIGIN` set to the address the UI is opened with (for example `http://172.16.101.42:8077`) it passes when that origin is in `CORS_ALLOWED_ORIGINS` and fails otherwise, printing the `.env` line to add. Without it nothing can be proved, so it only advises (a `[PASS]` line starting `advice:`): when every allowed origin is loopback while `API_HOST` is not, or when exactly one non-loopback origin is allowed, it says to list every address the page is opened with (IP address, host name, `localhost`) and to set `VERIFY_UI_ORIGIN` to check one. It reads settings only (no network, no database), so the admin panel runs it too. The preflight now has twenty-two checks with one data source.
- Documentation: runbook §2 (the origin list must hold every address), §3 and §3.1 (the check and `VERIFY_UI_ORIGIN`), §4 and §5 (the new log line), and a new §5.1 table of the two symptoms with the log line and the fix; the same additions in section 2.3.1, section 2.8 and the preflight section of both setup guides (the check count is now twenty-two); `web/README.md`; the CORS block of `.env.example`.

## [6.14.0] — 2026-10-18

A benchmark track for accuracy, beside the private golden set, and confidence intervals on every figure. `python -m eval.cli bench-import bird` loads the public BIRD mini-dev benchmark (500 questions over 11 SQLite databases, CC BY-SA 4.0) into a database of the operator's own, one schema per database, and writes a golden file in the existing format whose reference results come from the original SQLite files; `bench-run bird` runs it through the production pipeline with the operator's model, once per database with that database's configuration, and merges the reports. It gives development decisions a large, fully visible sample whose full report can be shared because the data is public, and it is never the deployment's accuracy: the report says so in its first line. Separately, every report now prints a Wilson 95% interval and the number of cases for its overall figure and each stratum (a stratum under 20 cases is "not interpretable"), `compare` adds the paired statistics (fixed, regressed, an exact McNemar p-value, a 95% interval of the difference), and a determinism probe can report outcome consistency and mean accuracy over its repeats.

### Added

- **A benchmark track: `python -m eval.cli bench-import bird` (`eval/benchmarks/bird.py`, `bird_import.py`, `bird_config.py`, `sqlite_load.py`) (PR #190).** From the unpacked BIRD mini-dev package it copies each selected SQLite database into one schema of a SQL Server database named after the `db_id` (SQLAlchemy reflection; types mapped from what the data holds, `BIGINT` / `FLOAT(53)` / `NVARCHAR(n)` / `VARBINARY(MAX)`, dates stay text; names kept and quoted; chunked inserts with pyodbc `fast_executemany`; primary and foreign keys added after the data, a key SQL Server refuses is a warning), checks that every table has the source's row count, and writes `golden.jsonl` (and with `--with-evidence`, `golden.with_evidence.jsonl`), overall and per database, an `import_report.json` / `.txt` and a `NOTICE.txt` with the licence attribution. The load takes its own administrator login (`--target-url`, or `env:NAME`); the application then reads the database with its read-only login. A non-empty schema is refused, before anything is written, unless `--replace` is given; `--skip-load` re-checks schemas already loaded. `--dbs a,b` selects databases.
- **The reference result comes from the original SQLite file, and the copy is shown to be faithful by its data (PR #190).** Each case's expected rows are the result of the dataset's own SQL on its own `.sqlite` (opened read-only) and are compared by execution accuracy, so no translation can make a question wrong. After the load every table is compared with its source by content, not only by row count: rows are reduced to a canonical form (integers as integers, floats to 12 significant digits, text exactly with case and trailing spaces, blobs by hash) and the sum of the row hashes is an order-independent digest computed in Python on both sides in chunks. If every table matches, no question is excluded for a doubt about the load; a table that differs is reported with its columns, and only the questions whose reference SQL reads it are excluded, with the reason `load_fidelity`. Other exclusions, all kept in the file and listed one by one in the import report: `reference_failed`, `reference_empty`, `reference_too_large`. Cases are tagged `bench:bird`, `db:<db_id>` and `difficulty:<x>`; the evidence hint is off by default and, with `--with-evidence`, appended to the question under a marker for an ablation.
- **The target database must be case- and accent-sensitive (PR #190).** SQLite compares text case-sensitively; in a default SQL Server database `WHERE name = 'abc'` also matches `'ABC'`, so a correct answer could differ from the reference. The import reads `DATABASEPROPERTYEX(DB_NAME(), 'Collation')` and refuses a database that is not (a `CS` and `AS` collation, or `BIN` / `BIN2`) before writing anything, naming `CREATE DATABASE ... COLLATE Latin1_General_100_CS_AS_SC_UTF8`; `--allow-ci-collation` imports anyway and the import report and every report header then flag it.
- **A per-query check is a diagnostic, not a filter (PR #190).** Each reference is also transpiled to T-SQL with sqlglot, run on the copy and compared. A difference on identical data is a dialect-semantics difference (integer division, string comparison, a mixed-type column): the case is tagged `fidelity:semantic_difference`, stays in the count by default because the SQLite answer is still the right answer, and is listed; `--exclude-semantic-difference` removes it (reason `semantic_difference`). A reference that cannot be transpiled or fails on SQL Server is tagged `fidelity:unverified`, information only, and never excluded: excluding the queries that are hard to translate would inflate the accuracy.
- **A report gives the accuracy on all scored cases and without the semantic-difference cases (PR #190).** A benchmark report states scored, excluded-by-reason and flagged counts in its header, and the JSON carries both figures under `fidelity`.
- **A `project_config/` per database, and one over all schemas (PR #190).** `--out/<db_id>/project_config/` is the 6.13 inspector's draft (`--include-schemas <db_id>`) read through the application's login, completed with `system_prompt.md` and the policies from `--base-config` and empty `business_rules.yaml`, `examples.yaml`, `metrics.yaml` and `retrieval_hints.yaml`; `--descriptions` adds BIRD's column descriptions to `schema.yaml` (a curated configuration; without it the raw draft). `--out/_combined/project_config/` covers every schema.
- **`python -m eval.cli bench-run bird` runs and merges (PR #190).** It starts `eval.cli run --live --reference stored` once per database, each process with `PROJECT_CONFIG_DIR` set to that database's configuration, then pools the reports into `runs/<label>/report.json` and `report.txt`. `--mode combined` is a separate measurement over the combined configuration, labelled as such; `--evidence` runs the hint variant; `--label` names the folder; arguments after `--` go to every `run`.
- **A report of a benchmark says so (PR #190).** A report whose cases all carry the same `bench:<name>` tag begins with `BENCHMARK RUN (<name>) ... This is NOT this deployment's accuracy`, and the merged report names the mode, the evidence setting and the configuration; `EvalReport.benchmark` carries it in the JSON.
- **A golden case can be `excluded`, with an `exclusion_reason`, and can be matched by execution (`eval/models.py`) (PR #190).** `status: "excluded"` is skipped by the runner like the pending statuses and `run` prints `Not scored: N of M case(s) -- excluded ...` with the reasons; `match: "execution"` makes `--reference stored` compare the result with the recorded `expected_rows` by execution accuracy (column names ignored, row order only when the reference sorts with `TOP`) instead of by fingerprint. Both fields are optional and default to the previous behaviour.
- **Wilson 95% intervals on every report (`eval/stats.py`) (PR #190).** The text report prints the interval and `n` under the overall accuracy and on every per-tag and per-source line, and a stratum with fewer than 20 cases is marked `n<20: not interpretable`; the `--out` JSON and the share file's `summary` carry the same under `intervals`.
- **`compare` reports the paired statistics (PR #190).** On the cases both rounds scored: fixed, regressed and unchanged counts, the exact two-sided McNemar p-value on the discordant pairs, and a Newcombe 95% interval of the accuracy difference, in the text and under `paired` in `--json`. No new dependency: the binomial tail is summed directly. An older report compares as before.
- **The determinism probe can report outcome consistency and mean accuracy over its repeats (PR #190).** With `--live --determinism` each repeat is also executed and judged like the run; the report adds the share of cases right on every repeat or wrong on every repeat, and the mean accuracy over the repeats.
- Documentation (PR #190): runbook §18.7 (the benchmark track: why it exists and why its number is not the deployment's accuracy, licence and download, loading with an administrator login and reading with the application's, import, per-database and combined runs, the evidence ablation, what to send back), §18.3 and §18.6.2 (intervals, the paired statistics, repeats), and a section 5.6 in both setup guides.

### Changed

- **`eval.verify.frame_to_rows` moved to `eval/rows.py` (PR #190)** so the benchmark importer can record rows without a `project_config/`; `eval.verify` still exports it.

### Upgrading

- **The server does not change, and nothing has to be done by a deployment that does not use the benchmark track (PR #190).** The code that changed is the `eval` package (the accuracy harness, run from the command line) and its tests; `api/`, `core/` and the other server packages are as in 6.13.0 apart from the version number, so the answers the server gives, the audit log and the admin panel are unchanged. No setting, dependency or `requirements.lock` pin changed (`git diff v6.13.0 -- requirements.lock` is empty): run `pip install -r requirements.lock` as for every upgrade, but nothing new is installed.
- **Reports gain intervals and paired blocks, all of it additive, so a report from 6.13.0 or earlier still loads and compares (PR #190).** The text report prints a Wilson 95% interval and the number of cases under the overall accuracy and at the end of every per-tag and per-source line; the `--out` JSON gains the keys `intervals`, `benchmark` and `fidelity` (the last two are `null` for a golden set of your own); the share file's `summary` gains `intervals` and its `format_version` stays 1; `compare` prints a paired block and `compare --json` returns it as `paired`. Nothing was removed or renamed. A baseline, `--out` report or share file recorded on 6.13.0 or earlier is read as before: `--baseline` gates against it and `compare` takes it as either side, the paired block being computed on the cases both rounds scored; there is no need to record the baselines again. A script that reads the per-tag or per-source lines of the text report by column has to allow for the interval now at the end of each line; the JSON is the stable source.
- **The golden set has a new status, `excluded`, and a new field, `match`; both are optional and your own set never uses them (PR #190).** An `excluded` case (with a required `exclusion_reason`) is skipped by the runner like the pending statuses, and `match: "execution"` makes `--reference stored` compare the recorded `expected_rows` by execution accuracy instead of by fingerprint. Only `bench-import` writes either; `harvest_golden.py`, `golden_sheet.py` and `verify` do not, and a case without them is read and scored exactly as before, so an existing `golden.jsonl` needs no edit. Two things are visible: `run` now prints a `Not scored: N of M case(s) -- ...` line under the report whenever the file holds a case that is not `active` (a pending one included, which was skipped without a word before), and a golden file the tools rewrite now carries `"exclusion_reason": null` and `"match": "fingerprint"` on every line, which 6.13.0 also reads, because it ignores keys it does not know.
- **`--live --determinism` now also executes and judges each repeat (PR #190).** The probe used to call the model only. It now runs every repeat's SQL through the guard and the read-only executor, as `run` does, so one probe sends up to cases times repeats (3 by default) extra read-only queries to the data sources, and its report adds the outcome consistency and the mean accuracy over the repeats. Size a probe on a large golden set for the warehouse's quiet hours.
- **The benchmark track is optional, and nothing runs it unless you call `python -m eval.cli bench-import` or `bench-run` (PR #190).** If you decide to use it (runbook §18.7):
  - BIRD mini-dev is not part of this repository. Download it yourself and keep it, the loaded database and the files `bench-import` writes outside the checkout.
  - The load needs a separate administrator login, `--target-url` (or `env:NAME`, which keeps the password off the command line), that may create schemas and tables in a database of its own; the application reads that database with its normal read-only login, and the application's login must never be given the right to create anything. `--datasource` without `--target-url` would load through the application's login, and the command prints a warning saying so.
  - The database must be case- and accent-sensitive: a collation with `CS` and `AS` in its name, or a `BIN` / `BIN2` collation (create it with `COLLATE Latin1_General_100_CS_AS_SC_UTF8`). `bench-import` reads the collation and refuses any other before it writes anything; `--allow-ci-collation` imports anyway, and the import report and every report header then carry a warning.
  - Its figure is not the deployment's accuracy. The questions are public, English and about other domains, and the report begins `BENCHMARK RUN (bird) ... This is NOT this deployment's accuracy`. It is a second instrument for development decisions and does not replace the golden set built from your own questions (runbook §18.1).

## [6.13.0] — 2026-10-16

Which schemas the setup tools skip is now one rule, kept in configuration, and the two tools that draft a project configuration read every data source the way the application does. `EXCLUDE_SCHEMAS` in `.env` and `exclude_schemas:` per data source name the schemas that `database.schema_inspector_cli`, `scripts/sync_schema.py` and `setup_project.py` do not scan, on top of the system schemas, and a preflight check reports a `schema.yaml` table that sits in one. The inspector and the wizard now read each source of `datasources.yaml` through the application's own engine, so a draft lists what the application can query; the inspector writes the format `sync_schema.py` writes and puts no sample value into `schema.yaml`, `entities.yaml` or `aliases.yaml`. The wizard writes to `project_config_draft/`, merges a second run into the first instead of replacing it, keys tables of the same name in different schemas separately and can record the schemas an operator left out in `datasources.yaml`. The setup guide now exists in English beside the Persian one, and a test keeps the two in step.

### Added

- **One rule says which schemas the setup tools do not scan, and it is kept in configuration (`database/schema_filter.py`) (PR #184).** A schema is excluded when its name matches, without regard to case, any of: the system schemas (`sys`, `INFORMATION_SCHEMA`, `guest`, `cdc`, the nine fixed `db_*` role schemas by exact name, `pg_catalog`, `pg_toast`, `pg_*`), the new `EXCLUDE_SCHEMAS` setting (comma-separated `fnmatch` patterns, every data source, so a deployment with only `DB_CONNECTION_URL` has the same control), the data source's new `exclude_schemas:` list in `datasources.yaml` (validated as non-empty strings), and the patterns of one command line. A user schema called `db_sales` is still scanned. `schema_is_excluded` moved there and is still importable from `database.schema_inspector`.
- **`python -m database.schema_inspector_cli` reads every data source through the application's own engine and writes drafts in the format `sync_schema.py` writes (PR #184).** With no `--db-url` it inspects each source of `datasources.yaml` (or the implicit `DB_CONNECTION_URL` source) with the login the server uses, so a draft lists only what the application can query. `--datasource NAME` (repeatable) narrows the run. `schema.yaml` is built by `schema_data.sync.draft_schema_text`, the code behind `--add-tables`: `datasource:` lines with several sources, a table with the same `schema.table` in several sources written once as `datasource: [a, b]`, `Schema.Table` keys only for names shared across schemas (counted across all selected sources). `entities.yaml` uses the same keys.
- **The inspector drafts relationships when the database declares no foreign key (PR #184).** Declared keys and the naming rules of `sync_schema.py`'s proposals (`<Table>_ID`, `<Table>ID`) both feed the draft; a guess carries the comment `# INFERRED from column names (not a declared foreign key) -- REVIEW before use`, a declared key none.
- **Staging and change-capture tables (`STAGING_PATTERNS`) are left out of the inspector's draft by default; `--include-staging` keeps them (PR #184).** The end-of-run summary says how many tables and schemas each rule skipped: system schemas, configured exclusion, `--exclude-schemas`, `--include-schemas`, `--exclude-tables`, staging.
- **`--sample-values` (with `--sample-rows N`, default 10) writes real column values to `sample_values.review.yaml` in the draft directory (PR #184).** The file is readable by its owner only (`core/fileperms.py`); nothing loads the file and `--dry-run` never prints it.
- **`sync_schema.py` honours the exclusion (PR #184).** The "in the database, not in `schema.yaml`" report and `--add-tables` skip tables in excluded schemas (`--add-tables '*'` adds only tables outside them), and the report says how many were skipped, per schema. `--exclude-schemas PATTERNS` (repeatable) adds patterns for one run. A table `schema.yaml` already lists is synced exactly as before, and an excluded table never makes `--check` fail.
- **A preflight check, `No table in an excluded schema`, reports a `schema.yaml` table whose schema every one of its sources excludes (PR #184).** It follows the other doctor advice: a `[PASS]` line starting `advice:` with the count and the first keys (never a value) and the two fixes, remove the table or narrow the exclusion; `[PASS]` when none; `[SKIP]` when nothing is excluded beyond the system schemas and no table matches. File reads only.
- **The setup wizard describes every data source of `datasources.yaml`, through the application's own login (`setup_project.py`, `database/setup_sources.py`) (PR #187).** With a `datasources.yaml` and no `--db-url`, step 1 opens each source with `database.connection.get_engine` and step 2 lists, plans, inspects and keys the tables of all of them together; `--datasource NAME` (repeatable) narrows the run, and `--db-url` keeps reading one database with the login in the URL. A bare table name shared by several schemas, in one source or across sources, is keyed `Schema.Table` (the inspector's and `sync_schema.py`'s rule), and a `schema.table` present in two sources is one entity. With several sources configured every `entities.yaml` entry carries `datasource: [sales, reports]`, written by the logic of `database/schema_draft.py` (`listed_datasources`); a later run for one source adds to an earlier entry's list instead of replacing it. `SchemaInspector.for_engine(engine)` reads through an engine it does not own and leaves it open. The resume fingerprint includes the names of the sources read and the sources of each table. The inspector's source selection is the same code now (`database.setup_sources.configured_sources`).
- **The wizard applies the configured schema exclusion and says which rule skipped what (PR #187).** Each source's reading plan uses the rule of `database/schema_filter.py`: the system schemas, `EXCLUDE_SCHEMAS`, that source's `exclude_schemas:` and `--exclude-schemas`, so a configured schema is never listed in the question, read or sampled. The skip summary adds the inspector's per-rule lines (`system schemas`, `configured exclusion (EXCLUDE_SCHEMAS / exclude_schemas:)`, `--exclude-schemas`, `left out interactively`, `staging`), one per source with several.
- **The wizard can record the schemas an operator leaves out interactively (PR #187).** It offers (default No; never with `--non-interactive` or `--dry-run`) to add them as the matching source's `exclude_schemas:` in `datasources.yaml` (`database/datasources_edit.py`): a comment-preserving text edit after a `datasources.yaml.bak-<YYYYmmdd-HHMMSS>` copy, re-validated with `validate_datasources_yaml_text` and compared with the original before the file is replaced; on any failure the file is left as it was and the lines to add by hand are printed. Without `datasources.yaml` (or with `--db-url`) it prints the `EXCLUDE_SCHEMAS=...` line for `.env` and never edits `.env`.
- Documentation (PR #184): runbook §2.3 (the inspector, rewritten), §3.1 (the new check), §16.2 and §16.3 (`exclude_schemas:`, the exclusion in the sync); `docs/design/DATASOURCES.md`; `docs/design/TABLE-NAMES.md`; `exclude_schemas:` and `EXCLUDE_SCHEMAS` in the example files.
- **The setup guide is bilingual: `docs/en/getting-started.md`, kept in step with the Persian guide by a test (PR #186).** The step-by-step guide existed only in Persian (`docs/fa/getting-started.md`). The English version has the same sections, numbering, tables and commands: only prose and comments are translated, and a Persian string that the operator sees in the UI (a button label, a card title) stays as it appears, with its English meaning in parentheses. Both files now open with a language switch, and the README links to both. `tests/test_setup_guide_parity.py` compares what survives translation: the heading outline with its section numbers (Persian digits folded to ASCII), the number of fenced code blocks (in total and section by section), the multiset of `--flags` and of `NAME=` environment variables, and the set of documents the guide names; a failure names the first section that differs, and a copy with one heading removed is shown to fail.
- Documentation (PR #188): the setup guides in both languages (`docs/fa/getting-started.md`, `docs/en/getting-started.md`) describe the 6.13.0 setup tools. Sections 0.3.1 to 0.3.3 and the install list say that the wizard and the inspector write drafts to `project_config_draft/`, read every data source of `datasources.yaml` through the application's login and share one schema-exclusion rule with `sync_schema.py`; the preflight section lists the new check (twenty-one checks in all); the fix for `Tables are in their data source` is `sync_schema.py`; part 4 carries the upgrade note about `.setup_log.json`; and an unclear sentence in 5.5 is rewritten. The admin panel is described with its thirteen sections (a key with only `admin` opens 4 and gets 403 on 9), in the guides, `docs/admin-panel-architecture.md` and `web/README.md`.

### Changed

- **The inspector's built-in system-schema filter compared a lower-case `information_schema` and missed SQL Server's `INFORMATION_SCHEMA`, `cdc`, `guest` and the fixed role schemas (PR #184).** All of them are skipped now, in any letter case, by `SchemaInspector.inspect` (used by the setup wizard) as well.
- **The inspector no longer drafts live values into `schema.yaml`, `entities.yaml` or `aliases.yaml` (PR #184).** Nothing in the application reads a `sample_values` key from `schema.yaml`; the `e.g. a, b, c` text drafted into column descriptions went into the prompt, and `aliases.yaml`'s `synonyms:` seeds were sample values. Without `--sample-values` no row is read (only `COUNT(*)` per table, unless `--no-row-counts`), `aliases.yaml` has `synonyms: {}`, and a column description is `<type> column`.
- **The draft `schema.yaml` follows `sync_schema.py`'s spelling (PR #184).** A name that is not a plain identifier is bracketed in its key (`[Price List]`), a qualified key (`sales.Trade`) carries its schema and has no `db_schema` line, and each table has `column_types:`. The inspector's classification and primary-key notes are no longer in `schema.yaml` (`entities.yaml` keeps `classification`, `row_count` and `is_pk`). Views are drafted as well as tables, as `sync_schema.py` does.
- **The setup wizard writes drafts to `project_config_draft/` by default, and refuses the live directory (`setup_project.py`, `core/wizard_drafts.py`) (PR #185).** The default `--output` was `project_config/`: the wizard replaced curated `entities.yaml`, `aliases.yaml`, `business_rules.yaml`, `examples.yaml` and `relationships.yaml` with no backup, which the schema inspector had refused to do since 6.9.3. Writing into the live configuration directory (`PROJECT_CONFIG_DIR`, by default `project_config/`) or `project_config.example/`, compared on resolved paths, now stops the run before the database or the model is touched (exit status 2, one-paragraph message) unless `--overwrite-live` is given; then every file about to be replaced, the setup log included, is first copied to `<name>.bak-<YYYYmmdd-HHMMSS>` next to it and the copies are listed. A file the wizard does not produce is never touched. A `--dry-run` writes nothing and is not refused.
- **A second run merges into the first instead of replacing it (PR #185).** Run once per database, the wizard's later `aliases.yaml` and `entities.yaml` used to replace the earlier ones, so the files shrank. Entries for tables this run did not cover now stay as they are, entries for tables of this run replace the old ones with the same key, examples are de-duplicated by question text and relationships by their two ends; nothing is removed. A table the model gave no answer for does not wipe an earlier entry: empty new aliases, or the placeholder rule written when the model gave none, leave the old entry in place (counted as kept and named in the summary). The summary prints `kept`, `added` and `replaced` per file. `--fresh` starts from scratch (still subject to the protected-directory rule). An existing wizard file that cannot be read stops the run before any model call.
- **The wizard keys entities, aliases, business rules and relationships like the schema inspector (PR #185).** A table is keyed by its bare name when no other table shares it and by `Schema.Table` when several schemas do (`docs/design/TABLE-NAMES.md`), and the model is told the schema-qualified name. A bare entry of an earlier run whose name is now shared is re-keyed in all five files and reported, not duplicated or overwritten by the other schema's table.
- **The wizard can leave schemas and staging tables out before any model call (PR #185).** New `--exclude-schemas PATTERNS` (comma-separated `fnmatch`, case-insensitive, wins over `--include-schemas`) and `--include-staging`. The tables `STAGING_PATTERNS` names are skipped by default, and the summary says how many tables were skipped and why. **A schema that is left out is never read:** step 2 first lists schema and table names (SQLAlchemy's inspector, names only), applies the include, exclude and staging rules and, in interactive mode, shows the schemas with their table counts and asks which to leave out, and only then inspects the remaining schemas with `SchemaInspector.inspect(include_schemas=..., exclude_schemas=..., exclude_tables=...)`, so no column or sample value of a left-out schema is fetched. A schema whose every table is staging is not read; a staging table with a bare name no other table shares is not read; one with a shared name is read and dropped afterwards so a same-named table elsewhere is kept. Non-interactive mode never prompts. The resume fingerprint is taken after the exclusions and includes the table keys.
- **The wizard's skip summary names the rule (PR #187).** `Skipped N of M tables` now also counts system schemas and configured exclusion (`EXCLUDE_SCHEMAS`, `exclude_schemas:`), which `SchemaInspector.inspect` used to drop silently, and `.setup_log.json` records the counts per rule and source (`step2_schema.skipped`).
- **Relationships and the business-rules join hints no longer depend on declared foreign keys (PR #185).** When the database declares fewer than one foreign key per ten tables, `relationships.yaml` also gets the relationships `schema_data/relationship_proposals.py` infers from column names; each carries `basis` and `confidence` fields and a `# REVIEW` comment, a reviewed entry is never replaced by a guess, and the business-rules prompt sees the same join hints.

### Fixed

- **The setup wizard no longer collapses tables of the same name in different schemas (`setup_project.py`) (PR #185).** Entities, aliases and rules were keyed by the bare table name, so `Trade` from four schemas became one entity: a 278-table warehouse produced a draft with 190 entities. Every table now has its own entry.
- **The wizard no longer overwrites curated configuration without a backup, and no longer loses the first database's entries on the second run (PR #185).** See Changed.
- **The warning `scripts/issue_api_key.py` prints for an `admin`-only key counted six sections the key cannot open; there are nine (PR #188).** The admin panel has thirteen sections, `require_admin` serves four of them, and the other nine (maintenance, feedback, schema drift, vocabulary, quality, per-analyst usage, auth failures, keys, access requests) need `operations` and/or `security`. The message names all nine; the key that is issued is unchanged.

### Upgrading

- **`python setup_project.py` now writes its drafts to `project_config_draft/` (git-ignored), not to `project_config/` (PR #185).** Copying the files you accept into `project_config/` is a reviewed step. To write into the live directory (`PROJECT_CONFIG_DIR`, by default `project_config/`) or `project_config.example/`, pass `--overwrite-live`: without it the run stops with exit status 2 before the database or the model is touched, and with it each file about to be replaced, `.setup_log.json` included, is first copied to `<name>.bak-<YYYYmmdd-HHMMSS>` next to it. A script or runbook of yours that ran the wizard and expected `project_config/` to be filled needs `--output project_config --overwrite-live` or a copy step. A `.setup_log.json` written by an earlier version is read as before; `--resume` generates again once, because the schema fingerprint now includes the table keys.
- **Running the wizard again over a directory of an earlier run merges into it (PR #185).** Use `--fresh` for the old start-from-scratch behaviour. Entries keyed by a bare name that is now shared by several schemas are re-keyed as `Schema.Table`; check the summary line `Re-keyed in ...`.
- **The wizard reads `datasources.yaml` when it exists (PR #187).** Run without `--db-url` it describes every source in the file through the application's login and writes `datasource:` lines to `entities.yaml`; give `--db-url` to keep the old single-database run. A deployment with only `DB_CONNECTION_URL` is unchanged.
- **The inspector now uses the application's login by default (PR #184).** Run it without `--db-url` and it reads `datasources.yaml` (or `DB_CONNECTION_URL`) through the engines the server uses, so tables the application cannot query no longer appear in a draft. `--db-url URL` still works for a first install before any configuration exists (one source, read with the login in the URL) and prints one line saying the application's login may see less. The `DATABASE_URL` environment variable is no longer read by `database.schema_inspector_cli`; pass `--db-url` (the wizard's single-database run still falls back to it when there is no `datasources.yaml` and no `DB_CONNECTION_URL`).
- **Drafts no longer contain sample values, and staging tables are skipped (PR #184).** A script that relied on `e.g.` text or `sample_values:` in the drafts must pass `--sample-values` and read `sample_values.review.yaml`; one that wanted staging tables passes `--include-staging`. Both flags are new and nothing else about the existing flags changed (`--exclude-schemas`, `--include-schemas`, `--exclude-tables`, `--no-row-counts`, `--dry-run`, `--output-dir`, `--sample-rows`).
- **One setting and one `datasources.yaml` key are new, both optional and empty by default: `EXCLUDE_SCHEMAS` in `.env` and `exclude_schemas:` per data source (PR #184).** Without them only the system schemas are skipped, which can hide a table `sync_schema.py` used to report in `cdc` or one of the fixed role schemas. A `schema.yaml` that already lists such a table keeps it and is synced as before. Both are read by the setup tools and the preflight only; nothing at run time uses them, so the answers the server gives do not change. An `exclude_schemas:` that is not a list of non-empty strings makes `datasources.yaml` fail to load, like any other invalid key. Both are in the example files.
- **Run `python scripts/verify_deployment.py` after the upgrade to see the new check, `No table in an excluded schema` (PR #184).** It never fails: it is a `[PASS]` line starting `advice:` when a `schema.yaml` table sits in a schema that every one of its data sources excludes (remove the table, or narrow the exclusion), a plain `[PASS]` when none does, and `[SKIP]` while nothing is excluded beyond the system schemas and no table matches. It reads files only. Runbook §3.1 lists it.
- **No `requirements.lock` pin changed and no file format changed (PR #184, PR #185, PR #187).** `git diff v6.12.0 -- requirements.lock` is empty. Run `pip install -r requirements.lock` as for every upgrade, but nothing new is installed.

## [6.12.0] — 2026-10-14

Accuracy rounds get a loop with a reviewer who has neither the model nor the warehouse, and the endpoint they measure is checked for a response cache. The operator runs `eval.cli run --live --reference live --share FILE` on the server and sends that one file; it says not only *that* an answer differed but *how* (too few rows, the right rows in the wrong order, a number two percent off) from a diagnosis computed on the server, and holds no result value. `eval.cli compare` puts two rounds side by side, `eval.cli split` keeps a held-out half nobody tunes to, and `--live` now measures what an analyst actually gets: it generates through the server's own correction loop instead of one model call, and a result cut by the row cap passes as `match_capped` only when a count of the uncapped SQL equals the reference's row count. Separately, a gateway in front of the model can keep a response cache and answer a new prompt with the reply stored for a similar one, which nothing in the reply shows: a setup run wrote one alias list for most of a database's tables, and the same endpoint can serve a valid but wrong SQL for a similar question and make an accuracy round measure the cache. The preflight check `Model endpoint answers each prompt itself` finds it with two tiny requests that differ in one random token, the setup wizard stops when the model gives the identical answer for three tables in a row, and `LLM_CACHE_SUSPECT_MS` warns at run time when a different question comes back with byte-identical SQL faster than the threshold.

### Added

- **A preflight check, `Model endpoint answers each prompt itself`, finds a response cache in front of the model (PR #181).** A gateway between the application and the model server can keep a semantic or prefix response cache and answer a new prompt with the reply stored for a similar one; nothing in the reply says so, and the application cannot tell. The check (`scripts/verify_deployment.py`, `check_model_answers_each_prompt`, right after `OpenAI-compatible model exists`) sends two tiny requests through the backend the server uses (same payload builder, so `LLM_EXTRA_BODY`, the seed and `temperature=0` apply; `max_tokens` cut to at most 128, 15 second timeout). Each asks for one fresh random token (`uuid4` hex); the prompts differ only in that token, so a cache collides on the second. `[PASS]` when each reply contains its own token (extra words around it are fine), with the reply times in the detail and never the reply; `[FAIL]` when the second reply carries the first token or either lacks its own, saying plainly that a response cache returned another prompt's answer, that it can serve the wrong SQL for a similar question, and that the fix is to disable response caching for this client at the gateway or to point `OPENAI_BASE_URL` at the model server directly. `[SKIP]` under `LLM_PROVIDER=mock`, for a remote endpoint while `LLM_ALLOW_REMOTE` is off (nothing is sent), when the endpoint does not answer (`OpenAI-compatible model exists` reports that), and when a reasoning model spends its whole budget before answering. It is a deep check on the admin panel (`GET /admin/health/checks?deep=1`): it is the only check that spends model-server time, and behind a response cache each run would leave two junk entries; the command line always runs it, and the panel's confirmation dialog for the deep checks now says that two small requests go to the model. Runbook §3.1 and §20.10.
- **The setup wizard stops when the model repeats itself, and `--allow-repeated-answers` overrides (PR #181).** Steps 3 (aliases) and 4 (business rules) compare the model's parsed answer for each table (the alias list; the rule text with the table names taken out; empty answers aside). When 3 tables in a row get the identical answer the step stops with `Step 3 stopped: the model endpoint returned the identical alias list for 3 different tables in a row (...)`, naming the suspected response cache, the preflight check and the gateway fix. It writes no file, exits with status 3 and leaves the run resumable (`--resume` continues once the endpoint is fixed). `--allow-repeated-answers` turns the stop into one warning, for a schema so small that identical answers are expected. Each of the two steps ends with a line such as `183 distinct alias lists for 183 tables`. Step 5 asks a single question for the whole schema and has nothing to compare. Runbook §2.2 and the Persian guide.
- **An early warning for a response cache at run time, `LLM_CACHE_SUSPECT_MS` (default `50`, `0` = off) (PR #181).** `observability/response_cache_watch.py` keeps, in memory per process, the last 200 (question hash, SQL hash) pairs per data source. When a different question returns byte-identical SQL within two minutes and the model's reply took less than the threshold, `POST /query` logs one `WARNING` per streak and writes `suspected_cached_response: true` in the audit `llm` block (and the response's). It never blocks or changes a request, since two questions can map to the same SQL; a check is a hash and a dictionary lookup, with an injectable clock for tests. The multi-turn session engine is not watched: its refinements can reuse a previous turn's SQL on purpose. Runbook §20.10.
- **Runbook §20.10, "A response cache in front of the model" (PR #181).** What it is (and how it differs from the model server's prefix KV cache), the symptom (identical answers, replies in milliseconds), the risk (a silent wrong SQL, and an accuracy run that measures the cache), how to confirm, and the fix. Tests: `tests/test_response_cache_detection.py`.
- **`python -m eval.cli run ... --share PATH` writes a share-safe results bundle next to the normal report (`eval/share.py`) (PR #180).** Needs `--live --reference live`; anything else is refused in one line, exit status 2, before the golden file is loaded or the model is called. UTF-8 JSON with sorted keys and cases sorted by id (two rounds `diff` cleanly), written atomically and owner-only. The path must be outside the checkout or under the git-ignored `eval_data/`: every other path in the tracked tree is refused (`check_share_path`, which needs neither git nor a `.gitignore` parser), and `--share` may not be the `--out` file. Header: `format_version`, application version, `prefix_version`, the model name (never the endpoint, host or key; a name that is a URL is cut to its last segment), the golden file's base name and SHA-256 (line endings normalised, so Windows and Linux agree), `PROMPT_RETRIEVAL_TOKEN_BUDGET`, each data source's prompt path (`static` or `retrieval`), start and end time, and a `privacy` block that says what is and is not inside. Per case: id, tags, question, reference and generated SQL, status, expected and selected data source, the guard's rejection reasons (class and redacted message), the correction count, redacted error text, latency and a mismatch diagnosis. A summary: execution accuracy overall, per tag and per data source, source-selection accuracy, the distribution of mismatch classes (every class, zeros included), correction rounds and latency p50/p95. **Never in it:** result rows or cell values, credentials, `.env` values, endpoint URLs, API keys, principal ids, a case's `notes` or recorded rows. **Readable on purpose, and said so in the block:** the question and the result column names (a `PIVOT` turns values into names). Runbook §18.6; Persian guide §۵.۵.
- **`--share-redact-literals` replaces string and numeric literals in both SQL texts (PR #180).** Without it the SQL is verbatim, because diagnosing a wrong filter needs the filter, and the header says so. The literals are found by parsing with sqlglot (`tsql`), never by pattern, so a quote inside a comment or a bracketed name is not mistaken for a string: strings (`N'...'`, hex and bit strings included) become `:str`, numbers `:num`, an `ORDER BY` / `GROUP BY` position is kept, comments are dropped, and text that does not parse is withheld whole rather than guessed at.
- **A mismatch diagnosis, computed on the server from the two result frames and holding no value (`eval/diagnosis.py`, `eval.models.MismatchDiagnosis`) (PR #180).** `mismatch_class` is one of `match`, `row_count_differs`, `column_count_differs`, `column_names_differ`, `same_rows_different_order`, `values_differ_same_shape`, `actual_is_subset`, `actual_is_superset`, `actual_empty`, `expected_empty` or `not_executed`, with the row counts, the column names of both results (cut at 128 characters and 64 names) and, for two single numeric cells that differ, `relative_error_bucket` (`<1%`, `<10%`, `>=10%`; never the numbers). The decision order is in the module docstring; it uses the same tolerance and ordering rule as the verdict (`eval.compare`), whose new `multiset_equal`, `multiset_contains` and `align_columns` tell "the same rows in another order" and "the same columns in another order" from "other values". It is also in every result of the normal `--out` report (`diagnosis`, `corrections`, `guard_rejections`; a report or baseline written before loads as before).
- **`python -m eval.cli compare OLD NEW` compares two rounds by `case_id` (PR #180).** Reads share bundles or `--out` reports and prints ids and counts only (no question, SQL or value): fixed, regressed, still failing, added and removed cases; the accuracy delta overall, on the cases both rounds have and per tag; the shift in each mismatch class; and a note when the golden file or the `--reference` differs. `--split dev|test` with `--split-file` (one id per line, a JSON list, or a JSON object with `dev` and `test` lists) limits it to one half; `--fail-on-regression` exits `1` when a compared case passed before and fails now, so `--split test --fail-on-regression` gates the held-out half; `--json` prints the comparison as JSON. Exit status `2` for an unreadable input. It needs no model, database or `project_config/`.
- **`python -m eval.cli split --golden FILE --out DIR` writes a deterministic dev / test split (`eval/split.py`) (PR #180).** Within each `tags[0]` category, ids sorted, even positions to dev and odd to test; nothing is random and the order of the file does not matter. Writes `dev_ids.txt`, `test_ids.txt` and `split.json` (counts per category, the golden file's name and hash) and prints the counts. Every case is placed whatever its `status`, so activating cases later moves nobody. Re-running on the same set is a no-op; on a changed set it refuses until `--force`, because the held-out half must not move between rounds.
- **`core.redaction.redact_error_text` and `redact_sql_literals` (PR #180).** The one redactor for text that leaves the server beside `redact_db_url`: quoted strings become `'…'` (an unterminated one to the end of the text), runs of four or more digits (Persian and Arabic-Indic too) become `#`, `PWD=` values and `user:password@` are masked first, the result is cut at 600 characters; and the SQL-literal redaction above.
- **`eval.cli run --live` generates through the server's own pipeline by default (`eval/pipeline.py`); `--single-shot` keeps the one-call behaviour (PR #180).** The default runs `llm.sql_agent.SQLAgent` (as `api.runner` builds it, with the router from the `LLM_*` settings): the guard, correctable and policy rejections (a policy rejection is not retried), up to `MAX_CORRECTION_ATTEMPTS` correction rounds with the 6.11.0 correction hints, source routing, the row cap the agent injects into a statement without one (`DEFAULT_TOP_N`), and `database.executor.execute_sql` with its query timeout, rolled-back transaction and `MAX_ROWS_RETURNED`. A round is a statement that the guard rejected or that failed to run. `TracingSQLAgent` overrides only the guard step to count rounds and keep each rejection; the generator hands back the result the agent already obtained for the final SQL (so it is not executed twice) and the reference query still goes to the real executor. A run that ran out of rounds is `guard_rejected` or `execution_error` with the last candidate SQL; a transport failure stays `generation_error`. **No principal policy:** the evaluation passes no `denied_columns`, so a deployment whose keys deny columns is measured as if they did not. `--single-shot` is the old behaviour, a model-only number; `--structured` needs it, and `--single-shot` needs `--live`. The mode is `pipeline` (`production` or `single_shot`) in the `--out` report and `run.pipeline` in the share file, and printed in the report; offline runs have none and are unchanged (no CI job runs `--live`).
- **A result cut by the row cap passes as `match_capped` (`eval/diagnosis.py`) (PR #180).** A result with exactly the cap's row count (the `TOP n` the agent injected, or `MAX_ROWS_RETURNED` if lower), fewer rows than the reference and every row a row of the reference under `compare`'s matching rules (for a reference with `ORDER BY ... TOP`, its first rows in that order) is scored `pass`: the analyst saw the right rows, truncated by policy. The diagnosis records `capped: true` and `row_cap` (a count; no row value is exported), and the class is in the summary distribution (12 classes now). **A count confirms it before it passes (`eval/capcheck.py`):** one more read-only query, `SELECT COUNT(*) FROM (<the model's SQL before the cap was injected>) AS q`, built with sqlglot (`tsql`; an `ORDER BY` without `TOP`/`OFFSET` is removed, a `WITH` clause hoisted and repeated or unnamed columns named, as SQL Server requires of a derived table) and run through the same executor, so the timeout and the read-only transaction apply. The case passes only if the count equals the reference's row count (`uncapped_rows`); an over-filtered query that returns a cap's worth of valid rows has a smaller count and fails as `actual_is_subset`. A count that cannot be built or run fails the case as `actual_is_subset` with `capped_unverified: true`, never a silent pass; when the reference result itself reached `MAX_ROWS_RETURNED` (so its length is not its size) the reference is counted the same way, `SELECT COUNT(*) FROM (<reference SQL>) AS q`, and the two counts must be equal (`uncapped_rows == reference_uncapped_rows`, also in the diagnosis; no "at least" rule), with a reference count that cannot run failing as `capped_unverified`. A row not in the reference, or a subset short of the cap, is still a failure (`actual_is_subset` and the rest); `--single-shot` has no cap, so the class never appears there. Tests: `eval/tests/test_diagnosis.py` (`TestMatchCapped`), `eval/tests/test_capcheck.py` (including a cut reference with equal, different and failing counts), `eval/tests/test_pipeline.py`.
- **`corrections`, `guard_rejections` and `execution_errors` per case are real, round by round (PR #180).** `guard_rejections` is `[{round, class, message}]` (round 1 is the first try) and `execution_errors` the same for statements that passed the guard and failed to run, so a repeated rejection is visible. In the share file the messages are redacted like the error text; the summary has `correction_rounds`, `guard_rejections` and `execution_errors`. `--single-shot` reports `corrections` 0 by construction, and its bundle note says so.
- **`compare` refuses two rounds whose `run.pipeline` differs; `--allow-mixed` compares them with a note (PR #180).** Reports written by `--out` carry the same field. A baseline (`--baseline`) is refused across pipelines too, like `--reference`; one written before the field existed counts as `single_shot`.
- **`eval.cli run --share ... --share-only-ids FILE` keeps the held-out half's content on the server (PR #180).** `FILE` is a list of case ids (`dev_ids.txt` from `split`, one id per line, or a JSON list). Only those cases carry the question, SQL, error text and diagnosis; every other case carries only `case_id`, `tags`, `passed` and `mismatch_class`, and the summary is computed from all cases first. The header has `detail` (`scope`, `cases_with_detail`, `cases_total`) and the `privacy` block says so; `compare --split test` works on such a file. An id not in the golden file is a warning; an empty or missing file is exit status 2. Needs `--share`.
- Documentation (PR #180): runbook §18.6 "Accuracy rounds with a reviewer" (the commands in order, what to send and what never to, how fixes come back, `compare`, the stop criteria and a privacy paragraph) with pointers in §18 and §19; the same in Persian, §۵.۵ of `docs/fa/getting-started.md`. Tests: `eval/tests/test_pipeline.py` (the correction loop with a fake model that fails once, repeated and exhausted rounds, the row cap, `--single-shot` making one call, the CLI modes, baseline across pipelines), `eval/tests/test_diagnosis.py` (every class), `eval/tests/test_share.py` (a whole `--live --reference live` run on SQLite with a fake model: no sentinel cell value in the file, with and without `--share-redact-literals`; error redaction; the refusals; the path guard; file mode and stable bytes), `eval/tests/test_rounds.py` (`compare` and `split`), `eval/tests/test_cli_startup.py`, `tests/test_redaction.py`.

### Changed

- **`eval.cli run --live` no longer measures a single model call (PR #180).** It used to generate once and apply the guard once, a model-only figure below what analysts get, because the server corrects a rejected or failing statement up to `MAX_CORRECTION_ATTEMPTS` times and caps rows. The default is now the server's pipeline (see Added); the old behaviour is `--single-shot`. `--live` figures recorded before this change are single-shot figures. `--structured` now requires `--single-shot` with `--live`.

### Fixed

- **A setup run against a model behind a response cache no longer writes one answer for every table (PR #181).** An operator ran the wizard against two databases through a gateway that kept a response cache: most tables of the first got the byte-identical alias list the model had given for the first table, the second database got that list again, and the business rules read as nonsense (`Value column: X. Volume column: X.` on a table where that makes no sense). The wizard wrote all of it without a word, because nothing in the reply says it came from a cache. Steps 3 and 4 now stop at the third identical answer in a row (above).
- **`python -m eval.cli --help` died in an import-time traceback on a machine with no, or a broken, `project_config/` (PR #180).** `eval.cli` imported the runner at module scope, which imports the prompt builder, which binds `knowledge.business_rules`. Everything that reads `project_config/` is now imported inside the command that needs it, so `--help`, `split` and `compare` need none. `run`, `verify` and `recall` check the files first (`core.config_preflight.exit_if_project_config_unusable`, as `scripts/prompt_budget.py` and `api/startup_preflight.py` do) and print one `cannot load project_config: ...` line with exit status 2. To make that possible `load_golden_cases` moved to `eval.store` (still importable from `eval.runner`) and `eval.determinism` imports the runner for type checking only. Tests: `eval/tests/test_cli_startup.py` (fresh interpreters: no `Traceback` in stderr for `--help`, nor, for a broken config, a second line).

### Upgrading

- **The live evaluation now measures the production pipeline by default (PR #180).** `python -m eval.cli run --live` generates through the server's own loop (guard, correction rounds, row cap), so its accuracy is the analyst's and is higher than a single-shot figure for the same model. A figure, a `--save-baseline` file or a share bundle recorded before this release is single-shot: `--baseline` and `compare` refuse to set it against a production run (`compare --allow-mixed` overrides, with a note). Record a new baseline on 6.12.0 after the restart, in the mode you will gate on (`--live --reference live --save-baseline ...`); add `--single-shot` to every command that must stay model-only, which still compares with a baseline recorded before the upgrade. Offline runs (CI) are unchanged. The run uses no per-key `denied_columns` policy, so a deployment that denies columns is measured as if it did not. For the rounds with a reviewer, add `--share-only-ids eval_data/split/dev_ids.txt` so the test half's content never leaves the server (runbook §18.6).
- **Run `python scripts/verify_deployment.py` after the upgrade, before trusting answers or running an accuracy round (PR #181).** The new check `Model endpoint answers each prompt itself` sends two tiny requests to the model through the backend the server uses. If it fails, a response cache in front of the model returned another prompt's answer: fix the gateway first (disable response caching for this client, or point `OPENAI_BASE_URL` and `WIZARD_LLM_BASE_URL` at the model server directly; runbook §20.10) and run the command again until it passes. It is skipped under `LLM_PROVIDER=mock` and, for a remote endpoint, while `LLM_ALLOW_REMOTE` is off; on the admin panel it is a deep check. The setup wizard also stops with `Step 3 stopped` when the model repeats itself (`--allow-repeated-answers` overrides, for a schema so small that identical answers are expected); a resumed run (`--resume`) continues once the endpoint is fixed.
- **One setting is new and optional: `LLM_CACHE_SUSPECT_MS` (default `50`, `0` = off) (PR #181).** It adds `suspected_cached_response: true` to the audit `llm` block (and the response's) of a request that returned byte-identical SQL to a different question faster than the threshold, and one `WARNING` per streak; it never blocks or changes a request. A reader of `audit_log.jsonl` that ignores unknown keys is unaffected, and records written earlier simply lack it. Set `0` to turn the warning off. It is in `.env.example`; no existing setting was renamed and no default changed.
- **No `requirements.lock` pin changed (PR #180, PR #181).** `git diff v6.11.0 -- requirements.lock` is empty. Run `pip install -r requirements.lock` as for every upgrade, but nothing new is installed, and no file format changed: a `project_config/` that loads today loads the same way.

## [6.11.0] — 2026-10-14

Table selection is now measured on a real, operator-written question set, and fixed. The largest cause of misses was that the retrieval configuration named tables by their bare name where `schema.yaml` keys them `Schema.Name`; configured names are now resolved to the schema-qualified keys, with smaller fixes for inflected and reordered aliases and for a year that never says the word. Recall on the held-out half of the question set went from 0.62 to 0.98; the questions and the tables stay with the deployment that measured them. Five new preflight checks tell a raw `database.schema_inspector_cli` draft from a curated `project_config/` (a draft copied over the curated files loads, starts and answers worse, and nothing used to say why); three can fail and two only advise. On the model side, `LLM_PREFIX_KEEPWARM_SECONDS` periodically re-sends the static prefix so a shared model server does not evict its cache between questions, the static prefix is about 2% smaller by formatting alone, `prefix_cache_hit` follows the `cached_tokens` the server reports instead of a prompt-size guess, and a correction round is given the real column names of the table the guard rejected.

### Added

- **`LLM_PREFIX_KEEPWARM_SECONDS` keeps the model server's prefix cache warm between questions (PR #178).** Default `0` (off). Above zero, a background thread (`llm.warmup.KeepWarm`) re-sends each static-prefix source's prefix every that many seconds: the request the start-up warm-up sends, built by the same code (`warm_prefix_cache`), so it cannot drift from it. **A source a real question used within the last interval is skipped** (`llm/prefix_activity.py` records, per prefix hash and never its text, when the router last served one), so under traffic the keep-warm sends nothing. It follows every rule of the start-up warm-up: nothing for a retrieval-path source, nothing under `LLM_PROVIDER=mock`, nothing to an untrusted endpoint without `LLM_ALLOW_REMOTE`, a failure is a log line and never fatal. A repeated outcome is logged once and then at `DEBUG` until it changes, so an endpoint that stays down is one warning and the pass that succeeds again says how many passes failed. It pauses while maintenance mode is on and stops with the server. `Settings.validate()` refuses a value that is neither `0` nor at least `30`. `300` is recommended on a shared model server; runbook §20.7 gives the reasoning, the cost under traffic (none) and idle (about 12 small requests an hour per source), and how to check from `cached_tokens` that the interval is short enough.
- **`GET /admin/llm/warmup` shows the keep-warm's last run and outcome; `POST /admin/llm/warmup` returns the same under `keepwarm` (PR #178).** `operations` or `security` keys can read it: `enabled` (with a `reason` when it is not), `interval_seconds`, `running`, `paused`, the number of `runs`, `last_run_at` (UTC), `last_run_outcome` (`warmed`, `skipped`, `failed`, `paused` or `busy`) and one entry per data source in `last_run`, the entries `POST` already returned. No prompt text, endpoint or key.
- **Runbook §20.8 says what `prefix_cache_hit` is and is not (PR #178).** The audit field is `prompt_tokens < 0.5 × the prefix estimate`, a rule made for a backend that reported only the tokens it computed. An OpenAI-compatible server reports the whole prompt whether or not it came from the cache, so today `true` means a short prompt and `false` a prompt about as large as the whole prefix, cached or not. The section gives the reading that works (the request's time against its size, and `cached_tokens` where the server reports it) and corrects the §20.4 row that took `false` for a cold cache.
- **The preflight now says when `project_config/` is a raw draft of `database.schema_inspector_cli` instead of the curated configuration (PR #177).** An operator who copied the inspector's drafts over a curated `project_config/` got a deployment that loaded (6.9.3 made the drafts valid YAML), started and answered worse, and nothing said why: 75 curated tables in two databases had become 278 tables from one, about 200 of them staging and change-capture tables; one database was gone; every description was the placeholder `<schema.table> — auto-detected <kind> table (~N rows)`; column notes quoted live sample values (phone numbers, street addresses); `aliases.yaml` had an empty `ring_aliases` and synonym seeds taken from sample values. Five checks in `scripts/verify_deployment.py` (`core/config_doctor.py` reads the files), after `project_config/ loads` and before `Rate limit sane for deployment`; each reads files only, with no database call, so the admin health card runs them without "deep checks":
  - `project_config/ is curated, not a raw draft` fails when any `*.yaml` file of `project_config/` still carries the `# AUTO-GENERATED DRAFT` header (the first ten lines are searched), or when more than a quarter of `schema.yaml`'s table descriptions are the inspector's placeholder. The detail names the files and the counts, says to restore the curated files and add tables with `python scripts/sync_schema.py --add-tables 'Schema.*'`, and points to the runbook (§2.3, §16.3); a file that was really reviewed passes once its header line is deleted. A table that `--add-tables` appended is described by its own name, so a curated file that grew that way stays curated. `schema.yaml` is read even when a repeated key (a draft from before 6.9.3) keeps the application from loading it, every occurrence counted.
  - `Aliases are curated` fails when more than half of `aliases.yaml`'s synonym keys are not words (no run of two letters: timestamps, numbers), or when more than half of its synonym lines still end in the inspector's `# REVIEW` marker (a seed is a sample value of any text column, so only part of a real draft is timestamps; all of it carries the marker). An `aliases.yaml` that is not YAML (a quote in a sample value, before 6.9.3) is read from its lines. An empty `ring_aliases` is advice, not a failure: only `ValueRetriever.extract_ring` reads it, to set the `Ring` filter from a ring (hall) name in the question, so a warehouse without that dimension leaves it empty, as the setup wizard does when it finds no dimension table; the `[PASS]` line says `advice: ring_aliases is empty` while `schema.yaml` or `entities.yaml` exist.
  - `Every data source is described` fails when a source of `datasources.yaml` (or the one `default` source without the file) has no table in `schema.yaml`, a table belonging to the source its `datasource:` line names (to each, for a list) and to the default source when it has none. That is a whole database lost; the detail names the source and the way back.
  - `No staging or change-capture tables` and `No live sample values in descriptions` are advice: they never fail. There is no `WARN` status; advice is a `[PASS]` line whose detail starts with `advice:`, which leaves the exit code, the CLI summary and the admin card (it shows any detail, and has no other status to show) as they are. The first counts the tables per schema that match `STAGING_PATTERNS`; the second counts, as phone-like (7 or more digits, Persian digits too), national-id-like (exactly 10) and address-like (a long segment of words with digits), the notes whose `e.g.` list quotes a value (an `e.g.` inside parentheses is prose, and dates are left alone) and lists the table keys. Neither detail ever contains a value from a file.
- **`STAGING_PATTERNS` sets what counts as a staging or change-capture table (PR #177).** Comma-separated `fnmatch` patterns, case-insensitive; a bare pattern is matched against the table's schema, a `table:` prefix against its name. The default is `*_Stg,*stg*,*staging*,table:*_Inserts,table:*_Updates,table:*_Deletes`; an empty value turns `No staging or change-capture tables` off. In `.env.example` and `config.Settings.staging_patterns`.
- Documentation (PR #177): `docs/deployment-runbook.md` §3 and §3.1 describe the five checks and the advice convention, and §17 step 3 lists the new refusal with the other things the preflight catches after an upgrade; the Persian guide (`docs/fa/getting-started.md`, §۰.۴ and §۴) says the same in Persian. Tests: `tests/test_config_doctor.py` (thresholds, empty and unreadable files, a single source, the patterns setting, no value in any message), `tests/test_setup_on_warehouse_shape.py` (`TestPreflightOnAnInspectorDraft`: the inspector's real drafts of the warehouse-shaped fixture make every check fire, and `TestPreflightOnTheCuratedConfig`: its curated config passes), `tests/test_admin.py` (`TestCurationChecksOnTheCard`).

### Changed

- **The static prefix is smaller by formatting alone (about 2% of a large schema) (PR #178).** A column line is `- Name: description` instead of `  - Name: description`, and each business rule is stripped before the rules are joined, so a rule's trailing newline no longer adds a blank line between rules. Nothing the model reads is removed or reworded; `tests/test_prefix_compaction.py` checks that every table, description, column, relationship, rule, metric and example is still there, and reverses the two changes to get the previous release's prefix back byte for byte. The retrieval path's schema block is the same function, so it is that much smaller too. On the synthetic `project_config.example` the prefix estimate goes from 1,815 to 1,798 tokens (7,260 to 7,193 characters); the saving is per column, so a schema with hundreds of columns saves hundreds of tokens. The golden hashes in `tests/test_source_prompts.py` were re-captured on purpose, for this reason.
- **Table names in `entities.yaml`, `fact_tables`, `fact_patterns` and `always_include` are resolved to `schema.yaml` keys (`retrieval/table_keys.py`) (PR #176).** A bare name resolves the way the SQL guard resolves a reference in a query: an exact key (brackets and case ignored) is itself; otherwise the keys with that bare name, narrowed by any qualifier written (`Dim.Customer`). A name that matches several keys is read in the schemas the configuration's other, unambiguous names live in, or in the data source whose `keywords:` the question contains (`datasources.yaml`); when neither narrows it, every candidate is kept; a name that matches no key is kept as written. `ContextRetriever.entities` and `.facts`, `RetrievalContext.selected_tables`, the pruning stage, join expansion's choice of the fact table and the fact/dimension split of the extras and of the TF-IDF fallback all use the keys. A configuration that already writes full keys is unchanged. Tests: `tests/test_table_keys.py`.
- **Alias matching also compares whole words after plural stemming, and a multi-word alias matches its words in any order (`retrieval/table_evidence.py`) (PR #176).** The substring test stays; beside it `city` finds `cities` and the plural alias `cities` finds "city" (`-ies` for `-y`, and a plural alias for a singular question), and an alias such as `gear tooth pitch` is found in "pitch of each tooth on the gear" because all its stemmed words occur. A different word is not an inflection (`order` is not found in `ordinary`). Tests: `tests/test_table_evidence.py` (`TestAliasHits`).
- **A four-digit calendar year in a question adds the words `year` and `سال` to the expanded question (`schema_data/retriever.py`) (PR #176).** "Totals in 2023" is about a period but never says the word, so the date dimension's `always_include` triggers and the descriptions did not see it. The years are 1300-1599 (Solar Hijri) and 1900-2199 (Gregorian), Persian and Arabic-Indic digits included; a number such as `2000` in "orders above 2000" is also taken for a year, at the cost of one extra table. Tests: `tests/test_retriever.py` (`TestExpandSynonyms`).
- **A correction prompt names what exists when the guard rejected a name that does not (`llm/correction_hints.py`) (PR #178).** In the real-use audit log a request that began with an unknown-column or unknown-table rejection used both correction rounds (three model calls) and was refused in the end every time; the retry carried only the guard's message, so the model guessed again. The retry now adds, deterministically and from `schema.yaml`: for an unknown column, the table's real column names (names only, in `schema.yaml` order, `... and N more` past 200); for an unknown table, the closest real tables as the reference to write in `FROM` (limited to the question's data source). A column the key's `denied_columns` fully denies is left out; a join-only column is kept and marked `(join key only)`, the one use the guard allows; an unreadable policy adds nothing. The text goes to the question segment of the retry like the rest of the correction text, never the static prefix, and is not repeated once shown. `CorrectableRejection` for an unknown column now carries the `table` it is about (`SqlGuardRejection.table`). Every other error gets the correction text it got before, byte for byte. Both loops (`SQLAgent`, `session/engine.py`) use it. Runbook §20.9.
- **`prefix_cache_hit` in the audit `llm` block follows the model server's `cached_tokens` when it reports one (PR #178).** `usage.prompt_tokens_details.cached_tokens` (vLLM with `--enable-prompt-tokens-details`) is recorded as `cached_tokens`, `prefix_cache_hit` is true when it covers at least half of the static prefix (of the whole prompt when shorter), and `prefix_cache_hit_source` says `"server"`. Without it the older prompt-size rule is the fallback and the source is `"estimate"`; a record written before these fields reads as an estimate. That rule compared `prompt_tokens` with the prefix estimate, which was a cache signal for a backend that reported only the tokens it computed and, on an OpenAI-compatible server that counts the whole prompt, says only that the prompt was short: in the real-use log a full-size prompt served from the cache in under a second counted as a miss and short prompts that were computed from scratch counted as hits. `scripts/analyze_audit_log.py` prints a real hit rate over the calls that reported it and the `llm` stage's time for `hit`, `miss` and `unreported` calls (`latency_by_cache`, also `cache_behaviour.server_hit_rate` and the quality report's `latency.llm_by_cache`); the admin prefix-reuse tile prefers the server rate and names its source, and the web badge marks an estimate `(est.)`. `docs/api-contract-v2.md` §6 and runbook §20.8.

### Fixed

- **A retrieval configuration that names tables by their bare name no longer loses the tables it names when `schema.yaml` qualifies the keys (PR #176).** A warehouse that repeats table names across schemas has keys such as `sales.Customer`; an `entities.yaml` written before that still said `Customer`. The alias retriever returned `Customer`, which is not a key: the schema block skips an unknown name, the ranking never scored it, the pruning stage kept it for the wrong reason, and `name in FACT_TABLES` was false for every key, so the extras and the TF-IDF fallback could not tell facts from dimensions. A plain count of one dimension table retrieved four other tables and not that one. It was by far the largest cause of the misses on the real question set that found it (`docs/design/RETRIEVAL.md` §8 has the method and the generic mechanisms; the figures stay with that deployment). Tests: `tests/test_table_keys.py`.
- **`ContextRetriever` finds an entity's label and its static value filter through the table key (PR #176).** The «list of values unavailable» warning looked the label up by the table key in `ENTITIES`, and the dimension-vocabulary pass was skipped for a table only when `ValueRetriever` had keyed a filter by that exact table name; both now go through the same resolution, so a bare entity name still gives its label and still makes the static filter win. Tests: `tests/test_table_keys.py` (`TestContextRetrieverWithBareEntityNames`).

### Upgrading

- **The new preflight checks may fail on a `project_config/` that has no `datasource:` lines when there is more than one data source (PR #177).** `Every data source is described` counts a table with no `datasource:` line toward the default source only, so a `datasources.yaml` that lists a second source while `schema.yaml` has no such line reports the second source as having no table. Run `python scripts/sync_schema.py`, which writes the lines; review `schema.synced.yaml`, replace `schema.yaml` and run `python scripts/verify_deployment.py` again (runbook §16.3). A raw `database.schema_inspector_cli` draft copied over the curated files fails `project_config/ is curated, not a raw draft` as well: restore the curated files, then add tables with `python scripts/sync_schema.py --add-tables 'Schema.*'`. The other checks only advise.
- **The prefix text changes once, so `prefix_version` and every cached answer's key change once (PR #178).** Answers cached by the previous release are not found after the restart, and the model server's prefix cache is cold for the new text. Run `POST /admin/llm/warmup` after restarting (or leave `LLM_PREFIX_WARMUP_ON_STARTUP` on, which does it). Nothing else needs doing; no file format changed.
- **A source whose prefix estimate was within about 2% above `PROMPT_RETRIEVAL_TOKEN_BUDGET` may now take the static path instead of retrieval (PR #178).** The estimate dropped by the saving above. Run `python scripts/prompt_budget.py` if a source sits that close to the budget; the line it prints says which path each source takes.
- **Add `--enable-prompt-tokens-details` to the vLLM launch command if you want `prefix_cache_hit` to be truthful (optional) (PR #178).** With it the server reports `cached_tokens` and `prefix_cache_hit` is the server's own answer. Without it `cached_tokens` stays `null`, `prefix_cache_hit_source` stays `"estimate"` and the analysis reports the real rate as `n/a`. Two new keys appear in the audit `llm` block (`prefix_cache_hit_source`, `cached_tokens`); a reader of the log that ignores unknown keys is unaffected, and records written earlier simply lack them.
- **The text of a correction round changes for an unknown column or table (and only then) (PR #178).** Nothing to configure; the retry is a few dozen tokens longer for those two rejections.
- **Set `LLM_PREFIX_KEEPWARM_SECONDS=300` on a model server that other people also use (optional) (PR #178).** Left at `0` nothing is sent, as before; the value must be `0` or at least `30`. Leave it at `0` when this deployment is the only user of the model server, when the server does not cache prefixes, or when every source is on the retrieval path.
- **Retrieval can select different tables by default for a configuration that names tables by their bare name (recall is the point; precision and token use follow it) (PR #176).** Run `python -m eval.cli recall --golden eval_data/golden.jsonl` before and after, as in the runbook (§18.4); on such a configuration the tables the aliases name are now in the prompt, so the mean number of retrieved tables rises. A configuration that writes full `Schema.Name` keys everywhere selects what it did before, apart from the inflection, word-order and year rules above.
- **The dimension vocabulary is now consulted for entities whose bare name used to resolve to nothing (PR #176).** `PREFETCH_COLUMNS` is keyed by schema key, so for `entities.yaml` written with bare names the prefetch and the match against the question's words were silently skipped; they now run for the tables that declare `prefetchable_columns`, as they always did for a configuration with full keys. Nothing is read from the warehouse on the question path (the cache is filled in the background, as before).
- **If a bare name in `entities.yaml` or `retrieval_hints.yaml` matches tables in several schemas, qualify it (`sales.Customer`) when the schema the other names live in is not the one you mean (PR #176).** The rule above reads it in the schemas of the file's other names, and keeps every candidate when it cannot tell.
- **Two settings are new, both optional, and no existing setting changed (PR #177, PR #178).** `STAGING_PATTERNS` (advice only) and `LLM_PREFIX_KEEPWARM_SECONDS` (default `0`, off) are added to `.env.example`; no other setting is renamed and no default changed (the `RETRIEVAL_*` settings keep theirs, `docs/design/RETRIEVAL.md` §6), and no `requirements.lock` pin changed. A `project_config/` that loads today loads the same way, apart from the preflight note above.

## [6.10.0] — 2026-10-13

Analysts can now confirm a correct answer with one click, and each confirmation becomes a reviewed case in the golden set, so everyday use builds the accuracy gate's reference instead of a separate review session. A real-use quality report, in `scripts/analyze_audit_log.py` and as a new admin card, joins the audit log with the confirmations and flags: execution success, guard-rejection, correction and empty-result rates, the confirmed share of rated answers, and latency per stage. Every setup tool is now tested on a warehouse-shaped fixture, which found and fixed three bugs: the inspector's entities, aliases and relationships drafts broke on a sample value with a quote or a backslash, an empty `relationships.yaml` crashed join-path building, and `database.catalogue` showed none of the tables in an attached SQLite database.

### Added

- **"This answer is correct" (پاسخ درست است): analysts confirm a correct answer with one click (PR #174).** The golden set stays empty because building it costs analysts a separate review session; everyday use now builds it. `POST` and `DELETE /v2/sessions/{sid}/turns/{tid}/confirmation` (and `GET /v2/sessions/{sid}/confirmations`, so a reopened transcript shows each turn's state) store a confirmation in the new `turn_confirmations` table of the application database (Alembic revision `0005_turn_confirmations`; created at start-up like the other tables; carried by `scripts/migrate_app_db.py`). Like a wrong-answer flag it holds identifiers only (`session_id`, `turn_id`, `principal_id`, `request_id` resolved from the audit record, `created_at`), never the question, the SQL or any data; there is at most one per turn and principal, confirming again is idempotent, and a principal withdraws only their own. Only the turn's owner may confirm it (404 for anyone else, as for flagging), and only a turn whose audit record shows SQL that ran and returned a result (`409` otherwise). A principal who flags a turn they had confirmed, or confirms one they had flagged, keeps only the later action: the earlier one is removed (an admin-resolved flag is kept, since triage recorded a decision on it). The web UI shows a «پاسخ درست است» toggle (`aria-pressed`, keyboard reachable) on the answer itself, in a footer under the result table or chart and outside the collapsed details drawer, on successful answers only; the wrong-answer flag stays in the drawer. The toggle keeps its box while it changes state, and its confirmed state (a check mark, a thicker border and a tint) shows in both themes. `docs/api-contract-v2.md` §3 documents the routes; `docs/deployment-runbook.md` §18.1.1 and the Persian guide §۵.۱.۱ show the flow. Tests: `tests/test_appdb_confirmations.py`, `tests/test_v2_confirmation_routes.py`, `tests/web_ui/test_web_ui_confirmation_control.py`, `tests/web_ui/test_web_ui_confirmation_placement.py`.
- **`scripts/harvest_golden.py` harvests confirmed answers as reviewed golden cases (PR #174).** On by default (`--no-confirmations` opts out). Each confirmed turn becomes a case with `status = "reviewed"` (a person confirmed its `expected_sql`, the SQL that ran; `eval.cli verify` has not run it yet), the confirming principal's id and the confirmation time in its `notes`, and the tag `origin:confirmed`. Confirmed cases come before the stratified sample and are never dropped by `--n`; two confirmations of the same normalised question are one case, the most recent one; a confirmation whose audit record has rotated away is counted and skipped. `scripts/golden_sheet.py export` lists them with the verdict already `correct`, and `python -m eval.cli verify --accept` is still the gate that activates them. The console output stays counts only, and the file stays owner-only in `eval_data/`. Tests: `tests/test_confirmed_golden_cases.py`.
- **A real-use quality report, in `scripts/analyze_audit_log.py` and in the admin panel (PR #174).** `observability.quality.build_quality_report` joins the audit log with the confirmations and flags over the last 7 days (`--quality-days N`) and all time, overall and per data source: execution success, guard-rejection, correction, empty-result and out-of-scope rates, error rate by class (reusing `failure_taxonomy`), confirmed and flagged answers with the confirmed share of rated answers (labelled a lower-bound signal, not accuracy), and end-to-end and per-stage latency p50/p95 including the `llm` split into `ttft_ms` and `generation_ms` (reusing `latency_report` and `llm_latency_split`; the split needs `LLM_STREAM_TIMINGS=true`). It copes with an empty log and with records from older versions that lack fields. `analyze_audit_log.py` prints it as a new section (and under `quality` with `--json`), counts and percentiles only. The read-only `GET /admin/quality?days=7` (operations or security, like `/admin/usage`) serves it to a new admin card «کیفیت در استفاده‌ی واقعی», which notes that the confirmed share is not accuracy and that the stage split appears only with `LLM_STREAM_TIMINGS=true`. The route reads the whole audit log, so it is cached per window for `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` and the card stays in the 30-second auto-refresh instead of joining `EXPENSIVE_CARDS`; its refresh button recomputes. `docs/deployment-runbook.md` §20.6, the Persian guide §۷ and `docs/admin-panel-architecture.md` §3 describe it. Tests: `tests/test_observability_quality.py`, `tests/test_admin_quality_route.py`, `tests/web_ui/test_web_ui_admin_quality_card.py`.

### Changed

- **Every setup tool is now tested on a warehouse-shaped fixture (PR #173).** The suite's fixtures were small, tidy sample schemas, and twice the first real use of a setup tool found a bug they could not. `tests/_warehouse_shape.py` builds, once per session and deterministically, two SQLite data sources with the shape of a real warehouse and invented names: 262 and 75 tables in ten and seven schemas (fact, dimension and staging schemas, change-capture tables, `Replay`, `Alerts`, `Metadata`, `dbo`), the same name in up to four schemas, one dimension in both sources, no declared foreign keys, empty tables, Persian text, and a name with a space and one with a dot; and a curated 34-table `project_config/`. `tests/test_setup_on_warehouse_shape.py` runs `database.schema_inspector_cli`, `scripts/sync_schema.py`, `scripts/assign_datasources.py`, `scripts/prompt_budget.py`, the `scripts/verify_deployment.py` checks that need no SQL Server and `python -m eval.cli recall` on it. They must stay green; the README's Tests section and the tutorial's "Writing tests" say so.

### Fixed

- **The entities, aliases and relationships drafts of `database.schema_inspector_cli` no longer break on a sample value with a quote or a backslash (PR #173).** Only the `schema.yaml` draft wrote its values through `_yaml_str`; the other three wrote `"{value}"` by hand, so the first value read from a live row such as `Pipe 5" steel` or `Path\to` made `entities.yaml` (its `sample_values`) or `aliases.yaml` (a synonym key) unreadable, and a table, schema or column name with a quote did the same in `relationships.yaml`. Every table, schema, column, type, sample value and reference is now quoted with the same helper as `schema.yaml`. Tests: `tests/test_schema_inspector.py` (`TestDraftsQuoteTheValuesTheyQuote`).
- **A `relationships.yaml` with no entry no longer crashes join-path building (PR #173).** The inspector's draft for a database that declares no foreign key was a bare `relationships:`, which is YAML null, and `retrieval.join_paths` (which builds the join graph for questions) and `database.relationship_map` iterated it as a list and raised `TypeError`; a hand-edited file whose last entry was commented out did the same. The draft now writes `relationships: []`, and both readers treat a null like an empty list. Tests: `tests/test_schema_inspector.py`, `tests/test_join_paths.py`, `tests/test_relationship_map.py`.
- **`database.catalogue` reads every `ATTACH`ed SQLite database as a schema (PR #173).** `read_catalogue`, `list_tables` and `list_columns` reflected only the connection's own database on a dialect other than SQL Server, so a SQLite source with attached schemas (the tests' stand-in for a warehouse with many schemas) showed none of its tables: `sync_schema.py` called every one absent and the "table lives in another data source" hint of `Tables are in their data source` found nothing. Each attached database is now a schema of its own name; the connection's own database is still schema `""`, and no other dialect changes. SQL Server is unaffected. Tests: `tests/test_catalogue.py` (`TestAttachedSqliteDatabasesAreSchemas`).

### Upgrading

- **The new `turn_confirmations` table is created automatically when the application starts (PR #174),** like the other application-database tables, so the default setup needs no manual step.
- **Deployments that manage the application database schema with Alembic instead should run `alembic upgrade head` from the repository root (PR #174);** it applies migration `0005_turn_confirmations`.
- **Tell analysts about the «پاسخ درست است» toggle, and review what they confirm (PR #174).** Any analyst can confirm their own answers; there is no separate permission for it. A confirmed answer becomes a reviewed golden case (`status = "reviewed"`, tag `origin:confirmed`), and `python -m eval.cli verify --accept` checks that its SQL runs, not that the SQL is right, so a wrong confirmation would become part of the accuracy gate. Check the `origin:confirmed` rows of `scripts/golden_sheet.py export` before accepting them, or harvest with `--no-confirmations` until you trust the confirmations you see. `docs/deployment-runbook.md` §18.1.1 shows the flow.
- **Set `LLM_STREAM_TIMINGS=true` if the quality card should show the time-to-first-token and generation split (PR #174).** Without it the card still shows end-to-end and per-stage latency, and notes that the split appears only with that setting.
- **Nothing else changes (PR #173, PR #174).** No setting is renamed and no `requirements.lock` pin changed. A `project_config/` that loads today loads the same way.

## [6.9.3] — 2026-10-13

A `schema.yaml` drafted by `database.schema_inspector_cli` is now valid for a warehouse that repeats table names across schemas (every table carries `db_schema`, shared names are keyed `Schema.Table`), the CLI can leave schemas out with `--exclude-schemas` and ends with a per-schema count, and a broken `project_config/` ends the server and `scripts/prompt_budget.py` in one readable line instead of a traceback, with every duplicate YAML key reported at once. The admin panel no longer leaves a card blank after a 401 or 403, and the preflight's query-timeout probe no longer logs the timeout it expects as an error.

### Added

- **`--exclude-schemas` on `python -m database.schema_inspector_cli` (PR #171).** A warehouse with many staging, replay and scratch schemas had no way to leave them out of the drafts except by listing every other schema in `--include-schemas`. `--exclude-schemas "*_Stg,Replay,Alerts"` takes comma-separated `fnmatch` patterns, matched against schema names without regard to case, applied in the inspector's table discovery next to `include_schemas`; when both are given, exclusion wins. A run now ends by printing to stderr the table count per schema, a line saying the drafts are for a first install only (an existing deployment adds tables with `python scripts/sync_schema.py --add-tables 'Schema.*'`, which keeps the curated text), and a warning when more than 150 tables were written. `docs/deployment-runbook.md` §2.3 and §16.3 and `docs/fa/getting-started.md` §0.3.2 say the same. Tests: `tests/test_schema_inspector.py`.

### Fixed

- **The schema inspector no longer writes a `schema.yaml` draft that the loader refuses (PR #171).** The draft keyed every table by its bare name and gave none a `db_schema`, so a warehouse that repeats a table name across schemas produced a file with a duplicate mapping key for each repeat (about 70 in the deployment that hit this). Every table now carries `db_schema` when its schema is known; a name unique in the run stays bare, and a name that occurs in more than one schema is keyed `Schema.Table` for every occurrence, the convention of `docs/design/TABLE-NAMES.md`. The `relationships:` entries in the draft and the entity names in the `entities.yaml` draft are spelled the same way, and a run with no foreign keys writes `relationships: []` instead of a null the validator rejects. Tests: `tests/test_schema_inspector.py` (a snapshot with the same table in two schemas passes `validate_schema_yaml_text`; a unique name stays bare).
- **A YAML file with repeated keys now reports all of them in one error, not only the first (PR #171).** `safe_load_strict` raised on the first repeated key, so a file with seventy of them took seventy runs to fix. It now reads the whole document, at every mapping level, and raises once: a single repeat keeps the message `duplicate key 'X' (first on line A, again on line B)`; several give `N duplicate keys: 'X' (lines A, B, C); ...` in file order, at most 20 and then `... and M more`. Callers that wrap the error (`knowledge.config_loader.load_yaml`, `validate_schema_yaml_text`, the preflight's `project_config/ loads`) pass it through unchanged. Tests: `tests/test_yaml_loading.py`.
- **A broken `project_config/` now ends `uvicorn api.server:app`, `python -m api` and `scripts/prompt_budget.py` in a readable message, not a traceback (PR #171).** `security.sql_guard` binds the schema allowlist at import time, so a bad `schema.yaml` (or any file the import chain reads) crashed those programs with a forty-line traceback. `core/config_preflight.py` now runs the real loaders for those files ahead of the imports that read them: `prompt_budget.py` prints `cannot load project_config: <message>` and exits with code 2, as `sync_schema.py` does; the server prints that line, one pointing to `python scripts/verify_deployment.py` and `nothing was started`, and exits with code 2. It stays fail-fast (there is no degraded mode), and nothing changes when the configuration is valid. Tests: `tests/test_config_preflight.py`.
- **An admin card is never left blank after a 401 or 403 (PR #171).** `refreshOne` in `web/admin/main.js` cleared the card, then for a 403 that is not a capability gap and for a 401 only raised the page-level banner or notice, so the card stayed an empty box. Both now also write a sentence into the card, in the `admin-loading` style: the key has no admin access for this section (see the banner), or the server did not accept the key. Tests: `tests/web_ui/test_web_ui_admin_card_errors.py`.
- **The preflight's query-timeout probe no longer logs the timeout it expects as an error (PR #171).** On a `nolock: true` source `check_query_timeout` first tried to add `WITH (NOLOCK)` to its `WAITFOR DELAY` statement (logging `WITH (NOLOCK) was not added (reason: parse_error)`) and then logged the expected timeout as `SQL execution failed on data source ...` with the full statement, once per data source, while the check passed. `execute_sql` gains a keyword-only `probe` flag that only this check sets: the statement is sent as written and its failure is logged at DEBUG. Real queries keep their hints and their error line. Tests: `tests/test_verify_deployment.py` (`TestCheckQueryTimeoutIsQuiet`).

### Upgrading

- **Nothing to change on a working deployment (PR #171).** A `project_config/` that loads today loads the same way, and no setting, file format or `requirements.lock` pin changed. Run `python scripts/verify_deployment.py` before restarting, as for any upgrade.
- **If `project_config/schema.yaml` (or `entities.yaml` / `aliases.yaml`) was replaced with a `schema_inspector` draft, restore your curated files (PR #171).** Copy them back from the backup taken before the replacement, and add the tables you want from the databases with `python scripts/sync_schema.py --add-tables 'Schema.*'` instead; it keeps the text you wrote. The inspector's drafts are for a first install only: every description is a placeholder, column notes may quote values read from live rows, and the `entities.yaml` and `aliases.yaml` drafts carry no curated aliases (their `ring_aliases` are empty), so replacing working files with them discards the curated text the prompt and the retrieval depend on. For a new install the drafts now load without duplicate-key errors (see Fixed), but they still need the review described in the runbook (§2.3) before they go into `project_config/`.

## [6.9.2] — 2026-10-10

The preflight check `Schema structure matches the databases` and `sync_schema.py --check` no longer fail on a `schema.yaml` written before 6.9.0 just because its column types are not recorded yet, and a failure is never a row of zeros: it names what differs, or says plainly that the counts do not cover it.

### Fixed

- **A `schema.yaml` written before 6.9.0 no longer fails the preflight check `Schema structure matches the databases` (and `sync_schema.py --check`) when nothing in the databases changed (PR #169).** Such a file has no `column_types:` map, so every column the database has landed in the sync plan's `recorded` list, `changed` was true, and the check failed with a message whose counts were all zero (`0 column(s) missing from schema.yaml; 0 column(s) and 0 table(s) no longer in the database; 0 column type(s) differ; 0 datasource: line(s) to set`), because that message never counted `recorded`. Every installation upgrading from 6.8 or earlier hit it on its first preflight. A column type that is merely not recorded yet is no longer drift: when recording types is the only edit a sync would make, the check passes and says `N column type(s) not recorded yet -- run python scripts/sync_schema.py once to record them (review schema.synced.yaml, replace schema.yaml), so later type changes are detected`, and `--check` exits 0 with the same note. `SyncResult.only_records_types` decides this from the plan by rendering the synced text with the recorded types left out and comparing it with the input, so a column to add, a differing type, a marker, a `datasource:` line, `--prune` and `--add-tables` all still count as drift. A failure adds `N column type(s) to record` to its counts, lists `--prune` and `--add-tables` effects when they are what differs, and a changed file that none of the counts explains now says `schema.yaml differs from the synced text in a way the drift counts do not cover -- run python scripts/sync_schema.py --dry-run to see it` instead of a row of zeros; `--check` prints the same counts. A plain `sync_schema.py` run still records the types and writes `schema.synced.yaml` as before. `docs/deployment-runbook.md` §3.1, §16.3, §17 and the Persian guide are corrected. Tests: `tests/test_schema_sync.py`, `tests/test_verify_deployment.py`, `tests/test_sync_schema_script.py`.

### Upgrading

- **`Schema structure matches the databases` now passes on a `schema.yaml` that records no column types (PR #169).** An installation coming from 6.8 or earlier sees the check pass with a `N column type(s) not recorded yet` note instead of failing; run `python scripts/sync_schema.py` once (runbook §17 step 14) to record them, so later type changes are detected. Nothing else needs doing.

## [6.9.1] — 2026-10-09

Every command-line program now writes UTF-8, so Persian output no longer crashes a Windows console or a redirected or piped stream: one shared helper, `core.console.use_utf8_console`, is called from every entry point's `__main__` block, and `scripts/release_notes.py` keeps its own standalone copy for the release workflow.

### Changed

- **docs: the upgrade path from 6.7 to 6.9.1 is complete.** `docs/deployment-runbook.md` §17 and the Persian guide's part 4 are now "Upgrading from 6.0 to 6.9.1", with ordered steps for the 6.8.0, 6.9.0 and 6.9.1 **Upgrading** notes (including that the first preflight on 6.9.x fails `Schema structure matches the databases` until `schema.yaml` is synced once, and that `sync_schema.py` arrived in 6.9.0, not 6.8.0). New runbook §18.5 and Persian §5.4 list the eleven `RETRIEVAL_*` settings and `eval.cli recall`; runbook §2.1, §2.2, §3.1 and the Persian guide are corrected for 6.8.0 (the preflight reads all ten `project_config/` files; the setup wizard follows `OPENAI_*`, obeys `LLM_ALLOW_REMOTE`, masks the connection string and resumes). `.env.example` and the README configuration table gain the `RETRIEVAL_*` settings; the README quick start gains `sync_schema.py`, its project structure the 6.8.0 to 6.9.0 modules, and its tests section `requirements-dev.txt` and `ruff check .`. No code changed.

### Fixed

- **Every command-line program now prints Persian on a Windows console (PR #167).** Reports, prompts and error messages carry Persian questions, table names and aliases; when output is piped or redirected, a console on a code page such as cp1252, cp1256 or cp437 cannot encode them, and `print` raised `UnicodeEncodeError` part-way through the output. Only `eval.cli` and `scripts/release_notes.py` guarded against it, each with its own copy. `core/console.py` now has the one helper, `use_utf8_console()`, which switches `sys.stdout` and `sys.stderr` to UTF-8 and keeps each stream's `errors` handler (`reconfigure` would otherwise reset `stderr` from `backslashreplace` to `strict`). Every entry point calls it first in its `if __name__ == "__main__":` block: `app.py`, `setup_project.py`, `webapp/app.py`, `database/schema_inspector_cli.py`, `eval/cli.py`, `eval/benchmarks/retrieval_synth.py`, `tests/test_stress.py` and every program in `scripts/` except the demo server. The web server (`python -m api`) is unchanged. `scripts/release_notes.py` keeps its own stdlib-only copy of the helper, because the release workflow fetches that file alone; a test keeps it standalone and in step with the shared one. `tests/test_cli_utf8_console.py` runs each program on a cp1252 console and fails when a new `__main__` block does not call the helper.

### Upgrading

- **Drop any console-encoding workaround (PR #167).** If a script or scheduled task sets `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1` or runs `chcp 65001` only so Persian output does not crash, it can go; leaving it in place is harmless. Nothing else needs doing.

## [6.9.0] — 2026-10-09

Table selection is now measured and improved (an offline recall harness, a 400-table synthetic benchmark, join-path expansion, inferred foreign keys and pruning: mean recall 0.71-0.75 to 0.95-0.97 on the benchmark); `schema.yaml`'s structure can be synced from the databases with the curated text kept, and the sync reports the prompt-size cost of what it adds; the prefix cache is warmed at start-up and the `llm` stage can be split into waiting and generating, with a `content: null` truncation fix; and ruff runs in CI, with optional absolute accuracy floors, a lazy `knowledge` import, one shared connection-string redactor and a password-masking fix.

### Added

- **`python scripts/sync_schema.py`: a reviewed, curation-preserving sync of `schema.yaml`'s structure (PR #162).** It reads every data source through the application's own engines (three `INFORMATION_SCHEMA` queries per source: tables, views, columns, data types, nullability, primary and foreign keys; names only, never a row) and writes `schema.synced.yaml` next to `schema.yaml`, which it never overwrites. The file is edited as text, so every comment, quote, description, flag and ordering survives. Rules: `datasource:` is set or repaired (a name, or a list for a table in several sources, as `assign_datasources.py` does); a column the database has and the file lacks is appended as `Name: "<type> column"  # TO BE FILLED (added by sync_schema.py)`; a column the file lists that no source has gets `# not in database (sync_schema.py)` above it and stays (`--prune` removes it, except one still named in `resolvable_columns` or `prefetchable_columns`); a recorded type that differs is corrected and reported; a table found in no source gets `# not found in any data source` (`--prune` removes it unless `relationships:` names it); a table the databases have and the file lacks is only reported, and `--add-tables PATTERN` (glob on `schema.table`, repeatable) adds the matching ones; a table with no `columns:` key is left alone. `--dry-run` prints the report; `--check` writes nothing and exits 1 when a sync would change the file (marked columns are listed in every report but do not fail it). The output is validated with `validate_schema_yaml_text` and compared with the original plus exactly the planned changes; anything else that differs is refused. Running it again on its own output changes nothing. A source whose catalogue cannot be read stops the run (exit 2).
- **`sync_schema.py` reports the prompt cost of what it adds (PR #165).** When anything would change, the report ends with a `== prompt size ==` section: per data source, the estimated static-prefix tokens of the current `schema.yaml` and of the synced text (both built by `build_static_prefix`, sized as `scripts/prompt_budget.py` sizes them), the delta, the path each takes, and how many added columns and tables still carry the `TO BE FILLED` draft description. A source the sync would move from the static prefix to retrieval (past `PROMPT_RETRIEVAL_TOKEN_BUDGET`) gets a `WARNING:` line naming it, both sizes and the budget, and pointing at `prompt_budget.py`. Advice only: exit codes and `--check` are unchanged, and without a `system_prompt.md` (or if the prefix cannot be built) the section is one line saying it was not computed. New: `prompt_engine.schema_preview.schema_text_in_effect`, `schema_data.registry.schema_in_effect` and `schema_data.sync.count_drafts`.
- **`relationships.proposed.yaml` (PR #162).** The same run proposes relationships for review, in the shape of `relationships.yaml`: every declared foreign key between two tables of `schema.yaml`, and `X_ID -> X.ID` links inferred from column names (`Order.CustomerID`, `Order.OrderDate_ID` to `Date.ID`). The source table's schema is preferred, both tables must share a data source, and a guess is dropped when the target has a composite or no key, the column types are of different kinds, or several tables match. Each entry carries `basis:` and `confidence:`. Nothing reads the file and `relationships.yaml` is never edited. `--no-relationships` skips it.
- **`column_types:`, an optional per-table map of `{column: SQL type}` (PR #162)** in `schema.yaml`, written only by the sync and validated against `columns:`. Nothing reads it at run time: the prompt and the SQL guard's allowlist are unchanged.
- **Preflight check `Schema structure matches the databases` (PR #162)** in `scripts/verify_deployment.py`: `sync_schema.py --check` as a check (FAIL with counts and the command to run; SKIP when a database cannot be read). Like `Tables are in their data source` it reads whole catalogues, so the admin panel runs it only on "deep checks". The failure text of `Tables are in their data source` now names `sync_schema.py`.
- **`database.catalogue.read_catalogue()` (PR #162)** returns tables and views with columns, data types, nullability and declared keys (three queries on SQL Server, SQLAlchemy reflection elsewhere).
- **Optional absolute accuracy floors in the evaluation release gate (PR #161).** The baseline comparison only blocks a relative drop (`EVAL_MAX_ACCURACY_DROP_PCT`), so a baseline that was already poor, or a slide in steps each under the allowed drop, never failed it. `EVAL_MIN_ACCURACY` / `--min-accuracy PCT` fails a run whose overall execution accuracy is below `PCT`; `EVAL_MIN_SOURCE_ACCURACY` / `--min-source-accuracy PCT` fails it when any data source is below `PCT`, or when the run has no per-source figures to check. Both are percentages in 0-100 (anything else is refused), are **off unless set**, need no `--baseline`, and a flag overrides the setting. A run exactly at the floor passes; a failure exits `1` and names the figure and the floor. `eval.baseline.check_absolute_floors()` evaluates one report and `ComparisonResult.floor_messages` carries the same result from `compare_to_baseline`. Documented in `docs/deployment-runbook.md` §18.2, `docs/fa/getting-started.md` §5.2 and `.env.example`. Set floors for `--live` runs only: an offline run is 100% by construction.
- **`core/redaction.py` (PR #161)**: `redact_db_url`, `url_secrets` and `scrub_secrets`, the connection-string redaction the setup wizard already had, now shared.
- **A `lint` job in CI (PR #161)** running `ruff check .` with ruff pinned to an exact version in `requirements-dev.txt` and the rule set (`E4`, `E7`, `E9`, `F`, what ruff applied implicitly) written out in `pyproject.toml`. Nothing is ignored; `tests/test_lint_config.py` fails if the pin, the rules or the job are loosened.
- **The prefix cache is warmed at start-up (`LLM_PREFIX_WARMUP_ON_STARTUP`, default `true`) (PR #163).** The static prefix per data source is byte-identical across requests so a model server with prefix caching only computes the tail, but nothing primed that cache: after a restart the first question paid the whole prefill (about a minute on a large local model). `llm/warmup.py` now sends the model one request per data source that uses the static prefix, from a daemon thread started by `lifespan`: the messages are exactly the prefix real requests start with (taken from `build_prompt_segments` itself, so it cannot drift), `max_tokens=1`, temperature 0, the same sampling fields and `LLM_EXTRA_BODY`. The server accepts requests at once, and a question that arrives mid-warm-up is served as before. The whole pass is bounded by `LLM_PREFIX_WARMUP_TIMEOUT_SECONDS` (default `180`, must be above zero), a request is never retried, and every failure is one log line holding no prompt text, response text or key. One INFO line per source gives the duration and `prompt_tokens` (and `cached_tokens` where the server reports it). It sends nothing for a source on the retrieval path, with `LLM_PROVIDER=mock`, or to an endpoint that is not trusted while `LLM_ALLOW_REMOTE` is off, which is why it can default to on. It helps only if the model server caches prefixes (`vllm serve --enable-prefix-caching`).
- **`POST /admin/llm/warmup` (`operations`) (PR #163).** The same pass on demand, whatever the start-up setting says, for after the model server restarts or a configuration change rewrites the prefix. Synchronous; one entry per source with `status`, `duration_ms` and `prompt_tokens`; `409` while another warm-up is running; recorded in the admin-action log as `llm.warmup`.
- **`LLM_STREAM_TIMINGS` (default `false`) splits the `llm` stage (PR #163).** With it on, `OpenAIBackend` sends `stream=true` with `stream_options={"include_usage": true}` and reassembles the chunks into the response the non-streaming call would have returned (content, reasoning text under whichever field the server uses, `finish_reason`, `usage`, `system_fingerprint`; proven on recorded chunk sequences and end to end against a stand-in server over both chunked and close-delimited transfer). The audit `llm` block gains `ttft_ms` (request to first generated token: the server's queue plus prefill) and `generation_ms` (first token to end), with `ttft_ms + generation_ms == total_ms`. The request timeout then also bounds the whole call. The constrained-decoding request is not streamed.
- **`reasoning_tokens` and `reasoning_tokens_estimated` in the audit `llm` block, with or without streaming (PR #163).** The server's own `usage.completion_tokens_details.reasoning_tokens` when it reports one; otherwise `completion_tokens` split by the share of characters that are reasoning, flagged as an estimate. `null` when the response shows no reasoning (not `0`). Only the count is recorded, never the reasoning text. The four new fields are in `docs/api-contract-v2.md` §6; a record written without them stays readable.
- **`scripts/analyze_audit_log.py` reports an "LLM stage split" (PR #163).** p50/p95 of `ttft_ms`, `generation_ms` and reasoning tokens, the median share of a call that was `ttft`, how many reasoning counts were estimates, and the share of completion tokens that were reasoning, each over the calls that have it. Also under `llm_latency_split` in `--json`.
- **docs: `docs/deployment-runbook.md` §20 and `docs/fa/getting-started.md` §7 (PR #163)** cover the warm-up (what it sends, how to read its log lines, vLLM `--enable-prefix-caching` and `--enable-prompt-tokens-details`, what it cannot do), the manual call, and how to read `ttft_ms`, `generation_ms` and `reasoning_tokens`. `.env.example`, the README and `technical_readme.md` list the three settings.
- **`python -m eval.cli recall --golden <file>` measures table selection offline (no LLM, no database) (PR #164).** For each golden case it resolves the tables `expected_sql` reads with the SQL guard's own resolution, runs `ContextRetriever`, and reports mean recall, the share of cases with every table retrieved, tables retrieved (mean, median, maximum), mean precision, an in-budget recall (a retrieval too large for `PROMPT_RETRIEVAL_TOKEN_BUDGET` counts as 0), per-tag figures and source-selection accuracy. Text or `--json`, deterministic; `--min-recall` makes it a gate. Runbook §18.4.
- **A synthetic retrieval benchmark: `python -m eval.benchmarks.retrieval_synth run --out DIR` (PR #164).** From a seed it generates a 400-table, two-source schema (star schemas, classifications, bridge tables, shared dimensions, same-name tables), its `project_config` and 480 English and Persian questions with reference SQL, and measures them. Generic words only. `docs/design/RETRIEVAL.md`.
- **Table retrieval recall work, measured on that benchmark (400 tables, 480 questions; recall / full-recall % / mean tables / precision / source accuracy %, main -> now, seeds 7 / 11 / 23): (PR #164)** 0.735 -> 0.966, 0.712 -> 0.951, 0.748 -> 0.971 mean recall; 42.1 -> 92.1, 37.3 -> 89.2, 41.7 -> 92.7 full recall; 9.2 -> 4.0, 8.2 -> 4.0, 11.0 -> 4.2 tables; 0.608 -> 0.678, 0.609 -> 0.680, 0.572 -> 0.656 precision; 90.8 -> 95.2, 90.0 -> 95.6, 86.0 -> 94.8 source accuracy. Bridge-table questions 0.558 -> 0.967 (English, seed 7). The parts:
  - **Column-name evidence.** A question word that is a word of a table's column name (`net weight` for `NetWeight`) now counts towards that table, weighted by rarity; key columns are skipped. Measure-only questions (fact table not named) 0.525 -> 0.925 (English, seed 7).
  - **Join-path expansion** (`retrieval/join_paths.py`). The tables that join the retrieved ones but were not named (a bridge table, the parent of a classification) are added from the foreign-key graph: `schema.yaml` relationships, `relationships.yaml`, and columns named `<Table>_ID` when neither declares the key (never written to configuration, no new join hint in the prompt). Shortest path of at most 2 keys, never through a hub, never across data sources, within the token budget. They are `RetrievalContext.join_tables`, part of `selected_tables`.
  - **Pruning** (`retrieval/pruning.py`). After the recall-favouring candidate generation, a table only its description matched, or a column match far below the best, is dropped unless it is joined to a better-evidenced table.
  - New settings: `RETRIEVAL_EXTRA_TABLES`, `RETRIEVAL_EXTRA_SCORE_RATIO`, `RETRIEVAL_JOIN_EXPANSION`, `RETRIEVAL_JOIN_MAX_HOPS`, `RETRIEVAL_JOIN_MAX_ADDED_TABLES`, `RETRIEVAL_JOIN_MAX_HUB_DEGREE`, `RETRIEVAL_INFER_RELATIONSHIPS`, `RETRIEVAL_PRUNE`, `RETRIEVAL_PRUNE_SCORE_RATIO`, `RETRIEVAL_PRUNE_CONNECT_HOPS`, `RETRIEVAL_PRUNE_CORROBORATE` (defaults in `docs/design/RETRIEVAL.md` §6).

### Changed

- **docs: `sync_schema.py` is the recommended way to produce `schema.yaml`'s structure (PR #162)** in the first-install checklist (steps 6 to 8), runbook §16.3 and §3.1, the Persian guide, `project_config.example/README.md` and the READMEs. `scripts/assign_datasources.py` stays, documented as the narrower tool (it writes only the `datasource:` lines and never adds, types or marks a column), because pipelines already call it; it now shares its matching and `datasource:` editing with the new script (`schema_data/placement.py`, `schema_data/yaml_text.py`), with unchanged behaviour. README: the preflight has 14 checks.
- **`import knowledge` no longer reads `project_config/` (PR #161).** `knowledge/__init__.py` imported its five names at the top, which read five YAML files the moment anything under `knowledge` was imported (`knowledge.config_loader` included), against every submodule's "import never fails" docstring. The names are now resolved on first access (PEP 562); the public API is unchanged (`__all__`, and `knowledge.X is knowledge.<module>.X`). Server start-up still stops on a missing or invalid file before the first request, because the modules `api.server` imports bind those names at import time; a test pins that. The setup wizard no longer needs to point the loaders at `project_config.example/` before importing them.
- **`build_prompt_segments` builds the static prefix and the per-request tail separately (PR #163)** (`PromptBuilder.build_static_suffix`) instead of concatenating them and cutting the result apart again at the prefix's length. Byte-identical output (`tests/test_prompt_segments.py`); the call now costs about 7 µs whatever the prefix size, where it cost 8.6 µs at a 5 KB prefix, 12.3 µs at 44 KB and 23.5 µs at 166 KB. The prefix itself was already built once per source and kept, and is invalidated when an applied configuration changes it; that is unchanged.
- **An alias or fact-pattern hit no longer ends the search for tables (PR #164).** `EntityRetriever` and `FactRetriever` returned only the aliased tables when any alias matched, so a question naming one aliased and one unaliased dimension lost the second, and an `always_include` table (the date dimension) vanished whenever another dimension matched. Up to `RETRIEVAL_EXTRA_TABLES` ranked tables are now added. Alias matching folds Persian/Arabic spellings and ZWNJ, and results keep configuration order instead of set order (they varied between runs).
- **The TF-IDF retriever splits on word characters (PR #164).** `customers?`, `سال؟` and `sales.Order` were single tokens and never matched a description word. `schema_data.retriever` gains `rank_tables`, `forced_tables` and `column_evidence`; `retrieve_tables` keeps its meaning and breaks score ties by table name.
- **Static-prefix prompts are unchanged (PR #164)**; only the tables the retrieval path lists differ. `RETRIEVAL_PRUNE=false`, `RETRIEVAL_JOIN_EXPANSION=false` and `RETRIEVAL_EXTRA_TABLES=0` restore the previous selection.

### Fixed

- **`python -m eval.cli` no longer crashes on a Windows console (PR #164).** Reports carry Persian questions and table names, and a console on a code page such as cp1252 cannot encode them, so `eval.cli recall` (and any subcommand printing Persian) failed with `UnicodeEncodeError` part-way through its output. The CLI now writes UTF-8 to stdout and stderr when run as a program, and the benchmark reads its child process's output as UTF-8.
- **Ruff is clean (89 findings fixed, none ignored) (PR #161).** Unused imports and variables removed, `l` renamed, two one-line loops split. Four `F821` undefined names were all one: `"PromptSegments"` in the annotations of `LLMBackend.generate_with_meta_segments` / `generate_structured` and the two provider overrides. Not a runtime bug (the annotations are strings under `from __future__ import annotations` and nothing evaluated them), but `typing.get_type_hints`, Sphinx or pydantic would have raised `NameError`; the name is now imported under `TYPE_CHECKING` (a runtime import would be circular), and a test resolves the hints.
- **A response with `content: null` is an empty answer, not the text `None` (PR #163).** A reasoning model cut off while still thinking returns `finish_reason: "length"` and `"content": null` (its thinking is in a separate field), and `OpenAIBackend` rendered that with `str(None)`. The four characters looked like an answer, so `is_truncated_empty_completion` never fired and the `LLM_OUTPUT_TRUNCATED` message, which names `LLM_NUM_PREDICT` and `LLM_EXTRA_BODY`, was replaced by a parse failure about SQL that was never written. A `null` or missing `content` is now `""` on the non-streaming and the streaming path alike, in the free-text and the constrained-decoding request (which used to raise a `TypeError` on `null`), and the reasoning-token estimate no longer works around the string. `mode="sql"` and the v2 session engine now report `LLM_OUTPUT_TRUNCATED` for such a response, as they always did for an empty string. `mode="full"` on `/query` goes through the correction loop, which has no truncation shortcut: a cut-off response there is still retried and reported as `INVALID_SQL_RESPONSE`, for `""` and `null` alike.

### Security

- **One robust connection-string redactor (PR #161).** `database/schema_inspector.py` masked passwords with a regular expression that left part of a password containing `@` or `:` visible and never touched `PWD=` inside an `odbc_connect` value, so the redacted URL in a schema snapshot could still carry a credential. `appdb/engine.py`'s `redact_url` and `DataSource.redacted_url` (used in refusal messages and `scripts/verify_deployment.py` output) had the same `odbc_connect` gap. All now use `core.redaction.redact_db_url`; the wizard keeps its `_redact_db_url` / `_scrub_secrets` names.

### Upgrading

- **Table retrieval selects differently by default (PR #164).** Eleven new `RETRIEVAL_*` settings control it (defaults in `docs/design/RETRIEVAL.md` §6); among them `RETRIEVAL_EXTRA_TABLES` defaults to `3`, and `RETRIEVAL_JOIN_EXPANSION` and `RETRIEVAL_PRUNE` default to `true`. Static-prefix prompts are unchanged; only the tables the retrieval path lists differ. `RETRIEVAL_PRUNE=false`, `RETRIEVAL_JOIN_EXPANSION=false` and `RETRIEVAL_EXTRA_TABLES=0` restore the previous selection.
- **Start-up now sends one small request per data source to the model server (PR #163).** `LLM_PREFIX_WARMUP_ON_STARTUP` defaults to `true` (the warm-up above). If the model server is shared and metered per token, or start-up must send it nothing, set it to `false`. Nothing else needs doing; `LLM_STREAM_TIMINGS` stays off until you turn it on.
- **Regenerate any schema snapshot written by `database/schema_inspector.py` (PR #161)** if its `source_url` may have been shared and quoted a password: earlier versions could leave part of the password in it. Rotate that password if it was.
- **`scripts/sync_schema.py` is now the recommended way to maintain `schema.yaml`'s structure (PR #162).** It writes `schema.synced.yaml` for review and never overwrites `schema.yaml`; `scripts/assign_datasources.py` stays for pipelines that already call it.
- **Contributors: install `requirements-dev.txt` (PR #161).** It pins `ruff` to an exact version, and CI now fails on `ruff check .`.

## [6.8.0] — 2026-10-09

A column listed in `denied_columns` can now be restricted to one table or one data source and used only as a join key; the setup documentation is rewritten around one ordered first install; the setup wizard no longer stores the database password in `.setup_log.json` and now obeys `LLM_ALLOW_REMOTE`, with its resume, regenerate and validation steps fixed.

### Added

- **Scoped, join-only column restrictions in `denied_columns` (PR #158).** Alongside the plain name (`NationalID`, unchanged: denied everywhere, every reference), an entry may be `schema.Table.Col` (join-only on that table, in any source that holds it), `Source:schema.Table.Col` (only when the query executes on `Source`) or `Source:Col` (every table of `Source` that has the column). A join-only column may appear only as one side of `a.col = b.col` inside a `JOIN ... ON`; the select list, `WHERE`, `GROUP BY`, `ORDER BY`, `HAVING`, a function argument, `ON` against a literal, `USING (...)`, a comma join's `WHERE` predicate and a `*` that expands to it are refused with the new, correctable guard reason `join_only_column` (its `subject` is the whole entry, so "Request access" removes exactly that entry). The data source is the one `database.routing.choose_datasource` picks, the same call the executor makes. Entries are parsed by the new `security/column_policy.py` and checked against `schema.yaml` and `datasources.yaml` when keys are loaded and when the admin panel saves an ACL, so a typo stops start-up (or answers 422) naming the entry. The model is told which columns are join-only by one line in the per-question part of the prompt; the cached static prefix is unchanged. The admin key editor shows and accepts the new forms.

### Changed

- **`scope_key` hashes the sorted `denied_columns` as a JSON array instead of joining them on `":"` (PR #158).** Scoped entries contain `:`, so the old join could give `("a:b",)` and `("a", "b")` the same key. Cached query results are keyed afresh once after the upgrade.
- **The value resolver and the dimension vocabulary skip join-only columns (PR #158)**, so their distinct values cannot reach the prompt.
- **docs: the setup documentation is rewritten around one ordered first install, and brought up to 6.7.0 (PR #157).** `docs/deployment-runbook.md` and `docs/fa/getting-started.md` open with "First install, in order", a 17-step list from an empty machine to a verified, running server, each step with its exact command and how to tell it worked (install, `.env`, `project_config/`, `datasources.yaml`, `scripts/assign_datasources.py` with a backup and `--check`, the columns it reports missing, per-source `nolock:`/`keywords:`/`description:`, the first API keys, `scripts/verify_deployment.py`, `scripts/prompt_budget.py`, starting the server, the golden set); the first admin key comes before the preflight because its `API key authentication` check fails while no key exists. New runbook §2.1 to §2.3 (and Persian ۰.۳.۱, ۰.۳.۲) document creating `project_config/`, the setup wizard `setup_project.py` (its options, its seven steps and the `.setup_log.json` keys, what it writes and what it does not: no `schema.yaml`, no `datasource:` per table; that it reads its own `WIZARD_*` settings and not `OPENAI_BASE_URL`, `DB_PASSWORD` or `datasources.yaml`) and `python -m database.schema_inspector_cli`, the tool that drafts `schema.yaml`, which no document mentioned. New runbook §3.1 (Persian ۰.۴) is a reference table of all 13 `verify_deployment.py` checks with what each does, what `[FAIL]` and `[SKIP]` mean (there is no warning status) and the fix, including `Tables are in their data source`. `nolock: true` is stated to be per data source, with a two-source example. `DB_APPLICATION_NAME` and a DMV query for the application's sessions and open transactions are in the Persian guide as well as the runbook. "Upgrading from 6.0 to 6.6" (§17, Persian بخش ۴) is now "6.0 to 6.7" and gains the 6.7.0 golden-set and release-gate step. New runbook §19 and Persian بخش ۶ say which files never to send when asking for help (`.env`, `api_keys.json`, raw keys, `.setup_log.json`, logs, `eval_data/`) and which are safe. README: quick start reordered so the preflight runs before the server starts, the wizard and the inspector, `harvest_golden.py`, `golden_sheet.py`, `create_db.py` and the `eval/` modules added to the project structure, the evaluation feature row updated, and the test count and coverage re-measured at 6.7.0 (5,470 tests, 94%). The tutorials and `project_config.example/README.md` point to the new sections. No code changed.

### Fixed

- **`--resume` continues from the first step `.setup_log.json` does not record as completed (PR #159).** It used to skip only files that already existed and ran every step again, the model calls included. The log now keeps the aliases, rules and examples each LLM step produced, with the language, the provider and a fingerprint of the schema. A resumed run reconnects (the connection string is never stored) and re-reads the schema, reuses the stored results of completed LLM steps while the schema and language are unchanged (never ones made by the mock LLM), and writes only the files that do not exist; when steps 1 to 6 are all recorded and every file exists it runs only the validation, without the database or the model. A run without `--resume` starts a new log, and `--dry-run` no longer writes one (it also no longer marks step 6 done with nothing written).
- **The wizard's "Regenerate" choice regenerates (PR #159).** No caller passed a regeneration function, so it showed the same text again. Step 6 now re-runs step 4 for `business_rules.yaml` and step 5 for `examples.yaml`; for `entities.yaml` and `aliases.yaml`, which are both built from the step 3 aliases, it re-runs step 3 for every table without the per-table questions. A failure is reported and the current version kept. The choice is not offered for `relationships.yaml` (built from the schema) or when there is no LLM.
- **Step 7 of the setup wizard reports missing `project_config/` files instead of crashing (PR #159).** Importing anything under `knowledge/` reads five files of the default `project_config/` on the spot, so on a fresh checkout step 7 (and the interactive validation) died with a `ConfigNotFoundError` traceback. The wizard now validates the files it wrote and lists the required files still missing, with the command that copies each template from `project_config.example/`; it does not copy them (the application has no fall-back to the sample configuration either) and does not say "Setup complete" while files are missing. The ten required names are one list, `core/project_config_files.py`, pinned by a test to the loaders.
- **The setup wizard's LLM follows the application's settings (PR #159).** An empty `WIZARD_LLM_BASE_URL` (as in `.env.example`) silently meant `https://api.openai.com/v1`, and the shipped `WIZARD_LLM_MODEL=gpt-oss-20b` shadowed `OPENAI_MODEL`. Endpoint, model and key now come from the flag, then `WIZARD_LLM_*`, then `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY`; the wizard prints one line naming the provider, model, endpoint and where each came from (never the key), a local endpoint no longer needs a key, and a fall-back to the mock LLM because the endpoint did not answer is announced loudly. `.env.example` leaves `WIZARD_LLM_MODEL` and `WIZARD_LLM_BASE_URL` empty and explains this.
- **`scripts/verify_deployment.py` checks all ten required `project_config/` files (PR #159).** "project_config/ loads" loaded six, so a missing or invalid `retrieval_hints.yaml`, `session_policy.yaml`, `memory_policy.yaml` or `system_prompt.md` passed. It now loads all ten through the application's own loaders, names every missing file at once, resolves a relative `PROJECT_CONFIG_DIR` against the repository root as the application does, and fails on an empty system prompt.

### Security

- Join-only hides a column's value from the output, not its existence: a join on a filtered foreign key can still probe it, and hiding a primary key whose foreign key is visible elsewhere is cosmetic. Restrict the foreign keys as well. An unqualified reference counts as restricted when any table in its `SELECT` is restricted for that name. An entry name cannot contain `.` or `:`; a legacy entry that did is now read as a scoped one and fails start-up if it names nothing real. See `docs/deployment-runbook.md` and `docs/design/DESIGN.md` D14 (PR #158).
- **The setup wizard no longer stores the database password (PR #159).** `.setup_log.json` recorded the connection string under `db_url_redacted` through a `'re' in dir()` test that is always false inside a function, so it held the URL exactly as typed. It is now redacted through SQLAlchemy's URL parser, which also masks a password in the query string or inside `odbc_connect` and a password containing `@`, `:` or `/`, with a masked-by-pattern fallback for a string the parser rejects. The same helper masks the `Source:` line in `entities.yaml`, and driver errors shown on a failed connection or schema inspection are scrubbed of the URL and the password. A `.setup_log.json` written by an earlier version may hold the password: delete it, and rotate the password if the file was shared or committed.
- **The setup wizard obeys `LLM_ALLOW_REMOTE` (PR #159).** It sends table and column names and sample column values to the model, and never consulted the gate the application applies. It now uses the application's definition of a remote endpoint (`LLM_TRUSTED`, else loopback, private-network and `*.local` hosts are local) and refuses a remote one, before it touches the database or the endpoint, unless `LLM_ALLOW_REMOTE=true`.

### Upgrading

- **Delete any `.setup_log.json` written by an earlier version, and rotate the database password if that file was shared or committed (PR #159).** Earlier versions stored the connection string in it exactly as typed, password included. The wizard now writes a redacted URL.
- **The query cache is invalidated once (PR #158).** `scope_key` now hashes `denied_columns` as a JSON array, so every cached result is keyed afresh after the upgrade; nothing needs to be cleared by hand.
- **Scoped `denied_columns` entries are validated at startup (PR #158).** A mistyped `schema.Table.Col`, `Source:schema.Table.Col` or `Source:Col` entry now fails startup loudly instead of silently restricting nothing; fix the entry it names. Plain names are unchanged.
- **`WIZARD_LLM_MODEL` and `WIZARD_LLM_BASE_URL` in `.env.example` now default to empty and fall back to `OPENAI_MODEL` and `OPENAI_BASE_URL` (PR #159).** If you copied the old `.env.example` values (`gpt-oss-20b`, and an empty base URL meaning `https://api.openai.com/v1`) into your `.env`, empty them to follow the application's own settings; the wizard prints one line naming the endpoint and model it uses.

## [6.7.0] — 2026-10-05

A real evaluation set can now be built from real usage and used to gate an upgrade; CI tests the pinned dependency set as well as the newest releases; the documentation is brought up to date.

### Added

- **A real evaluation set can be built from real usage and gated on before an upgrade (PR #155).** `python -m eval.cli run --live --reference live` executes each case's `expected_sql` in the same run and compares the two results as execution accuracy (`eval/compare.py`: rows as a multiset, column names ignored and column order as selected, row order only for a top-level `ORDER BY` with `TOP`/`OFFSET`, numbers within `--float-tolerance` (default `1e-6`) but integers exactly, `NULL` equals `NULL`), so a warehouse that changes daily no longer turns a stale recorded fingerprint into a false failure; the default `--reference stored` keeps the old fingerprint comparison, `--reference live` requires `--live`, and a baseline can only be compared with a run that used the same reference. `scripts/harvest_golden.py` writes a seeded, stratified sample (data source, outcome, language; default 150) of de-duplicated candidates from the audit log to `eval_data/candidates.jsonl` as `pending_review` cases, printing counts only unless `--include-examples`; `scripts/golden_sheet.py export`/`import` round-trips them through a UTF-8-with-BOM CSV for Excel review (`eval_data.example/golden_review_template.csv`), validating every SQL with the guard and reporting problems by row; `python -m eval.cli verify [--accept]` runs each reviewed case's SQL read-only through the application's executor, records `expected_rows`/`expected_fingerprint`, and only with `--accept` makes it `active` (atomic write, `.bak` kept). `GoldenCase` gains an optional `datasource` and the statuses `pending_review` and `reviewed` (the runner skips every status but `active`); the report adds per-source execution accuracy and source-selection accuracy, and baselines written before these fields still load. See `docs/deployment-runbook.md` §18, `eval_data.example/README.md` and `docs/fa/getting-started.md` §۵.

### Changed

- **CI now tests the pinned dependency set that deployments install, as well as the newest releases (PR #152).** The `test` job in `.github/workflows/ci.yml` gained a `deps` matrix dimension: the existing legs still install `requirements.txt` (floors only, so the newest releases, which warns early when an upstream release breaks us), and a second set of legs, named `<os> / Python <version> / locked deps`, install `requirements.lock` across the same three operating systems and Python 3.11, 3.12 and 3.13 (every pin has a wheel on all nine combinations). They run the same suite, doctests and offline evaluation run; `webapp/requirements.txt` and pytest are installed under the lock as a constraint so nothing can move a pin. Until now CI never ran the tree a deployment installs (sqlglot, whose AST `security/sql_guard.py` decides from, is pinned at 30.18.0 while CI ran 30.21.0). The existing check names are unchanged; the pip-audit, coverage and upload steps still run once, on the Linux 3.12 floors leg.
- **The documentation is brought up to 6.6 and reorganised around one place per topic (PR #153).** `docs/deployment-runbook.md` gains an install step (`pip install -r requirements.lock`, with the reason, and what CI tests: the pinned lock and the newest floors), §16 rewritten as the operator's ordered procedure for several data sources (describing the sources, `scripts/assign_datasources.py`, `keywords:`, preflight, `scripts/prompt_budget.py`, `nolock`, reading the schema-drift and vocabulary cards, reading `datasource_selection`) and a new §17, "Upgrading from 6.0 to 6.6", which sequences the **Upgrading** notes of 6.0.1 to 6.6.1 into one checklist; `docs/design/DATASOURCES.md` stays the one full explanation and gets four decision records (DS1 to DS4), and `docs/design/DESIGN.md` gets D13 for the display-only SQL layout. The Persian guide and tutorial cover the same operator path, with the duplicated long explanations in the README, the tutorials and the guides replaced by short pointers. Corrected against the code: the prompt has a static path (whole schema, the default) and a retrieval path (over `PROMPT_RETRIEVAL_TOKEN_BUDGET`) rather than always being scoped; the guard is parser-based, not a keyword list; the tutorials' domain-authoring steps are the YAML files in `project_config/`, not `knowledge/*.py`; `/query` responses, `/health`, rate-limit and cache descriptions, the admin panel's twelve sections and their capabilities, the `DIMENSION_VOCABULARY_WARM_ON_STARTUP` default (`true`), `DEFAULT_TOP_N`, the CLI banner and log file names, and the test count. `PROMPT_RETRIEVAL_TOKEN_BUDGET` is now in `.env.example` and the README's configuration table; `SCHEMA_CACHE_TTL`, which nothing reads, is removed from `.env.example`; and `AUTO_DISCOVER_SCHEMA` is documented as read only by `database/relationship_map.py`, which nothing in the server imports. No code changed.

## [6.6.1] — 2026-10-04

A template for the `API_KEYS_FILE` key array.

### Added

- **`project_config.example/api_keys.example.json` is a template for the `API_KEYS_FILE` file (PR #150).** Copy it to `project_config/api_keys.json` (git-ignored) and replace each `key_sha256` with the digest `scripts/issue_api_key.py` prints. It holds an analyst entry with a `denied_columns` example and an admin entry with `admin`, `operations` and `security`, which together show every field the parser reads. Both digests are placeholders, so a copy that was not edited is refused at start-up (`key_sha256 must be a 64-character SHA-256 hex digest`) instead of loading keys nobody holds. `.env.example`, the deployment runbook, `project_config.example/README.md` and the Persian getting-started guide point to it; `tests/test_api_keys_file.py` pins its fields and the refusal.

## [6.6.0] — 2026-10-04

`scripts/prompt_budget.py` measures what each data source's prompt really costs and prints the `PROMPT_RETRIEVAL_TOKEN_BUDGET` to set.

### Added

- **`scripts/prompt_budget.py` shows what each data source's prompt really costs and which `PROMPT_RETRIEVAL_TOKEN_BUDGET` to set (PR #148).** Run it from the repository root: it builds each source's static prefix exactly as the server does (the system prompt from `PROJECT_CONFIG_DIR`, `build_static_prefix(system_prompt, source)`; the one implicit source when there is no `datasources.yaml`) and reports its characters, table count, the heuristic estimate, and the path it takes under the current budget. Unless `--no-model` is given it also sends each prefix to the endpoint the router uses for SQL generation as one chat completion with `max_tokens=1` and reads `usage.prompt_tokens`, using the application's own request body, headers and `LLM_EXTRA_BODY` and its data-governance gate (an untrusted endpoint is sent nothing unless `LLM_ALLOW_REMOTE` is true); this warms the model server's prefix cache for each source. The context length comes from `--context-length N` or `GET {base}/models` (`max_model_len`, `context_length` or `context_window`), else it is unknown. With it the script checks per source that real tokens (or the estimate times the largest ratio measured elsewhere, or times `--assumed-ratio`, default 1.15, labelled as such) + `--question-room` (default 2000) + `LLM_NUM_PREDICT` fit the window, leaves a source that does not fit out of the recommendation with a warning (it should stay on the retrieval path) and exits 1, and recommends the budget as the largest estimate among the sources that fit, plus `--headroom` percent (default 10), rounded up to a multiple of `--round` (default 500), with the exact `.env` line and each source's path now and after. If the endpoint is unreachable or returns no usage a source's count is shown as `unavailable` and the run continues. `--json` prints one JSON document; no API key or URL credential is printed; exit 2 means the configuration could not be loaded or an option is invalid.

### Upgrading

- **With several data sources, run `python scripts/prompt_budget.py` once after upgrading (PR #148)** and set the `PROMPT_RETRIEVAL_TOKEN_BUDGET` it prints in `.env`; it needs the model endpoint reachable for measured numbers (`--no-model` works offline from the estimate).

## [6.5.0] — 2026-10-04

A data source can read its tables `WITH (NOLOCK)`, a table in `schema.yaml` may now live in several data sources, `scripts/assign_datasources.py` writes the `datasource:` lines for you, schema drift says where a table really is, and with several sources each question is routed to one of them so the model sees that source's whole schema; the SQL shown in the UI now has a fixed, aligned layout.

### Added

- **`datasource:` accepts a list, for a table that exists in several data sources (PR #146).** `datasource: [sales, inventory]` says the same table, with the same shape, is in each (a date dimension replicated into both databases is the usual case); a plain name works as before. The list must be non-empty and its names distinct, and every name is checked against `datasources.yaml` wherever names are checked: at start-up, in the admin panel's `schema.yaml` draft validation and by `verify_deployment.py`. A statement now runs on a source that has **every** table it reads: the default source if it is one of those, otherwise the first in `datasources.yaml` order. A statement that reads only a shared table runs on the default source, and so do the value resolver and the dimension-vocabulary prefetch for it (one copy is read, not each). The SQL guard applies the same rule as the executor (`database.routing.choose_datasource`), so they cannot disagree. A statement is refused as `cross_datasource` only when no source has all its tables, and the message still names every source with its tables, now also listing which tables are available in several. The prompt's schema block prints `Data source: sales, inventory` for a shared table.
- **`scripts/assign_datasources.py` works out each table's `datasource:` from the databases (PR #146).** Run it from the repository root: it reads the tables, views and columns of every data source through the application's own read-only engines (two `INFORMATION_SCHEMA` queries per source, names only), matches every `schema.yaml` table by schema and name, and writes `schema.with_datasources.yaml` next to `schema.yaml` (`--output PATH` to choose another file; `schema.yaml` itself is never overwritten) with every original line and comment kept and one `datasource:` line under each table key: a name, `[A, B]` for a table found in both, and `# not found in any data source` under a table found in neither. An existing `datasource:` entry is replaced, not duplicated. The output is validated with the project's own `validate_schema_yaml_text`, and the script refuses to write a file that fails it or differs from `schema.yaml` in anything but `datasource`. The report lists, per source, the tables found only there, the shared tables, the tables found nowhere, the tables whose current `datasource:` disagrees with what was found, and the columns `schema.yaml` lists that the database does not have. `--check` writes nothing and exits 1 if any `datasource:` disagrees; exit 2 means a catalogue could not be read or the file could not be produced.
- **Schema drift and `verify_deployment.py` say where a misplaced table is (PR #146).** When every column of a table is missing from the source `schema.yaml` assigns it to and another source has the table, `GET /admin/schema-drift` returns it in a new `misplaced_tables` list with a hint such as `stock_dim.Broker: not in sales, found in inventory — set datasource: inventory`, and the admin panel's schema-drift card lists it with the value to write. It costs one `INFORMATION_SCHEMA.TABLES` query per other source, only when such a table exists, and is cached with the rest of the drift result. `verify_deployment.py` has a new check, `Tables are in their data source`, that fails with the same hints (skipped with one data source); the admin panel runs it only under "deep checks", because it reflects every catalogue.
- **A data source can read its tables `WITH (NOLOCK)` (`nolock: true` in `datasources.yaml`) (PR #146).** For warehouses where the DBA requires the hint on every read. It is off by default and set per source, in the structured and the `url_env` form; anything but a YAML `true` or `false` is refused naming the source, and so is `nolock: true` when `SQL_DIALECT` is not `tsql` (table hints are T-SQL). Just before a statement is sent, `database.executor` passes it through the new `database.table_hints.add_nolock_hints` when its routed source has the flag, which inserts ` WITH (NOLOCK)` after each physical table reference, after the alias when there is one, in `FROM`, every `JOIN`, subqueries, CTE bodies and each branch of `UNION`/`EXCEPT`/`INTERSECT`. Nothing else in the text changes: sqlglot only locates the references and the hint is inserted at that offset, so literals, comments, `?` placeholders, `TOP`, `OFFSET`/`FETCH` and the SQL as the model wrote it stay as they were. CTE names, derived tables, table-valued functions, `#temp` tables, `@table` variables, `INFORMATION_SCHEMA`/`sys` objects and a table that already has a `WITH (...)` hint are left alone. The rewritten text is parsed again and checked; if that fails, or the statement cannot be parsed, it runs unchanged and one warning per reason is logged. The audit trail's `generated_sql` stays the validated, unhinted SQL; only the text sent to the server differs. `NOLOCK` allows dirty reads (uncommitted rows, and occasionally a row twice or missing), so turn it on only where it is required. `project_config.example/datasources.example.yaml` documents `nolock` and the `APP` ODBC option (the `program_name` a DBA sees in `sys.dm_exec_sessions`), and `DialectProfile` gains `supports_table_hints`.
- **With several data sources, each question is routed to one source and the model is shown that source's schema (PR #146).** A statement can only run on one source, so the prompt no longer describes every table of every source. The source is chosen before the prompt is built, with no model call (`retrieval/source_selector.py`): first by `keywords:` in `datasources.yaml`, then, for a follow-up turn of a conversation, by the previous turn's source, then by the tables retrieval selected and the tables whose warehouse values matched (a table in several sources counts for each, with a lower weight), then by the default source. The prompt for the chosen source holds only the tables that live in it (shared tables included), only the relationships whose two ends are both among them, and only the few-shot examples whose SQL reads only such tables (an example whose tables cannot be determined is kept); business rules and metrics stay whole, and `Data source: <name> — <description>` is printed once above the schema. `build_static_prefix`, `static_prefix_token_estimate` and `should_use_static_prefix` take the source, so each source has its own cached, byte-identical prefix and its own budget decision: a source whose prefix fits `PROMPT_RETRIEVAL_TOKEN_BUDGET` uses it, a larger one uses the retrieval path restricted to its tables, and when retrieval finds no table that path now shows every table of the chosen source, not of the deployment. Both engines build the prompt through `llm.router.build_prompt_segments(..., source=)`. A deployment with one data source is unchanged: prompts, prefixes, cache keys and the estimate are byte for byte what they were (pinned by `tests/test_source_prompts.py`).
- **`keywords:` in `datasources.yaml` (PR #146).** An optional list of words or phrases, Persian or English, that mark a question as being about a source (`description:` already existed and is now also printed above the source's tables in its prompt). A phrase matches as whole words after the folding the retrieval layer uses everywhere (Arabic `ي`/`ك` to `ی`/`ک`, Persian and Arabic digits to `0-9`, ZWNJ removed, case ignored), so `stock` does not match `stockholder`. A value that is not a list of non-empty strings, or lists a phrase twice (two spellings that fold to the same text count as twice), is refused at start-up naming the source. `database.datasources.datasource_keywords()` and `datasource_descriptions()` read them without touching credentials.
- **One retry when the model answers `OUT_OF_SCOPE` for the chosen source (PR #146).** If another source exists, the request is retried once with the next candidate (ordered by strength of evidence): one extra model call at most, never more, outside the correction budget, with the correction history dropped because it describes the other source's tables. A second `OUT_OF_SCOPE` is returned as before, and a deployment with one source never retries. `llm.source_routing.generate_with_source_fallback` is the one helper `SQLAgent` (`/query`, result, full and SQL-only modes) and `TurnEngine` (`/v2` sessions) both call.
- **The audit record gains `datasource_selection` (PR #146).** `{"chosen", "reason", "candidates", "fallback_from"}`: the source whose tables the model last saw, the signal that ordered the candidates (`keyword`, `session`, `retrieval` or `default`), every source in fallback order, and the source that declined when the request retried. `None` with one source and for a result served from the cache; additive, like `datasource`, which still records where the generated SQL routes (the guard and router stay authoritative, and no new refusal is added when the two differ). `prefix_cache_hit` in the audit `llm` block is now measured against the chosen source's own prefix estimate. With several sources one INFO line per source at start-up (or first use) says its table count, its static-prefix estimate, the budget and the path its prompts take.
- **A conversation keeps its source between turns (PR #146).** `session.store.TurnMemory.datasource` records the source a turn was answered from and is persisted with the turn (only when set, so a single-source deployment stores exactly what it did). A follow-up turn starts from it unless the question's keywords point elsewhere; a refinement composed over the previous turn's SQL stays on that source; a question the session classes as new is judged on its own.

### Changed

- **Schema drift checks a shared table in each of its sources (PR #146).** A column missing from one copy is reported as `Table.Column [source]`, so the copies are no longer merged; tables in one source keep their plain `Table.Column`. A copy of a table that `schema.yaml` describes only for another source is no longer merged into that table's columns.
- **`TableDefinition.datasource` is a tuple of names, and `get_table_datasource_names()` returns tuples (PR #146)** (`()` for a table with no `datasource:` key, where both used to be a string). `database.datasources.table_datasources()` still returns `{table: one source}`; `table_datasource_sets()` is new and returns every source of every table, and `database.routing.group_tables_by_datasource()` lists a shared table under each of its sources.
- **`PROMPT_RETRIEVAL_TOKEN_BUDGET` applies per data source when several are configured (PR #146).** The default (6000) is unchanged. It is compared with each source's own prefix estimate, not the whole schema's. The estimator is still `len(text) // 4`, which undercounts Persian text by roughly 15%, so leave that margin when sizing it; `docs/design/DATASOURCES.md` ("Choosing a source per question") and `docs/deployment-runbook.md` §16 say how.
- **`build_static_prefix(system_prompt, source=None)`, `static_prefix_token_estimate(system_prompt, source=None)`, `should_use_static_prefix(system_prompt, source=None)`, `PromptBuilder.build(..., source=None)`, `SchemaRegistry.build_schema_context(..., source=None)` and `llm.router.build_prompt_segments(..., source=None)` take an optional data source (PR #146).** `None` is what every call site did before, and with one source configured a name is ignored. `SchemaRegistry.tables_for_source()` is new. `SQLGenerationResult` gains an optional `datasource_selection`.
- **`Turn.sql_display` is laid out in the house style instead of sqlglot's default (PR #146).** The new `security/sql_format.format_sql` writes `SELECT` alone on its line (`SELECT TOP (100)`, `SELECT DISTINCT`), one item per line with a leading comma (the first indented five spaces, the rest four and a comma), the `AS` of every alias in one column two spaces after the longest aliased expression (a `CASE`, or an expression longer than 60 characters, does not stretch it), a blank line before `FROM`, and one line per join (`INNER JOIN`, `LEFT JOIN`, `RIGHT JOIN`, `FULL JOIN`, `CROSS JOIN`, `CROSS APPLY`, `OUTER APPLY`, always in full and in upper case) with the `AS`, `ON` and `=` columns aligned. `WHERE`, `HAVING` and multi-condition `ON` continue with right-aligned `AND` / `OR`, and derived tables, `IN` / `EXISTS` / scalar subqueries, CTE bodies and the branches of `UNION` / `EXCEPT` / `INTERSECT` are laid out by the same rules one level in. Expression text still comes from sqlglot's T-SQL generator (with `IS NOT NULL` and `NOT IN` written as a person writes them), and identifier quoting, `N'...'` literals, numbers and `?` placeholders are shown as parsed. The result is parsed again and compared with the parse of the input; if the trees differ, if the statement has a comment or a construct the layout does not handle (`MERGE`, `PIVOT`, comma joins, `OPTION`, `INTO`, `ROLLUP`, ...), or if anything raises, `pretty_sql` gives sqlglot's pretty output as before and, failing that, the input. Several statements in one string, which `pretty_sql` used to cut down to the first, now come back unchanged. `Turn.sql`, the text that is validated, executed and audited, is never touched, and the web UI needs no change: it shows a multi-line `sql_display` as it is, in a `white-space: pre` block.
- **Displayed SQL is never a rewrite of what ran, a refused statement is laid out too, and the SQL shown while a turn streams is the final one (PR #146).** `pretty_sql` (every dialect) now uses sqlglot's pretty printer only when its output parses back to the same tree as the input and keeps the case of every function name sqlglot does not know; otherwise the input comes back unchanged, so `DATEDIFF(day, a, b)` is no longer shown as `DATEDIFF(DAY, CAST(a AS DATETIME2), CAST(b AS DATETIME2))` nor `FROM dbo.fn(1)` as `FROM dbo.FN(1)`. `GuardVerdict.rejected_sql_display` is `pretty_sql` over `rejected_sql` (`null` when `rejected_sql` is, equal to it when nothing could be laid out) and is serialised with it, in the turn and the `sql` and `done` events and the stored session; the «دیدن SQL» reveal shows it when present and falls back to `rejected_sql` through the client formatter, which is never altered. The `sql` stream event gains `sql_display`, so the first paint is the text `done` brings instead of the client formatter's layout; a client that ignores the field is unaffected. The generative path now sets `Turn.sql_display` (from the executed SQL, as the refinement path already did), where it was `null` and the client formatted `sql` itself, so the copy button on such a turn copies the laid-out statement.

### Upgrading

- **Nothing changes for a deployment with one data source, or whose tables each name one source or none (PR #146):** prompts, routing, errors and drift output are as before.
- **With several sources and no `datasource:` lines yet (PR #146),** run `python scripts/assign_datasources.py`, review `schema.with_datasources.yaml`, and replace `schema.yaml` with it (it needs a restart like any `schema.yaml` change to the allowlist). `--check` in a deploy pipeline keeps the assignments honest afterwards.
- **With several sources (PR #146),** add `description:` and `keywords:` to each source in `datasources.yaml` (optional; routing works from the retrieved tables without them), restart, and read the start-up lines `Prompt path for data source ...`: a source over `PROMPT_RETRIEVAL_TOKEN_BUDGET` takes the retrieval path restricted to its tables, so raise the budget (leaving ~15% for Persian text) or move tables to another source if you want its cacheable prefix. Audit records now carry `datasource_selection`; a reader that does not know it can ignore it.

## [6.4.1] — 2026-10-03

python-dotenv moves to 1.2.4 so a `.env` saved with a byte-order mark loads its first variable.

### Changed

- **python-dotenv is 1.2.4 in `requirements.lock` and `>=1.2.3` in `requirements.txt` and `webapp/requirements.txt` (PR #144).** Releases from 1.2.3 on strip a leading UTF-8 byte-order mark themselves, where 1.2.2 read it as part of the first variable's name and silently lost that variable. Windows editors often save a `.env` that way, so a `.env` saved as UTF-8 with BOM now loads its first variable instead of losing it, and the startup refusal for a byte-order mark added in 6.4.0 no longer fires on a pinned install (it still names the cause on an older python-dotenv).

### Upgrading

- **Run `pip install -r requirements.lock` after pulling** to move to python-dotenv 1.2.4.

## [6.4.0] — 2026-10-03

API keys can now be read from a JSON file, and a `.env` line python-dotenv cannot use stops startup with its line number instead of a one-line warning that is easy to miss.

### Added

- **`API_KEYS_FILE` reads the key array from a file (PR #142).** Set it to a path (recommended: `project_config/api_keys.json`, which is git-ignored) and put there exactly what `API_KEYS_JSON` holds, formatted however you like, with none of `.env`'s quoting rules. A relative path resolves against the repository root, like `PROJECT_CONFIG_DIR`. The file is read once per process, like an environment variable, so edit it and restart. Setting both `API_KEYS_FILE` and `API_KEYS_JSON` is refused. A missing or unreadable file, a file that is not UTF-8, and invalid JSON (the message gives the line and column) are refused naming the path, never the content. Messages and the "no `denied_columns` field" warning now name the source, for example `API_KEYS_FILE (project_config/api_keys.json)[0] (id='x') ...`, and `verify_deployment.py`, the startup refusal and `issue_api_key.py` mention the file option.

### Changed

- **Unusable `.env` lines are refused at startup with line numbers and names (PR #142).** python-dotenv skips a line it cannot parse with one line on stderr, so a pretty-printed `API_KEYS_JSON` without single quotes, or with an apostrophe in a value such as `"Ali's key"`, ended as "no usable key is configured". `Settings.validate()` now lists every unparsable line, every leftover line of a broken multi-line value (and the variable whose value was cut off at its first line break), and every variable assigned twice with different values (names and line numbers only, never values), and a byte-order mark at the start of the file that hides its first variable ("UTF-8 with BOM", which python-dotenv 1.2.2 as pinned in `requirements.lock` reads as part of the name; newer releases strip it) is named as such, so the server and the `Settings.validate()` check in `verify_deployment.py` fail with the cause. The same variable set twice to the same value is not flagged, and neither is a missing `.env`.
- **A repeated field inside one key object is refused (PR #142).** `json.loads` kept the last of two identical fields silently, so a pasted second `"denied_columns"` replaced the first. This applies to `API_KEYS_JSON` as well as the file. The startup refusal for a bad key configuration now begins `Invalid API key configuration:` instead of `Invalid API_KEYS_JSON:`, because the cause may be in the file.

### Upgrading

- **A `.env` that has an unparsable line, a leftover line, or a variable assigned twice with different values now stops startup.** Fix the lines the message names: wrap a multi-line `API_KEYS_JSON` in single quotes with no apostrophe inside, or delete the stale duplicate (the later one was the one in effect).
- **To move keys to a file:** create `project_config/api_keys.json` containing the array, set `API_KEYS_FILE=project_config/api_keys.json`, remove `API_KEYS_JSON`, restart, and run `python scripts/verify_deployment.py`.

## [6.3.2] — 2026-10-03

A key in `API_KEYS_JSON` with no `denied_columns` field is now warned about once, not every time the key list is read.

### Fixed

- **The "no `denied_columns` field" warning is logged once per key (PR #140).** The key list is read several times: at startup, by `python -m scripts.verify_deployment`, and again every `KEY_CACHE_TTL_SECONDS` while the server runs. Each read logged the warning again for every such key, so `verify_deployment` printed each one twice, and a running server repeated them at startup and on every key-cache refresh, burying real log lines. Each key is now warned about once per process; a new key, or the same id with a new hash, warns again, and adding `"denied_columns": []` stops the warning. Which keys get the warning, and what it says, are unchanged.

## [6.3.1] — 2026-10-03

A configuration file that repeats a key is now refused instead of silently losing the first one, and the deployment check counts API keys the way the server does.

### Fixed

- **A duplicate key in a YAML config file is refused (PR #138).** `yaml.safe_load` kept the last of two identical keys without a word, so a second `datasources:` heading dropped the first block, a whole data source, with no error. Every config file is now read so that a key written twice in one mapping fails with its name and both line numbers, for example `[datasources.yaml] is not valid YAML: duplicate key 'datasources' (first on line 12, again on line 21)`. An explicit key that overrides one brought in by a merge key (`<<: *anchor`) is still allowed.
- **`verify_deployment.py` counts application-database keys (PR #138).** The API key check counted only `API_KEYS_JSON`, so it failed on a deployment whose keys live in the application database (imported at first start, or issued from the admin panel) even though the server starts. It now counts the same merged set the server does, says how many keys came from each place, and resolves `VERIFY_API_KEY` against it. If the database cannot be read it counts `API_KEYS_JSON` only and says so; it fails only when that has no keys either.

### Upgrading

- **A config file that repeats a key is now refused,** naming the key and both lines. A duplicate in `datasources.yaml` or `schema.yaml` stops the server at startup; one in the other files fails when that file is first read, and `python scripts/verify_deployment.py` reports it before then. `relationships.yaml` is optional and is skipped with a warning naming the duplicate, as with any other read error. Remove the duplicate; the later one was the one in effect, so keep that block's content if it is what you meant.

## [6.3.0] — 2026-10-02

A warehouse connection is now described in `datasources.yaml`, and `.env`
holds only the raw password. Existing setups keep working unchanged.

### Added

- **Structured data sources (PR #136).** A source in `project_config/datasources.yaml` may give `host`, `port` (default 1433), `database`, `driver` (default `ODBC Driver 18 for SQL Server`), a login (`username`, or `username_env` naming a variable) and `password_env`, the name of the variable holding the **raw** password. `trusted_connection: true` selects Windows authentication and takes no login. Extra ODBC keywords go under `options`; YAML `true`/`false` become `yes`/`no` and numbers become their text.
  - The URL is built in one place with SQLAlchemy's `URL.create`, which escapes the password itself. A password containing `@ % ] : / ? # & = + ;` or spaces needs no encoding.
  - Two databases on one server are two sources, each with its own pool. One query still runs on one source; keep a single source with a multi-part `db_schema` when questions join across databases.
- **`DB_PASSWORD` (PR #136).** Without `datasources.yaml`, the raw password for `DB_CONNECTION_URL` can be set here instead of being percent-encoded inside the URL. It is applied to the parsed URL. A password in both places is refused at startup.
- **Clear refusals at startup (PR #136).** A `password_env` or `username_env` variable that is unset or empty is refused naming the source and the variable, never the value. A source that sets both `url_env` and structured fields, a `password:`/`pwd:`/`url:` key in the file, a credential-like `options` key (`pwd`, `password`, `uid`, `user`, `user id`) and a placeholder host, database, user name or password are refused too.

### Changed

- **`url_env` is the legacy form (PR #136).** It works exactly as in 6.1 and 6.2, may share a file with structured sources, and still needs the password inside the URL percent-encoded (`@` becomes `%40`).
- **`.env.example` and the example `datasources.example.yaml` (PR #136)** now show the structured form and `DB_PASSWORD_*` variables. The `DB_URL_*` examples are gone.

### Security

- **Passwords are masked in messages (PR #136).** The refusal when `APP_DB_URL` points at a warehouse source used to print both URLs in full, passwords included. It now masks them, as `scripts/verify_deployment.py` does for the connectivity check.
- **urllib3 2.8.0 (PR #136).** The pinned urllib3 in `requirements.lock` moves from 2.7.0 to 2.8.0, which fixes PYSEC-2026-4175, PYSEC-2026-4176 and PYSEC-2026-4177.

### Upgrading

- No action needed. A deployment without `datasources.yaml` is unchanged, and so is one that uses `url_env`.
- **To use `DB_PASSWORD`,** remove the password from `DB_CONNECTION_URL` and put it, raw, in `DB_PASSWORD`. A raw password written inside the URL is not detected or re-encoded, because `/ ? # :` or a literal `%40` cannot be told apart from an encoded one.
- **To move a `url_env` source to the structured form,** copy the host, port, database, login and query parameters from its URL into the YAML (query parameters such as `TrustServerCertificate=yes` become `options`), put only the raw password in a new `DB_PASSWORD_*` variable, replace `url_env` with `password_env`, restart, and run `python scripts/verify_deployment.py`. Remove the old `DB_URL_*` variable afterwards.

## [6.2.0] — 2026-09-28

Tables with the same name in different schemas can be described and
queried, and a query's schema is now checked against the allowlist.

### Fixed

- **`schema.yaml` table keys may name their schema (PR #133).** A key may be qualified, such as `sales.Customer` or `OtherDb.dbo.Customer`; brackets are allowed. This is what lets `sales.Customer` and `ref.Customer` both be described. Before, a qualified key never matched a query, so every query on it was refused as an unknown table.
- **An ambiguous table name is refused with the fix named (PR #133).** `FROM Customer`, when two schemas have a `Customer`, is refused with the new guard reason `ambiguous_table`. The message names the references to use, so the model's retry corrects it, and the web UI explains it in Persian.
- **The prompt shows how to reference a shared name (PR #133).** Such tables get a `Reference as: [sales].[Customer]` line. Prompts for schemas with unique names are unchanged.

### Security

- **A query's schema is checked, not ignored (PR #133).** Before, the guard matched tables by bare name only, so with `Customer` allowlisted in `sales`, a query on `[hr].[Customer]` passed and read a table that is not allowlisted. An explicitly qualified reference must now match the table's known schema. A table with no known schema (a bare key without `db_schema`) still accepts any schema, because there is nothing to check it against.

### Upgrading

- No action needed for a `schema.yaml` with unique table names. A generated query that names the wrong schema is now refused, and the model's retry corrects it.
- **If the same table name exists in several schemas,** key each one with its schema, such as `sales.Customer:` and `ref.Customer:`.
- **Set `db_schema` on every bare-keyed table.** Without it, that table's schema cannot be checked. See `docs/design/TABLE-NAMES.md`.

## [6.1.0] — 2026-09-28

A deployment can query more than one warehouse database, including
databases on different servers. Existing setups keep working unchanged.

### Added

- **Multiple warehouse data sources (PR #131).** `project_config/datasources.yaml` is optional and lists named sources. Each source names only the environment variable holding its connection string, for example `url_env: DB_URL_MAIN`. The connection string itself stays in `.env`, because `project_config/` is versioned and reviewed.
  - Without the file there is one source, `default`, using `DB_CONNECTION_URL`, exactly as before.
  - A template is in `project_config.example/datasources.example.yaml`; the design and roadmap are in `docs/design/DATASOURCES.md`.
- **Each table names its source (PR #131).** A `schema.yaml` table may set `datasource:`; a table without it belongs to the default source. Several databases on one server are one source: a table in another database on that server sets a multi-part `db_schema: "OtherDb.dbo"`, rendered `[OtherDb].[dbo].[Table]`.
- **Queries are routed by their tables (PR #131).** The source is derived from the tables a query reads, never chosen by the model. One query runs on one source. A query whose tables span two sources is refused with the new guard reason `cross_datasource`, and the web UI explains why in Persian.
- **Every source is checked (PR #131).**
  - Startup refuses an application database that matches any source, and a table assigned to a source that is not configured. Admin-panel `schema.yaml` drafts are checked the same way.
  - `/health` pings every source; `database_detail` names each one when there are several.
  - `scripts/verify_deployment.py` and the admin panel run the connectivity, read-only login, row cap and query timeout checks once per source.
  - Schema drift compares each source's tables against that source's own server.
- **The audit record names the data source (PR #131).** The new `datasource` field is the source the query's SQL targeted. Older records simply lack it.

### Upgrading

- No action needed for a single-database setup.
- **To add a second database on the same server,** keep one source and give the table a multi-part `db_schema`, such as `OtherDb.dbo`.
- **To add a database on another server,** copy `datasources.example.yaml` to `project_config/datasources.yaml`, set one `DB_URL_*` variable per source in `.env`, add `datasource:` to that server's tables in `schema.yaml`, and restart. Create the read-only login on every source's server (`docs/db-hardening.md`), then run `python scripts/verify_deployment.py`.
- Changing `datasources.yaml` needs a restart.

## [6.0.2] — 2026-09-28

Running the API and the web UI on different ports or hosts no longer needs
guesswork.

### Added

- **`python -m api` starts the server from settings (PR #127).** It uses `API_HOST` (default `127.0.0.1`) and `API_PORT` (default `8000`) and sends no `Server` header. An invalid `API_PORT` fails with a message naming it. The documented `uvicorn api.server:app ...` command still works unchanged.
- **The server logs its allowed CORS origins at startup (PR #127).** It also shows a hint in both pages when the backend cannot be reached. The hint names the two likely causes (the backend is not running, or this page's origin is missing from `CORS_ALLOWED_ORIGINS`) and prints the origin to add.

### Fixed

- **The admin panel uses the same API address as the main UI (PR #127).** Both pages read it from `web/js/config.js`, with the same precedence: `?base=`, then a saved value, then the file. If `DEFAULT_BASE_URL` is empty, both derive the address from the page's own host and `DEFAULT_API_PORT`. Before this, the admin panel ignored the file and started from `http://localhost:8000`.
- **API addresses are normalised and validated (PR #127).** An address typed without `http://` is no longer treated as a relative path. Previously that sent requests such as `/admin/<host>:<port>/admin/...` to the static server. Now:
  - a missing scheme is added;
  - whitespace, paths and credentials are dropped;
  - IPv6 addresses work;
  - anything else is rejected with a message, and the previous address stays in use.
- **The application's own log lines are shown (PR #127).** INFO-level lines, such as the startup banner and the CORS line, were silently dropped under both documented start commands. When nothing else has configured logging, they now go to stderr at `LOG_LEVEL` (default `INFO`).

### Upgrading

- No action needed for an existing setup.
- **To use `python -m api` on a server reached from other machines,** set `API_HOST=0.0.0.0`; its default listens on the local machine only.
- **If the UI is served from anywhere other than `localhost:8080`,** set `CORS_ALLOWED_ORIGINS` to that origin, for example `http://172.16.101.42:8077`.

## [6.0.1] — 2026-09-27

The remaining `SELECT 1` traffic to the warehouse is gone. A diagnostic kit
helps a DBA find what is really driving disk activity.

### Changed

- **Pooled connections are pinged only after they have been idle (PR #124).** With `pool_pre_ping`, every checkout of a warehouse connection sent a `SELECT 1` first, so the warehouse saw one ping per query.
  - A connection is now pinged only if it has been idle for at least `DB_POOL_PING_IDLE_SECONDS` (default 60). `0` pings on every checkout, and `DB_POOL_PRE_PING=false` never pings.
  - A failed ping refreshes the whole pool, and the checkout is retried on a fresh connection.
  - Measured on 20 back-to-back questions: 20 pings in 5.2.0 and 6.0.0, none now. An idle application sends nothing.
  - Trade-off: a connection the server drops before it reaches the idle threshold makes one query fail before the pool recovers.
- **`/health` always sends its own `SELECT 1` (PR #124),** so it still verifies the database when the checkout was not pinged.
- **Comments and test docstrings describe behaviour, not history (PR #125).**

### Added

- **A read-only diagnostic kit for the DBA (PR #124).** `docs/dba/warehouse-load-diagnostics.sql` and `docs/dba/README.md` show:
  - whether `AUTO_CLOSE` is on;
  - this application's sessions (`program_name = 'local-sql-agent'`);
  - which database file (data, log or tempdb) is busy;
  - the queries with the most physical reads;
  - login audit and trigger checks.

  `SELECT 1` reads no data pages, so disk activity that coincides with it usually comes from something else.

### Upgrading

- No action needed. The new `DB_POOL_PING_IDLE_SECONDS` defaults to 60; set it to `0` to keep pinging on every checkout.
- Upgrading from 5.x: follow the **Upgrading** steps of 6.0.0 first (the system prompt must be at `<PROJECT_CONFIG_DIR>/system_prompt.md`).

## [6.0.0] — 2026-09-27

A release about trust in what the screen says, and about the load the
application puts on the systems around it.

A hall named in a question could be silently ignored, and a filter chip could
show a value the SQL never applied. Both now either work or say plainly that
they did not. Two concurrency defects are fixed: the request cap did not
cover the analyst's main route, and the SQLite application database shared a
single connection between every request thread. The admin panel stops
re-running expensive warehouse checks every thirty seconds.

The major version is for one operational change. The system prompt now lives
in the deployment's own project-config directory, and the server will not
start until it is there. See **Upgrading**.

### Fixed

- **A hall named in the question now filters the answer (PR #119).** The dimension vocabulary only matched a question that contained a stored value in full, so naming just a hall's distinguishing word matched nothing. A new token tier matches on a value's distinctive words when the question names the dimension; when two values tie, the analyst is asked which one, never guessed for. Also in this PR:
  - The vocabulary is now warmed at startup by default, so the first question after a restart is no longer answered as if the value had not been named.
  - A named dimension whose vocabulary could not be checked produces a Persian warning instead of silence.
  - After generation, every text filter the turn presents as applied is checked against the SQL. A missing one triggers one regeneration and, if it is still missing, a Persian warning naming it.
  - Editing an inherited filter on an "among those…" turn now rebuilds the query rather than changing only the chip.
  - Choosing an answer in a "which one?" clarification is now sent to the server in live mode instead of changing only the local chip.
- **Every pipeline route is under the concurrency cap, for the whole response (PR #117).** The cap covered only `POST /query`, and a streaming response released its slot as soon as its headers were sent. It is now pure ASGI middleware and covers `/query`, `/query/stream`, v2 turns (streaming or not) and the assumptions `PATCH`. The slot is held until the body is fully sent, and released on error or disconnect. All blocking pipeline work shares one worker-thread bound (`QUERY_THREAD_LIMIT`). The 503 `SERVER_OVERLOAD` response carries the request id again.
- **The SQLite application database no longer shares one connection across threads (PR #120).** Concurrent requests could end each other's transactions. File-backed SQLite now gives each request its own connection, with WAL journal mode and a busy timeout, and its `-wal`/`-shm` files get the same restricted permissions as the database.
- **Approving an access request is all-or-nothing (PR #115).** Approval runs in one transaction. A failure part-way leaves the request open with no key changed, so it can simply be retried. An approval and a denial of the same request can no longer both succeed.
- **No more backend English in the failure banners (PR #118).** The generic database message is no longer shown under the Persian lead sentence. A database-specific message appears as a labelled technical detail («پیام پایگاه داده:»), and the guard's English rule text stays in the SQL panel rather than in the banner.
- **Table names in the migration tool's reseed step are quoted (PR #114),** and the web UI tests' Node timeout is long enough for slow CI runners.

### Changed

- **Much less load on the warehouse (PR #121).** A DBA reported a steady stream of `SELECT 1` from the application. Most of it came from the admin panel, which re-ran the deployment checks and a full schema-drift scan of the warehouse catalogue every 30 seconds. The deployment checks include a rolled-back `CREATE TABLE` and a `WAITFOR` probe.
  - Those cards now load when the page opens and on their own refresh button, with a server-side cache (`ADMIN_EXPENSIVE_CACHE_TTL_SECONDS`, default 300).
  - The DDL and `WAITFOR` checks run from the panel only when explicitly requested.
  - `/health` caches its database ping (`HEALTH_CACHE_TTL_SECONDS`, default 15).
  - `pool_pre_ping` is configurable (`DB_POOL_PRE_PING`).
  - SQL Server connections identify themselves as `local-sql-agent` (`DB_APPLICATION_NAME`).
  - The deployment runbook lists everything the application sends to the warehouse, and how often.
- **The public repository no longer names one deployment's schema (PR #122).** Its schema qualifiers and table names are replaced by the example schema's own names everywhere.
  - The example configuration is now self-consistent: every relationship and every golden-set case refers to tables and columns the example schema defines, and the example `Order` has foreign keys to `Ring`, `Broker` and `Symbol`.
  - `database/relationship_map.py` honours `PROJECT_CONFIG_DIR`.
  - A test scans the contents and paths of every tracked file for the removed names; it holds them only as salted hashes.
- **The system prompt moved to the project-config directory (PR #122).** It is now read from `<PROJECT_CONFIG_DIR>/system_prompt.md`, with a generic example in `project_config.example/`. `prompts/few_shots.md` and `prompts/business_glossary.md`, which nothing read at runtime, are removed.

### Upgrading

- **Required:** before upgrading, copy your current `prompts/system_prompt.md` to `<PROJECT_CONFIG_DIR>/system_prompt.md`. Without it:
  - the API server refuses to start with `RuntimeError: System prompt not found: <path>`;
  - `app.py` exits with status 1;
  - the eval CLI raises `FileNotFoundError`;
  - the webapp raises the same `RuntimeError`.
- A **relative** `PROJECT_CONFIG_DIR` is now resolved against the repository root, not the working directory. A service started from another directory should check it, or use an absolute path.
- Startup now reads the prefetched dimensions' values from the warehouse. Set `DIMENSION_VOCABULARY_WARM_ON_STARTUP=false` if startup must not touch the warehouse; set `DIMENSION_VOCABULARY_TOKEN_FALLBACK_ENABLED=false` to keep whole-value matching only.
- New optional settings: `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS`, `HEALTH_CACHE_TTL_SECONDS`, `DB_POOL_PRE_PING`, `DB_APPLICATION_NAME` (see `.env.example`).
- Tests that check a deployment's own values now read them from optional files under `<PROJECT_CONFIG_DIR>/_test_fixtures/` (see `project_config.example/_test_fixtures/README.md`) and skip without them.

## [5.2.0] — 2026-09-24

A security fix and the first of the refusal actions the design has always
specified but the product never offered.

The security fix is the same class as finding #11 of the 4.12.1 audit, this
time for the database rather than the model endpoint: when a query failed, the
raw database error reached the analyst, carrying the server's host, port and
instance, the database login name, the database name, and the full SQL with its
bound parameter values. No passwords and no data rows were exposed, but it was
a map of the infrastructure handed to anyone who could make a query fail.

The rest is about what an analyst can do when the answer is "no". A refusal
over a restricted column now offers a way to ask for that column; a refusal of
any kind now shows the statement that was refused; and every failure is
explained in Persian, in the analyst's terms, instead of in the backend's
English. The test matrix also grew to Windows and macOS, which surfaced a real
bug in how the router measured latency on Windows.

### Security

- **Database error text no longer leaks infrastructure detail (PR #109).** Before this change, when a query failed, `database/executor.py` wrapped the raw SQLAlchemy error and both the v2 conversation path and the v1 `/query` path showed it to the analyst. Depending on the database that exposed the server host, port and instance, the driver name and version, the database login name, the database name, and — for any execution error — the full SQL and the bound parameter values. It is the same class of problem as finding #11 in the 4.12.1 audit (the model endpoint), this time for the database. Now one shared classifier (`database/errors.py`) handles every path: connection and login failures become `DATABASE_UNAVAILABLE` and timeouts become `QUERY_TIMEOUT`, both with generic messages; a malformed query shows only the database's own sentence (for example `Invalid column name 'X'. (207)`). It fails closed: database text is shown only when the error is positively recognised as a statement error, and a final check replaces any text that still contains an IP address, a port, `user@host`, or driver/host/login wording. The full raw error is still written to the server log. Recognised across SQL Server, PostgreSQL, MySQL and SQLite.

### Added

- **"Request access" on a denied-column refusal (PR #110).** When the guard refuses a question because a column is restricted for the analyst's account, the refusal card now offers «درخواست دسترسی». A `security` admin reviews the request in the admin panel and approves or denies it (a denial needs a reason, which the analyst can see; the analyst sees their requests' status in the account menu). Approval removes that column from `denied_columns` on every live key the person holds; revoked keys are never touched. The column is taken from the server-side audit record, never from what the browser sends, and approval goes through the existing permission-changing code, so no new code path can widen access. A second request for the same column while one is open merges into it. New table `access_requests`.
- **"See the SQL" on a guard refusal (PR #105).** The statement the guard refused is now kept, as `guard.rejected_sql`, and shown behind a collapsed «دیدن SQL», clearly labelled as not run. It deliberately does not reuse `Turn.sql`, which means "the SQL that ran". The refused statement is now also kept in the audit record, so operators can see what was blocked.
- **The audit-log report shows what the guard refused (PR #106).** `scripts/analyze_audit_log.py` gains a section with refusals counted by reason and by subject (for example `SERVERPROPERTY ×2`). Verbatim refused statements appear only with `--include-examples`, so the default report stays safe to copy off the server.

### Changed

- **Every failure the analyst sees now has a Persian sentence (PR #107).** One sentence per error code and one per guard-refusal reason; the backend's English message no longer appears in those banners (it stays in the server log). Failures raised at the HTTP level, such as maintenance mode, now keep their real code instead of being reported as a network error. The out-of-scope message no longer names a specific deployment's domain.
- **CI runs on Windows and macOS as well as Linux (PRs #101, #104),** across Python 3.11, 3.12 and 3.13. Getting there fixed a real bug: router latency budgets did not work on Windows before Python 3.13, where `time.monotonic()` resolves to only about 15.6 ms, so any backend call faster than that measured as zero and a budget breach was never detected; latency is now measured with `time.perf_counter()`.

### Fixed

- **Identifiers containing `]` no longer break retrieval queries (PR #103).** The two query builders in `retrieval/` wrapped names in square brackets by hand without escaping; they now use sqlglot to quote identifiers.
- **The CI doctest step no longer opens real database connections (PR #102).** Doctests in `eval/runner.py` started background threads that tried to reach the configured warehouse host.

### Upgrading

- The new `access_requests` table is created automatically when the application starts, like the other application-database tables, so the default setup needs no manual step.
- Deployments that manage the application database schema with Alembic instead should run `alembic upgrade head` from the repository root; it applies migration `0004_access_requests`.

## [5.1.0] — 2026-09-20

A security release for the SQL guard. Tables and columns had long since moved
to an allowlist, but functions and session variables were still governed by a
denylist — three names and two identifier prefixes. That asymmetry left a
reconnaissance channel open, and an adversarial review of the guard found it.

The gap had two spellings. A query that names **no table** never engages the
table/column allowlist at all: it escapes the allowlist rather than tripping
it. And a query that *does* name a real, allowlisted table could still carry a
metadata function in its `SELECT` list — `SERVERPROPERTY`, `OBJECT_NAME`,
`COL_NAME` and their relatives produce an output column that is neither a
column reference nor a table reference, so nothing inspected it. The second
spelling is the reason this release is broader than the table-reference rule
first proposed: requiring a table closes only the first.

Closing it properly meant finishing the job the module started — moving the
last construct from a denylist to an allowlist, which also covers metadata
functions SQL Server ships in future rather than only the ones found.

### Security

- **The guard now restricts every accepted query to warehouse-reading shape**:
  allowlisted tables and columns, read through ordinary data functions, and
  nothing else. Three rules run at the end of `validate_sql`, after every
  existing rule, so a query that already violated an earlier rule still reports
  that rule's own more specific reason — the only verdicts that change are
  ones that were previously *accepted*.
  - Server, session and connection state is refused wherever it appears in the
    tree — projection, `WHERE`, `ORDER BY`, a CTE body, a scalar subquery — not
    only in the `SELECT` list. `GETDATE()`/`CURRENT_TIMESTAMP` is deliberately
    exempt: it reads the wall clock, not the server's identity.
  - Unrecognised function calls are refused unless named in a new allowlist,
    seeded with the measured set of legitimate T-SQL data functions that
    `sqlglot` has no typed class for. Everything an ordinary analytic query
    uses — aggregates, casts, date parts, window functions, `STRING_AGG`,
    `IIF` — parses to a typed node and never reaches this check.
  - A query must reference at least one non-CTE table.

### Added

- A `no_table_reference` value on the guard verdict's `reason`, for the
  table-less case. It is additive: clients already fall back to the generic
  refusal action for any reason they do not specially handle. The v2 contract
  document now lists it, and a test asserts every reason the guard can emit
  appears there — the registration was two places and silently drifted to a
  stale document, so it is now three places with a failing build behind it.

### Fixed

- The rejection reported for a state-reading construct named the `sqlglot`
  class (`PARAMETER`, `CURRENTUSER`) rather than the construct. It now names
  what the analyst wrote (`@@VERSION`, `CURRENT_USER`, `OBJECT_ID`), with any
  call arguments cut so a model-generated literal never reaches the structured
  `subject` field — which the contract documents as analyst-facing and never
  internal detail.
- `anyio` is bumped past CVE-2026-63374 and CVE-2026-64847, both published
  after the pin was last refreshed and caught by the `pip-audit` CI gate.

## [5.0.0] — 2026-09-16

The analyst UI redesign. Major, because it changes muscle memory: the topbar,
the anatomy of a turn, the composer's position, and the admin navigation all
move. Nothing about the query path or the SQL guard changes; this release is
about what the screen tells you and how much of it you have to take on trust.

The organising idea is that a conversational analytics UI is a claims
interface. Every answer asserts something about the warehouse, and the screen
either shows what that claim rests on or asks the analyst to assume it. So
what a turn had buried — the resolved question, the assumptions applied, the
stage the pipeline reached, why a refusal happened — is now visible before
the result rather than folded into a drawer.

### Changed — analyst-visible

- **The run-mode switch is gone from the topbar.** Run mode decided whether
  the numbers came from the warehouse or from built-in fixtures, and nothing
  else on screen distinguished the two, so one mis-click sat between an
  analyst and a screen of synthetic figures that look exactly like real ones.
  Simulated mode stays fully available at `?live=0`, where choosing it takes
  intent, and remains the automatic fallback when a backend cannot serve the
  v2 conversational path.
- **Turn anatomy is outcome-first.** The resolved question, assumption chips
  and clarifications render *before* the result node, never only inside the
  details drawer. Each failure mode — guard rejection, model unavailable,
  truncated output, forbidden SQL, execution error — renders its own anatomy:
  what happened, why, and a real next action. A guard refusal is resolved
  before a result card is considered, so a refusal can no longer appear as a
  zero-row result.
- **The composer sits below the transcript**, pinned to the viewport bottom.
- **Admin gains a health summary rail**, a sticky section jump nav, and a
  global refresh with a last-updated stamp; per-section refresh is demoted.

### Added

- **Design tokens in one file** (`web/styles/tokens.css`). Surfaces import
  them and never redefine a hex — light and dark redefine the *same* custom
  properties, which is what stops the two themes drifting apart.
- **An inline SVG icon set** (`web/js/icons.js`), built with DOM APIs so CSP
  `script-src 'self'` stays clean.
- **A user menu** carrying theme, language and the API key — identity moved
  out of the topbar's open surface.
- **Generated SQL is prettified and highlighted** like a code editor, with
  T-SQL rules for bracket identifiers and `N'…'` strings that stock Prism gets
  wrong. Highlighting is presentation-only: the copied text stays byte-exact.
- **A catalog-driven chart engine.** Chart form is selected from a closed
  vocabulary of analytical jobs rather than by an agent, and the boundary is
  specified and pinned by tests — a wrong chart form manufactures a false
  narrative, and that error class must stay deterministic and testable.

### Fixed

- Demo mode died at boot: a `const` the boot path read was declared below its
  use, so module evaluation threw and the whole UI never painted.
- All SQL highlighting was silently dead — the "already patched" marker was an
  enumerable key on the grammar, so Prism iterated it as a token and threw.
- A settled result could sit behind the sticky composer.
- Line and split-bar charts were offered for non-sequential data because a
  calendar word was matched as a substring rather than a whole token.
- The line chart drew its time axis right-to-left in an RTL document.
- The site's flagship hero query filtered on an alias that was never joined,
  so the landing page advertised SQL that cannot run.
- The marketing site fetched four font families from Google's CDN on every
  visit, leaking visitor IPs from the page that sells local-only analytics.

### Accessibility

- Light-palette contrast now clears WCAG 1.4.3: the brand teal measured
  3.74:1 and secondary text 4.40:1 against a 4.5:1 floor. Hue and saturation
  are unchanged; only lightness moved.
- Service health no longer rides on colour alone (WCAG 1.4.1) — state is
  carried in text and shape, so it survives both colour-vision deficiency and
  a monochrome screen.
- Interactive control edges reach 3:1 via a dedicated token; RTL layout and
  24×24 touch targets corrected across the chrome.
- Chrome ships no emoji: font-dependent colour glyphs render differently per
  OS, ignore `currentColor`, and carry no reliable accessible name.

### Internal

- `Turn` errors carry a `request_id` and a structured guard reason.
- The design policy, its invariants, and the chart-engine boundary are
  written down under `docs/design/` and pinned by tests rather than convention.
- Includes everything in 4.12.1.

## [4.12.1] — 2026-09-14

A security release. An independent audit (code review across MLSecOps, data,
API/web and infrastructure, plus a live penetration test against a running
server) found twenty issues, four of them High. Nineteen were code-fixable and
are addressed here; the twentieth is a repository-history matter left to the
owner (see **Not in this release**). Every fix carries a regression test under
`tests/security_audit/`, and the two live-confirmed leaks below were re-tested
against a running server after the fix, not only in unit tests.

### Security

- **Admin panel: stored XSS that escalated `operations` to `security` is
  closed** (High). A `principal_id` carrying a `"` was accepted, stored, and
  re-served into an HTML attribute where `escapeHtml`'s `textContent` idiom
  never encoded the quote. Broken in four independent places: the escaper now
  encodes `"`/`'`, rows are built with `createElement`/`dataset` rather than
  string interpolation, `principal_id` carries `pattern=^[A-Za-z0-9._-]+$`, and
  both static pages ship a `<meta>` CSP whose `script-src` excludes
  `'unsafe-inline'`.
- **`webapp/` no longer bypasses the per-principal column ACL** (High). The
  Flask app called the query pipeline with no principal, which the runner read
  as "no column restriction", so every `denied_columns` rule the admin panel
  could set was void on that path. It now maps the logged-in user to a real
  `Principal`, and an unmapped user gets a maximally restrictive fallback rather
  than unrestricted access.
- **Secrets and data files are created with owner-only permissions** (High).
  Nothing in the tree restricted file modes, so the Flask session-signing key,
  the app database, the session store and exports were world-readable on a
  POSIX host — a local shell could forge an admin session. A single
  `core/fileperms.py` authority now applies `0o600`/`0o700` at every creation
  point, SQLite `-wal`/`-shm` sidecars included.
- **The internal model endpoint no longer leaks in error text** (Medium; found
  Low, raised after a live test). A model-unreachable failure returned the
  backend's host, port, scheme and retry policy to any authenticated analyst.
  Both paths are fixed: the v1 `/query` path moves the raw transport error to a
  server-only `detail`, and the v2 conversational turn — a **second leak the
  first pass missed**, caught by a post-remediation live re-test — logs the raw
  error server-side and returns a generic summary, since `TurnErrorInfo` has no
  client-safe field to carry detail.
- **HTTP security headers are sent** (Medium). Responses now carry
  `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy`. The
  middleware also strips an app-set `Server` header, but uvicorn appends its own
  at the protocol layer where no middleware can reach it, so the documented
  start command now passes `--no-server-header` — the only place the banner is
  actually suppressed on the wire (`README.md`, `docs/deployment-runbook.md`).
- **`/health` is cached, so an unauthenticated flood cannot drain the query
  pool** (Medium). The probe result is cached briefly; a load balancer and an
  attacker both get the cached answer instead of one live pool probe per call.
- **CSV/Excel exports defuse formula injection** (Medium). A cell beginning
  `= + - @` (or a leading tab/CR) is prefixed so a spreadsheet cannot execute
  it, on both the API and Flask export paths.
- **Warehouse values are fenced as untrusted data in the model prompt**
  (Medium), reducing second-order prompt injection through dimension values
  pulled from the database.
- **Flask login hardened** (Medium): per-IP failure throttling with a window
  that lifts on its own (a throttle that never lifted was a denial of service in
  its own right), session cookies set `Secure`/`HttpOnly`/`SameSite=Strict`,
  and a session-bound CSRF token is enforced on every POST including `/login`.
  The real administrator username is no longer a baked-in default; a missing
  `ADMIN_USER` is now a startup failure rather than a guess.
- **Dependencies are pinned** with a `requirements.lock`, and CI runs
  `pip-audit` against it so a known-vulnerable pinned dependency fails the
  build.
- **Lower-severity hardening**: audit-log write failures surface a counter
  rather than failing silently; a dormant cache-scope parameter is documented
  as a trap before the future cache tier that would trip it; identifier quoting,
  the asymmetric default ACL between env and panel keys, and a global session
  cache with no per-owner quota are all addressed.

### Fixed

- A `frame-ancestors` directive that a `<meta>` tag cannot deliver was removed
  and the equivalent reverse-proxy configuration documented instead, so the
  page no longer logs a console error on every load.

## [4.12.0] — 2026-09-08

### Added

- **The conversational path can summarise its own result, and the analyst
  decides whether it does.** It never could: `session/engine.py` set
  `interpretation=None` as a literal from the commit that introduced the
  file, and never ran an `interpret` stage at all — so the web UI's fifth
  pipeline step, labelled "تفسیر", could only ever sit at "در انتظار".
  That was invisible while none of the five steps moved, and conspicuous
  the moment 4.11.0 made the other four tick.

  Producing a summary sends up to twenty rows of **real query results** to
  the model, so it is opt-in per request rather than on for a whole
  deployment: `AskTurnRequest.interpret` defaults to `false`, matching
  `/query`'s own default since phase 2, and the web UI exposes it as a
  checkbox under the question box that each analyst sets for themselves
  and that persists in their own browser. The label states the cost, not
  just the benefit — the person who asked the question is the one who
  knows whether these particular rows should go to a model.

  Asking for it also runs an `interpret` stage, so the fifth step reports
  what actually happened rather than staying decorative.

### Changed

- **The interpretation logic moved to `llm/interpret.py`.** It lived in
  `api/runner.py`, reachable only from `/query`; the session layer sits
  below the API layer and cannot import from it. Moved rather than copied,
  because it carries a data-governance gate — the refusal to send rows to
  a remote backend without `LLM_ALLOW_REMOTE` — and two copies of a gate
  is one copy that gets fixed and one that does not.
  `api.runner._interpret` is now a thin wrapper, and its existing gate
  tests cover the shared implementation unchanged.

---

## [4.11.2] — 2026-09-08

### Fixed

- **The admin panel laid out its English output right-to-left.** The page
  is `dir="rtl"`, which is right for its Persian chrome and wrong for what
  it displays: deployment-check cards carry
  `scripts/verify_deployment.py`'s own text — exceptions, SQL, connection
  URLs, Windows paths. Rendered RTL, `Settings.validate()` displayed as
  `()Settings.validate`, and a connection URL broke across lines in the
  wrong order. This is output an operator reads character by character to
  find a typo in. The check card is now LTR as a block, since its name,
  status pill and detail are one English sentence.

- **The keys table showed `.env` as `env.`** Its leading dot is a neutral
  character, so an RTL paragraph moved it to the end — a filename
  displayed wrong, in the column that says where a key came from. Cells in
  that table, and values in the key/value lists, now take direction from
  their own content (`unicode-bidi: plaintext`), because the same field
  legitimately holds a Persian maintenance note on one row and a path on
  the next.

- **Principal ids and role names are isolated**, so a hyphen inside
  `ops-1` cannot be reordered by the surrounding paragraph — the treatment
  `style.css` already gives numbers via `.num`.

### Added

- `tests/web_ui/test_web_ui_admin_direction.py`. The `.env` case is why
  these are assertions rather than a review habit: four characters, looks
  almost right, and invisible in the markup. It was found by rendering the
  panel and looking at it.

### Checked, and already correct

The rest of the panel was audited against the same failures found in the
analyst UI — page-level overflow, content clipped outside a scrollable
ancestor, labels rendered outside their container — at 1280px, 900px and
760px. None of them are present: the panel has no SVG, so the chart's
anchor bug does not apply, and the topbar inherits the wrapping fix from
4.11.1's shared `style.css`.

---

## [4.11.1] — 2026-09-08

### Fixed

- **Chart labels rendered outside the chart.** `web/js/render/chart.js`
  computes every position left-to-right — x grows rightward, and
  `edgeSafeLabel` flips `text-anchor` between `"start"` and `"end"` to keep
  a label inside `[0, W]`. But `text-anchor` resolves against the inline
  base direction, and both host pages are `dir="rtl"`, so the SVG
  inherited rtl and every one of those decisions meant its opposite.

  Measured in a browser: a label clamped to `x=2` with anchor `"start"`
  rendered at `x=-140`, 140 units outside the box — on the exact side the
  clamp existed to protect. Every label the function tried to rescue was
  precisely the one it threw out. The SVG now pins `direction: ltr`;
  Persian text inside is unaffected, since base direction sets where a run
  is anchored, not how it is shaped.

- **A mispositioned label escaped the chart entirely.** `.chart-block svg`
  carried `overflow: visible`, so a label outside the viewBox drew over
  the card around it rather than being clipped. Now `hidden`: a future
  geometry mistake reads as a cut-off label inside the chart instead of
  text across the layout.

- **Summed rial figures overflowed their summary tile.** The strip's grid
  tracks were sized at 140px for counts and percentages; a twenty-digit
  total at 17px needs far more, and a grid item's default `min-width: auto`
  meant the `1fr` track could not shrink to contain it. Tracks widened,
  `min-width: 0` so the track wins, and the value now scales down and
  wraps before it would clip — the digits cut in RTL are the most
  significant ones.

- **Ranking labels truncated to about twelve characters**, which turns
  most real company names into an ellipsis and a hint. The category track
  goes from 130px to 176px; the bars lose the difference and stay legible,
  where an unreadable label makes the whole ranking unreadable.

### Added

- `tests/web_ui/test_web_ui_chart_direction.py`. The existing chart test
  drives the real module against a stub DOM with no layout, fonts or bidi
  algorithm, so this class of bug was structurally invisible to it. The
  new test asserts the declaration whose absence *was* the defect, and
  checks its own premise: if `chart.js` ever resolves anchors against the
  document direction, the test says to delete itself along with the CSS
  rather than work around it.

---

## [4.11.0] — 2026-09-08

### Fixed

- **The web UI's five pipeline steps now actually tick.** They never did.
  The SSE endpoint emitted exactly one `stage` event, before the engine
  was even called, naming a stage `"plan"` — an id no step has. So
  `renderPipeline`'s `setStage` looked for `[data-step="plan"]`, found
  nothing, and returned without a word; all five steps sat at "در انتظار"
  for the whole turn while the answer appeared beside them.

  Nothing in the stack was broken enough to notice: the event was
  well-formed, the stream was valid, the turn was correct, and the one
  test covering the stream asserted the single frame as if it were the
  contract.

  Progress is now live. `StageTimer` already knew when each stage started
  and finished and had no way to say so; it takes an optional observer,
  `TurnEngine.ask` threads one through, and the endpoint bridges the
  worker thread back to the event stream with a queue. Each of the five
  steps reports `running` when it starts and `done` — or `error` — when it
  ends, including the repeats a correction round genuinely produces.

- **Generated SQL is formatted before it is displayed.** `sql_display` was
  the model's own text, so its layout was the model's mood: a tidy
  multi-line statement for one question and a single 300-character line
  for the next, which reads as the system formatting sometimes and not
  others. It is now re-rendered with sqlglot — already a dependency, and
  already the thing that parsed this SQL to validate it.

  Display only. `Turn.sql` stays byte-for-byte what the guard validated
  and the database ran; re-rendering that would mean the audit trail
  recorded a statement nobody executed. `pretty_sql` never raises — SQL it
  cannot re-render is shown as generated, because no formatting problem is
  worth failing a successful query over.

---

## [4.10.3] — 2026-09-08

### Added

- **Three badges for guarantees a build step actually enforces**, each
  linking to the step that makes it true: `SQL guard: AST allowlist`
  (`security/sql_guard.py`, parsing with sqlglot rather than matching
  strings), `engine: no domain literals`
  (`tests/test_no_domain_literals.py`, which fails the build if a real
  warehouse name reappears in engine source), and
  `doctests: enforced in CI` (a required workflow step across fourteen
  packages, not an optional one).

  `tests/test_readme_claims.py` covers these the way it already covers the
  coverage figure, but against a different failure: not drift, but the
  backing quietly going away. Each badge must still be present, still link
  to its evidence, and that evidence must still exist and still contain
  the thing being claimed — so a guard deleted in a refactor takes the
  badge down with it instead of leaving the README promising something
  that stopped being true.

---

## [4.10.2] — 2026-09-08

### Changed

- **Coverage gate raised 85% → 90%.** The README now advertises the
  measured figure, and a badge saying 92% with a gate at 85 is a number
  nobody is holding: coverage could fall seven points with every build
  still green and the README still claiming otherwise. Measured coverage
  is 92.84%, so 90 leaves real headroom without letting the claim rot.

- **README badges rebuilt around what can be verified.** The CI status
  and the release version now read GitHub directly and cannot go stale.
  Coverage, test count, Python versions and licence remain fixed
  strings — every one of them re-measured for this release, because two
  were wrong: `Tests-2573` never matched any real count, and
  `Version-4.6.1` was three releases behind.

### Added

- **`tests/test_readme_claims.py` watches the coverage badge.** The real
  percentage cannot be measured from inside the run being measured, but
  the relationship between the advertised figure and the gate enforcing
  it is a pure configuration fact, and that gap is the failure mode. The
  build now fails if the badge promises more than `fail_under` holds up,
  if the gate moves up without the badge following, or if a hardcoded
  version badge comes back. Same idea as `tests/test_version.py`, applied
  to the claim whose truth lives in another file.

### Documentation

- **The README described a project three releases old.** Its feature table
  had no admin panel and no analyst web UI — the two largest things
  shipped since it was written — and stated the test count as 2,235 in
  three separate places. The configuration reference, corrected in 4.10.1,
  is now joined by a Tests section that says what the coverage number does
  *not* cover: the interactive wizards excluded by policy, and the two
  modules excluded as a declared ratchet.

---

## [4.10.1] — 2026-09-08

Documentation catch-up for 4.9.0 and 4.10.0, which shipped code without
updating the prose that described it.

### Fixed

- **`web/README.md` said a 401 clears the stored key.** It has not since
  4.10.0 — the opposite is now true, and deliberately so. This was a
  documented promise the code no longer kept.

### Documentation

- **`docs/fa/getting-started.md` §2.9 — "مدل ریزنینگ و «جوابی نگرفتم»".**
  The whole failure, end to end: why a reasoning model returns nothing at
  the default token cap, the four audit fields that identify it *together*
  (`finish_reason: length`, `completion_tokens` exactly at the cap,
  `reasoning_detected: true`, empty SQL), and the two fixes with a
  per-server table for `LLM_EXTRA_BODY`. Including the warning that
  matters most: a server silently ignores a field it does not recognise,
  so the effect has to be confirmed in the audit record rather than
  assumed. This is the guide the deployment that hit this actually
  follows, and it said nothing about any of it.

- The Persian troubleshooting table gains rows for `LLM_OUTPUT_TRUNCATED`
  and for the pre-4.10.0 form of the same failure, and its 401 row now
  says the stored key is no longer cleared.

- §2.2 notes that the key field collapses once a key is saved — a reader
  looking for the field after saving is not signed out.

- `README.md`'s configuration reference gains `LLM_NUM_PREDICT` and
  `LLM_EXTRA_BODY`, neither of which was listed, and its `API_KEYS_JSON`
  row now shows the three capability flags 4.7.0 made issuable.

- `docs/en/tutorial.md`'s error table gains `EmptySQLResponseError` and
  `TruncatedSQLResponseError`; `docs/api-contract-v2.md` says what
  `finish_reason: "length"` with empty SQL now surfaces as; and
  `docs/deployment-runbook.md`'s "raise `llm_num_predict`" advice now
  names the cheaper alternative for a reasoning model.

---

## [4.10.0] — 2026-09-08

### Fixed

- **A completion cut off at the token cap is named as such, and not
  retried.** When a reasoning model spends its whole budget thinking, the
  response is empty with `finish_reason="length"`. The correction loop
  treated that as bad SQL and re-prompted twice — "the SQL query you
  generated failed / --- FAILED SQL --- (nothing) / --- ERROR --- LLM
  returned an empty response" — which cannot succeed, because nothing
  about the truncation depends on the question. Three rounds, ~17 seconds,
  one certain outcome.

  It now returns on the first round with `LLM_OUTPUT_TRUNCATED` and a
  message naming the limit in force, `LLM_NUM_PREDICT`, and
  `LLM_EXTRA_BODY`. `EMPTY_SQL_RESPONSE` stays for its own case — a model
  that stopped cleanly and said nothing — which may well differ on a
  retry, so that path still retries. Both the v2 conversational path and
  `/query` make the distinction.

- **The web UI no longer looks signed out while holding a valid key.** The
  key field is cleared after a save, so in live mode an empty password box
  sat in the topbar permanently — which is what a page shows when you are
  signed *out*, and analysts read it that way and re-entered a key they
  already had. With a key stored, the control now collapses to
  `کلید: ذخیره شده ✓` plus a "تغییر کلید" button; the entry field appears
  only when there is no key, or when the server has rejected the one
  stored. Same change in the admin panel, where it was worse: an empty
  login box beside ten loaded cards.

- **A 401 no longer destroys the stored key.** The raw key is printed
  exactly once by `scripts/issue_api_key.py` and is not recoverable, and a
  401 is not always the key's fault — a server restarted with a different
  `API_KEYS_JSON`, or an application database briefly unreachable, produces
  one from a perfectly good key. The UI kept clearing it, turning a
  transient server condition into "find that 43-character string again".
  The key is now kept and marked rejected, with the entry field revealed
  so a genuinely bad key can still be replaced.

- **The RTL topbar no longer pushes its own controls off the screen.**
  Neither half of the bar could shrink below its content width (no
  `min-width: 0`), so instead of wrapping it overflowed — and in RTL that
  overflow runs off the *left* edge into negative coordinates, where
  nothing can scroll to it. Below roughly 1400px this silently put the
  API-key controls and the mode switch outside the window. Both halves now
  shrink and wrap, and the collapsed key control removes ~300px of width
  that was causing it in the first place.

---

## [4.9.0] — 2026-09-08

### Added

- **`LLM_EXTRA_BODY` — server-specific fields, passed through verbatim.**
  A JSON object merged into every chat-completions request body. Empty by
  default, which sends exactly what this project has always sent.

  It exists for one concrete failure. A reasoning model spends its
  completion budget thinking before it answers, so with `LLM_NUM_PREDICT`
  at its 512-token default a Qwen3-class model can consume every token on
  reasoning and be cut off before emitting a single character of SQL. That
  arrives as `EMPTY_SQL_RESPONSE` — a description of the response, not of
  the cause — after the correction loop has retried it twice.

  Turning that reasoning off is not expressible in the OpenAI
  chat-completions schema, and every server spells it differently: vLLM and
  SGLang take `chat_template_kwargs.enable_thinking`, Ollama takes `think`,
  OpenAI takes `reasoning_effort`. Encoding those dialects here would mean
  claiming to know every inference server's private vocabulary and silently
  sending the wrong key whenever the guess was wrong. The passthrough says
  the honest thing: these are your server's fields.

  ```ini
  LLM_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}
  ```

  Applies to the structured (`response_format: json_schema`) path too — a
  constrained decode is still a decode, and a reasoning model asked for
  JSON reasons first.

  Keys that would let one environment variable contradict the settings the
  `llm` status block reports are refused at parse time rather than
  overridden at merge time: `model`, `messages`, `temperature`, `top_p`,
  `seed`, `max_tokens`, `stop`, `stream`, `n`. The refusal names the
  setting to use instead. A malformed value fails in `Settings.validate()`
  — so `scripts/verify_deployment.py` and the server's own start-up gate
  catch it, rather than an analyst's first question.

### Documentation

- **`LLM_NUM_PREDICT` is documented in `.env.example`.** It never was, so
  the one setting that explains an empty response from a reasoning model
  was discoverable only by reading `config.py`. The entry names the
  signature to look for in the audit record — `finish_reason: "length"`,
  `completion_tokens` exactly equal to the cap, `reasoning_detected: true`,
  empty `generated_sql` — because that combination does not look like a
  token limit from the UI, which reports only that the model returned
  nothing.

---

## [4.8.0] — 2026-09-07

The admin panel can manage keys and roles. Until now it could not, and
nothing said so: `POST /admin/keys`, the disable/enable/revoke routes, the
ACL route and the role routes have all existed since phase 2 with no UI
calling any of them, so issuing a key or granting a role meant the CLI —
and a revocation phase 2 deliberately made *immediate* was, in practice,
immediate once someone reached a terminal.

### Added

- **A "کلیدها و دسترسی‌ها" section in the admin panel.** Lists every key
  with its state, source (`.env` or the panel), and column restriction;
  issues new keys; disables, re-enables and revokes them; edits a key's
  `denied_columns`; and grants or revokes either admin role. Each control
  is gated server-side exactly as its route always was — `operations` for
  the key lifecycle, `security` for anything that changes what a key can
  see — and the panel never guesses at that gate client-side: it makes the
  call and reports the server's own answer.

  Three things the routes alone do not make visible, which this section
  states at the moment each one matters:

  - A key issued through the API starts with **every** column denied. It
    authenticates and can read nothing until a security admin loosens it.
    That split is deliberate, but an operations admin who does not know it
    has just handed someone a key that looks broken.
  - Disable is reversible; revoke is not, and they are one button apart.
    Revoke asks for the key's id to be typed rather than confirmed — every
    row's buttons sit in the same place, and a confirm dialog defends
    against not reading, not against clicking the right button on the
    wrong row.
  - The raw key exists exactly once, in the issue response. The reveal is
    not written to `localStorage`, not put in the URL, and never
    re-rendered by a later refresh.

- **A 403 on a capability-gated card renders inside that card.** The
  panel's sections do not share one capability, so a key holding only some
  of the three legitimately sees a 403 on the rest. That previously raised
  the page-level "this key is not an admin key" banner — false, and
  pointing at the wrong fix. The keys card now names the missing flag.

### Fixed

- **Revoking a role granted through `API_KEYS_JSON` is refused instead of
  silently doing nothing.** `appdb.key_store.get_active_principals` unions
  the environment's capabilities back into every resolved principal on
  every load, so deleting a row from `admin_principal_roles` cannot remove
  one — there may be no row at all. The route returned
  `200 {"granted": false}` and the next read of the holders still listed
  the principal. It is now a 409 naming the only thing that does work:
  remove the flag from that entry and restart.

  Found by the new panel section, which is the first caller that made this
  route clickable. The last-holder rule still takes precedence when both
  apply — losing a capability entirely is the larger fact, and that error
  already points at `API_KEYS_JSON` and a restart.

### Changed

- `web/admin/admin.js`'s `_get` and `_post` were two hand-written copies
  of the same auth-and-error block. Phase 7 needed a third verb, and a
  third copy would have meant three places for the 401/403 distinction to
  drift apart — a distinction the panel depends on to tell "enter a key"
  from "this key lacks a capability". Both are now thin wrappers over one
  `_request`, alongside the new `_patch`.

- The panel header no longer claims the page only reads. That stopped
  being true in phase 4.

---

## [4.7.0] — 2026-09-07

### Added

- **`scripts/issue_api_key.py` can grant all three admin capabilities.**
  `--operations` and `--security` join `--admin`, plus `--full-admin` for
  all three at once. All three fields have been parseable by
  `security/auth.py` since the admin panel's phase 2, but only `--admin`
  was issuable — so granting the other two meant hand-editing the
  `API_KEYS_JSON` array, which is exactly the "raw key and digest get
  confused" territory this script exists to keep operators out of.

- **The script warns when asked for a capability set that produces a
  partly-403 admin panel.** The panel's ten sections do not sit behind one
  capability: `require_admin` serves four of them, and
  `require_operations_or_security` the other six. A key holding only
  `admin` therefore loads a panel where more than half the sections report
  a permissions error — which reads as a broken deployment rather than as
  a decision someone made at issue time. Both partial shapes are now named
  at issue time, with the flag that fixes them.

### Fixed

- **The raw key is no longer the easy half of the output to miss.** This
  script prints two strings that go to two different places: the raw key
  to a browser field, the JSON entry to `.env`. 4.6.1 added a 401 hint for
  operators who pasted the digest into the key field, on the reasoning
  that the entry is "the conspicuous, copy-pasteable artefact" — correct,
  and aimed one step too late. The entry was conspicuous *because this
  script made it so*, printing both values as equally-weighted flat lines
  under similar-looking headings; operators reported not registering that
  a second value existed at all.

  The raw key is now framed, indented, and labelled with its length and
  its destination, and the two are numbered as sequential steps rather
  than listed as outputs. Plain ASCII, no colour: this output is routinely
  piped, redirected, and pasted into tickets.

- **`VERIFY_API_KEY`'s usage hint is spelled for the shell it is printed
  into.** It read `VERIFY_API_KEY=<raw key> ...` — the POSIX inline-
  environment form, which PowerShell does not have. On Windows, where
  `docs/fa/getting-started.md` walks through every setup step in
  `powershell` blocks, pasting it produced `The term 'VERIFY_API_KEY=...'
  is not recognized as a name of a cmdlet` — which reads as "this script
  is broken", the opposite of what a check whose whole job is building
  confidence should do. The hint now branches on the platform.

### Documentation

- **The capability-to-panel-section mapping is written down**, in
  `docs/fa/getting-started.md` §2.5.1, `docs/deployment-runbook.md` §1.1,
  and `.env.example`. It was previously derivable only by reading the
  `Depends(...)` on each route, which is why "the panel is half 403" had
  no answer anywhere in the documentation.

- **`docs/fa/getting-started.md` §0.4 says what the pre-flight check does
  not prove.** Without `VERIFY_API_KEY` it confirms that `API_KEYS_JSON`
  parses and holds at least one key — not that *your* key authenticates.
  Passing it while the browser returns 401 was a coherent state with no
  explanation in the guide.

- Every `python scripts/issue_api_key.py` invocation in the docs is now
  `python -m scripts.issue_api_key`, matching the form the setup guide has
  used throughout. README's step 4 also no longer shows the command
  without its two required arguments.

---

## [4.6.1] — 2026-09-06

### Fixed

- **A 401 now says so when the presented credential is a SHA-256
  digest.** `scripts/issue_api_key.py` prints two things: the raw key,
  once, under a line saying it will not be shown again — and then an
  `API_KEYS_JSON` entry containing that key's `key_sha256`. The entry is
  the conspicuous artefact: it is what goes into configuration, what gets
  copied into a ticket, and what is still on screen after the "copy this
  now" line has scrolled away.

  So presenting the digest at the key field is an easy and repeatable
  mistake, and it was one this system said nothing about — hashing a hash
  finds no match, and "Missing or invalid API key" is indistinguishable
  from a typo, a revoked key, or a key meant for another deployment.

  The reverse mistake has been a loud start-up error since phase 8, where
  `key_sha256` is checked against a 64-hex pattern. This is the same
  courtesy in the direction an operator actually hits, where there is no
  start-up left to fail. The hint cannot fire on a legitimate key:
  `secrets.token_urlsafe(32)` is 43 characters of URL-safe base64 and can
  never be 64 hex characters.

### Documentation

- **`docs/fa/getting-started.md` names the two traps the key-issuance
  flow sets.** Which of the two printed values goes to the browser and
  which to `.env`, the lengths that tell them apart at a glance, and a
  one-line command that hashes a raw key so it can be compared against
  what is configured. Plus the `.env` one: a multi-line `API_KEYS_JSON`
  must be wrapped in single quotes, because `.env` is line-based and
  dotenv otherwise takes `[` as the whole value and reads the remaining
  lines as new keys — a silent failure a long way from "the server will
  not start".

---

## [4.6.0] — 2026-09-06

Phases 3 through 6 of `docs/admin-panel-architecture.md`. The panel is
complete, and that document's "still to be decided" section is empty.

### Added

- **`project_config/` is editable, versioned, and reversible.** The nine
  YAML files live in the application database as full bundle snapshots —
  never diffs, so restoring any depth is a read rather than a replay.
  Restoring an old version always creates a *new* one: intervening
  history is never touched, and the restore is itself reversible.

  `schema.yaml` stays the security admin's. An operations save that
  touches it is held as an unapplied draft until a security admin
  approves or rejects it; the other eight apply immediately. Every save
  is validated through the real loaders, diffed — naming exactly which
  tables and columns enter and leave the guard's allowlist — and dry-run
  against the golden set before it can apply. A save names the version it
  was based on and is refused, not merged, if that version has moved on.

- **A channel for "this answer was wrong".** The audit trail records what
  happened *technically*; a query that executed cleanly and returned a
  plausible wrong number was indistinguishable from a correct one in
  every artefact this system produced. The analyst-facing flag control
  lives in `web/`, because an analyst is the only person who knows.

  The feedback row deliberately holds no question, no SQL and no result
  rows — those columns do not exist. Both are already in the audit log
  under the same ids, so triage joins to them rather than duplicating
  them into a second place to keep consistent and a second place to leak
  from.

  Nothing auto-applies, structurally: `appdb/feedback.py` does not import
  the configuration write path. A promoted golden case is written
  `pending_expected` and the regression gate skips it entirely until
  someone supplies the expectation — a promoted case that silently joined
  the baseline with a wrong expectation would make the gate enforce the
  bug.

- **Moving the application database between backends.**
  `python -m scripts.migrate_app_db`. Starting on SQLite is only safe if
  leaving it is supported, and leaving is the expected path. Identifiers
  are preserved exactly — renumbering would turn audit and feedback
  references into dangling ones with nothing failing at the time. After
  copying, both sides are re-read and compared per table by row count and
  content hash; a migration that cannot prove equality is a failed
  migration and says so. The source is never mutated, and its hash is
  printed before and after every run, including a failed one.

- **The operational tier.** Maintenance mode; a read-only schema-drift
  comparison against the live warehouse; vocabulary freshness with manual
  refresh; per-analyst usage and rate-limit pressure; cache controls.

  Maintenance mode is a switch rather than a trap: the panel stays
  reachable, `/health` and `/` stay open, analyst queries get a 503 with
  a body instead of a hang, and in-flight requests drain. Both properties
  fall out of *where* the check is placed — a dependency on the routes
  that submit a query and on nothing else — rather than from an exemption
  list a new route could be forgotten from.

  Schema drift proposes and never applies: `schema.yaml` is asserted
  byte-identical after a check that finds differences.

### Changed

- **Failed authentication gets its own rate-limit bucket.** Keys are 256
  bits, so guessing one is arithmetically impossible and tightening the
  shared limit for that reason would be theatre. The real gap was
  structural: authentication runs before the limiter, so the `ip:` bucket
  held auth failures *together with* the unauthenticated traffic a
  monitoring probe lives in — one client looping on a stale key could
  starve the probe sharing its address, and the resulting 429 reads
  exactly like an outage. Failures now bucket separately and small, and
  every failure is recorded with its source address and surfaced, because
  the threat to an admin key is leak rather than guessing.

- **The admin-action trail is retained by time, not size.** It rotated by
  size like every other JSONL log, which discards the oldest evidence
  first — exactly when there is most activity. Anyone wanting to bury one
  action could do it by generating noise. A trail whose purpose is "each
  role can read that the other acted" cannot depend on a mechanism the
  watched party defeats by volume. Configuration versions and feedback
  rows are kept in full: capping rollback depth removes the feature, and
  deleting resolved feedback destroys the trend the loop exists to show.

### Fixed

- **A knowledge-base edit did not move the query cache key.** Only three
  of the nine configuration files feed the static prefix, so editing
  `aliases.yaml` or `retrieval_hints.yaml` changed what the engine
  retrieved — and therefore the SQL it wrote — without moving the
  prefix's content hash. Every answer cached under the old configuration
  kept being served. The shape of that failure was the bad part: an
  operations admin makes the edit, asks the same question to check it,
  and gets the pre-edit answer back with nothing to say why. The cache
  key is now composed with the configuration bundle's own version id.

- **Configuration import was the one unguarded way into a new version.**
  Save, restore and approve all validated, diffed, dry-ran and held a
  `schema.yaml` change from an operations-only caller as a draft. Import
  did none of that when the deployment had no version history yet — and
  since history is created lazily, "no history yet" means "nobody has
  opened the configuration page since this deployment came up", exactly
  when the first admin action is most likely to *be* an import.

- **The migration tool copied nothing, and said so nowhere.** A
  `sqlite_sequence` existence check asked through `inspect(engine)`.
  SQLite engines here use `StaticPool`, so every checkout shares one
  DBAPI connection: that opened a second `Connection` facade over the
  same underlying connection, and closing it returned it to the pool,
  which resets with a ROLLBACK. The rollback landed on the copy's own
  transaction and discarded every row — silently, no exception, no
  warning, `import_export` returning normally. A read-only existence
  check, of a table that does not exist, threw away the entire migration.
  Only the per-table verification caught it.

- **`AuditRecord.as_dict()` silently dropped `session_id` and
  `turn_id`.** 4.2.0 added the fields and never serialised them, which
  would have made the feedback loop's join to the audit log impossible.

### Notes

- Maintenance mode's flag lives in the server process's memory, which is
  what makes its own toggle unblockable — and also means the migration
  tool, a separate process, cannot observe it. That tool keeps its own
  recent-write-activity refusal and says so in its message. Moving the
  flag into the application database is the clearest remaining piece of
  work on the panel, and is recorded as such.
- Exercised against SQLite only. No PostgreSQL or SQL Server was
  reachable, so those backends are implemented and unverified by
  execution.

---

## [4.5.0] — 2026-09-06

### Added

- **An application database.** A new `appdb/` package: one schema, one
  SQLAlchemy layer, several backends. SQLite when `APP_DB_URL` is unset —
  the only genuine zero-configuration fallback, since PostgreSQL is a
  server and cannot be made to appear. Elsewhere, tables are created
  inside a database someone else provisioned, because `CREATE DATABASE`
  needs rights a DBA will not grant an application.

  `session/persistence.py` is ported off raw `sqlite3` onto the same
  layer. Its existing tests pass unchanged, including the byte-level
  check that result rows never reach the file or its WAL.

  Start-up now depends on this database and fails closed when it is
  unreachable, consistent with how every other misconfiguration is
  treated here. The error names *which* database is missing, warehouse or
  application, because those have different fixes.

- **Two admin roles.** Operations holds key lifecycle and domain
  knowledge; security holds `denied_columns`, `schema.yaml`, the
  warehouse connection string, and the granting of either role. The rule
  that decides any future case: anything that changes who can see what
  data belongs to security.

  Each escalation path has a test that fails if it opens — issuing a key
  cannot set its ACL, so operations cannot mint itself an unrestricted
  one; operations cannot grant any role, including to itself; every
  security-gated route refuses an operations key, discovered from the
  route table rather than hand-listed; and `AUTH_REQUIRED=false` confers
  neither role.

- **Admin actions are audited to their own stream**, so admin activity
  does not pollute the analytics the analyst audit log exists for. Each
  record names *which* capability authorised it — otherwise one person
  holding both roles makes the separation invisible to whoever reviews it
  later. Each role can read that the other acted without being able to
  act.

### Changed

- **API keys moved into the database, read at call time.** `API_KEYS_JSON`
  was read at start-up, so a disable button would have taken effect at the
  next restart; for a leaked key, "tomorrow morning" is not an answer.

  That plus an external database is a network round trip per request — two
  correct decisions colliding. The key set is cached with a short TTL *and*
  invalidated explicitly on every revocation, disable and ACL change. Both
  halves are tested, because with only one of them either revocation is
  not immediate or every question pays a hop.

  Revocation writes a tombstone rather than deleting the row. Restoring
  the database to an earlier point would otherwise restore a key revoked
  because it leaked — backup as the cause of a security regression rather
  than the cure.

- **Phase 1's "no `/admin` route accepts a mutating method" rule is
  replaced, not deleted.** That rule was only ever true because phase 1
  had no writes; the guarantee underneath it was never "no writes exist"
  but "no write is ungated". The replacement walks each route's real
  FastAPI dependant tree and asserts every mutating route declares one of
  the role dependencies.

### Fixed

- **The application database must never be the warehouse.**
  `docs/db-hardening.md` specifies a read-only login for the warehouse and
  `database/executor.py` rolls back every transaction. This database needs
  writes, so if the two ever point at the same place, our own writes
  become the mechanism that undoes that posture — silently, from inside
  the process. The two URLs are compared as parsed endpoints, not as
  strings: `localhost` and `127.0.0.1` naming one database is the same
  mistake spelled twice.

  Reviewing that check found it compared database *names*
  case-sensitively. Case sensitivity varies by backend and by platform —
  SQL Server folds under most collations, PostgreSQL folds unquoted names,
  MySQL depends on the host filesystem — so there is no answer to inherit.
  The failure directions decide it: a false positive costs an operator a
  rename and a clear start-up error; a false negative silently undoes the
  read-only posture. It now over-matches on purpose.

### Notes

- Exercised against SQLite only. No PostgreSQL or SQL Server was reachable
  in this environment, so those backends are implemented and unverified by
  execution — the same boundary the multi-dialect work was held to.

---

## [4.4.0] — 2026-09-05

### Changed

- **Live mode is now the default.** A deployment is live and analysts open
  the UI expecting real answers; defaulting to the simulated demo meant
  synthetic figures rendered in the same interface as real ones —
  labelled, but the wrong default once the system is actually serving
  people. Simulated stays fully available, and `?live=0` now selects it
  explicitly, which was not previously possible because it was already
  the default.

  The honest consequence: a first load against an unreachable backend now
  shows an error rather than a working demo.

- **The backend URL left the analyst's top bar.** It is deployment
  configuration, and a wrong value there looks exactly like a dead
  backend. `web/js/config.js` now holds the default and is the one file a
  deployment edits; `?base=` and the persisted value still override.

  The API-key field stays. It is the analyst's own identity — the audit
  trail records `principal_id` and the rate-limit bucket is the
  `(principal, ip)` pair — so removing it would force either a shared key
  or a key embedded in static files anyone can read.

### Added

- **Generated SQL is syntax-highlighted**, with Prism vendored locally
  the way the fonts are, because `web/README.md` states a no-CDN
  requirement. The theme is authored against this app's existing custom
  properties rather than copied, since no upstream Prism stylesheet is
  both offline and theme-aware here. Highlighting is presentation only:
  the copy path reads the `Turn` object, never the DOM, so it still
  yields the exact original SQL.

### Fixed

- **The UI rendered numbers in two digit systems at once.** `chart.js`
  declared both an `en-US` formatter and an `fa-IR` one and used them in
  adjacent tiles — the total in Latin digits beside the point count in
  Persian. `llm-status.js` did the same inside one stat grid, and
  `table.js`, `turn.js`, `sessions.js`, `memory.js` and `data.js` each
  made the call again locally. `table.js`'s `fmtCell`, which formats every
  table cell *and* every chart value label, was Latin while the row count
  beside it was Persian.

  That is not one bug — it is the same decision taken eight times,
  differently, which is why the numbers looked unsettled.

  `web/js/num.js` is now the only place that decides. Persian digits for
  everything a reader reads; Latin survives in one deliberate carve-out
  for values that travel to other systems, because converting those makes
  them wrong at the far end.

  The test asserts the invariant rather than the formatting — within one
  rendered view, numeric strings may not use both digit systems — so it
  survives a future change of digit system. It caught `fmtCell`, which
  the first pass missed.

---

## [4.3.0] — 2026-09-05

### Added

- **An admin panel, read-only.** Phase 1 of
  `docs/admin-panel-architecture.md`: an `admin` capability on
  `Principal`, four `GET` routes under `/admin`, and a dashboard at
  `web/admin/`. Nothing in it mutates state — no key issuance, no ACL
  change, no config edit, no cache invalidation.

  Read-only is the point of a first cut. The architecture's §2.1 spends
  its length on escalation paths that exist only because an admin can
  change things; none apply yet. It needs no application database and no
  role bootstrapping beyond one capability, which makes it safe to ship
  during a deployment — which a write path is not.

  `/admin/summary` calls `scripts/analyze_audit_log.py`'s `build_report`
  directly rather than reimplementing any of it, and defaults to the
  aggregate-safe mode with the report's own `mode` field passed through.
  `/admin/health/checks` runs `verify_deployment`'s checks on demand.

  `AUTH_REQUIRED=false` cannot confer the capability: the escape hatch
  resolves to `ANONYMOUS`, which has none, and the dependency checks the
  capability rather than short-circuiting on "auth is off".

### Fixed

- **The auth-coverage test had stopped covering `/v2` entirely.**
  FastAPI 0.141 represents each `include_router()` as one opaque
  `_IncludedRouter` whose `.path` is `None` instead of flattening its
  sub-routes into `app.routes`. Dispatch is unaffected; introspection is
  not.

  `tests/test_auth.py` walked `app.routes` naively, so discovery had
  silently fallen from *every route* to *the eight registered directly on
  `app`* — zero `/v2/*` routes. The test whose entire purpose is "a route
  added without auth fails automatically" had stopped looking at the
  conversational API, and stayed green because its floor was `>= 8` and
  it still found exactly eight.

  A silently-narrowed security test is worse than a missing one: it
  reports coverage it does not have. Discovery now recurses, and the
  guard names the route families that must be covered rather than
  counting, because a count cannot express "and it is still looking at
  the conversational API". Coverage went from 8 routes to 23.

- **Chart labels were drawn outside their own viewBox** — sinking out of
  sight in some places, running past the edge in others. SVG neither
  clips nor reflows text, so a label near a boundary simply disappears
  and nothing errors.

  Three causes: the ranked bar chart reserved a fixed 60px gutter for a
  value label that can be a rial figure in the billions; the line chart
  anchored its end-of-axis labels `middle` at the extreme positions,
  putting half of each outside on both sides, and drew its focus label
  above a point that is often the maximum; and `xAt` built its span from
  one padding while starting from the other.

  Value labels now move inside the bar when the gutter cannot hold them,
  edge labels flip their anchor inward, and long category names truncate
  with the full text kept in a `<title>`. The new test asserts geometry
  rather than appearance, across hostile inputs and every framing.

---

## [4.2.0] — 2026-09-05

### Added

- **Audit records now carry `session_id` and `turn_id`.** Additive and
  optional: `None` on the `/query` and CLI paths, which have no session.

  Without them the audit trail could say nothing at all about the
  conversational product. A follow-up question and a fresh one looked
  identical in the log, and "this answer was wrong" could not be traced
  back to the turn that produced it or to the turns it refined.

  `docs/admin-panel-architecture.md` names their absence as the
  prerequisite blocking the panel's entire first tier. This shipped
  during a deployment rather than after it because the cost is not
  recoverable: a week of production logs written without these is a week
  permanently blind to the dimension the deployment exists to exercise.

  Both are identifiers this system generated, not user content — the same
  category as `request_id`, and they reveal no more about the warehouse
  than it does.

---

## [4.1.5] — 2026-09-05

### Documentation

- **A review pass over `docs/admin-panel-architecture.md` found five gaps
  in the design as written.** All are now in the document; one was a real
  security hole.

  - **`AUTH_REQUIRED=false` must not confer either admin capability.**
    The escape hatch resolves the caller to `ANONYMOUS`, which has no
    capabilities — so the safe behaviour falls out, *provided the
    implementation checks the capability*. The shortcut that suggests
    itself ("auth is off, let everything through") would hand every
    anonymous caller the ability to rewrite the guard's allowlist.
  - **Tier 1 is not only a panel feature.** The flag control and its
    endpoint live in the analyst UI, so it changes the analyst-facing
    product too — and "promote this to a golden case" has no defined
    destination, because the golden set is a file outside the
    application database and outside the versioning scheme.
  - **Stored column names are not re-checked against a changed ACL.**
    Result rows are never persisted precisely because a stored row
    cannot be re-checked; column names *are* persisted and get no such
    treatment. Weaker than exposing values, but inconsistent with the
    principle we applied to rows.
  - **Maintenance mode was leaned on twice and never defined** — and
    migration safety depends on it stopping writes. It must also keep
    the panel reachable, or enabling it is a one-way door.
  - **"Bind the panel to loopback" contradicts "the panel is a client of
    the API".** The privileged routes live on the same application that
    serves analysts, so restricting the static server restricts nothing.
    Three resolutions are named; picking one is a prerequisite.

- Four further open questions recorded rather than left implicit,
  including that failed admin authentication carries no principal and so
  buckets on IP alone.

---

## [4.1.4] — 2026-09-05

### Documentation

- **`docs/admin-panel-architecture.md`** — the agreed design for an admin
  panel, written as a contract in the spirit of `api-contract-v2.md`:
  decisions with their reasons, so an implementation can be checked
  against intent rather than guessed at. Nothing is implemented yet.

  The parts worth knowing before reading it:

  - **Two admin roles, split by one rule** — anything that changes who
    can see what data belongs to the security admin, everything else to
    operations. Four escalation paths that would have collapsed that
    split are closed explicitly, including two non-obvious ones:
    `schema.yaml` is a data-access change wearing operational clothes
    (it is the guard's allowlist), and `DB_CONNECTION_URL` could be
    pointed at a more privileged login on the same database.
  - **The split holds because enforcement lives in the guard, not the
    prompt.** That is why editing domain knowledge is safely operational
    — a crafted few-shot example still cannot reach a denied column — and
    why `schema.yaml` is the exception.
  - **Three kinds of data, three homes.** Metadata to a managed database
    (durability and stewardship), the audit log to append-only files
    (tamper-evidence — a DBA must not be able to edit it), secrets to the
    environment.
  - **Configuration versioning is a table, not git**, with revert
    semantics, bundle versions plus per-file restore, and validated
    rollback. Git remains as an *output*: the YAML bundle is exported on
    every version, so an operator's own repository still gives offline
    inspection and off-box backup.
  - **Migration between backends is a shipped tool**, not a property of
    using SQLAlchemy. Verified by row counts and content hashes, never
    mutating the source, requiring maintenance mode, refusing a
    schema-version mismatch.
  - Four prerequisites in today's code are named: `AuditRecord` has no
    `session_id`, `Principal` has no role concept, `API_KEYS_JSON` is
    read at start-up so revocation cannot be immediate, and
    `session/persistence.py` is hardcoded to `sqlite3`.

---

## [4.1.3] — 2026-09-05

### Documentation

- **The setup guide listed `OPENAI_API_KEY` among four values to fill
  in, which reads as required. It is not.** `Settings.validate()` demands
  only `OPENAI_MODEL` and `DB_CONNECTION_URL`, and `.env.example` has
  always shipped the LLM key empty — because most local
  OpenAI-compatible servers (LM Studio, Ollama, llama.cpp) check no
  credentials at all.

  So on a local-model deployment there is exactly **one** real token in
  the whole system: the analyst's key. The three-row table in the guide
  shows three *places*, not three secrets.

  Saying otherwise sent people looking for a credential their model
  server never asked for, and invited the genuinely dangerous shortcut of
  reusing the analyst key for it. The guide now states, explicitly, that
  the two must never share a value: they point in opposite directions —
  `OPENAI_API_KEY` is what this server presents *outward* to the model,
  the analyst key is what a browser presents *inward* to this server —
  and many model servers log the `Authorization` header they receive.

- `tests/test_local_model_needs_no_llm_key.py` pins the claim rather than
  leaving it to drift. Three things in three different modules have to
  hold for that advice to be true: `validate()` must not demand the key,
  the provider must omit the header when it is empty, and the health
  probe must do the same. The last of those was false until 4.1.2.

---

## [4.1.2] — 2026-09-05

### Fixed

- **The LLM health light showed red while the CLI answered questions
  through the same endpoint.** The probe disagreed with the engine in two
  ways, and either alone was enough.

  It **sent an `Authorization` header even with no key configured**,
  building `Bearer ` with nothing after it. Many self-hosted
  OpenAI-compatible servers check no credentials, so an empty
  `OPENAI_API_KEY` is legitimate — and a malformed header is not the same
  as an absent one: plenty of servers answer the first with 401 and the
  second with 200. `llm.providers.OpenAIBackend` omits the header
  entirely when the key is empty, which is exactly why the engine
  succeeded where the probe failed. Both now use the same rule.

  It also **judged an endpoint the engine never calls**. Generation goes
  to `/chat/completions`; the probe asks `/models`, because a liveness
  check must be cheap and must not spend tokens. That trade is fine, but
  it means a 404 says only that this server lacks an endpoint we do not
  use — not a fault, and no longer reported as one. A 401 or 403 is the
  opposite and stays a failure, because the endpoint is reachable and has
  actively refused our credentials.

- **A red light now says why.** `GET /health` gained `openai_detail` and
  `database_detail` (additive, optional), and the UI shows them in the
  indicator's tooltip. "Unreachable host", "wrong key" and "this server
  does not implement `/models`" were three different problems with three
  different fixes and one indistinguishable symptom.

### Documentation

- `docs/fa/getting-started.md` explains why the setup asks for something
  token-shaped in three places, which is a fair thing to find confusing:
  they are **two** secrets, not one entered three times. `OPENAI_API_KEY`
  is the credential for the *model endpoint* and is unrelated to the
  other two; the analyst's key exists as a hash on the server and as the
  raw value in the browser, which is how every password system works —
  storing the raw key would let anyone who reads `.env` impersonate any
  analyst and make the audit trail's `principal_id` worthless.

  It also points at `AUTH_REQUIRED=false`, which removes the analyst key
  entirely for a single-user local run, and says plainly what that costs:
  the audit log can no longer say who asked what, and rate limiting sees
  everyone as one caller.

---

## [4.1.1] — 2026-09-05

### Fixed

- **The server had no route at `/`,** so the first thing anyone does
  after starting it — open `http://localhost:8000` in a browser —
  returned `{"detail":"Not Found"}`. With `APP_DOCS_PUBLIC=false` (the
  default) `/docs` is behind authentication too, so a correctly running,
  correctly configured server looked completely dead to the one check a
  person actually performs. That cost a real deployment an investigation
  into a server that was working perfectly.

  `GET /` now answers, unauthenticated, with the service identity and a
  directory of paths — and, most importantly, says that **the browser UI
  is a separate static server on a different port**. That single
  confusion is what every wrong turn on a first run traces back to.

  It is deliberately boring: no model name (the disclosure `/health`
  already withholds from anonymous callers), no connection string, no
  configuration, no counts. `tests/test_auth.py` pins both halves — open
  without credentials, and leaking nothing — and the route-coverage test
  that enumerates the live route table now carries `/` in an explicit
  `_OPEN_ROUTES` set, so opening a route stays a recorded decision rather
  than something that happens by omission.

- **The version the API reported was `1.0.0`** while the project was at
  4.1.0. Harmless only while nothing read it; `GET /` publishes it. There
  is now one source (`core/version.py`) and a test that reads the newest
  `## [x.y.z]` heading out of this file and fails if the two disagree —
  the failure being prevented is exactly "two places, one updated".

- **Two SQLite sidecar files were committed.** `.gitignore` had `*.db`,
  which matches `sessions.db` but **not** `sessions.db-wal` or
  `sessions.db-shm` — and the write-ahead log is where recently-written
  pages live, so on a deployment that has served real questions it is the
  file most likely to hold questions and generated SQL not yet
  checkpointed into the main database.

  The two files that reached the repository contained only the empty
  schema — no session ids, no turns, no SQL — because the session store
  was disabled in every run that produced them. The gap that let them in
  was real regardless, and would have mattered on a machine that had
  served a week of traffic.

  `.gitignore` now covers the sidecars, and
  `tests/test_no_runtime_artifacts_tracked.py` fails the build both if
  such a file is tracked and if the ignore rules stop covering it. The
  warning explaining this hazard was already in `.gitignore`, written for
  the rotated audit logs; it did not stop the same mistake one line below
  it, because a comment cannot fail a build.

### Documentation

- `docs/fa/getting-started.md` said "two terminals" but let a reader
  treat the second as optional. It now states which port the UI is on,
  that the backend's own port shows nothing in a browser, and that
  terminal 2 is required — with the health check shown through `curl`
  rather than a browser.

---

## [4.1.0] — 2026-09-05

Many conversations instead of one, and preferences that outlive a
session.

### Added

- **A conversation index that survives a restart.** `GET /v2/sessions`
  lists the caller's conversations with titles, recency and turn counts;
  `PATCH /v2/sessions/{sid}` renames one. Sessions, turns and the
  refinement sidecar persist to SQLite (`session_store_path`, default
  `logs/sessions.db`) for `session_retention_days`.

  This separated three lifetimes that had been two: the prompt window
  (`session_prompt_turns`), the live in-memory context
  (`session_ttl_seconds`), and retention. TTL expiry now **demotes** a
  session out of memory instead of deleting it — previously a
  conversation vanished after thirty idle minutes, which is why there was
  nothing worth listing.

- **Cross-session memory.** `GET/PUT/DELETE /v2/memory` — standing
  preferences from a closed set declared in
  `project_config/memory_policy.yaml`. `docs/api-contract-v2.md` §10 had
  deferred this explicitly ("needs authentication first; building it
  before authz means building it wrong"); Phase 8 landed authentication,
  so §10 is now amended rather than deleted, saying what unblocked it and
  what stays out of scope.

  Memory is **explicit only** — an entry exists because the analyst
  pinned it, nothing is inferred from repetition. It surfaces through the
  existing contract rather than as a hidden channel: `Assumption.source`
  gains `"memory"`, editable, so the chips show it and
  `PATCH …/assumptions` overrides it for one turn without changing what
  is stored. Precedence is `question > session > memory > default`;
  `policy` is overridable by none of them.

- **`TurnResult.rows_omitted`** — additive, defaults false. Set on a turn
  rehydrated from disk, so a client can never render a restored turn as
  an empty result: `row_count` stays accurate and this says why the rows
  are absent.

- The web UI gained a conversation sidebar (switch, rename, delete, and
  the last session restored on reload), a memory panel, and a pin control
  on editable assumption chips — the only way memory is created.

- `scripts/verify_deployment.py` checks the session store is writable,
  beside the audit-log check it already performed.

### Security

- **Result rows are never written to disk.** Persistence stores the
  question, the SQL, result **column names**, `row_count` and the
  refinement sidecar. Not the rows. Beyond keeping warehouse data out of
  a file outside the DBA's control, the decisive reason is that a
  persisted row cannot be re-checked against a changed ACL: a principal's
  `denied_columns` can gain a column after the row was written, and no
  guard work at query time catches that, because no query runs. Verified
  at the byte level in both the SQLite file and its WAL.

- **Memory is re-checked against the ACL at read time**, not only when
  written. An entry naming a column the principal may no longer see is
  dropped for that turn and reported in `Turn.warnings` — not applied,
  and not silently ignored either.

- **The query cache partitions on memory that actually applied.**
  Memory-derived filters change the answer, so `scope_key` includes them
  — but only the entries that influenced *this* query. Hashing the whole
  memory set would partition the cache per principal and discard
  cross-user sharing for anyone who ever pinned anything.

### Fixed

- **The shutdown segfault, properly this time.** 4.0.1 made abandoned
  workers daemons, which stops `concurrent.futures` from joining them
  during finalisation. That was real but insufficient: a daemon still
  *running* when finalisation begins is killed at its next GIL
  acquisition, and if that lands inside a C extension call the process
  dies. Being a daemon means the interpreter is allowed to cut the thread
  off; it was never a claim that doing so is safe. Background threads are
  now drained at test-session end, while the interpreter is healthy and a
  join is an ordinary operation. The `PYTHONFAULTHANDLER` and
  thread-reporting added in 4.0.1 are what identified the second source
  by name.

- **Pinning used a chip's display label as the memory key.** Those are
  different things — the label is what the chip shows, the key is a
  config identifier — so every pin would have failed against the real
  backend. The test that covered it picked a fixture where the two
  strings coincided and then asserted against its own input.

- The README described the project as it was at 3.2.0, and printed the
  real warehouse schema in every example. Examples are now a neutral
  illustration; the quick start now mentions `project_config/`, without
  which the server does not start.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — session
  persistence, conversation index, cross-session memory, web UI, and the
  shutdown fixes

---

## [4.0.1] — 2026-09-04

### Fixed

- **A segmentation fault during interpreter shutdown, reported as a bare
  exit code 139 underneath a green test summary.** `resolve_value`'s
  deadline is deliberately soft — on a breach the caller stops waiting
  and the query keeps running with its result discarded — but the query
  ran in a module-level `ThreadPoolExecutor`, whose worker threads are
  **not** daemons. `concurrent.futures` registers an `atexit` hook that
  joins every one of them, so each breached deadline left a thread
  guaranteed to be joined during interpreter finalisation, running
  caller-supplied code against a process whose module globals were
  already being torn down.

  The work now runs in a daemon thread — the same conclusion
  `retrieval/dimension_vocabulary.py` had already reached for its own
  background refresh, and whose comment argues against exactly the pool
  this removes. An abandoned daemon is cut off at exit rather than
  joined.

  Python 3.12 was not special as an interpreter: it is the only version
  whose CI workflow runs the whole suite a second time for coverage, so
  it had two chances per run at the race.

### Added

- `resolve_value_max_concurrency` (`RESOLVE_VALUE_MAX_CONCURRENCY`,
  default 8) — the in-flight bound the removed pool provided as
  `max_workers`, now read at call time. Waiting for a free slot spends
  the caller's own `resolve_value_timeout_seconds`: a saturated resolver
  reports a miss on time rather than queuing past its deadline.
- `PYTHONFAULTHANDLER=1` in CI, so a finalisation crash dumps the
  faulting stack instead of a bare exit code.
- `pytest_sessionfinish` names every thread still alive when a test run
  ends and flags any that is not a daemon. It does not fail the run —
  lingering daemons here are deliberate — but the next crash of this
  shape starts from a thread name instead of a guess.

---

## [4.0.0] — 2026-09-04

Everything between 3.2.0 and here: authentication, the conversational
session API, the evaluation harness, LLM observability, the separation of
domain data from engine code, multi-dialect support, and two web clients.
Two of those changes are breaking.

### Breaking

- **Every route except `/health` now requires authentication.** Requests
  must carry `Authorization: Bearer <key>`. Keys are hashed with SHA-256
  (deliberately not bcrypt or argon2 — these are high-entropy tokens, not
  passwords) and compared with `hmac.compare_digest`. Startup fails
  closed if `AUTH_REQUIRED` is on and no key is configured. Issue keys
  with `python -m scripts.issue_api_key`.

- **Domain data no longer lives in the repository.** `project_config/` is
  gitignored and mandatory: `aliases.yaml`, `business_rules.yaml`,
  `entities.yaml`, `examples.yaml`, `metrics.yaml`, `schema.yaml`,
  `retrieval_hints.yaml`, `session_policy.yaml`. A missing file raises
  `ConfigNotFoundError` at start-up. There is deliberately **no** silent
  fallback to `project_config.example/` — running a real warehouse on
  sample business rules would produce confidently wrong SQL, which is
  worse than refusing to start. An existing deployment will not boot
  until those files are placed by hand.

- `/health` no longer reports the model name to unauthenticated callers.

### Added

- **Conversational sessions (`/v2/sessions…`)** — the `Turn` contract in
  `docs/api-contract-v2.md`, SSE streaming, declared assumptions with a
  source and an editability flag, `PATCH …/assumptions` to re-run a turn
  under edited assumptions, and CTE-composed refinement so «از بین
  آن‌ها» resolves against the previous turn rather than re-querying the
  warehouse.
- **Evaluation harness (`eval/`)** — golden set, execution accuracy,
  per-tag breakdown, error taxonomy, latency percentiles, an
  order-insensitive result fingerprint so "the same answer" is decidable,
  determinism measurement against a live endpoint, and a baseline
  regression gate with a CI exit code.
- **LLM status block** — 21 fields on every response and audit record:
  token counts, prefix-cache-hit ratio, timings, correction count,
  `finish_reason` read from the response rather than assumed, and
  detection of a model answering on its reasoning channel.
- **Multi-dialect support** — `SQL_DIALECT` selects the target; the model
  still generates T-SQL, which is transpiled and then **re-validated by
  the guard in the dialect it will actually execute in**. `tsql` and
  `sqlite` are verified by end-to-end execution; `postgres` and `mysql`
  transpile and re-validate cleanly but are unverified by execution and
  are not claimed. Per-dialect data lives in a `DialectProfile` registry
  (`security/dialects.py`), not in branches.
- **Static web client (`web/`)** — Persian, RTL, no build step: per-turn
  pipeline view, assumption chips, LLM status panel, result-shape
  selection between chart and table driven by the declared column types,
  chart defaults following Storytelling with Data, and export.
- **Flask web app (`webapp/`)** — bilingual FA/EN, sample-question panel,
  SQL beautifier, result pagination, copy and download.
- **Value resolution from the warehouse** (`retrieval/value_resolver.py`)
  with a stale-while-revalidate prefetch and single-flight refresh,
  replacing per-request lookups.
- **Guard rejection taxonomy** — `CorrectableRejection` versus
  `PolicyRejection`, with refusal tracked as an independent axis, so the
  model is no longer re-prompted for rejections no rewrite could satisfy.
- **One canonical Persian normalizer** (`core/persian.py`), versioned, so
  the cache and the retriever agree on what the same question is.
- **Observability** (`observability/`) — compliance-grade audit records
  that never contain result rows, stage timings, and the status block
  above.
- Operator tooling: `scripts/verify_deployment.py`,
  `scripts/issue_api_key.py`, `scripts/analyze_audit_log.py`.
- Documentation: `docs/api-contract-v2.md`,
  `docs/deployment-runbook.md`, `docs/db-hardening.md`, and bilingual
  tutorials under `docs/fa/` and `docs/en/`.
- Licence provenance notices (`core/provenance.py`, `NOTICE`) — stated
  where the licence is actually encountered rather than only in a file
  nobody opens.
- `tests/test_no_domain_literals.py` — walks the AST of first-party
  source and fails if a warehouse name reappears in an executable
  literal. This is what keeps the separation above from decaying.
- `project_config.example/` and `eval_data.example/` templates, so the
  suite and CI run with no real data present.
- CI across Python 3.11, 3.12 and 3.13, with doctests, coverage and an
  offline evaluation gate; branch protection on `main`.

### Changed

- **Prompt assembly is split into a byte-identical static prefix and a
  variable suffix**, so a local endpoint can reuse the prefix's KV cache.
  Per-turn content — session context, resolved filters, correction text —
  goes only in the suffix. This is enforced by tests, because breaking it
  is silent: the suite stays green and every request pays full prefill.
- All domain knowledge moved from Python modules to YAML loaded through
  `knowledge/config_loader.py`, and the guard's table and column
  allowlists are now derived from `schema.yaml` rather than hardcoded.
- `ensure_top` keeps its byte-identical text-splicing path for T-SQL and
  uses an AST row cap for the other dialects.
- The LLM layer reduced to a single OpenAI-compatible provider
  (`llm/providers.py`) behind a router with fallback and bounded retries.
- Rate limits raised from 60 to 600 requests per window and moved into
  `Settings`.
- The suite is now 2,079 tests, up from the 427 the README badge
  advertised at 3.2.0.

### Fixed

- **The rate-limit bucket keyed on the principal alone, then on the IP
  alone.** Either half by itself collapses distinct callers into one
  bucket — every analyst behind one UI host, or every request from one
  key. It is now the `(principal, ip)` pair.
- **The web UI sent no `Authorization` header**, so every route but
  `/health` returned 401 and the only thing the UI could reach was the
  liveness probe. The suite was green throughout because all of it was
  server-side.
- **`denied_columns` reached the query cache's scope key but never
  `validate_sql`** — the column ACL partitioned the cache without
  enforcing anything. It is now threaded through both the `/query` and
  the `/v2/…/turns` generation paths.
- **T-SQL `N'…'` literals and `'a' + 'b'` concatenation transpiled
  completely unchanged.** SQLite rejects the first as a syntax error and
  silently evaluates the second to `0` — an error and a plausible wrong
  number respectively. Both are now refused for non-T-SQL targets.
- Persian presentation forms and Arabic/Persian letter variants made the
  cache and the retriever disagree about the same question.

### Security

- Per-principal column ACL, enforced in the guard and not merely
  partitioned around in the cache.
- Audit records carry `principal_id` and never carry result rows.
- The guard parses to an AST and works from a closed allowlist; the
  bypass suite is parametrised over every claimed dialect, because a
  guard proven for one dialect and assumed for another has unknown holes.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — engine,
  security layer, conversational sessions, evaluation harness,
  observability, multi-dialect support, static web client, domain
  separation
- [**Melika Bahmanabadi**](https://github.com/MelikaBahmanabadi) — Flask
  web application, bilingual FA/EN system, UI work, schema knowledge

---

## [3.2.0] — 2026-06-12

License, documentation, and attribution release.

### Added

- **`LICENSE`** — Project is now licensed under the **Business Source License 1.1 (BUSL-1.1)**.
  - Free for non-production use.
  - Commercial/production use requires a written agreement with the author (Ali Sadeghi Aghili).
  - Converts automatically to Apache 2.0 on **2029-01-01**.
  - Includes an explicit **Attribution Requirement**: any derivative work must retain
    `LICENSE` and display: *"Based on Local SQL Agent by Ali Sadeghi Aghili —
    https://github.com/alisadeghiaghili/local-sql-agent"*.

- **`docs/tutorial.md`** — Comprehensive Persian-language tutorial covering:
  architecture overview, installation, first query end-to-end, TF-IDF retrieval
  internals, prompt building, SQL security pipeline, adding new tables/synonyms,
  miss-analysis workflow, test writing examples, health check, and troubleshooting.

- **`README.md`** — Updated to reflect BUSL-1.1 license, added license badge,
  attribution notice, contributors table, and link to `docs/tutorial.md`.
  `docs/tutorial.md` added to architecture tree.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — Creator & Lead Maintainer

---

## [3.1.0] — 2026-06-12

Minor release — FastAPI HTTP layer, SQLAgent auto-correct loop, LRU query cache,
tested LLM backend abstraction, and 1 bugfix in cache isolation.

### Added

- **`api/`** — FastAPI HTTP service package:
  - `server.py` — FastAPI app factory with `/query`, `/health`, `/cache/stats`,
    `/cache/invalidate`, `/cache/clear` endpoints. `_system_prompt` module-level
    variable allows test-time injection without environment changes.
  - `runner.py` — `run_query()`: cache-aware orchestrator that consults
    `query_cache` before calling `_agent.run`; populates cache on miss.
  - `query_cache.py` — `QueryCache`: thread-safe TTL + LRU in-process cache;
    `set / get / invalidate / clear / stats / reconfigure` API.
    `reconfigure()` now clears the store when TTL or max-size changes.
  - `models.py` — Pydantic `QueryRequest` / `QueryResponse` request/response
    models; `mode` field validates `"full" | "sql" | "result"`.
  - `errors.py` — `NLQError` hierarchy: `OutOfScopeError` (422),
    `ModelTimeoutError` (504), `ModelUnavailableError` (503),
    `QueryExecutionError` (500); FastAPI exception handlers registered
    for each type.
  - `middleware.py` — `RequestLoggingMiddleware`: per-request correlation ID
    (`X-Request-Id`), latency header (`X-Response-Time-Ms`), structured log.
  - `health.py` — `/health` endpoint: probes SQL Server connectivity and
    Ollama reachability; returns per-component status dict.

- **`llm/base.py`** — `LLMBackend` abstract base class + `SQLGenerationResult`
  dataclass (`sql`, `raw_response`, `attempt`, `correction_prompts`).

- **`llm/sql_agent.py`** — `SQLAgent`: wraps any `LLMBackend`; runs the
  generate → `clean_sql` → `validate_sql` loop with up to N correction
  attempts, feeding validation errors back into the prompt.

- **`llm/ollama_backend.py`** — `OllamaBackend(LLMBackend)`: HTTP client
  extracted from the old monolithic `ollama_client.py`; exponential back-off
  retry, `OUT_OF_SCOPE` sentinel detection, `ModelUnavailableError` mapping.

- **`config.py`** — `override_settings()` context manager added for
  test-time settings mutation without environment side-effects.

- **Test files added:**
  `test_api_endpoints.py`, `test_api_runner.py`, `test_cache_endpoints.py`,
  `test_errors.py`, `test_middleware.py`, `test_ollama_backend.py`,
  `test_query_cache.py`, `test_runner_cache.py`, `test_sql_agent.py`.
  Total test count: **427+** (up from 231).

### Fixed

- **`api/query_cache.py` — `reconfigure()` did not clear stale entries.**
  After calling `reconfigure(ttl_seconds=60)`, entries stored under the
  previous TTL could survive and be returned as valid hits.
  Fix: `reconfigure()` now calls `self._store.clear()` after updating
  `_ttl` and `_max_size`.

- **`tests/test_api_runner.py` — cache pollution between tests.**
  Added `autouse` fixture that calls `query_cache.clear()` before and after
  each test, preventing cache hits from earlier tests masking failures in
  later ones (e.g. `test_exception_translated` receiving a cached success).

- **`tests/test_query_cache.py` — `TestQueryCacheRunnerIntegration.test_second_call_hits_cache`
  triggered real Ollama connection.**
  The test called `reconfigure()` inside `override_settings()`, which (after
  the fix above) cleared the pre-populated cache entry. `run_query` then fell
  through to the real `OllamaBackend` → `ModelUnavailableError`.
  Fix: removed `reconfigure()` / `override_settings()` from the test; the
  cache is pre-populated with `query_cache.set()`, `_agent` is patched with
  a `MagicMock` that raises on `.run()`, and the returned object is asserted
  to be the exact cached instance. Added companion test
  `test_cache_miss_calls_agent_and_stores_result` for the miss path.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — FastAPI layer, SQLAgent, OllamaBackend, QueryCache, middleware, error taxonomy, test suite expansion

---

## [3.0.1] — 2026-06-11

Bugfix release — 12 failing tests resolved across three independent areas.

### Fixed

- **`schema_data/registry.py`** — `SchemaRegistry.build_context()` alias added;
  `None` and empty-tuple arguments now treated identically to "include all tables".
  Resolves `AttributeError: type object 'SchemaRegistry' has no attribute 'build_context'`
  (7 tests in `test_schema_registry.py`).

- **`schema_data/retriever.py`** — `_IdfDict.get()` now overrides `dict.get` to
  return `_max_idf` for unseen terms instead of the caller-supplied default.
  `dict.__missing__` is only invoked on `[]` access, not `.get()`, so the earlier
  implementation silently returned `0` for any token absent from the corpus.
  Also added `fallback: bool = True` parameter to `retrieve_tables()`; when
  `False`, an empty list is returned instead of the full table list when no
  table scores above `_MIN_SCORE`. Used by `analyse()` to avoid false negatives.
  Resolves `test_rare_term_has_higher_idf` and `test_detects_miss_when_table_not_retrieved`.

- **`schema_data/tables.py`** — Persian translations appended to every description
  so that common Persian terms (e.g. `معامله`, `مشتری`, `عرضه`) appear in the
  IDF corpus as seen terms with a finite IDF, while truly unseen terms receive
  the strictly higher `_max_idf`. This is required for `test_rare_term_has_higher_idf`
  to be meaningful.

- **`scripts/analyze_misses.py`** — `_KNOWN_TOKENS` now built from three sources:
  `TABLE_DESCRIPTIONS` values, `SYNONYMS` keys, **and** `SYNONYMS` values.
  Previously only descriptions were scanned; synonym expansion terms such as
  `مشتری` were therefore not recognised as known and appeared in the candidate
  list. Resolves `test_filters_existing_description_tokens`.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — retrieval bugfixes, IDF override, schema registry alias, miss-analysis token scan

---

## [3.0.0] — 2026-06-11

Full architectural consolidation. All feature branches merged into `main`.
Legacy `schema/` package retired. Modular retrieval pipeline fully activated.

### Added

- **`core/models.py`** — `RetrievalContext` frozen dataclass: single shared contract
  between the retrieval layer and `PromptBuilder`. Fields: `entities`, `facts`,
  `dimensions`, `relationships`, `business_rules`, `examples`, `filters`.
  Convenience properties: `selected_tables` (order-preserving dedup) and `is_empty()`.
- **`core/__init__.py`** — package marker.
- **`knowledge/aliases.py`** — `SYNONYMS` dict (156 entries, 10 categories: temporal,
  trade, customer, offer, ring/hall, commodity, broker, logistics, finance,
  aggregation) added alongside the existing `RING_ALIASES`. Resolves
  `ImportError: cannot import name 'SYNONYMS'` that blocked all 5 test files.
- **`schema_data/retriever.py`** — TF-IDF bigram engine migrated from the retired
  `schema/retriever.py`; now the canonical fallback for `EntityRetriever` and
  `FactRetriever`. Unchanged logic, corrected import path.
- **`knowledge/`** — full knowledge base promoted from `develop` branch:
  `entities.py` (entity catalog), `examples.py` (20+ tagged few-shot SQL examples),
  `metrics.py` (metric definitions), `business_rules.py` (expanded, topic-keyed rules).
- **`retrieval/`** — modular retrieval pipeline promoted from `develop` branch:
  `EntityRetriever`, `FactRetriever`, `RelationshipRetriever`, `RuleRetriever`,
  `ExampleRetriever` (tag-overlap scoring, 20+ bilingual tags), `ValueRetriever`
  (ring canonical lookup + Persian year regex).
- **`schema_data/`** — schema package promoted from `develop` branch:
  `registry.py` (LRU-cached `build_schema_context`), `columns.py`, `tables.py`,
  `relationships.py` (FK edge → JOIN SQL map).
- **`prompt_engine/`** — `PromptBuilder` and `PROMPT_TEMPLATE` promoted from
  `develop` branch; replaces inline string construction in `ollama_client.py`.

### Changed

- **`retrieval/context_retriever.py`** — `from core.models import RetrievalContext`
  now resolves correctly. `selected_tables` uses `dict.fromkeys()` for
  order-preserving deduplication (replaces non-deterministic `list(set())`).
- **`llm/ollama_client.py`** — pipeline comment updated to reflect final
  module paths (`retrieval.context_retriever`, `prompt_engine.builder`).
- **`tests/test_retriever.py`** — import updated to `schema_data.retriever`.
- **`tests/test_schema_registry.py`** — import updated to `schema_data.registry`.
- **`scripts/analyze_misses.py`** — import updated to `schema_data.retriever`.

### Removed

- **`schema/`** package fully retired (8 files deleted):
  `retriever.py`, `schema_registry.py`, `synonyms.py`, `tables.py`,
  `table_schemas.py`, `relationships.py`, `business_rules.py`, `__init__.py`.
  All functionality migrated to `schema_data/` and `knowledge/`.
- **`develop` branch** — merged and deleted.
- **`prompt-accurated` branch** — ancestor of `main`, no unique changes; deleted.

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — full architectural consolidation, modular retrieval pipeline, knowledge base, schema migration

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
- `schema_data/retriever.py`: bigram scoring (bigram match counts ×1.5); `_MIN_SCORE` threshold; `_ALWAYS_INCLUDE` forced-table logic
- `schema_data/registry.py`: `lru_cache(maxsize=64)` on `build_schema_context()`; accepts `tuple` for cache-safe API
- `app.py`: structured REPL with emoji indicators; separates `RuntimeError` from `ValueError`; logs elapsed time; prints row-count summary; graceful `KeyboardInterrupt` / `EOFError` shutdown
- `tests/test_config.py`: rewritten for new `Settings` dataclass
- `tests/test_sql_guard.py`: **new** — full coverage of `clean_sql`, `validate_sql`, `ensure_top`

### Changed
- `config.py`: flat module-level constants replaced by `Settings` dataclass + `settings` singleton
- All modules now import `from config import settings` (single import point)
- `schema_data/registry.py`: `build_schema_context` takes `tuple[str, …]` for LRU-cache compatibility

### Removed
- `tests/test_validator.py`: replaced by `tests/test_sql_guard.py`

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — config refactor, SQL guard, exporter improvements, REPL, test coverage

---

## [1.1.0] — 2026-06-06

### Added
- Modular NLQ engine: `database/`, `exporters/`, `llm/`, `logs/`, `prompts/`, `security/`
- `app.py` entry point replacing `main.py`
- `config.py` with env-var-only configuration (no hardcoded credentials)
- `security/sql_guard.py`: read-only SQL validator
- Initial `schema/`: table registry, column schemas, FK relationships, business rules, TF-IDF keyword retriever
- `prompts/`: system prompt, business glossary, few-shot examples
- `.env.example` with all required variables documented
- `README.md` added

### Removed
- Legacy root scripts: `main.py`, `nlq.py`, `langsql.py`, `langchain_sql.py`, `nlq_with_sqlite.py`, `prompt_based_nlq.py`, `simple_nlq.py`, `create_db.py`
- Legacy folders: `agents/`, `src/`
- Old config files: `CHANGELOG` (plain text), `pyproject.toml`, `ruff.toml`

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — initial modular architecture, CLI, security layer, schema package

---

## [1.0.0] — initial

### Added
- Proof-of-concept scripts for NLQ-to-SQL via Ollama + LangChain
- `scripts/create_db.py`: SQLite sample database for local testing
- Initial `agents/`, `runners/`, `tests/` structure

### Contributors

- [**Ali Sadeghi Aghili**](https://github.com/alisadeghiaghili) — proof-of-concept, initial structure
