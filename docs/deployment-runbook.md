# Deployment runbook — first production week

Status: **operational checklist, not a design document**. Read this the day
you deploy, and again the day the pilot week ends. Every command assumes
you are in the repo root, on the machine that will actually run the server
(the same one whose `.env` / real environment variables the server reads).

Why this exists: this deployment's first week of real use produces
`logs/audit_log.jsonl` — the only source this project has ever had for real
accuracy and latency numbers. If the deployment stumbles, or the log is
lost, that week (and the users' confidence) does not come back. Everything
below is ordered so each step is verified before the next depends on it.

Where to start: a first deployment, on one database or several, follows
"First install, in order" just below: one list from an empty machine to a
verified, running server, with the command for each step and how to tell it
worked. The numbered sections hold the detail it links to. A deployment that
queries more than one database also follows §16, in the order it gives. An
installation already running 6.0 that is moving to 6.7 follows §17, which
sequences the upgrade notes of every release in between into one checklist.
Before sending logs or configuration to anyone for help, read §19.

### First install, in order

Do these in order; each one assumes the ones before it. A step marked
*several databases only* is skipped on a one-database deployment. "Done when"
is the sign you can move on: do not continue past a step that did not show it.

1. **Check the prerequisites.** Python 3.11 or newer; an ODBC driver for SQL
   Server (17 or 18) on the machine that runs the server; an
   OpenAI-compatible model endpoint it can reach; a read-only login on every
   database (`docs/db-hardening.md`, which the DBA applies).
   *Done when:* `python --version` shows 3.11 or newer and, once step 2 is
   done, `python -c "import pyodbc; print(pyodbc.drivers())"` lists
   `ODBC Driver 18 for SQL Server` or `ODBC Driver 17 for SQL Server`.
2. **Install the dependencies**, in a virtual environment, from the
   repository root: `pip install -r requirements.lock` (next section).
   *Done when:* pip ends without an error.
3. **Create `.env`**: `cp .env.example .env`, then set `DB_CONNECTION_URL`
   and `DB_PASSWORD` (with several databases, one `DB_PASSWORD_*` per source
   in step 5 instead), `OPENAI_BASE_URL` and `OPENAI_MODEL`, and
   `OPENAI_API_KEY` only if your endpoint checks one (§2).
   *Done when:* `python -c "import config as cfg; cfg.settings.validate(); print('settings ok')"`
   prints `settings ok`. Anything else is a `ValueError` whose message names
   the variable to fix.
4. **Create `project_config/`**: `cp -r project_config.example project_config`,
   then replace the example's content with your own domain. Two optional
   helpers draft it from the live database: `python -m database.schema_inspector_cli`
   drafts `schema.yaml`, and the setup wizard `python setup_project.py` drafts
   `entities.yaml`, `aliases.yaml`, `business_rules.yaml` and `examples.yaml`
   (§2.1 and §2.2; the wizard does not write `schema.yaml` and does not set
   `datasource:`). *Done when:* `project_config/` holds the nine YAML files
   and `system_prompt.md`, `schema.yaml` lists your tables, and, if you ran
   the wizard, its last line reads `Setup complete.` followed by the counts.
5. **Describe the databases** (*several databases only*): write
   `project_config/datasources.yaml` from `project_config.example/datasources.example.yaml`
   (§16.1, §16.2) and put each source's raw password in `.env` under the
   variable its `password_env` names.
   *Done when:* `python -c "from database.datasources import datasource_names; print(datasource_names())"`
   prints every source name, and the check of step 3 still prints `settings ok`.
6. **Work out each table's `datasource:`** (*several databases only*):
   `python scripts/assign_datasources.py`. Skipping it makes the preflight in
   step 11 fail with `Tables are in their data source` as soon as a table
   lives outside the default source (§3.1).
   *Done when:* the run ends with `written: ... -- review it before replacing schema.yaml`
   (§16.3).
7. **Review the proposal, replace `schema.yaml`, check** (*several databases
   only*): read `project_config/schema.with_datasources.yaml`, copy
   `schema.yaml` to `schema.yaml.bak`, move the proposal over `schema.yaml`,
   then run `python scripts/assign_datasources.py --check`.
   *Done when:* it prints `CHECK OK: every table's datasource: matches the databases`
   (exit code 0).
8. **Fix the columns the report lists as missing.** The report's last section
   is `== columns listed in schema.yaml that the database does not have: N ==`,
   one `Table [source]: Column, ...` row each. Remove a column the database
   does not have from `schema.yaml`, correct a misspelt one, or have the DBA
   lift a `DENY` that hides it (§16.3). `--check` does not fail on these, so
   read the section. A one-database deployment can run
   `python scripts/assign_datasources.py` for the same report; it writes
   nothing there.
   *Done when:* that section reads `: 0 ==`.
9. **Set what each source needs** (*several databases only*): `description:`
   and `keywords:` so questions reach the right source (§16.4), and
   `nolock: true` on exactly the sources whose DBA requires `WITH (NOLOCK)`;
   it is a per-source switch (§16.7). *Done when:* the command of step 5
   still prints the names.
10. **Issue the first API keys** (§1, §1.1):
    `python -m scripts.issue_api_key --id admin-1 --name "Admin" --full-admin`,
    add the printed entry to `project_config/api_keys.json` (start from
    `project_config.example/api_keys.example.json`) and set
    `API_KEYS_FILE=project_config/api_keys.json` in `.env`. This comes before
    the preflight because its `API key authentication` check fails while no
    key exists. *Done when:* you hold the raw key (it is printed once) and
    its entry is in the file. One key per analyst follows in step 16.
11. **Run the preflight**: `python scripts/verify_deployment.py`, with
    `VERIFY_API_KEY` set to the raw key of step 10 (§3). *Done when:* the last
    line reads `N passed, 0 failed, N skipped` and the exit code is 0. §3.1
    says what each line means and how to fix a `[FAIL]`.
12. **Size the prompt budget**: `python scripts/prompt_budget.py` (§16.6; it
    applies with one database too), then put the `PROMPT_RETRIEVAL_TOKEN_BUDGET=`
    line it prints in `.env`. *Done when:* the line is in `.env`. Exit code 1
    means a source does not fit the model's context window, and the output
    says which.
13. **Start the server** (§4): `uvicorn api.server:app --host 0.0.0.0 --port 8000 --no-server-header`
    (or `python -m api`), and for the web UI the static server of
    `web/README.md` (`python -m http.server 8080`, started from `web/`).
    *Done when:* the log shows the provenance banner, `CORS allowed origins: ...`
    and `System prompt loaded (N chars)` (with several sources also one
    `Prompt path for data source '<name>'` line each, §5), and
    `curl http://localhost:8000/health` answers with `status` set to `ok`.
14. **Ask one real question and confirm it was audited** (§6): the `curl` of
    §6, then `tail -n 1 logs/audit_log.jsonl`. *Done when:* the line carries
    today's `timestamp` and the `request_id` of the response.
15. **Run the preflight again** after the `.env` change of step 12, and after
    any later change to `.env` or `project_config/`, with the same command as
    step 11. *Done when:* `0 failed` each time.
16. **Issue one key per analyst** (§1), add each entry to the key file and
    restart the server (the file is read once at start-up). *Done when:* each
    analyst can open the UI and ask a question.
17. **Build the golden set** once real questions are in the audit log, and
    keep it for upgrades (§18). *Done when:*
    `python -m eval.cli verify --golden eval_data/golden.jsonl --accept` has
    activated the cases that held up.

### Install the dependencies

On the machine that will run the server, in its virtual environment:

```bash
pip install -r requirements.lock
```

Install from `requirements.lock`, not from `requirements.txt`. The lock pins
every package, transitively, to the exact version this code was validated
against; `requirements.txt` states floors only, so two installs a month apart
can resolve different trees with nothing to review. One pin matters for
security specifically: `sqlglot` is the parser every allow/deny decision of the
SQL guard (`security/sql_guard.py`) is made from, so an unreviewed `sqlglot`
upgrade is the one dependency change that can alter security behaviour without
a line of this repository changing. Re-run the same command after every pull
(§17 step 2).

CI tests both sets of dependencies: every operating system and Python version
in its matrix runs the suite once on the newest releases `requirements.txt`
allows, and once on exactly the pins of `requirements.lock`, so a failure that
only a newer release causes is seen before anyone deploys it, and what is
deployed is also what was tested (the locked legs are named `... / locked
deps`; pip-audit checks the lock once). A checkout older than PR #152 still has
the single floors-only job.

## 1. Issue an API key

Every route except `GET /health` requires a named API key
(`Authorization: Bearer <key>` — see `README.md`'s "Authentication" section
and `docs/api-contract-v2.md`). Issue one **per analyst**, not one shared
key for the web UI as a whole — the audit trail and the rate limiter both
key on principal id (`observability/audit.py`, `api/middleware.py`'s
`(principal, ip)` bucket), so one shared key behind one UI host makes every
analyst's traffic look like a single caller and collapses everyone into one
rate-limit bucket. `web/` (the static UI under `web/README.md`) is built
for this: each analyst enters their own key once, in their own browser, on
first use — the UI never ships or bakes in a key of its own.

```bash
python -m scripts.issue_api_key --id analyst-1 --name "Jane Analyst"
```

This prints the raw key **once** — hand it to that analyst directly (a
password manager share, or read aloud/typed by hand — never a group
channel or a shared doc) so they can paste it into the web UI's "کلید API"
field themselves; do not also collect it back into your own secrets
manager unless you specifically intend to be able to act as that analyst.
It also prints the array entry to add to your key file in step 2.
If a key is lost, issue that analyst a new one; there is no way to recover
the old one from `key_sha256` alone (that is the point — see
`security/auth.py`'s module docstring).

Repeat for every analyst who will use the UI, plus one more for any other
real caller/integration, appending each entry to the same array
(`[{"id": ...}, {"id": ...}]`) — the file named by `API_KEYS_FILE` (step 2),
or `API_KEYS_JSON` for a single key.

### 1.1 The admin key

At least one key needs the admin capabilities, or the admin panel has
nobody who can open it. The panel's sections do **not** all sit behind one
capability — `docs/admin-panel-architecture.md` §2 splits them:

| Capability | Panel sections it unlocks |
|---|---|
| `admin` | audit summary, deployment checks, query cache, domain config |
| `operations` or `security` | maintenance mode, feedback, schema drift, dimension vocabulary, per-analyst usage, auth failures |
| `operations` | keys and access (issue, disable, revoke) |
| `security` | access requests (the triage queue for a denied-column request) |

The panel has twelve sections. A key holding only `admin` loads four of them
and gets 403 on the other eight, which reads as a broken deployment rather
than as a permissions decision: the keys and access-requests sections say
which capability they are missing, while a 403 on any other section raises
the page-level banner that calls the key "not an admin key", which is
misleading for a key that holds `admin`. For a deployment with a single
operator, grant all three at once:

```bash
python -m scripts.issue_api_key --id admin-1 --name "Admin" --full-admin
```

Where the two-role split is actually being used, grant `admin` alongside
whichever half that person holds:

```bash
python -m scripts.issue_api_key --id ops-1 --name "Ops" --admin --operations
python -m scripts.issue_api_key --id sec-1 --name "Security" --admin --security
```

The script warns when it is asked for a combination that produces a
partly-403 panel, so this is checkable at issue time rather than at first
login. Both admin roles must be bootstrapped from `API_KEYS_FILE` /
`API_KEYS_JSON` this way:
the first admin of each kind comes from the environment, never from a web
flow (`docs/admin-panel-architecture.md` §2.3).

## 2. Set the environment

Copy `.env.example` to `.env` (if not already done) and fill in, at minimum:

- `DB_CONNECTION_URL` — the real warehouse connection string, with the
  login but **without** the password (`mssql+pyodbc://nlq_reader@host:1433/DB?driver=...`) —
  and `DB_PASSWORD`, the raw password, exactly as the database knows it
  with no URL encoding (`p@ss/w:rd` stays `p@ss/w:rd`). A password written
  inside the URL instead must be percent-encoded by hand (`@` becomes
  `%40`); setting it in both places is refused at start-up, and a raw
  password inside the URL is not auto-detected, because `/ ? # :` or a
  literal `%40` cannot be told apart from an encoded one. Querying more
  than one database instead of one? See §16 below: the connection is then
  described in `datasources.yaml` and `.env` holds only a
  `DB_PASSWORD_*` variable per source.
- `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY` — the real LLM
  endpoint.
- `API_KEYS_FILE` — the path of a file holding the array from step 1 (every
  issued key's entry), pretty-printed as you like. Recommended:
  `project_config/api_keys.json` (`project_config/` is git-ignored, and a
  relative path resolves against the repository root, like
  `PROJECT_CONFIG_DIR` below). Create it by copying the template,
  `cp project_config.example/api_keys.example.json project_config/api_keys.json`,
  replace each `key_sha256` with the digest `scripts/issue_api_key.py` printed
  (an entry still holding the template's placeholder stops startup with
  `key_sha256 must be a 64-character SHA-256 hex digest`), and set
  `API_KEYS_FILE=project_config/api_keys.json` in `.env`. The template has
  the shape below: an analyst entry with a `denied_columns` example and an
  admin entry with all three capabilities.

  ```json
  [
    {"id": "analyst-1", "name": "Jane Analyst",
     "key_sha256": "<64 hex>", "denied_columns": ["NationalID", "sales.Order.ID"]},
    {"id": "admin-1", "name": "Admin", "key_sha256": "<64 hex>",
     "denied_columns": [], "admin": true, "operations": true, "security": true}
  ]
  ```

  `denied_columns` takes column names as they appear in `schema.yaml`
  (`"NationalID"`, not `Customer.NationalID`). An entry that leaves the field
  out gets **no** column restriction (a key issued from the admin panel starts
  with every column denied instead) and the server logs one warning for it;
  write `"denied_columns": []` once that is what you mean.

  **Restricting one table, or one data source.** A plain name denies the
  column on every table of every source and refuses every reference. To keep a
  column usable for joins while hiding its values, write a scoped entry; the
  column is then *join-only* (allowed only as one side of `a.col = b.col`
  inside a `JOIN ... ON`):

  | Entry | Effect |
  |---|---|
  | `sales.Order.ID` | join-only on that table (written as `schema.Table.Column`, case-insensitive), whichever source the query runs on |
  | `Warehouse:sales.Order.ID` | the same, only when the query runs on data source `Warehouse` |
  | `Warehouse:ID` | join-only on every table of `Warehouse` that has an `ID` column |

  A table listed under several sources (`datasource: [A, B]`) runs on the
  default source when it is one of them, else the first in `datasources.yaml`;
  that is the source an entry's `Source:` is compared with. Every scoped entry
  is checked at start-up (and when saved from the admin panel): the source
  must be in `datasources.yaml`, the table in `schema.yaml` and in that
  source, the column on the table. A mistake stops the server and names the
  entry. Anything else (the select list, `WHERE`, `GROUP BY`, `ORDER BY`,
  `HAVING`, `USING`, a function, `ON` against a literal) is refused as
  `join_only_column`, and the model is asked to rewrite the query.

  What join-only does not do: it hides a column's **value**, not the fact
  that a row exists, so a join on a filtered foreign key can still probe it.
  If the foreign key pointing at a hidden primary key is visible in another
  table, hiding the primary key is cosmetic; restrict the foreign key as
  well. A reference with no table qualifier counts as restricted when any
  table in its `SELECT` is restricted for that name, so qualify columns.
  Entry names cannot contain `.` or `:`.

  The file is read once at start-up, so **restart the server after editing
  it** (the preflight below runs in its own process and always sees the
  current file). A missing file, invalid JSON (the error gives the line and
  column) or a field repeated inside one entry stops startup.
  `API_KEYS_JSON` still works for a single key written on one line, but do
  not set both: that is refused. A multi-line `API_KEYS_JSON` must be wrapped
  in single quotes with no apostrophe anywhere inside it (a key named
  `Ali's key` breaks it), and double quotes around the array break the JSON
  — which is why the file is the recommended form. Either way, a `.env` line
  python-dotenv cannot use is now refused at start-up with its line number
  and variable name (never its value).
- `PROJECT_CONFIG_DIR` — leave unset (defaults to `project_config/`, this
  deployment's real domain data) unless you deliberately mean to run
  against the sample `project_config.example/` template. If you do set it
  to a **relative** path, it is resolved against the repository root, not
  against whatever directory you happen to start the server from. If your
  deployment starts the server from a directory other than the repository
  root (a systemd `WorkingDirectory`, a container `WORKDIR`, a process
  manager's cwd) and sets a relative `PROJECT_CONFIG_DIR`, double-check
  that it still resolves to the directory you expect after upgrading —
  use an absolute path if you want to be certain regardless of the
  process's working directory.

- `DB_APPLICATION_NAME` — optional; default `local-sql-agent`. The name every
  warehouse connection reports to SQL Server as its `program_name`, so the DBA
  can tell this application's sessions from every other client's. It is added
  only to `mssql+pyodbc` connections, and never replaces an `APP=` or
  `Application Name=` the URL already sets. A source in `datasources.yaml` can
  use its own with `application_name:`. To see the application's sessions and
  whether any holds an open transaction, the DBA (or you, with the right to
  read the DMVs) runs, with the name you configured in place of the default:

  ```sql
  SELECT s.session_id, s.login_name, s.host_name, s.program_name, s.status,
         s.last_request_start_time, s.open_transaction_count
  FROM sys.dm_exec_sessions AS s
  WHERE s.program_name = N'local-sql-agent'
  ORDER BY s.last_request_start_time DESC;
  ```

  This is the first query of `docs/dba/warehouse-load-diagnostics.sql` §2a with
  fewer columns; §2b there lists the statements running right now and §2c the
  blocking, and `docs/dba/README.md` says how to read them. An
  `open_transaction_count` above zero on an idle session is worth a question:
  the executor's transaction is always rolled back, never left open.

Leave `RATE_LIMIT_*`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`, `AUTH_REQUIRED`,
and `MAX_CONCURRENT_REQUESTS` at their shipped defaults unless step 3 below
tells you otherwise for your specific expected concurrency — see
`config.Settings.rate_limit_requests` / `.log_backup_count` for the
reasoning behind each default before changing it. `PROMPT_RETRIEVAL_TOKEN_BUDGET`
(default `6000`) is the exception that is worth measuring:
`python scripts/prompt_budget.py` shows what your prompt costs and which
value to set (§16.6 has the steps, and applies per data source when there
are several).

If the API and the static UI end up on different ports or hosts (the
`web/` layout described in step 4 below and in `web/README.md` puts them
on separate ports even on one machine), set two more things now rather
than discovering the gap once the UI is already showing three red lights:

- `API_HOST` / `API_PORT` — where this API itself binds when started with
  `python -m api` (§4 below); `.env` is now a complete description of
  this, not just of everything downstream of it. Defaults to
  `127.0.0.1:8000` — loopback only, until you widen it on purpose.
- `CORS_ALLOWED_ORIGINS` — the UI's **own** origin (protocol + host +
  port, e.g. `http://172.16.101.42:8077`), so the browser's own
  cross-origin check does not silently block every call the UI makes.
  See its dedicated warning in step 4 below — this is the single most
  common cause of "the UI shows the backend as down when it is not."

### 2.1 Create `project_config/`

`.env` says how to reach the database and the model; `project_config/` says
what is in the database. It is your domain data, git-ignored on purpose, and
the server will not start without it: there is no fallback to
`project_config.example/`. Nine YAML files and one text file must be there:
`aliases.yaml`, `business_rules.yaml`, `entities.yaml`, `examples.yaml`,
`memory_policy.yaml`, `metrics.yaml`, `retrieval_hints.yaml`, `schema.yaml`,
`session_policy.yaml`, and `system_prompt.md`.

Start from the template, which has all of them with placeholder content:

```bash
cp -r project_config.example project_config
```

(PowerShell: `Copy-Item -Recurse project_config.example project_config`.) The
copy also holds `README.md`, `datasources.example.yaml`, `api_keys.example.json`
and `_test_fixtures/`; the templates are not read under those names, and the
folder is yours to tidy. Then replace the placeholders with your own domain.
Who writes what:

| File | How it is made |
|---|---|
| `schema.yaml` | By hand, or drafted from the live database by `python -m database.schema_inspector_cli` (§2.3). It is the SQL guard's allowlist: a table you add widens what generated SQL may touch. Give every table a `db_schema` (§16.3). |
| `entities.yaml`, `aliases.yaml`, `business_rules.yaml`, `examples.yaml` | By hand, or drafted with an LLM by the setup wizard `python setup_project.py` (§2.2). |
| `metrics.yaml`, `retrieval_hints.yaml`, `session_policy.yaml`, `memory_policy.yaml` | By hand. Neither tool writes them, so after the copy they still hold the template's content. `docs/en/tutorial.md` shows how each one steers retrieval and the prompt. |
| `system_prompt.md` | By hand: the model's instructions for your schema, rules and dialect (§5 says what happens without it). |
| `relationships.yaml` | Optional; the server does not read it (the prompt's relationships come from `schema.yaml`). |
| `datasources.yaml` | Only for more than one database (§16). |
| `api_keys.json` | The key array that `API_KEYS_FILE` names (§1, §2 above). |

`python scripts/verify_deployment.py` (§3) loads six of these (`aliases`,
`entities`, `business_rules`, `examples`, `metrics`, `schema`) as `project_config/ loads`.
`session_policy.yaml`, `memory_policy.yaml`, `retrieval_hints.yaml` and
`system_prompt.md` are not part of that check, so a missing one is not caught
there; the server reads the system prompt at start-up (§5) and the others when
they are first needed.

### 2.2 The setup wizard, `setup_project.py`

A one-time helper that drafts four of those files (and `relationships.yaml`)
from the live database with the help of a model. It is optional: everything it
writes can be written by hand, and everything it writes must be reviewed,
because each file starts with `# AUTO-GENERATED by setup_project.py — review before use`.
Run it from the repository root, after §2.1 and with `.env` filled in (it reads
`.env`):

```bash
python setup_project.py \
    --db-url "mssql+pyodbc://reader:p%40ss@dbhost:1433/WarehouseDB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes" \
    --llm-base-url http://<llm-host>:<llm-port>/v1 \
    --llm-model <model name served by that endpoint> \
    --language fa
```

It asks questions as it goes, unless told not to. Its options:

| Option | Meaning |
|---|---|
| `--db-url URL` | The database to describe. Without it: `DB_CONNECTION_URL`, then `DATABASE_URL`; if there is none, interactive mode asks and `--non-interactive` stops. |
| `--llm-provider openai\|mock` | `openai` (default) is any OpenAI-compatible endpoint; `mock` calls no model and leaves aliases, rules and examples empty. Also `WIZARD_LLM_PROVIDER`. |
| `--llm-model NAME` | Default `gpt-4o-mini` when `WIZARD_LLM_MODEL` is not set. |
| `--llm-base-url URL` | Default `https://api.openai.com/v1` when `WIZARD_LLM_BASE_URL` is not set. |
| `--language fa\|en\|both` | The language analysts ask in. Also `WIZARD_LANGUAGE`. Unset: asked interactively (default `en`), `en` when not interactive. |
| `--output DIR` | Where to write (default `project_config`). |
| `--review interactive\|auto` | `auto` is the same as `--non-interactive`. |
| `--non-interactive` | Accept every suggestion without asking; for scripts. |
| `--include-schemas a,b` | Only these database schemas. |
| `--dry-run` | Print the files, write none, and skip the validation step. |
| `--resume` | Do not write a file that already exists. |

What it does, in order. Each step is recorded in `<output>/.setup_log.json`
under the key shown:

| Key | Step | What happens |
|---|---|---|
| `step1_connection` | Database connection | Opens the URL and runs `SELECT 1`; on failure it prints the error and, interactively, offers a retry. |
| `step2_schema` | Schema discovery | Reads tables, columns, foreign keys and up to five sample values per text column, and calls each table a fact or a dimension table. Interactively it asks which tables to leave out. |
| `step3_aliases` | Aliases | One model call per table for aliases and a one-line description; interactively you accept, edit or clear each. |
| `step4_rules` | Business rules | One model call per fact table for its value and volume columns and a rule text. |
| `step5_examples` | Examples | One model call that asks for ten question-and-SQL pairs. |
| `step6_write` | Review and write | Writes `entities.yaml`, `aliases.yaml`, `business_rules.yaml`, `examples.yaml` and `relationships.yaml` into `--output`. Interactively each file is shown first: Accept, Edit in `$EDITOR`, Regenerate or Skip. |
| `step7_validate` | Validation | Loads the four files that have a loader through the application's own validators and prints `OK`, `FAILED: ...` or `skipped` for each, then `Setup complete.` and the number of entities, rules and examples. |

What it does not do:

- **It does not write `schema.yaml`**, so it cannot make the SQL guard's
  allowlist; use §2.3 or write it by hand. It does not write `metrics.yaml`,
  `retrieval_hints.yaml`, the two policy files, `system_prompt.md`,
  `datasources.yaml`, `.env` or any API key.
- **It does not assign `datasource:` to any table.** It describes one database
  per run and knows nothing of `datasources.yaml`; for several databases that
  is `python scripts/assign_datasources.py` (§16.3).
- **It does not read `DB_PASSWORD`, `DB_PASSWORD_*` or `datasources.yaml`.** The
  URL it is given must carry the password itself, percent-encoded (`@` is
  `%40`). Passing it as `--db-url` leaves it in the shell history; the
  `DATABASE_URL` environment variable avoids that.
- **It does not read `OPENAI_BASE_URL` or `OPENAI_MODEL`.** It has its own
  settings, which win over those: the options above, or the variables
  `WIZARD_LLM_PROVIDER`, `WIZARD_LLM_MODEL`, `WIZARD_LLM_BASE_URL` and
  `WIZARD_LANGUAGE`. With none of them set it asks `https://api.openai.com/v1`
  for `gpt-4o-mini`. `.env.example` sets all four, so a `.env` copied from it
  gives the wizard the model name `gpt-oss-20b`, the language `fa` and an
  **empty** endpoint address (`WIZARD_LLM_BASE_URL=`); an empty address cannot be
  reached, so set that variable or pass `--llm-base-url` with your endpoint
  (`http://<llm-host>:<llm-port>/v1`). It also needs a non-empty
  `OPENAI_API_KEY`. If the key is missing or the endpoint cannot be reached it
  prints `Warning: LLM unavailable (...)` and carries on with the mock
  provider, so aliases, rules and examples come out empty. It does not apply `LLM_ALLOW_REMOTE`. What it sends to the model
  is table and column names, up to ten sample values per table read from the
  warehouse, and a schema summary: do not point it at a remote endpoint if
  those values may not leave your network.

Things to know before you run it:

- **It overwrites.** If `project_config/` was copied from the template (§2.1),
  the five files above are replaced by the wizard's output with no backup, in
  interactive mode as well after you accept them. Copy the directory first, or
  run it with `--output project_config_draft` and move over what you want.
  `--resume` writes nothing into a directory that already has those files, and
  it does not skip steps 1 to 5 either (the model is still called): the log is
  a record, not a checkpoint.
- **At the time of writing, step 7 needs a populated `project_config/`.** It
  imports the application's knowledge package, which loads `aliases`,
  `business_rules`, `entities`, `examples` and `metrics` from
  `PROJECT_CONFIG_DIR`, not from `--output`. On a tree without the template
  copy (§2.1) it can end with `ConfigNotFoundError: ... metrics.yaml not found`
  after step 6 has already written its files. Do §2.1 first and it does not
  arise.
- **`.setup_log.json` may contain the database URL, including a password written
  in it.** Do not share it (§19), and delete it when the run is done. (The
  `# Source:` comment in the generated `entities.yaml` has the password masked.)
- **In the review menu use Accept, Edit or Skip;** at the time of writing
  Regenerate shows the same text again.

### 2.3 Drafting `schema.yaml`: `database.schema_inspector_cli`

```bash
python -m database.schema_inspector_cli \
    --db-url "mssql+pyodbc://reader:p%40ss@dbhost:1433/WarehouseDB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes" \
    --output-dir project_config_draft \
    --include-schemas sales,ref
```

Without `--db-url` it reads the `DATABASE_URL` or `DB_CONNECTION_URL`
environment variable of the shell it runs in (unlike the wizard it does not
read `.env`); like the wizard it does not apply `DB_PASSWORD`, so the URL needs
the password percent-encoded in it. Other options: `--exclude-tables a,b`,
`--sample-rows N` (default 10; `0` skips reading sample values),
`--no-row-counts` (skip `COUNT(*)` per table) and `--dry-run` (print instead
of writing). It writes `schema.yaml`, `entities.yaml`, `aliases.yaml` and
`relationships.yaml` into the draft directory, never into `project_config/` or
`project_config.example/` (it refuses those two names with exit code 2), and it
prints `Done.` at the end. The draft directory is git-ignored.

The draft is a starting point, not a `schema.yaml` to deploy: every description
is a placeholder (`TO BE FILLED`), column notes may quote real values read from
live rows, and there is no `db_schema` on any table. Treat the directory as
sensitive until you have rewritten those notes, write real descriptions, add
`db_schema` to every table (§16.3), and then copy `schema.yaml` into
`project_config/`. Run `tests/test_schema_registry_snapshot.py` after editing it,
as the README says, and `python scripts/verify_deployment.py` (§3) to load it.

## 3. Run the preflight

```bash
python scripts/verify_deployment.py
```

This must print `0 failed` before you go further. Every line is
`[PASS] / [FAIL] / [SKIP]` with a reason — a `[FAIL]` tells you exactly
what to fix (a placeholder still in `.env`, an unreachable database, a
model name the endpoint doesn't actually serve, no API key configured, an
unwritable log directory, a `project_config/` that fails to load, or a
rate limit too tight for your expected number of analysts). `[SKIP]` is
not a failure — it means a check couldn't run (e.g. no database reachable
yet to test the row cap against), not that something is wrong.

The checks run in this order: `Settings.validate()` (required settings,
leftover placeholders, `.env` lines python-dotenv cannot use),
`Tables map to data sources`, then `Database connectivity`,
`Login is read-only`, `Row cap` and `Query timeout`, then
`Tables are in their data source`, `OpenAI-compatible model exists`,
`API key authentication`, `Audit log directory writable`,
`Session store directory writable`, `project_config/ loads` (the six domain
files) and `Rate limit sane for deployment`. With several data sources the four database
checks run once per source and each name carries the source, as
`Database connectivity [sales]`; `Tables are in their data source` is skipped
with one source. The last line is `N passed, N failed, N skipped`, and the
exit code is 1 when anything failed.

Two optional environment variables sharpen two of the checks without
changing anything persistent:

- `VERIFY_API_KEY=<raw key from step 1>` — proves that *specific* key
  round-trips through real authentication, not just that some key is
  configured. It is checked against `API_KEYS_FILE` / `API_KEYS_JSON` and the
  application database together, as the server does, so a key issued from the admin
  panel works too.
- `VERIFY_EXPECTED_ANALYSTS=<N>` — states how many concurrent analysts you
  actually expect behind the smallest configured key's bucket, so the
  rate-limit check reasons about your real deployment shape instead of
  the default assumption of 10.

```bash
VERIFY_API_KEY=<raw key> VERIFY_EXPECTED_ANALYSTS=15 python -m scripts.verify_deployment
```

PowerShell has no inline environment-variable prefix, so on Windows that
line is a parse error (`The term 'VERIFY_API_KEY=...' is not recognized`),
not a failing check. Set them as variables first:

```powershell
$env:VERIFY_API_KEY = "<raw key>"
$env:VERIFY_EXPECTED_ANALYSTS = "15"
python -m scripts.verify_deployment
$env:VERIFY_API_KEY = $null   # do not leave the raw key in the shell
```

The script prints whichever form matches the shell it is running in.

Do not proceed to step 4 with any `[FAIL]` outstanding.

### 3.1 What each check means, and what to do about a `[FAIL]`

A line is `[PASS]`, `[FAIL]` or `[SKIP]` followed by the check's name and a
reason; there is no warning status. `[FAIL]` is a problem to fix before going
on, and any `[FAIL]` makes the exit code 1. `[SKIP]` means the check could not
run (nothing to test against, or deliberately switched off) and does not fail
the run. In the order they run, with the name exactly as printed (with several
data sources, the four marked * print once per source, as
`Database connectivity [sales]`):

| Check | What it does | `[FAIL]` means, and the fix | `[SKIP]` means |
|---|---|---|---|
| `Settings.validate()` | Runs the same validation the server runs at start-up: required settings, leftover placeholders, `.env` lines python-dotenv cannot use, a `SQL_DIALECT` that matches the connection, every warehouse connection, `LLM_EXTRA_BODY`. | The reason is the error text: `OPENAI_MODEL is not configured`; `DB_CONNECTION_URL still has the factory-default placeholder host (username@server)`; `SQL_DIALECT=... does not match ...`; a `.env` problem listed by line number and variable name; a source whose `password_env` or `username_env` variable is unset or empty (named). Fix `.env` (§2) and run again. | Never. |
| `Tables map to data sources` | Loads `datasources.yaml` and checks that every `datasource:` in `schema.yaml` names a configured source. PASS prints `N data source(s): a, b` (`1 data source(s): default` without a `datasources.yaml`). | `schema.yaml assigns tables to data sources that are not configured: Order -> elsewhere. Configured sources: [...]`: a misspelt name (the letter case must match `datasources.yaml`) or a source missing from the file. Or the message of an invalid `datasources.yaml` (§16.2). | Never. |
| `Database connectivity`* | Opens a connection through the application's own engine and runs `SELECT 1`. PASS names the target with its password masked. | `could not connect to <target>: <driver error>`. Host, port, firewall, ODBC driver name (`ODBC Driver 18 for SQL Server`), login, password, `TrustServerCertificate`. Nothing after it can be trusted until it passes. | Never. |
| `Login is read-only`* | Tries `CREATE TABLE` on a scratch table (`_nlq_agent_deploy_verify_probe`) inside a transaction that is always rolled back, then checks nothing persisted; it also drops that scratch table if a previous run left one. | `... PERSISTED -- the login can write ...`: the login is not read-only. Stop and have the DBA apply `docs/db-hardening.md`. Or `could not verify: ...`. A `[PASS]` that says the `CREATE TABLE` did not raise but the rollback held means the login *could* create tables and only the rollback saved it: have the DBA tighten it anyway. | There is no database connection. |
| `Row cap`* | Runs `SELECT TOP (10 x cap + 10) name FROM sys.all_objects` through the executor and counts the rows. | `returned 1500 rows, expected <= 1000`: the executor returned more rows than `MAX_ROWS_RETURNED` allows. Do not deploy until it holds; there is no setting that fixes it, so report it. | The database is unreachable, or the probe query cannot run (it is T-SQL). |
| `Query timeout`* | Runs `WAITFOR DELAY` with the timeout cut to at most 5 seconds and times how long the executor takes to abort it. | `took Ns -- longer than the Ms timeout should allow`, or `WAITFOR DELAY completed ... without the timeout firing`: the driver timeout is not applied, or the server does not support `WAITFOR` (some serverless Azure SQL tiers). Check `QUERY_TIMEOUT_SECONDS` and the driver. | The database is unreachable, or the probe cannot run. |
| `Tables are in their data source` | Compares `schema.yaml` with each source's catalogue and fails for a table whose columns are all missing from the source it is assigned to while another source has it. | `N table(s): <table>: not in <assigned>, found in <other> — set datasource: <other>; ...` (ten hints, then `and N more`). The table has no `datasource:` (so it runs on the default source) or the wrong one. Run `python scripts/assign_datasources.py` and replace `schema.yaml` (§16.3); this is step 6 and 7 of "First install, in order". Or `could not compare schema.yaml with the data sources: ...` when a catalogue cannot be read. | One data source (`one data source -- nothing to place`). |
| `OpenAI-compatible model exists` | Asks `OPENAI_BASE_URL` for `/models` (5 second timeout, `OPENAI_API_KEY` as the bearer token) and looks for `OPENAI_MODEL` among the ids it lists. | `could not reach <base>: ...`: wrong URL or port, endpoint down, a proxy, or an endpoint with no `/models` route. Or `'<model>' not found among models <base> lists: [...]`: set `OPENAI_MODEL` to one of the listed ids. | Never. |
| `API key authentication` | Checks that at least one key is configured (`API_KEYS_FILE` or `API_KEYS_JSON`, plus the application database) so that the server would start, and, with `VERIFY_API_KEY` set, that this raw key authenticates. | `API key configuration is invalid: ...` (the server would refuse to start; §5); `AUTH_REQUIRED is true but there are no configured keys ...` (issue one, §1); every key revoked or disabled in the application database; or `VERIFY_API_KEY was set but did not match any configured key's SHA-256 digest` (a truncated paste, or the `key_sha256` pasted instead of the raw key). With `AUTH_REQUIRED=false` it passes and says so; do not use that in production. | Never. |
| `Audit log directory writable` | Creates `LOG_DIR` if needed and writes then deletes a probe file in it; checks an existing `audit_log.jsonl` is writable. It never writes to `audit_log.jsonl` itself. | `could not create ...` or `... is not writable`: fix the directory's permissions or `LOG_DIR`. Fail it loudly here because a failing audit write never fails the user's query (§6). | Never. |
| `Session store directory writable` | The same probe on the directory of `SESSION_STORE_PATH`. | `could not create ...` or `... is not writable`. | `SESSION_STORE_PATH` is empty (persistence deliberately off). |
| `project_config/ loads` | Loads `aliases.yaml`, `entities.yaml`, `business_rules.yaml`, `examples.yaml`, `metrics.yaml` and `schema.yaml` under the current code's models. | `<file> not found under '<dir>'` (§2.1), or `<file> failed validation ...` with the field: a file copied from an older deployment is missing a field a later release requires. Edit the field, or compare with `project_config.example/`. Duplicate YAML keys are refused (§17 step 3). | Never. |
| `Rate limit sane for deployment` | Works out requests per second per analyst from `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW_SEC` and `RATE_LIMIT_BURST`, assuming `VERIFY_EXPECTED_ANALYSTS` (default 10) analysts share one key's bucket. | Under 0.1 requests per second per analyst: raise `RATE_LIMIT_REQUESTS`, or set `VERIFY_EXPECTED_ANALYSTS` to the real number. | Never. |

The most common first-install `[FAIL]` with several data sources is
`Tables are in their data source -- 44 table(s): ... not in sales, found in inventory — set datasource: inventory`.
It means `scripts/assign_datasources.py` was never run, or its proposal was not
moved over `schema.yaml`. The same findings are on the admin panel's schema-drift
card (§16.8). `GET /admin/health/checks` runs these checks from the panel; it
leaves out `Login is read-only`, `Query timeout` and `Tables are in their data
source` unless "deep checks" is pressed (§12).

## 4. Start the server

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000 --no-server-header
```

`--no-server-header` matters: `SecurityHeadersMiddleware` deletes an
app-set `Server` header, but uvicorn appends its own `Server: uvicorn` at
the protocol layer *after* the ASGI app returns, where no middleware can
reach it. The flag is the only place that banner is actually suppressed on
the wire; a reverse proxy that overrides `Server` also works. (The
in-process test can only see the app layer, so it passes either way — the
flag is a deployment guarantee, not a code one.)

(Add `--workers N` for more than one process if your expected load needs
it — the audit log, rate limiter, and query cache are all per-process
in-memory state today, so multiple workers each keep their own rate-limit
buckets and cache; this does not affect correctness, only means each
worker's rate limit applies independently.)

Equivalently, once `API_HOST`/`API_PORT` are set in `.env` (step 2 above):

```bash
python -m api
```

This runs the identical app with the identical `--no-server-header`
guarantee, bound to whatever `API_HOST`/`API_PORT` resolve to — useful
when the host/port belong in `.env` alongside everything else this
deployment already keeps there, rather than only on a command line
someone has to remember or script separately. It does not support
`--workers`; use the plain `uvicorn` command above for that.

> **The API and the UI on different ports/hosts?** This is the single
> most common first-week deployment snag, and it presents as a dead
> backend, not as a configuration error: the UI shows all three health
> lights red, `curl http://<api-host>:<api-port>/health` from the same
> machine answers fine, and the browser's console shows nothing more
> specific than "Failed to fetch" — because a browser reports a blocked
> cross-origin (CORS) request and a truly unreachable host identically,
> on purpose. Set `CORS_ALLOWED_ORIGINS` to the **UI's own origin**
> (protocol + host + port the UI is served from, e.g.
> `http://172.16.101.42:8077`) and restart — see the CORS block in `.env`
> step 2 above, and confirm the effective list in the startup log (step 5
> below logs it as `CORS allowed origins: ...`).

## 5. Confirm the startup banner

The very first thing logged, before any config is even validated, is the
provenance banner (`core/provenance.log_startup_notice`) — confirm it
appears in the server's log output:

```
Local SQL Agent - (c) 2024-2026 Ali Sadeghi Aghili - BUSL-1.1
```

(it is the first of a few lines naming the licence terms and the files that
state them)

Right after it, confirm the CORS line — the one place the effective,
post-`.env` allowlist is stated plainly, rather than left for the UI to
discover by failing:

```
CORS allowed origins: http://localhost:8080, http://127.0.0.1:8080
```

If the UI's own origin is not in that list (and the UI is not
same-origin with the API), that is the fix — see the CORS callout in
step 4 above before assuming anything else is wrong.

With several data sources, also look for one line per source saying which
prompt path its questions take (logged at start-up, or at first use if that
comes first):

```
Prompt path for data source 'sales': static prefix (cacheable) -- 26 table(s), static prefix estimate 5100 tokens, PROMPT_RETRIEVAL_TOKEN_BUDGET 6000 (per source)
```

`retrieval restricted to its tables` in place of `static prefix (cacheable)`
means that source's prefix is over the budget; §16.6 says what to do about it.

If neither line appears at all, nothing is wrong with this deployment's
`.env` — it means logging itself never reached a handler. Both
documented start commands leave the ROOT logger exactly as Python starts
it (level `WARNING`, no handler): uvicorn's own default logging config
only covers its own `uvicorn`/`uvicorn.access` loggers, never the root
one. `api/server.py`'s `lifespan` now fixes this itself, once, on every
startup (`core/logging_setup.py`) — so on a current checkout this should
never actually happen; if it does, an unusual logging setup elsewhere in
the process (an operator's own `logging.basicConfig()` or `dictConfig`
that attached a handler at a level above `INFO` before startup reached
this point) is the most likely cause. `LOG_LEVEL` (default `INFO`, see
`.env.example`) is what that same fix applies to the root logger's level
when nothing else has configured logging first.

If startup instead exits immediately with `RuntimeError: ...`, the
preflight in step 3 should have already caught the same problem — go back
and re-run it. The three most common fail-closed exits, all intentional:

- `Invalid configuration: ...` — a `Settings.validate()` failure (a
  placeholder left in `.env`, or a `.env` line python-dotenv could not use:
  the message lists each by line number and variable name — a multi-line
  `API_KEYS_JSON` that is not wrapped in single quotes is the usual one).
- `Invalid API key configuration: ...` — `API_KEYS_FILE` is missing,
  unreadable or not valid JSON, an entry is malformed, or both
  `API_KEYS_FILE` and `API_KEYS_JSON` are set.
- `AUTH_REQUIRED is true but no usable key is configured in API_KEYS_FILE,
  API_KEYS_JSON or the application database` — step 1/2 was skipped or the
  entry didn't make it into the key file or `.env`.

Also confirm `System prompt loaded (N chars)` appears — a missing
`<PROJECT_CONFIG_DIR>/system_prompt.md` is a packaging error, not a config one.
**The server refuses to start without this file**: `api/server.py`'s
`lifespan` raises, verbatim, `RuntimeError: System prompt not found:
<resolved path>`, which is exactly the exit this section's preflight
should already have caught. If you see it, create the file by copying
`project_config.example/system_prompt.md` to
`<PROJECT_CONFIG_DIR>/system_prompt.md` and rewriting it to describe your
real schema, business rules, and dialect requirements — the example copy
describes only the generic example schema and is not meant to be used
as-is.

## 6. Confirm the audit log is being written

Send one real (or throwaway) authenticated query, then confirm a line
landed:

```bash
curl -X POST http://localhost:8000/query \
  -H "Authorization: Bearer <a real issued key>" \
  -H "Content-Type: application/json" \
  -d '{"question": "test", "mode": "sql"}'

tail -n 1 logs/audit_log.jsonl
```

The line should be a JSON object with today's `timestamp`, the
`request_id` your response's `X-Request-ID` header also carries, and a
`guard`/`llm` block. If `logs/audit_log.jsonl` doesn't exist or didn't
grow, re-run `python scripts/verify_deployment.py` — the audit-log
writability check will say exactly why (directory not writable, wrong
`LOG_DIR`, ...). Remember: a broken audit write never fails the user's
query (`observability/audit.py`'s second hard rule) — it fails *silently*
from the caller's point of view, which is exactly why this step exists.

## 7. During the week

Nothing to do by default — `logs/audit_log.jsonl` rotates on its own
(`LOG_MAX_BYTES`/`LOG_BACKUP_COUNT`, defaults sized so a single
organisation's first weeks cannot plausibly exhaust the retained history;
see `config.Settings.log_backup_count`). As a belt-and-suspenders measure
for a week whose data cannot be re-collected, consider copying
`logs/audit_log.jsonl*` to a second location once a day (a scheduled
`cp`/backup job, or manually) — the rotation defaults are generous, but a
second copy costs little and removes any dependency on this specific
disk surviving the week.

## 8. At week's end: run the analyser

```bash
python scripts/analyze_audit_log.py > report.txt
```

With no arguments this reads `logs/audit_log.jsonl` plus any rotated
`.1`, `.2`, ... backups automatically. The output is a **fully aggregated**
report — record counts, latency percentiles, `finish_reason`/error-code
distributions, cache hit rates, SQL-shape clusters, correction-round
stats — with no question text, no generated SQL, and no error messages
anywhere in it. `report.txt`'s own `mode` line will read `aggregate_safe`.
**This is the file to send back.**

Before sending it, sanity-check the top of the report:

- `records_by_model` — confirms the week's traffic is real (not
  `mock:stub`/`ollama:test` left over from local development).
- `finish_reason distribution` — any non-trivial `length` count means
  `llm_num_predict` was too low for at least some real questions. Raising
  it is one fix; if the model is a reasoning one (Qwen3, DeepSeek-R1,
  gpt-oss) and `reasoning_detected` is true on those records, turning its
  reasoning off with `LLM_EXTRA_BODY` is the cheaper one — those tokens
  are paid for and waited on either way. A `length` count paired with
  empty SQL is the extreme case and now reports itself as
  `LLM_OUTPUT_TRUNCATED`; see `.env.example`'s `LLM_NUM_PREDICT` entry.
- `cache_behaviour`'s `prefix_cache_hit_rate` — the first real measurement
  of whether Phase 2's static-prefix latency premise actually held under
  real traffic.

If, and only if, you have separately decided sharing example questions is
acceptable (e.g. you are debugging a specific miss with whoever wrote this
code), re-run with `--include-examples` — this adds a small number of
verbatim questions and is clearly labelled
`mode: aggregate_with_examples` with a warning banner in the text output.
Never send an `aggregate_with_examples` report anywhere by default; treat
it the same way you would treat the raw log itself.

```bash
python scripts/analyze_audit_log.py --include-examples > report_internal.txt
```

Send `report.txt` (or `report.json` via `--json`, for anyone who wants to
compute their own numbers off the aggregates). Do not send
`logs/audit_log.jsonl` itself, or `report_internal.txt`, unless that
decision has been made explicitly and separately.

## 9. A restore of the application database triggers a key review

Admin panel phase 2 (`docs/admin-panel-architecture.md` §5.7) moves API
keys into the application database (`APP_DB_URL`, or the SQLite fallback
at `logs/app.db`) so a leaked key can be revoked immediately, without a
restart. Revocation is recorded as a tombstone (`revoked_at`), never a
deleted row, specifically so a backup cannot silently undo it — **but a
restore still reaches back to a point in time before the tombstone
existed at all**, which reintroduces exactly the key that revocation
exists to keep out. The backup is the cause of the regression here, not
the cure for it.

So: whenever the application database is restored from a backup (for any
reason — disaster recovery, a botched migration, moving to a new host),
before resuming normal operation:

1. List every key (`GET /admin/keys`, an operations action) and compare
   its `revoked_at`/`disabled_at` against what you expect — a key you
   revoked after the backup's timestamp will show up as active again.
2. Re-revoke anything that should still be revoked. This is idempotent
   (`POST /admin/keys/{key_sha256}/revoke` on an already-revoked key is a
   no-op), so doing this defensively for every key revoked in the last
   backup cycle costs nothing.
3. If the leak that originally caused a revocation is still live (the
   same credential could still be circulating), treat the restore itself
   as a new incident, not merely a maintenance step — the window between
   the restore completing and this review finishing is a window where
   that key worked again.

This is a process step, not something the application can enforce from
inside itself: nothing running *after* a restore can know what should
have stayed revoked *before* it happened.

## 10. Moving the application database to another backend

`python -m scripts.migrate_app_db --from <url> --to <url>` copies the
application database — keys, role grants, configuration versions,
feedback — between any two supported backends (admin panel phase 5,
`docs/admin-panel-architecture.md` §5.4). Starting on SQLite is only safe
because leaving it is a supported operation, and leaving is the expected
path: an organisation with no DBA today gains one tomorrow and decides
its metadata belongs on the managed server.

Run it with `--dry-run` first. It reports, per table, what it would copy
without writing anything.

**The tool never touches the source.** It copies forward, verifies, and
stops; pointing `APP_DB_URL` back at the original is the rollback, and it
needs nothing to have been prepared in advance. The source's content hash
is printed before and after every run — including a failed one — so that
claim is checkable rather than asserted.

**A migration that cannot prove equality is a failed migration.** After
copying, the tool re-reads both databases from scratch and compares row
counts and a content hash per table. Read that report; do not switch
`APP_DB_URL` over on the strength of the summary line alone.

**The application must be stopped, or in maintenance mode.** Otherwise a
write lands in the old database after the copy has read past it, and is
lost with no error anywhere. The tool refuses to run against a database
with recent write activity for this reason, and says so.

### The exported artefact is sensitive

The export contains every API key's `key_sha256` hash and every key's
column-level ACL (`denied_columns_json`). Not secrets in the `.env`
sense — a hash is not a key — but an inventory of who exists and what
each one is allowed to see, which is not something to leave in a shared
folder. The same care that applies to `project_config/` (§2 above:
gitignored, kept off shared storage) applies here.

The tool writes its export to a temporary file and deletes it when the
run finishes. If you take a copy deliberately, store it where the
application database itself is stored, not beside a deployment bundle.

## 11. Admin-action log retention

Admin panel phase 6 adds a real retention policy for
`logs/admin_action_log.jsonl` (`docs/admin-panel-architecture.md` §9). The
short version: retained by TIME, configurably, never by size.

**Why not size, like every other log here.** Every other JSONL log in
this project (`query_log.jsonl`, `audit_log.jsonl`) rotates once it
passes `LOG_MAX_BYTES`, discarding the oldest lines first. That is the
wrong shape for an audit trail whose entire purpose is "each admin role
can read that the other one acted" — size-based rotation discards the
*oldest* evidence exactly when there is the *most* activity, so anyone
wanting to bury a specific admin action could do so on purpose, by
generating enough unrelated admin noise to roll it off the end of the
file before the other role ever reads it. `appdb.admin_audit` now writes
this one log with rotation disabled entirely and relies solely on the
mechanism below.

**The policy**: `ADMIN_ACTION_LOG_RETENTION_DAYS` (default `365`) bounds
how long a record is kept. `appdb.admin_audit.purge_expired_admin_actions`
runs once at every start-up (mirroring the existing session-retention
purge) and discards only records older than that many days — never
because something noisier was appended after them. Set it to `0` to keep
every record forever, which is the safer choice for a deployment that
would rather manage its own disk space by hand than risk an automated
purge; an on-prem deployment operating under an externally imposed
retention requirement (a regulator, an internal audit policy) should set
this explicitly to whatever that requirement specifies, rather than
accept the default.

**Configuration versions and feedback rows are not covered by this**,
deliberately: both are retained in full, unconditionally, and always have
been — a `project_config/` bundle snapshot and a wrong-answer flag are
both small and comparatively rare, and capping either would remove a
feature this system already promises (rolling back to *any* prior
configuration version; seeing the full trend of flagged answers over
time), not merely trim log noise.

**If your organisation's compliance posture wants more than a
time-bounded file**, the stronger version of this control — the
admin-action log living in the application database, where the
organisation's own backup and retention regime already reaches — was
considered and is recorded as a recommendation in
`docs/admin-panel-architecture.md` §4.2/§9, but was not built this
phase: moving it there needs the same tamper-evidence argument that log
being a file (not a database row anyone with a connection could edit) was
originally built on to be re-made and re-satisfied first, which is a
larger change than a retention window.

## 12. What this application sends to the warehouse, and how often

A DBA watching `sys.dm_exec_sessions`/a trace sees this application as one
of possibly many clients. Everything below is a real, periodic or
per-request round trip to a configured warehouse connection
(`DB_CONNECTION_URL`, or each source in `datasources.yaml`, which has a pool
and its own traffic) — kept here in one place, with the setting that controls
each, following the 2026 warehouse-load audit that traced a steady `SELECT 1`
stream (plus DDL attempts and catalogue scans) back to a few specific
sources.

| Source | What it sends | How often | Controlled by |
|---|---|---|---|
| Connection-pool checkout (`database/connection.py`, `database/pool_ping.py`, one pool per data source; `appdb/engine.py` too, for a non-SQLite application database) | An idle-aware liveness probe (`SELECT 1`) before handing a pooled connection to any caller | Only when the connection has sat idle in the pool for at least `DB_POOL_PING_IDLE_SECONDS` — i.e. roughly once per burst of activity after a gap, not once per query. `DB_POOL_PING_IDLE_SECONDS=0` reverts to the old ping-every-checkout behaviour | `DB_POOL_PRE_PING` (default `true`) — see §13 below for the trade-off; `DB_POOL_PING_IDLE_SECONDS` (default `60`) — the idle threshold; `DB_POOL_RECYCLE_SECONDS` (default `3600`) bounds how long a connection sits in the pool before being recycled regardless |
| `GET /health` (`api/health.py`) | Always exactly one explicit `SELECT 1` on the checked-out connection (of each data source, when there are several) — checkout's own idle-aware probe no longer runs unconditionally, so `/health` cannot rely on it (see §13). In the rare case the checked-out connection had also gone idle long enough for checkout to probe it too, that is a second round trip on top of this one | At most once per `HEALTH_CACHE_TTL_SECONDS` (default `15`) no matter how often external monitors call this endpoint; concurrent callers within that window share one probe | `HEALTH_CACHE_TTL_SECONDS` |
| Admin panel — deployment checks, non-deep (`GET /admin/health/checks`, `scripts/verify_deployment.build_checks()` minus the deep checks below, per data source) | `check_db_connectivity`'s `SELECT 1`, `check_row_cap`'s `SELECT TOP n name FROM sys.all_objects` | Once when the admin panel is opened, and again only when the operator presses that card's own refresh button — **not** on the 30-second auto-refresh. A repeat within `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` (default `300`) is served from cache instead of re-run; `?refresh=1` forces a fresh run | `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` |
| Admin panel — deployment checks, deep (`GET /admin/health/checks?deep=1`) | Everything above, **plus** `check_login_is_read_only`'s always-rolled-back `CREATE TABLE`/`DROP TABLE` attempt, `check_query_timeout`'s multi-second `WAITFOR DELAY` probe, and (with more than one data source) `check_tables_in_assigned_sources`'s full catalogue reflection | Only when an operator explicitly presses the panel's "deep checks" button (confirmation dialog first) — never automatically, never on a timer. `python -m scripts.verify_deployment` (the CLI) still runs every check, deep included, every time it is invoked by hand or in CI | Not time-based — opt-in per click. Cached separately from the non-deep result under the same `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` |
| Admin panel — schema drift (`GET /admin/schema-drift`, `schema_data.drift.check_schema_drift`) | A full catalogue reflection: `get_table_names` + `get_columns` for every table of every schema, on each source a table lives in; plus, only with more than one source and only when some table is missing from its assigned source, one `INFORMATION_SCHEMA.TABLES` query per other source (the "found in ..." hint) | Once when the admin panel is opened, and again only on that card's own refresh button — not on the 30-second auto-refresh. Same cache/`?refresh=1` behaviour as above | `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` |
| Admin panel — every other card (audit summary, query cache stats, maintenance mode, feedback, keys, access requests, dimension-vocabulary status, per-analyst usage, auth failures) | No direct warehouse query — these read the audit log, the application database, or in-process bookkeeping | Every 30 seconds (`AUTO_REFRESH_MS` in `web/admin/main.js`) while the panel tab is visible, plus on open and on each card's own refresh button | Not warehouse-relevant; listed here only to be explicit about what the 30-second timer *does* still touch |
| Dimension-vocabulary refresh (`retrieval/dimension_vocabulary.py`) | A `DISTINCT`-style scan of one configured dimension column, as one single-table statement on the source that column's table routes to (a table listed under several sources is read from one copy: the default source if the table is there, else the first in `datasources.yaml` order) | At start-up when `DIMENSION_VOCABULARY_WARM_ON_STARTUP=true` (the default; set `false` for a start-up that must not touch the warehouse); afterwards lazily, at most once per column per TTL, triggered by the first `/query` request that needs a stale-or-missing entry (a background, non-blocking refresh — the triggering request itself is served from whatever was cached, stale or not) | `DIMENSION_VOCABULARY_TTL_SECONDS` (default `3600`), `DIMENSION_VOCABULARY_WARM_ON_STARTUP` |
| Relationship-map schema inspection (`database/relationship_map.py`) | Nothing, in a running deployment. The module reflects foreign keys only if no `project_config/relationships.yaml` exists and `AUTO_DISCOVER_SCHEMA=true`, but the server, the API and the CLI do not import it (the relationships in the prompt come from `schema.yaml`), so the setting causes no warehouse traffic today | Not applicable. Were it wired in, it would run at most once per process lifetime (cached in memory; never on a timer) and read `DB_CONNECTION_URL` itself, not `DB_PASSWORD` or `datasources.yaml` | `AUTO_DISCOVER_SCHEMA` (default `false`) |
| Operator scripts, run by hand: `scripts/assign_datasources.py` | Two metadata queries per data source, `INFORMATION_SCHEMA.TABLES` and `INFORMATION_SCHEMA.COLUMNS`, names only, no rows | Once per run | Not time-based. `--check` runs the same two queries |
| Every `/query` request that reaches SQL execution (`database/executor.py`) | The generated, guard-validated `SELECT` itself, inside a transaction | Once per end-user query — this is the real workload the application exists to serve, not overhead | `MAX_CONCURRENT_REQUESTS`, `RATE_LIMIT_*` bound how many of these can be in flight/arriving at once |

## 13. `pool_pre_ping`: what it costs, and when turning it off is reasonable

`DB_POOL_PRE_PING` (default `true`) is what makes checking a connection out
of the pool run a liveness probe first. As of the idle-aware-ping change,
that probe is no longer unconditional: `database/pool_ping.py` tracks how
long each pooled connection has actually sat unused (via SQLAlchemy's
documented connect/checkin/checkout pool events), and only runs `SELECT 1`
on checkout once a connection has been idle for at least
`DB_POOL_PING_IDLE_SECONDS` (default `60`). A connection reused sooner than
that is handed to the caller unprobed. This applies to both the warehouse
engine (`database/connection.py`) and, when `APP_DB_URL` points at a real
server rather than the SQLite fallback, the application-database engine
(`appdb/engine.py`) — the same setting, the same mechanism, the same
trade-off, on both.

Before this change, the probe ran on *every* checkout regardless of idle
time — a DBA-visible `SELECT 1` scaling with checkout rate (i.e. with query
volume) rather than with how often a connection actually needed
re-verifying. Setting `DB_POOL_PING_IDLE_SECONDS=0` restores that old,
simpler guarantee for an operator who wants it back — pinging literally
every checkout — with one narrow, rarely-observable exception: SQLAlchemy's
own `pool_pre_ping=True` never pings the very first checkout of a
brand-new physical connection (a one-time internal "fresh" flag,
independent of idle time), while `DB_POOL_PING_IDLE_SECONDS=0` here pings
even that one, since it honours "every checkout" literally. See
`database.pool_ping.install_idle_aware_ping`'s docstring for the full
detail; the difference shows up at most once per physical connection, on
its very first use, never again after.

**Leave `DB_POOL_PRE_PING` on (the default)** unless you have a specific
reason not to: a connection that went stale while idle in the pool (the
SQL Server side closed it, or a firewall/load balancer idle-timed it out)
is silently discarded and replaced instead of failing a real query. A
failed idle-aware probe raises SQLAlchemy's own `InvalidatePoolError` --
the same exception plain `pool_pre_ping=True`'s own dialect-level ping
raises on failure -- which makes the pool invalidate and transparently
replace *every* pooled connection, not just the one that was probed,
before the caller's own statement runs. That matters for the common real
cause of a failed ping: a server restart or a firewall/load-balancer
dropping every idle connection at once is one failure away from full
recovery, not one failure per pooled connection discovered one-by-one on
its own next checkout — the caller never sees any of it either way.

**Turning `DB_POOL_PRE_PING` off** removes the probe entirely, at every
idle threshold. A connection that went stale is then only discovered when
a real query is sent through it, which fails once and is transparently
retried on a fresh connection — a reasonable trade specifically when
`DB_POOL_RECYCLE_SECONDS` is set comfortably below whatever idle timeout
the network path to the warehouse (SQL Server itself, a firewall, a load
balancer) actually enforces, so a connection is proactively recycled
before it would go stale from sitting idle in the first place. Confirm
that idle timeout with the DBA before turning this off; without a matching
recycle margin, turning it off trades a quiet steady cost for occasional,
noisier first-query-after-idle failures.

**Tuning `DB_POOL_PING_IDLE_SECONDS` instead of turning pre-ping off** is
usually the better first move if the goal is just to cut down the `SELECT
1` volume: it keeps the safety net (a stale connection is still caught and
replaced before a real query sees it) while making the probe frequency
track how bursty traffic actually is rather than raw checkout count. Raise
it if analysts ask questions in noticeably spaced-out bursts; lower it
(down to `0`) if the warehouse's own idle timeout is aggressive enough that
even a short gap can leave a connection stale. Full detail:
`config.Settings.db_pool_pre_ping` and `config.Settings.db_pool_ping_idle_seconds`.

## 14. Disk activity on trivial queries — what to ask the DBA about

If the DBA reports meaningful disk I/O correlated with connections this
application opens or with plain `SELECT` traffic, and the query volume
above does not obviously account for it, these are database-side settings
worth checking — facts about what they do, not a diagnosis of this
specific deployment:

- **`AUTO_CLOSE` on the database.** When on, SQL Server closes the database
  completely (and releases its resources) once the last connection to it
  ends, then reopens it — a cold-start cost — on the next connection. A
  connection pool that lets its connections idle out and reconnect (or an
  `AUTO_CLOSE` interval shorter than this application's real idle gaps)
  can turn what looks like a trivial query into a full close/reopen cycle.
  Checking and, if appropriate, disabling `AUTO_CLOSE` on the warehouse
  database is a DBA-side change, not something this application can see or
  control from a connection string.
- **Login auditing or login triggers.** A `SERVER AUDIT`/login trigger that
  runs its own logic (a lookup, a write, a check) on every new login event
  adds real work per new connection, independent of what that connection
  goes on to query. This compounds with a small `DB_POOL_RECYCLE_SECONDS`
  or a low `pool_size`/high churn: more new connections means more login
  events means more trigger executions. Whether one is configured, and
  what it does, is visible from the SQL Server side, not from this
  application.

Neither of these is asserted to be present on any specific deployment —
they are the two most common DBA-side explanations for "disk activity on
a query that should be nearly free," offered so the DBA conversation
starts with a concrete question instead of a guess.

## 15. Handing this to the DBA directly

`docs/dba/` is a self-contained kit for the DBA to run themselves:
[`docs/dba/warehouse-load-diagnostics.sql`](dba/warehouse-load-diagnostics.sql)
(read-only DMV/catalog queries only — no writes, no `DBCC` that changes
state, no trace creation) covering `AUTO_CLOSE`, this application's own
sessions/requests (filtered by `program_name = 'local-sql-agent'`, i.e.
`DB_APPLICATION_NAME`), two-snapshot file-I/O deltas, top queries by
physical reads, and login-trigger/audit checks, plus
[`docs/dba/README.md`](dba/README.md) explaining when to run each section
and how to read the result — including the same point §14 makes above:
`SELECT 1` reads no data pages, so disk activity that coincides with it
usually comes from something else. Hand every configured source's
server, in turn, to its own DBA if they differ — see §16 below.

## 16. Several data sources (optional)

Every step above assumes the default shape: one warehouse connection,
`DB_CONNECTION_URL` plus `DB_PASSWORD`. Add `project_config/datasources.yaml`
when this deployment queries more than one database (a second database on the
same server, another SQL Server instance, a partner's warehouse on its own
box), or when you simply want host, database and driver written in a reviewable
file instead of one long URL in `.env`.

This section is the order to do it in, with the commands. The reasoning, the
exact routing rules and what was rejected are in `docs/design/DATASOURCES.md`;
each step links to the part you need.

| Step | What | Where |
|---|---|---|
| 1 | Choose: two sources, or one source with a multi-part `db_schema` | §16.1 |
| 2 | Describe each connection, put each raw password in `.env` | §16.2 |
| 3 | Write each table's `datasource:` with `assign_datasources.py`, replace `schema.yaml`, fix the columns it reports missing | §16.3 |
| 4 | Give each source a `description:` and `keywords:` | §16.4 |
| 5 | Preflight and restart | §16.5 |
| 6 | Size `PROMPT_RETRIEVAL_TOKEN_BUDGET` with `prompt_budget.py` | §16.6 |
| 7 | `nolock: true`, only where the DBA requires it | §16.7 |
| 8 | Read the admin panel's drift and vocabulary cards | §16.8 |
| 9 | Watch which source answers | §16.9 |

### 16.1 Two sources, or one

Two databases on the same server are a supported configuration as **two
sources** (same `host`, different `database`). Each source has its own
connection pool and login, and one query runs on exactly one source. If
questions must **join across** the two databases, use **one** source instead
and give the second database's tables a multi-part `db_schema`
(`db_schema: "OtherDb.dbo"`), which SQL Server joins with a three-part name.
A database on another server is always its own source. The statement that
needs tables of two sources is refused (§16.10).

### 16.2 Describe the connections and set the passwords

1. Copy `project_config.example/datasources.example.yaml` to
   `project_config/datasources.yaml` and describe each real source:

   ```yaml
   default: sales
   datasources:
     sales:
       host: 10.0.0.5
       database: SalesDW
       username: nlq_reader
       password_env: DB_PASSWORD_SALES
       options:
         TrustServerCertificate: true
     inventory:                      # same server, its own pool
       host: 10.0.0.5
       database: InventoryDW
       username: nlq_reader
       password_env: DB_PASSWORD_INVENTORY
   ```

   `port` defaults to 1433 and `driver` to `ODBC Driver 18 for SQL Server`.
   For Windows authentication write `trusted_connection: true` and leave out
   the login and `password_env`; `username_env` names a variable that holds the
   login instead. The file is versioned like `schema.yaml`, so it must never
   hold a password: a `password:` key is refused, and so is a credential key
   under `options`. `default` is required once there is more than one source.
2. Set each source's **raw** password in `.env`, under the variable
   `password_env` names:

   ```
   DB_PASSWORD_SALES=p@ss/w:rd#1
   DB_PASSWORD_INVENTORY=...
   ```

   Write the password exactly as the database knows it and do **not**
   URL-encode it: the application builds the connection URL and escapes every
   special character itself. `DB_CONNECTION_URL` and `DB_PASSWORD` are then
   unused and no longer required. A variable that is missing or empty, a
   placeholder host, database, login or password, and an application
   database (`APP_DB_URL`) that points at any of the sources are all refused
   at start-up, naming the source and never the value.
3. Create the read-only login on **every** source's server
   (`docs/db-hardening.md`).

A source written for 6.1 or 6.2 with `url_env: DB_URL_MAIN` (a variable
holding a complete URL, password percent-encoded by hand) keeps working and
may sit beside structured sources; §16.11 says how to move it. Changing
`datasources.yaml` needs a restart: it is deployment topology, not one of the
nine files the admin panel's versioned config bundle covers.

### 16.3 Write each table's `datasource:`

A table with no `datasource:` belongs to the default source, and fails there
(`Invalid object name ...`) when it is really in another database. A table
that exists with the same shape in several sources (a date dimension replicated
into each database) takes a list, `datasource: [sales, inventory]`: distinct
names, each a configured source, in the same letter case as in
`datasources.yaml`.

Do not write these by hand. From the repository root, with the server's
environment active (the sources' passwords in `.env`):

```bash
python scripts/assign_datasources.py
```

It reads the tables, views and columns of every source (two
`INFORMATION_SCHEMA` queries per source, through the application's own
read-only engines; names only, nothing is written to a database), matches
every `schema.yaml` table to the sources that have it and writes
`schema.with_datasources.yaml` next to `schema.yaml`: your file with every line
and comment kept and one `datasource:` line per table (`[sales, inventory]` for
a table found in both, `# not found in any data source` under a table found in
neither). `schema.yaml` itself is never changed, and `--output PATH` writes the
proposal elsewhere. The report lists, per source, the tables found only there,
the shared tables, the tables found nowhere, the tables whose current
`datasource:` disagrees with what was found, and the columns `schema.yaml`
names that the database does not have (a column the read-only login cannot see
through `INFORMATION_SCHEMA` is reported missing too, so check the `DENY` grants
of `docs/db-hardening.md` before deleting one).

Then:

1. Review `schema.with_datasources.yaml`: look at the tables found nowhere and
   at every `[A, B]`, because a list asserts that the table has the same shape
   in each source.
2. Keep a copy of the current file, then replace it with the proposal:

   ```bash
   cp project_config/schema.yaml project_config/schema.yaml.bak
   mv project_config/schema.with_datasources.yaml project_config/schema.yaml
   ```

   (PowerShell: `Copy-Item project_config\schema.yaml project_config\schema.yaml.bak`,
   then `Move-Item -Force project_config\schema.with_datasources.yaml project_config\schema.yaml`.)
   `schema.yaml` is the guard's allowlist, so a change to it takes effect at
   the next restart whichever way it is made (by hand, or through the admin
   panel's draft and approval).
3. Fix the columns the report lists as missing. Its last section reads
   `== columns listed in schema.yaml that the database does not have: N ==`, one
   `Table [source]: Column, ...` row per table and source. A column that stays in
   `schema.yaml` without existing in the database is still allowed by the guard,
   so a question that uses it fails when the query runs (the admin panel shows it
   as *فقط در schema.yaml*, §16.8). For each row: delete the column from
   `schema.yaml`, correct its spelling, or, if it exists but the read-only login
   cannot see it through `INFORMATION_SCHEMA`, ask the DBA about the `DENY`
   grants (`docs/db-hardening.md`) before deleting it. Run the script again
   until the section reads `: 0 ==`. The exit code does not tell you: only the
   `datasource:` mismatches in step 4 change it.
4. Run `python scripts/assign_datasources.py --check`. It writes nothing,
   prints `CHECK OK: every table's datasource: matches the databases` and exits 0
   when every `datasource:` matches what was found (a table with none counts as
   being on the default source); otherwise it prints
   `CHECK FAILED: N table(s) disagree with the databases` and exits 1. Put it in
   the deploy pipeline to keep the assignments honest. Exit code 2 means a
   source's catalogue could not be read (the run stops rather than guess,
   because an unreadable source would make a shared table look single),
   `schema.yaml` could not be edited, or the output would not validate.

If the same table name exists in several schemas (`sales.Customer` and
`ref.Customer`), give each its own qualified `schema.yaml` key instead of
colliding on `Customer:` (`docs/design/TABLE-NAMES.md`); this applies with one
source too. Give every table a `db_schema`, even a bare-keyed one: a table with
no qualifier configured at all cannot have its schema checked by the guard.

How a statement that reads a shared table is routed (the intersection rule)
is in `docs/design/DATASOURCES.md`, "Tables that live in several sources" and
decision DS2.

### 16.4 Help the router with `description:` and `keywords:`

With more than one source each question is routed to **one** source before
the prompt is built, and the model is shown that source's tables only. Nothing
changes with one source. Two optional keys per source help; both are about what
the model sees, never about where a statement runs:

```yaml
datasources:
  sales:
    description: Sales warehouse           # printed above this source's schema block
    keywords: [revenue, invoice, فروش]     # whole words or phrases that mark a question
  inventory:
    description: Inventory warehouse
    keywords: [stock level, reorder, موجودی, انبار]
```

`keywords` match the question as whole words after Persian/Arabic letter
folding, digit folding, ZWNJ removal and case folding: `stock` does not match
`stockholder`, and a plural or a prefixed form is another word, so list each
form you expect. The value is a list of non-empty strings with no repeats
(two spellings that fold to the same text are a repeat); anything else stops
the server at start-up, naming the source. Without a keyword hit the choice
follows the conversation (a follow-up stays on the previous question's
source), then the tables retrieval finds for the question, then the default
source. If the model answers `OUT_OF_SCOPE` the request is retried **once**
with the next candidate (one extra model call at most); the CLI does not
retry. The full rules are in `docs/design/DATASOURCES.md`, "Choosing a source
per question".

### 16.5 Preflight and restart

```bash
python scripts/verify_deployment.py
```

Every database check runs once per source (`Database connectivity [sales]`,
`Database connectivity [inventory]`, then the same for `Login is read-only`,
`Row cap` and `Query timeout`). Two checks are specific to several sources:

- `Tables map to data sources` confirms every table's `datasource:` (if any)
  names a configured source.
- `Tables are in their data source` fails, with the exact line to write, for a
  table whose columns are all missing from the source `schema.yaml` assigns it
  to while another source has it: `stock_dim.Broker: not in sales, found in
  inventory — set datasource: inventory`. It reads every catalogue, so the
  admin panel runs it only under "deep checks" (§16.8).

`0 failed` before you go on (§3). Restart the server, and confirm the
`Prompt path for data source '<name>'` line for each source (§5).
`GET /health` then names every source in `database_detail`:

```
sales: SELECT 1 succeeded; inventory: SELECT 1 succeeded
```

and `database` is `true` only when **every** source answered: one source being
down flips the whole field to `false`, with the detail naming which one.

### 16.6 Size `PROMPT_RETRIEVAL_TOKEN_BUDGET`

The budget (default `6000`) is compared with **each source's own** static-prefix
estimate, not the sum: a source under it uses its cacheable static prefix (fast
warm requests through the model server's prefix cache), a source over it uses
retrieval restricted to its tables. The estimator is `len(text) // 4`, which
undercounts Persian by roughly 15%. Do not work that out from the log lines;
run, from the repository root with the server's environment active:

```bash
python scripts/prompt_budget.py
```

It builds each source's prefix as the server does, asks the model endpoint for
the real `prompt_tokens` (one chat completion per source with `max_tokens=1`),
reads the context length from `GET /models`, checks that each source's real
tokens plus room for the question plus `LLM_NUM_PREDICT` fit it, and prints the
exact line for `.env`:

```
PROMPT_RETRIEVAL_TOKEN_BUDGET=6000
```

with each source's path now and after. Put that line in `.env` and restart.

- Run it before the service is opened to users, not in a busy hour: counting
  real tokens prefills each prefix once, which warms the model server's prefix
  cache (a cold prefill of a large prefix can take about a minute, hence
  `--timeout`, default 300 seconds).
- `--no-model` never contacts the endpoint (give `--context-length N` for the
  fit check); `--json` prints one JSON document; `--headroom` (default 10%),
  `--round` (default 500) and `--question-room` (default 2000) tune the
  recommendation. Pass `--context-length` too when the model server divides its
  context between parallel slots, because it may accept less per request than
  it reports.
- A source that cannot fit the context window is left out of the
  recommendation and must stay on the retrieval path; the exit code is then 1.
  Moving some of its tables to another source usually helps more than raising
  the budget.
- Exit code 2 means the configuration could not be loaded or an option is
  invalid. No API key or URL credential is ever printed, and an endpoint that
  is not trusted is sent nothing unless `LLM_ALLOW_REMOTE` is true.

How the numbers are derived, and the same reasoning by hand, is in
`docs/design/DATASOURCES.md`, "One path per source, and the token budget".

### 16.7 Reading with `WITH (NOLOCK)`

If the DBA requires every table read by this application to carry
`WITH (NOLOCK)`, set `nolock: true` on that source (both the structured and the
`url_env` form accept it; it defaults to `false`). Just before a statement is
sent to that source, ` WITH (NOLOCK)` is inserted after each physical table
reference (after the alias, when there is one) in `FROM`, every `JOIN`,
subqueries, CTE bodies and each branch of a `UNION`. The rest of the text is
not touched. This applies to every statement the executor sends to that
source: the generated query, and also the value resolver's and the vocabulary
prefetch's reads. CTE names, derived tables, table-valued functions, `#temp` tables, `@table`
variables, `INFORMATION_SCHEMA` and `sys` objects, and tables that already have a
`WITH (...)` hint are left as they are (`database/table_hints.py` lists
everything). A statement that cannot be parsed, or whose rewrite does not pass a
second parse, is sent unchanged and one warning naming the reason is logged. The
audit trail's `generated_sql` is the validated SQL without hints.

**`nolock` belongs to one source and does not spread to the others.** The flag
is read from the definition of the source a statement is routed to. Setting
`nolock: true` on `sales` leaves `inventory`, and every other source, reading
with ordinary locking; a source that does not mention it is `false`. There is no
global switch and nothing is inherited from the default source, so a DBA rule
that covers several servers is written once under each source. With two sources
and the hint on the first only:

```yaml
default: sales
datasources:
  sales:
    host: 10.0.0.5
    database: SalesDW
    username: nlq_reader
    password_env: DB_PASSWORD_SALES
    nolock: true                    # statements routed to sales get WITH (NOLOCK)
  inventory:
    host: 10.0.0.6
    database: InventoryDW
    username: nlq_reader
    password_env: DB_PASSWORD_INVENTORY
                                    # no nolock: statements routed here are sent as validated
```

A statement routed to `sales` reaches the server with ` WITH (NOLOCK)` after each
table; one routed to `inventory` does not. If the DBA of the second server
requires it too, add `nolock: true` under `inventory` as well. The audit
record's `datasource` says which source a statement ran on (§16.9). A
deployment without `datasources.yaml` cannot use the hint at all, because the
one implicit source always reads as `false`: describe the single database in a
`datasources.yaml` (one source; `default` is optional then) to set it.

Table hints are T-SQL, so start-up is refused if a source sets `nolock: true`
and `SQL_DIALECT` is not `tsql`; anything but a YAML `true` or `false` is
refused naming the source. **`NOLOCK` allows dirty reads**: a query can see
rows another transaction has not committed (and may roll back), and
occasionally a row twice or not at all while pages split. It is the operator's
decision, made per source, and needs a restart like the rest of
`datasources.yaml`. To let the DBA tell this application's sessions apart in
`sys.dm_exec_sessions`, every connection carries `APP=<DB_APPLICATION_NAME>` as
its `program_name`; a source can use another name with `options: {APP: ...}` (or
`application_name:`). Why the hint is inserted rather than produced by
regenerating the SQL: `docs/design/DATASOURCES.md`, decision DS4.

### 16.8 Reading the admin panel

Open `http://<ui-host>/admin/` with a key that holds the capabilities of §1.1.
Two cards matter for data sources, and a third helps when something looks off.
Both are read-only and apply nothing by themselves. The drift card reads
catalogues, so it loads when the panel opens and on its own refresh button, not
on the 30-second timer (its result is cached for
`ADMIN_EXPENSIVE_CACHE_TTL_SECONDS`); the vocabulary card reads in-process
bookkeeping and follows the timer.

**Schema drift** (`GET /admin/schema-drift`; operations or security). It
compares `schema.yaml` with each source's live catalogue and shows, in this order:

- *جدول در منبع دادهٔ دیگری است*: a table every one of whose columns is missing
  from a source it is assigned to while another source has it, with the
  source it is missing from, the source where it was found and the value to
  write: `datasource: inventory`, or `datasource: [sales, inventory]`. This is
  the misplaced-table finding of §16.5, shown at any time. It costs one
  `INFORMATION_SCHEMA.TABLES` query per other source, and only when such a
  table exists.
- *فقط در انبار داده*: tables and columns the warehouse has that `schema.yaml`
  does not. They cannot be queried, because the guard refuses anything outside
  the allowlist.
- *فقط در schema.yaml*: tables and columns `schema.yaml` lists that the warehouse
  no longer has. A query that uses one fails when it runs.
- *نوع ستون تغییر کرده*: a column whose type changed since the previous time
  the check ran. The first run has no baseline and says so.

A table listed under several sources is compared on each of them, and a
column missing from one copy reads `Table.Column [source]`. Fixing a finding
is an edit of `schema.yaml`: an operations key proposes it as a draft, a
security key approves it, and the guard picks it up at the next restart.

**Dimension vocabulary** (`GET /admin/vocabulary`; operations or security to
read, operations to refresh). One row per prefetched dimension column
(`prefetchable_columns` in `schema.yaml`), with `table.column`, a status, the
number of values, the time of the last refresh and a «بازخوانی» button:

- *تازه*: fetched within `DIMENSION_VOCABULARY_TTL_SECONDS` (default `3600`).
- *کهنه*: older than that. It is still used, and a background refresh is
  triggered by the next question that needs it.
- *هرگز*: never fetched. A question that names this dimension cannot be
  checked against its values, so the answer is not filtered by the value and
  the analyst gets a warning saying so, until a refresh succeeds. Such a
  question also starts a background refresh itself (after a failed attempt, at
  most one automatic retry a minute), so a transient failure mends on its own;
  «بازخوانی» tries at once. With `DIMENSION_VOCABULARY_WARM_ON_STARTUP=true`
  (the default) this should only appear after a failed warm-up, which is logged
  and does not stop the server.
- *آخرین تلاش ناموفق*: the last attempt, automatic or manual, failed. Press
  «بازخوانی» and read the message: it is the database's own error for the
  source that holds the table.

With several sources a column is read from one copy of its table, the default
source when the table lives there, otherwise the first source listed in
`datasources.yaml`. A hall or product added to the warehouse and never
refreshed makes value resolution miss silently, which is why the card exists.

**Deployment checks** (`GET /admin/health/checks`; `admin`). The same checks as
§3, once per source. The panel leaves out three on its own (the rolled-back
`CREATE TABLE`, the `WAITFOR` probe, and `Tables are in their data source`)
unless an operator presses "deep checks" and confirms; the command line always
runs all of them.

### 16.9 Watch which source answers

Each audit record carries `datasource` (where the generated SQL routed) and
`datasource_selection` (`chosen`, `reason`, `candidates`, `fallback_from`;
`null` with one source and for an answer served from the cache). `reason` is
`keyword`, `session`, `retrieval` or `default`. To find the questions that
needed the one retry, which usually means a keyword is missing:

```bash
grep '"fallback_from": "' logs/audit_log.jsonl
```

`prefix_cache_hit` in the audit `llm` block is measured against the chosen
source's own prefix estimate.

### 16.10 A query that spans two sources

A statement is refused before it opens a connection (the guard reason
`cross_datasource`, with an analyst-facing Persian explanation asking for the
question to be split) only when **no** source has every table it reads. The
refusal names every source with its tables and says which tables are available
in several. This is expected, not a bug to investigate: combining sources in
one answer is not supported yet (`docs/design/DATASOURCES.md`, "Roadmap"). A
table listed under several sources counts for each of them, so a statement
mixing it with the tables of one source is not refused.

### 16.11 Moving a `url_env` source to the structured form

A source written for 6.1 or 6.2 keeps working unchanged. To move it: copy the
host, port, database, login and query parameters (`TrustServerCertificate=yes`
and the like, which become `options`) from the URL into the YAML; put only the
raw, un-encoded password in a new `DB_PASSWORD_*` variable; replace `url_env`
with `password_env`; restart and run `python scripts/verify_deployment.py`.
Remove the old `DB_URL_*` variable afterwards.

## 17. Upgrading from 6.0 to 6.7

One checklist for an installation that is running 6.0.0 and is moving to
6.7.0. It puts the **Upgrading** notes of 6.0.1 to 6.6.0 in the order to do
them, and adds what 6.7.0 brings that you will want to use (6.6.1 and 6.7.0
have no **Upgrading** notes of their own); `CHANGELOG.md` has each release's
full text. From 5.x, do the 6.0.0
notes first: copy `prompts/system_prompt.md` to
`<PROJECT_CONFIG_DIR>/system_prompt.md` before the first start (the server
refuses to start without it), and check a relative `PROJECT_CONFIG_DIR`,
which is now resolved against the repository root.

Steps 1 to 4 and 9 apply to every installation. Steps 5 to 8 are each
optional: do the ones that fit (a password move, a key file, a UI on another
origin, several databases). Step 11 is recommended, and comes last on purpose.

1. **Back up** `.env` and `project_config/`. Both are outside the repository.
2. **Pull, then re-run the install.** Every upgrade starts with
   `pip install -r requirements.lock` (never `requirements.txt`; the pins keep
   `sqlglot`, which the SQL guard depends on, from changing unreviewed) (6.4.1:
   python-dotenv 1.2.4, so a `.env` saved as UTF-8 with a byte-order mark
   loads its first variable; 6.3.0: urllib3 2.8.0).
3. **Run the preflight before restarting**, in its own process:
   `python scripts/verify_deployment.py` (§3). Two releases made the server
   refuse input it used to accept silently, and this is where each refusal shows
   up with its cause:
   - *6.3.1, a YAML key written twice.* `[datasources.yaml] is not valid YAML:
     duplicate key 'datasources' (first on line 12, again on line 21)`. A
     duplicate in `datasources.yaml` or `schema.yaml` stops the server; one in
     another file fails when that file is first read; `relationships.yaml` is
     skipped with a warning. Remove the duplicate. The later one was the one in
     effect, so keep that block's content if it is what you meant.
   - *6.4.0, a `.env` line python-dotenv cannot use.* `Invalid configuration:`
     with each line number and variable name. A multi-line `API_KEYS_JSON` must
     be wrapped in single quotes with no apostrophe inside, or moved to
     `API_KEYS_FILE` (step 6); a variable assigned twice with different values
     must lose the stale one (the later one was in effect).
   - *6.4.0, a repeated field inside one key object.* `Invalid API key
     configuration:`. Delete the repeat (a pasted second `denied_columns`
     used to replace the first silently).
4. **Check `schema.yaml`** (6.2.0). A generated query that names the wrong
   schema is now refused, and the model's retry corrects it, so nothing needs
   doing for unique table names. If the same table name exists in several
   schemas, key each one with its schema (`sales.Customer:`, `ref.Customer:`).
   Set `db_schema` on every bare-keyed table, or its schema cannot be checked
   (`docs/design/TABLE-NAMES.md`).
5. **Choose how the database password is given** (6.3.0, optional). With one
   database, remove the password from `DB_CONNECTION_URL` and put it, raw, in
   `DB_PASSWORD`; a raw password written inside the URL is not detected or
   re-encoded. To move a `url_env` data source to the structured form, follow
   §16.11.
6. **Move the keys to a file** (6.4.0, optional but recommended for more than
   one key): `cp project_config.example/api_keys.example.json
   project_config/api_keys.json`, replace each `key_sha256` with the digest
   `scripts/issue_api_key.py` printed (6.6.1: an entry still holding the
   template's placeholder stops start-up), set
   `API_KEYS_FILE=project_config/api_keys.json`, **remove** `API_KEYS_JSON`
   (setting both is refused), and run the preflight again.
7. **Set the bind address and the CORS origin** if they matter (6.0.2).
   `python -m api` listens on `127.0.0.1` unless `API_HOST=0.0.0.0` is set,
   and a UI served from anywhere other than `localhost:8080` needs
   `CORS_ALLOWED_ORIGINS` set to that origin (for example
   `http://172.16.101.42:8077`).
8. **Several databases only** (6.1.0, 6.3.0, 6.5.0, 6.6.0): do §16 in its order,
   which is the combined form of these notes: describe the sources
   (`datasources.yaml`, one `DB_PASSWORD_*` per source) and create the
   read-only login on every server (6.1.0); run
   `python scripts/assign_datasources.py` and replace `schema.yaml` with
   `schema.with_datasources.yaml` after copying the old one aside, then fix
   the columns its report lists as missing (6.5.0); add `description:` and `keywords:` to
   each source (6.5.0, optional); run `python scripts/prompt_budget.py` and set
   the `PROMPT_RETRIEVAL_TOKEN_BUDGET` it prints (6.6.0). If the DBA requires
   `WITH (NOLOCK)`, set `nolock: true` on that source, and on each other
   source that needs it: the flag is per source (6.5.0, §16.7).
9. **Restart** (every change above, including `datasources.yaml` and
   `schema.yaml`, takes effect at a restart). Then run the preflight once more
   with `VERIFY_API_KEY` set to an analyst's raw key (§3), and read the
   start-up log (§5): the provenance banner, `CORS allowed origins`, and with
   several sources one `Prompt path for data source` line each.
10. **After the first day**, read the new audit field (6.5.0): a reader of
    `audit_log.jsonl` that does not know `datasource_selection` can ignore
    it. And note what changed on screen, because analysts will ask: the SQL
    shown in a conversation is laid out in a fixed style (6.5.0; display only,
    the statement that ran is unchanged), and a refused statement can be
    opened with the same layout.
11. **Start measuring accuracy** (6.7.0, optional but recommended). 6.7.0
    adds the tools that turn real usage into an evaluation set and a release
    gate: `python scripts/harvest_golden.py` (candidates from the audit log),
    `python scripts/golden_sheet.py export` and `import` (the analysts' review
    in Excel), `python -m eval.cli verify --accept` (run and activate the
    cases) and `python -m eval.cli run --live --reference live` (execution
    accuracy against the reference SQL, run in the same session on the same
    data). They do not exist before 6.7.0, so the first baseline is recorded
    on 6.7.0 itself, once the audit log holds real questions; from then on you
    record a baseline on the running version *before* each upgrade and compare
    after it (§18, which has every command). A baseline file written by an
    older version still loads, but cannot be compared with a
    `--reference live` run: the tool refuses and says how to record it again.
    Nothing else in 6.7.0 changes how the server runs.

Nothing in 6.0.1 (`DB_POOL_PING_IDLE_SECONDS` defaults to `60`; `0` keeps
pinging on every checkout), 6.3.2 (one warning per key instead of one per key
read), 6.6.1 (the template of step 6) or the CI change of 6.7.0 (the
`requirements.lock` you install is now also what CI tests) needs an action of
its own.

## 18. Measuring accuracy on the real warehouse, and gating an upgrade on it

Until a deployment has a golden set built from its own questions, it has no
measured accuracy: the committed `eval_data.example/` is made-up data and
says nothing about your warehouse. This section builds the real set from the
audit log and turns it into the check you run before every upgrade.

**The set lives only on the server.** `eval_data/` is git-ignored and holds
real questions, the SQL that answers them and, after step 4, real result
rows. Create it, read it and run the gate on the server; do not copy
`candidates.jsonl`, `review.csv`, `golden.jsonl`, their `.bak` files or a
JSON report (`--out`, `--save-baseline`: they carry each case's question and
SQL) off it. What the commands below **print** is counts and case ids only,
the same stance as `scripts/analyze_audit_log.py`, and is safe to paste into
a chat; `--include-examples` on the harvest is the one opt-in that prints a
few verbatim questions.

### 18.1 Build the set (once, then top it up)

```bash
# 1. Harvest ~150 candidates from the audit log (stratified by data source,
#    outcome and language; de-duplicated with the Persian/Arabic folding).
python scripts/harvest_golden.py --n 150
#    -> eval_data/candidates.jsonl, status "pending_review". The model's own
#       SQL is only a proposal. Refuses to overwrite without --force.

# 2. Give the analysts a spreadsheet (UTF-8 with BOM: Persian opens correctly).
python scripts/golden_sheet.py export
#    -> eval_data/review.csv. Each analyst fills `verdict` (correct / wrong /
#       skip) and, where it is wrong, `correct_sql`. See
#       eval_data.example/golden_review_template.csv for a filled-in example.

# 3. Bring the verdicts back; every SQL goes through the guard.
python scripts/golden_sheet.py import
#    -> eval_data/golden.jsonl, status "reviewed". Problems are listed by
#       spreadsheet row; fix the cell and import again (rows already
#       imported are recognised).

# 4. Run each reviewed case's reference SQL read-only, record its rows, and
#    activate the ones that held up.
python -m eval.cli verify --golden eval_data/golden.jsonl            # look first
python -m eval.cli verify --golden eval_data/golden.jsonl --accept   # then activate
```

`verify` goes through the application's own executor (routing, `NOLOCK`,
timeouts, always-rolled-back transaction), so it sees exactly what a user's
query sees. It reports a guard rejection, a database error, an empty result
where `expect` says `success`, and a result that hit the row cap; only cases
without a problem move to `active`, and the file is replaced atomically with
the previous version kept as `golden.jsonl.bak`. Cases with any other status
(`pending_review`, `pending_expected`, `reviewed`) are skipped by
`eval.cli run`, so a half-reviewed set is safe to keep in the file.

Aim for questions that cover each data source and each kind of question
(counts, rankings, date filters in Jalali, empty answers, and a few that
should be declined as out of scope). Give a case a `datasource`
(`sales`, `inventory`, ...) in the sheet's column when the answer lives in
one: that is what makes source-selection accuracy measurable.
Re-harvest later with `--exclude eval_data/golden.jsonl` to add questions
that are not already in the set.

### 18.2 Before an upgrade: the release gate

The warehouse changes every day, so a result recorded last month is no longer
the answer to "trades yesterday". The gate therefore runs each case's
`expected_sql` in the same run, against the same data, and compares the two
results (`--reference live`). Record the baseline once on the version you run
today, upgrade, and run the same command with `--baseline`:

```bash
# On the current version, before upgrading (once per baseline):
python -m eval.cli run --live --reference live \
    --golden eval_data/golden.jsonl --save-baseline eval_data/baseline.json

# ... upgrade ...

# On the new version:
python -m eval.cli run --live --reference live \
    --golden eval_data/golden.jsonl --baseline eval_data/baseline.json
```

Exit code `0` is "no regression versus the baseline", `1` is a regression
(accuracy dropped by more than `EVAL_MAX_ACCURACY_DROP_PCT` points, latency
p95 rose by more than `EVAL_MAX_LATENCY_P95_INCREASE_PCT`, or more guard
rejections than `EVAL_MAX_GUARD_REJECTION_INCREASE` allows). Do not go
live on a `1` without understanding it. A baseline can only be compared with
a run that used the same `--reference`, and live with live: an offline
baseline, or one recorded with the default `--reference stored`, is refused
with a message saying how to re-record it. Without `--reference live`, `--live`
still compares with each case's recorded `expected_fingerprint`, which goes
stale as the data moves; keep that for a warehouse that does not change.

**Absolute floors (optional).** The thresholds above only say "no worse than
the baseline": they cannot stop a release whose baseline was already poor, or
one that slid down in steps each smaller than the allowed drop. Two optional
floors close that gap. Both are **off unless set**, so nothing changes until
you opt in:

| Setting / flag | Fails the run when |
| --- | --- |
| `EVAL_MIN_ACCURACY` / `--min-accuracy PCT` | overall execution accuracy is below `PCT` (0-100) |
| `EVAL_MIN_SOURCE_ACCURACY` / `--min-source-accuracy PCT` | any one data source's accuracy is below `PCT`, or the run has no per-source figures to check (no case names a `datasource`) |

```bash
python -m eval.cli run --live --reference live --golden eval_data/golden.jsonl \
    --baseline eval_data/baseline.json --min-accuracy 90 --min-source-accuracy 80
```

A floor needs no `--baseline` and is checked even when the comparison shows no
regression. A run exactly at the floor passes. On failure the exit code is `1`
and the output says which figure is under which floor, for example
`accuracy 82.50% (33/40) is below the absolute floor of 90.00%`. A flag takes
precedence over the environment setting. Set floors only for `--live` runs:
an offline run is 100% by construction and passes any floor.

### 18.3 What the numbers mean

- **Execution accuracy** is the share of active cases whose generated SQL
  returned the same answer as the reference. It is *execution* accuracy:
  two different queries that return the same result both count as right, and
  a query that looks right but returns something else does not.
- **"The same answer"** (`eval/compare.py`): rows are compared as a multiset
  (duplicates count, order does not); **column names are ignored** (an alias
  does not matter) but the **column order as selected** does; row order only
  matters when the reference has a top-level `ORDER BY` with `TOP` /
  `OFFSET` (then the sort-key columns must match position by position and
  rows tied on the key may come in any order); numbers match within a
  relative tolerance of `1e-6` (change it with `--float-tolerance`; `0` is
  exact), **integers always exactly**; `NULL` equals `NULL` and nothing else;
  a midnight datetime equals the plain date; text is compared exactly. Known
  limit: when a `TOP n` cuts through rows tied on the sort key, a correct
  answer can differ from the reference; give such a reference a tie-breaking
  second sort key.
- **Per-tag accuracy** breaks the figure down by the case's tags (the harvest
  adds `lang:`, `source:` and `outcome:` tags). **Per-source execution
  accuracy** and **source-selection accuracy** appear when cases name a
  `datasource`: the second is how often the pipeline picked the source the
  case belongs to, the first how often the answer was right per source. A
  wrong source is usually also a wrong answer, so the overall figure already
  moves; the per-source lines and the deltas printed under a baseline
  comparison say where. They are informational and do not by themselves
  fail the gate, unless you set `EVAL_MIN_SOURCE_ACCURACY` (§18.2).
- **Error taxonomy.** `fingerprint_mismatch` (in `--reference live`: the
  result differs from the reference; the message gives counts, never
  values); `guard_rejected`, `execution_error`, `generation_error`;
  `unexpected_out_of_scope` / `missed_out_of_scope` (the model declined a
  question it should answer, or answered one it should decline);
  `reference_error` means the **case's own** `expected_sql` was rejected by
  the guard or failed to run today (a table was renamed, say): it counts as
  a failure so it is seen, but the fix is the case, not the model. Re-run
  `eval.cli verify` to find such cases.
- **Offline mode** (`eval.cli run` without `--live`, what CI does) replays
  each case's recorded rows, so its 100% is true by construction and only
  proves the harness, the guard and the fingerprinting work. It is not a
  measure of accuracy and `--reference live` refuses to run offline.
- The sample is stratified so that rare sources and outcomes are present;
  with 150 cases a single source can have only a handful, so read a
  per-source percentage with its denominator.
- Keep the set alive: `reference_error` cases and cases whose answer changed
  for a real reason (a business rule changed) are fixed in `golden.jsonl`
  and re-verified (`eval.cli verify --refresh` also re-records the stored
  rows of active cases for the offline replay).

## 19. Sharing diagnostics safely

When something goes wrong you will be asked for logs, configuration or command
output. Send what helps and nothing that opens a door. Read every file before
you attach it, and prefer pasting the few lines that matter to attaching a whole
file.

**Never send these with their real contents:**

- `.env`. It holds `DB_PASSWORD`, every `DB_PASSWORD_*` (and any variable a
  `password_env:` names), `OPENAI_API_KEY`, `API_KEYS_JSON`, and possibly a
  password written inside `DB_CONNECTION_URL`, `APP_DB_URL` or a `url_env`
  variable. If someone needs to see your settings, make a copy, blank every
  secret in the copy (`DB_PASSWORD=`, `OPENAI_API_KEY=`, the password part of
  every URL, the whole `API_KEYS_JSON` value, which may span several lines),
  read the copy through, and send that.
- `project_config/api_keys.json` (or the `API_KEYS_JSON` value). It holds
  SHA-256 digests, not keys, but it is the list of who may use the system and
  which columns each may not see (§10). The same goes for any key export.
- A raw API key, and `VERIFY_API_KEY`. If a raw key reached a chat or a ticket,
  treat it as leaked: revoke it from the admin panel and issue a new one (§1).
- `project_config/.setup_log.json`, which may contain the database URL with its
  password (§2.2).
- `logs/audit_log.jsonl*` and `logs/query_log.jsonl*` (real questions and the
  SQL generated for them), the SQLite files under `logs/` (`app.db`,
  `sessions.db` and their `-wal` and `-shm` files), `exports/` (result sets),
  `eval_data/` and every report written from it (§18), and
  `project_config_draft/` (drafts may quote real values, §2.3).

**Safe to send, after you have looked at it:**

- The output of `python scripts/verify_deployment.py`. It never prints a
  password: connection targets are shown with the password masked, and
  `VERIFY_API_KEY` is not echoed. It does name hosts, databases and tables.
- `python scripts/assign_datasources.py` and `python scripts/prompt_budget.py`
  output: table names and numbers; no credential or API key is printed.
- The aggregate report of `python scripts/analyze_audit_log.py` (§8), but not
  the `--include-examples` one.
- `project_config/schema.yaml`: table and column names and their descriptions.
  Check that no description quotes a real value (a draft from §2.3 can).
- `project_config/datasources.yaml`. It cannot hold a password: a `password:`,
  `pwd:` or `url:` key is refused, and the file names the *variable* that holds
  the secret (`password_env: DB_PASSWORD_SALES`), never its value. It does
  contain server names, database names and login names; blank them if how your
  servers are laid out is itself confidential.
- The start-up log lines named in §5 (banner, `CORS allowed origins`,
  `Prompt path for data source`), and the `[FAIL]` line you are asking about.

Whatever you send, say what you removed, so the reader does not mistake a
blanked value for a missing one.
