# Deployment runbook — first production week

Status: **operational checklist, not a design document**. Read this the day
you deploy, and again the day the pilot week ends. Every command assumes
you are in the repo root, on the machine that will actually run the server
(the same one whose `.env` / real environment variables the server reads).

Output is UTF-8. Every command-line program in the repository (`scripts/`,
`python -m eval.cli`, `setup_project.py`, `app.py` and the rest) writes UTF-8
to standard output and standard error, whatever code page the Windows console
uses (cp1252, cp1256, cp437). A report that names a Persian table or question
is not cut short by a `UnicodeEncodeError`, and nothing needs setting first
(`PYTHONUTF8`, `chcp 65001`). A file saved from that output is UTF-8; open it
as UTF-8. The web server is not affected.

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
installation already running any 6.x release (6.7.0 included) that is moving to
6.16.1 follows §17, which sequences the upgrade notes of every release in between
into one checklist.
Before sending logs or configuration to anyone for help, read §19.

### First install, in order

Do these in order; each one assumes the ones before it. A step marked
*several databases only* is skipped on a one-database deployment. "Done when"
is the sign you can move on: do not continue past a step that did not show it.
Two things decide the order. The databases are described first (step 4),
because every tool that drafts a file afterwards reads them through the login
the application itself uses. And the first API key is issued (step 11) before
the first preflight (step 12), because the preflight fails while no key exists.

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
   in step 4 instead), `OPENAI_BASE_URL` and `OPENAI_MODEL`, and
   `OPENAI_API_KEY` only if your endpoint checks one (§2).
   *Done when:* `python -c "import config as cfg; cfg.settings.validate(); print('settings ok')"`
   prints `settings ok`. Anything else is a `ValueError` whose message names
   the variable to fix.
4. **Describe the databases first: `datasources.yaml`** (§16.1, §16.2). Every
   tool of the steps that follow reads the databases through the login the
   application itself uses, so say where they are before anything else.
   *Several databases:* `mkdir -p project_config`, write
   `project_config/datasources.yaml` from `project_config.example/datasources.example.yaml`
   (one entry per source), and put each source's raw password in `.env` under
   the variable its `password_env` names. *One database:* the file is optional
   and you can skip it. Without it the deployment has exactly one source, named
   `default`, whose connection is `DB_CONNECTION_URL` with the raw `DB_PASSWORD`
   of step 3 (`database/datasources.py`), and every tool below reads that one.
   For one database, write the file only if you want a setting that lives in
   it: `exclude_schemas:` (the same rule is `EXCLUDE_SCHEMAS=` in `.env`) or
   `nolock: true` (step 10).
   *Done when:* `python -c "from database.datasources import datasource_names; print(datasource_names())"`
   prints every source name (`('default',)` for one database without the file),
   and the check of step 3 still prints `settings ok`.
5. **Create `project_config/` from the template**:
   `mkdir -p project_config && cp -r project_config.example/. project_config/`
   (the copy keeps a `datasources.yaml` already there; `cp -r project_config.example project_config`
   would nest the template inside an existing folder). What you now have is the
   template's sample content, not yours: `schema.yaml` lists the sample tables,
   and steps 6 to 9 replace the files one by one (§2.1).
   *Done when:* `project_config/` holds the nine YAML files and `system_prompt.md`.
6. **Draft `schema.yaml`** (§2.3):
   `python -m database.schema_inspector_cli --output-dir project_config_draft --exclude-schemas '*_Stg,Replay'`.
   It reads every source of step 4 (names, types and keys, no row) and writes
   `project_config_draft/schema.yaml`: every table described by its own name and
   every column by its type. Leave out the schemas you will never ask about,
   with `--exclude-schemas` for this run or, to keep the rule, `EXCLUDE_SCHEMAS=`
   in `.env` or `exclude_schemas:` in `datasources.yaml`: every table costs
   prompt tokens. Read the file, delete the tables you do not want, delete its
   `# AUTO-GENERATED DRAFT` line (the structure is now reviewed; the
   descriptions are step 8) and put it in place of `project_config/schema.yaml`.
   Copy only this file: the wizard of step 9 drafts the others better. The
   tables of one more schema can join it later with
   `python scripts/sync_schema.py --add-tables 'Schema.*'` (also with
   `--exclude-schemas`, §16.3). A hand-typed table and column list is a valid
   start too; step 7 brings its structure in step with the databases.
   *Done when:* `project_config/schema.yaml` lists your tables and no longer the
   template's.
7. **Check the structure**: `python scripts/sync_schema.py --check` (§16.3).
   It reads every source's catalogue (three `INFORMATION_SCHEMA` views per
   source, names and types only). *Done when:* it prints
   `CHECK OK: schema.yaml's structure matches the databases` (exit code 0). If
   it prints `CHECK FAILED` (a table or column changed after the draft, or you
   typed the list by hand), run `python scripts/sync_schema.py` without
   `--check`: it writes `project_config/schema.synced.yaml` next to
   `schema.yaml` (your file with every comment and description kept, plus the
   `datasource:` line of every table with several databases, the columns the
   databases have and the file lacks as drafts, a `column_types:` map, and a
   marker on every column or table the databases do not have; it also writes
   `relationships.proposed.yaml` for review). Read it (a diff against
   `schema.yaml` shows exactly what changed), copy `schema.yaml` to
   `schema.yaml.bak`, move the proposal over `schema.yaml` and check again. The
   report's section `== columns in schema.yaml that the database does not have: N ==`
   lists each column that carries the comment `# not in database (sync_schema.py)`,
   and `== tables in schema.yaml found in no data source: N ==` each table marked
   `# not found in any data source`. Remove them with
   `python scripts/sync_schema.py --prune` (review the result and replace
   `schema.yaml` again), correct a misspelt column by hand, or have the DBA lift
   a `DENY` that hides a column from the login (§16.3). `--check` does not fail
   on marked columns, so read the sections: they should read `: 0 ==`. Without
   this step the preflight of step 12 fails with `Tables are in their data source`
   (several databases) and with `Schema structure matches the databases` as soon
   as the databases differ from the file; a file that only lacks the recorded
   column types passes that check with a note (step 14 of "Upgrading from 6.0 to
   6.14.2", §17). Whatever the sync adds comes with the same name-only drafts,
   which step 8 describes.
8. **Draft the descriptions, review them, replace `schema.yaml`, delete the
   header** (§2.4): `python scripts/draft_descriptions.py` (add `--language en`
   for English; the default is Persian, the language of the questions). For each
   table it asks the wizard's model one question about the names, types, keys and
   relationships in `schema.yaml` (no row is read or sent), and writes
   `project_config/schema.described.yaml`: your file with every description that
   was empty, a placeholder, only a table's name or only `<type> column` drafted,
   every other line exactly as it was, each drafted line ending with `# REVIEW`,
   and a first line `# AUTO-GENERATED by draft_descriptions.py`. Read it and
   correct what is wrong, copy `schema.yaml` to `schema.yaml.bak`, move the file
   over `schema.yaml` and **delete the first line**: the preflight fails on that
   line until you do. If some tables failed, the summary lists them;
   `python scripts/draft_descriptions.py --resume` asks only about those.
   *Done when:* `schema.yaml` has no `AUTO-GENERATED` line and
   `python scripts/sync_schema.py --check` still prints `CHECK OK`.
9. **The setup wizard: entities, aliases, rules, examples** (§2.2):
   `python setup_project.py --language fa`. The model is the application's
   (`OPENAI_BASE_URL` and `OPENAI_MODEL`) unless you give `--llm-base-url` and
   `--llm-model`. With the `datasources.yaml` of step 4 it needs no URL, and with
   one database it uses `DB_CONNECTION_URL`. It drafts `entities.yaml`,
   `aliases.yaml`, `business_rules.yaml` and `examples.yaml` (and
   `relationships.yaml`) into `project_config_draft/`, never into
   `project_config/` (it refuses that unless told to). Read the drafts and copy
   the files you accept into `project_config/`, deleting each file's
   `# AUTO-GENERATED by setup_project.py` line once you have reviewed it. The
   wizard does not write `schema.yaml` and does not set `datasource:`.
   *Done when:* the four files in `project_config/` are the reviewed ones. The
   wizard's own last line, `Setup complete.`, appears only in a directory that
   already holds all ten files; in the draft directory it lists the six files it
   does not write and says `Setup is NOT complete`, which is expected.
   **Then draft the scope values** (§2.5): `python scripts/draft_policies.py`.
   The wizard writes neither `session_policy.yaml` nor `memory_policy.yaml`, so
   they are still the template's made-up options. The command reads the distinct
   values of one small dimension column (a region, a channel, a kind) and writes
   both files, with those values as options, into `project_config_draft/`. Read
   them and copy them into `project_config/`. *Done when:* the preflight check
   `Session and memory policies use real values` passes (step 12).
10. **Set what each source needs** (*several databases only*): `description:`
    and `keywords:` so questions reach the right source (§16.4), and
    `nolock: true` on exactly the sources whose DBA requires `WITH (NOLOCK)`;
    it is a per-source switch (§16.7). *Done when:* the command of step 4
    still prints the names.
11. **Issue the first admin key and create `api_keys.json`** (§1, §1.1):
    `python -m scripts.issue_api_key --id admin-1 --name "Admin" --full-admin`,
    `cp project_config/api_keys.example.json project_config/api_keys.json`, put
    the printed entry in it in place of the sample one, and set
    `API_KEYS_FILE=project_config/api_keys.json` in `.env`. This comes before
    the preflight because its `API key authentication` check fails while no
    key exists. *Done when:* you hold the raw key (it is printed once) and
    its entry is in the file. One key per analyst follows in step 17.
12. **Run the preflight**: `python scripts/verify_deployment.py`, with
    `VERIFY_API_KEY` set to the raw key of step 11 (§3). *Done when:* the last
    line reads `N passed, 0 failed, N skipped` and the exit code is 0. §3.1
    says what each line means and how to fix a `[FAIL]`; the check
    `project_config/ is curated, not a raw draft` is the one that holds steps 6
    to 9 to account.
13. **Size the prompt budget**: `python scripts/prompt_budget.py` (§16.6; it
    applies with one database too), then put the `PROMPT_RETRIEVAL_TOKEN_BUDGET=`
    line it prints in `.env`. *Done when:* the line is in `.env`. Exit code 1
    means a source does not fit the model's context window, and the output
    says which.
14. **Start the server** (§4): `uvicorn api.server:app --host 0.0.0.0 --port 8000 --no-server-header`
    (or `python -m api`), and for the web UI the static server of
    `web/README.md` (`python -m http.server 8080`, started from `web/`).
    *Done when:* the log shows the provenance banner, `CORS allowed origins: ...`
    and `System prompt loaded (N chars)` (with several sources also one
    `Prompt path for data source '<name>'` line each, §5), and
    `curl http://localhost:8000/health` answers with `status` set to `ok`.
15. **Ask one real question and confirm it was audited** (§6): the `curl` of
    §6, then `tail -n 1 logs/audit_log.jsonl`. *Done when:* the line carries
    today's `timestamp` and the `request_id` of the response.
16. **Run the preflight again** after the `.env` change of step 13, and after
    any later change to `.env` or `project_config/`, with the same command as
    step 12. *Done when:* `0 failed` each time.
17. **Issue one key per analyst** (§1), add each entry to the key file and
    restart the server (the file is read once at start-up). *Done when:* each
    analyst can open the UI and ask a question.
18. **Build the golden set** once real questions are in the audit log, and
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

The panel has thirteen sections. A key holding only `admin` loads four of them
and gets 403 on the other nine, which reads as a broken deployment rather
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

  **How an entry is read, and when it restricts nothing.** An entry is
  compared with the column names of `schema.yaml` without regard to case, but
  otherwise exactly. Since 6.15.1 whitespace and invisible Unicode format
  characters (the zero-width non-joiner U+200C and joiner U+200D, the marks
  U+200E and U+200F, the bidi embeddings and isolates U+202A to U+202E and
  U+2066 to U+2069, the byte-order mark U+FEFF, the soft hyphen) are removed from
  the **start and the end** of an entry, repeatedly, before it is read; a Persian
  editor inserts them without showing them, and an entry that carried one used to
  deny nothing. Nothing inside an entry is changed: a space inside a name is not
  removed (`National ID` is not `NationalID`), and neither is a zero-width
  non-joiner inside a Persian column name, which stays exactly as written in the
  key file, in the stored list and in what the panel shows. When a plain entry
  does hold such a character inside, the guard denies the entry as written and
  also the same name without that character (never narrower than what you
  wrote); an entry matches a column of `schema.yaml` when either spelling does. The
  same cleaning applies to keys of the admin panel, when they are read and when
  `PATCH /admin/keys/{key}/acl` saves them. A key with a changed entry logs one
  WARNING at start-up naming the key id and showing both spellings with `ascii()`
  (`'NationalID\u200f' -> 'NationalID'`), never the key's hash. A plain name that
  matches no column of `schema.yaml` (a typo, a renamed column) denies nothing: it
  is logged as a WARNING once per key, and the preflight check `Column
  restrictions name real columns` (§3.1) fails and lists the key id and the entry.
  The server still starts, so an installation that starts today keeps starting;
  run the preflight after editing the list. The same personal number often lives
  under several names in a warehouse (`NationalID`, `NationalCode`,
  `CustomerNationalID`); denying one leaves the others, and the same check
  advises, when every entry matches, which columns look like a denied name and are
  not denied.

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
  The list must hold **every** address the page is opened with, spelt exactly
  as the browser shows it: the IP address, the host name and `localhost` are
  three different origins, and so are two ports on one machine. Setting the
  variable replaces the default list, so name every one you need, comma-separated.
  `VERIFY_UI_ORIGIN=<address>` makes the preflight check it (§3).

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

(PowerShell: `Copy-Item -Recurse project_config.example project_config`.) If
`project_config/` already exists, because `datasources.yaml` was written first
(the order of "First install, in order"), copy the contents into it instead:
`cp -r project_config.example/. project_config/`, in PowerShell
`Copy-Item -Recurse project_config.example\* project_config\`. The
copy also holds `README.md`, `datasources.example.yaml`, `api_keys.example.json`
and `_test_fixtures/`; the templates are not read under those names, and the
folder is yours to tidy. Then replace the placeholders with your own domain.
Who writes what:

| File | How it is made |
|---|---|
| `schema.yaml` | By hand, or drafted from the live database by `python -m database.schema_inspector_cli` (§2.3), whose table and column descriptions `python scripts/draft_descriptions.py` then drafts for you to review (§2.4). It is the SQL guard's allowlist: a table you add widens what generated SQL may touch. Give every table a `db_schema` (§16.3). |
| `entities.yaml`, `aliases.yaml`, `business_rules.yaml`, `examples.yaml` | By hand, or drafted with an LLM by the setup wizard `python setup_project.py` (§2.2), which writes the drafts to `project_config_draft/` for you to review and copy across. |
| `session_policy.yaml`, `memory_policy.yaml` | By hand, or the values drafted from the warehouse by `python scripts/draft_policies.py` (§2.5): the options an analyst may pin and a clarification offers. Neither the wizard nor the inspector writes them, so after the copy they still hold the template's made-up options, which the preflight check `Session and memory policies use real values` refuses. |
| `metrics.yaml`, `retrieval_hints.yaml` | By hand. Neither tool writes them, so after the copy they still hold the template's content. `docs/en/tutorial.md` shows how each one steers retrieval and the prompt. |
| `system_prompt.md` | By hand: the model's instructions for your schema, rules and dialect (§5 says what happens without it). |
| `relationships.yaml` | Optional; the server does not read it (the prompt's relationships come from `schema.yaml`). |
| `datasources.yaml` | Only for more than one database (§16). |
| `api_keys.json` | The key array that `API_KEYS_FILE` names (§1, §2 above). |

`python scripts/verify_deployment.py` (§3) checks all ten as `project_config/ loads`
(6.8.0; before that it loaded only six): it names every file that is missing in one
message, then loads each through the application's own loaders, and it fails on an
empty `system_prompt.md`. A relative `PROJECT_CONFIG_DIR` is resolved against the
repository root, as the server does.

### 2.2 The setup wizard, `setup_project.py`

A one-time helper that drafts four of those files (and `relationships.yaml`)
from the live database with the help of a model. It is optional: everything it
writes can be written by hand, and everything it writes must be reviewed,
because each file starts with `# AUTO-GENERATED by setup_project.py — review before use`.
It writes **drafts** into `project_config_draft/` (git-ignored) and never into the
live `project_config/` unless you say so with `--overwrite-live` (below); you review
the drafts and copy the files you accept across. Run it from the repository root,
after §2.1 and with `.env` filled in (it reads `.env`). With a `project_config/datasources.yaml`
(§16) it needs no URL at all: it describes **every data source** in it, through the
application's own login for each (see "It reads the data sources of
`datasources.yaml`" below):

```bash
python setup_project.py \
    --llm-base-url http://<llm-host>:<llm-port>/v1 \
    --llm-model <model name served by that endpoint> \
    --language fa
```

For a first install, before any configuration exists, give one database by URL:

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
| `--db-url URL` | One database, read with the login in the URL: for a first install. Without it, and with a `datasources.yaml`, every data source in that file is read (below). With neither: `DB_CONNECTION_URL`, then `DATABASE_URL`; if there is none, interactive mode asks and `--non-interactive` stops. |
| `--datasource NAME` | Read only this data source of `datasources.yaml`; repeat it for several (default: all). An unknown name, or a run with no `datasources.yaml`, stops with exit status 2; it cannot be combined with `--db-url`. |
| `--llm-provider openai\|mock` | `openai` (default) is any OpenAI-compatible endpoint; `mock` calls no model and leaves aliases, rules and examples empty. Also `WIZARD_LLM_PROVIDER`. |
| `--llm-model NAME` | The model. Default: `WIZARD_LLM_MODEL`, else `OPENAI_MODEL`. |
| `--llm-base-url URL` | The endpoint. Default: `WIZARD_LLM_BASE_URL`, else `OPENAI_BASE_URL`. A remote endpoint is refused unless `LLM_ALLOW_REMOTE=true`. |
| `--language fa\|en\|both` | The language analysts ask in. Also `WIZARD_LANGUAGE`. Unset: asked interactively (default `en`), `en` when not interactive. |
| `--output DIR` | Where to write the drafts (default `project_config_draft`). The live configuration directory (`PROJECT_CONFIG_DIR`, by default `project_config/`) and `project_config.example/` are refused unless `--overwrite-live` is given; paths are compared after resolving them, so `./project_config/`, a `..` detour or a link does not get around it. |
| `--overwrite-live` | Allow `--output` to be a protected directory. Every file the wizard is about to replace is first copied to `<name>.bak-<YYYYmmdd-HHMMSS>` next to it (the setup log included), and the copies are listed at the end. A file the wizard does not produce (`schema.yaml`, `metrics.yaml`, ...) is never touched. |
| `--fresh` | Do not merge into the files already in the output directory; write them from this run alone (see "It merges" below). The protected-directory rule still applies. |
| `--review interactive\|auto` | `auto` is the same as `--non-interactive`. |
| `--non-interactive` | Accept every suggestion without asking; for scripts. |
| `--include-schemas a,b` | Only these database schemas. |
| `--exclude-schemas PATTERNS` | Comma-separated `fnmatch` patterns, matched case-insensitively against schema names (`'*_Stg,Replay'`); those schemas are not read at all. For this run, on top of the system schemas, `EXCLUDE_SCHEMAS` and each source's `exclude_schemas:` (see "Which schemas are left out" below). Wins over `--include-schemas`. |
| `--include-staging` | Keep the staging and change-capture tables that `STAGING_PATTERNS` names (§3.1, the check `No staging or change-capture tables`). By default they are skipped. |
| `--dry-run` | Print the files and write none, the setup log included; the validation step is skipped. |
| `--resume` | Continue from the first step `.setup_log.json` does not record as completed (see below). |
| `--allow-repeated-answers` | Do not stop step 3 or 4 when 3 tables in a row get the identical answer from the model (see "It stops when the model repeats itself" below). Only for a genuinely tiny schema whose tables really are alike. |
| `--keep-existing-synonyms` | When merging into an `aliases.yaml` that already holds synonyms, keep the keys that are values read from rows. By default they are dropped and counted per shape (see "No value from a row" below). |
| `--value-synonyms` | Also write the `synonyms` of `aliases.yaml` from the values of label columns (name, title, kind, status, type) that hold at most 50 distinct values. Off by default: no synonym is built from a value read from a row (see "No value from a row" below). Even with it, dates and times, numbers and codes, phone numbers, e-mail and IP addresses, URLs, addresses, masked values (`xxx`, `***`), error text, anything over 40 characters, any column named like personal or contact data (`FirstName`, `Family`, `Mobile`, `Phone`, `Address`, `Email`, `NationalCode`, ...) and the `Name` or `Title` of a table of people (`Customer`, `Employee`, `User`, ...) are never taken; at most 20 per table. It reads up to 51 distinct values of each such column. |

What it does, in order. Each step is recorded in `<output>/.setup_log.json`
under the key shown:

| Key | Step | What happens |
|---|---|---|
| `step1_connection` | Database connection | Opens the URL, or each data source of `datasources.yaml` through the application's engine, and runs `SELECT 1`; on failure it prints the error (the password masked) and, for a URL and interactively, offers a retry; a data source that cannot be reached stops the run (exit status 1). |
| `step2_schema` | Schema discovery | In two phases, over all the selected sources together. First it lists schema and table names (names only: no column, no row) for each source, applies the exclusion (system schemas, `EXCLUDE_SCHEMAS`, the source's `exclude_schemas:`, `--include-schemas`, `--exclude-schemas`), drops the staging tables (below) and, interactively, shows the schemas of each source with the number of tables that would be read and asks which to leave out (and whether to record the answer, below). Only then does it read the remaining schemas (tables, columns, foreign keys and up to five sample values per text column) and call each table a fact or a dimension table. Interactively it then asks which single tables to leave out. |
| `step3_aliases` | Aliases | One model call per table for aliases and a one-line description (the model is told the schema-qualified name). The list is cleaned: repeats (after folding case, Persian and Arabic letter forms, digits and punctuation) are dropped, and a list of more than 8 first loses every alias whose words are all in a shorter alias of the same table, then keeps the shortest 8; the summary counts the tables cleaned. Interactively you accept, edit or clear each (what you type stays as typed). |
| `step4_rules` | Business rules | One model call per fact table that has at least one numeric column that is not a key (not the primary key, not a foreign key or join column, not named `...ID`, `...Key` or `...Code`), for its value and volume columns and a rule text; the prompt lists those columns and only they can be named (a key the model names, as in `Value column: ID. Volume column: ID.`, is dropped). A fact table with none gets no rule and the summary lists it. The prompt carries the table's join hints (declared foreign keys, or relationships inferred from column names). |
| `step5_examples` | Examples | One model call for each data source (one for a single database) that asks for ten question-and-SQL pairs about the tables of that source, fact tables first, up to 20. An answer with no usable pair is asked once more with a smaller request. A pair needs a question and a read-only statement (`SELECT` or `WITH`); the others are counted and dropped (`4 item(s) of the answers discarded`). When no pair is usable, the step and the summary say so (`WARNING: examples.yaml has no example from this run`), the setup log records why under `problems`, and the file begins with `# NO EXAMPLES`. |
| `step6_write` | Review and write | Merges this run into the files already in `--output` and writes `entities.yaml`, `aliases.yaml`, `business_rules.yaml`, `examples.yaml` and `relationships.yaml` there. Earlier entries for one table are made one before the merge, and `aliases.yaml` gets no `synonyms` unless `--value-synonyms` is given. Interactively each file is shown first: Accept, Edit in `$EDITOR`, Regenerate or Skip. A summary at the end gives, per file, the entries kept, added and replaced. |
| `step7_validate` | Validation | Loads the four files that have a loader through the application's own validators and prints `OK`, `FAILED: ...` or `skipped` for each. If any of the ten required `project_config/` files (§2.1) is still missing it lists them with the `cp` command that copies each template from `project_config.example/`, and says `Setup is NOT complete`; it copies nothing. Only when none is missing and none failed does it print `Setup complete.` and the number of entities, rules and examples. |

What it does not do:

- **It does not write `schema.yaml`**, so it cannot make the SQL guard's
  allowlist; use §2.3 or write it by hand. It does not write `metrics.yaml`,
  `retrieval_hints.yaml`, the two policy files, `system_prompt.md`,
  `.env` or any API key. It edits `datasources.yaml` only when you
  ask it to record the schemas you left out (below); it never edits `.env`.
- **It does not assign `datasource:` to `schema.yaml`'s tables.** It writes the
  data sources a table was found in to its `entities.yaml` entry; the
  `datasource:` lines of `schema.yaml` are `python scripts/sync_schema.py`'s
  (§16.3).
- **A URL it is given must carry the password itself**, percent-encoded (`@` is
  `%40`): `--db-url`, `DB_CONNECTION_URL` and `DATABASE_URL` are read as they
  are, `DB_PASSWORD` is not applied. Passing it as `--db-url` leaves it in the
  shell history; the `DATABASE_URL` environment variable avoids that. The data
  sources of `datasources.yaml` need none of this: their secrets come from the
  environment variables that file names, exactly as for the server.
- **Its model settings follow the application's.** For each of endpoint, model and
  key it takes the flag, then `WIZARD_LLM_BASE_URL` / `WIZARD_LLM_MODEL`, then
  `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY` (an empty `WIZARD_*`
  value counts as unset; `.env.example` ships both empty), and it prints one
  line naming the provider, model and endpoint it chose and where each came
  from (never the key). `WIZARD_LLM_PROVIDER` (`openai` or `mock`) and
  `WIZARD_LANGUAGE` are separate settings. A local endpoint needs no key. If
  the endpoint does not answer, it says so loudly and carries on with the mock
  provider, so aliases, rules and examples come out empty.
- **It obeys `LLM_ALLOW_REMOTE`.** What it sends to the model is table and
  column names, sample values read from the warehouse, and a schema summary.
  An endpoint that is not local (the application's own definition: `LLM_TRUSTED`,
  else loopback, private-network and `*.local` hosts are local) is refused,
  before the wizard touches the database or the endpoint, unless
  `LLM_ALLOW_REMOTE=true`. Use `--llm-provider mock` to run without a model.

Things to know before you run it:

- **It writes drafts, not your configuration.** The default output is
  `project_config_draft/`. Pointing `--output` at the live configuration directory or
  at `project_config.example/` stops the run, before the database or the model is
  touched, with a message and exit status 2. With `--overwrite-live` it writes there
  after copying each file it replaces to `<name>.bak-<YYYYmmdd-HHMMSS>`; the copies
  are printed. Earlier versions wrote into `project_config/` by default and replaced the
  files with no backup. The usual flow is: run the wizard, read the drafts, copy
  the files you accept into `project_config/` (`cp project_config_draft/entities.yaml project_config/`).
- **It merges, run after run.** For two databases you run it twice, and the
  second run must not lose the first's entries. When the output directory already
  holds wizard files, the new entries are merged by key: entries for tables this
  run did not cover stay as they are; an entry for a table of this run replaces
  the old one with the same key; examples are de-duplicated by question text
  (case and spacing ignored) and relationships by their two ends. Nothing is
  removed, so a file never shrinks. A table the model gave no answer for does not
  wipe what is there: if the new aliases of an entity are empty, or the model gave
  no rule for a fact table (the placeholder `Primary fact table: ...` the wizard writes
  on a first run), the earlier entry is kept, counted as kept, and named in the
  summary (`1 earlier entry kept because this run gave no answer for it: Customer`). To
  clear an entry on purpose, edit the file. A hand-set `label:` on an entity survives
  its replacement. The comments of an old file are not kept (the file is written again
  from the merged data), and `aliases.yaml`'s `synonyms` are united word by word.
  The summary prints `kept N, added N, replaced N` per file. An existing wizard
  file that cannot be read stops the run before any model call (exit status 2);
  fix or move it, or use `--fresh`.
- **Keys are the inspector's.** Entities, aliases, business rules and
  relationships are keyed by the bare table name when no other table shares it,
  and by `Schema.Table` when several schemas (of one source or of several) do (`docs/design/TABLE-NAMES.md`; the
  same rule as `database.schema_inspector_cli`). Before, everything was keyed by the
  bare name, so `Trade` from four schemas became one entity (190 entities for a
  278-table warehouse). The rule looks at the tables of this run together with those
  of the earlier run's `entities.yaml`: if a name that was unique is now shared, the
  earlier bare entry is re-keyed (`Trade` becomes `sales.Trade`) in all five files and
  the summary lists it, instead of being replaced by the other schema's table. Only a
  key that is the table's default spelling is re-keyed; an entity name you chose is
  left alone. The model is told the schema-qualified name of each table.
- **One entity per table, and the keys of `schema.yaml`.** The keys are
  exactly the ones `schema.yaml` gets from `database.schema_inspector_cli` and
  `scripts/sync_schema.py`: one function, `schema_data.registry.table_key_for`,
  spells them all (a bare name, `Schema.Table` for a shared name, a part that is
  not a plain identifier in brackets: `[Price List]`, `ref.[Run.Log]`), so an
  entity key always names a table of `schema.yaml`; retrieval reads it that way.
  An earlier entry is the same table when its key is the table's bare name or
  `Schema.Table`; its schema is read from `schema_name:`, from the `schema:` the
  inspector's draft writes, from its key, or from the one table of that name this
  run found. Earlier, an inspector draft in the output folder (bare keys, `schema:`)
  was read as a second schema of every name, every name looked shared, everything
  was written `Schema.Table` and the bare entry stayed beside it: two entities per
  table, the one that matched `schema.yaml` without aliases. Now two earlier entries
  that end up on one key (a bare one and a qualified one) become one, with the
  aliases of both, in `entities.yaml`, `aliases.yaml` and `business_rules.yaml`, and
  the summary lists them (`Merged 1 duplicate entry of one table in entities.yaml:
  Sales_Dim.ActionType -> ActionType`). A bare entry whose name several schemas
  share cannot be matched to one of them and is left as it is; an entity name you
  chose is never touched.
- **No value from a row goes into `aliases.yaml` unless you ask (`--value-synonyms`).**
  The wizard of 6.13 and 6.14 made a `synonyms` key of every sample value of every
  text column: hundreds of dates (`1387/03/24`), times, numeric ids, national-id-like
  numbers, phone numbers, addresses, IP addresses, partly masked names (`ab xxxxx cd`),
  application error messages and hashes, each mapped to a table name, in a file that goes
  into the prompt and into version control. It now builds none. With `--value-synonyms`
  it reads, for each text column whose name is a label (a word such as `name`, `title`,
  `kind`, `status`, `type`, `category`), up to 51 distinct values; a column with more than
  50 is not categorical and gives nothing. Of the others it keeps a value only when it is
  not a date or time, a number or code (mostly digits or punctuation, a hash), a phone
  number, an e-mail or IP address, a URL, an address (words with digits), a masked value
  (`xxx` runs, `***`), error text, or longer than 40 characters; never from a column named
  like personal or contact data (`FirstName`, `Family`, `Mobile`, `Phone`, `Address`,
  `Email`, `NationalCode`, ...), and never the `Name` or `Title` of a table of people
  (`Customer`, `Employee`, `User`, ...); at most 20 per table. **The trade-off:** a
  word that only a row holds (the name of a status) no longer reaches retrieval through a
  synonym, and a bare `Name` column of a lookup table (`Bank.Name`) is read, so review the
  keys it wrote before you use them; in return no row value is in the prompt or in git by
  default. The setup log records the counts, never a value. The preflight check `Aliases
  hold no raw data values` (§3.1) counts the keys of an existing `aliases.yaml` that have
  these shapes. A re-run over an output folder that holds such an `aliases.yaml` drops
  those keys (date, phone, IP, masked value, long digit string, address, hash, error
  message) when it merges, and the summary and `.setup_log.json` give the count per shape,
  never a key (`aliases.yaml: 37 synonym key(s) from an earlier run were values read from
  rows (<date>: 20, <phone>: 9, ...) and were dropped`); `--keep-existing-synonyms` keeps
  them. The same rules filter the sample values the model sees in step 3: nothing from a
  column named like personal or contact data, and no date, number, phone number, address,
  masked value or error text from the others; short statuses and kinds stay.
- **It reads the data sources of `datasources.yaml`.** When `project_config/datasources.yaml`
  exists and no `--db-url` is given, every source in it is described (or those named
  with `--datasource NAME`, repeatable), each through the engine the server uses
  for it (`database.connection.get_engine`), so the wizard sees exactly the tables
  the application's login can query. A wider login (a Windows administrator) lists
  change-capture, temporary and staging tables the application cannot read; the
  drafts of that kind used to end up in live configuration. The listing, the
  reading plan, the inspection and the keys span all selected sources together:
  a bare table name shared by several schemas, in one source or across sources, is
  keyed `Schema.Table` (the rule of the inspector and `sync_schema.py`), and a table
  present with the same `schema.table` in two sources is one entity. With several
  sources configured, every `entities.yaml` entry carries `datasource: [sales, reports]`
  (the sources it was found in; the inspector's drafts write the same line, and
  this holds for a run narrowed with `--datasource`, too). A later run for one
  source adds to that list instead of replacing it. The model sees the
  schema-qualified name, never a data source. `--db-url` keeps today's behaviour:
  one database, read with the login in the URL, no `datasource:` lines, and none
  of the sources' own `exclude_schemas:`, because a URL is not one of them.
  Without `datasources.yaml` and without `--db-url` it reads `DB_CONNECTION_URL` or
  `DATABASE_URL` as before. A view is not described (as before).
- **Which schemas are left out, and who says so.** Per source, in this order, and
  counted under the first rule that applies: the system schemas (`sys`,
  `INFORMATION_SCHEMA`, `guest`, `cdc`, the fixed `db_*` roles, `pg_*`); the
  `EXCLUDE_SCHEMAS` setting (every source); that source's `exclude_schemas:` in
  `datasources.yaml`; `--exclude-schemas` (this run); the schemas you pick when it
  asks. They are the same rules the inspector applies (§2.3, `database/schema_filter.py`).
  The summary says which rule skipped what, in the inspector's categories:
  `Skipped 12 of 40 tables: ...` and, under `Skipped, by rule:`,
  `configured exclusion (EXCLUDE_SCHEMAS / exclude_schemas:): 4 table(s) in 2 schema(s)`,
  with one line per source when there are several.
- **It can record the schemas you left out.** When, interactively, you name schemas to
  leave out, the wizard offers to keep the answer so that no later run (or tool) asks
  again. With `datasources.yaml`, the patterns that matched a schema of a source are
  offered as that source's `exclude_schemas:`; the question (`Record them as
  exclude_schemas in datasources.yaml?`) defaults to **No**, and is never asked with
  `--non-interactive` or `--dry-run` (a dry run prints the lines instead). On Yes the
  file is first copied to `datasources.yaml.bak-<YYYYmmdd-HHMMSS>`, then one
  line is added to the source's entry (or the items to its existing list), as a text
  edit: the comments, quoting and line endings stay. The result is checked with the
  same validation the server applies, and must equal the original except for the
  added patterns; otherwise the file is left exactly as it was, nothing is backed up,
  and the lines to add by hand are printed (a source written inline as
  `name: {...}` or a flow list over several lines are not edited). Without
  `datasources.yaml` (or with `--db-url`) it prints the line to add to `.env`,
  e.g. `EXCLUDE_SCHEMAS=Alerts,Scratch` (the current value, then the new patterns);
  `.env` is never edited. The new `exclude_schemas:` applies to the sources' later
  runs of the inspector, `sync_schema.py` and this wizard; it never hides a table
  `schema.yaml` already lists (§2.3).
- **Staging tables and chosen schemas never reach the model.** The tables
  `STAGING_PATTERNS` names (§3.1; by default `*_Stg`, `*stg*`, `*staging*` as schema
  patterns and `table:*_Inserts`, `table:*_Updates`, `table:*_Deletes`) are skipped
  unless `--include-staging` is given, and so are the schemas matching
  `--exclude-schemas` and those you pick interactively. **A schema that is left out
  is never read**: the wizard lists names first and inspects (columns, sample values)
  only the schemas that remain, so the question is asked, and `--exclude-schemas`
  applied, before any table of those schemas is touched. A schema whose every table is
  staging is not read; a staging table whose bare name no other listed table shares is
  not read either; one whose name is shared (a staging copy of a fact table) is read and
  then dropped, so that the same-named table of another schema is not lost. The summary
  says how many tables were skipped and why, e.g.
  `Skipped 66 of 278 tables: 40 in excluded schemas, 26 staging or change-capture`
  (and, per rule, the inspector's lines).
  Relationships to a skipped table are dropped. Single tables you name in the
  interactive question after the inspection are dropped from its result. Non-interactive
  mode never prompts.
- **Relationships are inferred when the database declares few keys.** With
  fewer foreign keys than one per ten tables (a warehouse without keys declares
  none), `relationships.yaml` also gets the relationships that
  `schema_data/relationship_proposals.py` infers from column names (`Order.CustomerID`
  or `Order.Customer_ID` pointing at `Customer`'s key). Each inferred entry carries
  `basis` and `confidence` fields and a `# REVIEW` comment line above it: check it, or
  delete it. The business-rules prompt sees the same join hints. A reviewed entry (no
  `basis`) is never replaced by a guess. The server does not read
  `relationships.yaml` for the prompt (§2.1), so nothing here widens what generated
  SQL may touch.
- **`--resume` continues, it does not start over.** It runs again from the first
  step `.setup_log.json` does not record as completed. Steps 1 and 2 always run
  again, because the connection string is never stored. The aliases, rules and
  examples of completed model steps are reused instead of generated again, as
  long as the schema and the language are unchanged (a result made by the mock
  provider is never reused, nor is an empty list of examples: that step asks the model
  again); a file that already exists is not rewritten, except an `examples.yaml` that holds
  no example (the file written when the model gave nothing). When
  steps 1 to 6 are recorded and every file exists, only the validation runs,
  without the database or the model. A run without `--resume` starts a new log. The
  schema's fingerprint is taken after the exclusions and includes the table keys,
  the data sources each table was found in and the names of the sources read, so a
  changed `--exclude-schemas`, `--include-staging`, key or `--datasource` selection
  generates again.
- **It stops when the model repeats itself.** Steps 3 and 4 ask one question per
  table. A gateway with a response cache in front of the model can answer all of
  them with the reply to the first (§20.10), and nothing in the reply says so: one
  alias list written for most of a database's tables, and business rules such as `Value
  column: X. Volume column: X.` on a table where that makes no sense, are what it
  looks like. The wizard compares the model's answers as they arrive (the alias
  list in step 3; the rule text with the table names taken out in step 4, empty
  answers aside). When **3 tables in a row** get the identical answer it stops
  that step with `Step 3 stopped: the model endpoint returned the identical alias
  list for 3 different tables in a row (...)`, writes no file, exits with status
  3 and leaves the run resumable: the steps already recorded stay in
  `.setup_log.json`, so after the fix (disable response caching for this client
  at the gateway, or point `OPENAI_BASE_URL` / `WIZARD_LLM_BASE_URL` at the model
  server directly; `python scripts/verify_deployment.py` confirms it with
  `Model endpoint answers each prompt itself`) `--resume` continues. Each of the two
  steps ends with a line such as `183 distinct alias lists for 183 tables`; a
  number far below the table count is the same warning after the fact, for a
  repeat that never ran three in a row. `--allow-repeated-answers` turns the stop
  into one warning, for a schema so small that identical answers are expected.
  Step 5 asks one question for each data source, so there is nothing to compare.
- **Step 7 no longer needs a populated `project_config/`.** It validates the
  files it wrote and lists the required files still missing (§2.1), instead of
  ending in a `ConfigNotFoundError` traceback on a tree without the template
  copy. In `project_config_draft/` that list is long by design: the draft holds four
  of the ten required files (plus `relationships.yaml`), and the other six come
  from your `project_config/` when you copy the accepted drafts across.
- **`.setup_log.json` holds the connection string with its password masked
  (6.8.0).** A log written by an earlier version holds the string exactly as
  typed: delete it, and rotate the password if the file was shared or
  committed (§19). The `# Source:` comment in the generated `entities.yaml` is
  masked too, and so are driver errors shown on a failed connection.
- **In the review menu, Regenerate asks the model again.** For `business_rules.yaml`
  and `examples.yaml` it re-runs their own step; for `entities.yaml` and
  `aliases.yaml`, which are both built from the aliases of step 3, it re-runs
  step 3 without the per-table questions. A failure is reported and the current
  version kept. It is not offered for `relationships.yaml` (built from the
  schema) or when there is no model.

### 2.3 Drafting `schema.yaml`: `database.schema_inspector_cli`

```bash
python -m database.schema_inspector_cli --output-dir project_config_draft
```

With no `--db-url` the inspector reads `project_config/datasources.yaml` and
inspects **every data source through the application's own engine for it**
(`database.connection.get_engine`, the same login the server uses), so a draft
lists only the tables the application can query. Without `datasources.yaml` the
one implicit source, `DB_CONNECTION_URL` (with `DB_PASSWORD` applied, as at
start-up), is used. `--datasource NAME` (repeatable) narrows the run to some
sources; the `datasource:` lines are still written when the deployment has
several. The reason it works this way: a draft built under a wide login (a
Windows administrator, say) listed about 150 change-capture, temporary and
staging tables that the application's login cannot even query, and they ended
up in the live `schema.yaml`.

`--db-url` still works for a first install before any configuration exists:

```bash
python -m database.schema_inspector_cli \
    --db-url "mssql+pyodbc://reader:p%40ss@dbhost:1433/WarehouseDB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes" \
    --output-dir project_config_draft --include-schemas sales,ref
```

That is one source, named as the default source, read with the login in the URL
(percent-encode its password; the wizard's rule), and the tool prints one line
saying the application's own login may see less. The inspector no longer reads
`DATABASE_URL`; `--db-url` is the only way to name a URL.

**The draft has the format `sync_schema.py` writes (§16.3), because it is built by
the same code.** With several sources every table has a `datasource:` line (none
with one source); a table that exists with the same `schema.table` in several
sources is written once with `datasource: [sales, reports]`; a table is keyed by
its bare name unless that name occurs in more than one schema across all the
selected sources, when every occurrence is keyed `Schema.Table`
(`docs/design/TABLE-NAMES.md`); `db_schema` is written on a bare-keyed table, a
qualified key carries its schema. The `relationships:` entries and the entity
names in `entities.yaml` use the same keys, and `entities.yaml` also gets a
`datasource:` line per table with several sources. A sync of the draft against
the same databases changes nothing. The setup wizard (§2.2) spells its keys with
the same function (`schema_data.registry.table_key_for`), so its entity, alias, rule and
relationship keys are the keys of this `schema.yaml` for the same tables.

**Which tables are left out.** The end-of-run summary says how many tables and
schemas each rule skipped:

| Rule | What it skips | Set by |
|---|---|---|
| system | `sys`, `INFORMATION_SCHEMA`, `guest`, `cdc`, the nine fixed `db_*` role schemas (`db_owner`, `db_datareader`, ...), `pg_catalog`, `pg_toast`, `pg_*`; any letter case | always |
| configured exclusion | schemas matching `EXCLUDE_SCHEMAS` (every source) or the source's `exclude_schemas:` | `.env`, `datasources.yaml` |
| configured table exclusion | tables whose `schema.table` matches `EXCLUDE_TABLES` (every source) or the source's `exclude_tables:` | `.env`, `datasources.yaml` |
| `--exclude-schemas` | schemas matching these comma-separated patterns, for this run | the command line |
| outside `--include-schemas` | schemas not in this comma-separated list | the command line |
| `--exclude-tables` | tables with these names, in any schema | the command line |
| staging | schema or table matching `STAGING_PATTERNS` (§3.1); `--include-staging` keeps them | `.env`, the command line |

Patterns are `fnmatch`, compared without regard to case (`*_Stg` skips `Sales_Stg`
and `sales_stg`; a plain name skips just that schema); the fixed role schemas are
matched by exact name, so a schema of your own called `db_sales` is still
drafted. Exclusion wins over `--include-schemas`. Put a rule you want kept in
configuration, not in a flag you must remember on every run:

```
# .env, for every source
EXCLUDE_SCHEMAS=*_Stg,Replay
```

```yaml
# datasources.yaml, for one source (see the example file)
datasources:
  sales:
    ...
    exclude_schemas: ["*_Stg", Replay]
```

The exclusion decides what a tool *proposes* from the databases (§16.3 uses the
same rule for its report and `--add-tables`). It never hides a table that
`schema.yaml` already lists, and the preflight check
`No table in an excluded schema` (§3.1) tells you about a listed table that sits
in one.

**Single tables, and a staging schema you want.** `exclude_schemas:` skips a whole
schema. To skip single tables, list `fnmatch` patterns in the source's
`exclude_tables:` (or in `EXCLUDE_TABLES=` in `.env`, for every source). A pattern
is compared, without regard to case, with `schema.table`: `X_Fact.Order` is one
table, `X_Stg.*_Delete` the delete tables of one schema, `*.Order` a table of that
name in any schema; a pattern with no `.` is compared with the table name alone
(`Order`, `*_Delete`). The staging rule (`STAGING_PATTERNS`, §3.1) drops a staging
schema (`*_Stg`, `*stg*`, `*staging*`) and its change-capture tables
(`table:*_Inserts`, `table:*_Updates`, `table:*_Deletes`) together, and
`--include-staging` keeps both. To scan a staging schema but still leave out its
change-capture tables, name the schema in the source's `keep_staging_schemas:`:
the schema patterns of `STAGING_PATTERNS` no longer apply to it, while its
`table:` patterns and `exclude_tables:` still do. It never overrides
`exclude_schemas:`. For example, one database where the schemas `Meta`, `Audit_Stg`,
`Old` and `dbo` are not scanned, the staging schema `X_Stg` is, without the tables
ending `_Insert`, `_Update` or `_Delete`, and the table `X_Fact.Order` is not:

```yaml
datasources:
  main:
    ...
    exclude_schemas: [Meta, Audit_Stg, Old, dbo]
    keep_staging_schemas: [X_Stg]
    exclude_tables: ["X_Stg.*_Insert", "X_Stg.*_Update", "X_Stg.*_Delete", "X_Fact.Order"]
```

The default `STAGING_PATTERNS` know the plural names only (`_Inserts`, `_Updates`,
`_Deletes`), and they are left as they are so that no run proposes different
tables after an upgrade. For the singular names use `exclude_tables:` as above, or
add `table:*_Insert,table:*_Update,table:*_Delete` to `STAGING_PATTERNS` in `.env`
(the whole value is replaced, so keep the patterns you still want). A deployment
with one database and no `datasources.yaml` has `EXCLUDE_TABLES=`; for
`keep_staging_schemas:` it writes `datasources.yaml` for its one source (§16.2), or
edits `STAGING_PATTERNS` to drop the schema patterns it does not want. The end-of-run
summary counts the tables the new rule skipped as `configured table exclusion
(EXCLUDE_TABLES / exclude_tables:)`. `database/datasources_edit.py` can record
`exclude_tables:` in `datasources.yaml` (a comment-preserving edit after a backup,
like `exclude_schemas:`); the wizard does not ask for it.

**Real values.** No value read from a row is written to `schema.yaml`,
`entities.yaml` or `aliases.yaml` any more (the application reads no
`sample_values` key from `schema.yaml`; the `e.g. a, b, c` text the older drafts
put in a column description went into the prompt, and the `synonyms:` seeds
into a draft vocabulary). `--sample-values` (with `--sample-rows N`, default 10)
reads distinct values of string columns and writes them to
`sample_values.review.yaml` in the draft directory, readable by its owner only;
nothing loads that file. Use it to write real column descriptions, then delete
it. `--dry-run` never prints it. Without `--sample-values` no row is read at all
(only `COUNT(*)` per table, unless `--no-row-counts`).

**Relationships.** Declared foreign keys come first; where a column is named
like a link (`Order.Customer_ID`, `OrderDate_ID`, `CustomerID`) and no key is
declared, the same naming rules as `sync_schema.py`'s proposals add a guess, so a
warehouse with no foreign keys still gets a draft. Every guess carries the
comment `# INFERRED from column names (not a declared foreign key) -- REVIEW
before use` in `schema.yaml` and `relationships.yaml`; a declared key has none.
A relationship to a table the run skipped is not drafted.

Other options: `--output-dir DIR` (default `project_config_draft`),
`--no-row-counts` and `--dry-run` (print instead of writing). It writes
`schema.yaml`, `entities.yaml`, `aliases.yaml` and `relationships.yaml` into the
draft directory, never into `project_config/` or `project_config.example/` (it
refuses those two names with exit code 2), and it prints `Done.` at the end. The
draft directory is git-ignored. Exit code 1: a source's catalogue could not be
read; 2: unknown `--datasource`, an invalid `datasources.yaml`, or a protected
output directory. At the end of a run it also prints, to stderr, the number of
tables per schema, the skip counts, the relationship counts, a reminder that the
drafts are for a first install only, and a warning when more than 150 tables
were written (narrow the run with `--include-schemas` / `--exclude-schemas`, then
size the prompt with `python scripts/prompt_budget.py`).

**The drafts are for a first install only.** They describe every table they find
as a new entry, so they cannot be merged into a `schema.yaml` you have already
curated. The draft is a starting point, not a `schema.yaml` to deploy: every
table is described by its own name and every column by its type, and the files
carry the `AUTO-GENERATED DRAFT` header, which the preflight refuses (§3.1) until
you delete it after review. Review the structure (which tables stay), copy
`schema.yaml` into `project_config/`, and draft the descriptions with
`python scripts/draft_descriptions.py` (§2.4) instead of writing them all by hand.
Run `tests/test_schema_registry_snapshot.py` after editing it,
as the README says, and `python scripts/verify_deployment.py` (§3) to load it.
A draft whose header line was deleted is still recognised: its tables are described
by their own name (`description: 'Sales_Fact.Trade'`) and its columns by
`<type> column`, and when most of them are the preflight fails
`project_config/ is curated, not a raw draft` and gives the counts (§3.1). The files
of the setup wizard begin with `# AUTO-GENERATED by setup_project.py`, a draft header
in the same sense.

An existing deployment does not redraft. Once `schema.yaml` is in
`project_config/`, keep it current with `python scripts/sync_schema.py` (§16.3),
and add the tables of a new schema with
`python scripts/sync_schema.py --add-tables 'Schema.*'`: it keeps your
descriptions and only brings in what changed in the databases, while the
inspector writes a new draft from scratch each time.

### 2.4 Drafting the descriptions: `scripts/draft_descriptions.py`

The preflight (§3.1, the check `project_config/ is curated, not a raw draft`)
refuses a `schema.yaml` whose descriptions only repeat a table's name
(`Sales_Fact.Trade`) or a column's type (`bigint column`): such a description
says nothing the key did not say, and table retrieval matches the words of a
question against the words of the descriptions. That is exactly what the
inspector (§2.3) and `sync_schema.py --add-tables` (§16.3) write. On an upgrade
the answer is to restore the curated files. On a **first install there are no
curated files**, and writing 76 table and 1388 column descriptions by hand is
the only way past the check, unless a model drafts them. This command does that
and leaves the review to you.

It is a separate command next to `sync_schema.py` rather than a step of the
setup wizard, because it follows the same pattern ("propose a file, the
operator reviews it and replaces the original"): it needs `schema.yaml` and a
model but no database connection, it can be run again whenever tables are added,
and the wizard's drafts go to another directory and merge by key, which is not
what an in-place text edit of `schema.yaml` needs.

```bash
python scripts/draft_descriptions.py                      # writes project_config/schema.described.yaml
python scripts/draft_descriptions.py --dry-run            # lists what would be asked; no model, no file
python scripts/draft_descriptions.py --tables 'sales.*'   # only these tables
python scripts/draft_descriptions.py --language en        # English instead of Persian
python scripts/draft_descriptions.py --resume             # continue an interrupted or partly failed run
python scripts/draft_descriptions.py --samples            # also show label values (below)
```

**What it drafts.** Every table description that is empty, the inspector's
placeholder or only the table's name, and every column description that is empty
or only `<type> column`. The tests are those of the preflight
(`core.config_doctor`), so what the command drafts is what the preflight objects
to. A description a person wrote is never changed. Every key, flag, comment and
the order of the file stay as they are; a drafted line ends with `# REVIEW` (a
`# TO BE FILLED` note that `sync_schema.py` left on it is answered and goes), and
a table without any `description:` line gets one. The script parses its own
output and compares it with the input plus exactly the drafted descriptions, and
refuses to write a file that differs in anything else (or a `schema.yaml` laid
out in a way the line editor cannot edit: flow-style entries). `schema.yaml`
itself is never overwritten.

**What the model is told.** One call per table: the table key, its schema and
data source, the column names and types, the notes already written, the
relationships that name the table (`schema.yaml`'s own, `relationships.yaml`, and
the declared foreign keys of `relationships.proposed.yaml`; the guesses from
column names are left out) and the table's aliases in `entities.yaml`. A
primary or foreign key is told to the model by those relationships and by the
curated notes, because `schema.yaml` does not record keys. **No row value is read
or sent** unless you give `--samples`. With it, up to five distinct values of
each label column (a column named name, title, kind, status, type, category,
class, label, caption or group, with at most 50 distinct values) are read from
the table's data source and shown beside the column, under the rules of the
wizard's `--value-synonyms` (`core/value_shapes.py`): a column named like
personal or contact data is never read, nothing that looks like a date, number or
code, phone number, e-mail or IP address, URL, address, masked value or error
text, and nothing over 40 characters, is shown, and at most 20 values per table.
A table in another database of the same server (a multi-part `db_schema`) is not
sampled. The model is told to describe only what the names, types, keys and
relationships show, to invent no business fact, and to end a guess with `(?)`.
A table is described in one sentence (what one row is), a column in a few words,
in Persian (`--language fa`, the default) or English.

**The model and its rules are the wizard's** (§2.2): `--llm-provider`,
`--llm-model` and `--llm-base-url`, then `WIZARD_LLM_*`, then `OPENAI_BASE_URL`,
`OPENAI_MODEL` and `OPENAI_API_KEY`, and one line naming the model and the
endpoint (never the key). An endpoint that is not local (`LLM_TRUSTED`,
otherwise a loopback host, a private network or `*.local`) is refused before
anything is sent unless `LLM_ALLOW_REMOTE=true`, and then it prints what it
sends. Unlike the wizard it does not fall back to a mock model when the endpoint
does not answer: a mock drafts nothing, so the command stops with an error
(exit code 2). Each call is sized for its table: `max_tokens` is about 60 per
column asked about plus a fixed part, never less than `LLM_NUM_PREDICT` and never
more than `--max-tokens` (default 8192). A model that reasons before it answers
needs the room; a table of several hundred columns does not fit in one answer
and is reported as failed.

**The review gate.** The first line of `schema.described.yaml` is
`# AUTO-GENERATED by draft_descriptions.py`. The preflight reads it as a draft
header, like the inspector's and the wizard's: while it is there, the check
fails on the file, whatever its descriptions say. Read the file, correct what is
wrong, keep `schema.yaml.bak`, move the file over `schema.yaml` and delete that
first line; then the check passes if the descriptions are real. (A draft from the
inspector also has its own `# AUTO-GENERATED DRAFT` line, deleted at step 6 of
"First install, in order" when the structure was reviewed.)

**Failure and resuming.** A table the model fails on (an answer that is not JSON
or has nothing usable, a timeout, an HTTP error) is recorded and the run goes on.
A name in an answer that the table does not have is ignored; a column the model
leaves out keeps the description it had, and the summary counts them; nothing is
ever removed. The output is written after every table, so an interrupted run
loses at most the table in progress. The summary gives the counts (tables and
descriptions drafted, columns left undescribed, failed tables with the reason,
never a description) and the exit code is 1 when a table failed or the run was
interrupted. `--resume` keeps the tables already drafted in the existing output,
as they are in the file now (a correction you made there survives), and asks only
about the rest. Without `--resume` or `--force` the command refuses to replace
an existing output, so a reviewed draft is not lost to a second run; `--force`
starts over.

Other options: `--schema PATH` (default: the `schema.yaml` of `PROJECT_CONFIG_DIR`),
`--output PATH` (default: `schema.described.yaml` next to the input; naming the
input itself is refused), `--tables PATTERNS` (comma-separated `fnmatch` patterns,
repeatable, matched case-insensitively against the table key and against
`schema.name`). Exit codes: 0 done, or nothing to describe; 1 some tables failed
or the run was interrupted (the output holds the rest); 2 nothing could be decided
or written (`schema.yaml` invalid or not editable line by line, the model refused
or not answering, an existing output without `--resume` or `--force`, a write
error).

### 2.5 Drafting the scope values: `scripts/draft_policies.py`

`session_policy.yaml` and `memory_policy.yaml` describe one dimension of your
warehouse, the one a ranking question is scoped to when it names no scope of its
own (a region, a sales channel, a hall):

- `session_policy.yaml`, `default_scope`: the table it is (`filter_key`), the
  label shown while the question leaves it open (`default_label`), the options the
  one-click clarification offers (`options`) and its prompt text.
- `memory_policy.yaml`, `keys`: the **closed** set of values an analyst may pin
  (`options`), with the column they constrain (`column`) and a length cap. The pin
  on an assumption chip and `PUT /v2/memory/{key}` accept a value only when it is
  one of the options.

No setup tool wrote them before 6.16.1: the wizard drafts entities, aliases, rules
and examples, the inspector and the sync write `schema.yaml`, and
`scripts/draft_descriptions.py` writes descriptions. So a first install ran on the
template's made-up options (`Ring A`, `Ring B`, `All rings`): the server started and
memory seemed to work, but every real value an analyst tried to pin was outside the
closed set and refused, and the clarification offered values the warehouse does not
have. To the operator the feature looked gone. The preflight check `Session and
memory policies use real values` (§3.1) now says so, and this command writes the
real values:

```bash
python scripts/draft_policies.py --dry-run                      # counts only, nothing written
python scripts/draft_policies.py                                # -> project_config_draft/
python scripts/draft_policies.py --table Region --column Name   # choose the dimension
```

It is a separate command, like `draft_descriptions.py`, and not a step of the
wizard. The wizard reads every table and asks a model; this reads one small column
and asks nothing. And it has to be run again whenever the dimension gains a value,
which should not mean running the wizard.

**The dimension.** `--table` (a `schema.yaml` key or a bare name) and `--column`;
without them, the `filter_key` of `session_policy.yaml` and the `column` of the
memory key that shares it, as long as they name a real table and column of
`schema.yaml`. A table is found the way the application finds a configured table
name (`Customer` or `sales.Customer`, case and brackets ignored); a name that fits
several tables is refused, and so is a column the table does not list. A column
named like personal or contact data, or the `Name` of a table of people, is never
read (the wizard's rules, `core/value_shapes.py`). A memory key with no `options`
(any value up to `max_length`) is left as it is unless `--memory-key` names it.

**The read.** One bounded statement per run,
`SELECT DISTINCT TOP (N+1) <column> FROM <table> WHERE <column> IS NOT NULL ORDER BY <column>`,
through the application's own engine and login (`database.executor`: a transaction
that is always rolled back, the query timeout, the row cap, `WITH (NOLOCK)` where
the source asks for it), on the data source of the table's `datasource:` (the
default source without one). `N` is `--max-values` (default 200, and at most
`MAX_ROWS_RETURNED - 1`). A column with more than `N` distinct values is refused
with an explanation: a scope must be a small dimension, and a clarification with
two hundred buttons is no clarification.

**What is kept.** The values the wizard's rules accept (`core.value_shapes`): no
date, number, code, phone number, e-mail or IP address, URL, address, masked value
or error text, nothing over 40 characters or over the memory key's `max_length`,
nothing equal to the session policy's `default_label`. Values that differ only by
what the project's Persian and Arabic folding ignores (the Arabic and Persian forms
of yeh and kaf, digit forms, the half-space, spacing, letter case) are one value;
the spelling kept is the first in sorted order, the Persian letters before the
Arabic. The list is sorted. The counts of what was left out, by reason, are printed.

**What is written.** `project_config_draft/memory_policy.yaml` and
`project_config_draft/session_policy.yaml`: the current files with only `options`
replaced (and `filter_key` / `column` when `--table` / `--column` moved the
dimension). Comments, `max_length`, `field_name`, `default_label`,
`clarification_prompt` and any other field stay byte for byte; the script loads the
result with the application's own validators and compares it with the input, and
writes nothing when anything but those fields would differ. The session options are
the `default_label` followed by the values, as in the template; the memory options
never contain the `default_label` (so a pinned value always differs textually from
the unset default). Unlike the other drafting tools they put no draft header on the files (the values
are read from the warehouse, not guessed), but read them before you copy them over
`project_config/`, then run the preflight.

**Where it writes.** `project_config_draft/` unless `--output` says otherwise. The
live configuration directory and `project_config.example/` are refused unless
`--overwrite-live` is given; then each file it replaces is first copied to
`<name>.bak-<YYYYmmdd-HHMMSS>` next to it, as the wizard does. An existing draft is
not replaced without `--force`. `--config-dir` reads from another directory.

**What is printed.** Counts: the values read, the options kept, the reasons for the
rest. The values are warehouse data, so they are printed only with `--show-values`
(the first ten); the files hold them, as they must. `--dry-run` reads the column
and writes nothing.

Exit codes: 0 done, or a dry run; 2 nothing could be decided or written (a file
does not load, the dimension is not a real table and column, the column has too
many values or none usable, the output is protected or already holds a draft, the
database cannot be read).

Two things to know before you move the dimension. `field_name`, `default_label` and
`clarification_prompt` are not touched, so after `--table` check that they still
describe the new dimension. And the scope a question names is recognised through
`ring_aliases` of `aliases.yaml`, which the retriever stores under the key `Ring`
(`retrieval/value_retriever.py`); `filter_key` is the key the session reads it
under, so for a ring named in the question to count as the scope, `filter_key` must
stay `Ring`, and the preflight asks for a table of that name in `schema.yaml`.

## 3. Run the preflight

```bash
python scripts/verify_deployment.py
```

This must print `0 failed` before you go further. Every line is
`[PASS] / [FAIL] / [SKIP]` with a reason — a `[FAIL]` tells you exactly
what to fix (a placeholder still in `.env`, an unreachable database, a
model name the endpoint doesn't actually serve, no API key configured, a
`denied_columns` name that no column has, an
unwritable log directory, a `project_config/` that fails to load, a `project_config/` that is
a raw draft from the schema inspector or the setup wizard, synonyms copied from rows,
entities that name no table of `schema.yaml`, session and memory policies that still hold the
template's sample options, a data source with no table, a rate
limit too tight for your expected number of analysts, or a web UI address the
CORS list would refuse). `[SKIP]` is
not a failure — it means a check couldn't run (e.g. no database reachable
yet to test the row cap against), not that something is wrong.

The preflight has twenty-six checks with one data source (twenty-five in 6.16.0, twenty-four in 6.15.0, twenty-two in 6.14.1, before two
file checks were added), and a twenty-seventh, `Filter value check has vocabularies`, while `FILTER_VALUE_CHECK=true`. The checks run in this order: `Settings.validate()` (required settings,
leftover placeholders, `.env` lines python-dotenv cannot use),
`Tables map to data sources`, then `Database connectivity`,
`Login is read-only`, `Row cap` and `Query timeout`, then
`Tables are in their data source`, `Schema structure matches the databases`,
`OpenAI-compatible model exists`, `Model endpoint answers each prompt itself`,
`API key authentication`, `Column restrictions name real columns` (the keys
and `schema.yaml`; no database call), `Audit log directory writable`,
`Session store directory writable`, `project_config/ loads` (all ten
required files), the five checks that tell a curated `project_config/` from a raw
draft (`project_config/ is curated, not a raw draft`, `Aliases are curated`,
`Every data source is described`, `No staging or change-capture tables` and
`No live sample values in descriptions`: file reads only, no database call),
`No table in an excluded schema` (advice, also file reads only),
`Aliases hold no raw data values`, `Entities match schema keys` and
`Session and memory policies use real values` (file reads only),
`Rate limit sane for deployment` and `Web UI origin is allowed by CORS` (settings
only). With several data sources the four database
checks run once per source and each name carries the source, as
`Database connectivity [sales]`; `Tables are in their data source` is skipped
with one source. The last line is `N passed, N failed, N skipped`, and the
exit code is 1 when anything failed.

Three optional environment variables sharpen three of the checks without
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
- `VERIFY_UI_ORIGIN=<address>` — the address the web UI is opened with, as
  the browser's address bar shows it (`http://172.16.101.42:8077`; a trailing
  slash or a path is ignored). The preflight, the CLI and `curl` send no
  `Origin`, so none of them can notice that a browser would be refused;
  with this set, `Web UI origin is allowed by CORS` passes when the address is
  in `CORS_ALLOWED_ORIGINS` and fails, printing the `.env` line to add,
  when it is not. Run it once per address people use.

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
reason; there is no warning status. Advice is a `[PASS]` line whose reason
starts with `advice:` (the admin panel shows it the same way, since it has no
other status): it never changes the exit code. `[FAIL]` is a problem to fix before going
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
| `Tables are in their data source` | Compares `schema.yaml` with each source's catalogue and fails for a table whose columns are all missing from the source it is assigned to while another source has it. | `N table(s): <table>: not in <assigned>, found in <other> — set datasource: <other>; ...` (ten hints, then `and N more`). The table has no `datasource:` (so it runs on the default source) or the wrong one. Run `python scripts/sync_schema.py` and replace `schema.yaml` (§16.3); this is step 6 and 7 of "First install, in order". Or `could not compare schema.yaml with the data sources: ...` when a catalogue cannot be read. | One data source (`one data source -- nothing to place`). |
| `Schema structure matches the databases` | Runs `python scripts/sync_schema.py --check` in memory: reads three catalogue views per source and fails when `schema.yaml` is out of step with them: a column the database has and the file lacks, a recorded type that differs, a column or table no longer in the database, a `datasource:` line to set. A column type that is merely not recorded yet is not drift (below). | `N column(s) missing from schema.yaml; N column(s) and N table(s) no longer in the database; N column type(s) differ; N datasource: line(s) to set[; N column type(s) to record] -- run python scripts/sync_schema.py ...`. Run it, review `schema.synced.yaml`, replace `schema.yaml` (§16.3); this is step 6 and 7 of "First install, in order". `schema.yaml differs from the synced text in a way the drift counts do not cover -- run python scripts/sync_schema.py --dry-run to see it` means the file differs from the synced text in something the counts do not name: read the dry run. A column already marked `# not in database` does not fail it; the detail of a `[PASS]` counts them. A `schema.yaml` written before 6.9.0 has no `column_types:` map; from 6.9.2 that is a `[PASS]` whose detail ends `N column type(s) not recorded yet -- run python scripts/sync_schema.py once to record them (review schema.synced.yaml, replace schema.yaml), so later type changes are detected`. Do it once, so a later type change is detected. | A database cannot be read (its own connectivity check fails), `schema.yaml` does not load (`project_config/ loads` says why), or its layout cannot be edited line by line (a flow-style table or `columns: {...}`). |
| `OpenAI-compatible model exists` | Asks `OPENAI_BASE_URL` for `/models` (5 second timeout, `OPENAI_API_KEY` as the bearer token) and looks for `OPENAI_MODEL` among the ids it lists. | `could not reach <base>: ...`: wrong URL or port, endpoint down, a proxy, or an endpoint with no `/models` route. Or `'<model>' not found among models <base> lists: [...]`: set `OPENAI_MODEL` to one of the listed ids. | Never. |
| `Model endpoint answers each prompt itself` | Sends two tiny requests to the model through the same backend settings the server uses (`OPENAI_BASE_URL`, `LLM_EXTRA_BODY`, the seed, `temperature=0`; only `max_tokens` is cut to at most 128, 15 second timeout). Each asks for one fresh random token (`Reply with exactly this token and nothing else: <32 hex characters>`); the two prompts differ only in that token, so a semantic or prefix response cache collides on the second. `[PASS]` when each reply contains its own token (a model that adds words around it still passes); the detail gives the reply times, as `each test request was answered with its own token (replies in 412 ms and 398 ms)`, and never the reply. | `the second test request, which carried a different random token, was answered with the first request's token (...). A response cache in front of the model returned another prompt's answer. The same cache can serve the wrong SQL for a similar question ...`, or `the reply to the first/second test request did not contain its own random token`: a response cache in front of the model (a gateway with semantic or prefix caching) is answering a new prompt with the reply stored for a similar one. Disable response caching for this client at the gateway, or point `OPENAI_BASE_URL` at the model server directly; §20.10 says what to look for. A model that ignores the instruction fails the second form too: ask the model server directly to tell the two apart. | `LLM_PROVIDER=mock`; the endpoint is remote and `LLM_ALLOW_REMOTE` is off (nothing is sent to it); the endpoint did not answer (`OpenAI-compatible model exists` reports that); or a reasoning model used all its tokens before answering (turn reasoning off for the request with `LLM_EXTRA_BODY`). Not part of the admin panel's ordinary run: see below. |
| `API key authentication` | Checks that at least one key is configured (`API_KEYS_FILE` or `API_KEYS_JSON`, plus the application database) so that the server would start, and, with `VERIFY_API_KEY` set, that this raw key authenticates. | `API key configuration is invalid: ...` (the server would refuse to start; §5); `AUTH_REQUIRED is true but there are no configured keys ...` (issue one, §1); every key revoked or disabled in the application database; or `VERIFY_API_KEY was set but did not match any configured key's SHA-256 digest` (a truncated paste, or the `key_sha256` pasted instead of the raw key). With `AUTH_REQUIRED=false` it passes and says so; do not use that in production. | Never. |
| `Column restrictions name real columns` | Reads the keys the server would use (`API_KEYS_FILE` or `API_KEYS_JSON`, plus the application database) and `schema.yaml`. A plain `denied_columns` entry (no `.` and no `:`) is cleaned of whitespace and invisible Unicode characters at its two ends, as the guard reads it, and compared with every column name of `schema.yaml` without regard to case; it matches when it, or it without any format character inside it (a zero-width non-joiner in a Persian name is kept in the first spelling), is a column name. Entries with a `.` or `:` are checked when the key is loaded and are not looked at here. | `N plain denied_columns entries of M key(s) match no column of schema.yaml, so they deny nothing -- <key id>: 'Entry', ...`, one group per set of keys with the same entries, each entry in `ascii()` form so an invisible character shows (`'\u200c'`). A typo, a column that was renamed, or a name with an inner space (`National ID`). Correct the entry in `API_KEYS_FILE` / `API_KEYS_JSON` and restart, or save it again with `PATCH /admin/keys/{key}/acl` for a key of the admin panel (§2). The server starts with the entry as it is; until it is fixed it restricts nothing. When every entry matches, a `[PASS]` line starting `advice: ...` lists, per denied plain name, the columns that look like it and are not denied (the name appears inside theirs, or their stem has another ending among `ID`, `Code`, `No`, `Num`, `Number`: `NationalID` next to `NationalCode` and `CustomerNationalID`), with counts and the first names, never a value. It is a guess from spelling and never fails: read each name and add the ones that hold the same data. | No key is configured, the key configuration is invalid (`API key authentication` says why), or `schema.yaml` does not load or lists no column. |
| `Audit log directory writable` | Creates `LOG_DIR` if needed and writes then deletes a probe file in it; checks an existing `audit_log.jsonl` is writable. It never writes to `audit_log.jsonl` itself. | `could not create ...` or `... is not writable`: fix the directory's permissions or `LOG_DIR`. Fail it loudly here because a failing audit write never fails the user's query (§6). | Never. |
| `Session store directory writable` | The same probe on the directory of `SESSION_STORE_PATH`. | `could not create ...` or `... is not writable`. | `SESSION_STORE_PATH` is empty (persistence deliberately off). |
| `project_config/ loads` | Checks that all ten required files exist (`aliases.yaml`, `entities.yaml`, `business_rules.yaml`, `examples.yaml`, `metrics.yaml`, `schema.yaml`, `retrieval_hints.yaml`, `session_policy.yaml`, `memory_policy.yaml`, `system_prompt.md`), then loads each under the current code's models. An empty `system_prompt.md` fails. | `N of 10 required files not found under '<dir>': <names>`, with every missing name at once (copy each from `project_config.example/`, §2.1), `<file> not found under '<dir>'`, or `<file> failed validation ...` with the field: a file copied from an older deployment is missing a field a later release requires. Edit the field, or compare with `project_config.example/`. Duplicate YAML keys are refused (§17 step 3). | Never. |
| `project_config/ is curated, not a raw draft` | Reads the first lines of every `*.yaml` file in `project_config/` for the header a draft carries (`# AUTO-GENERATED DRAFT` from `database.schema_inspector_cli`, `# AUTO-GENERATED by setup_project.py` from the wizard, `# AUTO-GENERATED by draft_descriptions.py` from `scripts/draft_descriptions.py`), and counts in `schema.yaml` the tables whose description is still the older inspector's placeholder (`<schema.table> — auto-detected <kind> table (~N rows)`), the tables described by nothing but their own name (`description: 'Sales_Fact.Trade'`: the key, the bare name or `Schema.Table`, which is what `sync_schema.py --add-tables` and the inspector since 6.9.3 write) and the column descriptions that are only `<type> column` (`bigint column`). It reads `schema.yaml` even when a repeated key (an inspector draft from before 6.9.3) keeps the application from loading it. No database call. | `N file(s) carry the AUTO-GENERATED DRAFT header: <files>` (for the wizard's files, the same with `AUTO-GENERATED by setup_project.py`, and for the file of `scripts/draft_descriptions.py` with `AUTO-GENERATED by draft_descriptions.py`) and/or `schema.yaml: N of M table descriptions (P%) are still the placeholder`, `... are only the table's own name` or `schema.yaml: N of M column descriptions (P%) are only <type> column`: the files are a draft, not a curated configuration (a draft lists every table of one database, so a curated file that was replaced by it loses its descriptions, its aliases and, with two databases, one of them). On an upgrade, restore the curated files from your backup or version control, and bring new tables in with `python scripts/sync_schema.py --add-tables 'Schema.*'` (§2.3, §16.3), which keeps the curated text. On a first install there are no curated files to restore: draft the descriptions with `python scripts/draft_descriptions.py` (§2.4), read `schema.described.yaml` (every drafted line ends with `# REVIEW`), replace `schema.yaml` with it and delete its header line. A file you really did review passes once its header line is deleted. The older placeholder counts when more than a quarter of the descriptions are it, the other two when more than half are; a few tables added with `--add-tables` among curated ones show in the `[PASS]` line (`3 of 37 only repeat the table's name`) and do not fail the check, but a file made mostly of added tables is a draft again, and so is one whose header line was deleted. Restore the curated files, or describe the added tables (§2.4). | `schema.yaml` is missing or unreadable (`project_config/ loads` says which). |
| `Aliases are curated` | Reads `aliases.yaml`. Counts the synonym keys that are not words (no run of two letters: timestamps, numbers) and the synonym lines that still end in the inspector's `# REVIEW` marker. It also reads an `aliases.yaml` that is not valid YAML (a draft from before 6.9.3 with a quote in a sample value), from its lines. | `N of M synonym keys are not words (timestamps or numbers)` or ``N of M synonym entries still end in the inspector's `# REVIEW` marker``, more than half of them: the synonyms are the inspector's seeds (sample values of string columns, not a vocabulary). Restore the curated `aliases.yaml`. An empty `ring_aliases` is **not** a failure: only `ValueRetriever.extract_ring` reads it, to set the `Ring` filter from a ring (hall) name in the question, so a warehouse without that dimension leaves it empty (the setup wizard writes it empty too when it finds no dimension table). It shows as `advice: ring_aliases is empty` in a `[PASS]` line while `schema.yaml` or `entities.yaml` exists; add the ring names if your questions do name one. | `aliases.yaml` is missing. |
| `Every data source is described` | Reads `datasources.yaml` (one source named `default` without it) and `schema.yaml`: a table belongs to the source its `datasource:` line names (to each, for a list) and to the default source when it has none. | `N of M data source(s) have no table in schema.yaml: <sources>`: a whole database is missing from the configuration (a `schema.yaml` drafted from one of two databases is the usual cause). Restore the curated `schema.yaml`, or add the tables with `python scripts/sync_schema.py --add-tables 'Schema.*'`. `schema.yaml describes no table` when the file has none. A `datasources.yaml` listing a second source while `schema.yaml` has no `datasource:` line fails here too, naming the second source: run `scripts/sync_schema.py` (§16.3) to write the lines. | `schema.yaml` or `datasources.yaml` does not load (their own checks say why). |
| `No staging or change-capture tables` | Counts the `schema.yaml` tables whose schema or name matches `STAGING_PATTERNS` (default `*_Stg,*stg*,*staging*,table:*_Inserts,table:*_Updates,table:*_Deletes`). Comma-separated `fnmatch` patterns, case-insensitive; a bare pattern is matched against the schema, a `table:` prefix against the table name; empty switches the check off. | Never fails. A `[PASS]` line starting `advice: N of M table(s) look like staging or change-capture tables (<schema>: N, ...)` lists the counts per schema (never a value). Those tables repeat the ones analysts ask about, so the model sees the same name several times and the prompt budget goes to them; take them out of `schema.yaml` unless analysts need them (the inspector's `--exclude-schemas` keeps them out of a draft). | `STAGING_PATTERNS` is empty, or `schema.yaml` does not load. |
| `No live sample values in descriptions` | Looks at the text after each `e.g.` in a table description or column note (outside a parenthesis) for a run of 7 or more digits (phone-like; Persian digits too), exactly 10 digits (national-id-like) or a long segment of words with digits (address-like). Dates and plain words are left alone. | Never fails. A `[PASS]` line starting `advice: N note(s) in M table(s) seem to quote live sample values (phone-like: N, national-id-like: N, address-like: N); tables: <keys>` gives counts and table keys, never a value. A `schema.yaml` drafted by a release before the inspector stopped writing values quotes up to three values of each text column; the notes go into the prompt sent to the model. Replace each `e.g.` list with a description of the column. | `schema.yaml` does not load. |
| `No table in an excluded schema` | Reads `schema.yaml` and the exclusion rule of each table's data source(s): the system schemas, `EXCLUDE_SCHEMAS` and the source's `exclude_schemas:` (§2.3). A table counts when its schema (`db_schema`, or the key's qualifier) is excluded in every source it is assigned to; a table whose schema is unknown is never counted. | Never fails. A `[PASS]` line starting `advice: N of M table(s) are in a schema the setup tools are told not to scan: <keys>` lists the first keys and the two fixes: remove the tables from `schema.yaml`, or narrow the exclusion. The exclusion only limits what the inspector and `sync_schema.py` propose, so such a table is still synced. `[SKIP]` when nothing is excluded beyond the system schemas and no table is in one. | `schema.yaml` or `datasources.yaml` does not load. |
| `Aliases hold no raw data values` | Reads `aliases.yaml` and counts the `synonyms` keys that have the shape of a value copied out of a row: a date or time (`1387/03/24`, `2024-05-01 10:00:00`), a phone number, an IP address, an e-mail address or URL, a masked value (`ab xxxxx cd`, `****1234`), a hash, a run of 8 or more digits (a national id) or an address (words with digits and a street word). A year, `q1` or any word is left alone; Persian digits are read as digits. It also reads an `aliases.yaml` that is not valid YAML, from its lines. | More than 5 such keys: `N of M synonym keys look like raw data values (<date>: N, <phone>: N, ...)`, the count for each shape and never a key. These are the synonyms the wizard of 6.13 and 6.14 built from sample values; they go into the prompt and stay in version control, and the check cannot tell a person's name from a word, so read the file as well. Delete them (keep only words people type); the wizard no longer writes them unless `--value-synonyms` is given (§2.2). | `aliases.yaml` is missing. |
| `Entities match schema keys` | Reads `entities.yaml`, the keys of `ring_aliases` and `schema.yaml`. Retrieval takes an entity's key to be the name of a table (`retrieval/table_keys.py`), so it has to be that table's key in `schema.yaml`: a bare name for a table keyed by its bare name, `Schema.Table` for one keyed so; the bare name of a `Schema.Table` key is accepted when it picks out one key. `db_schema` does not help: `Sales_Dim.ActionType` finds nothing when the table is keyed `ActionType`. It reports an entity key that is no table's key, two entities for one table, and a `ring_aliases` key that names a table of `schema.yaml` by something other than its key (the name of a hall that is no table is left alone, as in a curated file). | `N entity key(s) are not a schema.yaml table key: Sales_Dim.ActionType (use ActionType), ...`, `N table(s) have two entities: Sales_Dim.ActionType (ActionType and Sales_Dim.ActionType), ...` and `N ring_aliases key(s) name a table but not by its schema.yaml key: ...`: the keys and the key to use, never a value. This is what the wizard of 6.13 and 6.14 left behind: it wrote every table as `Schema.Table` while `schema.yaml` keys a table whose name is unique by its bare name, so the qualified entity matched nothing and the bare duplicate beside it had no aliases; retrieval and alias matching lost those tables, and nothing else failed. Rename each key to the one in brackets, merge the aliases of two entities into one, and give `ring_aliases` the same keys; running the wizard again over the same output folder merges them for you (§2.2). | `schema.yaml` or `entities.yaml` does not load. |
| `Session and memory policies use real values` | Reads `session_policy.yaml`, `memory_policy.yaml` and `schema.yaml`. Each `filter_key` must name a table of `schema.yaml` (found as the application finds a configured table name, §2.5), each memory key's `column` must be a column of it (a table that lists no column gives no verdict), and the options of a file must not be exactly the sample values of `project_config.example/` (compared as a set; a set that differs by one value is not flagged). Reading `project_config.example/` itself, it does not compare the template with itself, so the shipped example passes. PASS may carry advice: when the running application has cached the dimension's values (`prefetchable_columns`; the admin panel's card, since the command line has no cache), the number of options that are not among them, as counts. | `session_policy.yaml default_scope: options are the sample values of the shipped session_policy.yaml`, `memory_policy.yaml keys.scope: filter_key 'X' names no table of schema.yaml`, `... column 'Y' is not a column of T in schema.yaml`. Values are never printed. A first install still has the template's options, so every real value an analyst pins is refused and the clarification offers values the warehouse does not have. Draft the real ones with `python scripts/draft_policies.py` (§2.5), read them and copy the two files into `project_config/`. A deployment that works with a `filter_key` that is no `schema.yaml` table fails here too, though the server answers as before: add the table to `schema.yaml` or correct the key. | `schema.yaml` or a policy file does not load; `project_config/ loads` says why. |
| `Rate limit sane for deployment` | Works out requests per second per analyst from `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW_SEC` and `RATE_LIMIT_BURST`, assuming `VERIFY_EXPECTED_ANALYSTS` (default 10) analysts share one key's bucket. | Under 0.1 requests per second per analyst: raise `RATE_LIMIT_REQUESTS`, or set `VERIFY_EXPECTED_ANALYSTS` to the real number. | Never. |
| `Web UI origin is allowed by CORS` | Settings only, no network. With `VERIFY_UI_ORIGIN` set (the address people open the UI at, e.g. `http://172.16.101.42:8077`), looks for that origin in `CORS_ALLOWED_ORIGINS`; `*` allows any. Without it nothing can be proved, so it only advises (below). | With `VERIFY_UI_ORIGIN` set and not listed: `http://localhost:8077 is not in CORS_ALLOWED_ORIGINS (http://172.16.101.42:8077): a page opened at that address gets 'Failed to fetch' and every light shows red. Set in .env and restart the API: CORS_ALLOWED_ORIGINS=http://172.16.101.42:8077,http://localhost:8077 -- or open the page with exactly an allowed address (scheme, host and port)`. Add the line (the variable replaces the list, so the line carries the current entries too) and restart, or open the page with an allowed address. Also a `VERIFY_UI_ORIGIN` that is not `scheme://host[:port]`. Without `VERIFY_UI_ORIGIN` it never fails; a `[PASS]` line starting `advice:` appears when every allowed origin is loopback while `API_HOST` is not (the API is reachable from other machines, whose pages would be refused), or when exactly one non-loopback origin is allowed (the same page opened by host name, as `localhost`, or on another port is refused). It says to list every address the page is opened with (IP address, host name, `localhost`) and to set `VERIFY_UI_ORIGIN` to check one. It reads `API_HOST`, not a `--host` given to `uvicorn` on the command line. | Never. |
| `Filter value check has vocabularies` | Listed only while `FILTER_VALUE_CHECK=true` (§20.11), last. Reads `schema.yaml` and `entities.yaml`: for each dimension table (one `entities.yaml` names) it looks for label-like columns (`Name`, `Title`, `Kind`, `Status`, `Type` ..., not a person's name or contact data) and for `prefetchable_columns`. | Never fails. A `[PASS]` line starting `advice: N dimension table(s) have label-like columns and no prefetchable_columns, so an invented value in them is not caught: <table> (<columns>), ...` names tables and columns, never a value. Add `prefetchable_columns` to the small ones in `schema.yaml` (a few hundred distinct values at most) and leave out tables of people and anything large. | `schema.yaml` or `entities.yaml` cannot be read -- `project_config/ loads` says why. |

The most common first-install `[FAIL]` with several data sources is
`Tables are in their data source -- 44 table(s): ... not in sales, found in inventory — set datasource: inventory`.
It means `scripts/sync_schema.py` (or `scripts/assign_datasources.py`) was never run, or its proposal was not
moved over `schema.yaml`. The same findings are on the admin panel's schema-drift
card (§16.8). `GET /admin/health/checks` runs these checks from the panel; it
leaves out `Login is read-only`, `Query timeout`, `Tables are in their data
source`, `Schema structure matches the databases` and `Model endpoint answers each
prompt itself` unless "deep checks" is pressed (§12). The last one is a deep check
because it is the only check that spends model-server time: two real completions,
a line in the gateway's own log and, behind a response cache, two junk entries in
it each time it runs. It tests a property of the deployment that changes only when
the gateway does, so it belongs where an operator asks for it, not on a page load
or a timer; the command line always runs it.
The nine checks above that read only `project_config/` run on the panel without it: they
only read files. So does `Column restrictions name real columns` (it reads the keys and `schema.yaml`), and so does `Web UI origin is allowed by CORS` (it reads settings only);
on the panel it sees the server's own environment, so `VERIFY_UI_ORIGIN` has to be
set there for it to be more than advice.

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
> below logs it as `CORS allowed origins: ...`). The page must then be
> opened with **exactly** one of the listed addresses: the same machine
> reached as `localhost` instead of by its IP address is a different origin
> and is refused. From the first refused request the server names the origin
> in its log (§5), and the UI says so in a banner at the top (§5.1).

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

When a request arrives from an origin that is not in the list, the server
also logs one `WARNING` for that origin (once per origin while the process
lives, whether the request is a preflight or an ordinary one):

```
CORS: origin 'http://localhost:8077' is not allowed (allowed: http://172.16.101.42:8077). Browsers show this as 'Failed to fetch'. To allow it add this line to .env and restart: CORS_ALLOWED_ORIGINS=http://172.16.101.42:8077,http://localhost:8077 -- or open the page with exactly an allowed address (scheme, host and port). Logged once per origin.
```

It names the rejected origin, the allowed list and the exact `.env` line; the
policy itself is unchanged (the preflight is still answered `400`). At most 64
distinct origins are remembered; after that one line says
`CORS: more than 64 distinct rejected origins seen; further rejected origins are not logged.`
The origin is request text, so control characters are removed and it is cut at
200 characters. A request to the server's own address (the `Origin` equals the
`Host`) is same-origin and is not reported.

With several data sources, also look for one line per source saying which
prompt path its questions take (logged at start-up, or at first use if that
comes first):

```
Prompt path for data source 'sales': static prefix (cacheable) -- 26 table(s), static prefix estimate 5100 tokens, PROMPT_RETRIEVAL_TOKEN_BUDGET 6000 (per source)
```

`retrieval restricted to its tables` in place of `static prefix (cacheable)`
means that source's prefix is over the budget; §16.6 says what to do about it.

Then, unless `LLM_PREFIX_WARMUP_ON_STARTUP=false`, one line per source from the
prefix-cache warm-up, which runs in the background after start-up (§20.2):

```
LLM prefix warm-up: source=(whole schema) warmed in 41873 ms, prompt_tokens=4612
```

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

### 5.1 The UI opens but cannot use the API

Both of these pass every preflight check that does not know the browser's
address (§3) and look healthy from the server's side, because the preflight,
the CLI and `curl` are not browsers:

| What you see | What the server log shows | Cause | Fix |
|---|---|---|---|
| The status lights (API, LLM, DB) are all red, a red banner at the top of the page says the connection to the API could not be made and lists two likely causes | Only `OPTIONS /health`, `OPTIONS /v2/sessions` and `OPTIONS /v2/access-requests`, each `400 Bad Request`, and the `CORS: origin '...' is not allowed` line above | The page was opened with an address that is not in `CORS_ALLOWED_ORIGINS` (here `http://localhost:8077` against a list holding only `http://172.16.101.42:8077`). A browser tells the page nothing more than `Failed to fetch`, and cannot tell this from an API that is down, so the banner names both | Add the origin the banner and the log line show: `CORS_ALLOWED_ORIGINS=<current list>,<rejected origin>`, restart, and open the page with exactly an allowed address (scheme, host and port). If the log has no such line and no request at all, the API is not running at the address the banner names |
| Every admin panel card says «به API نرسید؛ پیام بالای صفحه را ببینید.» (earlier releases showed a 404 on each), and a banner at the top says the address in the top bar is not the API | No `/admin/...` request at all | The API address saved in this browser (it takes precedence over `web/js/config.js`, and the admin panel's top bar saves it) is the address of the static file server (`web/`), which answers 404 to what it does not serve. The banner appears when the address is the page's own origin, or when `GET <address>/health` answers a 404, an HTML page, or JSON that is not the API's health response | Enter the API's address (`http://<host>:8000`) in the top bar and press «اتصال». It is saved and also applies to the analyst page. For the analyst page alone, set `DEFAULT_BASE_URL` in `web/js/config.js` |
| The analyst page shows the same banner for a wrong address (its text names `web/js/config.js` instead of the top bar, which that page does not have) | No request from this page reaches the API | The same as the row above | The same as the row above |

What the page can and cannot tell: a failed call that never got a response is
shown as the first row, a guess between a refused origin and an unreachable
host, because a browser hides the difference on purpose. The second row is a
verdict only when the address answers `/health` and is not the API; a
`401`, `403`, `429` or `5xx` is the API answering and keeps its own message on
the card.

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
- "LLM stage split" (present when `LLM_STREAM_TIMINGS=true` was on for part of
  the week; §20.4 says how to read it) — how much of the `llm` stage was
  waiting for the first token and how much was generating, and how many of
  the generated tokens were reasoning.

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
| Admin panel — deployment checks, deep (`GET /admin/health/checks?deep=1`) | Everything above, **plus** `check_login_is_read_only`'s always-rolled-back `CREATE TABLE`/`DROP TABLE` attempt, `check_query_timeout`'s multi-second `WAITFOR DELAY` probe, and (with more than one data source) `check_tables_in_assigned_sources`'s full catalogue reflection and `check_schema_structure_matches_databases`'s three catalogue queries per source (and, to the model endpoint rather than the warehouse, `check_model_answers_each_prompt`'s two tiny completions, §3.1) | Only when an operator explicitly presses the panel's "deep checks" button (confirmation dialog first) — never automatically, never on a timer. `python -m scripts.verify_deployment` (the CLI) still runs every check, deep included, every time it is invoked by hand or in CI | Not time-based — opt-in per click. Cached separately from the non-deep result under the same `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` |
| Admin panel — schema drift (`GET /admin/schema-drift`, `schema_data.drift.check_schema_drift`) | A full catalogue reflection: `get_table_names` + `get_columns` for every table of every schema, on each source a table lives in; plus, only with more than one source and only when some table is missing from its assigned source, one `INFORMATION_SCHEMA.TABLES` query per other source (the "found in ..." hint) | Once when the admin panel is opened, and again only on that card's own refresh button — not on the 30-second auto-refresh. Same cache/`?refresh=1` behaviour as above | `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` |
| Admin panel — every other card (audit summary, query cache stats, maintenance mode, feedback, keys, access requests, dimension-vocabulary status, per-analyst usage, auth failures) | No direct warehouse query — these read the audit log, the application database, or in-process bookkeeping | Every 30 seconds (`AUTO_REFRESH_MS` in `web/admin/main.js`) while the panel tab is visible, plus on open and on each card's own refresh button | Not warehouse-relevant; listed here only to be explicit about what the 30-second timer *does* still touch |
| Dimension-vocabulary refresh (`retrieval/dimension_vocabulary.py`) | A `DISTINCT`-style scan of one configured dimension column, as one single-table statement on the source that column's table routes to (a table listed under several sources is read from one copy: the default source if the table is there, else the first in `datasources.yaml` order) | At start-up when `DIMENSION_VOCABULARY_WARM_ON_STARTUP=true` (the default; set `false` for a start-up that must not touch the warehouse); afterwards lazily, at most once per column per TTL, triggered by the first `/query` request that needs a stale-or-missing entry (a background, non-blocking refresh — the triggering request itself is served from whatever was cached, stale or not); with `FILTER_VALUE_CHECK=true`, also by a statement that filters on a stale-or-missing prefetched column (§20.11) | `DIMENSION_VOCABULARY_TTL_SECONDS` (default `3600`), `DIMENSION_VOCABULARY_WARM_ON_STARTUP` |
| Relationship-map schema inspection (`database/relationship_map.py`) | Nothing, in a running deployment. The module reflects foreign keys only if no `project_config/relationships.yaml` exists and `AUTO_DISCOVER_SCHEMA=true`, but the server, the API and the CLI do not import it (the relationships in the prompt come from `schema.yaml`), so the setting causes no warehouse traffic today | Not applicable. Were it wired in, it would run at most once per process lifetime (cached in memory; never on a timer) and read `DB_CONNECTION_URL` itself, not `DB_PASSWORD` or `datasources.yaml` | `AUTO_DISCOVER_SCHEMA` (default `false`) |
| Operator scripts, run by hand: `scripts/assign_datasources.py` | Two metadata queries per data source, `INFORMATION_SCHEMA.TABLES` and `INFORMATION_SCHEMA.COLUMNS`, names only, no rows | Once per run | Not time-based. `--check` runs the same two queries |
| Operator scripts, run by hand: `scripts/sync_schema.py` (and the preflight check `Schema structure matches the databases`) | Three metadata queries per data source over one connection: `INFORMATION_SCHEMA.TABLES` joined to `COLUMNS` (names, data types, nullability), and the primary-key and foreign-key constraints from `TABLE_CONSTRAINTS`, `KEY_COLUMN_USAGE` and `REFERENTIAL_CONSTRAINTS`. No table is read | Once per run | Not time-based. `--check` and `--dry-run` run the same three queries |
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
| 3 | Sync `schema.yaml` with `sync_schema.py` (it writes each table's `datasource:`), replace `schema.yaml`, resolve what it marked | §16.3 |
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
   A source may also list `exclude_schemas: ["*_Stg", Replay]`: `fnmatch`
   patterns, compared without regard to case, for schemas of that source that
   the setup tools (the inspector, §2.3, and `sync_schema.py`, §16.3) must not
   scan, on top of the system schemas and the `EXCLUDE_SCHEMAS` setting that
   applies to every source. A list of non-empty strings; anything else is
   refused at start-up. Nothing at run time reads it. `exclude_tables:` (patterns on
   `schema.table`) and `keep_staging_schemas:` work the same way for single tables
   and for a staging schema to keep (§2.3).
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

### 16.3 Sync `schema.yaml` with the databases, and write each table's `datasource:`

A table with no `datasource:` belongs to the default source, and fails there
(`Invalid object name ...`) when it is really in another database. A table
that exists with the same shape in several sources (a date dimension replicated
into each database) takes a list, `datasource: [sales, inventory]`: distinct
names, each a configured source, in the same letter case as in
`datasources.yaml`. `schema.yaml` also drifts in other ways: columns are added
to the databases, a few it lists were dropped, a type changes. Do not chase
these by hand. From the repository root, with the server's environment active
(the sources' passwords in `.env`):

```bash
python scripts/sync_schema.py
```

It reads the structure of every source through the application's own
read-only engines (three `INFORMATION_SCHEMA` queries per source: tables,
views, columns, data types, nullability, primary and foreign keys; names and
types only, no row is read, nothing is written to a database) and writes
`schema.synced.yaml` next to `schema.yaml`. `schema.yaml` itself is never
changed, and `--output PATH` writes the proposal elsewhere. The proposal is your
file with every comment, description, flag and ordering kept, and only these
structural changes (the script parses its own output and refuses to write it
if anything else differs):

| Situation | What the proposal does |
|---|---|
| Table has no `datasource:`, or a wrong one (several sources only) | Sets it: `datasource: sales`, or `[sales, inventory]` for a table found in both, in `datasources.yaml` order |
| Column in a database, not in `schema.yaml` | Appends it to the table's `columns:` as `Name: "<type> column"  # TO BE FILLED (added by sync_schema.py)` and records its type |
| Column in `schema.yaml` with no entry in `column_types:` (every column of a file written before 6.9.0) | Records its type. This alone is not drift: `--check` and the preflight pass with a note saying how many types are not recorded yet |
| Column in `schema.yaml`, in no source's table | Keeps it and adds the line `# not in database (sync_schema.py)` above it; `--prune` removes it instead (never one still named in `resolvable_columns` or `prefetchable_columns`) |
| Recorded type differs from the database | Corrects the entry in the table's `column_types:` map and reports `Table.Column: old -> new`. Types are kept in that map, never in a description |
| Table in a database, not in `schema.yaml` | Reports it only; `--add-tables 'sales.*'` (a glob on `schema.table`, repeatable) appends the matching ones with draft descriptions. A table in an excluded schema (below) is neither reported nor added; the report counts them per schema |
| Table in `schema.yaml`, in no source | Keeps it with `# not found in any data source`; `--prune` removes it (unless `relationships:` still names it) |
| Table with no `columns:` key | Left alone (described only, deliberately not queryable) |

**Excluded schemas.** The report and `--add-tables` skip the tables of schemas
that are excluded for the source being read: the system schemas (`sys`,
`INFORMATION_SCHEMA`, `guest`, `cdc`, the fixed `db_*` role schemas, `pg_*`), the
`EXCLUDE_SCHEMAS` setting (every source), the source's `exclude_schemas:`
(`datasources.yaml`) and `--exclude-schemas PATTERNS` for one run (comma-separated
`fnmatch` patterns, repeatable; compared without regard to case). A pattern such
as `--add-tables '*'` therefore adds only tables outside those schemas, and the
report has a section `tables in excluded schemas, not listed or added` with the
number of tables skipped, per schema. This is for discovery only: a table
`schema.yaml` already lists is synced exactly as before, in whatever schema it
is, and an excluded table does not make `--check` fail. The preflight check
`No table in an excluded schema` (§3.1) lists a table of `schema.yaml` that sits
in an excluded schema, so the two do not silently disagree.

**Excluded tables.** `EXCLUDE_TABLES` (every source) and the source's `exclude_tables:`
(patterns on `schema.table`, §2.3) are applied in the same two places. A table they
name is neither listed nor added, and the report has its own section, `tables named
by an excluded table pattern, not listed or added`, with the count per schema. A
table whose schema is excluded is counted under the schema section only. The
staging rule is not part of the sync: `--add-tables 'X_Stg.*'` adds a staging
schema's tables whatever `STAGING_PATTERNS` says, so use `exclude_tables:` to keep
its change-capture tables out. A table `schema.yaml` already lists is synced as
before; the preflight check `No table in an excluded schema` also lists a listed
table that a pattern names.
`scripts/assign_datasources.py` only reads the tables `schema.yaml` lists, so it
has nothing to exclude and ignores the setting.

To bring a whole new schema into a deployment that already has a curated
`schema.yaml`, use `--add-tables 'Schema.*'` here, not the inspector of §2.3:
the inspector's drafts are for a first install only, and the sync keeps the text
you have already written.

`column_types:` is a new optional map per table, `{column: SQL type}`, written
only by this script; nothing reads it at run time, so the prompt and the guard's
allowlist are unchanged.

`column_units:` is another optional per-table map, `{column: rial | none}`, which
you write by hand. It says whether a number in a column is money, for one thing
only: the wording of the answer summary (`interpret`). The summary writes
Rial/ریال on money and on nothing else (not a count, a quantity, a volume, a
percentage, an id, a code or a date), and the prompt tells the model, per numeric
result column, which side each is on. A column you list is decided by you (`rial`
money, `none` not money). One you do not list is decided from its name: `price`,
`value`, `amount`, `cost`, `fee`, `ارزش`, `قیمت`, `مبلغ`, `کارمزد` and the like are
money; `count`, `quantity`, `volume`, `weight`, `percent`, `id`, `code`, `date`,
`حجم`, `تعداد`, `درصد`, `کد` and the like are not (a name with both, such as
`FeeCount`, is not money); when the name says nothing the model decides. The
lookup is by the column's name in the result, so a column you rename in a query
(`AS TotalVolume`) is judged by that name; if two tables give the same name
different units the name is not looked up. Keys must be columns of the table's
`columns:`; write `none`, not `no` (YAML reads `no` as false). The sync leaves the
map alone, except that `--prune` removes the entry of a column it removes. A
deployment that writes no `column_units:` changes only in this: the summary no
longer puts Rial/ریال on a volume or a count.

It also writes `relationships.proposed.yaml` next to `schema.yaml`: every
declared foreign key between two tables of `schema.yaml`, plus relationships
guessed from column names (`Order.CustomerID` to `Customer.ID`, `Order.OrderDate_ID`
to `Date.ID`; the same schema is preferred, both tables must share a data source,
and a guess is dropped when the target has a composite key, no key at all, a
different column type, or several candidates). Each entry says `basis:` (the
constraint, or the naming rule) and `confidence:`. Nothing reads this file and
`relationships.yaml` is never touched: review each entry and copy the right ones
by hand. `--no-relationships` skips it.

`--dry-run` prints the report without writing anything. The report lists, in
sections, the `datasource:` lines to set, the columns added, the columns
`schema.yaml` names that the database does not have, the types recorded and
corrected, the tables the databases have that the file lacks, the tables found
nowhere, and the columns that differ between sources of a shared table. A
column the read-only login cannot see through `INFORMATION_SCHEMA` is reported
as not in the database too, so check the `DENY` grants of `docs/db-hardening.md`
before running `--prune`.

When anything would change, the report also ends with a `== prompt size ==`
section: for each data source, the estimated static-prefix tokens of the current
`schema.yaml` and of the proposal, the difference, the path each takes (static
prefix or retrieval, by `PROMPT_RETRIEVAL_TOKEN_BUDGET`, §16.6), and how many
added columns and tables still carry the `TO BE FILLED` draft description. Every
added column and table is text the model reads on every request, so this is the
cost of the proposal before you replace `schema.yaml`. Both prefixes are built by
the code the server and `prompt_budget.py` use, so the numbers are the same
estimate (`len(text) // 4`), not real model tokens. A source the proposal would
move from the static prefix to retrieval gets a `WARNING:` line with both sizes
and the budget: review the draft columns, or raise the budget after running
`python scripts/prompt_budget.py`. It is advice only; no exit code changes and
`--check` is not affected. Without a `system_prompt.md` the section is one line
saying it was not computed.

Then:

1. Review `schema.synced.yaml` (`diff project_config/schema.yaml project_config/schema.synced.yaml`):
   look at the tables found nowhere and at every `[A, B]`, because a list asserts
   that the table has the same shape in each source.
2. Keep a copy of the current file, then replace it with the proposal:

   ```bash
   cp project_config/schema.yaml project_config/schema.yaml.bak
   mv project_config/schema.synced.yaml project_config/schema.yaml
   ```

   (PowerShell: `Copy-Item project_config\schema.yaml project_config\schema.yaml.bak`,
   then `Move-Item -Force project_config\schema.synced.yaml project_config\schema.yaml`.)
   `schema.yaml` is the guard's allowlist, so a change to it takes effect at
   the next restart whichever way it is made (by hand, or through the admin
   panel's draft and approval).
3. Resolve what the sync marked. A column that stays in `schema.yaml` without
   existing in the database is still allowed by the guard, so a question that
   uses it fails when the query runs (the admin panel shows it as
   *فقط در schema.yaml*, §16.8). For each `# not in database` column: delete it
   (or run `--prune` and replace the file again), correct its spelling, or, if it
   exists but the read-only login cannot see it, ask the DBA about the `DENY`
   grants before deleting it. Write real descriptions for the columns and tables
   marked `# TO BE FILLED`, or draft them with `python scripts/draft_descriptions.py`
   (§2.4) and review the result; the placeholder `<type> column` is what the model
   sees until you do.
4. Run `python scripts/sync_schema.py --check`. It writes nothing, prints
   `CHECK OK: schema.yaml's structure matches the databases` and exits 0 when
   `schema.yaml` is in step with the databases; otherwise it prints
   `CHECK FAILED: ...` with the counts and exits 1. Put it in the deploy pipeline
   to keep the structure honest (the preflight runs the same test as
   `Schema structure matches the databases`). Column types that are not recorded
   yet are not out of step: a file written before 6.9.0 has no `column_types:` map,
   so `--check` exits 0 and adds `N column type(s) not recorded yet -- run python
   scripts/sync_schema.py once to record them ...` to the `CHECK OK` line, until a
   plain run has recorded them in `schema.synced.yaml` and you have replaced
   `schema.yaml` with it (step 2). A column already
   marked `# not in database` does not fail `--check`; it is listed in every
   report until it is removed. `--check --prune` fails while anything is left to
   prune, and `--check --add-tables PATTERN` fails until the matching tables are
   listed. Exit code 2 means a source's catalogue could not be read (the run
   stops rather than guess, because an unreadable source would make a shared
   table look single), `schema.yaml` is invalid or laid out in a way that cannot
   be edited line by line (a flow-style table or `columns: {...}`), or the output
   would not validate. Running the script again on the replaced file changes
   nothing.

**`assign_datasources.py` is still there, and is the narrower tool.** It writes
only the `datasource:` lines (`schema.with_datasources.yaml`) and reports missing
columns; it never adds a column, records a type or marks anything, and it
shares its matching and its `datasource:` editing with `sync_schema.py` (the
same rules, the same code). Use it when you want exactly that and no other
change to `schema.yaml`, for example on a file whose layout `sync_schema.py`
reports it cannot edit. Everything it does, `sync_schema.py` does too, so it is
not the recommended first step; it stays so that existing pipelines keep working.

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
§3, once per source. The panel leaves out five on its own (the rolled-back
`CREATE TABLE`, the `WAITFOR` probe, `Tables are in their data source`,
`Schema structure matches the databases` and `Model endpoint answers each prompt
itself`) unless an operator presses "deep checks" and confirms; the command line
always runs all of them.

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

## 17. Upgrading from 6.0 to 6.16.1

One checklist for an installation that is running any 6.x release and is moving
to 6.16.1. It puts the **Upgrading** notes of 6.0.1 to 6.16.1 in the order to do
them, and adds what 6.7.0 and 6.9.0 bring that you will want to use (6.6.1 and
6.7.0 have no **Upgrading** notes of their own); `CHANGELOG.md` has each
release's full text. From 5.x, do the 6.0.0
notes first: copy `prompts/system_prompt.md` to
`<PROJECT_CONFIG_DIR>/system_prompt.md` before the first start (the server
refuses to start without it), and check a relative `PROJECT_CONFIG_DIR`,
which is now resolved against the repository root.

Every step names the release it comes from, so skip the ones for releases you
already run. An installation on **6.7.0** does steps 1 to 3, then 9 to 29, then
the parts of step 30 that are new in 6.9.0, and step 31 if it contributes code;
one on 6.8.0 does steps 1 to 3 and 11 to 29; one on 6.9.0 does steps 1 to 3 and
15 to 29; one on 6.9.1 does steps 1 to 3, step 14 if it never ran
`scripts/sync_schema.py` (which also records the column types the 6.9.2 note
of step 3 is about), step 15 if a console-encoding workaround is still in place,
and steps 16 to 28; one on 6.9.2 or 6.9.3 does steps 1 to 3, steps 16 to 27
and the restart in step 28 (6.9.3 changes no configuration format); one on
6.10.0 does steps 1 to 3, steps 17 to 27 and the restart in step 28; one on
6.11.0 does steps 1 to 3, steps 18 to 27 and the restart in step 28; one on
6.12.0 does steps 1 to 3, steps 19 to 27 and the restart in step 28; one on
6.13.0 does steps 1 to 3, steps 20 to 27 and the restart in step 28; one on
6.14.0 does steps 1 to 3, steps 21 to 27 and the restart in step 28; one on
6.14.1 does steps 1 to 3, steps 22 to 27 and the restart in step 28; one on
6.14.2 does steps 1 to 3, steps 23 to 27 and the restart in step 28; one on
6.15.0 does steps 1 to 3, steps 24 to 27 and the restart in step 28; one on
6.15.1 does steps 1 to 3, steps 25 to 27 and the restart in step 28; one on
6.15.2 does steps 1 to 3, steps 26 and 27 and the restart in step 28; one on
6.16.0 does steps 1 to 3, step 27 and the restart in step 28. Steps 1 to 4, 14
and 16 to 29 apply to every installation. Steps 5 to
8 are each optional: do the ones that fit (a password move, a key file, a UI on
another origin, several databases). Steps 9 to 13 and 15 each say what to check or decide, and
need no action on an installation that does not use the thing they change.
Steps 30 and 31 come last on purpose.

1. **Back up** `.env` and `project_config/`. Both are outside the repository.
   If you already have a golden set (6.7.0 or later), record its baseline now,
   on the version that is running, before the pull (§18.2): the gate compares
   the new version with a baseline recorded on the old one, and after the
   pull the old one is gone.
2. **Pull, then re-run the install.** Every upgrade starts with
   `pip install -r requirements.lock` (never `requirements.txt`; the pins keep
   `sqlglot`, which the SQL guard depends on, from changing unreviewed) (6.4.1:
   python-dotenv 1.2.4, so a `.env` saved as UTF-8 with a byte-order mark
   loads its first variable; 6.3.0: urllib3 2.8.0; 6.15.0: certifi 2026.10.19,
   for a published advisory). Releases 6.8.0 to 6.14.2 change no pin in
   `requirements.lock`, but run the command anyway.
3. **Run the preflight before restarting**, in its own process:
   `python scripts/verify_deployment.py` (§3). Several releases made the
   preflight or the server refuse input it used to accept silently, and this is
   where each refusal shows up with its cause:
   - *6.3.1, a YAML key written twice.* `[datasources.yaml] is not valid YAML:
     duplicate key 'datasources' (first on line 12, again on line 21)`. A
     duplicate in `datasources.yaml` or `schema.yaml` stops the server; one in
     another file fails when that file is first read; `relationships.yaml` is
     skipped with a warning. Remove the duplicate. The later one was the one in
     effect, so keep that block's content if it is what you meant. From 6.9.3
     the error lists every duplicate key in the file at once (`N duplicate keys:
     'X' (lines A, B, C); ...`, at most 20, then `... and M more`), so one run
     shows all of them; a single duplicate reads as above.
   - *6.4.0, a `.env` line python-dotenv cannot use.* `Invalid configuration:`
     with each line number and variable name. A multi-line `API_KEYS_JSON` must
     be wrapped in single quotes with no apostrophe inside, or moved to
     `API_KEYS_FILE` (step 6); a variable assigned twice with different values
     must lose the stale one (the later one was in effect).
   - *6.4.0, a repeated field inside one key object.* `Invalid API key
     configuration:`. Delete the repeat (a pasted second `denied_columns`
     used to replace the first silently).
   - *6.8.0, `project_config/ loads` reads all ten required files.* It used to
     read six, so a missing or invalid `retrieval_hints.yaml`,
     `session_policy.yaml`, `memory_policy.yaml` or `system_prompt.md` passed
     and failed later. It now names every missing file at once
     (`N of 10 required files not found under '<dir>': ...`); copy each from
     `project_config.example/` and edit it (§2.1).
   - *6.8.0, a scoped `denied_columns` entry that names nothing real.*
     `API key authentication` reports `API key configuration is invalid:
     <source>[<n>].denied_columns: ...` naming the entry (step 9).
   - *6.9.0, `Schema structure matches the databases` and the column types.*
     A `schema.yaml` written before 6.9.0 has no `column_types:` map, so the
     sync has a type to record for every column although nothing in the
     databases changed. 6.9.0 and 6.9.1 failed the check for that, with a
     message of all zeros (`0 column(s) missing from schema.yaml; 0 column(s)
     and 0 table(s) no longer in the database; ...`). From 6.9.2 an
     unrecorded type is not drift: when nothing else differs the check passes,
     and its detail ends `N column type(s) not recorded yet -- run python
     scripts/sync_schema.py once to record them (review schema.synced.yaml,
     replace schema.yaml), so later type changes are detected`. Step 14 records
     them, and it comes before the restart. The check still fails for real
     drift (a column the databases have and the file lacks, a recorded type that
     differs, a column or table the databases lost, a `datasource:` line to
     set), then with `N column type(s) to record` added to the counts. (It is
     skipped, not failed, when a database cannot be read.)
   - *6.11.0, a raw draft in place of the curated files.* Copying the
     output of `database.schema_inspector_cli` over `project_config/` loads and starts, and
     answers worse: the drafts list every table of one database (staging and change-capture
     tables included), describe each with a placeholder, quote live sample values and leave
     `ring_aliases` empty. The preflight now fails `project_config/ is curated, not a raw
     draft` (naming the files that carry the `# AUTO-GENERATED DRAFT` header and the share of
     placeholder descriptions), `Aliases are curated` and `Every data source is described` (a
     database whose tables are gone), and advises on staging tables and sample values in
     descriptions. Restore the curated files, then add tables with
     `python scripts/sync_schema.py --add-tables 'Schema.*'` (§16.3). `STAGING_PATTERNS` in
     `.env` sets what counts as a staging table. With several data sources a `schema.yaml`
     that has no `datasource:` line also fails `Every data source is described`, naming the
     second source: run `python scripts/sync_schema.py`, which writes the lines (step 17).
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
   read-only login on every server (6.1.0); add `description:` and `keywords:` to
   each source (6.5.0, optional); resolve the `datasource:` lines and the columns
   the databases lack with the sync of step 14 (6.5.0 introduced the
   `datasource:` lines and the report of missing columns, written then by
   `scripts/assign_datasources.py`; since 6.9.0 `scripts/sync_schema.py` does
   it); and after step 14 has replaced `schema.yaml`, run
   `python scripts/prompt_budget.py` and set the `PROMPT_RETRIEVAL_TOKEN_BUDGET`
   it prints (6.6.0). If the DBA requires `WITH (NOLOCK)`, set `nolock: true` on
   that source, and on each other source that needs it: the flag is per source
   (6.5.0, §16.7).
9. **Scoped, join-only `denied_columns` (6.8.0).** A plain name such as
   `NationalID` means what it always did: denied everywhere, every reference.
   Two things to do, one to know.
   - *Look at every key's `denied_columns` (key file, `API_KEYS_JSON`, and the
     admin panel's key editor) for an entry that contains `.` or `:`.* An entry
     name cannot contain either any more: such an entry is now read as a scoped
     entry, and if it names no real source, table or column the server stops at
     start-up and the preflight's `API key authentication` fails, naming it.
     Correct it or remove it. A mistyped scoped entry fails the same way
     instead of silently restricting nothing.
   - *Use the new forms where they fit (optional).* To keep a column usable as
     a join key while hiding its values, write `schema.Table.Col` (join-only on
     that table, whichever source runs the query), `Source:schema.Table.Col`
     (only when the query runs on `Source`) or `Source:Col` (every table of
     `Source` that has the column); §2 has the table and the rules. A
     join-only column is allowed only as one side of `a.col = b.col` inside a
     `JOIN ... ON`; anywhere else the guard refuses the statement with the
     correctable reason `join_only_column`. The model is told which columns are
     join-only in one line of the per-question part of the prompt, so the
     cached static prefix is unchanged. The admin key editor shows and accepts
     the new forms, and checks them when you save.
   - *Know that the query cache is invalidated once.* `scope_key` now hashes
     `denied_columns` as a JSON array, so every cached result is keyed afresh
     after the upgrade. Nothing needs clearing by hand.

   Join-only hides a column's value, not the fact that a row exists: a join on
   a filtered foreign key can still probe it. Restrict the foreign keys that
   point at a hidden key as well (§2).
10. **Setup wizard leftovers (6.8.0)**, only if you have ever run
    `setup_project.py` or copied its settings from `.env.example`.
    - *Delete any `.setup_log.json` written by an earlier version* (by default
      `project_config/.setup_log.json`, or under the `--output` directory you
      used), and rotate the database password if the file was shared or
      committed. Earlier versions stored the connection string in it exactly as
      typed, password included; the wizard now writes it masked (§2.2).
    - *Empty `WIZARD_LLM_MODEL` and `WIZARD_LLM_BASE_URL` in `.env`* if you copied
      the old `.env.example` values (`gpt-oss-20b`, and an empty base URL that
      meant `https://api.openai.com/v1`). Empty, they follow `OPENAI_MODEL` and
      `OPENAI_BASE_URL`, and the wizard prints one line naming the endpoint and
      model it uses. It now also refuses a remote endpoint unless
      `LLM_ALLOW_REMOTE=true`.
11. **Table retrieval selects differently by default (6.9.0).** Eleven new
    `RETRIEVAL_*` settings control which tables the retrieval path puts in the
    prompt; §18.5 lists them with their defaults. Among them
    `RETRIEVAL_EXTRA_TABLES` defaults to `3`, and `RETRIEVAL_JOIN_EXPANSION` and
    `RETRIEVAL_PRUNE` default to `true`. Only a data source on the retrieval path
    is affected (its schema is over `PROMPT_RETRIEVAL_TOKEN_BUDGET`; the
    `Prompt path for data source '<name>'` line of the start-up log says which
    path each source takes, §5); a source on the static prefix gets the same
    prompt as before. If you accept the new selection there is nothing to do. To
    restore the previous selection, put these three in `.env` and restart:

    ```ini
    RETRIEVAL_PRUNE=false
    RETRIEVAL_JOIN_EXPANSION=false
    RETRIEVAL_EXTRA_TABLES=0
    ```

    To see whether the change helps on your own questions, measure table
    selection before and after with `python -m eval.cli recall` (§18.4).
12. **Start-up now sends the model server one small request per data source
    (6.9.0).** `LLM_PREFIX_WARMUP_ON_STARTUP` defaults to `true`: a background
    thread sends each static-prefix source's prefix once (`max_tokens=1`) so the
    first question after a restart does not pay the whole prefill (§20). It
    sends nothing for a source on the retrieval path, with `LLM_PROVIDER=mock`,
    or to an endpoint that is not trusted while `LLM_ALLOW_REMOTE` is off, and it
    only helps if the model server caches prefixes (`vllm serve
    --enable-prefix-caching`). If the model server is shared and metered per
    token, or start-up must send it nothing, set `LLM_PREFIX_WARMUP_ON_STARTUP=false`.
    `LLM_PREFIX_WARMUP_TIMEOUT_SECONDS` (default `180`) bounds the pass and must
    be above zero, or the preflight's `Settings.validate()` fails. After the model
    server restarts on its own, `POST /admin/llm/warmup` (`operations`) does the
    same pass on demand (§20.3). `LLM_STREAM_TIMINGS` (default `false`) stays off
    until you turn it on (§20.4); nothing else needs doing.
13. **Regenerate any schema draft that may quote a password (6.9.0).** Drafts
    written by `database/schema_inspector.py` (`python -m database.schema_inspector_cli`:
    `schema.yaml`, `entities.yaml`, `aliases.yaml`, `relationships.yaml`) start
    with a comment `# Generated: <time> from <url>`. Earlier versions masked the
    password in that URL with a pattern that left part of a password containing
    `@` or `:` visible, and never masked `PWD=` inside an `odbc_connect` value.
    If such a draft was shared or quoted, and the line is still in a copy under
    `project_config/`, regenerate the draft (or edit the line out), and rotate
    the database password if it was exposed. The same redaction now covers the
    application database's refusal messages and the preflight's output.
14. **Sync `schema.yaml`'s structure with the databases (6.9.0, every
    installation).** `scripts/sync_schema.py` is now the way to maintain it (§16.3
    has the full rules). It reads each source's catalogue (three
    `INFORMATION_SCHEMA` queries per source; names and types only, no row),
    and never overwrites `schema.yaml`. In order:
    1. `python scripts/sync_schema.py --dry-run` prints the report and writes
       nothing. Read it.
    2. `python scripts/sync_schema.py` writes `project_config/schema.synced.yaml`
       next to `schema.yaml` (your file with every comment and description
       kept, plus the `datasource:` lines with several databases, the columns
       the databases have and the file lacks as `TO BE FILLED` drafts, a
       `column_types:` map, and markers on what the databases lack) and
       `project_config/relationships.proposed.yaml` (`--no-relationships`
       skips it; nothing reads it, so review each entry and copy the right ones
       by hand).
    3. Read the report's `== prompt size ==` section before replacing anything:
       per data source the estimated prefix tokens of the current file and of
       the proposal, the path each takes, and how many added columns and tables
       still carry the draft description. A `WARNING:` line means the proposal
       would move that source from the static prefix to retrieval. It is advice
       only and changes no exit code.
    4. `diff` the two files, copy `schema.yaml` to `schema.yaml.bak`, and move
       the proposal over `schema.yaml`. Resolve what the report marks
       (`# not in database (sync_schema.py)`, `# not found in any data source`:
       correct, delete or `--prune`) and write the descriptions marked
       `# TO BE FILLED`.
    5. `python scripts/sync_schema.py --check` writes nothing; it must print
       `CHECK OK: schema.yaml's structure matches the databases` and exit 0.
       Until the types are recorded (a file from before 6.9.0 has none; the
       replacement in step 4 records them), the line ends with `N column
       type(s) not recorded yet -- run python scripts/sync_schema.py once to
       record them ...`: that is still exit 0, and the note is gone after the
       replacement. Run it again whenever you want to know whether the
       databases moved.

    `scripts/assign_datasources.py` stays for pipelines that already call it;
    it writes only the `datasource:` lines. Run `python scripts/prompt_budget.py`
    again after the replacement if the prompt-size section showed a source near
    or over its budget (§16.6).
15. **Console-encoding workarounds can go (6.9.1).** If a script or scheduled task
    sets `PYTHONIOENCODING=utf-8` or `PYTHONUTF8=1`, or runs `chcp 65001`, only
    so Persian output does not crash, remove it. Every command-line program now
    writes UTF-8 itself; leaving the workaround in place is harmless. Nothing
    else needs doing.
16. **Confirmed answers and the real-use quality card (6.10.0).** Nothing here
    renames a setting or changes a file format, and `requirements.lock` has no
    new pin. Three things to do or decide before the restart:
    - *Alembic-managed application database only.* The new `turn_confirmations`
      table is created at start-up like the other application-database tables,
      so the default setup needs no manual step. A deployment that manages the
      application database with Alembic runs `alembic upgrade head` from the
      repository root first; it applies `0005_turn_confirmations`.
    - *Tell analysts, and review what they confirm.* Under each successful
      answer they now see a «پاسخ درست است» toggle (§18.1.1). Any analyst can
      confirm their own answers; there is no separate permission for it. A
      confirmed answer becomes a reviewed golden case (tag `origin:confirmed`),
      and `python -m eval.cli verify --accept` checks that its SQL runs, not
      that the SQL is right, so a wrong confirmation would end up in the
      accuracy gate of step 30. Check the `origin:confirmed` rows of
      `scripts/golden_sheet.py export` before accepting them, or harvest with
      `--no-confirmations` until you trust the confirmations you see.
    - *Optional: `LLM_STREAM_TIMINGS=true`.* Set it in `.env` if the admin
      panel's quality card «کیفیت در استفاده‌ی واقعی» (§20.6) should show the
      time-to-first-token and generation split of the `llm` stage.
17. **Table selection, the setup doctor and the warm prefix (6.11.0).** No
    setting is renamed, no file format changes and `requirements.lock` has no
    new pin; two optional settings are new, `STAGING_PATTERNS` and
    `LLM_PREFIX_KEEPWARM_SECONDS`. Five things to do or decide before the
    restart:
    - *Fix what the new preflight checks report (step 3).* With more than one
      data source and no `datasource:` line in `schema.yaml`, `Every data source
      is described` fails for the second source: run
      `python scripts/sync_schema.py`, which writes the lines, review
      `schema.synced.yaml`, replace `schema.yaml` and run the preflight again
      (§16.3). A raw `database.schema_inspector_cli` draft copied over the
      curated files fails `project_config/ is curated, not a raw draft`:
      restore the curated files, then add tables with
      `python scripts/sync_schema.py --add-tables 'Schema.*'`. The other two
      new checks only advise.
    - *The prefix text changes once.* `prefix_version` and every cached
      answer's key change once, so answers cached by 6.10.0 are not found and
      the model server's prefix cache is cold for the new text. After the
      restart run `POST /admin/llm/warmup` (or leave
      `LLM_PREFIX_WARMUP_ON_STARTUP` on, which does it). A source whose prefix
      estimate was within about 2% above `PROMPT_RETRIEVAL_TOKEN_BUDGET` may now
      take the static path: run `python scripts/prompt_budget.py` if one sits
      that close (§16.6).
    - *Retrieval can select different tables if `entities.yaml` or
      `retrieval_hints.yaml` names tables by their bare name.* Bare names are
      now resolved to the `schema.yaml` keys, so the tables they name reach the
      prompt. Run `python -m eval.cli recall --golden eval_data/golden.jsonl`
      before and after (§18.4). A bare name that matches tables in several
      schemas is read in the schemas of the file's other names; qualify it
      (`sales.Customer`) when that is not the one you mean.
    - *Recommended on a shared model server: `LLM_PREFIX_KEEPWARM_SECONDS=300`.*
      Left at `0` nothing is sent, as before; a value must be `0` or at least
      `30` (§20.7). Leave it at `0` when this deployment is the only user of the
      model server, when the server does not cache prefixes, or when every
      source is on the retrieval path.
    - *Optional: `--enable-prompt-tokens-details` on the vLLM launch command.*
      It makes `prefix_cache_hit` truthful: the server then reports
      `cached_tokens` and the audit field follows it. Without it the field
      stays the older prompt-size estimate (`prefix_cache_hit_source` says
      which), and §20.8 explains how to read it. A correction round for an
      unknown column or table is a few dozen tokens longer (§20.9); nothing to
      configure.
18. **A response cache in front of the model, and accuracy rounds (6.12.0).**
    No setting is renamed, no file format changes and `requirements.lock` has no
    new pin (`git diff v6.11.0 -- requirements.lock` is empty); one optional
    setting is new, `LLM_CACHE_SUSPECT_MS`. Four things to do or decide before
    the restart:
    - *Run `python scripts/verify_deployment.py` and, if `Model endpoint
      answers each prompt itself` fails, fix the gateway before trusting
      answers or running an accuracy round (step 3).* The check sends two tiny
      requests that differ only in a random token, through the backend the
      server uses. A failure means a response cache in front of the model
      returned another prompt's answer, which can serve the SQL stored for a
      similar question: disable response caching for this client at the
      gateway, or point `OPENAI_BASE_URL` (and `WIZARD_LLM_BASE_URL`) at the
      model server directly, and run the command again until it passes
      (§20.10). It is skipped under `LLM_PROVIDER=mock` and, for a remote
      endpoint, while `LLM_ALLOW_REMOTE` is off; on the admin panel it is a
      deep check (§3.1).
    - *Set `LLM_CACHE_SUSPECT_MS` if the default does not suit (optional).*
      Default `50`, `0` turns it off. When a different question comes back
      with byte-identical SQL faster than that, the server logs one `WARNING`
      per streak and writes `suspected_cached_response: true` in the audit
      `llm` block. It never blocks or changes a request, and a reader of the
      log that ignores unknown keys is unaffected (§20.10).
    - *Re-record the evaluation baselines (if you have a golden set).*
      `python -m eval.cli run --live` now generates through the server's own
      pipeline (guard, correction rounds, row cap), so its accuracy is the
      analyst's and is higher than before. A baseline or share bundle recorded
      on 6.11.0 or earlier is single-shot: `--baseline` and `compare` refuse to
      set it against a production run (`compare --allow-mixed` overrides, with
      a note). Record a new one on 6.12.0 after the restart, in the mode you
      will gate on (`--live --reference live --save-baseline ...`), and add
      `--single-shot` to every command that must stay model-only, which still
      compares with the baseline of step 1. Offline runs (CI) are
      unchanged. The run applies no per-key `denied_columns` policy, and §18.6
      has the commands for rounds with a reviewer (`--share`, `compare`,
      `split`).
    - *Re-run the setup wizard only against an endpoint that passed.* Steps 3
      and 4 now stop, with `Step 3 stopped` and exit status 3, when the model
      gives the identical answer for three tables in a row; `--resume`
      continues once the endpoint is fixed, and `--allow-repeated-answers`
      overrides for a schema so small that identical answers are expected
      (§2.2).
19. **Schema exclusion as configuration, and the setup tools' new defaults
    (6.13.0).** No setting is renamed, no file format changes and
    `requirements.lock` has no new pin (`git diff v6.12.0 -- requirements.lock`
    is empty); one optional setting is new, `EXCLUDE_SCHEMAS`, and one optional
    `datasources.yaml` key, `exclude_schemas:`. Both are read by the setup
    tools and the preflight only, so the answers the server gives do not
    change. Four things to do or decide before the restart:
    - *Run `python scripts/verify_deployment.py` and read the new check `No
      table in an excluded schema` (step 3).* It never fails. A `[PASS]` line
      starting `advice:` gives the count and the first keys of the
      `schema.yaml` tables that sit in a schema every one of their sources
      excludes: remove the table, or narrow the exclusion. A plain `[PASS]`
      means no table does, and `[SKIP]` means nothing is excluded beyond the
      system schemas and no table matches. It reads files only (§3.1).
    - *Decide on `EXCLUDE_SCHEMAS` and `exclude_schemas:` (optional).* Both
      are empty by default, and then only the system schemas are skipped
      (`sys`, `INFORMATION_SCHEMA`, `guest`, `cdc`, the nine fixed `db_*` role
      schemas by exact name, `pg_catalog`, `pg_toast` and `pg_*`, in any
      letter case; a user schema called `db_sales` is still scanned).
      `EXCLUDE_SCHEMAS` in `.env` takes comma-separated `fnmatch` patterns for
      every source, `exclude_schemas:` in `datasources.yaml` a list for one
      source. The inspector, `sync_schema.py` and the wizard skip what they
      match, and a `schema.yaml` table that is already listed is synced as
      before. A table in `cdc` or in a fixed role schema that `sync_schema.py`
      used to report is not reported any more. An `exclude_schemas:` that is
      not a list of non-empty strings makes `datasources.yaml` fail to load
      (§16.2, §16.3).
    - *Fix the scripts and habits that run the wizard (§2.2).*
      `python setup_project.py` writes to `project_config_draft/`
      (git-ignored): review the drafts and copy the files you accept into
      `project_config/`. A script that expected `project_config/` to be filled
      needs a copy step, or `--output project_config --overwrite-live`; without
      the flag the run stops with exit status 2, and with it each file it
      replaces is first copied to `<name>.bak-<YYYYmmdd-HHMMSS>`. A second run
      merges into the first (`--fresh` starts from scratch), and a bare table
      name that several schemas share is re-keyed `Schema.Table`: read the
      `Re-keyed in ...` line. Without `--db-url` the wizard now reads every
      source of `datasources.yaml` through the application's login
      (`--datasource NAME` narrows it). A `.setup_log.json` of an earlier
      version is read as before, and `--resume` generates once again.
    - *Fix the scripts that run the inspector (§2.3).*
      `python -m database.schema_inspector_cli` without `--db-url` reads the
      sources of `datasources.yaml` (or `DB_CONNECTION_URL`) with the
      application's login, so a draft lists only what the application can
      query. It no longer reads `DATABASE_URL`: pass `--db-url` (one source,
      read with the login in the URL, and a line says the application's login
      may see less). Drafts hold no sample value and no staging table any
      more: `--sample-values` writes `sample_values.review.yaml`, and
      `--include-staging` keeps the staging tables.
20. **Intervals and paired comparison in every report, and an optional benchmark
    track (6.14.0).** No setting is renamed, no file format changes and
    `requirements.lock` has no new pin (`git diff v6.13.0 -- requirements.lock`
    is empty); no setting is new. The change is in the accuracy harness
    (`python -m eval.cli`), so the answers the server gives, the audit log and
    the admin panel do not change. Four things to read or decide before the
    restart:
    - *Read the reports with their intervals (§18.3, §18.6.2).* The text report
      now prints a Wilson 95% interval and the number of cases under the overall
      accuracy and at the end of every per-tag and per-source line, and marks a
      stratum of fewer than 20 cases `n<20: not interpretable`; `compare` adds
      the paired statistics (fixed, regressed, an exact McNemar p-value, a 95%
      interval of the difference). All of it is additive: a baseline, `--out`
      report or share file from 6.13.0 or earlier still loads, `--baseline`
      still gates against it and `compare` takes it as either side, so no
      baseline has to be recorded again for this release. A script that reads
      the per-tag or per-source lines of the text report by column must allow
      for the interval at the end; read the JSON instead.
    - *Check your golden set and the scripts that run it (§18.1).* The golden
      file has a new optional status, `excluded`, and a new optional field,
      `match`; a set built from your own questions uses neither, and an existing
      `golden.jsonl` needs no edit. `run` now prints a `Not scored: N of M
      case(s) -- ...` line under the report whenever the file holds a case that
      is not `active` (a pending one included, which was skipped silently
      before), and a golden file the tools rewrite carries
      `"exclusion_reason": null` and `"match": "fingerprint"` on every line,
      which 6.13.0 also reads.
    - *Plan for the extra queries of the determinism probe (§18.3).*
      `run --live --determinism` now also executes and judges each repeat,
      through the guard and the read-only executor like `run`, so one probe
      sends up to cases times repeats (3 by default) extra queries to the data
      sources. Run it on a large golden set when the warehouse is quiet.
    - *Decide on the benchmark track (optional, §18.7).* Nothing runs it unless
      you call `bench-import` or `bench-run`. If you use it: download BIRD
      mini-dev yourself; the load needs its own administrator login
      (`--target-url`, or `env:NAME` to keep the password off the command line)
      on a database of its own, while the application keeps its read-only
      login and must never get the right to create anything; the database must
      be case- and accent-sensitive (`COLLATE Latin1_General_100_CS_AS_SC_UTF8`),
      and the import refuses any other before it writes anything, unless
      `--allow-ci-collation` is given, in which case every report says so; and
      its figure is not the deployment's accuracy, as the first line of its
      report states. It does not replace the golden set.
21. **Say which browser origin is refused, and when the API address is wrong
    (6.14.1).** No setting is renamed or added, no file format changes and
    `requirements.lock` has no new pin (`git diff v6.14.0 -- requirements.lock`
    is empty). The server change is one log line, written by a layer around
    the CORS middleware that answers nothing and changes no request or
    response; the CORS policy is as in 6.14.0. Four things to read or do before
    the restart:
    - *Know the new log line (§5, §5.1).* When a request arrives from an origin
      that `CORS_ALLOWED_ORIGINS` does not hold, the server logs one `WARNING`
      for that origin (once per origin while the process lives, for a preflight
      and an ordinary request alike): `CORS: origin '...' is not allowed
      (allowed: ...)`, with the `.env` line that allows it. It means a browser
      was opened at an address the list does not hold, not that the server is
      unwell. At most 64 origins are remembered.
    - *List every address the page is opened with (§2, §4).*
      `CORS_ALLOWED_ORIGINS` replaces the default list, and the IP address, the
      host name and `localhost` are three different origins, so write each one
      people use, comma-separated. The API's own address needs no entry: a page
      served by the API itself is same-origin. Do this in `.env` before the
      restart.
    - *Run the new preflight check (§3, §3.1).* `Web UI origin is allowed by
      CORS` is one of the twenty-four checks the preflight has with one data
      source (it made them twenty-two in 6.14.1; step 22 adds two). It reads
      settings only. Set `VERIFY_UI_ORIGIN` to the address as the browser's
      address bar shows it (`http://172.16.101.42:8077`) and it passes or fails
      on that address, printing the `.env` line to add; run it once per
      address. Without it the check never fails and only advises. On the admin
      panel `VERIFY_UI_ORIGIN` has to be set in the server's own environment.
    - *If an admin panel card says «به API نرسید؛ پیام بالای صفحه را ببینید.»
      (§5.1).* The address saved in the browser is the static file server's,
      not the API's, and a banner at the top now says so (every card used to
      show its own 404). Enter `http://<host>:8000` in the address bar at the
      top of the admin panel and press «اتصال», or load the page once with
      `?base=http://<host>:8000`. The analyst page shows the same banner and
      names `web/js/config.js` (`DEFAULT_BASE_URL`).
22. **One entity per table, no row values in aliases, and drafts caught by the
    preflight (6.14.2).** No setting is renamed or added, no file format
    changes and `requirements.lock` has no new pin
    (`git diff v6.14.1 -- requirements.lock` is empty). One template changed:
    `project_config.example/entities.yaml` now names real keys of the
    template's own `schema.yaml`. The server's answers, the audit log and the
    CORS policy are as in 6.14.1; the changes are in the setup wizard, the
    schema inspector and the preflight. Four things to read or do before the
    restart:
    - *If you ran the wizard of 6.13 or 6.14, check what it wrote (§2.2, §3,
      §3.1).* It produced drafts that passed the preflight and answered almost
      every question about one source with `OUT_OF_SCOPE`: two entities for
      nearly every table, `aliases.yaml` full of raw values from the rows, an
      empty `examples.yaml`. Run `python scripts/verify_deployment.py` on the
      folder you deployed (or on the drafts, with `PROJECT_CONFIG_DIR` pointing
      at them). The preflight now has twenty-four checks with one data source
      (twenty-two before): `Entities match schema keys` lists the entities
      that name no table of `schema.yaml` and the tables that have two, and
      `Aliases hold no raw data values` counts the synonyms copied from rows.
      Fix `entities.yaml` and `aliases.yaml`: one entity per table, under the
      key `schema.yaml` uses, with the aliases of both united and the same
      keys in `ring_aliases`; or restore the curated files from a backup or
      version control and add new tables with
      `python scripts/sync_schema.py --add-tables 'Schema.*'`. Running the new
      wizard again over the same output folder does the merge for you and
      prints what it merged.
    - *Remove the value synonyms and read `aliases.yaml` for names (§2.2).* The
      merge of the new wizard drops every `synonyms` key of an earlier
      `aliases.yaml` that is a date, a time, a phone number, an IP address, a
      masked value, a long digit string, an address, a hash or an error
      message, and counts them per shape in the summary and in
      `.setup_log.json`, never listing a key (`--keep-existing-synonyms` keeps
      them). A key that is a person's name has no shape the check can
      recognise: read the file yourself and delete such keys. The values were
      in the prompt, and in git if the file was committed; treat a committed
      copy as having published them. Copy the cleaned `aliases.yaml` into
      `project_config/` yourself; the wizard never writes there on its own.
    - *A preflight that passed on 6.14.1 can fail on this version (§3.1).*
      `project_config/ is curated, not a raw draft` now sees a draft without
      its header line: it fails when most table descriptions are only the key,
      the bare name or `Schema.Table`, or most column descriptions are only
      `<type> column`, with the counts, and the wizard's own header,
      `# AUTO-GENERATED by setup_project.py`, counts as a draft header like the
      inspector's. It is the same check telling you a file is a draft: restore
      the curated files and add tables with `sync_schema.py --add-tables`, and
      describe the ones you add. An `entities.yaml` copied from the older
      template and never filled in fails `Entities match schema keys` too,
      because `Entity1` is not a table of `schema.yaml`.
    - *Scripts that run the wizard (§2.2).* It builds no synonym from a row
      value unless `--value-synonyms` is given; it makes one model call per
      data source for the examples, and says so, in the summary and in the
      file, when the model gave none; and it drafts a value or volume rule only
      for a fact table that has a numeric column that is not a key. It still
      writes to `project_config_draft/` unless you say otherwise.
23. **Aliases kept from a looping answer, no rules for dimensions, and
    descriptions drafted for review (6.15.0).** No setting is renamed or added,
    no file format or template changes, and `requirements.lock` moves one pin,
    `certifi` 2026.10.19, for a published advisory (`git diff v6.14.2 --
    requirements.lock` shows that line only; step 2 installs it). The preflight still has
    twenty-four checks with one data source; one of its messages changed. The
    server's answers, the audit log and the CORS policy are as in 6.14.2; the
    changes are in the setup wizard, a new setup command and the preflight's
    advice. Three things to read or do before the restart:
    - *Tables the wizard left with no alias (§2.2).* A model that repeated the
      same phrases until `max_tokens` cut it off left JSON that never closed,
      and the table got 0 aliases (`Could not extract valid JSON after
      retry`). The alias step now keeps the complete phrases written before
      the cut, at most eight, and asks once more only when nothing is usable.
      Run the wizard again over the same output folder: the merge keeps what
      you reviewed, and the tables that had none get their aliases; the step
      line says `salvaged N from a truncated answer`. The alias call sends its
      own `max_tokens` of 320 unless `LLM_NUM_PREDICT` is above 512 or
      `LLM_EXTRA_BODY` is set (the two ways to make room for a reasoning
      model). Review the result and copy `entities.yaml` into `project_config/`
      yourself, as before.
    - *Dimension tables get no value rule (§2.2).* A table classed `dim`, one in
      a schema named like a dimension (`..._Dim`, `lookup`), or a lookup in
      shape (one key and one or two text columns) is no longer sent to the
      model for a value or volume rule. The summary lists those tables apart
      from the fact tables that really have no measure. If an earlier
      `business_rules.yaml` holds rules the old wizard drafted for such tables,
      delete them.
    - *A `schema.yaml` whose descriptions are only names (§2.4, §3.1).* If
      `project_config/ is curated, not a raw draft` fails because most table
      descriptions are only the table's name, or most column descriptions are
      only `<type> column`, and there is no curated file to restore, run
      `python scripts/draft_descriptions.py` (`--dry-run` first lists what it
      would ask; `--tables 'Schema.*'` limits a first try). It drafts only those
      descriptions with the wizard's model, from names, types, keys and
      relationships (no row value unless `--samples`), and writes
      `schema.described.yaml` beside `schema.yaml`. Read every line marked
      `# REVIEW`, replace `schema.yaml` with it (keep `schema.yaml.bak`) and
      delete its first line: until that line is gone the check fails on
      purpose. A remote endpoint is refused unless `LLM_ALLOW_REMOTE=true`, as
      for the wizard.
24. **Column restrictions that cannot silently fail open, and an audit that
    names the caller (6.15.1).** No setting is renamed or added, no file
    format changes and `requirements.lock` has no new pin
    (`git diff v6.15.0 -- requirements.lock` is empty). Every `api_keys` file
    and every panel key that works today still works. The preflight now has
    twenty-five checks with one data source. Three things to do before and
    after the restart:
    - *Run the preflight and read `Column restrictions name real columns`
      (§3, §3.1).* It fails, naming the key id and showing the entry with
      `ascii()`, for a plain `denied_columns` name that matches no column of
      `schema.yaml` (a typo, or a name from another database): correct or
      remove each one. Its PASS detail may start with `advice:` and list
      columns that probably hold the same data under another name
      (`CustomerNationalID` or `NationalCode` beside a denied `NationalID`);
      add the ones that must stay hidden.
    - *Read the log once after the restart.* An entry with a space or an
      invisible character (a non-joiner, a direction mark) at either end, easy
      to paste from a Persian editor, used to deny nothing; it now denies the
      column it names, and one WARNING per key names the key id and shows the
      entry with `ascii()`. Clean the file so the warning stops. A question
      such a key could ask before may now be refused: that is the restriction
      the entry always asked for. Characters inside a name are kept as written,
      so a Persian column name that contains a non-joiner stays denied.
    - *Check which key the web UI uses.* The audit log now fills
      `principal_id` for every question asked in the web UI (earlier records
      keep it empty). A key issued with `--full-admin` and no `--denied-column`
      has no restriction at all: give analysts their own keys.
25. **Money only gets Rial, identifier columns are not charted, and single
    tables can be excluded (6.15.2).** No setting is renamed, no file format
    changes and `requirements.lock` has no new pin (`git diff v6.15.1 --
    requirements.lock` is empty). Three new optional keys, all ignored when
    absent: `column_units:` per table in `schema.yaml`, and `exclude_tables:`
    and `keep_staging_schemas:` per source in `datasources.yaml` (plus the
    setting `EXCLUDE_TABLES`). The SQL the server writes, the guard and the
    audit log are as in 6.15.1. What to look at:
    - The answer summary writes Rial/ریال only for a column that holds money.
      It decides from the column's name (price, value, amount, fee, ارزش,
      قیمت, مبلغ, کارمزد are money; count, quantity, volume, weight, id, code,
      date are not); where a name misleads it, write `column_units: {Quantity:
      none, TotalPrice: rial}` under that table in `schema.yaml`.
    - A two-column answer whose number is an identifier (`ID`, `Ring_ID`,
      `RingID`, `Code`, `کد`, `شناسه`) is shown as a table, no longer as a
      chart. Other shapes are unchanged.
    - To skip single tables when the setup tools discover tables, list them
      per source as `exclude_tables: ["Sales_Fact.Order"]` (fnmatch on
      `schema.table`). To scan a staging schema without its change-capture
      tables, list it under `keep_staging_schemas:`; the `table:` patterns of
      `STAGING_PATTERNS` (`*_Inserts`, `*_Updates`, `*_Deletes`) still drop
      those tables. Singular names (`*_Insert`) are not in the default
      patterns: add them to `exclude_tables:`. Nothing already in
      `schema.yaml` is hidden.
26. **A filter value that does not exist can get one correction round
    (6.16.0).** No setting is renamed, no file format changes and
    `requirements.lock` has no new pin (`git diff v6.15.2 --
    requirements.lock` is empty). One new optional setting,
    `FILTER_VALUE_CHECK`, off by default: until you turn it on, every request,
    prompt, SQL statement, audit line and preflight list is as in 6.15.2.
    Decide whether to turn it on:
    - What it does. With `FILTER_VALUE_CHECK=true`, a statement that compares
      a dimension column with a string that is not one of that column's cached
      values (for example a hall name the model invented) gets one extra
      correction round: the model is told the value does not exist, shown the
      nearest real values, and told to drop the filter if the question names
      no value of that column. If the model keeps the value, or anything else
      goes wrong, the original statement runs as before, so the check never
      refuses an answer.
    - What it needs. The values come from the dimension vocabulary cache, so
      only columns listed under `prefetchable_columns` in `schema.yaml` are
      checked (typically the name column of each small dimension). With the
      setting on, the preflight check `Filter value check has vocabularies`
      lists dimension tables whose label columns are not listed.
    - How to decide. Turn it on after you have a golden set (Part 5): record a
      baseline with it off, run again with it on, and keep it on only if the
      comparison is better. The audit record gets a `value_check` block when
      the check found something to compare; it holds the column and a hash of
      the value, never the value itself.
27. **Real values for the analysts' memory and the default scope (6.16.1).**
    No setting is renamed or added, no file format changes and
    `requirements.lock` has no new pin (`git diff v6.16.0 --
    requirements.lock` is empty). Memory and the default scope behave at run
    time exactly as in 6.16.0. The preflight now has twenty-six checks with
    one data source, and the new one can fail on a configuration that runs:
    - Run the preflight and read `Session and memory policies use real values`
      (section 0.4). It fails when `memory_policy.yaml` or
      `session_policy.yaml` still holds the template's sample options (`Ring
      A`, `Ring B`, `All rings`), or names a table or column that
      `schema.yaml` does not have. With the template's options an analyst
      cannot pin any real value, and the clarification offers made-up ones.
    - Draft the real values with `python scripts/draft_policies.py` (section
      0.3.4.1): `--dry-run` first, then without it. It reads the scope
      column's distinct values with one bounded, read-only query through the
      application's login and writes both files to `project_config_draft/`,
      changing only `options`. Read them and copy them into `project_config/`.
      Leave `--table` alone unless you mean to change the scope dimension: the
      key the scope is stored under is the one the question matching uses.
28. **Restart** (every change above, including `datasources.yaml` and
    `schema.yaml`, takes effect at a restart). Then run the preflight once more
    with `VERIFY_API_KEY` set to an analyst's raw key (§3), and read the
    start-up log (§5): the provenance banner, `CORS allowed origins`, and with
    several sources one `Prompt path for data source` line each. Since 6.9.0
    each static-prefix source also logs an `LLM prefix warm-up` line a little
    later (§20.2).
29. **After the first day**, read the new audit fields (6.5.0, 6.9.0): a reader of
    `audit_log.jsonl` that does not know `datasource_selection` (6.5.0), or the
    `llm` block's `ttft_ms`, `generation_ms`, `reasoning_tokens` and
    `reasoning_tokens_estimated` (6.9.0; the first two are filled only with
    `LLM_STREAM_TIMINGS=true`), can ignore them, and older records stay
    readable. `python scripts/analyze_audit_log.py` reports the new "LLM stage
    split" when the records have it. And note what changed on screen, because
    analysts will ask: the SQL shown in a conversation is laid out in a fixed
    style (6.5.0; display only, the statement that ran is unchanged), and a refused
    statement can be opened with the same layout.
30. **Start measuring accuracy** (6.7.0, optional but recommended). 6.7.0
    adds the tools that turn real usage into an evaluation set and a release
    gate: `python scripts/harvest_golden.py` (candidates from the audit log),
    `python scripts/golden_sheet.py export` and `import` (the analysts' review
    in Excel), `python -m eval.cli verify --accept` (run and activate the
    cases) and `python -m eval.cli run --live --reference live` (execution
    accuracy against the reference SQL, run in the same session on the same
    data). They do not exist before 6.7.0, so an installation older than that
    records its first baseline on 6.7.0 or later, once the audit log holds
    real questions; from then on you record a baseline on the running version
    *before* each upgrade and compare after it (§18, which has every command).
    A baseline file written by an older version still loads, but cannot be
    compared with a `--reference live` run: the tool refuses and says how to
    record it again. 6.9.0 adds two optional things to the gate: absolute
    accuracy floors, `EVAL_MIN_ACCURACY` and `EVAL_MIN_SOURCE_ACCURACY`, both
    off unless set (§18.2), and `python -m eval.cli recall`, which measures table
    selection with no model and no database (§18.4). Nothing else in 6.7.0 changes
    how the server runs.
31. **Contributors only: install `requirements-dev.txt` (6.9.0).** It pins `ruff`
    to an exact version, and CI now has a `lint` job that fails on
    `ruff check .`. Run `pip install -r requirements.txt -r requirements-dev.txt`
    and `ruff check .` before pushing. A deployment that does not change this
    code does not need the file.

Nothing in 6.0.1 (`DB_POOL_PING_IDLE_SECONDS` defaults to `60`; `0` keeps
pinging on every checkout), 6.3.2 (one warning per key instead of one per key
read), 6.6.1 (the template of step 6) or the CI change of 6.7.0 (the
`requirements.lock` you install is now also what CI tests) needs an action of
its own.

## 18. Measuring accuracy on the real warehouse, and gating an upgrade on it

Until a deployment has a golden set built from its own questions, it has no
measured accuracy: the committed `eval_data.example/` is made-up data and
says nothing about your warehouse. This section builds the real set from the
audit log and turns it into the check you run before every upgrade. A public
benchmark can be run beside it for development decisions (§18.7); it is a second
instrument and never replaces this set.

**The set lives only on the server.** `eval_data/` is git-ignored and holds
real questions, the SQL that answers them and, after step 4, real result
rows. Create it, read it and run the gate on the server; do not copy
`candidates.jsonl`, `review.csv`, `golden.jsonl`, their `.bak` files or a
JSON report (`--out`, `--save-baseline`: they carry each case's question and
SQL) off it; the one file built to leave is the share file of §18.6. What
the commands below **print** is counts and case ids only, the same stance as
`scripts/analyze_audit_log.py`, and is safe to paste into a chat;
`--include-examples` on the harvest is the one opt-in that prints a few
verbatim questions.

### 18.1 Build the set (once, then top it up)

```bash
# 1. Harvest ~150 candidates from the audit log (stratified by data source,
#    outcome and language; de-duplicated with the Persian/Arabic folding).
python scripts/harvest_golden.py --n 150
#    -> eval_data/candidates.jsonl, status "pending_review". The model's own
#       SQL is only a proposal. Refuses to overwrite without --force.
#       Answers analysts have already confirmed (§18.1.1) come first, as
#       "reviewed" cases, and do not count against --n.

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

### 18.1.1 The easiest way to grow the set: analysts confirm answers as they work

The spreadsheet in §18.1 costs the analysts a separate review session, and
that is why a golden set tends not to get built. The web UI now lets them
build it without one. Every answer whose query ran and returned a result
carries a toggle, **«پاسخ درست است»** ("this answer is correct"), in a footer
right under the result table or chart: visible on the answer, with nothing to
open first. (The wrong-answer flag «این عدد درست نیست؟» stays in the
collapsed «جزئیات» drawer.) The toggle is absent on an answer that was
rejected, declined as out of scope or failed. One click stores a
confirmation; pressing it again withdraws it. A confirmation holds
identifiers only (session, turn, the confirming principal, the request id
and the time), never the question, the SQL or any result data, and it
cannot be given to a turn that was rejected, declined or failed (the server
answers `409`). Confirming an answer you had flagged removes the flag, and
flagging one you had confirmed removes the confirmation: the later action
wins.

`harvest_golden.py` reads the confirmations by default, so the flow into the
golden set is the one you already have, with the confirmed answers already
reviewed:

```bash
python scripts/harvest_golden.py --n 150
#    -> confirmed answers first (status "reviewed"), then the sample of
#       "pending_review" candidates. Prints counts, including
#       "confirmations: read N, included M (...)".
python scripts/golden_sheet.py export
#    -> confirmed rows already carry verdict "correct" and a note saying who
#       confirmed them and when; an analyst only changes one they disagree
#       with (to "wrong" or "skip") and reviews the rest as before.
python scripts/golden_sheet.py import
python -m eval.cli verify --golden eval_data/golden.jsonl            # look first
python -m eval.cli verify --golden eval_data/golden.jsonl --accept   # then activate
```

What to know:

- **Status.** A confirmed answer becomes a case with `status = "reviewed"`:
  a person confirmed its `expected_sql` (the SQL that ran), and
  `eval.cli verify` has not run it yet. It is not `pending_review` (that
  would ask a second person to re-judge what the first already judged) and
  not `active` (that would skip the gate). `verify --accept` is still what
  runs the SQL against the database, records the rows and activates the case;
  until then the regression gate skips it.
- **Provenance.** The case's `notes` name the confirming analyst's principal
  id, the confirmation time and the audit record it came from, and it is
  tagged `origin:confirmed`. These are in the file and in `review.csv` (both
  stay on the server, §18); the console output stays counts only.
- **Never dropped by `--n`.** Confirmed cases are added before the stratified
  sample and are not counted against it: `--n 150` gives every confirmed case
  plus up to 150 sampled ones, and a question is never in the file twice.
- **De-duplicated.** Two confirmations of the same question (after the
  Persian/Arabic folding of §18.1) are one case, the most recent
  confirmation's. A question already in the set (`--exclude`) is skipped.
- **`--no-confirmations`** harvests only the sample, as before, and does not
  touch the application database.
- **The audit log must still hold the turn.** The question and SQL come from
  the audit record; a confirmation whose record has rotated away is counted
  (`no audit record`) and skipped, so harvest more often than the log
  rotates.
- **A click is a judgement, not a proof.** An analyst who confirms an answer
  that merely looked right puts it into the set. `verify` still catches SQL
  that no longer runs or returns nothing where something is expected, and the
  sheet is the place to catch the rest.
- **Setup.** The `turn_confirmations` table is created with the other
  application-database tables the first time the server starts; a deployment
  that manages the schema with Alembic runs `alembic upgrade head` (revision
  `0005_turn_confirmations`). `scripts/migrate_app_db.py` carries it between
  backends with the others.

The confirmations also feed the admin panel's real-use quality card (§20.6).
There they are a **lower-bound signal, not accuracy**: analysts confirm only
some answers, and an unconfirmed answer is not a wrong one. The golden set
and `eval.cli run` (§18.2, §18.3) are what measure accuracy.

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
A baseline also records which pipeline generated the SQL (`pipeline`, §18.6):
`--live` runs the server's loop, `--live --single-shot` one model call. A
baseline written before this field was single-shot, so a production run
compared with it is refused like the cases above; re-record it.

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
- **How precise a percentage is.** Under the overall accuracy, and on every
  per-tag and per-source line, the report prints the 95% Wilson interval of the
  proportion and the number of cases behind it (`eval/stats.py`): 33 of 40 is
  82.5%, and the data are compatible with anything from 68% to 91%. A stratum
  (a tag such as a language, a category, `db:*` or `difficulty:*`, or a data
  source) with **fewer than 20 cases is marked `n<20: not interpretable`**: its
  interval spans tens of points, so a difference between two such strata is
  noise. The same intervals are in the `--out` report under `intervals` (a field
  added to the report; older reports load as before).
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
- **Repeats.** `run --live --determinism` generates every question several times
  (`--determinism-repeats`) and reports how often the SQL text came back
  identical. It also executes each repeat and judges it with the same rule as the
  run, and adds the **outcome consistency** (the share of cases that were right on
  every repeat or wrong on every repeat) and the **mean accuracy over the
  repeats**. A case that is right on some repeats and wrong on others is what
  inconsistency means; a single run's percentage hides it. The probe uses the
  single-call generator, so read it as a model-only figure.
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

### 18.4 Table selection on its own: `eval.cli recall`

Before the model sees a question, retrieval decides which tables go into the
prompt (once the schema is too large for the cacheable static prefix, see
`PROMPT_RETRIEVAL_TOKEN_BUDGET`). A table that is not there cannot appear in
the SQL, so this stage can be measured without a model and without the
database, from the same `golden.jsonl`:

```bash
python -m eval.cli recall --golden eval_data/golden.jsonl
python -m eval.cli recall --golden eval_data/golden.jsonl --json --out recall.json
python -m eval.cli recall --golden eval_data/golden.jsonl --min-recall 0.9   # exit 1 below
```

For each case the tables its `expected_sql` reads (resolved with the SQL
guard's own table resolution, so a schema qualifier or a same-name table in
two schemas matches) are compared with the tables `ContextRetriever` selects.
The report gives mean **recall**, the share of cases with **every** table
retrieved, the mean/median/max number of tables retrieved and mean
**precision** (the cost side: recall is easy to buy by retrieving more), an
**in-budget recall** in which a retrieval too large for the token budget counts
as 0, per-tag figures and, for cases with a `datasource`, source-selection
accuracy. A case whose `expected_sql` names a table the loaded `schema.yaml`
does not know is listed as skipped, not counted as a miss. The text output
prints case ids and table names only (no question text); `--json` and `--out`
add each case's question, so keep that file on the server like the others in
this section. Use it to see whether a drop in accuracy is retrieval or
generation, and after any change to `schema.yaml`, `entities.yaml`,
`retrieval_hints.yaml` or the `RETRIEVAL_*` settings. How the stage works, the
settings and the measured numbers are in `docs/design/RETRIEVAL.md`.

If the recall is far lower than the synthetic benchmark's, look first at how
`entities.yaml`, `fact_tables`, `fact_patterns` and `always_include` spell the
tables: since this release a bare name (`Customer`) is resolved to the
`schema.yaml` key (`sales.Customer`), and a name that matches no key is kept as
written and finds nothing. When a bare name matches tables in several schemas,
the schemas the file's other names live in decide, then the data source whose
`keywords:` the question contains, and otherwise every candidate is retrieved;
qualify the name to pin it (`docs/design/RETRIEVAL.md` §8).

### 18.5 The `RETRIEVAL_*` settings

Eleven settings (6.9.0) decide which tables the retrieval path puts in the
prompt. They matter only for a data source whose schema is over
`PROMPT_RETRIEVAL_TOKEN_BUDGET` (§16.6); a source on the static prefix gets the
whole schema and ignores them. Leave them at the defaults unless
`eval.cli recall` (§18.4) shows a reason; each change takes effect at a restart.

| Setting | Default | What it does |
|---|---|---|
| `RETRIEVAL_EXTRA_TABLES` | `3` | Ranked tables added beside the ones an alias or fact pattern named. `0` restores the old behaviour, where an alias hit ended the search for that kind of table. |
| `RETRIEVAL_EXTRA_SCORE_RATIO` | `0.5` | An extra table must score at least this fraction of the best of its kind. Higher: fewer extras, higher precision. |
| `RETRIEVAL_JOIN_EXPANSION` | `true` | Add the tables a question needs to join but did not name (a bridge table, the parent of a classification), found on the foreign-key graph. |
| `RETRIEVAL_JOIN_MAX_HOPS` | `2` | Longest join, in foreign keys, that is bridged (`2` allows one intermediate table). |
| `RETRIEVAL_JOIN_MAX_ADDED_TABLES` | `4` | Most tables join expansion adds to one question. |
| `RETRIEVAL_JOIN_MAX_HUB_DEGREE` | `10` | A table referenced by more tables than this (a calendar shared by every fact) is never a stepping stone between two others. |
| `RETRIEVAL_INFER_RELATIONSHIPS` | `true` | For join expansion only, take a column named `<Table>_ID` or `<Table>ID` as a key to that table's `ID` when neither `schema.yaml` nor `relationships.yaml` declares it. Nothing is written to configuration. |
| `RETRIEVAL_PRUNE` | `true` | Drop a candidate that only its description matched, or a column match far below the best, unless it is joined to a better-evidenced table. |
| `RETRIEVAL_PRUNE_SCORE_RATIO` | `0.85` | A table with column-name evidence must score this fraction of the best to stand on its own. `0` keeps every such table. |
| `RETRIEVAL_PRUNE_CONNECT_HOPS` | `2` | How many foreign keys may separate a weak candidate from a better one for it to survive. `0` disables the rescue. |
| `RETRIEVAL_PRUNE_CORROBORATE` | `true` | Two weak candidates joined to each other keep each other. |

The previous selection (before 6.9.0) is `RETRIEVAL_PRUNE=false`,
`RETRIEVAL_JOIN_EXPANSION=false` and `RETRIEVAL_EXTRA_TABLES=0`. The settings
are read from `.env` like the others; the same table, with the measured effect
of each part, is in `docs/design/RETRIEVAL.md` §6.

### 18.6 Accuracy rounds with a reviewer

The model and the warehouse exist only on your server, so whoever reviews the
accuracy cannot run anything. A **round** is how the two of you work around
that: you run the evaluation here and send **one** share-safe file; the
reviewer diagnoses the failures from it and ships fixes that are generic (they
never mention your data); you re-run and compare the two files. A fingerprint
only says *that* an answer differed; the share file says *how* (too few rows,
the right rows in the wrong order, a number two percent off) without a single
result value leaving the server.

You need the golden set of §18.1 (the more active cases, the less a single
case swings the numbers; §18.6.3 says how many). Do not copy anything out of
`eval_data/` except the file in step 4.

**1. Split the set once: a dev half the reviewer studies and a test half
nobody tunes to.**

```bash
python -m eval.cli split --golden eval_data/golden.jsonl --out eval_data/split
```

Within each category (the case's first tag), cases are sorted by `id`; even
positions go to **dev**, odd positions to **test**. Nothing is random and the
order of the file does not matter. The command prints the counts and writes
`dev_ids.txt`, `test_ids.txt` and `split.json` (the counts per category, the
golden file's name and hash). Run it **once**, before round 1, and keep the
files: the test half is only worth something if it does not move. Running it
again on the same file changes nothing; on a changed file it refuses until you
pass `--force`, which starts a new held-out set on purpose. A case
added later is in neither file and is left out of a `--split` comparison:
append its id to one of the two files by hand (to `test` only if the reviewer
has not seen it), because `split --force` deals every case again. It places every case whatever its `status`, so the next step does
not shift anyone. Give each category at least two cases, or it has a dev case
and no test case.

**2. Activate the cases whose reference SQL runs.**

```bash
python -m eval.cli verify --golden eval_data/golden.jsonl            # look first
python -m eval.cli verify --golden eval_data/golden.jsonl --accept   # then activate
```

`--accept` moves a `reviewed` case to `active` only when its reference SQL
passed the guard, ran, and returned a result as `expect` says (§18.1); only
`active` cases are run.

**3. Run the round.** `N` is the round number: 1, 2, 3, ...

```bash
python -m eval.cli run --live --reference live --golden eval_data/golden.jsonl \
    --share eval_data/share/round-N.json --share-only-ids eval_data/split/dev_ids.txt \
    --out eval_data/round-N.report.json
```

**`--live` measures what an analyst gets.** It generates through the server's
own loop (`SQLAgent`): the SQL guard, correctable rejections, up to the
configured correction rounds with the hints that name the columns or tables
that exist, source routing, the row cap the server injects into a statement
that has none (`DEFAULT_TOP_N`) and the executor's query timeout and
`MAX_ROWS_RETURNED`. The reference SQL is run as written, so a question whose
reference returns more rows than the cap is scored the way the analyst would
see it: a result of exactly cap rows, every one of them a row of the reference,
**passes** as `match_capped` once a count confirms it (the analyst saw the
right rows, cut short by policy), while any other subset stays
`actual_is_subset` and fails. **One difference, on purpose:** the run has no caller, so
no per-key `denied_columns` policy is applied; a deployment whose keys deny
columns is measured as if they did not. `--single-shot` makes one model call
and no correction (the old behaviour): a model-only number, lower than the
analyst's, useful to see what the correction loop is worth. The mode is printed
in the report, written to `pipeline` in the `--out` report and to
`run.pipeline` (`production` or `single_shot`) in the share file; **a baseline
(§18.2) or a `compare` across the two modes is refused**, because the
difference would be the pipeline and not a fix. Choose one for all the rounds.
Offline `eval.cli run` (what CI does) is unchanged, and no CI job runs `--live`.

**`--share-only-ids` keeps the held-out half's content on the server.** With
`eval_data/split/dev_ids.txt` only the dev cases carry the question, the SQL,
the error text and the diagnosis; every other case carries only `case_id`,
`tags`, `passed` and `mismatch_class`, and the `summary` still counts all of
them. The reviewer cannot tune to what the file does not hold, and `compare
--split test` still works on it. Use it every round; without it the file holds
both halves in full.

`--share` is refused with one line and exit status 2 unless the run is
`--live --reference live` (the diagnosis compares the generated result with
the same-run reference; offline replay and a stored fingerprint have none),
and before anything is loaded or sent to the model. The path must be outside
the repository checkout or under its git-ignored `eval_data/`; a path anywhere
else in the tree (`docs/`, `eval_data.example/`, the root) is refused, so the
file cannot end up in a commit. The file is written owner-only, as UTF-8 with
sorted keys and cases sorted by id, so two rounds `diff` cleanly. The normal
report (`--out`) is written as well, and `--baseline`, `--min-accuracy` and
the other options of §18.2 work as before.

The bundle keeps both SQL texts **verbatim**, filters and literal values
included, because telling a wrong filter from a right one needs the filter. If
the SQL itself must not leave, add `--share-redact-literals`: every string and
numeric literal in both SQL texts becomes a placeholder (`:str`, `:num`),
found by parsing the SQL with sqlglot, never by pattern. The reviewer then
sees the shape of the query and not what it filters on, which diagnoses fewer
failures; the header says which mode was used.

**4. Look at it, then send that one file.** Open `eval_data/share/round-N.json`
in an editor. The `privacy` block at the top says what is inside, and `detail`
how many cases carry it; the cases follow. Send **only** `eval_data/share/round-N.json`.

Never send: `eval_data/round-N.report.json` (`--out`: it has each case's
unredacted error text, fingerprints and the model's SQL), `golden.jsonl`,
`candidates.jsonl`, `review.csv`, a baseline, any `.bak`, anything else from
`eval_data/`, `.env`, `project_config/`, `logs/` (§19 has the full list). Before you send, read the
`question` fields: they are what your users typed and are kept verbatim. If a
question names a person or a company, take that case out of the golden set
and run the round again.

**Privacy: what the share file contains, exactly.**

- *In it:* a header (format version, application version, `prefix_version`,
  model name, the golden file's name and hash, `PROMPT_RETRIEVAL_TOKEN_BUDGET`,
  each data source's prompt path (static prefix or retrieval), start and end
  time, the `privacy` block); for each case its id, tags, **question**,
  reference SQL, generated SQL, status, expected and selected data source, the
  SQL guard's rejection reasons and run failures **per correction round**
  (round, class and redacted message, so a repeated rejection shows), the number
  of self-correction rounds, the latency, the error text, and a **diagnosis**
  computed on the server from the two result frames: a class
  (`match`, `match_capped`, `row_count_differs`, `column_count_differs`,
  `column_names_differ`, `same_rows_different_order`,
  `values_differ_same_shape`, `actual_is_subset`, `actual_is_superset`,
  `actual_empty`, `expected_empty` or `not_executed`), `capped`, the cap
  value, `uncapped_rows` and `reference_uncapped_rows` (counts) when the cap
  applied, `capped_unverified`,
  the row counts, the **column names** of both results and, for a single
  numeric cell, a bucket of the relative error (`<1%`, `<10%`, `>=10%`); and a
  summary (execution accuracy overall, per tag and per data source, the
  distribution of the classes, correction rounds, latency p50 and p95).
  With `--share-only-ids`, a case outside the list has only its id, tags,
  `passed` and `mismatch_class`.
- *Changed on the way out:* error text and the per-round messages have every
  quoted string replaced by `'…'` and every run of four or more digits
  (Persian digits too) by `#`, and are cut at 600 characters; with `--share-redact-literals` so are the
  literals in the two SQL texts.
- *Never in it:* result rows or any cell value from either query, credentials,
  `.env` values, the endpoint URL or host, API keys, principal ids, a case's
  `notes` or recorded rows. The model is named, not located.
- *Two things stay readable, and you should know it:* the questions, and the
  result column names. A column name is schema, but a `PIVOT` query turns data
  values into column names, and an alias can carry whatever the model wrote.
  The diagnosis cannot tell, so read the column names too if your SQL pivots.
- The golden file is identified by its base name and a SHA-256 of its text
  (line endings normalised), never by its directory.

**The reviewer's rule.** Look at the failures of the **dev** ids, tune to
those, and use the **test** half only as a number (the next sections). With
`--share-only-ids` the file cannot show the test half's content; without it the
discipline is the reviewer's alone. A fix that raises dev and lowers test is
overfit, and `compare` shows it.

#### 18.6.1 How the fixes come back

- **Code** comes in a release. Upgrade as §17 describes; record a baseline on
  the version you run today first (§18.2) if you want the usual gate as well.
- **Configuration** comes as a reviewed patch to `business_rules.yaml`,
  `examples.yaml` or `aliases.yaml` (the files whose wording the model and the
  retrieval read). Read the patch like any change to the prompt: it is text the
  model will see on every request. Apply it either through the admin
  configuration versions (`POST /admin/config/versions`; each change is a new
  version and `POST /admin/config/restore` reverts it) or by editing the file
  by hand. Then run `python scripts/verify_deployment.py` and restart the
  server, because the files are read at start-up. A change to
  `business_rules.yaml` or `examples.yaml` changes `prefix_version`, so the
  model server's prefix cache is cold once (§20.1); `POST /admin/llm/warmup`
  warms it.
- A patch is generic: it names tables, columns and rules of your schema, never a
  value from a question. If one does, send it back.

#### 18.6.2 Compare with the previous round

```bash
python -m eval.cli compare eval_data/share/round-1.json eval_data/share/round-2.json
python -m eval.cli compare eval_data/share/round-1.json eval_data/share/round-2.json \
    --split test --split-file eval_data/split/test_ids.txt --fail-on-regression
```

It reads both files by `case_id` (a plain `--out` report works as well, with
the classes it recorded) and prints ids and counts, never a question, SQL or
value: the cases **fixed**, **regressed**, **still failing**, **added** and
**removed**; the accuracy before and after overall, on the cases both rounds
have, and per tag; and the shift in the count of each mismatch class (a fix to
retrieval should move `actual_is_subset` and `column_count_differs` before it
moves anything else). It needs no model, no database and no `project_config/`,
so the reviewer can run it too.

**The paired statistics.** Both rounds answered the same cases, so the cases both
scored are a paired sample, and `compare` prints (and `--json` carries under
`paired`) the counts that matter for it: **fixed**, **regressed** and **unchanged**
(passing in both or failing in both); the **exact two-sided McNemar p-value** on the
fixed and regressed cases (the chance of a split at least as lopsided if the two
rounds were equally accurate; no approximation, so it holds for small counts); and a
**95% interval of the accuracy difference** (Newcombe's method for paired proportions,
which is narrower than comparing two intervals because the rounds fail on mostly the
same cases). Read them before you read the delta: 12 cases fixed and 4 regressed out of
150 is +5.3 points with p = 0.077, a difference the data cannot yet separate from
chance; 12 fixed and 1 regressed is p = 0.003. A comparison of fewer than 20 cases is
marked `n<20: not interpretable`. An older report compares exactly as before.

`--split dev|test` with `--split-file` limits the comparison to one half; the
file is `dev_ids.txt` or `test_ids.txt` from step 1 (a JSON list of ids, or an
object with `dev` and `test` lists, also works). With `--fail-on-regression`
the exit status is `1` when any compared case passed in the old round and
fails in the new one, so `--split test --fail-on-regression` is a regression
in the held-out half. Without the flag the status is `0`. **Two rounds
generated by different pipelines (`run.pipeline`: `production` against
`single_shot`) are refused with status `2`**; `--allow-mixed` compares them
anyway, with a note that the deltas include the pipeline. A bundle written for
a different golden file, or judged with a different `--reference`, is
compared with a note saying so.

#### 18.6.3 When to stop

The numbers that count are measured on the **test** half, with the halves of
step 1 unchanged between the rounds. Read them from
`compare ... --split test` (the "Execution accuracy" and "Per-tag accuracy"
lines), and the per-source figures from the `summary` block of the share file.

- **Accuracy 7.5:** held-out (test) execution accuracy of **at least 75%**,
  **no tag below 60%**, on **two consecutive rounds**. One good round is
  luck or a fix tuned to the cases; the second, with different fixes, is not.
- **Accuracy 8** (the roadmap target): execution accuracy of **at least 80%
  on at least 150 cases**, **with every data source at 75% or better**
  (`summary.per_datasource`).

A tag with only a few test cases swings by whole tens of points on one case:
read each tag's denominator, and add cases to a thin tag (§18.1) rather than
discount its miss. A set below 150 active cases can reach 7.5 but not 8.

#### 18.6.4 What the numbers do and do not say

The default figure is execution accuracy of the pipeline an analyst uses, so
`corrections` is real and a `guard_rejected` case is one that ran out of
correction rounds. Read `guard_rejections` in the file round by round: the same
message twice means the correction text did not help; a different message each
round means the model is searching. What it does not cover: per-key
`denied_columns`, the result cache, and the interpretation step. **The row
cap:** a question that asks for everything returns the first rows and no more,
as it does for the analyst. A result with exactly as many rows as the cap (the
`TOP n` the server injected, or `MAX_ROWS_RETURNED` if lower), fewer than the
reference has, every row of it a row of the reference (the same matching rules
as `compare`; for a reference with `ORDER BY ... TOP`, the first rows in that
order), qualifies for `match_capped`. It is not excused when a row is not in
the reference, nor when the result stops short of the cap. **A qualifying result
is then verified with one more read-only query:** `SELECT COUNT(*) FROM (<the
model's SQL before the cap was injected>) AS q`, built from the syntax tree
(an `ORDER BY` without `TOP`/`OFFSET`, a `WITH` clause and repeated or unnamed
columns, which SQL Server rejects in a derived table, are removed, hoisted or
named) and run through the same executor, so the query timeout and the
rolled-back read-only transaction apply. The case passes as `match_capped` only
if that count equals the reference's row count (`uncapped_rows` in the
diagnosis records it). An over-filtered query that merely returns a cap's worth
of valid rows has a smaller count and is `actual_is_subset`, a failure. If the
count cannot be asked (the SQL does not parse into a derived table, the guard
refuses it, a timeout) the case **fails** as `actual_is_subset` with
`capped_unverified: true`; there is no silent pass. **When the reference result
itself reached `MAX_ROWS_RETURNED`** its length is not its size, so the
reference is counted the same way (`SELECT COUNT(*) FROM (<the reference SQL>)
AS q`, same builder, same executor) and the two uncapped counts must be equal
(`uncapped_rows == reference_uncapped_rows`; there is no "at least" rule). If
the reference count cannot be asked, the case is `capped_unverified` and fails.
If the
model wrote its own `TOP` and only `MAX_ROWS_RETURNED` cut the result, the same
check runs on its SQL as written. `--single-shot` has no cap, so
`match_capped` never appears there. Only the counts are exported, never a row
value. `--single-shot` gives
the model-only figure; never compare it with a production round. §18.3
describes how "the same answer" is decided.

### 18.7 The benchmark track: a public benchmark on your own SQL Server

The golden set of §18.1 is the number that matters: your questions, your schema,
your analysts' verdicts. It is also small (150 cases is a good set), private (it
never leaves the server) and thin per tag, so it is a poor tool for the question
a developer asks every week: *did this change to the prompt, the retrieval or the
model help?* The **benchmark track** adds a second, separate instrument for that
question: a public text-to-SQL benchmark, loaded into a database of your own and
run through the same production pipeline with the same model. It gives a large,
fully visible sample, and because the data is public, the full report (questions,
generated SQL, errors) can be shared as it is.

**Its number is not your accuracy.** The questions are English and about
someone else's domain, the schemas are small and sometimes dirty, the "evidence"
hint that comes with each question does not exist in your analysts' work, and the
data was copied from SQLite. It does not replace the golden set and must never
be quoted as the deployment's accuracy. The report says so in its first line
(`BENCHMARK RUN (bird) ... This is NOT this deployment's accuracy`), every case
is tagged `bench:bird`, and a golden set is only a benchmark report when *all* of
its cases carry that tag, so the two cannot be mixed by accident.

#### The benchmark: BIRD mini-dev

[BIRD](https://github.com/AlibabaResearch/DAMO-ConvAI/tree/main/bird) is a text-to-SQL benchmark over
real-looking databases; its **mini-dev** release has **500 questions over 11
databases** (about 30% simple, 50% moderate, 20% challenging). It is published as
SQLite, MySQL and PostgreSQL, and there is no T-SQL version: that is what the
importer is for.

- **Licence.** The dataset is released under **CC BY-SA 4.0**
  (<https://creativecommons.org/licenses/by-sa/4.0/>): the `bird-bench/mini_dev`
  repository carries the licence badge, and the BIRD repository says "Due to large
  volume of requests, now we change the license to `CC BY-SA 4.0`". Some third-party
  papers cite BIRD as CC BY-NC 4.0; the repositories state CC BY-SA 4.0. Read
  the licence on the page you download from before you rely on either. The databases
  are collected by the BIRD authors from public sources (their paper names Kaggle and
  Relational.vit among them) and are distributed inside the same package under the
  same statement; no separate per-database licence is published, and the upstream
  sources may have terms of their own. Using the data to evaluate your own system is
  what the benchmark is for. If you **share** anything derived from it (the golden
  files, the reports with questions and SQL, a loaded copy), keep the attribution and
  the same licence; the import writes `NOTICE.txt` with the text. This is not legal
  advice.
- **Download.** Hugging Face `birdsql/bird_mini_dev`
  (<https://huggingface.co/datasets/birdsql/bird_mini_dev>) is the canonical copy;
  the `bird-bench/mini_dev` repository (<https://github.com/bird-bench/mini_dev>) also
  links `minidev.zip` and a Google Drive package. Unpack it **outside** the
  repository: nothing of the benchmark is ever committed.
- **Layout.**

  ```text
  mini_dev_sqlite.json                          question_id, db_id, question, evidence, SQL, difficulty
  dev_databases/<db_id>/<db_id>.sqlite
  dev_databases/<db_id>/database_description/<table>.csv
  ```

  The importer needs `db_id`, `question` and `SQL` of each question and tolerates a
  missing `evidence`, `question_id` or `difficulty`; it finds the JSON under
  whatever folder the package unpacked into.

#### Load it with an administrator login, read it with the application's

The load creates schemas and tables, which the application's login must never be
allowed to do. Use **two logins** on a **separate database** (never your
warehouse), created **case-sensitive and accent-sensitive**:

```sql
CREATE DATABASE BirdBench COLLATE Latin1_General_100_CS_AS_SC_UTF8;   -- SQL Server 2019 and later
-- older versions: COLLATE Latin1_General_100_CS_AS_SC  (or Latin1_General_BIN2)
CREATE LOGIN bench_loader WITH PASSWORD = '...';   -- DDL rights: used by bench-import only
CREATE LOGIN bench_reader WITH PASSWORD = '...';   -- read only: what the application uses
USE BirdBench;
CREATE USER bench_loader FOR LOGIN bench_loader;  ALTER ROLE db_owner ADD MEMBER bench_loader;
CREATE USER bench_reader FOR LOGIN bench_reader;  ALTER ROLE db_datareader ADD MEMBER bench_reader;
```

**Why the collation matters.** SQLite compares text case-sensitively and
accent-sensitively. A default SQL Server database does not, so `WHERE name = 'abc'`
also matches `'ABC'`, and a correct T-SQL answer can then differ from the SQLite
reference through no fault of the model. The import reads
`DATABASEPROPERTYEX(DB_NAME(), 'Collation')` and **refuses** a database that is not
case- and accent-sensitive (a name with `CS` and `AS`, or a `BIN` / `BIN2` collation)
before it writes anything, with the `CREATE DATABASE` line above in the message.
`--allow-ci-collation` imports anyway; the first line of `import_report.txt` and a
`WARNING` line in the header of every report then say the accuracy is a lower bound.
Text columns are `NVARCHAR` in the database's collation, so primary keys and indexes
work as usual (keys are limited to 450 characters, SQL Server's 900-byte limit); with
a case-sensitive collation, two keys that differ only in case no longer collide.

Each BIRD database becomes **one schema of that one database, named after its
`db_id`**, so a combined configuration exercises the multi-schema handling and the
schema exclusion of §2.3 and §16. Table and column names are kept and quoted.
Types are mapped conservatively from what the data holds, not from what SQLite
declared (SQLite does not enforce declared types): integers only `BIGINT`, numbers
`FLOAT(53)` (an IEEE double, so every value round-trips), anything with a text value
`NVARCHAR(n)` (dates stay the text they were, so a date is never silently
reinterpreted), blobs `VARBINARY(MAX)`. Rows are inserted in chunks (pyodbc
`fast_executemany` unless `--no-fast-executemany`); primary and foreign keys are
added **after** the data, and one SQL Server refuses is a warning, not a failure.
After the load every table's row count on SQL Server is compared with the SQLite file,
and the import stops if one differs. Then every table is compared **by content**: each
row is reduced to a canonical form (integers as integers; floats rounded to 12
significant digits, a relative tolerance of about 5e-13; text exactly, case and
trailing spaces included; blobs by their SHA-256) and hashed, and the sum of the row
hashes is an order-independent digest of the table, computed in Python on both sides in
chunks, so it works on large tables. A table whose digests differ is read again with a
digest per column, to name the columns that differ.

Run everything from a shell of its own, so the production `.env` is not touched
(a variable exported in the shell wins over `.env`):

```bash
export PROJECT_CONFIG_DIR=project_config.example           # only so the tools start; each run sets its own
export DB_CONNECTION_URL="mssql+pyodbc://bench_reader@server:1433/BirdBench?driver=ODBC+Driver+18+for+SQL+Server"
export DB_PASSWORD='...'                                    # bench_reader's raw password
export BIRD_ADMIN_URL="mssql+pyodbc://bench_loader:<percent-encoded password>@server:1433/BirdBench?driver=ODBC+Driver+18+for+SQL+Server"

python -m eval.cli bench-import bird --source /data/bird/MINIDEV --target-url env:BIRD_ADMIN_URL \
    --out /data/bird/out --descriptions --with-evidence
```

`--target-url` is the administrator login (`env:NAME` reads it from an environment
variable, so the password is not in the command line or the shell history). The
configurations are drafted with the **application's** login: the inspector of §2.3
runs as `bench_reader` through `DB_CONNECTION_URL` (or `--datasource NAME` for a
source of `datasources.yaml`), so a draft lists exactly what the application can
query. `--dbs a,b` imports only those databases. A column that SQLite stores with mixed
types (numbers and text in one column) is loaded as text, so the copy cannot answer a
question about its storage type (`typeof`) as SQLite does; the importer lists these as
semantic differences below. The import refuses a schema that
already holds tables (exit `2`, nothing written): add `--replace` to drop and reload
it, or `--skip-load` to keep what is there and only check its row counts against the
SQLite files again.

#### What the import writes

```text
/data/bird/out/
  golden.jsonl  golden.with_evidence.jsonl   all databases (the second only with --with-evidence)
  <db_id>/golden.jsonl  .../golden.with_evidence.jsonl  .../project_config/
  _combined/project_config/                  one configuration over every schema
  import_report.json  import_report.txt  NOTICE.txt
```

- **The reference result comes from the original.** Each question's expected result is
  produced by running the dataset's own SQL on the dataset's own `.sqlite` file (opened
  read-only) and is stored in the case (`expected_rows`, `match: "execution"`). A run
  with `--reference stored` compares the answer with those rows by execution accuracy
  (§18.3: column names ignored, the order of rows only where the reference sorts with
  `TOP`). No reference is translated, so a translation error cannot make a question
  wrong. Cases have the tags `bench:bird`, `db:<db_id>` and `difficulty:<x>`.
- **The data are shown faithful, not guessed from queries.** The table-content comparison
  above is the load-fidelity check. If every table is identical, the copy is faithful and no
  question is excluded for a doubt about the load. If a table differs, the import report
  names it and the columns, and **only the questions whose reference SQL reads that table**
  are excluded, with `exclusion_reason: "load_fidelity"`; the rest are unaffected.
- **A per-query check, as a diagnostic only.** Each reference is also transpiled to T-SQL
  (sqlglot), run on the copy and compared with the SQLite result. The transpilation is kept as
  `expected_sql` for reading, ordering and `eval.cli recall`; a stored run never executes it.
  The outcome is a tag on the case and never removes it by itself:
  `fidelity:semantic_difference` when the result differs although every table the query reads
  is identical (integer division, string comparison, NULL ordering, a mixed-type column: the
  dialect's semantics, not the data), and `fidelity:unverified` when the reference cannot be
  transpiled or fails on SQL Server (`julianday`, a SQLite double-quoted string...), which is
  information only. A semantic-difference case **stays in the count by default**: the model
  is asked to answer the question correctly in T-SQL, so the SQLite answer is still the right
  answer, and dropping the questions that are hard to translate would inflate the accuracy.
  They are counted and listed (`import_report.txt`, "Semantic differences"), and every report
  gives the accuracy on all scored cases and, beside it, without them.
  `--exclude-semantic-difference` removes them instead (reason `semantic_difference`).
- **Excluded cases are listed, never dropped.** `reference_failed` (the SQLite reference errors
  or runs over `--query-timeout`), `reference_empty` (no row, or only NULLs),
  `reference_too_large` (as many rows as `MAX_ROWS_RETURNED`, which the server would cut),
  `load_fidelity` (the reference reads a table that differs) and, only with the flag,
  `semantic_difference`. They stay in `golden.jsonl`, are skipped by `run` (which prints
  `Not scored: N of M case(s) -- excluded ...`) and are listed one by one with their reason in
  `import_report.txt`. Transpilation trouble never excludes a case.
- **Evidence.** Each question comes with an expert's `evidence` hint. Your analysts
  give none, so the default golden file has none. `--with-evidence` also writes
  `golden.with_evidence.jsonl`, the same cases with the hint appended under a marker, for
  the ablation below.
- **Configurations.** `<db_id>/project_config/` is the inspector's draft restricted to
  that schema (`--include-schemas <db_id>`), completed so it can be a `PROJECT_CONFIG_DIR`:
  `system_prompt.md` and the two policy files come from `--base-config` (default
  `project_config.example/`; pass your live `project_config/` to run with your own
  prompt), and `business_rules.yaml`, `examples.yaml`, `metrics.yaml` and
  `retrieval_hints.yaml` are **empty**, so the benchmark gets none of your hand-written
  help. Without `--descriptions` the configuration is the raw draft an operator has on
  the first day; with it the dataset's `database_description/*.csv` text is added to each
  column's description in `schema.yaml`, which models a curated configuration. Compare
  like with like.

#### Run it

```bash
python -m eval.cli bench-run bird --bench /data/bird/out --label model-a                 # per database, merged
python -m eval.cli bench-run bird --bench /data/bird/out --label model-a-combined --mode combined
python -m eval.cli bench-run bird --bench /data/bird/out --label model-a-hint --evidence
```

`bench-run` starts `python -m eval.cli run --golden <db>/golden.jsonl --live --reference stored`
once per database, each in its own process with `PROJECT_CONFIG_DIR` set to that
database's configuration (the pipeline reads its configuration when it starts, so one
process measures one configuration), then merges the reports into
`runs/<label>/report.json` and `report.txt`. The merged figure pools all scored cases;
it is not a mean of per-database means. Arguments after `--` go to every `run`
(`bench-run bird --bench ... -- --single-shot`). `--merge-only` merges reports already
there; the exit status is `1` if a database produced none. Give each model or setting
its own `--label`, or the next run replaces the last.

- **Per-database mode** (the default): one configuration per database, so retrieval
  chooses among that database's tables only. Use it to compare models, prompts and
  settings.
- **Combined mode** (`--mode combined`): one configuration over all 11 schemas, so the
  pipeline must also find the right database; it is the 6.13 multi-schema case at its
  hardest. It is a **separate measurement**: the report says `Mode: COMBINED ... do not
  compare it with the per-database figure`, and the two are never merged.
- **Evidence ablation.** Run once without and once with `--evidence`, then
  `python -m eval.cli compare runs/model-a/report.json runs/model-a-hint/report.json`:
  the paired block (§18.3) says whether the hint moved accuracy by more than noise.

#### Reading and sending the report

The text report starts with the benchmark notice and says the mode, whether the hint was
given and whether descriptions were added, then states how many cases were scored, how many
were excluded and why, and how many are flagged (`Cases: scored 112; excluded 14
(reference_empty 9, ...); semantic-difference cases kept in 5; ...`). It gives **two
accuracies**: on all scored cases, and without the semantic-difference cases ("Without the 5
semantic-difference case(s): ..."); the first is the figure for the model asked to answer in
T-SQL, the second the figure where SQLite's and T-SQL's semantics cannot disagree, and the gap
between them is the cost of the dialect. The overall figure and every stratum (`db:*`,
`difficulty:*`, and the two `fidelity:*` tags) carry a Wilson 95% interval and their `n`; a stratum under 20
cases is marked **not interpretable** (§18.3): with 30 questions in a database a single
case moves its accuracy by three points. To compare two models or two settings use
`compare` on the two merged `report.json`, not the two percentages.

The data is public, so **the full report is shareable**: send `runs/<label>/report.json`
(or `report.txt`) and `import_report.txt` as they are. They hold the questions, the
generated SQL and the error text, and no result row. Skim the `error` fields for a host
or database name your driver may echo, and send nothing else from your server (not
`.env`, not the configuration you ran the model with). Anything you share carries the
attribution of `NOTICE.txt`.

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
- `project_config/.setup_log.json`: one written before 6.8.0 holds the database
  URL with its password, and the current one holds the aliases, rules and
  examples the model produced (§2.2).
- `logs/audit_log.jsonl*` and `logs/query_log.jsonl*` (real questions and the
  SQL generated for them), the SQLite files under `logs/` (`app.db`,
  `sessions.db` and their `-wal` and `-shm` files), `exports/` (result sets),
  `eval_data/` and every report written from it (§18; the one exception is the
  share file, below), and `project_config_draft/` (drafts may quote real
  values, §2.3).

**Safe to send, after you have looked at it:**

- The output of `python scripts/verify_deployment.py`. It never prints a
  password: connection targets are shown with the password masked, and
  `VERIFY_API_KEY` is not echoed. It does name hosts, databases and tables.
- `python scripts/sync_schema.py`, `python scripts/assign_datasources.py` and `python scripts/prompt_budget.py`
  output: table names, column names, types and numbers; no credential or API key is printed.
- The aggregate report of `python scripts/analyze_audit_log.py` (§8), but not
  the `--include-examples` one.
- The share file of `python -m eval.cli run --share` (§18.6), after you have
  read it: it holds your questions and both SQL texts and no result value.
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

## 20. Latency: the prefix-cache warm-up and the split `llm` stage

The `llm` stage is almost all of a question's wall-clock time, and it used to
be one number. This section is about the two things that are now
separated out of it: the **prefill** of the static prompt prefix (which a
restart makes slow once, and which a warm-up now pays for before a question
does), and **waiting versus generating** (which `LLM_STREAM_TIMINGS` lets you
tell apart).

### 20.1 Why the first question after a restart is slow

Every SQL-generation request starts with the same text: the system prompt,
the business rules, the schema, the relationships and the examples of one
data source (the *static prefix*, `prompt_engine/static_prefix.py`), then a
short tail (filters, session context, the question). A model server that
caches prompt prefixes only computes the tail; one that has not seen the
prefix yet computes all of it. On a large local model that is about a minute.
The cache starts empty whenever the model server starts, so the question that
used to meet that minute was whichever one arrived first.

**Turn prefix caching on in the model server.** For vLLM that is
`--enable-prefix-caching` (recent versions enable it by default; passing it
explicitly costs nothing and puts the dependency in the launch command where
the next person will see it). Add `--enable-prompt-tokens-details` if you
want the server to say how many prompt tokens came from the cache
(`usage.prompt_tokens_details.cached_tokens`); the warm-up log line shows the
number when the server reports it. llama.cpp's server reuses the KV cache of a shared prompt prefix
(`cache_prompt`, on by default in current versions). Without any prefix caching on the model server, nothing in this
section makes a question faster, and the warm-up costs one extra prefill at
start-up.

### 20.2 The start-up warm-up

`LLM_PREFIX_WARMUP_ON_STARTUP` (default `true`) makes the server, as it
starts, send the model server one request per data source whose prompt uses
the static prefix:

- `messages` is exactly the prefix real requests start with, taken from the
  same function that builds real prompts (`llm/router.build_prompt_segments`),
  so it cannot drift from them;
- `max_tokens` is `1` and `temperature` is `0`; the sampling fields and
  `LLM_EXTRA_BODY` are the ones real requests send;
- it runs on a background thread. The server accepts requests at once, and a
  question that arrives while the warm-up is still running is served as it
  would have been without one;
- it is bounded by `LLM_PREFIX_WARMUP_TIMEOUT_SECONDS` (default `180`) for
  the whole pass, is never retried, and every failure is one log line and
  nothing else. The lines carry no prompt text, no response text and no key.

It is **on by default** because it is inert unless there is something to
warm. It sends nothing when the schema is over `PROMPT_RETRIEVAL_TOKEN_BUDGET`
(no stable prefix to cache; §16.6), when `LLM_PROVIDER=mock`, or when the
endpoint is not trusted and `LLM_ALLOW_REMOTE` is off (the same gate real
requests pass). Turn it off (`LLM_PREFIX_WARMUP_ON_STARTUP=false`) if the
model server is shared and metered per token, or if start-up must send no
model traffic.

What to look for in the start-up log, one line per data source:

```
LLM prefix warm-up: source=(whole schema) warmed in 41873 ms, prompt_tokens=4612
LLM prefix warm-up: source=sales warmed in 9 ms, prompt_tokens=4612, cached_tokens=4608
```

The second form is a cache that was already warm (the model server did not
restart, this server did): the request came back in milliseconds. The other
outcomes are `skipped, its prompt uses retrieval, not a static prefix`,
`skipped, the endpoint is not trusted and LLM_ALLOW_REMOTE is off`, and
`failed after N ms (ReadTimeout)` / `(HTTPError (HTTP 400))`. A `failed`
line does not affect questions; it means the first one will be slow, as it
used to be. HTTP 400 usually means the model server rejected `max_tokens=1`
or a field in `LLM_EXTRA_BODY`; the same field would be rejected on a real
question, so the log of the model server says which.

**Verify it worked:** ask a question straight after start-up and look at the
audit record: `llm.prompt_tokens` should be a few hundred (the tail), not the
size of the prefix, and `llm.prefix_cache_hit` should be `true` (and
`ttft_ms`, §20.4, should be small).

**What it cannot do.** The warm-up primes the cache once. A model server
that restarts on its own, or that evicts the prefix under memory pressure
from other traffic, is cold again, and so is a different replica behind a
load balancer (the warm-up reaches whichever replica answers). After a model
server restart, run the warm-up by hand (§20.3). After editing the system
prompt, business rules, schema or examples in the admin panel the prefix
changes and the old cache entry is useless; the same manual call prepares the
new one. For the eviction under other people's traffic, `LLM_PREFIX_KEEPWARM_SECONDS`
repeats the warm-up on an interval (§20.7).

### 20.3 Warming on demand: `POST /admin/llm/warmup`

```bash
curl -X POST -H "Authorization: Bearer $OPS_KEY" http://localhost:8000/admin/llm/warmup
```

Needs the `operations` capability. Runs the same pass synchronously, whatever
`LLM_PREFIX_WARMUP_ON_STARTUP` says, and returns one entry per data source:

```json
{"results": [{"source": null, "status": "warmed", "detail": null,
              "duration_ms": 41873, "prompt_tokens": 4612, "cached_tokens": null}]}
```

`status` is `warmed`, `skipped` or `failed`; `detail` says why, without prompt
text. A source that failed is reported in the body, not as an HTTP error. If a
warm-up is already running (the start-up one, say) the answer is `409` and
nothing is sent. The call is recorded in the admin-action log as `llm.warmup`.

### 20.4 Opening up the `llm` stage: `LLM_STREAM_TIMINGS`

A non-streaming request cannot see inside its own wait: `timings.llm_ms` and
`llm.total_ms` say how long it took, not whether the time was spent queueing
and prefilling or generating. With `LLM_STREAM_TIMINGS=true` the request is
sent with `stream=true` (and `stream_options={"include_usage": true}`) and the
chunks are reassembled into the same response: same content, same reasoning
text, same `finish_reason`, same `usage`. It defaults to `false`; turn it on,
compare `total_ms` before and after for a day, and leave it on if nothing
moved. Two differences to know about: the request timeout (120 s) now also
bounds the whole call, checked as chunks arrive, and a proxy between this
server and the model server that buffers or rewrites server-sent events will
make `ttft_ms` look like `total_ms`. The constrained-decoding request
(`LLM_STRUCTURED_OUTPUT=true`) is not streamed.

The audit record's `llm` block gains four fields (they are `null`/`false` in
a record written without them, and records written by earlier versions simply lack them):

| Field | Meaning |
|---|---|
| `ttft_ms` | Request sent to first generated token (reasoning or answer): the model server's queue plus prefill. Only with `LLM_STREAM_TIMINGS`. |
| `generation_ms` | First token to the end of the response; `ttft_ms + generation_ms == total_ms`. Includes the time spent reasoning. |
| `reasoning_tokens` | How many of `completion_tokens` were reasoning. `null` when the response showed none (not the same as `0`). Recorded with or without streaming. |
| `reasoning_tokens_estimated` | `true` when `reasoning_tokens` is an estimate: the server reports `usage.completion_tokens_details.reasoning_tokens` only on some stacks, and where it does not, `completion_tokens` is split by the share of characters that are reasoning. |

The reasoning text itself is never stored. They describe the call that
produced the record's answer; a request that needed correction rounds also
spent earlier calls, which only `timings.llm_ms` includes.

`python scripts/analyze_audit_log.py` (§8) prints them as "LLM stage split":
p50/p95 of `ttft`, `generation` and reasoning tokens, the median share of a
call that was `ttft`, how many reasoning counts were estimates, and the share
of completion tokens that were reasoning. Every figure is over the calls that
have it, so a log that mixes streamed and older records is not skewed. How to
read it:

| You see | It means | Do |
|---|---|---|
| High `ttft_ms`, `prompt_tokens` near the prefix size, `prefix_cache_hit: false` | The prefix was prefilled: a cold cache (`prefix_cache_hit` only says the prompt was large, §20.8; the time is what says it was cold) | Check §20.1 (prefix caching on?), run §20.3 after restarts, consider §20.7 |
| High `ttft_ms`, `prompt_tokens` small, `prefix_cache_hit: true` | The model server queued the request behind others | Look at the model server's load and at `MAX_CONCURRENT_REQUESTS` (how many questions this server lets through at once), not at the prompt |
| Low `ttft_ms`, high `generation_ms` | Decoding is the cost | Next row |
| `reasoning_tokens` is most of `completion_tokens` | The model spends its time thinking | `LLM_EXTRA_BODY` to lower or switch off reasoning (§8, `.env.example`) or accept it; raising `LLM_NUM_PREDICT` only makes the call longer |
| `finish_reason: length` with high `reasoning_tokens` | Cut off while reasoning | The §8 `length` advice |

### 20.5 What this costs

Nothing per request apart from `LLM_STREAM_TIMINGS`, which replaces one
blocking read with a read of the same bytes in pieces: about 65 ms of CPU for
a response of a thousand tokens with a thousand tokens of reasoning (against
about 2 ms without streaming), measured against a local stand-in server. The static prefix itself was already built once per data
source and kept; it is rebuilt only when the configuration that feeds it
changes. `build_prompt_segments` takes about 7 µs per call (it was 8 to 23 µs,
growing with the prefix), so the prompt stage is not where a question's time
goes.

### 20.6 The same numbers on real use: the quality card

§20.4 reads latency from a log on the server. The admin panel's card
**«کیفیت در استفاده‌ی واقعی»** (and `GET /admin/quality?days=7`, for
operations or security keys) shows them continuously, next to the other
signals the audit log and the analysts' ratings already hold, over the last
7, 30 or 90 days and over all time, overall and once per data source:

- the share of questions that executed, were rejected by the guard, needed a
  correction round, returned no rows (of those that executed) or were out of
  scope, and the error rate by class (guard policy, guard correctable, scope
  decline, transport, execution, other);
- how many answers analysts confirmed («پاسخ درست است», §18.1.1) and flagged,
  and the confirmed share of the answers they rated: a **lower bound, not
  accuracy**, because they rate only some answers;
- end-to-end latency p50/p95, and p50/p95 of each stage (`plan`, `prompt`,
  `llm`, `guard`, `execute`, `interpret`), with the `llm` stage split into
  `ttft` (queue plus prefill) and `generation`.

The `llm` split exists only for calls made with `LLM_STREAM_TIMINGS=true`
(§20.4). To get it, set `LLM_STREAM_TIMINGS=true` in `.env` and restart; the
two `llm · ttft` and `llm · generation` rows fill from the first streamed
call on, and stay empty (0 samples) until then. That is expected, and the
card says so under the table. Records written before the restart never gain
the fields, so give it a day before reading the 7-day figures. Read the split
with the table in §20.4.

The route reads the whole audit log, so it is cached for
`ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` per window (the same cache as the
deployment-checks and schema-drift cards, but it makes no warehouse call), and
the card stays in the panel's 30-second auto-refresh; its refresh button
recomputes at once. The same figures print from the command line with
`python scripts/analyze_audit_log.py` (section "Real-use quality";
`--quality-days N` sets the window, `--json` adds a `quality` object); both
carry counts and percentiles only, never a question.

### 20.7 Keeping the cache warm: `LLM_PREFIX_KEEPWARM_SECONDS`

The start-up warm-up (§20.2) primes the model server's prefix cache once. On a
model server that other people also use, the cache does not stay primed: when
their traffic needs the memory, the least recently used prefix is evicted, and
the next question pays the whole prefill again (about a minute for a large
prefix on a large local model, and several times that when the server is also
busy). `LLM_PREFIX_KEEPWARM_SECONDS` (default `0`, off) makes this server send
each data source's prefix again every that many seconds, so the prefix is never
the one that has gone unused longest.

What it sends is the warm-up's request, built by the same code: the prefix
alone, `max_tokens=1`, `temperature=0`, the sampling fields and `LLM_EXTRA_BODY`
real requests send. The rules are the warm-up's, too:

- **A source a real question used within the last interval is skipped.** That
  question already refreshed the server's cache, so under traffic the
  keep-warm sends nothing at all. It only ever fills the quiet stretches.
- Nothing for a source on the retrieval path (§16.6), nothing under
  `LLM_PROVIDER=mock`, and nothing to an endpoint that is not trusted unless
  `LLM_ALLOW_REMOTE` is on.
- A failure is one `WARNING` line (`LLM prefix warm-up: source=... failed after N ms
  (ReadTimeout)`), not one per pass: a repeat of the same outcome is logged at
  `DEBUG` until the outcome changes, and the pass that succeeds again says
  `(recovered after N failed pass(es))`. No line holds prompt text, response
  text or a key. A failed pass never affects a question.
- It **pauses while maintenance mode is on** (nothing is sent; the status
  says `paused`) and **stops at shutdown**.
- The first pass is one interval after start-up, not at once; the start-up
  warm-up (§20.2) is what primes a cold server.

**Recommended value: `300`** (five minutes) for a model server shared with
other users, set in `.env`, then restart. The value must be `0` or at least
`30`; `python scripts/verify_deployment.py` and the server's start-up refuse
anything else. The reasoning: the server's cache has no timer, it evicts the
oldest entry when memory runs short, so what matters is that your prefix is
never much older than other people's traffic can age it. Five minutes keeps it
younger than almost any other entry while sending twelve small requests an
hour per source. Go lower (`120`) only if the check below shows the prefix is
gone between passes; do not go below `60`. Leave it at `0` on a model server
only this deployment uses (nothing else evicts the prefix), when the server
does not cache prefixes (§20.1), and when every source is on the retrieval
path (there is nothing to keep).

**What it costs.**

- *Under traffic, nothing.* A source with a question in the last interval is
  skipped, so a busy deployment sends no keep-warm request at all.
- *Idle, with the prefix still cached:* per source and pass, one request that
  the server answers from its cache with a single generated token (milliseconds
  to about a second of server time), so about 12 requests an hour at `300`.
- *Idle, with the prefix evicted:* one full prefill, the same cost the next
  question would have paid. The keep-warm moves that cost off an analyst's
  question; it does not add a second one unless nobody then asks. On a server
  that evicts the prefix between *every* pass this is a prefill every interval
  for no benefit, which is what the check below catches.

**What it saves.** One cold question's wait (the prefill time) for every
eviction it prevents. The honest estimate is `cold questions per day it
prevents × prefill seconds`; the audit log gives the first term: the questions
whose `llm_ms` (or `ttft_ms`, §20.4) sits at the prefill time after an idle
stretch, and `python scripts/prompt_budget.py` the prefix size.

**Check it is working.**

```bash
curl -H "Authorization: Bearer $OPS_KEY" http://localhost:8000/admin/llm/warmup
```

```json
{"keepwarm": {"enabled": true, "interval_seconds": 300.0, "running": true,
              "paused": false, "runs": 12, "last_run_at": "2030-01-01T10:20:00+00:00",
              "last_run_outcome": "warmed",
              "last_run": [{"source": null, "status": "warmed", "detail": null,
                            "duration_ms": 640, "prompt_tokens": 4612, "cached_tokens": 4608}]}}
```

`last_run_outcome` is `warmed` (a prefix was sent and answered), `skipped`
(every source was kept warm by questions, is on the retrieval path or is not
allowed; each entry's `detail` says which), `failed`, `paused` (maintenance
mode) or `busy` (another warm-up was running). With `--enable-prompt-tokens-details` on the
model server, `cached_tokens` close to `prompt_tokens` and a short
`duration_ms` mean the prefix was still cached when the keep-warm arrived: the
interval is short enough. `cached_tokens` far below `prompt_tokens`, pass after
pass, means the server evicts the prefix faster than the interval: lower it, or
turn the keep-warm off if the server evicts it whatever you do. The same
object is returned as `keepwarm` by `POST /admin/llm/warmup` (§20.3), and
`enabled: false` comes with a `reason` (`LLM_PREFIX_KEEPWARM_SECONDS is 0`,
`LLM_PROVIDER=mock has no model server`).

**What it cannot do.** It does not make the model server faster when other
people's requests are queued ahead of yours; a question that waits in their
queue with a warm prefix is still slow (§20.4: a high `ttft_ms` with a short
prompt). It keeps one replica warm, the one that answers, not each replica
behind a load balancer. And it keeps only the *static prefix* warm: a source on
the retrieval path has none, and its prompts are rebuilt for each question (see
§20.8 for the one shape of retrieval prompt that is nevertheless identical
every time).

### 20.8 Reading `prefix_cache_hit`, and how large the prefix is

**`prefix_cache_hit` is the model server's word when the server gives one.**
The audit `llm` block has three fields for it:

| Field | Meaning |
|---|---|
| `cached_tokens` | `usage.prompt_tokens_details.cached_tokens`: how many prompt tokens the server says it served from its prefix cache. `null` when the server does not report it (vLLM reports it only with `--enable-prompt-tokens-details`; add that flag to the launch command). `0` is a real answer: nothing was cached. |
| `prefix_cache_hit_source` | `"server"` when `cached_tokens` decided the flag, `"estimate"` when it did not. |
| `prefix_cache_hit` | With source `"server"`: true when `cached_tokens` covers at least half of the static prefix (of the whole prompt when that is shorter, or there is no prefix). With source `"estimate"`: the older rule `prompt_tokens < 0.5 × the source's static-prefix estimate` (`docs/api-contract-v2.md` §6). |

Records written before these fields existed carry neither, and read as
estimates. **Do not read an `"estimate"` as a cache result.** That rule was a
cache signal when the backend reported only the tokens it had to compute; an
OpenAI-compatible server such as vLLM reports the whole prompt in
`usage.prompt_tokens` whether or not most of it came from the cache. So an
estimate of `true` means *the prompt was short* (a question that went through
the retrieval path, or a small source), and `false` means *the prompt was about
as large as the whole prefix*, cached or not: such a request can return in under
a second (the server had the prefix) or take a minute (it did not). Without
`cached_tokens`, tell the two apart by the request's time against its size:

| `prompt_tokens` | `llm.total_ms` | Reading |
|---|---|---|
| near the prefix size | about a second | the server had the prefix cached |
| near the prefix size | a prefill's worth (tens of seconds) | cold: the prefix was computed |
| small | long | not a prefill: the request waited behind other people's, or the model decoded for long (`ttft_ms` and `reasoning_tokens`, §20.4, say which) |

**Latency by the real answer.** `python scripts/analyze_audit_log.py` prints
a "real hit rate (server)" line under "Cache behaviour", counted only over the
calls whose server reported `cached_tokens` (it says `n/a` when none did), and
a section "LLM stage by real cache hit" with the `llm` stage's time for `hit`,
`miss` and `unreported` calls. `unreported` is every call whose flag is only an
estimate; such a call is never put in `hit` or `miss`. The `--json` report has
the same as `latency_by_cache` (with `call_ms`, `prompt_tokens` and
`cached_tokens` per group) and `cache_behaviour.server_hit_rate`; the quality
card's JSON has `latency.llm_by_cache`. The admin panel's prefix-reuse tile uses the server rate
when there is one and says so. The warm-up and keep-warm entries report
`cached_tokens` too, when the server sends it.

**Prefix size.** The prefix is most of a cold question's cost, so its size is
latency. `PROMPT_RETRIEVAL_TOKEN_BUDGET=... python scripts/prompt_budget.py
--no-model` prints each source's characters and estimated tokens
(`len(text) // 4`, the estimate the whole project uses) without contacting the
model; without `--no-model` it also asks the model server for the real count.
For the synthetic `project_config.example` and its own system prompt the
estimate is 1,798 tokens (7,193 characters). Two formatting changes shrank the
prefix without removing anything the model reads (the example was 1,815 tokens
before): a column line is `- Name: description` instead of
`  - Name: description`, and a business rule's trailing newline no longer adds a
blank line between rules. The saving is two characters per column, so it grows
with the schema (about 2% of a large one); the larger levers (a description
repeated on many columns, rules the model rarely needs) change content, and
stay with whoever owns `project_config/`.

**A retrieval prompt that is identical every time.** When retrieval finds no
table for a question, the prompt for a source shows *every* table of that
source (§16.6). With no tables there is nothing else retrieved either, so that
prompt is the same bytes up to the question, and a model server with prefix
caching serves it from cache after the first. It is not a static prefix, so
neither the warm-up nor the keep-warm sends it, and it is as large as the
whole source. A source whose whole-schema prompt is large and which meets this
case often is a candidate for `PROMPT_RETRIEVAL_TOKEN_BUDGET` above its
estimate (the static path, warmed and kept warm), if the model's context
window fits it; `python scripts/prompt_budget.py` says whether it does.

### 20.9 Corrections that name what exists

A question whose SQL the guard rejects is re-prompted up to twice
(`MAX_CORRECTION_ATTEMPTS`), so a question that needs both rounds is three
model calls: `corrections` in the audit `llm` block counts the rounds, and
`timings.llm_ms` includes all the calls while `llm.total_ms` describes only the
last. In a log of real use, the requests that needed corrections were a large
share of all model calls and nearly all of them ended refused: the guard's
message ("Unknown column 'X' on table 'T'") says what is wrong and not what is
right, and the model guessed again.

The retry prompt now adds what is right, from `schema.yaml`, for two rejections
and only those:

```
--- DATABASE ERROR ---
Unknown column 'Bogus' on table 'Customer'

--- COLUMNS THAT EXIST ON Customer ---
ID, Name, NationalID, IsActive
Use only these names for Customer; any other column does not exist there.

--- INSTRUCTIONS ---
```

and, for an unknown table, the real tables whose names are closest to the one
written, as the reference to write in `FROM`:

```
--- TABLES THAT EXIST, CLOSEST TO Custmer ---
[sales].[Customer]
Use one of these names exactly as written, or another table from the schema above.
```

(The names above are the synthetic `project_config.example`'s.) What it
guarantees:

- **Names only**, in `schema.yaml` order, no descriptions. A table with more
  than 200 columns ends the list with `... and N more`. With several data
  sources the table suggestions come from the source the question was routed to.
- **Access policy.** A column the key's `denied_columns` fully denies is left
  out of the list. A join-only column (`Source:schema.Table.Column` entries)
  stays in the list marked `(join key only)`: the
  guard allows exactly that use, and the first-attempt prompt already says so.
  If the key's policy cannot be read, no hint is added.
- **The cached prefix is untouched.** The hint is part of the retry text,
  which is appended to the question segment, never to the static prefix, so it
  cannot cost a prefill. A block the model has already been shown in the same
  request is not repeated in the next round.
- Any other error (syntax, execution, an ambiguous table that already says
  "did you mean") gets exactly the correction text it got before.

To see whether it helps on your own log: `python scripts/analyze_audit_log.py`
prints the correction-round distribution; compare the share of requests with
`corrections: 2` that end rejected before and after the upgrade.

### 20.10 A response cache in front of the model

**What it is.** A gateway or proxy between this application and the model
server (an LLM gateway, an API management layer, a caching reverse proxy) that
keeps a *response cache*: when a request looks like one it has answered before,
it returns the stored reply and never reaches the model. Two kinds matter here.
A *semantic* cache matches prompts that are similar in meaning or text; a
*prefix* cache matches prompts that start the same way. A request this
application sends is a long fixed prefix with a short question at the end, so
two different questions are, to such a cache, almost the same prompt. This is
not the model server's own prefix **KV** cache (§20.1 to §20.8), which only saves
computing the prefix and always generates a fresh answer; a response cache
skips the generation. Nothing in this application caches model replies on its
own: the query-result cache (`GET /admin/cache`) keys on the question and the
SQL, and the endpoint's `cached_tokens` in the audit `llm` block (§20.8) counts
KV-cache reuse, not this.

**The symptom.**

- **Identical answers for different prompts.** The setup wizard writes one
  alias list for most of the tables of a database (the list the model gave for
  the first table), only the tables with long column lists, whose prompts differ
  most, getting their own; run against a second database later, it writes that
  same list again for a large share of its tables. The business rules read as
  nonsense, such as `Value column: X. Volume column: X.` on a table where that
  makes no sense.
- **Replies in milliseconds.** A request answered in single-digit milliseconds
  with the full completion length (`llm.total_ms` of 8 and `completion_tokens` of
  512 in the audit log) was not generated: no model produces 512 tokens in 8 ms.
- **Byte-identical SQL for different questions**, with very short `llm.total_ms`.
  The audit record then carries `suspected_cached_response: true` and the server
  logs one `WARNING` per streak (below).

**The risk.** Beyond the wizard, the same endpoint serves SQL generation. A
semantic cache can answer "count of active customers" with the SQL cached for
"count of inactive customers". That SQL is valid, the guard accepts it, the
query runs and returns rows: a silent wrong answer, with a clean audit record.
It also corrupts accuracy measurement (§18): a run that replays questions
through a caching gateway measures the cache, not the model.

**How to confirm.**

1. `python scripts/verify_deployment.py`, the check `Model endpoint answers each
   prompt itself` (§3.1). It sends two prompts that differ only in a random token
   and fails when the second is answered with the first's token. Run it from the
   same host and with the same `.env` as the server. It tests the endpoint in
   `OPENAI_BASE_URL`; with `LLM_ENDPOINTS` (§2) point it at each in turn.
2. The setup wizard stopping with `Step 3 stopped` (§2.2) says the same about
   `WIZARD_LLM_BASE_URL`, which defaults to `OPENAI_BASE_URL`.
3. In the audit log, `suspected_cached_response: true` in an `llm` block, or the
   `WARNING` named `Suspected response cache in front of the model`.

**The fix.** Disable response caching for this client at the gateway (an
exemption for the application's key or route; a semantic cache has no safe
setting for SQL generation), or point `OPENAI_BASE_URL` (and
`WIZARD_LLM_BASE_URL`) at the model server directly. Run the check again; it must
say `PASS`. If you cannot change the gateway, do not use this endpoint for SQL
generation.

**The runtime early warning.** The server keeps, in memory and per process, the
last 200 pairs of (question hash, SQL hash) per data source for the questions
that reached the model on `POST /query` (a question answered from the query cache
never does). When a *different* question comes back
with byte-identical SQL within two minutes and the model's reply took less than
`LLM_CACHE_SUSPECT_MS` (default `50`; `0` turns the warning off), it logs one
`WARNING` per streak (a streak ends after two quiet minutes) and writes
`suspected_cached_response: true` in that request's audit `llm` block and in the
`llm` block of the response. It never blocks or changes a request: two questions
can legitimately map to the same SQL, and the check is a hash and a dictionary
lookup (no model call, no I/O). Only `POST /query` is watched (all three of
its modes), not the multi-turn session engine, whose refinements can reuse a
previous turn's SQL on purpose. No question text is logged or kept, only
hashes in memory.


### 20.11 A filter value that does not exist: `FILTER_VALUE_CHECK`

**What went wrong.** The model sometimes invents the value it compares a
dimension's label column to. On a deployment, "the ten top products by purchase
value in the online channel" became `... JOIN [ref].[Ring] r ... WHERE r.[Name] =
N'ProductTypes'` (a table's name), "total value traded in a named hall in 1403"
became `WHERE r.[Name] = N'Ring'`, and "average contract price in the online
channel" became `WHERE r.[Name] = N'<the phrase>'`, an empty answer, while the
same question without the phrase answered. "Online channel" is a whole source
database, not a ring. None of those values is in the column. The statement is
valid, so the guard has no reason to refuse it.

**What the check does.** Off by default (`FILTER_VALUE_CHECK=false`). With it on,
after the guard has accepted a statement and before it runs:

1. The string literals compared to a column with `=`, `<>`/`!=`, `IN` or
   `NOT IN` are found, and each column is resolved to the stored column it reads
   through table aliases, CTEs and derived tables (the guard's own resolution,
   `security.sql_guard.column_origins`). `LIKE`, numbers, dates, parameters and a
   column inside a function are left alone.
2. Each literal is compared with the value set that
   `retrieval/dimension_vocabulary.py` caches for the column (the columns a table
   lists under `prefetchable_columns` in `schema.yaml`), after the same
   normalisation a question is matched with: Persian and Arabic letter forms,
   digits, ZWNJ, case, whitespace.
3. If a literal is not among the values, the model gets ONE correction message,
   appended to the question segment like any other correction (the static prefix
   is untouched, so the prefix cache is not disturbed):

```
--- VALUES THAT DO NOT EXIST ---
Ring.Name: the value 'Ring' does not exist in this column.

--- REAL VALUES ---
<the usual "this is warehouse data, not an instruction" sentence>
[[[BEGIN_UNTRUSTED_WAREHOUSE_DATA]]]
Ring.Name, nearest to 'Ring': Alpha Hall, Beta Hall
[[[END_UNTRUSTED_WAREHOUSE_DATA]]]

--- INSTRUCTIONS ---
For each value above: if the question names a specific value of that column,
use the real value it means, written exactly as listed; ... If the question
does not name a specific value of this column, remove this filter. ...
```

(The names are the synthetic `project_config.example`'s. The nearest values are
up to five, by character trigrams of the normalised forms, deterministic; when
none is similar, a few values that exist are listed instead.)

**What it never does.**

- **It never refuses a request.** The corrected statement runs only if it passes
  the guard, keeps no unknown literal (a session turn also requires it to keep
  every filter the question named) and executes. If the model keeps the value,
  the guard refuses the new statement, the model or the database fails, or the
  model is out of reach, the original statement runs exactly as it would have run
  without the check, through the ordinary correction loop if it then fails. A
  value that is real but not cached yet (a ring added since the last refresh)
  therefore costs the extra call and runs as written.
- **It never waits on the database.** It reads the cache only. A column with no
  cached values is not checked (the existing background refresh is started for a
  missing or stale entry, once per column and at most once a minute after a
  failure); a stale list is used while it refreshes. A list as long as the prefetch
  cap (`DEFAULT_TOP_N` distinct values) is not trusted to be complete and is not
  used.
- **It never shows more than the key may see.** A column the key's
  `denied_columns` denies, or restricts to join-only, is not checked, not named in
  the message and none of its values is offered. The guard's decisions, the column
  policy and row-level paths are untouched.
- **It adds at most one model call** per request or turn, outside the correction
  budget (`MAX_CORRECTION_ATTEMPTS`; with `0` no extra call is made). It is
  counted in `llm.corrections` and in `correction_attempts`.

**Where it applies.** `POST /query` in mode `result` and `full`, and the
fresh and carry-forward generation of a session turn. Mode `sql` (no correction
loop) and the `_prev` refinement of a session turn are not checked.

**Turning it on.**

1. Give the small dimension tables `prefetchable_columns` in `schema.yaml` (a few
   hundred distinct values at most; leave out tables of people and anything
   large). Without it there is nothing to compare with.
2. Set `FILTER_VALUE_CHECK=true` in `.env` and restart. Run the preflight: with
   the setting on it lists `Filter value check has vocabularies` (§3.1), whose
   advice names the dimension tables that have label-like columns and no
   `prefetchable_columns`.
3. Watch the audit log. A request where the check found something to compare has
   a `value_check` block:

```json
"value_check": {"checked": 2, "unknown": [{"column": "Ring.Name", "value_sha256": "0f3a...", "length": 4}], "corrected": true, "fallback": false}
```

`checked` is how many literals had a cached vocabulary; `unknown` lists the ones
that were not in it, by column, a hash of the normalised literal (the same
invented value can be counted across requests) and its length; `corrected` means
the statement that ran is the corrected one, `fallback` that there were unknown
literals and the original ran. **The literal itself is not written** (this log
keeps names, never values; the statement that ran is in `generated_sql` as ever)
and neither are the real values the model was shown. A line with nothing to
compare has no `value_check` key, and so does every line written with the
setting off. To count the requests it corrected:

```powershell
(Select-String -Path logs\audit_log.jsonl -Pattern '"corrected": true' | Measure-Object).Count
```

A corrected request counts in `llm.corrections`, so the real-use quality card's
"needed a correction" share rises by those requests; the card has no figure of its
own for the check.

**What it costs.** Parsing the statement and comparing a few strings with a few
hundred: negligible. One more model call for each request whose statement filters
on a value that is not cached as real. The `timings` of the request include it
(`llm_ms`, `guard_ms`, `execute_ms` add up across the calls, as for any
correction).

**What it cannot catch.** A value that exists but is wrong for the question; a
filter on a column that is not in `prefetchable_columns`; a `LIKE` pattern; a
literal compared to a column inside a function or a computed expression.
