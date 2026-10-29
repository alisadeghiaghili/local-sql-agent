# Setup — Step by Step

> **[فارسی](../fa/getting-started.md)**

> **This guide is for 4.5.0 and later.** Three things changed from 4.3.0, and
> if you follow an earlier version of this document you will get stuck on
> exactly these:
>
> | What | Before | Now |
> |---|---|---|
> | The UI's default mode | Demo; you switched to live by hand | **Live** (4.4.0) |
> | The backend address | A field at the top of the page | The file `web/js/config.js` (4.4.0) |
> | Keys and roles | `.env` only | The application database; `.env` is bootstrap only (4.5.0) |
>
> If you are looking for the "backend address" field (نشانی بک‌اند) at the top of the page, it has **been removed** — section 2.5.
>
> And two common traps that have nothing to do with the version, both
> explained in section 2.2: **type the raw key in the browser, not the
> `key_sha256`** (43 characters versus 64), and if you write `API_KEYS_JSON`
> over several lines, the whole value must be in **single quotes** (and must
> not contain an apostrophe) or it will not be read. The simpler way: put the
> array of keys in a file and point to it with `API_KEYS_FILE`.
>
> **Since 4.7.0:** `issue_api_key` has a flag for each of the three roles
> (`--full-admin` grants all three) and prints the raw key inside a box that
> names its destination. If the admin panel is half-403 for you, section
> 2.5.1 says why: `--admin` on its own opens only 4 of the 13 sections.
>
> **Since 4.9.0:** if your language model reasons (Qwen3, DeepSeek-R1,
> gpt-oss) and the UI says «جوابی نگرفتم» ("I got no answer"), read section
> 2.9 — it is almost always the token cap, not the model and not the prompt.
>
> **Since 4.10.0:** after you save the key, the sign-in field **collapses**
> and what stays is `کلید: ذخیره شده ✓` ("Key: saved ✓") plus a «تغییر کلید»
> ("Change key") button. If you are looking for the field and it is not
> there, the key is saved. Before, the empty field always stayed in place and
> everyone thought they had been signed out.
>
> **Since 6.13.0:** the setup tools no longer write into `project_config/`. The wizard and
> the inspector write drafts to `project_config_draft/` and you copy across what you accept
> (sections 0.3.1 and 0.3.2). Without `--db-url` they read every source of `datasources.yaml`
> through the application's own login, so a URL on the command line is only for a first
> install; and the inspector no longer puts sample values from the database into `schema.yaml`.

There are two ways to use the system, and they are **completely separate**:

| | CLI | Web |
|---|---|---|
| Launch | `python app.py` | Two separate servers |
| Needs an API key? | **No** | **Yes** |
| Multi-turn conversation ("from among those…") | No | Yes |
| Multiple sessions and memory | No | Yes |
| Excel export | Automatic | Download button |

If you only want to see the engine work, start with the CLI — it is quicker
and needs no key.

---

## Install from scratch, in order

This list is the whole path from an empty machine to a verified, running server; each step has its command and its sign that it is finished, and the full explanation is in the section it points to. A step marked "several databases only" is skipped by anyone with a single database. Do not move to the next step until you have seen the "Done when" sign. Two things decide the order. The databases are described first (step 4), because every tool that drafts a file afterwards reads them through the login the application itself uses. And the first API key is issued (step 11) before the first preflight (step 12), because the preflight fails while no key exists. (The same list, with references to the sections of `docs/deployment-runbook.md`, opens that document.)

1. **Prerequisites.** Python 3.11 or newer; an ODBC driver for SQL Server (version 17 or 18) on the machine that runs the server; an OpenAI-compatible language-model endpoint reachable from that machine; and a read-only login on every database (`docs/db-hardening.md`, which the DBA applies). *Done when:* `python --version` shows 3.11 or higher and (after step 2) `python -c "import pyodbc; print(pyodbc.drivers())"` prints a list that includes `ODBC Driver 18 for SQL Server` or `ODBC Driver 17 for SQL Server`.

2. **Install the dependencies** (section 0.1): in a virtual environment, from the repository root, `pip install -r requirements.lock`. *Done when:* pip finishes without an error.

3. **The `.env` file** (section 0.2): `copy .env.example .env`, then fill in `DB_CONNECTION_URL` and `DB_PASSWORD` (with several databases, one `DB_PASSWORD_*` per source in step 4 instead), `OPENAI_BASE_URL` and `OPENAI_MODEL`; `OPENAI_API_KEY` only if your endpoint checks one. *Done when:* `python -c "import config as cfg; cfg.settings.validate(); print('settings ok')"` prints `settings ok`. Anything else is a `ValueError` that names the variable at fault.

4. **Describe the databases first: `datasources.yaml`** (section 0.3.3 step 1). Every tool of the steps that follow reads the databases through the login the application itself uses, so say where they are before anything else. *Several databases:* create the folder (`New-Item -ItemType Directory project_config`), write `project_config/datasources.yaml` from `project_config.example/datasources.example.yaml` (one entry per source), and put each source's raw password in `.env` under the variable name its `password_env` gives. *One database:* the file is optional and you can skip it. Without it the deployment has exactly one source, named `default`, whose connection is `DB_CONNECTION_URL` with the raw `DB_PASSWORD` of step 3, and every tool below reads that one. Write the file for one database only if you want a setting that lives in it: `exclude_schemas:` (the same rule is `EXCLUDE_SCHEMAS=` in `.env`), or `nolock: true` (step 10). *Done when:* `python -c "from database.datasources import datasource_names; print(datasource_names())"` prints the names of all the sources (`('default',)` for one database without the file) and the command of step 3 still prints `settings ok`.

5. **Copy the template** (section 0.3): `Copy-Item -Recurse project_config.example\* project_config\` (create the folder first if step 4 did not: `New-Item -ItemType Directory project_config`; the copy keeps a `datasources.yaml` already in it). What you now have is the template's sample content, not yours: `schema.yaml` lists the sample tables, and steps 6 to 9 replace the files one by one. *Done when:* the nine YAML files and `system_prompt.md` are in `project_config/`.

6. **Draft `schema.yaml`** (section 0.3.2): `python -m database.schema_inspector_cli --output-dir project_config_draft --exclude-schemas "*_Stg,Replay"`. It reads every source of step 4 (names, types and keys; no row) and writes `project_config_draft\schema.yaml`: every table described by its own name and every column by its type. Leave out the schemas you will never ask about, with `--exclude-schemas` for this run or, to keep the rule, `EXCLUDE_SCHEMAS=` in `.env` or `exclude_schemas:` in `datasources.yaml` (section 0.3.3): every table costs prompt tokens. Read the file, delete the tables you do not want, delete its `# AUTO-GENERATED DRAFT` line (the structure is now reviewed; the descriptions are step 8), and put it in place of `project_config\schema.yaml`. Copy only this file: the wizard of step 9 drafts the others better. The tables of one more schema can join it later with `python scripts/sync_schema.py --add-tables 'Schema.*'` (also with `--exclude-schemas`). *Done when:* `project_config\schema.yaml` lists your tables and no longer the template's.

7. **Check the structure**: `python scripts/sync_schema.py --check` (section 0.3.3 step 2). It only reads each source's tables, columns, types and keys (three `INFORMATION_SCHEMA` views per source; no row). *Done when:* it prints `CHECK OK: schema.yaml's structure matches the databases` (exit code 0). If it prints `CHECK FAILED` (a table or column changed after the draft), run `python scripts/sync_schema.py` without `--check`, read `project_config\schema.synced.yaml` (a diff against `schema.yaml` shows exactly what changed), copy `schema.yaml` to `schema.yaml.bak`, put the proposal in its place and check again. The report's section `== columns in schema.yaml that the database does not have: N ==` lists the columns that received the comment `# not in database (sync_schema.py)`, and `== tables in schema.yaml found in no data source: N ==` the tables marked `# not found in any data source`. Remove them with `python scripts/sync_schema.py --prune` (review the result and replace `schema.yaml` again), correct a misspelt column by hand, or, if the column exists but the login cannot see it, ask the DBA about a `DENY`. `--check` does not fail on marked columns, so read those sections: they should read `: 0 ==`. Review `relationships.proposed.yaml` too. Whatever the sync adds comes with the same name-only drafts, which step 8 describes.

8. **Draft the descriptions, review them, replace `schema.yaml`, delete the header** (section 0.3.4): `python scripts/draft_descriptions.py` (add `--language en` for English; the default is Persian, the language of the questions). For each table it asks the wizard's model one question about the names, types, keys and relationships in `schema.yaml` (no row is read or sent), and it writes `project_config\schema.described.yaml`: your file with every description that was empty, a placeholder, only a table's name or only `<type> column` drafted, every other line exactly as it was, each drafted line ending with `# REVIEW`, and a first line `# AUTO-GENERATED by draft_descriptions.py`. Read it and correct what is wrong, copy `schema.yaml` to `schema.yaml.bak`, put the file in its place and **delete the first line**: the preflight fails on that line until you do. If some tables failed, the summary lists them; `python scripts/draft_descriptions.py --resume` asks only about those. *Done when:* `schema.yaml` has no `AUTO-GENERATED` line and `python scripts/sync_schema.py --check` still prints `CHECK OK`.

9. **The wizard: entities, aliases, rules, examples** (section 0.3.1): `python setup_project.py --language fa`. The model is the application's (`OPENAI_BASE_URL` and `OPENAI_MODEL`) unless you give `--llm-base-url` and `--llm-model`. With the `datasources.yaml` of step 4 it needs no URL, and with one database it uses `DB_CONNECTION_URL`. It drafts `entities.yaml`, `aliases.yaml`, `business_rules.yaml` and `examples.yaml` (and `relationships.yaml`) into `project_config_draft/`, never into `project_config/`. Read the drafts and copy the files you accept into `project_config/`, deleting each file's `# AUTO-GENERATED by setup_project.py` line once you have reviewed it. *Done when:* the four files in `project_config/` are the reviewed ones. In the draft folder the wizard's validation step lists the six files it does not write and says `Setup is NOT complete`; that is expected. `Setup complete.` and the counts appear only when the folder it wrote to holds all ten files. **Then draft the scope values** (section 0.3.4.1): `python scripts/draft_policies.py`. The wizard writes neither `session_policy.yaml` nor `memory_policy.yaml`, so they still hold the template's made-up options. The command reads the distinct values of one small dimension column and writes both files with those values into `project_config_draft/`; read them and copy them into `project_config/`. *Done when:* the preflight check `Session and memory policies use real values` passes (step 12).

10. **Per-source settings** (*several databases only*): `description:` and `keywords:` so a question reaches the right source, and `nolock: true` only on the sources whose DBA requires `WITH (NOLOCK)`; this key is **per source** (section 0.3.3 step 6). *Done when:* the command of step 4 still prints the names.

11. **Issue the first admin key and create `api_keys.json`** (section 0.3.5; details in section 2.2): `python -m scripts.issue_api_key --id admin-1 --name "Admin" --full-admin`, copy `project_config\api_keys.example.json` to `project_config\api_keys.json`, put the printed entry in it in place of the sample one, and put `API_KEYS_FILE=project_config/api_keys.json` in `.env`. This step comes before the preflight because the `API key authentication` check fails while there is no key. *Done when:* you hold the raw key (it is printed once) and its entry is in the file. Each analyst's key comes in step 17.

12. **Preflight** (section 0.4): `python -m scripts.verify_deployment` with `VERIFY_API_KEY` set to the raw key of step 11. *Done when:* the last line is `N passed, 0 failed, N skipped`. The table in section 0.4 says what each line means and how to fix each `[FAIL]`; the check `project_config/ is curated, not a raw draft` is the one that holds steps 6 to 9 to account.

13. **The prompt budget** (section 0.3.3 step 5): `python scripts/prompt_budget.py` (it works with one database too), and put the `PROMPT_RETRIEVAL_TOKEN_BUDGET=` line it prints in `.env`. *Done when:* that line is in `.env`. Exit code 1 means a source does not fit in the model's context window, and the output says which.

14. **Start the server** (sections 2.3 and 2.4): `uvicorn api.server:app --port 8000 --no-server-header` from the repository root, and from `web/` the static file server `python -m http.server 8080`. *Done when:* in the log you see the provenance banner, `CORS allowed origins: ...` and `System prompt loaded (N chars)` (and, with several sources, one `Prompt path for data source '<name>'` line per source), and `curl http://localhost:8000/health` returns `ok` for `status`.

15. **Ask one real question and see that it was recorded:** one authenticated `POST /query`, then `Get-Content logs/audit_log.jsonl -Tail 1`. *Done when:* that line has today's `timestamp` and the `request_id` of the response (details: section 6 of `docs/deployment-runbook.md`).

16. **Run the preflight again** after the `.env` change of step 13, and after every later change to `.env` or `project_config/`. *Done when:* `0 failed` each time.

17. **Each analyst's key** (section 2.2): issue one key per person, add the entry to the file and restart the server (the file is read only at start-up). *Done when:* every analyst can open the UI and ask a question.

18. **The golden set** (section 5 of this guide): build it once real questions have accumulated in the audit log, and keep it for upgrades. *Done when:* `python -m eval.cli verify --golden eval_data/golden.jsonl --accept` has made the sound cases `active`.

---

## Part 0 — Prerequisites (once)

### 0.1 Installation

```powershell
cd <repo-root>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.lock
```

Install from `requirements.lock`, not from `requirements.txt`: the lock file pins the exact version of every package, and `sqlglot`, from whose syntax tree every decision of the SQL guard is made, is one of them; an unreviewed upgrade of it is the only dependency change that can alter security behaviour without any change in this repository. Run the same command again after every `git pull`.

### 0.2 The `.env` file

```powershell
copy .env.example .env
```

Then fill in these in `.env`:

```ini
DB_CONNECTION_URL=mssql+pyodbc://user@server:1433/DB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
DB_PASSWORD=<database password, exactly as it is>
OPENAI_BASE_URL=http://<llm-host>:<llm-port>/v1
OPENAI_MODEL=<model name on that same endpoint>
```

> **Write the database password "raw", not inside the URL.** The value of
> `DB_PASSWORD` is exactly the password the database itself knows, with no
> encoding: if your password is `p@ss/w:rd#1`, write exactly that. The
> application sets the password on the parsed URL and escapes any special
> character correctly by itself. The `URL` must contain only a username, not
> a password.
>
> If you do not set `DB_PASSWORD`, the URL is used as you wrote it; so you
> must encode the password inside it yourself (`@` becomes `%40`, `/` becomes
> `%2F`, `%` becomes `%25`). The application deliberately does not guess at a
> password written inside the URL and does not encode it again, because a
> password that contains `/ ? # :` or even a literal `%40` cannot be told
> apart from an encoded one, and guessing would silently break sound
> configurations. If you set both `DB_PASSWORD` and a password inside the
> URL, the server does not start, because it is unclear which is valid.
> With Windows authentication (`trusted_connection=yes` in the URL) neither a
> username nor a password is needed.

And **with a local model, usually leave this one empty**:

```ini
OPENAI_API_KEY=
```

> **With a local model, `OPENAI_API_KEY` almost always stays empty.** Most
> local OpenAI-compatible servers (LM Studio, Ollama, llama.cpp) do not check
> credentials at all, and `Settings.validate()` does not require it either —
> only `OPENAI_MODEL` and `DB_CONNECTION_URL` are required.
>
> That means that in a deployment with a local model, **in the whole system
> you have only one real token**: the analyst's key. The table in section
> 2.2.1 has three rows because it shows three *places*, not three secrets.
>
> Also, since 4.1.2, an empty key means the `Authorization` header is **not
> sent at all** — not an empty `Bearer `. That difference used to turn the
> LLM indicator red while the same endpoint answered the CLI.

**`DB_APPLICATION_NAME` (optional).** The name each connection to the data warehouse presents to SQL Server as its `program_name`, so that the DBA can tell this application's sessions apart from other clients; the default is `local-sql-agent`. It is added only to `mssql+pyodbc` connections and does not replace an `APP=` or `Application Name=` that the URL itself has. Each source in `datasources.yaml` can have its own name with `application_name:`. To see the application's sessions and whether they hold an open transaction, the DBA (or you, if you may read the DMVs) runs this query; if you have set a different name, put it in place of the default:

```sql
SELECT s.session_id, s.login_name, s.host_name, s.program_name, s.status,
       s.last_request_start_time, s.open_transaction_count
FROM sys.dm_exec_sessions AS s
WHERE s.program_name = N'local-sql-agent'
ORDER BY s.last_request_start_time DESC;
```

This is the first query of section 2a in `docs/dba/warehouse-load-diagnostics.sql` with fewer columns; section 2b there shows the running statements and 2c the blocking, and `docs/dba/README.md` says how to read them (`docs/db-hardening.md` also mentions this same `program_name`). Because the executor runs every query in a transaction that is always rolled back, an idle session whose `open_transaction_count` stays above zero deserves a question.

> **Since 4.5.0 an application database is needed too — but usually you have
> nothing to do.** API keys, admin roles and sessions live in a database from
> this version on, not only in `.env`. If you leave `APP_DB_URL` empty (the
> default), it creates a SQLite file at `logs/app.db` by itself and no
> setting is needed.
>
> If your organization has a DBA and a backup routine, you can supply a real
> database instead:
>
> ```ini
> APP_DB_URL=postgresql+psycopg://user:pass@host:5432/nlq_app
> ```
>
> The database must **already exist** — this application creates the tables
> inside it, but not the database itself.
>
> **This must never be the same database as the data warehouse.** The server
> checks at start-up and does not start if both point to the same place —
> because the warehouse is read with a read-only login and our own writes
> would silently destroy that property.

> **Watch for a port clash.** The default value of `OPENAI_BASE_URL` in
> `.env.example` is on `localhost:8000`, and this project's own backend also
> starts on `8000` by default. If your LLM is on the same machine and the same
> port, change one of the two, or the server does not start.

### 0.3 `project_config/` — the server will not start without it

This folder is your domain data and is deliberately not in git. Nine YAML
files must be in place:

```
project_config/
├── aliases.yaml
├── business_rules.yaml
├── entities.yaml
├── examples.yaml
├── memory_policy.yaml      ← new in 4.1.0
├── metrics.yaml
├── retrieval_hints.yaml
├── schema.yaml
└── session_policy.yaml
```

The absence of any one of them means a `ConfigNotFoundError` at start-up. **There is no silent fallback
to `project_config.example/`** — this is deliberate: running on the sample
rules produces SQL with false confidence, which is worse than not starting.

Besides these YAML files, one more text file must be here:
`system_prompt.md` (the language model's system instructions). It is no longer in
`prompts/system_prompt.md` — it has moved to this `project_config/` folder.
Its absence also stops the server from starting, in exactly the same way as a missing YAML file:

```
RuntimeError: System prompt not found: <the resolved path>
```

To create it, copy `project_config.example/system_prompt.md` and rewrite it for your
own real domain.

**If you have only one data warehouse** — that is, most deployments — you do not need `datasources.yaml` (the one database of `.env` is the source `default`) and you can skip section 0.3.3; draft the descriptions (0.3.4), issue the first key (0.3.5) and go to 0.4. The order of the "Install from scratch" list is the order that works: the databases first, then the files each tool drafts from them.

### 0.3.1 The setup wizard `setup_project.py`

A one-off, **optional** tool that, with the help of a language model, drafts four of the files above (plus `relationships.yaml`) from the live database. It writes **drafts** into `project_config_draft/` (ignored by git) and never into `project_config/` on its own: you read them and copy the files you accept across. It refuses `project_config/` and `project_config.example/` as an output folder unless you add `--overwrite-live`, and then it first copies every file it replaces to `<name>.bak-<stamp>`. Everything it writes must be reviewed: each of its files begins with `# AUTO-GENERATED by setup_project.py — review before use`. Run it from the repository root, after copying the template (section 0.3) and with `.env` filled in (it reads `.env` itself). With a `project_config/datasources.yaml` (section 0.3.3) it needs no URL: it describes every source in that file, each through the application's own login, or only those you name with `--datasource NAME`:

```powershell
python setup_project.py `
    --llm-base-url http://<llm-host>:<llm-port>/v1 `
    --llm-model <model name on that same endpoint> `
    --language fa
```

For a first install, before any configuration exists, give one database by URL:

```powershell
python setup_project.py `
    --db-url "mssql+pyodbc://reader:p%40ss@dbhost:1433/WarehouseDB?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes" `
    --llm-base-url http://<llm-host>:<llm-port>/v1 `
    --llm-model <model name on that same endpoint> `
    --language fa
```

If you leave an option out, it asks at each step. The options:

| Option | Meaning |
|---|---|
| `--db-url URL` | One database, read with the login in the URL: for a first install. Without it, and with a `datasources.yaml`, every source in that file is read through the application's own login. With neither: `DB_CONNECTION_URL`, then `DATABASE_URL`; if neither is set, interactive mode asks and `--non-interactive` stops. |
| `--datasource NAME` | Read only this source of `datasources.yaml`; repeat it for several (default: all). An unknown name, or a run with no `datasources.yaml`, stops with exit code 2; it cannot be combined with `--db-url`. |
| `--llm-provider openai\|mock` | `openai` (the default) is any OpenAI-compatible endpoint; `mock` calls no model and the aliases, rules and examples stay empty. |
| `--llm-model NAME` | The model. Default: `WIZARD_LLM_MODEL`, otherwise `OPENAI_MODEL`. |
| `--llm-base-url URL` | The endpoint address. Default: `WIZARD_LLM_BASE_URL`, otherwise `OPENAI_BASE_URL`. A remote endpoint is refused unless `LLM_ALLOW_REMOTE=true`. |
| `--language fa\|en\|both` | The language in which the analysts are asked; or `WIZARD_LANGUAGE`. Unset: interactive mode asks (default `en`) and non-interactive uses `en`. |
| `--output DIR` | Where to write the drafts (default `project_config_draft`). `project_config/` and `project_config.example/` are refused (exit code 2) unless `--overwrite-live` is given. |
| `--overwrite-live` | Allows `--output` to be one of those two folders. Each file the wizard is about to replace is first copied to `<name>.bak-<YYYYmmdd-HHMMSS>` next to it, and the copies are listed at the end. |
| `--fresh` | Does not merge into the files already in the output folder; writes them from this run alone (see below). |
| `--review interactive\|auto` | `auto` is the same as `--non-interactive`. |
| `--non-interactive` | Accepts all suggestions without asking; for scripts. |
| `--include-schemas a,b` | Only these database schemas. |
| `--exclude-schemas PATTERNS` | Comma-separated `fnmatch` patterns of schemas to leave out, compared without regard to case (`'*_Stg,Replay'`); those schemas are not read at all. Adds to the rules of section 0.3.3 for this run, and wins over `--include-schemas`. |
| `--include-staging` | Keeps the staging and change-capture tables that `STAGING_PATTERNS` names (section 0.4, the check `No staging or change-capture tables`); by default they are skipped. |
| `--dry-run` | Prints the files and writes nothing, not even the setup log; the validation step is skipped. |
| `--resume` | Continues from the first step that `.setup_log.json` has not recorded as finished (see below). |
| `--allow-repeated-answers` | Does not stop step 3 or 4 when 3 tables in a row get the same model answer (see below). Only for a genuinely small schema whose tables really are alike. |
| `--value-synonyms` | Also writes the `synonyms` of `aliases.yaml` from the values of label columns (name, title, kind, status, type) that hold at most 50 distinct values. Off by default: no synonym is built from a value read from a row, because a date, a phone number, a person's name or an address does not belong in a prompt or in a versioned file. Even with it, these are never taken: dates and times, numbers and codes, phone numbers, e-mail and IP addresses, URLs, addresses, masked values (`xxx`, `***`), error text, anything over 40 characters, any column named like personal or contact data (`FirstName`, `Family`, `Mobile`, `Phone`, `Address`, `Email`, `NationalCode`, ...) and the `Name` or `Title` of a table of people (`Customer`, `Employee`, `User`, ...); at most 20 per table. It reads up to 51 distinct values of each such column from the database. The trade-off: fewer words reach retrieval (the name of a status in a question no longer finds its table through a synonym) and a word people type has to be added by hand; in return no row value is in the prompt or in git. Review the result before you use it. |
| `--keep-existing-synonyms` | When the output folder already holds an `aliases.yaml`, keep the synonym keys of the earlier run that are values read from rows. By default the merge drops them (a date, a phone number, an IP address, a masked value, a long digit string, an address, a hash, an error message), counts them per shape in the summary and in `.setup_log.json`, and never lists them. |

The steps in order; each step is recorded in `<output>/.setup_log.json` under the key shown:

| Key | Step | What happens |
|---|---|---|
| `step1_connection` | Connection | With a `datasources.yaml`: opens each source through the application's own engine and runs `SELECT 1`; a source that cannot be reached stops the run (exit code 1). With a URL: opens it and runs `SELECT 1`; on failure it prints the error and, in interactive mode, offers to retry. |
| `step2_schema` | Schema discovery | In two phases, over all the selected sources together. First it lists schema and table names only, leaves out what the rules of section 0.3.3 and the staging tables exclude and, in interactive mode, shows each schema with the number of tables that would be read, asks which to leave out and offers to record the answer (below). Only then does it read the schemas that remain: tables, columns, foreign keys and up to five sample values for each text column, labelling each table fact or dim. In interactive mode it then asks which single tables to leave out. |
| `step3_aliases` | Aliases | One model call per table for aliases and a one-line description (the model is told the table's `Schema.Table` name). The list is cleaned: repeats (after folding case, Persian and Arabic letter forms, digits and punctuation) are dropped, and a list of more than 8 first loses every alias whose words are all in a shorter alias of the same table, then keeps the shortest 8; the summary counts the tables cleaned. Interactively you can accept, edit or delete (what you type stays as typed). A model that repeats itself until it is cut off at the token limit is not asked to "repair" its text: the complete aliases it wrote before the cut are kept and the step line says `salvaged N from a truncated answer`; when none is usable, it is asked once more with the same prompt and a stricter instruction, and a table that still gets nothing is named in the summary. |
| `step4_rules` | Business rules | For each fact table that has at least one numeric column that is not a key (not the primary key, not a foreign key or join column, not named `...ID`, `...Key` or `...Code`), one model call for the value column, the volume column and the rule text; the prompt lists those columns and only they can be named (a key the model names is dropped). A fact table with none gets no rule and the summary lists it. The prompt carries the table's join hints (declared foreign keys, or relationships inferred from column names). A table the name heuristic called a fact table but whose schema is named like a dimension (`ref_Dim`), or that holds only a key and descriptive text, is left out too, and the summary names it apart from the fact tables without a numeric column. |
| `step5_examples` | Examples | One model call for each data source (one for a single database): ten question-and-SQL pairs about the tables of that source (fact tables first, up to 20). An answer with no usable pair is asked once more with a smaller request. A pair needs a question and a read-only statement (`SELECT` or `WITH`); the others are counted and dropped. If no pair is usable the step and the summary say so (`WARNING: examples.yaml has no example from this run`) and the file begins with `# NO EXAMPLES`. |
| `step6_write` | Review and write | Merges this run into the files already in `--output` and writes `entities.yaml`, `aliases.yaml`, `business_rules.yaml`, `examples.yaml` and `relationships.yaml` there. Earlier entries for one table are made one before the merge, and the `synonyms` of `aliases.yaml` stay empty unless you give `--value-synonyms`. Interactively, each file is shown first: Accept, Edit in `$EDITOR`, Regenerate or Skip. At the end it prints, for each file, how many entries were kept, added and replaced. |
| `step7_validate` | Validation | Reads the four files that have loaders with the application's own validators and prints `OK`, `FAILED: ...` or `skipped` for each. If any of the ten required files of `project_config/` (section 0.3) is still missing from the output folder, it lists them with the `cp` command that copies each one's template from `project_config.example/` and says `Setup is NOT complete`; it copies nothing itself. Only when no file is missing and none reported an error does it print `Setup complete.` and the number of entities, rules and examples. |

**What it does not do:**

- **It does not write `schema.yaml`**, so it does not build the SQL guard's allowlist; for that use section 0.3.2 or write it by hand. It also does not write `metrics.yaml`, `retrieval_hints.yaml`, the two policy files, `system_prompt.md`, `.env` or any API key. It edits `datasources.yaml` only when you ask it to record the schemas you left out: when, interactively, you name schemas to leave out, it offers to add them as that source's `exclude_schemas:` (the default answer is No, and it never asks with `--non-interactive` or `--dry-run`). On Yes it edits the file as text, so your comments stay, checks the result with the server's own validation, and only then saves a copy as `datasources.yaml.bak-<stamp>` and replaces the file; if the edit cannot be made safely it leaves the file alone and prints the lines to add by hand. Without a `datasources.yaml` (or with `--db-url`) it prints an `EXCLUDE_SCHEMAS=` line to put in `.env`, and never edits `.env`.
- **It does not assign `datasource:` to the tables of `schema.yaml`.** With several sources configured it writes, in each `entities.yaml` entry, the sources the table was found in (`datasource: [sales, reports]`); the `datasource:` lines of `schema.yaml` are the work of `python scripts/sync_schema.py` (section 0.3.3).
- **A bare URL must carry its own password.** `--db-url`, `DB_CONNECTION_URL` and `DATABASE_URL` are read as they are and `DB_PASSWORD` is not applied, so the password goes inside the URL, encoded (`@` becomes `%40`). Passing it with `--db-url` leaves it in the shell history; the `DATABASE_URL` environment variable does not have this problem. The sources of `datasources.yaml` need none of this: their passwords come from the variables that file names (`password_env`), exactly as for the server.
- **Its model settings follow the application's settings.** For each of the endpoint, the model and the key it takes, in this order: the command-line option, then `WIZARD_LLM_BASE_URL` / `WIZARD_LLM_MODEL`, then `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_API_KEY` (an empty `WIZARD_*` value means unset; `.env.example` leaves both empty). It prints one line saying which provider, model and endpoint were chosen and the source of each (never the key). `WIZARD_LLM_PROVIDER` (`openai` or `mock`) and `WIZARD_LANGUAGE` are separate settings. A local endpoint needs no key. If the endpoint does not answer, it says so loudly and carries on with the mock provider, which means the aliases, rules and examples come out empty.
- **It respects `LLM_ALLOW_REMOTE`.** It sends the model the names of tables and columns, sample values from the data warehouse and a summary of the schema. A non-local endpoint (the application's own definition: `LLM_TRUSTED`, otherwise a loopback host, a private network and `*.local` are local) is refused before anything touches the database or the endpoint, unless `LLM_ALLOW_REMOTE=true`. To run without a model, use `--llm-provider mock`.

**Know before you run:**

- **It writes drafts, not your configuration.** The default output folder is `project_config_draft/`. Pointing `--output` at `project_config/` or `project_config.example/` stops the run before the database or the model is touched; with `--overwrite-live` it writes there after copying each file it replaces to `<name>.bak-<stamp>`. (Earlier versions wrote into `project_config/` by default and replaced the files without a backup.) The usual flow is: run the wizard, read the drafts, copy the files you accept into `project_config/`; the summary at the end says so.
- **It merges, run after run.** For two databases you run it twice, and the second run must not lose the first one's entries, so when the output folder already holds wizard files the new entries are merged by key: entries for tables this run did not cover stay, an entry for a table of this run replaces the old one with the same key, and nothing is removed. A table the model gave no answer for keeps its earlier entry, and the summary names it. Two earlier entries for one table (a bare one and a `Schema.Table` one, say) become one with the aliases of both, and the summary lists them (`Merged 1 duplicate entry of one table in entities.yaml`). The summary gives `kept N, added N, replaced N` for each file. To start from nothing, use `--fresh`; a wizard file that cannot be read stops the run before any model call (exit code 2) until you fix or move it, or use `--fresh`. The full rules are in section 2.2 of `docs/deployment-runbook.md`.
- **Keys are `Schema.Table` when a name is shared.** An entry is keyed by the bare table name, or by `Schema.Table` when several schemas (of one source or of several) share the name; this is the inspector's rule (`docs/design/TABLE-NAMES.md`). If a name that was unique in the earlier run is now shared, the earlier entry is re-keyed (`Trade` becomes `sales.Trade`) in all five files and the summary lists it. The keys are exactly the ones `schema.yaml` gets from the inspector and `sync_schema.py`: one function, `schema_data.registry.table_key_for`, spells them all (including `[Price List]` for a name with a space), so an entity key always names a table of `schema.yaml`. An earlier entry is the same table when its key is the table's bare name or `Schema.Table`; its schema is read from `schema_name:`, from the inspector's `schema:`, from its key or from the only table of that name this run found, so an inspector draft of the same tables is merged into and not added to.
- **No value from a row goes into `aliases.yaml` unless you ask.** The wizard of 6.13 and 6.14 turned the sample values of every text column into `synonyms` keys: hundreds of dates, phone numbers, national ids, addresses, IP addresses, partly masked names and error messages, in the prompt and in a versioned file. It now builds none; `--value-synonyms` brings back the safe part of it (the option's row above has the rules and the trade-off). The setup log records only counts, never a value. The same rules decide which sample values the model sees in step 3: nothing from a column named like personal or contact data, and no date, number, phone, address, masked value or error text from the others; short statuses and kinds stay. A re-run also drops the synonym keys of an earlier `aliases.yaml` that are values read from rows, with a count per shape in the summary; `--keep-existing-synonyms` keeps them.
- **A table's aliases go in `entities.yaml`, never in `ring_aliases` (6.16.3).** A key of `ring_aliases` is the exact value the ring dimension's name column stores (the name of a hall), and the question's hall becomes the filter `WHERE r.[Name] = N'<key>'`. The wizard of 6.13 to 6.16.2 wrote one entry per table there, so a question that named a currency or a broker filtered the ring by `N'Currency'`, and a question about the halls by `N'Ring'`, and came back empty. The wizard now writes none, and when it merges into an earlier `aliases.yaml` it drops the entries whose key names a table (the summary and `.setup_log.json` give the count and the keys), keeping their aliases in the table's entity. Entries that are ring names are not touched, and the wizard drafts none: write them or run `python scripts/draft_policies.py --ring-aliases`. The application ignores a table-keyed entry when it loads the file, and the preflight check `Aliases are curated` fails on it.
- **If you ran the wizard of 6.13 or 6.14, check what it wrote.** Run `python -m scripts.verify_deployment` (section 0.4) on the folder you deployed: `Entities match schema keys` lists the duplicate and the qualified entities, `Aliases hold no raw data values` counts the synonyms copied from rows, and `project_config/ is curated, not a raw draft` fails on a file that still has the wizard's header line. Run the wizard again over the same output folder: it merges the duplicates into one entity per table and drops the synonym keys that are values read from rows.
- **Left-out schemas and staging tables are never read.** The wizard lists names first and reads columns and sample values only from the schemas that remain, so the question is asked, and `--exclude-schemas` applied, before any table of those schemas is touched. Staging and change-capture tables are skipped unless `--include-staging`, and a relationship to a skipped table is dropped. The summary says how many tables were skipped and under which rule (`Skipped 66 of 278 tables: ...`).
- **Relationships are inferred when the database declares few keys.** With fewer foreign keys than one per ten tables, `relationships.yaml` also gets relationships guessed from column names (`Order.CustomerID` pointing at `Customer`'s key). Each guess has `basis` and `confidence` fields and a `# REVIEW` line above it: check it or delete it. The server does not read `relationships.yaml`, so nothing here widens what generated SQL may touch.
- **It stops when the model repeats itself.** Steps 3 and 4 ask one question per table. A gateway with a response cache in front of the model can answer all of them with the first answer, and nothing in the reply shows it: one alias list for most of a database's tables, and rules such as `Value column: X. Volume column: X.` on a table where that makes no sense. The wizard compares the model's parsed answers as they arrive (the alias list in step 3; the rule text, with the table names removed, in step 4; an empty answer does not count). When **3 tables in a row** get the same answer it stops that step with the message `Step 3 stopped: the model endpoint returned the identical alias list for 3 different tables in a row (...)`, writes no file, ends with exit code 3 and leaves the run resumable: the steps recorded in `.setup_log.json` stay. After the fix (turn off response caching for this client at the gateway, or point `OPENAI_BASE_URL` / `WIZARD_LLM_BASE_URL` straight at the model server; `python scripts/verify_deployment.py` confirms it with the check `Model endpoint answers each prompt itself`) continue with `--resume`. Each of these two steps ends with a line such as `183 distinct alias lists for 183 tables`; a number far below the table count is the same warning after the fact. `--allow-repeated-answers` turns the stop into a warning, for a schema so small that identical answers are to be expected. Step 5 asks one question for each data source and has nothing to compare.
- **`--resume` continues; it does not start over.** It runs again from the first step that `.setup_log.json` has not recorded as finished. Steps 1 and 2 always run again, because the connection string is never stored. The aliases, rules and examples of the model steps that finished are not regenerated, provided the schema and the language have not changed (the mock provider's result is never reused, and neither is an empty list of examples: that step asks the model again); a file that already exists is not overwritten, except an `examples.yaml` that holds no example. When steps 1 to 6 are recorded and all the files are present, only validation runs, with no database and no model. A run without `--resume` starts a fresh log. The schema's fingerprint is taken after the exclusions and includes the table keys, the sources each table was found in and the names of the sources read, so changing `--exclude-schemas`, `--include-staging` or the `--datasource` selection generates them again.
- **In the draft folder a list of missing files is expected.** Step 7 validates the files the wizard wrote and lists the required files still missing from that folder (section 0.3), instead of ending in a `ConfigNotFoundError`. A draft folder holds only the wizard's own files, so the list there is the six it never writes (`schema.yaml`, `metrics.yaml`, `retrieval_hints.yaml`, `session_policy.yaml`, `memory_policy.yaml` and `system_prompt.md`) and step 7 ends with `Setup is NOT complete`: that is normal there. `Setup complete.` is printed only when the folder holds all ten files.
- **`.setup_log.json` keeps the connection string with the password masked (since 6.8.0).** A log written by an older version holds the string exactly as typed, password included: delete it, and if the file was shared or committed, change the password (section 6). The `# Source:` comment in `entities.yaml` and the driver errors on a failed connection are masked too.
- **In the review menu, Regenerate asks the model again.** For `business_rules.yaml` and `examples.yaml` it re-runs their own step; for `entities.yaml` and `aliases.yaml`, which are both built from step 3's aliases, it re-runs step 3 without the per-table questions. An error is reported and the current version stays. For `relationships.yaml` (built from the schema) it is not offered, nor is it offered without a model.

### 0.3.2 Drafting `schema.yaml`: `database.schema_inspector_cli`

```powershell
python -m database.schema_inspector_cli `
    --output-dir project_config_draft `
    --include-schemas sales,ref `
    --exclude-schemas "*_Stg,Replay,Alerts"
```

Without `--db-url` the inspector reads `project_config/datasources.yaml` (section 0.3.3) and inspects **every source through the application's own login**, the engine the server uses, so the draft lists only the tables the application can query. Without that file it uses the one database of `DB_CONNECTION_URL`, with `DB_PASSWORD` applied as at start-up. This matters: a draft made under a wider login (a Windows administrator, say) listed about 150 change-capture, temporary and staging tables that the application cannot even query, and they ended up in the live `schema.yaml`. `--datasource NAME` (repeatable) narrows the run to some of the sources. `--db-url` is for a first install, before any configuration exists: one database, read with the login in the URL (the password percent-encoded inside it, as for the wizard), and the tool prints a line saying the application's own login may see less. The inspector no longer reads the `DATABASE_URL` environment variable.

Which schemas it leaves out is the shared rule of section 0.3.3 (a deployment with one database has it too, through `EXCLUDE_SCHEMAS`): the system schemas, `EXCLUDE_SCHEMAS`, the source's `exclude_schemas:` and, for this run only, `--exclude-schemas`. That option is a comma-separated list of `fnmatch` patterns compared with the schema name regardless of case (`*_Stg` leaves out both `Sales_Stg` and `sales_stg`; a plain name leaves out only that schema), and if `--include-schemas` is given as well, exclusion wins. Use it for a warehouse with many staging, replay and experimental schemas: every table in `schema.yaml` costs prompt tokens. Staging and change-capture tables (the ones `STAGING_PATTERNS` names, section 0.4) are skipped too, unless you give `--include-staging`.

The other options: `--exclude-tables a,b`, `--no-row-counts` (no `COUNT(*)` per table), `--dry-run` (prints instead of writing), and `--sample-values` with `--sample-rows N` (below). It writes four files, `schema.yaml`, `entities.yaml`, `aliases.yaml` and `relationships.yaml`, into the draft folder, never into `project_config/` or `project_config.example/` (it refuses these two names with exit code 2), and prints `Done.` at the end. The draft folder is ignored by git.

**No value from a row goes into the drafts.** Nothing in the application reads sample values from `schema.yaml`, and the `e.g. a, b, c` text that older drafts put in column descriptions went straight into the model's prompt, so `schema.yaml`, `entities.yaml` and `aliases.yaml` no longer carry any. `--sample-values` (with `--sample-rows N`, default 10) reads distinct values of text columns and writes them to `sample_values.review.yaml` in the draft folder, readable by its owner only; nothing loads that file. Use it to write real column descriptions, then delete it. Without `--sample-values` no row is read at all, only a `COUNT(*)` per table.

At the end of the run it prints on stderr the number of tables per schema, how many tables each skip rule left out, the number of relationships (declared and inferred), a reminder that the drafts are for the first install only, and, if more than 150 tables were written, a warning (`WARNING`); in that case narrow the run with `--include-schemas` or `--exclude-schemas` and then measure the prompt size with `python scripts/prompt_budget.py`.

The draft has the format `sync_schema.py` writes (section 0.3.3), because it is built by the same code. With several sources every table has a `datasource:` line (none with one source), and a table found with the same `schema.table` in several sources is written once, with `datasource: [sales, reports]`; `entities.yaml` gets the same line. Each table of the draft `schema.yaml` has `db_schema`. If the table name is unique across the whole run, that plain name is the key; if a name appears in more than one schema (`Trade` in `sales` and in `ref`), both occurrences are keyed as `Schema.Table` (`sales.Trade`, `ref.Trade`), and the `relationships:` entries and the entity names in `entities.yaml` are written the same way (`docs/design/TABLE-NAMES.md`). Relationships come from the foreign keys the database declares and, where none is declared but a column is named like a link (`Order.Customer_ID`), from a guess marked `# INFERRED from column names (not a declared foreign key) -- REVIEW before use`: check each one or delete it. A file that repeats a key is rejected at load time, and the error message lists all the duplicate keys together.

**The drafts are for the first install only.** They describe every table they find afresh, so they cannot be merged into a `schema.yaml` you have already refined. A draft is a starting point, not the `schema.yaml` you deploy: every table is described by its own name and every column by its type, and the files carry the `# AUTO-GENERATED DRAFT` header that the preflight refuses (section 0.4) until you delete it after review. Review the structure (which tables stay), put `schema.yaml` in `project_config/`, and let `scripts/draft_descriptions.py` draft the descriptions for you to review (section 0.3.4) instead of writing them all by hand. If you used `--sample-values`, treat the draft folder as sensitive until you have deleted the review file. After editing, run `tests/test_schema_registry_snapshot.py` and run `python -m scripts.verify_deployment` (section 0.4) to check that it loads.

An installation that is already deployed does not draft again. Add the tables of a new schema with `python scripts/sync_schema.py --add-tables 'Schema.*'` (section 0.3.3 and runbook section 16.3): it keeps your refined text and brings in only what changed in the databases.

### 0.3.3 If you need to query more than one database

For example, two databases on one server, or a separate SQL Server for the archive. In that case a tenth, optional file is added to `project_config/`: `datasources.yaml`. This file **describes the connection itself** — server address, port, database name, driver and username — and only the password stays outside it, in `.env`. Because this file is versioned and reviewed like `schema.yaml`, the password never goes into it; only the *name* of the variable that holds the password does. The step-by-step details, with commands, are in section 16 of `docs/deployment-runbook.md` and the reason for the design is in `docs/design/DATASOURCES.md`; the short path is this:

1. **Describe the sources.** Copy `project_config.example/datasources.example.yaml` to `project_config/datasources.yaml`. Example: two databases on one server, as two sources. (The Persian values are sample data: `انبار فروش` is "Sales warehouse" and `انبار موجودی` is "Inventory warehouse"; the keywords `فروش`, `موجودی` and `انبار` mean "sales", "stock" and "warehouse", and stay in Persian because they are matched against Persian questions.)

```yaml
default: sales
datasources:
  sales:
    description: انبار فروش
    keywords: [revenue, invoice, فروش]
    host: 10.0.0.5
    database: SalesDW
    username: nlq_reader
    password_env: DB_PASSWORD_SALES
    exclude_schemas: ["*_Stg", Replay]   # optional: schemas the setup tools never scan
    options:
      TrustServerCertificate: true
  inventory:
    description: انبار موجودی
    keywords: [stock level, reorder, موجودی, انبار]
    host: 10.0.0.5
    database: InventoryDW
    username: nlq_reader
    password_env: DB_PASSWORD_INVENTORY
```

And in `.env` only the passwords, **raw and with no encoding at all**:

```ini
DB_PASSWORD_SALES=p@ss/w:rd#1
DB_PASSWORD_INVENTORY=another-password
```

The default port is 1433 and the default driver is `ODBC Driver 18 for SQL Server`. For Windows authentication write `trusted_connection: true` instead of a username and password. If `datasources.yaml` exists, `DB_CONNECTION_URL` and `DB_PASSWORD` are not used. Two databases on one server, as two sources, is a perfectly valid configuration: each source has its own connection pool and login, and each query runs on only one of them. If questions must join the tables of **both** databases with `JOIN`, create a single source and give the tables of the second database a multi-part `db_schema` in `schema.yaml` (for example `db_schema: "InventoryDW.dbo"`). Sources written for 6.1 and 6.2 (`url_env`) still work.

**Schemas the setup tools leave out.** The wizard, the inspector and `sync_schema.py` share one rule for the schemas of a database they do not scan, and a deployment with one database has it too. A schema is left out when it matches any of these, listed in the order the reports count them under: the system schemas, always (`sys`, `INFORMATION_SCHEMA`, `guest`, `cdc` and the fixed `db_*` role schemas); `EXCLUDE_SCHEMAS` in `.env` (for example `EXCLUDE_SCHEMAS=*_Stg,Replay`), for every source; the `exclude_schemas:` of that source in `datasources.yaml`; and `--exclude-schemas` on the command line, for one run (the wizard also asks, interactively). Patterns are `fnmatch`, compared without regard to case. The rules add up: none of them can bring back a schema another one left out, and exclusion wins over `--include-schemas`. Record a rule you want kept in `.env` or `datasources.yaml` rather than in a flag you must remember each time. It governs only what a tool *proposes* from the databases: a table `schema.yaml` already lists is never hidden by it (the preflight check `No table in an excluded schema`, section 0.4, tells you about one that sits in an excluded schema), and nothing reads it when questions are answered. The detail is in section 2.3 of `docs/deployment-runbook.md`.

**Single tables, and a staging schema you want to scan.** `exclude_tables:` in `datasources.yaml` (or `EXCLUDE_TABLES=` in `.env`, for every source) lists `fnmatch` patterns compared, without regard to case, with `schema.table`: `X_Fact.Order` is one table, `X_Stg.*_Delete` the delete tables of one schema, `*.Order` a table of that name in any schema (a pattern with no `.` is compared with the table name alone). Tools apply it exactly where they apply `exclude_schemas:`, and count what it skipped as `configured table exclusion`. `STAGING_PATTERNS` drops a staging schema and its change-capture tables together; to scan a staging schema and still leave out its change-capture tables, name it in the source's `keep_staging_schemas:` and list the tables in `exclude_tables:`:

```yaml
datasources:
  main:
    exclude_schemas: [Meta, Audit_Stg, Old, dbo]
    keep_staging_schemas: [X_Stg]
    exclude_tables: ["X_Stg.*_Insert", "X_Stg.*_Update", "X_Stg.*_Delete", "X_Fact.Order"]
```

The default `STAGING_PATTERNS` know `_Inserts`, `_Updates` and `_Deletes` only; for the singular names use `exclude_tables:` as above, or add `table:*_Insert,table:*_Update,table:*_Delete` to `STAGING_PATTERNS` in `.env`. `exclude_schemas:` always wins over `keep_staging_schemas:`. A table `schema.yaml` already lists is never hidden by any of these.

2. **Sync `schema.yaml` with the databases and write each table's `datasource:`.** A table without this key runs on the default source; a table that has the same structure in several sources (for example a repeated date table) takes a list: `datasource: [sales, inventory]` (the names spelled exactly as in `datasources.yaml`). `schema.yaml` also falls behind the database in other ways: a new column, a removed column, a type that changed. Do not hunt for them by hand; run this once from the project root:

```powershell
python scripts/sync_schema.py
```

It only reads each source's structure (three `INFORMATION_SCHEMA` queries per source: tables, views, columns, data type, nullable, and the primary and foreign keys; no row is read and nothing is written to the database) and creates `schema.synced.yaml` next to `schema.yaml`; it never changes `schema.yaml` itself, and `--output PATH` gives another path. The proposal is your same file with all its comments, descriptions, flags and order, and only these structural changes (the script parses and compares its own output and does not write it if anything else differs):

| Situation | What the proposal does |
|---|---|
| The table has no `datasource:`, or a wrong one (several sources only) | Writes it: `datasource: sales`, or `[sales, inventory]` for a table in both, in the order of `datasources.yaml` |
| The column is in the database and not in `schema.yaml` | Adds it at the end of the table's `columns:`: `Name: "<type> column"  # TO BE FILLED (added by sync_schema.py)` and records its type |
| The column is in `schema.yaml` and in no source | Keeps it and puts `# not in database (sync_schema.py)` above it; `--prune` removes it instead (not a column that is still in `resolvable_columns` or `prefetchable_columns`) |
| The recorded type differs from the database | Fixes the entry in the table's `column_types:` map and reports `Table.Column: old -> new`. Types live in this map, never in the column description |
| The table is in the database and not in `schema.yaml` | Only reports it; `--add-tables 'sales.*'` (a glob pattern on `schema.table`, repeatable) adds the matching tables with a draft description. A table in a schema that is left out (above) is neither reported nor added; the report counts those per schema, and `--exclude-schemas PATTERNS` adds patterns for one run |
| The table is in `schema.yaml` and in no source | Keeps it with `# not found in any data source`; `--prune` removes it (unless `relationships:` still names it) |
| A table without a `columns:` key | Left alone (described only, deliberately not queryable) |

`column_types:` is a new optional map for each table, `{column: SQL type}`, which only this script writes; nothing reads it at run time, so the prompt and the guard's allowlist do not change.

`column_units:` is another optional map for each table, `{column: rial | none}`, which you write by hand. It says whether a number in a column is money, and it only changes the wording of the answer summary: the summary puts Rial/ریال on money and on nothing else (not a count, a quantity, a volume, a percentage, an id, a code or a date). A column you do not list is judged by its name (`price`, `value`, `amount`, `cost`, `fee`, `ارزش`, `قیمت`, `مبلغ`, `کارمزد` are money; `count`, `quantity`, `volume`, `weight`, `percent`, `id`, `code`, `date`, `حجم`, `تعداد`, `درصد`, `کد` are not), and when the name says nothing the model decides. List only the columns whose name misleads. Keys must be columns of the table's `columns:`; write `none`, not `no` (YAML reads a bare `no` as false). The sync keeps the map, and `--prune` removes the entry of a column it removes.

It also writes `relationships.proposed.yaml` next to `schema.yaml`: every foreign key defined between two tables of `schema.yaml`, and the relationships it guesses from column names (`Order.CustomerID` to `Customer.ID`, `Order.OrderDate_ID` to `Date.ID`; the same schema is preferred, the two tables must share a source, and a guess is dropped when the target has a composite key, no key, a different column type, or several candidates). Each entry has `basis:` (a constraint or a naming rule) and `confidence:`. Nothing reads this file and `relationships.yaml` is never touched: review each entry and copy the correct ones by hand. `--no-relationships` does not create it.

`--dry-run` only prints the report. A column that the read-only login cannot see through `INFORMATION_SCHEMA` is also reported as "missing", so before `--prune` ask the DBA about a `DENY` (in `docs/db-hardening.md`).

If anything changes, a `== prompt size ==` section comes at the end of the report: for each source, the estimated tokens of the static prefix for the current `schema.yaml` and for the proposal, their difference, the path each takes (static prefix or retrieval, based on `PROMPT_RETRIEVAL_TOKEN_BUDGET`), and how many of the added columns and tables still carry the draft description `TO BE FILLED`. Every added column and table is text the model reads on every request, so this is the cost of the proposal before you change `schema.yaml`. Both prefixes are built by the same code the server and `prompt_budget.py` use, so it is the same estimate (`len(text) // 4`), not the model's real tokens. A source whose proposal would move it from the static prefix to retrieval gets a `WARNING:` line with both sizes and the budget: review the draft columns, or raise the budget after running `python scripts/prompt_budget.py`. This is advice only; no exit code changes and `--check` is not affected. Without `system_prompt.md` this section is one line saying it could not be computed.

Then review `schema.synced.yaml` (the "not found" tables, and every `[A, B]`, since a list means the table has the same structure in both sources) and put it in place of `schema.yaml`, but first back up the current file:

```powershell
Copy-Item project_config\schema.yaml project_config\schema.yaml.bak
Move-Item -Force project_config\schema.synced.yaml project_config\schema.yaml
```

`schema.yaml` is the guard's allowlist, so any change to it takes effect when the server is restarted. A column that stays in `schema.yaml` and is not in the database is still allowed by the guard, so a question that uses it fails at run time; remove every `# not in database` column (or run `--prune`), fix its spelling or ask the DBA, and write real descriptions for the `# TO BE FILLED` columns and tables (until then the model sees that same `<type> column`). Finally, `python scripts/sync_schema.py --check` writes nothing; if `schema.yaml` has fallen behind the databases (a column the database has and the file lacks, a recorded type that differs, a column or table the database has lost, or a `datasource:` line that should be set) it prints `CHECK FAILED: ...` with the counts and ends with code 1, which suits a deployment pipeline, and in the healthy case it prints `CHECK OK: schema.yaml's structure matches the databases` and code 0. A type that is not yet recorded is not falling behind: a `schema.yaml` written before 6.9.0 lacks the `column_types:` map, so since 6.9.2 this same `--check` ends with code 0 and with the note `N column type(s) not recorded yet -- run python scripts/sync_schema.py once to record them ...` at the end of the `CHECK OK` line, until an ordinary run writes the types into `schema.synced.yaml` and you replace `schema.yaml` with it. A column marked `# not in database` does not fail it (it appears in every report until it is removed); `--check --prune` fails while anything is left to remove, and `--check --add-tables PATTERN` fails until the matching tables have been brought into the file. Code 2 means a source's catalogue could not be read, `schema.yaml` is invalid or has a layout that cannot be edited line by line (a flow-style table or `columns: {...}`), or the output was not valid. Running it again on the replaced file changes nothing.

**`assign_datasources.py` still exists and is the narrower tool.** It only writes the `datasource:` lines (`schema.with_datasources.yaml`) and reports missing columns; it adds no columns, records no types and marks nothing, and it shares the matching and editing of `datasource:` with `sync_schema.py` (the same rule, the same code). Use it when you want exactly this and no other change to `schema.yaml`. Whatever it does, `sync_schema.py` does too, so it is not the recommended first step and stays so that existing pipelines keep working.

3. **Routing questions.** With several sources, each question is routed to **one** source before the prompt is built, and the model sees only the tables of that source (plus shared tables). The `description:` is printed above the source's tables in the prompt, and `keywords:` is a list of Persian or English words or phrases. A keyword is compared with the question as a "whole word" after normalising Arabic/Persian letters, digits, the half-space (zero-width non-joiner) and letter case: `stock` is not found inside `stockholder`, and a plural or prefixed form is a different word, so write every form you expect. If no keyword is found, the choice is made by the continuation of the conversation, then by the tables the retrieval layer finds, and finally by the default source; if the model says `OUT_OF_SCOPE`, the question is retried **once** with the next source (the CLI does not retry).

4. **Preflight and restart.** `python -m scripts.verify_deployment` (section 0.4) runs the database checks once for each source and has two checks specific to several sources: "`Tables map to data sources`" and "`Tables are in their data source`", which for a table whose columns are not all in the assigned source but are in another source writes a line like `stock_dim.Broker: not in sales, found in inventory — set datasource: inventory`. If you have never run `sync_schema.py` (or `assign_datasources.py`), this same check fails with a line like `[FAIL] Tables are in their data source -- 44 table(s): ... not in sales, found in inventory — set datasource: inventory`; the fix is to run that script and replace `schema.yaml` (step 2 above). The check `Schema structure matches the databases` is the same test as `sync_schema.py --check` and fails if `schema.yaml`'s structure has fallen behind the databases; a type that is not yet recorded does not count as falling behind and only gets a note. The table of all the checks is in section 0.4. Then restart the server and look in the start-up log for one `Prompt path for data source '<name>'` line per source.

5. **The size of the prompt budget.** `PROMPT_RETRIEVAL_TOKEN_BUDGET` is measured against each source's prompt separately, not against their total, and the estimator (`len // 4`) undercounts Persian text by about 15%. Instead of guessing, run this from the project root:

```powershell
python scripts/prompt_budget.py
```

It prints each source's prefix size, asks the model for the real token count, compares it with the model's context length, and prints the `PROMPT_RETRIEVAL_TOKEN_BUDGET=` line that you should put in `.env`. With `--no-model` it does not connect to the model (then give `--context-length`); counting real tokens warms the model server's prefix cache, so run it before opening the service to users. Exit code 1 means a source does not fit in the context window and must stay on the retrieval path.

6. **`NOLOCK`, only if the DBA requires it.** In `datasources.yaml`, write `nolock: true` for that same source (in both the structured form and `url_env`; the default is `false`). Just before each statement is sent to that source, the executor adds ` WITH (NOLOCK)` after every reference to a real table (after the alias) and touches nothing else in the text; `generated_sql` in the audit log stays the approved SQL without the hint. Because the table hint is T-SQL specific, the server does not start with an `SQL_DIALECT` other than `tsql`. **`NOLOCK` means dirty reads**: uncommitted rows may be seen, and sometimes a row is seen twice or not at all. It is the operator's decision and is taken for each source separately: **`nolock: true` on one source does not reach the others.** The flag is read from the definition of the very source the statement is routed to; a source that says nothing is `false` and inherits nothing from the default source. An example with two sources where only the first has the hint:

```yaml
default: sales
datasources:
  sales:
    host: 10.0.0.5
    database: SalesDW
    username: nlq_reader
    password_env: DB_PASSWORD_SALES
    nolock: true                    # statements routed to sales get WITH (NOLOCK) after every table
  inventory:
    host: 10.0.0.6
    database: InventoryDW
    username: nlq_reader
    password_env: DB_PASSWORD_INVENTORY
                                    # no nolock: statements routed here go as they were approved
```

If the DBA of the second server requires it too, put `nolock: true` under `inventory` as well. The source a statement ran on is in the `datasource` field of the audit record. Without `datasources.yaml` this hint cannot be used at all, because the single implicit source always reads as `false`: describe the database in a `datasources.yaml` with one source (then `default` is optional). The exact list of what receives the hint and what does not is in section 16.7 of `docs/deployment-runbook.md`.

7. **Read the admin panel** (section 2.5.2): the cards «انحراف شِما» ("Schema drift") and «تازگی واژگان ابعاد» ("Dimension vocabulary freshness") say that a table is in the wrong source or that a dimension's vocabulary has gone stale.

### 0.3.4 Drafting the descriptions: `scripts/draft_descriptions.py`

The preflight refuses a `schema.yaml` whose descriptions only repeat a table's name (`Sales_Fact.Trade`) or a column's type (`bigint column`): such a description says nothing the key did not say, and table retrieval matches the words of a question against the words of the descriptions. That is exactly what the inspector and `sync_schema.py --add-tables` write, so on a first install, where there are no curated files to restore, the descriptions have to be written. This command drafts them with the same model the wizard uses and leaves the review to you. It is a separate command next to `sync_schema.py`, with the same pattern: it proposes a file next to `schema.yaml` and never overwrites `schema.yaml`.

```powershell
python scripts/draft_descriptions.py
```

- **What it drafts.** Every table description that is empty, the inspector's placeholder or only the table's name, and every column description that is empty or only `<type> column`. These are the tests of the preflight itself, so what it drafts is what the preflight objects to. A description a person wrote is never changed, and every key, flag, comment and the order of the file stay as they are: the script parses its own output and refuses to write a file that differs from the original in anything but the drafted descriptions.
- **What the model is told.** One question per table: the table key, its schema and data source, the column names and types, the notes already written, the relationships that name the table (`schema.yaml`, `relationships.yaml` and the declared foreign keys of `relationships.proposed.yaml`) and its aliases in `entities.yaml`. **No row value is read or sent**, unless you add `--samples`: then up to five distinct values of each label column (a column named name, title, kind, status, type, category, class, label, caption or group, with at most 50 distinct values) are read from the database and shown, under the wizard's rules: a column named like personal or contact data is never read, and no date, number, code, phone number, address, masked value or error text is shown. The model is told to describe only what the names, types and keys show, to invent no business fact and to end a guess with `(?)`.
- **The model and its rules are the wizard's.** The endpoint comes from `--llm-base-url`, `--llm-model` and `--llm-provider`, then `WIZARD_LLM_*`, then `OPENAI_BASE_URL`, `OPENAI_MODEL` and `OPENAI_API_KEY`; one line names the model and the endpoint (never the key). An endpoint that is not local is refused unless `LLM_ALLOW_REMOTE=true`, as for the application and the wizard. Unlike the wizard it does not fall back to a mock when the endpoint does not answer: a mock drafts nothing, so it stops with an error.
- **The language.** `--language fa` (the default) or `--language en`. A table is described in one sentence (what one row is), a column in a few words.
- **The file it writes.** `schema.described.yaml` next to the input (`--schema` and `--output` change the paths). Each drafted line ends with `# REVIEW`; a `# TO BE FILLED` note on it is answered and goes. The first line is `# AUTO-GENERATED by draft_descriptions.py`. While that line is there, the preflight check `project_config/ is curated, not a raw draft` fails on the file, whatever its descriptions say; that is the review gate. Read the file, correct it, replace `schema.yaml` with it (keep `schema.yaml.bak`) and **delete the first line**. The `# REVIEW` markers are harmless and may stay or go. The file is written after every table, so an interrupted run loses at most the table in progress.
- **When something fails.** A table the model fails on (an answer that is not JSON, or has nothing usable, or a timeout) is recorded and the run goes on; the summary gives the counts and lists the failed tables, and the exit code is 1. `--resume` keeps the tables already drafted in the output (as they are in the file now, so a correction you made survives) and asks only about the rest. Without `--resume` the script refuses to replace an existing output, so a reviewed draft is not lost to a second run; `--force` starts over. A name in an answer that the table does not have is ignored, a column the model leaves out keeps the description it had, and nothing is removed. Each answer is allowed about 60 tokens per column asked about (never less than `LLM_NUM_PREDICT`, never more than `--max-tokens`, default 8192): a model that reasons first needs the room, and a table of several hundred columns does not fit one answer and is reported as failed.
- **Narrowing the run.** `--tables 'sales.*'` (repeatable, comma-separated, case-insensitive) limits it to the tables whose key or `schema.name` matches; `--dry-run` lists what would be asked and calls nothing.

Exit codes: 0 done (or nothing to describe); 1 some tables failed or the run was interrupted (the output holds the rest); 2 nothing could be decided or written (`schema.yaml` invalid or in a layout the line editor cannot edit, the model refused or silent, an existing output without `--resume` or `--force`). The full reference is the docstring of `scripts/draft_descriptions.py` and section 2.4 of `docs/deployment-runbook.md`.

### 0.3.4.1 Drafting the scope values: `scripts/draft_policies.py`

Two of the ten required files hold the values of one dimension of your warehouse, the one a ranking question is scoped to when it names no scope of its own: `session_policy.yaml` (`default_scope`: the table, the label shown while the question leaves it open, and the options the one-click clarification offers) and `memory_policy.yaml` (the closed set of values an analyst may pin with the pin on an assumption chip). No other tool writes them, so a first install still has the template's made-up options (`Ring A`, `Ring B`, `All rings`). The server starts and memory seems to work, but every real value an analyst tries to pin is outside the closed set and is refused, and the clarification offers values the warehouse does not have. The preflight check `Session and memory policies use real values` fails on that. This command reads one small dimension and drafts both files.

```powershell
python scripts/draft_policies.py --dry-run
python scripts/draft_policies.py --table Region --column Name
```

- **Which dimension.** `--table` and `--column`; without them, the table and column the two files already name, as long as they are a real table and column of `schema.yaml`. A table is found the way the application finds a configured table name (`Customer` or `sales.Customer`); a column named like personal or contact data is never read. A memory key with no `options` (any value up to its length cap) is left as it is unless `--memory-key` names it.
- **What it reads.** The column's distinct values, with one bounded query through the application's own login (read-only), on the data source of the table's `datasource:`. A column with more than 200 distinct values is refused, because a scope must be a small dimension; `--max-values` changes the limit.
- **What it keeps.** Values the wizard's rules accept (no date, number, code, phone number, address, masked value or error text; at most 40 characters), without the `default_label`, one spelling of each value under the project's Persian and Arabic folding, sorted.
- **What it writes.** `project_config_draft/memory_policy.yaml` and `project_config_draft/session_policy.yaml`: your files with only `options` replaced (and `filter_key` and `column` if you moved the dimension). Comments, `max_length`, `field_name`, `default_label`, `clarification_prompt` and every other field stay as they were, and the script proves it by loading the result as the application does. The memory options never contain the `default_label`. It writes into `project_config/` only with `--overwrite-live`, and then copies each file it replaces to `<name>.bak-<stamp>` first; `--output` chooses another folder, and `--force` replaces a draft that is already there.
- **What it prints.** Counts only. The values are warehouse data, so you see the first few only with `--show-values`; the files hold them, as they must. `--dry-run` reads the column and writes nothing.
- **The ring names, `--ring-aliases`.** The same values are the keys `ring_aliases` of `aliases.yaml` needs (the exact stored value of the name column). With this option the command also writes `project_config_draft/aliases.yaml`: your file with an entry added for each value that has none, the value itself as its form and, with `--generic-word Hall` (repeatable), the value without that leading word (`Hall Export` also gets `Export`, unless another value would give the same form). Your entries, `synonyms`, comments and line endings are not changed, and the script proves it by loading the result. Read the forms and add the ones people really type.

Read both files, copy them into `project_config\`, and run the preflight (section 0.4). Run the command again whenever the dimension gains a value. `field_name`, `default_label` and `clarification_prompt` are not changed, so check them if you moved the dimension. Exit codes: 0 done; 2 nothing could be decided or written. The full reference is the docstring of `scripts/draft_policies.py` and section 2.5 of `docs/deployment-runbook.md`.

### 0.3.5 The first key

The preflight of section 0.4 needs an API key. Its check `API key authentication` fails while no key is configured, and `VERIFY_API_KEY` must hold the raw key of one for the end-to-end proof. So the first key is issued before the first preflight, not after it. Section 2.2 explains keys in full (the raw key and the hash go to two different places; the 401 that comes from pasting the hash); this is the short path for the first, administrator's key:

```powershell
python -m scripts.issue_api_key --id admin-1 --name "Admin" --full-admin
Copy-Item project_config\api_keys.example.json project_config\api_keys.json
```

The first command prints the **raw key** once (43 characters; this is what `VERIFY_API_KEY` and the browser take) and a JSON entry with `key_sha256` (64 hex characters; this goes in the file). Replace the sample entry of `project_config\api_keys.json` with that entry (as long as a sample value is left in the file, the server does not start), and put `API_KEYS_FILE=project_config/api_keys.json` in `.env`. Then set `VERIFY_API_KEY` in the shell of section 0.4 to the raw key. The file is read once, when the server starts. The analysts' own keys come later (section 2.2 and step 17 of the list), once the server is running.

### 0.3.6 Optional: catching a value the model made up (`FILTER_VALUE_CHECK`)

The model sometimes writes a value that is in no row: `WHERE r.Name = N'Ring'` (the name of the table itself), or a phrase from the question that names no ring. The query is valid, so nothing refuses it, and the answer comes back empty or wrong. With this check on, a query the guard accepted is compared, before it runs, with the values of the dimension columns the server has already cached (`prefetchable_columns` in `schema.yaml`; the freshness card of section 2.5.2 shows them). If a value is not among them, the model gets one more message that names the column, says the value does not exist, lists the nearest real values and says to drop the filter if the question names no specific value. The corrected query runs only if it passes the guard, has no value left that does not exist and runs without an error; in every other case the original runs exactly as it would have without the check. **The check never refuses a question**: at most it adds one call to the model.

It is off by default. To use it:

1. In `schema.yaml`, give the small dimension tables `prefetchable_columns`: the label columns a question may name (ring names, currencies, statuses), a few hundred values at most, and not tables of people or anything large. Without them the check has nothing to compare with.
2. Turn it on in `.env` and restart the server:

```
FILTER_VALUE_CHECK=true
```

3. Run the preflight (section 0.4). With the setting on it adds an advice line, `Filter value check has vocabularies`, that lists the dimension tables which still have label columns and no `prefetchable_columns`.

What you will see: the audit log gets a `value_check` block on the requests where something was compared, with the column, a hash and the length of each value that was not found, and whether the corrected query ran (`corrected`) or the original did (`fallback`). The value itself is not written. A corrected question counts as one correction. The full description is section 20.11 of `docs/deployment-runbook.md`.

**A related fix that is on by default (`FILTER_VALUE_SPELLING`).** The warehouse may store a name with the Arabic letters `ي` and `ك` while the model, like the question, writes the Persian `ی` and `ک`. The query is valid, but under the database's collation the two spellings are different strings, so it returns nothing. Before a query runs, the server now looks at each text value it compares with a column listed under `prefetchable_columns` (the cache only: no database read, and `FILTER_VALUE_CHECK` need not be on). If the value is not stored as written but exactly one stored value is the same text spelled with the other letters (`ي`, `ى`, `ك`, an Arabic presentation form, a half-space that is there or not), that stored value takes its place. In a `LIKE` pattern the letters are written the way the column stores them, but only when the column uses a single form. Nothing else is changed, nothing is changed when two stored values could be meant, the guard checks the new query again, and no call goes to the model. The audit log gets a `value_spelling` block (a count and the column, never the value). To turn it off:

```
FILTER_VALUE_SPELLING=false
```

### 0.4 The preflight test

```powershell
python -m scripts.verify_deployment
```

It runs twenty-six checks (with several sources, the database checks run separately for each source): the settings, the mapping of tables to sources, the database connection, whether the login is read-only,
the row cap and the timeout, whether the tables are in the right source, whether the structure of `schema.yaml` matches the databases, whether the model exists on the endpoint and answers each prompt itself, the API key, whether every `denied_columns` name is a real column, whether the audit-log and session-store paths are writable, the loading of `project_config/`, nine checks that only read files and do not touch the database (seven tell a raw draft, or what one leaves behind, from a configured setup; the eighth notices a table of `schema.yaml` that sits in a schema the setup tools are told not to scan; the ninth, the session and memory policies that still hold the template's sample options),
whether the rate limit is sane, and whether the address of the web UI is allowed by CORS; the table below describes each one. The last line is `N passed, N failed, N skipped` and must be `0 failed`. **Run this before every real deployment.** While `FILTER_VALUE_CHECK=true` (section 0.3.6) a twenty-seventh check, `Filter value check has vocabularies`, is added; it only advises.

> **Issue the first key before you run it** (section 0.3.5): the check `API key authentication` fails while no key exists.
>
> **This test alone does not prove that your key works.** Without
> `VERIFY_API_KEY` it only checks that the configured keys (`API_KEYS_FILE` or
> `API_KEYS_JSON`) parse and that there is at least one — not that *your* key authenticates. For
> end-to-end proof, give the raw key:
>
> ```powershell
> $env:VERIFY_API_KEY = "<raw-key-43-characters>"
> python -m scripts.verify_deployment
> $env:VERIFY_API_KEY = $null   # do not leave the raw key in the shell
> ```
>
> The form `VERIFY_API_KEY=... python ...` is bash syntax and in PowerShell
> it gives a **parse error** (`The term 'VERIFY_API_KEY=...' is not
> recognized`) — which looks like a failure of the script itself, not like
> a difference between shells. Since 4.7.0 the script itself prints the right
> form for the shell it was run in.

> **The preflight cannot see the browser's address unless you tell it.** The script, the CLI and `curl` send no `Origin`, so they cannot notice that the page you open would be refused by CORS. Set `VERIFY_UI_ORIGIN` to the address people open the UI at, exactly as the browser's address bar shows it (for example `http://172.16.101.42:8077`), and the check `Web UI origin is allowed by CORS` passes or fails on it. Without it the check only advises.

#### What each check means, and what to do on `[FAIL]`

Each line is `[PASS]`, `[FAIL]` or `[SKIP]` with the check name and the reason; there is no "warning" status; an "advice" is a `[PASS]` line whose reason begins with `advice:` (the admin panel shows it the same way, since it has no other status) and does not change the exit code. A `[FAIL]` is a problem that must be fixed before continuing, and any `[FAIL]` makes the exit code 1. `[SKIP]` means the check could not run (there was nothing to test, or it is deliberately off) and does not fail the run. The checks are in the order they run, with the exact printed name (with several sources, the four checks marked * are printed separately for each source, in the form `Database connectivity [sales]`):

| Check | What it does | What `[FAIL]` means and how to fix it | What `[SKIP]` means |
|---|---|---|---|
| `Settings.validate()` | Runs the same validation the server runs at start-up: the required settings, a left-over placeholder, `.env` lines that python-dotenv cannot read, `SQL_DIALECT` consistent with the connection, every data-warehouse connection and `LLM_EXTRA_BODY`. | The reason is the error text: `OPENAI_MODEL is not configured`; `DB_CONNECTION_URL still has the factory-default placeholder host (username@server)`; `SQL_DIALECT=... does not match ...`; a `.env` problem with the line number and variable name; a source whose `password_env` or `username_env` variable is unset or empty (with its name). Fix `.env` (section 0.2) and run again. | Never. |
| `Tables map to data sources` | Reads `datasources.yaml` and checks that every `datasource:` in `schema.yaml` is the name of a configured source. `PASS` writes `N data source(s): a, b` (without `datasources.yaml`: `1 data source(s): default`). | `schema.yaml assigns tables to data sources that are not configured: Order -> elsewhere. Configured sources: [...]`: a misspelt name (the letters must match `datasources.yaml` exactly) or a source that is not in the file. Or the message of an invalid `datasources.yaml`. | Never. |
| `Database connectivity`* | Connects with the application's own engine and runs `SELECT 1`. `PASS` shows the target with the password masked. | `could not connect to <target>: <driver error>`. Host, port, firewall, ODBC driver name, login, password, `TrustServerCertificate`. Until this passes, no later check can be trusted. | Never. |
| `Login is read-only`* | Tries `CREATE TABLE` on a scratch table (`_nlq_agent_deploy_verify_probe`) inside a transaction that is always rolled back, then checks that nothing persisted; it also drops such a table if a previous run left one. | `... PERSISTED -- the login can write ...`: the login is not read-only. Stop and have the DBA apply `docs/db-hardening.md`. Or `could not verify: ...`. If the `[PASS]` says the `CREATE TABLE` did not raise but the rollback held, the login *could* create tables and only the rollback saved it; still have the DBA tighten it. | There is no database connection. |
| `Row cap`* | Through the executor runs `SELECT TOP (10 x cap + 10) name FROM sys.all_objects` and counts the rows. | `returned 1500 rows, expected <= 1000`: the executor returned more than `MAX_ROWS_RETURNED` rows. Do not deploy until it is fixed; there is no setting to fix it, report it. | The database is unreachable or the test query does not run (it is T-SQL). |
| `Query timeout`* | Runs `WAITFOR DELAY` with a timeout of at most 5 seconds and measures when it is cut off. | `took Ns -- longer than the Ms timeout should allow` or `WAITFOR DELAY completed ... without the timeout firing`: the driver's timeout is not applied or the server does not support `WAITFOR` (some serverless tiers of Azure SQL). Look at `QUERY_TIMEOUT_SECONDS` and the driver. | The database is unreachable or the test query does not run. |
| `Tables are in their data source` | Compares `schema.yaml` with each source's list of tables and fails for a table whose columns are not all in the assigned source but which another source has. | `N table(s): <table>: not in <assigned>, found in <other> — set datasource: <other>; ...` (ten examples, then `and N more`). The table has no `datasource:` (so it goes to the default source) or has a wrong one. Run `python scripts/sync_schema.py` and replace `schema.yaml` (section 0.3.3 step 2); this is step 7 of the "Install from scratch" list. Or `could not compare schema.yaml with the data sources: ...` when a source's table list cannot be read. | One data source (`one data source -- nothing to place`). |
| `Schema structure matches the databases` | The same as `python scripts/sync_schema.py --check` in memory: it reads three catalogue views from each source and fails if `schema.yaml` has fallen behind the databases: a column the database has and the file lacks, a recorded type that differs, a column or table the database has lost, a `datasource:` line that should be set. A type that is merely not yet recorded is not falling behind. | `N column(s) missing from schema.yaml; N column(s) and N table(s) no longer in the database; N column type(s) differ; N datasource: line(s) to set[; N column type(s) to record] -- run python scripts/sync_schema.py ...`. Run the script, review `schema.synced.yaml` and replace `schema.yaml` (section 0.3.3 step 2). If instead of the counts it says `schema.yaml differs from the synced text in a way the drift counts do not cover -- run python scripts/sync_schema.py --dry-run to see it`, the file differs from the synced text in something the counts do not name; read the `--dry-run` output. A column that was already marked `# not in database` does not fail and is counted in the `PASS` detail. A `schema.yaml` written before 6.9.0 lacks the `column_types:` map; since 6.9.2 this is a `PASS` and its detail ends with `N column type(s) not recorded yet -- run python scripts/sync_schema.py once to record them (review schema.synced.yaml, replace schema.yaml), so later type changes are detected`. Do it once so the next type change is seen. | No database could be read (the connectivity check reports the error itself), `schema.yaml` does not load (`project_config/ loads` gives the cause), or its layout cannot be edited line by line. |
| `OpenAI-compatible model exists` | Requests the `/models` path of `OPENAI_BASE_URL` (5-second timeout, `OPENAI_API_KEY` as the bearer) and looks for `OPENAI_MODEL` among the ids. | `could not reach <base>: ...`: wrong address or port, endpoint down, a proxy, or an endpoint without a `/models` path. Or `'<model>' not found among models <base> lists: [...]`: set `OPENAI_MODEL` to one of the listed ids. | Never. |
| `Model endpoint answers each prompt itself` | Sends two small requests to the model, with the same backend settings as the server (`OPENAI_BASE_URL`, `LLM_EXTRA_BODY`, the seed, `temperature=0`; only `max_tokens` at most 128 and a 15-second timeout). Each asks for a fresh random token (`Reply with exactly this token and nothing else: <32 hex characters>`) and the two prompts are identical except for that token, so a semantic or prefix response cache collides on the second request. `PASS` when each reply has its own token (a model that adds words around the token passes too); the detail gives the reply times in milliseconds and never the reply text. | `the second test request, which carried a different random token, was answered with the first request's token (...). A response cache in front of the model returned another prompt's answer ...` or `the reply to the first/second test request did not contain its own random token`: a response cache in front of the model (a gateway with a semantic or prefix cache) answers a new prompt with the stored answer of a similar prompt. That cache can give a wrong SQL for a similar question. Turn off response caching for this client at the gateway, or point `OPENAI_BASE_URL` straight at the model server (part 7, "A response cache in front of the model"). A model that ignores the instruction fails the same way; ask the model server directly to tell the two apart. | `LLM_PROVIDER=mock`; the endpoint is remote and `LLM_ALLOW_REMOTE` is off (nothing is sent to it); the endpoint does not answer (`OpenAI-compatible model exists` says so); or a reasoning model spent all its tokens before answering (turn reasoning off for the request with `LLM_EXTRA_BODY`). In the admin panel it runs only with "deep checks" («بررسی‌های عمیق»), because it is the only check that spends model-server time. |
| `API key authentication` | Checks that at least one key is configured (`API_KEYS_FILE` or `API_KEYS_JSON`, plus the application database) so the server can start, and that `VERIFY_API_KEY`, the raw key, authenticates. | `API key configuration is invalid: ...` (the server will not start); `AUTH_REQUIRED is true but there are no configured keys ...` (issue a key, section 2.2); all the keys in the application database are revoked or disabled; or `VERIFY_API_KEY was set but did not match any configured key's SHA-256 digest` (an incomplete copy, or you put the `key_sha256` instead of the raw key). With `AUTH_REQUIRED=false` it passes and says so; do not use that in production. | Never. |
| `Column restrictions name real columns` | Reads the keys the server would use (`API_KEYS_FILE` or `API_KEYS_JSON`, plus the application database) and `schema.yaml`. A plain `denied_columns` entry is cleaned of whitespace and invisible Unicode characters at its two ends, as the guard reads it, and compared with every column name of `schema.yaml` without regard to case; it matches when it, or it without any invisible character inside it (a Persian name may contain a zero-width non-joiner), is a column name. | `N plain denied_columns entries of M key(s) match no column of schema.yaml, so they deny nothing -- <key id>: 'Entry'`; each entry is shown with `ascii()`, so an invisible character appears as `\u200c`. A typo, a renamed column, or a space inside the name (`National ID`). Correct it in the key file and restart, or save it again in the admin panel; the server starts anyway, and until it is fixed the entry denies nothing. When every entry matches, a `[PASS]` line starting `advice:` lists the columns that look like a denied name and are not denied (`NationalCode` and `CustomerNationalID` next to `NationalID`); it never fails. | No key is configured, the key configuration is invalid, or `schema.yaml` does not load. |
| `Audit log directory writable` | Creates `LOG_DIR` if it is missing, writes a scratch file in it and deletes it; it also checks that an existing `audit_log.jsonl` is writable. It never writes `audit_log.jsonl` itself. | `could not create ...` or `... is not writable`: fix the folder's permissions or `LOG_DIR`. Because a failed audit write never fails a user's question, it has to fail loudly here. | Never. |
| `Session store directory writable` | The same test for the `SESSION_STORE_PATH` folder. | `could not create ...` or `... is not writable`. | `SESSION_STORE_PATH` is empty (persistence is deliberately off). |
| `project_config/ loads` | Checks that each of the ten required files (`aliases.yaml`, `entities.yaml`, `business_rules.yaml`, `examples.yaml`, `metrics.yaml`, `schema.yaml`, `retrieval_hints.yaml`, `session_policy.yaml`, `memory_policy.yaml` and `system_prompt.md`) is present, and then reads each with the current code's models. An empty `system_prompt.md` fails. | `N of 10 required files not found under '<dir>': ...` with the names of all the missing files together (copy each from `project_config.example/`, section 0.3), `<file> not found under '<dir>'` or `<file> failed validation ...` with the field name: a file from an old deployment lacks a field that a later version made required. Fix the field or compare with `project_config.example/`. A duplicate key in YAML is rejected (part 4 step 3). | Never. |
| `project_config/ is curated, not a raw draft` | Reads the first lines of every `*.yaml` file in `project_config/` for the header a draft carries (`# AUTO-GENERATED DRAFT` from `database.schema_inspector_cli`, `# AUTO-GENERATED by setup_project.py` from the wizard, `# AUTO-GENERATED by draft_descriptions.py` from `scripts/draft_descriptions.py`), and counts in `schema.yaml` the tables whose description is still the older inspector's placeholder (`<schema.table> — auto-detected <kind> table (~N rows)`), the tables described by nothing but their own name (`description: 'Sales_Fact.Trade'`: the key, the bare name or `Schema.Table`, which is what `sync_schema.py --add-tables` and the inspector since 6.9.3 write) and the column descriptions that are only `<type> column` (`bigint column`). It reads `schema.yaml` even when a duplicate key (a draft from before 6.9.3) prevents the application from loading it. No call to the database. | `N file(s) carry the AUTO-GENERATED DRAFT header: <files>` (for the wizard's files, the same with `AUTO-GENERATED by setup_project.py`, and for the file of `scripts/draft_descriptions.py` with `AUTO-GENERATED by draft_descriptions.py`) and/or `schema.yaml: N of M table descriptions (P%) are still the placeholder`, `... are only the table's own name` or `schema.yaml: N of M column descriptions (P%) are only <type> column`: the files are drafts, not a configured setup (a draft lists every table of one database, so if it has taken the place of the configured file, the descriptions, the aliases and, with two databases, one of the databases are lost). On an upgrade, restore the configured files from backup or version control and add the new tables with `python scripts/sync_schema.py --add-tables 'Schema.*'` (sections 0.3.2 and 0.3.3), which keeps the configured text. On a first install there are no configured files to restore: draft the descriptions with `python scripts/draft_descriptions.py` (section 0.3.4), read `schema.described.yaml` (every drafted line ends with `# REVIEW`), replace `schema.yaml` with it and delete its header line. A file you have really reviewed passes once you delete its header line. The older placeholder counts when more than a quarter of the descriptions are it, the other two when more than half are; a few tables added with `--add-tables` among configured ones show in the `[PASS]` line (`3 of 37 only repeat the table's name`) and do not fail the check. A file whose header line was deleted is caught by these descriptions. | `schema.yaml` is missing or cannot be read (`project_config/ loads` says which). |
| `Aliases are curated` | Reads `aliases.yaml`. Counts the `synonyms` keys that are not words (without a run of two letters: timestamps and numbers) and the synonym lines that still end in the inspector's `# REVIEW` marker. It also reads an `aliases.yaml` that is not valid YAML (a draft from before 6.9.3 with a quote inside a sample value) from its lines. It reads `schema.yaml` too and compares the keys of `ring_aliases` with its table keys (the whole key, letter case ignored). | `N of M synonym keys are not words (timestamps or numbers)` or ``N of M synonym entries still end in the inspector's `# REVIEW` marker``, more than half of the keys: the synonyms are the inspector's seeds (sample values of text columns, not vocabulary). Restore the configured `aliases.yaml`. An empty `ring_aliases` is **not a failure**: only `ValueRetriever.extract_ring` reads it, to set the `Ring` filter from the name of a trading hall (ring) in the question; a warehouse without such a dimension leaves it empty (the wizard also writes it empty when it finds no dimension table). While `schema.yaml` or `entities.yaml` exists it appears in the `[PASS]` line as `advice: ring_aliases is empty`; if your questions name the hall, add the names. A key of `ring_aliases` is the exact stored value of the ring dimension's name column, not the name of a table: `N of M ring_aliases key(s) are keys of schema.yaml tables: Hall, Region, ...` is what the wizard of 6.13 to 6.16.2 left, one entry per table, so a question that named a currency filtered the ring by `N'Currency'`. The application ignores such entries when it loads the file (one `WARNING` at start). Run `python setup_project.py` again over the folder that holds the file (it drops them and says how many) or restore the configured ring names from a backup; `python scripts/draft_policies.py --ring-aliases` drafts entries from the stored values. A ring whose stored name is exactly, letter case aside, a key of `schema.yaml` cannot be told from such an entry and is ignored too. | `aliases.yaml` is missing. |
| `Every data source is described` | Reads `datasources.yaml` (without it, a single source named `default`) and `schema.yaml`: a table belongs to the source its `datasource:` line names (for a list, to each) and without one to the default source. | `N of M data source(s) have no table in schema.yaml: <sources>`: a whole database has dropped out of the configuration (usually a `schema.yaml` drafted from one of two databases). Restore the configured `schema.yaml` or add the tables with `python scripts/sync_schema.py --add-tables 'Schema.*'`. If the file has no table, `schema.yaml describes no table`. A `datasources.yaml` that has a second source while `schema.yaml` has no `datasource:` line also fails here and names the second source: run `scripts/sync_schema.py` to write the lines. | `schema.yaml` or `datasources.yaml` does not load (their own checks give the reason). |
| `No staging or change-capture tables` | Counts the tables of `schema.yaml` whose schema or name matches `STAGING_PATTERNS` (default `*_Stg,*stg*,*staging*,table:*_Inserts,table:*_Updates,table:*_Deletes`). `fnmatch` patterns with commas, case-insensitive; a plain pattern is matched against the schema and the `table:` prefix against the table name; empty turns the check off. | It never fails. A `[PASS]` line beginning with `advice: N of M table(s) look like staging or change-capture tables (<schema>: N, ...)` gives the count per schema (never a value). These tables duplicate the very tables the analyst asks about, so the model sees one name several times and the prompt budget is spent on them; unless the analyst needs them, take them out of `schema.yaml` (the inspector's `--exclude-schemas` keeps them out of the draft). | `STAGING_PATTERNS` is empty or `schema.yaml` does not load. |
| `No live sample values in descriptions` | Looks at the text after each `e.g.` in a table description or column note (outside a parenthesis) for a run of 7 or more digits (phone-like; Persian digits too), exactly 10 digits (national-id-like) or a long segment of words with digits (address-like). Dates and plain words are left alone. | It never fails. A `[PASS]` line beginning with `advice: N note(s) in M table(s) seem to quote live sample values (phone-like: N, national-id-like: N, address-like: N); tables: <keys>` gives counts and table keys, never a value. A `schema.yaml` drafted before 6.13.0 quotes up to three values of each text column, and the notes go into the prompt sent to the model. Replace each `e.g.` list with a description of the column. | `schema.yaml` does not load. |
| `No table in an excluded schema` | Reads `schema.yaml` and the exclusion rule of each table's source (the system schemas, `EXCLUDE_SCHEMAS` and the source's `exclude_schemas:`, section 0.3.3). A table counts when its schema (`db_schema`, or the key's qualifier) is excluded in every source it is assigned to; a table whose schema is unknown is never counted. | It never fails. A `[PASS]` line beginning `advice: N of M table(s) are in a schema the setup tools are told not to scan: <keys>` lists the first keys and the two fixes: remove the tables from `schema.yaml`, or narrow the exclusion. The exclusion only limits what the inspector and `sync_schema.py` propose, so such a table is still synced. | Nothing is excluded beyond the system schemas and no table is in one, or `schema.yaml` or `datasources.yaml` does not load. |
| `Aliases hold no raw data values` | Reads `aliases.yaml` and counts the `synonyms` keys that have the shape of a value copied out of a row: a date or time (`1387/03/24`, `2024-05-01 10:00:00`), a phone number, an IP address, an e-mail address or URL, a masked value (`ab xxxxx cd`, `****1234`), a hash, a run of 8 or more digits (a national id) or an address (words with digits and a street word). A year, `q1` or any word is left alone; Persian digits are read as digits. | More than 5 such keys: `N of M synonym keys look like raw data values (<date>: N, <phone>: N, ...)`, the count for each shape and never a key. These are the synonyms the wizard of 6.13 and 6.14 built from sample values; they go into the prompt and stay in version control, and the check cannot tell a person's name from a word, so read the file as well. Delete them (keep only words people type); the wizard no longer writes them unless you give `--value-synonyms`. | `aliases.yaml` is missing. |
| `Entities match schema keys` | Reads `entities.yaml`, the keys of `ring_aliases` and `schema.yaml`. Retrieval takes an entity's key to be the name of a table, so it has to be that table's key in `schema.yaml` (a bare name for a table keyed by its bare name, `Schema.Table` for one keyed so; the bare name of a `Schema.Table` key is accepted when it picks out one key). It reports an entity key that is no table's key, two entities for one table, and a `ring_aliases` key that names a table by something other than its key (the name of a hall that is no table is left alone). | `N entity key(s) are not a schema.yaml table key: Sales_Dim.ActionType (use ActionType), ...`, `N table(s) have two entities: ...` and `N ring_aliases key(s) name a table but not by its schema.yaml key: ...`. This is what the wizard of 6.13 and 6.14 left behind when it wrote every table as `Schema.Table`: `schema.yaml` keys a table whose name is unique by its bare name, so the qualified entity matches nothing and the bare duplicate beside it has no aliases. Rename each key of `entities.yaml` to the one in brackets and merge the aliases of two entities into one; a `ring_aliases` key that names a table does not belong in that file (a key there is the name of a ring, and `Aliases are curated` fails on it): drop it, its aliases are in `entities.yaml`. Running the wizard again over the same output folder does all of this for you. | `schema.yaml` or `entities.yaml` does not load. |
| `Session and memory policies use real values` | Reads `session_policy.yaml`, `memory_policy.yaml` and `schema.yaml`. Each `filter_key` must name a table of `schema.yaml`, each memory key's `column` must be a column of it, and the options of a file must not be exactly the sample values of `project_config.example/` (a set that differs by one value is not flagged). Reading `project_config.example/` itself, it does not compare the template with itself. A `[PASS]` may carry advice: when the running server has cached the dimension's values, the number of options that are not among them (counts only; the command line has no cache). | `session_policy.yaml default_scope: options are the sample values of the shipped session_policy.yaml`, or `memory_policy.yaml keys.scope: filter_key 'X' names no table of schema.yaml`, or `column 'Y' is not a column of T`. Values are never printed. With the template's options every real value an analyst pins is refused and the clarification offers values the warehouse does not have. Draft the real ones with `python scripts/draft_policies.py` (section 0.3.4.1), read them and copy the two files into `project_config/`. | `schema.yaml` or a policy file does not load (`project_config/ loads` gives the reason). |
| `Rate limit sane for deployment` | From `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_WINDOW_SEC` and `RATE_LIMIT_BURST` works out the requests per second for each analyst, assuming `VERIFY_EXPECTED_ANALYSTS` people (default 10) share one key's bucket. | Fewer than 0.1 requests per second per analyst: raise `RATE_LIMIT_REQUESTS` or set `VERIFY_EXPECTED_ANALYSTS` to the real number. | Never. |
| `Web UI origin is allowed by CORS` | Reads the settings only. With `VERIFY_UI_ORIGIN` set to the address people open the UI at, it looks for that origin in `CORS_ALLOWED_ORIGINS`. Without it nothing can be proved, so it only advises. | `<origin> is not in CORS_ALLOWED_ORIGINS (...)` together with the line to add: `CORS_ALLOWED_ORIGINS=<current list>,<origin>`. Add it and restart the server, or open the page with exactly an allowed address. Without `VERIFY_UI_ORIGIN` it never fails: an `advice:` appears when every allowed origin is loopback but `API_HOST` is not, or when exactly one non-loopback origin is allowed. List every address the page is opened with (IP address, host name, `localhost`) and set `VERIFY_UI_ORIGIN` to test one. | Never. |
| `Filter value check has vocabularies` | Listed only while `FILTER_VALUE_CHECK=true` (section 0.3.6), after the others. Reads `schema.yaml` and `entities.yaml`: for each dimension table (one `entities.yaml` names) it looks for label-like columns (`Name`, `Title`, `Kind`, `Status`, `Type` ..., not a person's name or contact data) and for `prefetchable_columns`. | It never fails. A `[PASS]` line beginning `advice: N dimension table(s) have label-like columns and no prefetchable_columns, so an invented value in them is not caught: <table> (<columns>), ...` gives table and column names, never a value. Add `prefetchable_columns` to the small ones in `schema.yaml` (a few hundred distinct values at most) and leave out tables of people and anything large. | `schema.yaml` or `entities.yaml` cannot be read; `project_config/ loads` says why. |

Note: up to 6.7.0 the `project_config/ loads` check read only six files and did not catch a missing `session_policy.yaml`, `memory_policy.yaml`, `retrieval_hints.yaml` or `system_prompt.md`; since 6.8.0 it reads all ten. A relative `PROJECT_CONFIG_DIR` is resolved from the repository root, as the server itself does.

---

## Part 1 — CLI

### 1.1 Running it

```powershell
cd <repo-root>
python app.py
```

You should see this:

```
============================================================
 Auction NLQ Engine
 Model : <your model>
 DB    : <server>/<database>
============================================================
 Type your question in Persian or English.
 Commands: exit | quit | Ctrl+C
============================================================

❓ Question:
```

### 1.2 Asking

Write the question in Persian or English and press Enter:

```
❓ Question: ۱۰ مشتری برتر از نظر مبلغ خرید در سال ۱۴۰۳
```

(The sample question reads "Top 10 customers by purchase amount in 1403".)

The output, in order:

1. **`GENERATED SQL`** — the generated SQL, after it has passed the guard
2. **The path of the Excel file** — saved automatically in `exports/`
3. **The execution time**
4. **`QUERY RESULT`** — the first 20 rows; if there are more, it says how many
   rows are not shown and what the total is

### 1.3 Exiting

`exit` or `quit` or `Ctrl+C`.

### 1.4 Things you should know

- **There is a 2-second gap between two questions.** If you press Enter
  sooner you get the message `Please wait N s…` and it waits by itself.
  This is deliberate, so that a mistaken Enter key held down does not
  put the endpoint under load.
- **The CLI needs no API key.** It does not import the authentication
  layer at all. `AUTH_REQUIRED` affects only HTTP.
- **The CLI has no memory or session.** Each question is independent; "from among
  those…" does not work. That path exists only in the web UI and `/v2`.
- **Each question writes one line to `logs/query_log.jsonl`** (the audit
  record is specific to the HTTP API). Result rows are never written to it.

### 1.5 When you get an error

| Message | What it means |
|---|---|
| `❌ Configuration error: …` | `.env` is incomplete or invalid; the server did not start at all |
| `⚠️ This system only answers … analytics questions` | The question was judged out of scope (`OUT_OF_SCOPE`) |
| `❌ Validation error: …` | The guard stopped the SQL — a table/column outside the allowlist, a comment, DDL/DML |
| `❌ Runtime error: Database error: …` | The SQL ran but the database rejected it |

---

## Part 2 — Web

### 2.1 The key point: two servers, two directories

`web/` is a **static** client. It imports no Python from this project and
never runs inside the backend process. So:

| | What | From where | Port | Open it in the browser? |
|---|---|---|---|---|
| Terminal 1 | FastAPI backend | **the repository root** | 8000 | **No** |
| Terminal 2 | Static file server | `web/` | 8080 | **Yes ← here** |

> **Both are needed. Terminal 2 is not optional.**
>
> The user interface is on **8080**, not 8000. If you start only terminal 1
> and open `http://localhost:8000`, you see no page — that is the API,
> not the UI. A server that is working correctly has nothing to show at
> that address either.
>
> Since version 4.1.1, if you open `http://localhost:8000` you get a JSON
> response that says exactly this and sends you to 8080. Before that you
> got only `{"detail":"Not Found"}`.

If you run the backend from inside `web/` you get this:

```
ModuleNotFoundError: No module named 'api'
```

and `pip install api` does not help either — no such package exists on PyPI.
The `api` it wants is this repository's own package and is imported only from the root.

### 2.2 Issuing an API key (once per analyst)

```powershell
python -m scripts.issue_api_key --id analyst-1 --name "Analyst name"
```

This command prints **two things**, and the most important point of this
whole section is that they go to **two different places**:

| What | Length | Where it goes |
|---|---|---|
| **The raw key** | 43 characters | **The browser** — the «کلید API» ("API key") field |
| **The JSON entry** (including `key_sha256`) | A 64-character hex hash | **The array of keys**: the `API_KEYS_FILE` file (recommended) or `API_KEYS_JSON` in `.env` |

For a single key, one line in `.env` is enough:

```ini
API_KEYS_JSON=[{"id":"analyst-1","name":"...","key_sha256":"<64 hex>"}]
```

For several keys use a file (next section).

Only the SHA-256 is stored, not the key itself. The raw key is printed **only once**
and is not saved anywhere.

> **The most common mistake: pasting the hash into the browser instead of the raw key.**
>
> This mistake is natural and not your fault. The JSON entry is what
> stays on the screen and gets copied; the raw key is above it and scrolls past, under a line that
> tells you to copy it now because it will not be shown again.
>
> But the result is a **401**: the server takes the SHA-256 of whatever
> you send and compares it with `key_sha256`, so sending the hash means the
> server computes the **hash of the hash**.
>
> **Tell them apart by length:** the raw key is 43 characters, the hash is 64
> characters of `0-9a-f`. If what you are pasting is 64 hex characters, it is the hash.
>
> To be sure — this must give exactly the `key_sha256` inside the key file (or `.env`):
>
> ```powershell
> python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "<raw-key>"
> ```
>
> Since 4.6.1 the 401 message itself says this: if the value you sent has
> the shape of a SHA-256, the reply says which value goes where. The reverse
> direction (pasting the raw key into `key_sha256`) was already caught
> at start-up with a loud error.

#### Several keys: the `API_KEYS_FILE` file

For several keys, put the array in a separate file. The content of the file is exactly
what you would have written in `API_KEYS_JSON`, only with whatever formatting and
indentation you like. The recommended path is `project_config/api_keys.json`; the
`project_config/` folder is ignored by git, so the key hashes do not enter the repository.

A ready-made template is in `project_config.example/api_keys.example.json`: copy it to
`project_config/api_keys.json` and replace each entry's `key_sha256` with
the hash that `scripts/issue_api_key.py` prints. As long as a sample value
(`<64-hex-char digest ...>`) is left in the file, the server does not start.

```json
[
  {"id": "admin-1", "name": "Admin", "key_sha256": "<64 hex>",
   "admin": true, "operations": true, "security": true},
  {"id": "analyst-1", "name": "Analyst", "key_sha256": "<64 hex>"}
]
```

Then in `.env` write only the path and remove `API_KEYS_JSON`:

```ini
API_KEYS_FILE=project_config/api_keys.json
```

- A relative path is resolved from the **repository root**, not from the folder
  you started the server in (like `PROJECT_CONFIG_DIR`).
- The file is read **once, when the server starts**, like `.env`. Restart
  the server after every edit. `verify_deployment` is a separate process and always sees the
  current file.
- Do not set `API_KEYS_JSON` and `API_KEYS_FILE` **at the same time**; the server does not start.
- If the file is missing, the JSON is invalid (the message gives the line and column number), or
  a field appears twice inside one entry, the server does not start.

> **And if you prefer to keep everything in `.env`:** one line is perfectly fine.
> Several lines work only when the whole value is in **single quotes**:
>
> ```ini
> API_KEYS_JSON='[
> {"id":"admin-1","name":"Admin","key_sha256":"...","admin":true},
> {"id":"analyst-1","name":"Analyst","key_sha256":"..."}
> ]'
> ```
>
> Without the quotes it **does not work**: `.env` is read line by line, so
> `python-dotenv` takes only `[` as the value and treats the remaining lines as
> **new keys**. Double quotes also break the JSON, and an
> apostrophe inside the value (for example `"name": "Ali's key"`) breaks the whole line.
> These used to be only a one-line warning on stderr and the server started with the misleading message
> "no usable key"; now every `.env` line that
> `python-dotenv` cannot read (an invalid line, the leftover of a broken
> multi-line value, or a variable that appears twice with different values) is rejected at start-up and
> names the **line number and the variable name**, never its value. The fix: use
> single quotes and no apostrophe, or move the keys to `API_KEYS_FILE`.

To restrict columns for one analyst:

```powershell
python -m scripts.issue_api_key --id analyst-2 --name "..." --denied-column NationalID
```

That column is rejected in the guard, not merely hidden in the cache.

A name is compared with the column names of `schema.yaml` without regard to case, and otherwise exactly. Since 6.15.1, whitespace and invisible Unicode characters at the start or the end of an entry (the zero-width non-joiner and the left-to-right and right-to-left marks that Persian editors insert without showing them, the bidi embeddings, the byte-order mark) are removed before the entry is read; characters inside an entry are never changed, so a Persian column name that contains a zero-width non-joiner stays as you wrote it, and the guard denies it both as written and without that character; for each key whose entry was changed the log has one warning that names the key and shows both spellings. A space inside a name is not removed either, so `National ID` is a different name from `NationalID`. A name that no column has (a typo, a renamed column) denies nothing: the server logs a warning, and the preflight check `Column restrictions name real columns` (section 0.4) fails and lists the key and the entry, but the server still starts. The same data often lives under several names (`NationalID`, `NationalCode`, `CustomerNationalID`); denying one leaves the others, and that check also advises which columns look like a denied name and are not denied.

To make a column usable only for joining tables (`JOIN ... ON a.col = b.col`), with its value never displayed and never filtered or grouped on, write the entry with a scope:

```powershell
python -m scripts.issue_api_key --id analyst-3 --name "..." --denied-column sales.Order.ID
python -m scripts.issue_api_key --id analyst-3 --name "..." --denied-column Warehouse:sales.Order.ID
python -m scripts.issue_api_key --id analyst-3 --name "..." --denied-column Warehouse:ID
```

The first form applies to that table in every source, the second only when the query runs on the `Warehouse` source, and the third to every `Warehouse` table that has this column. The source, table and column names are checked at start-up against `datasources.yaml` and `schema.yaml`, and a typo stops the start-up. A limitation: "join only" hides the column's value, not its existence; if a foreign key that points to that column is visible elsewhere, hiding the primary key is only apparent and the foreign key must be restricted too. A column without a table prefix is conservatively counted as restricted.

> **Give each analyst a separate key, not one shared key.** Two practical reasons:
> the audit record logs `principal_id` — with a shared key, the whole week's log
> shows everything as one person. And the rate-limit bucket is on the pair (principal, ip) —
> a shared key behind one host puts the whole organization into one bucket.

### 2.2.1 Why so many tokens? (and when you can have none)

A fair question. During setup you enter something like a token in three places,
but they are **two different secrets, not one secret three times**:

| Where | What | Between whom |
|---|---|---|
| `OPENAI_API_KEY` in `.env` | The **language model's** credential | Your server ← the model endpoint |
| `API_KEYS_FILE` or `API_KEYS_JSON` | Only the **hash** of the analyst's key | What the server stores |
| The key field in the UI | The analyst's key **itself** | The browser ← your server |

The first row is completely unrelated to the other two — it is your password to the language model,
not to this system. **With a local model it usually stays empty**, and then the only real
token in the whole system is the analyst's key.

> **Never set these two to the same value.** They point in opposite directions: `OPENAI_API_KEY`
> is what your server presents **outward** (to the language model), and the analyst's
> key is what the browser presents **inward** (to your server).
> Making them one means anyone with access to the language-model server's configuration or logs —
> and many of those servers log the `Authorization` header — could
> impersonate an analyst.

The second and third rows are **one** secret, but this is not entering it twice: the server keeps the *hash*
and the browser keeps the *key itself*. This is not duplication; it is the same pattern every
password system has. If the server stored the raw key, anyone who read `.env`
could impersonate any analyst — and the audit record, which logs
`principal_id`, would be worthless.

**But for a single-user run on your own laptop, none of it is needed:**

```ini
AUTH_REQUIRED=false
```

Then you do not need `API_KEYS_FILE` / `API_KEYS_JSON` and the UI does not ask for a key. The server gives a
warning at every start-up that you must not do this in production — which is right:
without keys, the audit log cannot say who asked what, and the rate-limit bucket
sees everyone as one. Fine for a local trial, not for the first working day.

### 2.3 Terminal 1 — the backend

```powershell
cd <repo-root>
.\.venv\Scripts\Activate.ps1
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

> If you have set `API_HOST` and `API_PORT` in `.env` (defaults
> `127.0.0.1` and `8000`), you can run this instead of the command above —
> the same application, the same `--no-server-header`, but it reads the
> host/port from `.env` and not from the command line:
>
> ```powershell
> python -m api
> ```

Check its health — **with curl, not with the browser**:

```powershell
curl http://localhost:8000/health
```

Something like this should come back:

```json
{"status":"ok","openai":true,"database":true}
```

`status: "degraded"` means one of the LLM and the database is unreachable and `"down"` means neither is; the backend itself is up — look at `.env`. With several data sources, `database` is `true` only when **all** the sources have answered, and `database_detail` lists the name of each source.

`/health` and `/` are the only routes that need no key.

### 2.3.1 If you serve the UI from somewhere else: CORS

The page is on 8080 and makes requests to 8000 — that is, cross-origin. The backend
allows `http://localhost:8080` and `http://127.0.0.1:8080` by default,
so the local setup works with nothing extra.

If you serve the UI from another host or port, put this in `.env` and
restart the server:

```ini
CORS_ALLOWED_ORIGINS=http://your-ui-host:8080
```

> **This error looks like "the backend is broken", not like a configuration
> mistake.** The browser does not tell the page the real reason a cross-origin request was
> refused — it gives the same `Failed to fetch` it gives when nobody is
> behind that port. The sign: **all three lights, API, LLM and DB, are red, but
> the CLI works fine with the same server.** The CLI is not a browser and sends no
> `Origin` at all. The web page itself, when the health check meets a
> network error, shows both possibilities (CORS, or nothing is up at all)
> side by side, with the exact origin of this very page — there is no need to guess
> by hand.
>
> For a definitive confirmation, look at the server's start-up log — where the
> provenance message appears, this line is there too:
>
> ```
> CORS allowed origins: http://localhost:8080, http://127.0.0.1:8080
> ```
>
> If your UI's real origin (protocol + host + port) is not in this list, set the
> `CORS_ALLOWED_ORIGINS` above and restart the server.

When a request arrives from an origin that is not in the list, the server also writes one warning to its log, once for each origin. It names the origin and the line to add to `.env`:

```
CORS: origin 'http://localhost:8077' is not allowed (allowed: http://172.16.101.42:8077). Browsers show this as 'Failed to fetch'. To allow it add this line to .env and restart: CORS_ALLOWED_ORIGINS=http://172.16.101.42:8077,http://localhost:8077 -- or open the page with exactly an allowed address (scheme, host and port). Logged once per origin.
```

The page must be opened with **exactly** an allowed address: protocol, host and port. `http://localhost:8077` and `http://172.16.101.42:8077` are two different origins even when they are the same machine, and so are `localhost` and `127.0.0.1`. The variable replaces the default list, so write every address the page is opened with (IP address, host name, `localhost`), comma-separated. The web page and the admin panel show a red banner at the top that names the origin to add, instead of leaving you with three red lights; and the preflight check `Web UI origin is allowed by CORS` (section 0.4) tests an address before the first analyst opens the page.

### 2.4 Terminal 2 — the static files

```powershell
cd <repo-root>\web
python -m http.server 8080
```

Then open: `http://localhost:8080`

`python -m http.server` only hands files to the browser; it has no idea what `/query`
is. Connecting to the backend is the browser's job, in the next step.

### 2.5 Live mode — the default since 4.4.0

> **If you follow an old guide, you get stuck here.** Up to 4.3.0 you had to
> press the «زندهٔ API» ("Live API") button, then enter the backend address in a field at
> the top of the page and press «اتصال» ("Connect"). **That field no longer exists** and looking
> for it is pointless.

Since 4.4.0 the page comes up directly in **live** mode and the backend address is no longer
something the analyst sees or touches. The reason was that a wrong value
in that field looked **exactly like a dead backend** and there was no
way, from inside the page, to tell the two apart.

The backend address is now in one file — `web/js/config.js`, the only file a
deployment edits — and the admin panel (`web/admin/`) uses exactly the same two
values and the same order of precedence (`?base=` > the stored value > this file)
— you no longer need to type the address separately at the top of the admin panel:

```js
export const DEFAULT_BASE_URL = "http://localhost:8000";
export const DEFAULT_API_PORT = 8000;
```

If your backend is on that same `localhost:8000`, **there is nothing to do**.
If it is elsewhere, change this one line — or leave `DEFAULT_BASE_URL` empty
(`""`) so that the backend address is built automatically from this page's protocol and host plus
`DEFAULT_API_PORT`; this means moving to another host no longer needs
a JS edit (as long as the backend is reachable on the UI's own host, only on a different
port).

Every address that is entered — this file, `?base=`, or the stored value — is cleaned
and validated: without `http://`/`https://` it automatically gets `http://`, a trailing
path or `/` is removed, and a value that cannot be understood at all
(for example without a valid scheme) is rejected and a clear message is shown —
instead of being silently sent to the static file server as a relative path.

So the only thing left is the key:

1. Open the page — it is already live
2. Put the raw key from step 2.2 in the **«کلید API»** ("API key") field and press **«ذخیره کلید»** ("Save key")

The demo mode has not been removed, it is just no longer the default:

```
http://localhost:8080/?live=0
```

And for debugging, `?base=` still works and takes precedence over `config.js`:

```
http://localhost:8080/?base=http://192.168.1.50:8000
```

The key stays in the same browser's `localStorage`, never goes into the URL, does not appear in
the log and is not echoed back in an error message.

> **An honest consequence:** because live mode is the default, the first load against
> a backend that is not up now shows an **error**, not a healthy demo. This
> is deliberate — the same error message used to exist but was never
> triggered.

### 2.5.1 The admin panel — access and roles

The panel is served from the same static server as terminal 2, in the `admin` subdirectory:

```
http://localhost:8080/admin/
```

It reads the backend address from that same `web/js/config.js`, so if the analyst UI
works, the panel works.

**But your analyst key does not open it** — and this is deliberate. An ordinary key
has no admin level at all.

#### Three roles, not one

| Role | What it sees / does |
|---|---|
| `admin` | Read only: dashboard, health, the audit-log summary. Changes nothing |
| `operations` | Key lifecycle, domain knowledge, eight of the nine config files, the triage queue |
| `security` | `denied_columns`, `schema.yaml`, the warehouse connection string, and granting both roles |

The rule that decides every future case: **anything that changes who
sees what data belongs to the security admin.** The rest belongs to operations.

#### The first admin comes from `.env`, not from the web

This is a decision, not a limitation: to grant a role through the API you must already have the
`security` role. So the first time it has to come from the environment.

Since 4.7.0 each of the three roles has a flag — `--admin`, `--operations`, `--security` —
and `--full-admin` grants all three together. For a single-operator setup:

```powershell
python -m scripts.issue_api_key --id admin-1 --name "Admin" --full-admin
```

And when you really use the separation of roles:

```powershell
python -m scripts.issue_api_key --id ops-1 --name "Operations" --admin --operations
python -m scripts.issue_api_key --id sec-1 --name "Security" --admin --security
```

> **`--admin` on its own is not enough, and this is the most common confusion in this section.**
>
> The panel's thirteen sections are spread over several access levels:
>
> | What is required | Which panel sections |
> |---|---|
> | `admin` | Audit, deployment checks, query cache, domain configuration |
> | `operations` **or** `security` | Maintenance, feedback, schema drift, dimension vocabulary, quality in real use, per-analyst usage, failed authentication attempts |
> | `operations` | Keys and access |
> | `security` | Access requests |
>
> So a key that has only `--admin` opens a panel in which **9 of its 13
> sections give 403** — which looks like a broken deployment, not like a
> decision someone took when issuing the key. The script itself now warns
> about this combination, so you do not have to wait for the first login.

If you prefer to write it by hand, this is the form:

```ini
API_KEYS_JSON=[{"id":"admin-1","name":"Admin","key_sha256":"<64 hex>","admin":true,"operations":true,"security":true}]
```

All three must be **JSON booleans**. If you write `"security":"false"` (a string,
not a boolean) the server does not start and says why — deliberately, because a non-empty string in
Python is truthy and that typo would silently grant the security role.

> **If you have already started the server once with this same `--id`,** the old key was
> imported into the application database, and a new key with the same id but a different hash causes
> `AmbiguousKeyIdentityError` and the server does not start. Either give a new `--id`
> or revoke the conflicting key.

Restart the server, then in `http://localhost:8080/admin/` put the **raw key**
— not the `key_sha256`. The same trap explained in section 2.2 is
here too, and here it is even more likely, because you have just edited the admin's JSON entry
and it is in front of your eyes on the screen.

If the panel gives `401`, you used the hash. If **all** sections give `403`, the key
is right but has no role. If **some** sections load and some give `403`,
the key has a role but an incomplete one — the table above says which role is missing.

> **In a real deployment do not combine these three on one key.** They are separate so that
> someone who edits the synonyms cannot also change `denied_columns`. A key that has
> all three makes that separation invisible to whoever reads the log
> later. For the initial setup on your own machine it does no harm.

#### After the first admin

From then on you do not need to touch `.env`. Since 4.8.0 the **«کلیدها و دسترسی‌ها»** ("Keys and access")
section of the panel does the same jobs without a terminal:

| Job | Which role is required |
|---|---|
| Issuing a new key | `operations` |
| Disabling / enabling a key | `operations` |
| Permanently revoking a key | `operations` |
| Changing a key's denied columns | `security` |
| Granting or removing a role | `security` |

Since 4.5.0 keys are in the database — so **revoking a key is immediate**, not
after the next restart. For a key that has leaked, "tomorrow morning" is not an answer.

> **A key issued from the panel starts with "all columns denied".**
> It authenticates but no query works for it until a security admin
> opens its restriction with the «ستون‌ها» ("Columns") button. This is deliberate — issuing a key
> is `operations`' job and deciding who sees what data is
> `security`'s — but if you do not know it, you have handed over a key that looks
> broken. The panel itself says this when it issues.

> **The raw key is shown only once, on that one page.** It is neither stored
> nor viewable again — exactly like the CLI.

> **"Disabling" is reversible; "revoking" is not.** Revoking archives the row
> and never deletes it, so that restoring the database to yesterday does not bring a
> leaked key back to life. That is why its button asks you to
> type the key id, not just press confirm.

> **A role that came from `API_KEYS_FILE` (or `API_KEYS_JSON`) cannot be removed from the panel.** The
> environment's capabilities are added on every reload, so deleting the database row
> does nothing. Up to 4.7.0 this case was reported as "success" and changed nothing;
> now you get 409 and the message says you must remove the flag from the same place (the key file or `.env`)
> and restart the server.

### 2.5.2 Reading the «انحراف شِما» ("Schema drift") and «تازگی واژگان ابعاد» ("Dimension vocabulary freshness") cards

These two cards are read-only and apply nothing themselves; both need `operations` or `security` (the vocabulary's «بازخوانی» ("Reload") button needs only `operations`). Because the schema-drift card reads the database catalogue, it loads only when the panel is opened and with its own refresh button (the result is cached for `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS`), not on the 30-second timer.

**Schema drift.** It compares `schema.yaml` with the live catalogue of each source and shows, in this order:

- *جدول در منبع دادهٔ دیگری است* ("the table is in another data source"): a table whose columns are not all in the source assigned to it but which another source has; with the source it is not in, the source it was found in and the value you should write (`datasource: inventory` or `datasource: [sales, inventory]`).
- *فقط در انبار داده* ("only in the data warehouse"): a table or column the warehouse has and `schema.yaml` lacks; it is not queryable for now, because the guard rejects everything outside the allowlist.
- *فقط در schema.yaml* ("only in schema.yaml"): a table or column that `schema.yaml` has and the warehouse no longer has; a query that uses it fails at run time.
- *نوع ستون تغییر کرده* ("column type changed"): the column's type has changed since the last run of this check; the first run has no baseline and says so.

A table that is in several sources is compared separately on each, and a column missing from one copy appears as `Table.Column [source]`. Fixing the findings means editing `schema.yaml`: the `operations` key drafts, the `security` key approves, and the guard picks it up at the next start-up.

**Dimension vocabulary freshness.** For each prefetched column (`prefetchable_columns` in `schema.yaml`) there is a row: `table.column`, status, the number of values, the time of the last update and a «بازخوانی» ("Reload") button.

- *تازه* ("fresh"): fetched within `DIMENSION_VOCABULARY_TTL_SECONDS` (default 3600 seconds).
- *کهنه* ("stale"): older than that; it is still used and the next question that needs it starts a background reload.
- *هرگز* ("never"): never fetched. A question that names this dimension cannot be checked against its values, so the answer is not filtered with that value and the analyst gets a warning, until a reload succeeds. The same question also starts a background reload (after a failure, at most one automatic attempt per minute), so a transient error fixes itself; «بازخوانی» tries at that moment. With `DIMENSION_VOCABULARY_WARM_ON_STARTUP=true` (the default) this status is seen only after a failed warm-up, which appears in the log and does not stop the server from starting.
- *آخرین تلاش ناموفق* ("last attempt failed"): the last attempt, automatic or manual, failed; press «بازخوانی» and read the message, which is the error of the database of that table's source itself.

With several sources, the column is read from one copy of its table: the default source if the table is there, otherwise the first source in `datasources.yaml`. A hall or commodity that was added to the warehouse and never reloaded silently loses value detection; that is why the card exists.

### 2.6 What you have in the web that the CLI lacks

- **Multi-turn conversation.** "Of those, which had the most orders?"
  is built on the previous turn as a CTE, rather than reading the whole
  warehouse again.
- **Declared assumptions.** Every assumption the system made (which measure, which period,
  which scope) is shown as a chip and can be edited.
- **The conversation list.** The right-hand sidebar: new conversation, moving, renaming,
  deleting. After a reload you come back to where you were.
- **Memory.** On every editable chip there is a 📌: press it so that preference
  also applies in later conversations. **It is created only by pinning** — it is never
  guessed from repetition.
- **Chart or table.** The output form is chosen from the types of the result's columns,
  and the table is always one click away.

### 2.7 An old conversation you open has no numbers

This is deliberate. **Result rows are never written to disk** — the question, the SQL and the column
names are stored, the rows themselves are not.

The reason is that a stored row cannot be re-checked against a changed ACL:
a user's `denied_columns` may add a column *after* that row was written, and
no guard work at query time would catch this because no query
runs.

So a restored turn shows the question, the SQL and the real row count with a
**«دوباره اجرا کن»** ("Run again") button instead of the numbers. If you do not want this file to exist at all:
`SESSION_STORE_PATH=""`.

### 2.8 When you get an error

| What you see | What it means |
|---|---|
| `ModuleNotFoundError: No module named 'api'` | You ran the backend from `web/`. Run it from the repository root |
| `localhost:8000` shows nothing in the browser | Correct — the UI is on 8080. Start terminal 2 |
| All three lights are red but the CLI works | Almost always CORS. Section 2.3.1 |
| All three lights are red and a red banner at the top lists two likely causes | The address of the page is not in `CORS_ALLOWED_ORIGINS`, or the API is down. The server log has the line `CORS: origin '...' is not allowed` with the `.env` line to add — section 2.3.1 |
| Every admin card says «به API نرسید؛ پیام بالای صفحه را ببینید.» ("Could not reach the API; see the message at the top of the page") and a banner says the address is not the API | The API address saved in the browser (or in `web/js/config.js`) is the address of the static server itself. Type `http://<host>:8000` in the top bar and press «اتصال» ("Connect") — section 2.5 |
| The server log has `OPTIONS /health ... 400 Bad Request` and nothing else | A browser at an origin that is not allowed; the `CORS: origin '...' is not allowed` line next to it says which — section 2.3.1 |
| Only the LLM light is red but the CLI answers | Hover over the light — the tooltip gives the reason. If it says "does not implement /models", since 4.1.2 it no longer turns red |
| `Failed to fetch` or `TRANSPORT_ERROR` | Either the backend is not up, or your origin is not allowed in `CORS_ALLOWED_ORIGINS` |
| The UI says the backend is unreachable | Terminal 1 is not up, or the port differs |
| Key rejected (401) and what you typed is 64 hex characters | **You typed the hash, not the raw key.** The raw key is 43 characters — section 2.2 |
| Key rejected (401) | The key is wrong or its hash is not in `API_KEYS_FILE` (or `API_KEYS_JSON`). After changing the key file or `.env`, restart the backend. Since 4.10.0 a saved key is **not cleared** — if the server has just restarted, just ask again |
| `LLM_OUTPUT_TRUNCATED` | The model hit the token cap before generating the SQL — section 2.9 |
| Step 5 ("Interpretation", «تفسیر») never gets ticked | The «تفسیر نتیجه» ("Interpret the result") checkbox under the question box is off — section 2.8.1 |
| «جوابی نگرفتم» ("I got no answer") / `EMPTY_SQL_RESPONSE` on a reasoning model | Almost always the same token cap. If your version is older than 4.10.0 you see this code and not the one above — section 2.9 |
| The server does not start, with `Invalid API key configuration` | The `API_KEYS_FILE` file is missing or its JSON is broken (the message gives the line and column), or you set both `API_KEYS_FILE` and `API_KEYS_JSON` — section 2.2 |
| The server does not start, with `Invalid configuration` and a list of "line N" | `python-dotenv` could not read those `.env` lines, or a variable appears twice with different values. If you wrote `API_KEYS_JSON` over several lines, the whole value must be in single quotes and without an apostrophe, or use `API_KEYS_FILE` — section 2.2 |
| 429 with a number and a time | The rate limit; the message itself gives the quota, the window and the wait time |
| «این بک‌اند از v2 پشتیبانی نمی‌کند» ("This backend does not support v2") | The backend is old; update it to the current version |
| I am looking for the «نشانی بک‌اند» ("Backend address") field and it is not there | Removed since 4.4.0. Edit `web/js/config.js` — section 2.5 |
| The page shows an error instead of the demo | Correct: since 4.4.0 live mode is the default, so a backend that is not up now really gives an error. `?live=0` gives the old demo |
| `Application database unreachable ... (APP_DB_URL resolved to ...)` | Since 4.5.0. If you have set `APP_DB_URL`, the database must already exist. Leave it empty so that it creates SQLite itself — section 0.2 |
| The server says the application database and the warehouse are the same | Exactly as it says: `APP_DB_URL` and `DB_CONNECTION_URL` (or, if you have `datasources.yaml`, one of its sources) point to the same place. It deliberately does not start |
| `/admin/` says 403 | Your key has no role. Issue it with `--full-admin` and restart — section 2.5.1 |
| Some `/admin/` sections load and some give 403 | The key has only `admin`. `operations`/`security` is needed too — the table in section 2.5.1 |
| The «کلیدها و دسترسی‌ها» ("Keys and access") section gives 403 | This section needs `operations`; the card itself says which flag you lack |
| A new key you issued from the panel cannot run any query | It was issued with "all columns denied". Open it with the «ستون‌ها» ("Columns") button — section 2.5.1 |

### 2.8.1 Interpreting the result — the switch next to the question box

Under the question box there is a checkbox: **«تفسیر نتیجه»** ("Interpret the result"). Turn it on so that every answer also has a
simplified summary in your own language.

**It is off by default, and this is deliberate.** Building that summary means **up to 20
rows of the real result are sent to the language model**. With a local model this is exactly
what this product promises; but the decision belongs to the person who
asked the question, not to the whole deployment — which is why it is a per-analyst checkbox and not a setting
in `.env`. Your choice is saved in that same browser.

If your model is remote, the data-governance gate stops it and, without
`LLM_ALLOW_REMOTE=true`, sends no rows — and in the log, at `ERROR` level,
it says how many rows it did not send and why.

> **Up to 4.12.0 this capability did not exist at all on the conversational path.** The fifth
> step, "Interpretation", in the list of steps stayed "pending" forever, because the engine never
> produced an interpretation. It went unnoticed while none of the five steps ticked;
> from 4.11.0, when the other four steps started to tick, the fifth
> became conspicuous.

---

### 2.9 A reasoning model and «جوابی نگرفتم» ("I got no answer")

If the UI says it got no answer and your model reasons — Qwen3, DeepSeek-R1,
gpt-oss and the like — it is almost always the **output token cap**, not the model, not
the prompt, not the schema.

These models **think first, then answer.** The default cap
`LLM_NUM_PREDICT` is 512 tokens. For an SQL of a few dozen tokens
that is generous; for a model that first writes a few hundred tokens of reasoning it is not. The model
spends the entire budget thinking and **is cut off before writing the first letter of
SQL**.

#### How to be sure this is it

In the audit record (`logs/audit_log.jsonl`) look for these four **together**:

| Field | Value |
|---|---|
| `finish_reason` | `length` |
| `completion_tokens` | Exactly equal to `LLM_NUM_PREDICT` |
| `reasoning_detected` | `true` |
| `generated_sql` | `""` |

All four together mean exactly this. Since 4.10.0 the system detects this itself and
returns the code `LLM_OUTPUT_TRUNCATED` instead of `EMPTY_SQL_RESPONSE`, with a message that
gives the fix — and it no longer retries three times pointlessly (that repetition
used to make each question take about 17 seconds to reach a failure that was certain
from the start).

#### Two fixes

**The first way — turn reasoning off (better).** It is faster and cheaper, because
reasoning tokens cost money and time anyway. In `.env`:

```ini
LLM_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}
```

This form is for **Qwen3 on vLLM or SGLang**. Each server has its own
spelling:

| Server | Value |
|---|---|
| vLLM / SGLang (Qwen3) | `{"chat_template_kwargs":{"enable_thinking":false}}` |
| Ollama | `{"think":false}` |
| OpenAI | `{"reasoning_effort":"low"}` |

Whatever you put here is sent to your server **verbatim**; the system does not know
what it means and does not change it.

> **Be sure to confirm that it took effect.** Servers usually **silently ignore**
> a field they do not recognise. After the restart ask a question and see in the audit
> record that `reasoning_detected` has become `false` and `completion_tokens`
> has come down. If it is still `512` and `length`, your server does not
> recognise that field — go to the second way.

**The second way — raise the cap.** It always works, but you pay for the
reasoning tokens on every request:

```ini
LLM_NUM_PREDICT=2048
```

For a reasoning model, 2048 to 4096 is a reasonable range.

> A bad value in `LLM_EXTRA_BODY` gives an error **when the server starts**, not
> at the first question — so `python -m scripts.verify_deployment` catches it before
> the analysts do. It must be a JSON **object**, and it may not set
> `model`, `messages`, `temperature`, `top_p`, `seed`, `max_tokens`,
> `stop`, `stream` or `n` — those have their own settings.

---


## Part 3 — Without an LLM and a real database

For a demo or a UI test, with no infrastructure at all:

```powershell
python -m scripts.dev_v2_demo_server
```

It brings up the same real FastAPI app on `http://localhost:8000`, but
on an in-memory SQLite and a fake model. Start the static file server next to it
and open:

```
http://localhost:8080/
```

(Since 4.4.0 the page comes up live by itself and `web/js/config.js` has the default
`http://localhost:8000`, so no parameter is needed.)

This is a manual verification tool; it is not a substitute for a test against a real model and database.

## Part 4 — Upgrading from 6.0 to 6.16.4

A checklist for an installation that is on any 6.x release and is moving to 6.16.4. It puts the "Upgrading" notes of releases 6.0.1 through 6.16.4 one after another, in the order in which they are to be done, together with what 6.7.0 and 6.9.0 added and you may want to use (releases 6.6.1 and 6.7.0 have no separate "Upgrading" note). The full text of each release is in `CHANGELOG.md`, and this same checklist, in more detail, is in section 17 of `docs/deployment-runbook.md`. If you are coming from 5.x, do the 6.0.0 notes first (copy `prompts/system_prompt.md` to `<PROJECT_CONFIG_DIR>/system_prompt.md` before the first run).

Each step names the release it comes from; skip the steps of releases you are already running. An installation on **6.7.0** does steps 1 to 3, then 9 to 32, then the parts of step 33 that are new in 6.9.0, and, if it writes code, step 34; a 6.8.0 installation does steps 1 to 3 and 11 to 32; a 6.9.0 installation does steps 1 to 3 and 15 to 32; a 6.9.1 installation does steps 1 to 3, step 14 if it has never run `scripts/sync_schema.py` (the same run also records the column types that the 6.9.2 note in step 3 is about), step 15 if the console-encoding workaround is still in place, and steps 16 to 31; a 6.9.2 or 6.9.3 installation does steps 1 to 3, steps 16 to 30 and the restart in step 31 (6.9.3 changed the template of no configuration file); a 6.10.0 installation does steps 1 to 3, steps 17 to 30 and the restart in step 31; a 6.11.0 installation does steps 1 to 3, steps 18 to 30 and the restart in step 31; a 6.12.0 installation does steps 1 to 3, steps 19 to 30 and the restart in step 31; a 6.13.0 installation does steps 1 to 3, steps 20 to 30 and the restart in step 31; a 6.14.0 installation does steps 1 to 3, steps 21 to 30 and the restart in step 31; a 6.14.1 installation does steps 1 to 3, steps 22 to 30 and the restart in step 31; a 6.14.2 installation does steps 1 to 3, steps 23 to 30 and the restart in step 31; a 6.15.0 installation does steps 1 to 3, steps 24 to 30 and the restart in step 31; a 6.15.1 installation does steps 1 to 3, steps 25 to 30 and the restart in step 31; a 6.15.2 installation does steps 1 to 3, steps 26 to 30 and the restart in step 31; a 6.16.0 installation does steps 1 to 3, steps 27 to 30 and the restart in step 31; a 6.16.1 installation does steps 1 to 3, steps 28 to 30 and the restart in step 31; a 6.16.2 installation does steps 1 to 3, steps 29 and 30 and the restart in step 31; a 6.16.3 installation does steps 1 to 3, step 30 and the restart in step 31.

1. Back up `.env` and `project_config/`. If you already have a golden set (6.7.0 or later), record its baseline right now, on the release that is running and before `git pull` (section 5.2): the gate compares the new release against the baseline recorded on the old release, and after `git pull` the old release is no longer there.
2. `git pull`, then `pip install -r requirements.lock` again (every upgrade starts with this). Releases 6.8.0 through 6.14.2 changed no pin in `requirements.lock`; 6.15.0 moves `certifi` to 2026.10.19 for a published advisory. Run the command every time.
3. **Before restarting**, run `python scripts/verify_deployment.py`. Several releases now reject inputs that they used to accept silently:
   - A duplicate key in a YAML file (6.3.1; the message gives the key name and both line numbers; remove the duplicate, the one that came later was the one in effect; since 6.9.3 the message lists all the duplicate keys of the file together: `N duplicate keys: 'X' (lines A, B, C); ...`, at most 20 entries and then `... and M more`).
   - An unusable `.env` line or a duplicate field inside a key entry (6.4.0; a multi-line `API_KEYS_JSON` must be in single quotes and contain no apostrophe, or move to `API_KEYS_FILE`).
   - 6.8.0: the `project_config/ loads` check now reads all ten required files (before, six). A missing file, such as `retrieval_hints.yaml`, `session_policy.yaml`, `memory_policy.yaml` or `system_prompt.md`, fails with the message `N of 10 required files not found ...` and all the names together; copy and edit each from `project_config.example/`.
   - 6.8.0: a scoped `denied_columns` entry that names nothing real fails the `API key authentication` check with `API key configuration is invalid: <source>[<n>].denied_columns: ...` and the name of that entry (step 9).
   - 6.9.0: the `Schema structure matches the databases` check and the column types. A `schema.yaml` written before 6.9.0 has no `column_types:` map, so the sync has a type to record for every column, although nothing changed in the databases. For that reason 6.9.0 and 6.9.1 failed the check, with a message in which everything was zero (`0 column(s) missing from schema.yaml; 0 column(s) and 0 table(s) no longer in the database; ...`). Since 6.9.2 a type that is not yet recorded is not falling behind: if nothing else differs, the check is `PASS` and its detail ends with `N column type(s) not recorded yet -- run python scripts/sync_schema.py once to record them (review schema.synced.yaml, replace schema.yaml), so later type changes are detected`. Recording the types is step 14, and it comes before the restart. For real drift (a column the database has and the file lacks, a type that differs, a column or table the database has lost, a `datasource:` line that should be set) the check still fails, and `N column type(s) to record` is added to the counts. (If a database cannot be read, the check is `skipped`, not failed.)
   - 6.11.0: a raw draft in place of the configured files. Copying the output of `database.schema_inspector_cli` over `project_config/` loads and the server starts, but it answers worse: the drafts list every table of one database (staging and change-capture tables included), describe every table with a placeholder, quote live row values and leave `ring_aliases` empty. The preflight now fails `project_config/ is curated, not a raw draft` (with the names of the files that carry the `# AUTO-GENERATED DRAFT` header and the share of placeholder descriptions), `Aliases are curated` and `Every data source is described` (a database whose tables are gone), and gives advice about staging tables and sample values in descriptions. Restore the configured files and add the new tables with `python scripts/sync_schema.py --add-tables 'Schema.*'` (section 0.3.3). `STAGING_PATTERNS` in `.env` decides what counts as a staging table. With several data sources, a `schema.yaml` with no `datasource:` line also fails `Every data source is described` and names the second source: run `python scripts/sync_schema.py`, which writes the lines (step 17).
4. Check `schema.yaml` (6.2.0): if a table name is repeated in several schemas, write each with a schema-qualified key (`sales.Customer:`, `ref.Customer:`) and give every table with a plain key a `db_schema`.
5. (Optional) Put the password raw in `DB_PASSWORD` and remove it from `DB_CONNECTION_URL` (6.3.0).
6. (Optional, recommended for more than one key) Move the keys to a file: copy `project_config.example/api_keys.example.json` to `project_config/api_keys.json`, replace each entry's `key_sha256` with the hash printed by `issue_api_key`, set `API_KEYS_FILE` and **remove** `API_KEYS_JSON` (6.4.0 and 6.6.1).
7. (If needed) Set `API_HOST=0.0.0.0` and `CORS_ALLOWED_ORIGINS` for a UI that is served from anywhere other than `localhost:8080` (6.0.2).
8. (Several databases only) Do section 0.3.3 of this guide in order: `datasources.yaml` and one `DB_PASSWORD_*` per source (6.1.0); `description:` and `keywords:` for each source (6.5.0, optional); the `datasource:` lines and the report's "missing" columns, with the sync of step 14 (from 6.5.0 they were written by `assign_datasources.py`; from **6.9.0** that is the job of `sync_schema.py`); and, once step 14 has replaced `schema.yaml`, `python scripts/prompt_budget.py` and the `PROMPT_RETRIEVAL_TOKEN_BUDGET` setting it prints (6.6.0). If the DBA requires it, put `nolock: true` on that source and on every other source that needs it; this key is per source (6.5.0).
9. **Scoped and "join-only" `denied_columns` (6.8.0).** A plain name such as `NationalID` means what it always meant: denied everywhere, every reference is rejected. Two jobs and one thing to know:
   - For every key, check `denied_columns` (the key file, `API_KEYS_JSON` and the key editor in the admin panel) for any entry that contains `.` or `:`. An entry's name can no longer contain either: such an entry is now read as scoped, and if it does not name a real source, table or column the server does not start and `API key authentication` in the preflight fails with the name of that entry. Correct it or remove it. A misspelt scoped entry is rejected in the same way, rather than silently restricting nothing.
   - (Optional) Use the new forms where you want a column to stay usable in joining tables but have its value hidden: `schema.Table.Col`, `Source:schema.Table.Col` or `Source:Col` (section 2.2). A "join-only" column can only be one side of `a.col = b.col` inside `JOIN ... ON`; anywhere else the guard rejects it with the correctable reason `join_only_column`. The model is told in one line of each question's section which columns are "join-only", so the fixed cached prefix does not change. The key editor in the admin panel shows the new forms, accepts them and validates them on save.
   - Know that the result cache is invalidated once: `scope_key` now hashes `denied_columns` as a JSON array, so after the upgrade all cached results are stored under the new key. Do not clear anything by hand.

   "Join-only" hides the column's value, not the existence of the row. Also restrict the foreign keys that point to the hidden key (section 2.2).
10. **Leftovers of the setup wizard (6.8.0)**; only if you have run `setup_project.py` so far or copied its settings from `.env.example`:
    - Delete every `.setup_log.json` written by an older version (by default `project_config_draft/.setup_log.json`; releases before 6.13.0 wrote it to `project_config/.setup_log.json`, or under the `--output` folder you gave), and if the file was shared or committed, change the database password. Older versions stored the connection string in it exactly as typed, password included; the wizard now writes it masked (section 0.3.1).
    - If you copied the old values of `.env.example` (`gpt-oss-20b` and the empty address that means `https://api.openai.com/v1`) into `.env`, empty `WIZARD_LLM_MODEL` and `WIZARD_LLM_BASE_URL` so that they follow `OPENAI_MODEL` and `OPENAI_BASE_URL`; the wizard prints one line saying which endpoint and model it used. The wizard now also refuses a remote endpoint unless `LLM_ALLOW_REMOTE=true`.
11. **Table selection on the retrieval path differs by default (6.9.0).** Eleven new `RETRIEVAL_*` settings decide which tables the retrieval path puts in the prompt (they are listed in section 5.4). `RETRIEVAL_EXTRA_TABLES` defaults to `3`, and `RETRIEVAL_JOIN_EXPANSION` and `RETRIEVAL_PRUNE` default to `true`. Only a source that is on the retrieval path is affected (its schema is larger than `PROMPT_RETRIEVAL_TOKEN_BUDGET`; the line `Prompt path for data source '<name>'` in the start-up log says which path each source has); a source on the fixed prefix gets the same prompt as before. If you accept the new selection, there is nothing to do. To get the previous selection back, put these three in `.env` and restart the server:

    ```ini
    RETRIEVAL_PRUNE=false
    RETRIEVAL_JOIN_EXPANSION=false
    RETRIEVAL_EXTRA_TABLES=0
    ```

    To see whether the change helps on your own questions, measure table selection before and after the upgrade with `python -m eval.cli recall` (section 5.4).
12. **Start-up now sends one small request to the model server for each data source (6.9.0).** `LLM_PREFIX_WARMUP_ON_STARTUP` defaults to `true`: a background thread sends each source's fixed prefix once (`max_tokens=1`) so that the first question after a restart does not pay for the whole prefill (part 7). For a source on the retrieval path, with `LLM_PROVIDER=mock`, or for an endpoint that is not trusted while `LLM_ALLOW_REMOTE` is off, it sends nothing, and it helps only when the model server caches the prefix (`vllm serve --enable-prefix-caching`). If the model server is shared and you pay per token, or start-up must send no traffic to the model at all, set `LLM_PREFIX_WARMUP_ON_STARTUP=false`. `LLM_PREFIX_WARMUP_TIMEOUT_SECONDS` (default `180`) bounds this whole step and must be greater than zero, or `Settings.validate()` fails in the preflight. After a separate restart of the model server, `POST /admin/llm/warmup` (the `operations` capability) does the same thing on the spot. `LLM_STREAM_TIMINGS` (default `false`) stays off until you turn it on; nothing else is needed.
13. **Regenerate a schema draft that may quote the password (6.9.0).** The drafts written by `database/schema_inspector.py` (`python -m database.schema_inspector_cli`: `schema.yaml`, `entities.yaml`, `aliases.yaml`, `relationships.yaml`) begin with the comment `# Generated: <time> from <url>`. Earlier versions masked the password in that URL with a pattern that left part of a password containing `@` or `:` visible and never masked a `PWD=` inside the `odbc_connect` value. If such a draft was shared or quoted, and that line is still in a copy under `project_config/`, regenerate the draft (or remove that line), and if the password was exposed, change it. The same masking now also covers the rejection messages in the application database and the preflight output.
14. **Sync `schema.yaml`'s structure with the databases (6.9.0, every installation).** `scripts/sync_schema.py` is now the way to keep it current (the full rules are in section 0.3.3 and in section 16.3 of the runbook). It reads each source's catalogue (three `INFORMATION_SCHEMA` queries per source; names and types only, no rows) and never rewrites `schema.yaml`. In order:
    1. `python scripts/sync_schema.py --dry-run` prints the report and writes nothing. Read it.
    2. `python scripts/sync_schema.py` writes `project_config/schema.synced.yaml` next to `schema.yaml` (your own file with all its comments and descriptions, plus the `datasource:` lines with several databases, the columns the database has and the file lacks as `TO BE FILLED` drafts, the `column_types:` map, and a marker on what the database lacks) and `project_config/relationships.proposed.yaml` (not written with `--no-relationships`; nothing reads it, so review each entry and copy the correct ones by hand).
    3. Before replacing, read the `== prompt size ==` section of the report: for each source, the estimated tokens of the prefix of the current file and of the proposal, the path each takes, and the number of added columns and tables that still carry a draft description. A `WARNING:` line means the proposal moves that source from the fixed prefix to the retrieval path. It is advice only and does not change the exit code.
    4. `diff` the two files, copy `schema.yaml` to `schema.yaml.bak` and put the proposal in its place. Resolve what the report marked (`# not in database (sync_schema.py)`, `# not found in any data source`) (fix the spelling, remove, or `--prune`) and write the `# TO BE FILLED` descriptions.
    5. `python scripts/sync_schema.py --check` writes nothing; it must print `CHECK OK: schema.yaml's structure matches the databases` and end with code 0. As long as the types are not recorded (a file from before 6.9.0 has no types; the replacement in step 4 records them), the line ends with `N column type(s) not recorded yet -- run python scripts/sync_schema.py once to record them ...`: the code is still 0, and after the replacement that note is gone. Run it again whenever you want to know whether the databases have moved.

    `scripts/assign_datasources.py` stays for pipelines that already call it; it only writes the `datasource:` lines. If the prompt size section showed a source near or above the budget, run `python scripts/prompt_budget.py` again after the replacement.
15. **You can remove the console-encoding workarounds (6.9.1).** If a script or task sets `PYTHONIOENCODING=utf-8` or `PYTHONUTF8=1` or runs `chcp 65001` only so that Persian output does not break, remove it. Every command-line program now writes UTF-8 itself; leaving them in place is harmless. Nothing else is needed.
16. **Confirmed answers and the quality card in real use (6.10.0).** No setting was renamed, no file's template changed and `requirements.lock` has no new pin. Three jobs or decisions before restarting:
    - (Only for an application database managed with Alembic) The new `turn_confirmations` table is created at start-up automatically, like the other application-database tables, and the default installation has nothing to do by hand. If you manage the application database with Alembic, run `alembic upgrade head` from the repository root; it applies migration `0005_turn_confirmations`.
    - Tell the analysts and review their confirmations: under each successful answer they now see the «پاسخ درست است» ("The answer is correct") button (section 5.1.1). Any analyst can confirm their own answers, and there is no separate permission for it. A confirmed answer becomes a reviewed golden case (tag `origin:confirmed`) and `python -m eval.cli verify --accept` only checks that its SQL runs, not that it is right; so a wrong confirmation enters the accuracy gate (step 33). Before accepting, check the `origin:confirmed` rows in the output of `scripts/golden_sheet.py export`, or, until you trust the confirmations, run `harvest_golden.py` with `--no-confirmations`.
    - (Optional) Put `LLM_STREAM_TIMINGS=true` in `.env` if you want the admin panel's «کیفیت در استفاده‌ی واقعی» ("Quality in real use") card (part 7) to show the time to first token and the split of the `llm` stage.
17. **Table selection on real questions, the new preflight checks and the warm prefix (6.11.0).** No setting was renamed, no file's template changed and `requirements.lock` has no new pin; there are two new optional settings, `STAGING_PATTERNS` and `LLM_PREFIX_KEEPWARM_SECONDS`. Five jobs or decisions before restarting:
    - Fix what the new preflight checks say (step 3). With more than one data source and no `datasource:` line in `schema.yaml`, `Every data source is described` fails for the second source: run `python scripts/sync_schema.py`, which writes the lines, review `schema.synced.yaml`, replace `schema.yaml` and run the preflight again (section 0.3.3). A raw draft of `database.schema_inspector_cli` copied over the configured files fails `project_config/ is curated, not a raw draft`: restore the configured files and add the new tables with `python scripts/sync_schema.py --add-tables 'Schema.*'`. The other two new checks only advise.
    - The fixed-prefix template changes this once: `prefix_version` and the key of every stored response change once, so the stored answers of 6.10.0 are not found and the model server's prefix cache is cold for the new text. After the restart, call `POST /admin/llm/warmup` (or let `LLM_PREFIX_WARMUP_ON_STARTUP` do it). A source whose prefix estimate was up to about 2% above `PROMPT_RETRIEVAL_TOKEN_BUDGET` may now move from the retrieval path to the fixed prefix; if a source is close to this budget, run `python scripts/prompt_budget.py`.
    - If `entities.yaml` or `retrieval_hints.yaml` name tables by short name, retrieval may choose other tables: a short name is now resolved to a `schema.yaml` key, and the tables it names reach the prompt. Run `python -m eval.cli recall --golden eval_data/golden.jsonl` before and after the upgrade (section 5.4). A short name that has a table in several schemas is resolved in the other schemas' names in the same file; if that schema is not the one you mean, write it with the schema (`sales.Customer`).
    - (Recommended on a model server that others use too) Set `LLM_PREFIX_KEEPWARM_SECONDS=300` and restart (part 7). With `0` (the default) nothing is sent; the value must be `0` or at least `30`. If this deployment is the only user of the model server, if the server does not cache the prefix, or if all sources are on the retrieval path, leave it at `0`.
    - (Optional) Add `--enable-prompt-tokens-details` to the command that runs vLLM so that `prefix_cache_hit` tells the truth: the server then reports `cached_tokens` and the audit field follows it. Without it the field stays the old estimate based on the prompt size (`prefix_cache_hit_source` says which) and part 7 explains how to read it. The correction round for an unknown column or table becomes a few tens of tokens longer; there is nothing to set.
18. **A response cache in front of the model, and accuracy rounds (6.12.0).** No setting was renamed, no file's template changed and `requirements.lock` has no new pin (`git diff v6.11.0 -- requirements.lock` is empty); there is one new optional setting, `LLM_CACHE_SUSPECT_MS`. Four jobs or decisions before restarting:
    - Run `python scripts/verify_deployment.py`, and if `Model endpoint answers each prompt itself` fails, fix the gateway before trusting the answers or running an accuracy round (step 3). This check sends the model two small requests that differ only in one random token, through the server's own backend. A failure means the response cache in front of the model returned another prompt's answer, and it can serve the stored SQL for a similar question: turn off response caching for this client at the gateway, or point `OPENAI_BASE_URL` (and `WIZARD_LLM_BASE_URL`) straight at the model server, and run the command again until it is `PASS` (part 7). It is `SKIP` under `LLM_PROVIDER=mock` and, for a remote endpoint, while `LLM_ALLOW_REMOTE` is off; in the admin panel it is a deep check (section 0.4).
    - (Optional) Set `LLM_CACHE_SUSPECT_MS` if its default does not suit you. The default is `50`, and `0` turns it off. When a *different* question receives byte-identical SQL faster than this value, the server writes one `WARNING` per streak and puts `suspected_cached_response: true` in the `llm` block of the audit record. It blocks or changes no request, and a reader of the log that ignores an unknown key sees no effect (part 7).
    - Record the evaluation baselines again (if you have a golden set). `python -m eval.cli run --live` now generates through the server's own production pipeline (the guard, the correction rounds, the row cap), so its accuracy is what the analyst sees, and it is higher than before. A baseline or share bundle recorded with 6.11.0 or older is single-shot: `--baseline` and `compare` refuse it against a production run (`compare --allow-mixed` allows it with a note). After the restart on 6.12.0, record a fresh one in the same mode in which you want to build the gate (`--live --reference live --save-baseline ...`), and add `--single-shot` to any command that should measure only the model; that run is still compared with the baseline of step 1. Offline runs (CI) have not changed. This run applies no key's `denied_columns` policy; the commands for accuracy rounds with a reviewer (`--share`, `compare`, `split`) are in section 5.5.
    - Run the wizard again only on an endpoint that failed the check. Steps 3 and 4 now stop with `Step 3 stopped` and exit code 3 when the model gives the same answer for three tables in a row; once the endpoint is fixed, `--resume` continues, and `--allow-repeated-answers` overrides the stop for a schema so small that identical answers are natural (section 0.3.1).
19. **Schema exclusion as configuration, and the setup tools' new defaults (6.13.0).** No setting was renamed, no file's template changed and `requirements.lock` has no new pin (`git diff v6.12.0 -- requirements.lock` is empty); there is one new optional setting, `EXCLUDE_SCHEMAS`, and one new optional key in `datasources.yaml`, `exclude_schemas:`. Both are read only by the setup tools and the preflight, so the answers the server gives do not change. Four jobs or decisions before restarting:
    - Run `python scripts/verify_deployment.py` and read the new check `No table in an excluded schema` (step 3). It never fails. A `[PASS]` line that starts with `advice:` gives the count and the first keys of the `schema.yaml` tables that sit in a schema every one of their sources excludes: remove the table, or narrow the exclusion. A plain `[PASS]` means no table does, and `[SKIP]` means nothing is excluded beyond the system schemas and no table matches. It reads files only (section 0.4).
    - (Optional) Decide on `EXCLUDE_SCHEMAS` and `exclude_schemas:`. Both are empty by default, and then only the system schemas are skipped (`sys`, `INFORMATION_SCHEMA`, `guest`, `cdc`, the nine fixed `db_*` role schemas by their exact names, `pg_catalog`, `pg_toast` and `pg_*`, in any letter case; a user schema called `db_sales` is still scanned). `EXCLUDE_SCHEMAS` in `.env` takes comma-separated `fnmatch` patterns and applies to every source; `exclude_schemas:` in `datasources.yaml` takes a list for one source. The inspector, `sync_schema.py` and the wizard skip what they match, and a `schema.yaml` table that is already listed is synced as before. A table in `cdc` or in one of the fixed role schemas, which `sync_schema.py` used to report, is not reported any more. An `exclude_schemas:` that is not a list of non-empty strings makes `datasources.yaml` fail to load (section 0.3.3).
    - Fix the scripts and habits that run the wizard (section 0.3.1). `python setup_project.py` now writes to `project_config_draft/` (git-ignored): review the drafts and copy the files you accept into `project_config/` yourself. A script that expected `project_config/` to be filled needs a copy step, or `--output project_config --overwrite-live`; without `--overwrite-live` the run stops with exit code 2, and with it each file the wizard replaces is first copied to `<name>.bak-<YYYYmmdd-HHMMSS>`. A second run merges into the first (`--fresh` starts from scratch), and a bare table name that several schemas share is re-keyed as `Schema.Table`: read the line `Re-keyed in ...`. Without `--db-url`, the wizard now reads every source of `datasources.yaml` through the application's own login (`--datasource` narrows it to one). A `.setup_log.json` from an earlier version is read as before, and `--resume` generates once again.
    - Fix the scripts that run the inspector (section 0.3.2). `python -m database.schema_inspector_cli` without `--db-url` reads the sources of `datasources.yaml` (or `DB_CONNECTION_URL`) with the application's own login, so a draft lists only what the application can query. It no longer reads `DATABASE_URL`: pass `--db-url` (one source, read with the login in the URL; a line says the application's login may see less). The drafts no longer hold a sample value or a staging table: `--sample-values` writes `sample_values.review.yaml`, and `--include-staging` keeps the staging tables.
20. **Intervals and paired comparison in every report, and an optional benchmark track (6.14.0).** No setting was renamed, no file's template changed and `requirements.lock` has no new pin (`git diff v6.13.0 -- requirements.lock` is empty); there is no new setting. The change is in the accuracy tool (`python -m eval.cli`), so the answers the server gives, the audit log and the admin panel do not change. Four jobs or decisions before restarting:
    - Read the reports with their intervals (sections 5.3 and 5.5). The text report now prints a 95% interval (Wilson) and the number of cases under the overall accuracy and at the end of every per-tag and per-source line, and marks a group of fewer than 20 cases `n<20: not interpretable`; `compare` adds the paired test (how many cases were fixed, how many regressed, the exact McNemar p-value and a 95% interval of the difference). All of it is added and nothing was removed or renamed: a baseline, `--out` report or share file recorded with 6.13.0 or older still loads, `--baseline` still checks against it and `compare` takes it on either side, so there is no need to record the baselines again for this release. A script that reads the per-tag or per-source lines of the text report by column has to allow for the interval at the end of the line; read the JSON instead.
    - Check your golden set and the scripts that run it (section 5.1). The golden file has a new optional status, `excluded`, and a new optional field, `match`; a set built from your own questions uses neither, and an existing `golden.jsonl` needs no edit. `run` now prints a `Not scored: N of M case(s) -- ...` line under the report whenever the file holds a case that is not `active` (a pending one too, which used to be skipped silently), and a golden file that the tools rewrite carries `"exclusion_reason": null` and `"match": "fingerprint"` on every line, which 6.13.0 also reads.
    - Plan for the extra queries of the determinism probe (section 18.3 of `docs/deployment-runbook.md`). `run --live --determinism` now also executes and judges each repeat, through the guard and the read-only executor as `run` does, so one probe sends up to cases times repeats (3 by default) extra queries to the data sources. On a large golden set, run it when the warehouse is quiet.
    - (Optional) Decide on the benchmark track (section 5.6). Nothing runs it unless you call `bench-import` or `bench-run`. If you use it: download BIRD mini-dev yourself; the load takes its own administrator login (`--target-url`, or `env:NAME` to keep the password off the command line) on a database of its own, while the application keeps its read-only login and must never be given the right to create anything; the database must be case- and accent-sensitive (`COLLATE Latin1_General_100_CS_AS_SC_UTF8`), and the import refuses any other before it writes anything unless you pass `--allow-ci-collation`, in which case every report says so; and its number is not the deployment's accuracy, as the first line of its report says. It does not replace the golden set.
21. **Say which browser origin is refused, and when the API address is wrong (6.14.1).** No setting was renamed or added, no file's template changed and `requirements.lock` has no new pin (`git diff v6.14.0 -- requirements.lock` is empty). The server's only change is one log line, so the answers it gives, the audit log and the CORS policy are as in 6.14.0. Four things to read or do before restarting:
    - Know the new log line (section 2.3.1). When a request arrives from an origin that `CORS_ALLOWED_ORIGINS` does not hold, the server writes one `WARNING` for that origin, once for each origin while the process lives, for a preflight and for an ordinary request alike: `CORS: origin '...' is not allowed (allowed: ...)`, together with the `.env` line that allows it. It means a browser was opened at an address the list does not hold, not that the server is unwell. At most 64 origins are remembered.
    - List every address the page is opened with (section 2.3.1). `CORS_ALLOWED_ORIGINS` replaces the default list, and the IP address, the host name and `localhost` are three different origins, so write each one people use, comma-separated, in `.env` before the restart. The API's own address needs no entry: a page served by the API itself is same-origin.
    - Run the new preflight check (section 0.4). `Web UI origin is allowed by CORS` is one of the twenty-four checks the preflight has with one source (it made them twenty-two in 6.14.1; step 22 adds two). It reads the settings only. Set `VERIFY_UI_ORIGIN` to the address as the browser's address bar shows it (`http://172.16.101.42:8077`) and it passes or fails on that address, printing the line to add; run it once for each address people use. Without it the check never fails and only advises. On the admin panel, `VERIFY_UI_ORIGIN` has to be set in the server's own environment.
    - If an admin card says «به API نرسید؛ پیام بالای صفحه را ببینید.» ("Could not reach the API; see the message at the top of the page") (section 2.8). The address saved in the browser is the static server's, not the API's, and a banner at the top now says so; every card used to show its own 404. Type `http://<host>:8000` in the address bar at the top of the admin panel and press «اتصال» ("Connect"), or open the page once with `?base=http://<host>:8000`. The analyst page shows the same banner and names `web/js/config.js` (`DEFAULT_BASE_URL`).
22. **One entity per table, no row values in aliases, and drafts caught by the preflight (6.14.2).** No setting was renamed or added, no file format changed and `requirements.lock` has no new pin (`git diff v6.14.1 -- requirements.lock` is empty). One template changed: `project_config.example/entities.yaml` now names real keys of the template's own `schema.yaml`. The answers the server gives, the audit log and the CORS policy are as in 6.14.1; the changes are in the setup wizard, the schema inspector and the preflight. Four things to read or do before restarting:
    - If you ran the wizard of 6.13 or 6.14, check what it wrote (sections 0.3.1 and 0.4). Its drafts passed the preflight and answered almost every question about one source with `OUT_OF_SCOPE`: two entities for almost every table, `aliases.yaml` full of raw values from the rows, an empty `examples.yaml`. Run `python scripts/verify_deployment.py` on the folder you deployed (or on the drafts, with `PROJECT_CONFIG_DIR` pointing at them). The preflight now has twenty-four checks with one source (twenty-two before): `Entities match schema keys` lists the entities that name no table of `schema.yaml` and the tables that have two, and `Aliases hold no raw data values` counts the synonyms copied from rows. Fix `entities.yaml` and `aliases.yaml` (one entity per table, under the key `schema.yaml` uses, with the aliases of both united; `ring_aliases` keys are ring names, never table keys: see step 29), or restore the curated files from a backup or from version control and add new tables with `python scripts/sync_schema.py --add-tables 'Schema.*'`. Running the new wizard again over the same output folder does the merge for you and prints what it merged.
    - Remove the value synonyms, and read `aliases.yaml` for names (section 0.3.1). The merge of the new wizard drops every `synonyms` key of an earlier `aliases.yaml` that is a date, a time, a phone number, an IP address, a masked value, a long digit string, an address, a hash or an error message, and counts them per shape in the summary and in `.setup_log.json`, never listing a key (`--keep-existing-synonyms` keeps them). A key that is a person's name has no shape the check can recognise: read the file yourself and delete such keys. The values were in the prompt, and in git if the file was committed; treat a committed copy as having published them. Copy the cleaned `aliases.yaml` into `project_config/` yourself; the wizard never writes there on its own.
    - A preflight that passed on 6.14.1 can fail on this version (section 0.4). `project_config/ is curated, not a raw draft` now sees a draft that has lost its header line: it fails when most table descriptions are only the key, the bare name or `Schema.Table`, or most column descriptions are only `<type> column`, with the counts, and the wizard's own header, `# AUTO-GENERATED by setup_project.py`, counts as a draft header like the inspector's. It is the same check telling you a file is a draft: restore the curated files, add tables with `sync_schema.py --add-tables`, and describe the ones you add. An `entities.yaml` copied from the older template and never filled in fails `Entities match schema keys` too, because `Entity1` is not a table of `schema.yaml`.
    - Fix the scripts that run the wizard (section 0.3.1). It builds no synonym from a row value unless you pass `--value-synonyms`; it makes one model call for each data source for the examples, and says so, in the summary and in the file, when the model gave none; and it drafts a value or volume rule only for a fact table that has a numeric column that is not a key. It still writes to `project_config_draft/` unless you say otherwise.
23. **Aliases kept from a looping answer, no rules for dimensions, and descriptions drafted for review (6.15.0).** No setting was renamed or added, no file format or template changed, and `requirements.lock` moves one pin, `certifi` 2026.10.19, for a published advisory (`git diff v6.14.2 -- requirements.lock` shows that line only; step 2 installs it). The preflight still has twenty-four checks with one data source; one of its messages changed. The answers the server gives, the audit log and the CORS policy are as in 6.14.2; the changes are in the setup wizard, a new setup command and the preflight's advice. Three things to read or do before restarting:
    - Tables the wizard left with no alias (section 0.3.1). A model that repeated the same phrases until `max_tokens` cut it off left JSON that never closed, and the table got 0 aliases (`Could not extract valid JSON after retry`). The alias step now keeps the complete phrases written before the cut, at most eight, and asks once more only when nothing is usable. Run the wizard again over the same output folder: the merge keeps what you reviewed, the tables that had none get their aliases, and the step line says `salvaged N from a truncated answer`. The alias call sends its own `max_tokens` of 320 unless `LLM_NUM_PREDICT` is above 512 or `LLM_EXTRA_BODY` is set (the two ways to make room for a reasoning model). Review the result and copy `entities.yaml` into `project_config/` yourself, as before.
    - Dimension tables get no value rule (section 0.3.1). A table classed `dim`, one in a schema named like a dimension (`..._Dim`, `lookup`), or a lookup in shape (one key and one or two text columns) is no longer sent to the model for a value or volume rule. The summary lists those tables apart from the fact tables that really have no measure. If an earlier `business_rules.yaml` holds rules the old wizard drafted for such tables, delete them.
    - A `schema.yaml` whose descriptions are only names (sections 0.3.4 and 0.4). If `project_config/ is curated, not a raw draft` fails because most table descriptions are only the table's name, or most column descriptions are only `<type> column`, and there is no curated file to restore, run `python scripts/draft_descriptions.py` (`--dry-run` first lists what it would ask; `--tables 'Schema.*'` limits a first try). It drafts only those descriptions with the wizard's model, from names, types, keys and relationships (no row value unless `--samples`), and writes `schema.described.yaml` beside `schema.yaml`. Read every line marked `# REVIEW`, replace `schema.yaml` with it (keep `schema.yaml.bak`) and delete its first line: until that line is gone the check fails on purpose. A remote endpoint is refused unless `LLM_ALLOW_REMOTE=true`, as for the wizard.
24. **Column restrictions that cannot silently fail open, and an audit that names the caller (6.15.1).** No setting was renamed or added, no file format changed and `requirements.lock` has no new pin (`git diff v6.15.0 -- requirements.lock` is empty). Every `api_keys` file and every panel key that works today still works. The preflight now has twenty-five checks with one data source. Three things to do before and after restarting:
    - Run the preflight and read `Column restrictions name real columns` (section 0.4). It fails, naming the key id and showing the entry with `ascii()`, for a plain `denied_columns` name that matches no column of `schema.yaml` (a typo, or a name from another database): correct or remove each one. Its PASS detail may start with `advice:` and list columns that probably hold the same data under another name (`CustomerNationalID` or `NationalCode` beside a denied `NationalID`); add the ones that must stay hidden.
    - Read the log once after restarting. An entry with a space or an invisible character (a non-joiner, a direction mark) at either end, easy to paste from a Persian editor, used to deny nothing; it now denies the column it names, and one WARNING per key names the key id and shows the entry with `ascii()`. Clean the file so the warning stops. A question such a key could ask before may now be refused: that is the restriction the entry always asked for. Characters inside a name are kept as written, so a Persian column name that contains a non-joiner stays denied.
    - Check which key the web UI uses. The audit log now fills `principal_id` for every question asked in the web UI (earlier records keep it empty). A key issued with `--full-admin` and no `--denied-column` has no restriction at all: give analysts their own keys.
25. **Money only gets Rial, identifier columns are not charted, and single tables can be excluded (6.15.2).** No setting is renamed, no file format changes and `requirements.lock` has no new pin (`git diff v6.15.1 -- requirements.lock` is empty). Three new optional keys, all ignored when absent: `column_units:` per table in `schema.yaml`, and `exclude_tables:` and `keep_staging_schemas:` per source in `datasources.yaml` (plus the setting `EXCLUDE_TABLES`). The SQL the server writes, the guard and the audit log are as in 6.15.1. What to look at:
    - The answer summary writes Rial/ریال only for a column that holds money. It decides from the column's name (price, value, amount, fee, ارزش, قیمت, مبلغ, کارمزد are money; count, quantity, volume, weight, id, code, date are not); where a name misleads it, write `column_units: {Quantity: none, TotalPrice: rial}` under that table in `schema.yaml`.
    - A two-column answer whose number is an identifier (`ID`, `Ring_ID`, `RingID`, `Code`, `کد`, `شناسه`) is shown as a table, no longer as a chart. Other shapes are unchanged.
    - To skip single tables when the setup tools discover tables, list them per source as `exclude_tables: ["Sales_Fact.Order"]` (fnmatch on `schema.table`). To scan a staging schema without its change-capture tables, list it under `keep_staging_schemas:`; the `table:` patterns of `STAGING_PATTERNS` (`*_Inserts`, `*_Updates`, `*_Deletes`) still drop those tables. Singular names (`*_Insert`) are not in the default patterns: add them to `exclude_tables:`. Nothing already in `schema.yaml` is hidden.
26. **A filter value that does not exist can get one correction round (6.16.0).** No setting is renamed, no file format changes and `requirements.lock` has no new pin (`git diff v6.15.2 -- requirements.lock` is empty). One new optional setting, `FILTER_VALUE_CHECK`, off by default: until you turn it on, every request, prompt, SQL statement, audit line and preflight list is as in 6.15.2. Decide whether to turn it on:
    - What it does. With `FILTER_VALUE_CHECK=true`, a statement that compares a dimension column with a string that is not one of that column's cached values (for example a hall name the model invented) gets one extra correction round: the model is told the value does not exist, shown the nearest real values, and told to drop the filter if the question names no value of that column. If the model keeps the value, or anything else goes wrong, the original statement runs as before, so the check never refuses an answer.
    - What it needs. The values come from the dimension vocabulary cache, so only columns listed under `prefetchable_columns` in `schema.yaml` are checked (typically the name column of each small dimension). With the setting on, the preflight check `Filter value check has vocabularies` lists dimension tables whose label columns are not listed.
    - How to decide. Turn it on after you have a golden set (Part 5): record a baseline with it off, run again with it on, and keep it on only if the comparison is better. The audit record gets a `value_check` block when the check found something to compare; it holds the column and a hash of the value, never the value itself.
27. **Real values for the analysts' memory and the default scope (6.16.1).** No setting is renamed or added, no file format changes and `requirements.lock` has no new pin (`git diff v6.16.0 -- requirements.lock` is empty). Memory and the default scope behave at run time exactly as in 6.16.0. The preflight now has twenty-six checks with one data source, and the new one can fail on a configuration that runs:
    - Run the preflight and read `Session and memory policies use real values` (section 0.4). It fails when `memory_policy.yaml` or `session_policy.yaml` still holds the template's sample options (`Ring A`, `Ring B`, `All rings`), or names a table or column that `schema.yaml` does not have. With the template's options an analyst cannot pin any real value, and the clarification offers made-up ones.
    - Draft the real values with `python scripts/draft_policies.py` (section 0.3.4.1): `--dry-run` first, then without it. It reads the scope column's distinct values with one bounded, read-only query through the application's login and writes both files to `project_config_draft/`, changing only `options`. Read them and copy them into `project_config/`. Leave `--table` alone unless you mean to change the scope dimension: the key the scope is stored under is the one the question matching uses.
28. **Filter values spelled the way the column stores them, and attached plurals matched (6.16.2).** No setting is renamed, no file format changes and `requirements.lock` has no new pin (`git diff v6.16.1 -- requirements.lock` is empty). One new setting, `FILTER_VALUE_SPELLING`, is on by default; `FILTER_VALUE_SPELLING=false` restores 6.16.1 exactly. What to know:
    - A filter value the model writes with Persian ی/ک, where the column stores Arabic ي/ك (or the other way round), is rewritten to the stored value before the statement runs, when exactly one stored value differs from it only in letter forms. Only columns listed under `prefetchable_columns` in `schema.yaml` have the cached values this needs: add the name column of each small dimension there. No query is added and no model is called. The audit record gets `value_spelling` with the column names, never the value.
    - A Persian word with an attached plural (`...ها`, `...های`, also written with a non-joiner) is matched as its stem as well as itself, so a table, alias or trigger phrase named in the singular is found. Table selection can therefore change for such questions; on the synthetic benchmark it got better for 25 of 1,440 questions and worse for none.
29. **`ring_aliases` holds ring names, not tables (6.16.3).** No setting is renamed or added, no file format changes and `requirements.lock` has no new pin (`git diff v6.16.2 -- requirements.lock` is empty). The setup wizard of 6.13 to 6.16.2 wrote one `ring_aliases` entry per table (key = table key), so a question naming a hall could be filtered on a table name (`WHERE r.[Name] = N'Ring'`) and come back empty. What to do:
    - Run the preflight. `Aliases are curated` now fails when `ring_aliases` keys are `schema.yaml` table keys, and the server ignores such entries at start-up with one WARNING. A curated file whose keys are real ring names behaves exactly as before.
    - Put real ring names back: restore your curated `aliases.yaml` (keys = the exact values of the ring table's name column), or run the wizard again over the same output folder, which drops the table-keyed entries and keeps their aliases in `entities.yaml`, then draft ring entries with `python scripts/draft_policies.py --ring-aliases` (section 0.3.4.1). Read the drafts before copying them into `project_config/`.
30. **«پاسخ نادرست است» on the answer; a re-asked turn is followed (6.16.4).** Web interface only: no setting is renamed or added, no file format changes, no endpoint changes and `requirements.lock` has no new pin (`git diff v6.16.3 -- requirements.lock` is empty). What to do:
    - Tell the analysts: beside «پاسخ درست است» under each answer there is now «پاسخ نادرست است». It opens the existing wrong-answer form (category and optional note) and the report is sent from there, to the same place as before (the admin panel's feedback card).
    - Reload the page in the browser after the upgrade (Ctrl+F5) so the new script and stylesheet are used. After a clarification is answered, the page now moves to the new answer at the foot of the conversation.
31. Restart the server (all the changes above, including `datasources.yaml` and `schema.yaml`, take effect on restart) and run the preflight one more time, this time with `VERIFY_API_KEY`. Since 6.9.0, each source on the fixed prefix also writes an `LLM prefix warm-up` line to the log a little later (part 7).
32. After the first day, look at the new audit fields (6.5.0 and 6.9.0): a reader that does not know `datasource_selection` (6.5.0) or the fields of the `llm` block, namely `ttft_ms`, `generation_ms`, `reasoning_tokens` and `reasoning_tokens_estimated` (6.9.0; the first two are filled only with `LLM_STREAM_TIMINGS=true`), can ignore them, and older records are still read. `python scripts/analyze_audit_log.py` prints the "LLM stage split" section if the records have it. And know what changed on screen, because analysts will ask: since 6.5.0 the SQL shown in the conversation is laid out with a fixed template (display only; the executed statement did not change), and a rejected statement can be opened in the same template.
33. (6.7.0, optional but recommended) **Start measuring accuracy.** 6.7.0 added tools that build an evaluation set and an upgrade gate from real use: `python scripts/harvest_golden.py` (candidates from the audit log), `python scripts/golden_sheet.py export` and `import` (the analysts' review in Excel), `python -m eval.cli verify --accept` (running and activating cases) and `python -m eval.cli run --live --reference live` (execution accuracy against the reference SQL, in the same run and on the same data). These do not exist before 6.7.0, so an installation older than that records its first baseline on 6.7.0 or later, once the audit log holds real questions; from then on you record a baseline on the current release before every upgrade, and compare afterwards (section 5 of this guide has all the commands). A baseline written by an older release still loads but cannot be compared with a `--reference live` run; the tool refuses and says how to record it again. 6.9.0 added two optional things to the gate: absolute accuracy floors, `EVAL_MIN_ACCURACY` and `EVAL_MIN_SOURCE_ACCURACY`, which are off until set (section 5.2), and `python -m eval.cli recall`, which measures table selection without a model and without a database (section 5.4). Apart from this, 6.7.0 changes nothing in how the server runs.
34. (Only for someone who writes code) **Install `requirements-dev.txt` (6.9.0).** It pins `ruff` to an exact version, and CI now has a job called `lint` that fails on `ruff check .`. Before a push, run `pip install -r requirements.txt -r requirements-dev.txt` and `ruff check .`. A deployment that does not change this code does not need that file.

These need no action of their own: 6.0.1 (`DB_POOL_PING_IDLE_SECONDS`, default `60`), 6.3.2 (one warning per key instead of one per read of the key), 6.6.1 (the template of step 6) and the CI change in 6.7.0 (the `requirements.lock` you install is now the one CI tests too).

---

## Part 5 — Measuring accuracy on real data, and the upgrade gate

Until you have a set of your own real questions, you have no number for the system's accuracy on your data warehouse; the samples in `eval_data.example/` are made up. The "golden set" is a list of questions for each of which an analyst has confirmed the correct answer. The full details are in section 18 of `docs/deployment-runbook.md`; here is a summary.

**The set stays on the server only.** The `eval_data/` folder (ignored by git) holds real questions, SQL and result rows. Do not take its files, or the JSON report of a run (which holds each case's question and SQL), off the server. The commands below print only **counts and case ids**, and what they print is safe; only `--include-examples` in step 1 prints a few questions word for word, and you must use it knowingly. The only file made to leave the server is the "share file" of section 5.5.

### 5.1 Building the set

```powershell
# 1. About 150 candidate questions from the audit log (stratified sampling: data source, result, language)
python scripts/harvest_golden.py --n 150

# 2. A review sheet for the analysts (CSV with a BOM; Persian opens correctly in Excel)
python scripts/golden_sheet.py export

# ... the analysts fill in eval_data/review.csv in Excel ...

# 3. Bring the verdicts back
python scripts/golden_sheet.py import

# 4. Run the reference SQL on the database, record the result, and activate the sound cases
python -m eval.cli verify --golden eval_data/golden.jsonl
python -m eval.cli verify --golden eval_data/golden.jsonl --accept
```

- **Step 1:** questions are normalised and de-duplicated (the difference between ي/ی and ك/ک, Persian/Arabic/Latin digits, half-spaces (zero-width non-joiners) and spaces is ignored). The SQL the model proposed is only a *suggestion* and is no verdict. The file `eval_data/candidates.jsonl` is never overwritten without `--force`, and result rows are never copied into it.
- **Steps 2 and 3:** the analyst fills the `verdict` column with `correct` (the suggestion is right), `wrong` (put the SQL of the `correct_sql` column in its place) or `skip` (this question is no use); empty means "not reviewed yet". The `expect` column is one of `success` / `empty` (the correct answer is zero rows) / `out_of_scope` (the system should refuse; empty SQL). A filled-in example: `eval_data.example/golden_review_template.csv`. Every SQL goes through the SQL guard and errors are reported with the **sheet row number**.
- **Step 4:** `verify` runs each reference SQL read-only and with the application's own executor (routing, `NOLOCK`, the timeout), reports the errors (a guard rejection, a database error, an empty result where `expect` wants success) and only with `--accept` makes the cases without a problem `active`. The file is rewritten atomically and the previous version stays with the extension `.bak`.

### 5.1.1 The easiest way to grow the set: analysts confirm the answer as they work

The Excel sheet above needs a review session separate from the analysts' working time, which is why a golden set usually does not get built. The web interface now makes this possible without a separate session. Under every answer whose query ran and returned a result, right under the table or chart, there is a two-state button: **«پاسخ درست است»** ("The answer is correct"); it is visible on the answer itself and you do not have to open anything. Beside it is **«پاسخ نادرست است»** ("The answer is incorrect"): it sends nothing by itself, but opens the closed «جزئیات» ("Details") drawer and the form of the flag for an incorrect answer, «این عدد درست نیست؟» ("Is this number wrong?"), with its category and optional note; the report is sent from that form. After a report is sent the button reads «✓ گزارش شد» ("Reported"). A rejected, out-of-scope or failed answer does not have this button. One click records the confirmation and another click withdraws it. A confirmation keeps only identifiers (session, turn, the confirming user's id, the request id and the time), not the question, not the SQL and no result data. A turn that the guard rejected, that was out of scope or that failed cannot be confirmed (the server answers `409`). If you confirm an answer you had flagged, the flag is removed, and if you flag a confirmed answer, the confirmation is removed: the last action wins.

`harvest_golden.py` reads the confirmations by default. So the way into the golden set is the familiar one, with the difference that the confirmed answers have already been reviewed:

```powershell
python scripts/harvest_golden.py --n 150
#    -> first the confirmed answers (status "reviewed"), then a sample of the
#       "pending_review" candidates. It prints counts only, including
#       "confirmations: read N, included M (...)".
python scripts/golden_sheet.py export
#    -> rows that are already confirmed have verdict correct, and their note
#       says who confirmed and when; the analyst only changes the ones they
#       disagree with to wrong or skip and reviews the rest as before.
python scripts/golden_sheet.py import
python -m eval.cli verify --golden eval_data/golden.jsonl            # look first
python -m eval.cli verify --golden eval_data/golden.jsonl --accept   # then activate
```

- **Status.** A confirmed answer becomes a case with `status = "reviewed"`: a person has confirmed `expected_sql` (the very SQL that ran) and `eval.cli verify` has not run it yet. It is not `pending_review` (that would force someone else to judge again), and it is not `active` (that would bypass the gate). `verify --accept` is still the step that runs the SQL on the database, records the rows and activates the case; until then the regression gate rejects it.
- **Origin.** The case's `notes` give the id of the confirming analyst, the time of the confirmation and the source audit record, and it carries the tag `origin:confirmed`. These are in the files and in `review.csv` (which stay on the server); the console output is counts only.
- **They are not dropped by `--n`.** Confirmed cases come before the stratified sample and are not subtracted from `--n`: `--n 150` means all the confirmed ones plus at most 150 sampled, and no question is in the file twice.
- **No duplicates.** Two confirmations for one question (under the same Persian/Arabic normalisation) are one case, with the latest confirmation. A question that is already in the set (`--exclude`) is skipped.
- **`--no-confirmations`** takes only the sample, as before, and does not touch the application database.
- **The audit log must still have that turn.** The question and the SQL come from the audit record; a confirmation whose record has gone with log rotation is counted (`no audit record`) and skipped; so harvest faster than the log rotates.
- **A click is a judgement, not proof.** An analyst who confirms an answer only because it looked right brings it into the set. `verify` catches SQL that no longer runs, or is empty where it should return data, and the sheet catches the rest.
- **Setup.** The `turn_confirmations` table is created, with the other application-database tables, the first time the server starts; a deployment that manages the schema with Alembic runs `alembic upgrade head` (revision `0005_turn_confirmations`). `scripts/migrate_app_db.py` moves it between backends with the rest.

Confirmations also go to the «کیفیت در استفاده‌ی واقعی» ("Quality in real use") card of the admin panel (part 7). There they are a **lower-bound signal, not accuracy**: analysts confirm only some of the answers, and an unconfirmed answer is not a wrong one. What measures accuracy is the golden set and `eval.cli run` (sections 5.2 and 5.3).

### 5.2 Before every upgrade

The warehouse data changes every day; a fingerprint recorded last month is not the answer to "yesterday's trades" today. That is why the gate runs each case's reference SQL **in the same run and on the same data** and compares the two results (`--reference live`):

```powershell
# On the current release, before the upgrade:
python -m eval.cli run --live --reference live --golden eval_data/golden.jsonl --save-baseline eval_data/baseline.json

# ... upgrade ...

# On the new release:
python -m eval.cli run --live --reference live --golden eval_data/golden.jsonl --baseline eval_data/baseline.json
```

The baseline also records the path that generated the SQL (`pipeline`, section 5.5): `--live` goes through the server's loop and `--live --single-shot` makes one model call. A baseline written before this field was single-call, so a production run against it is refused like the cases above; record it again.

Exit code `0` means no regression and `1` means a regression (accuracy, latency or guard rejections went past the threshold). Do not go to production on a `1` without understanding why. A baseline is compared only with a run that has the same `--reference`; an old baseline (without `--reference live`) is refused and the message says how to record it again.

**An absolute floor (optional).** The thresholds above only say "no worse than the baseline"; if the baseline itself was weak, or accuracy has gone down in small steps, they do not stop it. Two optional floors close this gap and are **off** until you give them a value:

- `EVAL_MIN_ACCURACY` or `--min-accuracy 90`: if the overall accuracy is below this percentage the run is refused.
- `EVAL_MIN_SOURCE_ACCURACY` or `--min-source-accuracy 80`: if the accuracy of any one data source is below this percentage (or no case has a `datasource` and there is no per-source number) the run is refused.

These floors do not need `--baseline`, being equal to the floor is accepted, and on a refusal the exit code is `1` and the message says which number is below which floor. The command-line flag takes precedence over the environment variable. Set them only for `--live` runs; an offline run is 100% by construction and clears any floor.

### 5.3 What the numbers mean

- **Execution accuracy**: the share of cases in which the generated SQL gave *the same result* as the reference. Two different SQL statements with the same result are both correct.
- **"The same result"**: rows are compared as multisets (duplicates are counted, order is not); **column names are ignored** (an alias does not matter) but the order of the columns as selected does matter; the order of rows matters only when the reference SQL has a top-level `ORDER BY` together with `TOP`/`OFFSET`; numbers are compared with a relative tolerance of `1e-6` (changed with `--float-tolerance`) and **integers are always exact**; `NULL` equals only `NULL`.
- **Accuracy per tag and per data source**, and **source-selection accuracy** (how often the system chose the source the case belongs to) when the cases have a `datasource`. These are descriptive and do not by themselves break the gate.
- The status `reference_error` means the case's own reference SQL was rejected or failed today (for example, a table was renamed); the fault is in the case, not in the model: run `eval.cli verify`.
- A run without `--live` (the one CI makes) replays the recorded answers and is 100% correct by construction, and is not accuracy; `--reference live` without `--live` is refused.

### 5.4 Table selection on its own: `eval.cli recall`

Before the model sees the question, retrieval decides which tables go into the prompt (when the schema is too large for the fixed, cacheable prefix; `PROMPT_RETRIEVAL_TOKEN_BUDGET`). A table that is not there will not appear in the SQL either, so this step can be measured without a model and without a database, from the same `golden.jsonl` (6.9.0; details in section 18.4 of `docs/deployment-runbook.md`):

```powershell
python -m eval.cli recall --golden eval_data/golden.jsonl
python -m eval.cli recall --golden eval_data/golden.jsonl --json --out recall.json
python -m eval.cli recall --golden eval_data/golden.jsonl --min-recall 0.9   # below this number, exit code 1
```

For each case, the tables that `expected_sql` reads (with the same table detection as the SQL guard) are compared with the tables that `ContextRetriever` selects. The report gives the mean **recall**, the share of cases for which **all** the tables came, the mean/median/maximum number of retrieved tables and the mean **precision** (the cost side: recall can be bought cheaply by retrieving more), **recall within budget** (a retrieval larger than the token budget counts as zero), the numbers for each tag and, for cases with a `datasource`, the source-selection accuracy. A case whose `expected_sql` names a table that the loaded `schema.yaml` does not know is listed as "skipped", not as an error. The text output prints only the case id and table names; `--json` and `--out` also hold each case's question, so keep that file on the server like the other files of this part. Use it to see whether a drop in accuracy comes from retrieval or from generation, and after any change to `schema.yaml`, `entities.yaml`, `retrieval_hints.yaml` or the `RETRIEVAL_*` settings.

Eleven `RETRIEVAL_*` settings (6.9.0) decide which tables the retrieval path puts in the prompt. They matter only for a source whose schema is larger than `PROMPT_RETRIEVAL_TOKEN_BUDGET`; a source on the fixed prefix gets the whole schema and ignores them. Unless `eval.cli recall` shows a reason, stay on the defaults; every change takes effect on restart.

| Setting | Default | What it does |
|---|---|---|
| `RETRIEVAL_EXTRA_TABLES` | `3` | Ranked tables added beside the tables named through an alias or a fact pattern. `0` brings back the previous behaviour. |
| `RETRIEVAL_EXTRA_SCORE_RATIO` | `0.5` | An extra table must score at least this fraction of the best table of its own kind. |
| `RETRIEVAL_JOIN_EXPANSION` | `true` | Adds, from the foreign-key graph, the tables the question needs for a join but did not name (a link table, the parent of a classification). |
| `RETRIEVAL_JOIN_MAX_HOPS` | `2` | The longest join, counted in foreign keys, that is bridged (`2` means one link table). |
| `RETRIEVAL_JOIN_MAX_ADDED_TABLES` | `4` | The most tables the join expansion adds to one question. |
| `RETRIEVAL_JOIN_MAX_HUB_DEGREE` | `10` | A table that more than this many tables refer to (such as a shared calendar) is never a stepping stone between two other tables. |
| `RETRIEVAL_INFER_RELATIONSHIPS` | `true` | For the join expansion only, a column named `<Table>_ID` or `<Table>ID` is taken as a key to that table's `ID` when `schema.yaml` and `relationships.yaml` did not declare the key. Nothing is written to the configuration. |
| `RETRIEVAL_PRUNE` | `true` | Drops a candidate that rests only on its description or on a column match much weaker than its best support, unless it is connected to a table with stronger evidence. |
| `RETRIEVAL_PRUNE_SCORE_RATIO` | `0.85` | A table with column-name evidence must score this fraction of the best score to stand on its own. `0` keeps all of them. |
| `RETRIEVAL_PRUNE_CONNECT_HOPS` | `2` | How many foreign keys may separate a weak candidate from a stronger one for it to survive. `0` turns the rescue off. |
| `RETRIEVAL_PRUNE_CORROBORATE` | `true` | Two weak candidates that are connected to each other keep each other. |

The selection from before 6.9.0 comes back with `RETRIEVAL_PRUNE=false`, `RETRIEVAL_JOIN_EXPANSION=false` and `RETRIEVAL_EXTRA_TABLES=0`. This same table, with the measured effect of each part, is in `docs/design/RETRIEVAL.md` section 6.

A Persian word written with an attached plural (`تالارها`, `تالارهای`, `تالارهایی`) is compared with its stem (`تالار`) as well as with itself, wherever the question is compared with aliases, `always_include` phrases, synonyms, table descriptions and the `keywords:` of the data sources; a question without such a word is matched exactly as before. `ان` is not stripped (`تهران`, `ایران`). There is nothing to set. Section 18.5 of `docs/deployment-runbook.md` and section 10 of `docs/design/RETRIEVAL.md` give the rules and the measured effect.

### 5.5 Accuracy rounds with a reviewer

The model and the data warehouse are only on your server, and the reviewer has no access to them and cannot run anything. A "round" is the way to work in exactly this situation: you run the evaluation on the server and send **one** safe file to share; from it the reviewer diagnoses the failures and sends back corrections that are generic (they say nothing about your data); you run again and compare the two files. A fingerprint only says that an answer *differed*; the share file says *how* (too few rows, the right rows in the wrong order, a number that is two percent off) without a single result value leaving the server. The full details are in section 18.6 of `docs/deployment-runbook.md`; here is a summary.

**1. Split the set in two, once:** a dev half that the reviewer works on, and a test half that nobody tunes a fix to.

```powershell
python -m eval.cli split --golden eval_data/golden.jsonl --out eval_data/split
```

Within each category (the case's first tag), cases are sorted by `id`; the even positions go to **dev** and the odd to **test**. Nothing is random and the order of the file does not matter. The command prints the counts and writes `dev_ids.txt`, `test_ids.txt` and `split.json`. Run it only **once**, before the first round, and keep the files: the test half is worth something only if it does not move. Running it again on the same file changes nothing; on a changed file it is refused unless you give `--force`, and `--force` means you are knowingly creating a new held-out set. A new case is in neither file; add its id to one of them by hand (to test only if the reviewer has not seen it). Each category should have at least two cases, or it has a dev case and no test case.

**2. Activate the cases whose reference SQL runs:**

```powershell
python -m eval.cli verify --golden eval_data/golden.jsonl            # look first
python -m eval.cli verify --golden eval_data/golden.jsonl --accept   # then activate
```

**3. Run the round** (`N` is the round number: 1, 2, ...):

```powershell
python -m eval.cli run --live --reference live --golden eval_data/golden.jsonl `
    --share eval_data/share/round-N.json --share-only-ids eval_data/split/dev_ids.txt `
    --out eval_data/round-N.report.json
```

**`--live` measures what the analyst gets.** It generates the SQL through the server's own loop (`SQLAgent`): the SQL guard, correctable rejections, up to the configured number of correction rounds with hints that name an existing column or table, source routing, the row cap that the server adds to a statement that has none (`DEFAULT_TOP_N`), and the executor's query timeout and `MAX_ROWS_RETURNED`. The reference SQL runs as written; so a question whose reference returns more than the row cap is scored the way the analyst sees it: a result with exactly the row cap's number of rows, all of whose rows are in the reference, is, after the count has been confirmed, **accepted** and called `match_capped` (the analyst saw the right rows, truncated only by policy), and any other subset stays `actual_is_subset` and fails. **One deliberate difference:** the run has no "caller", so no key's `denied_columns` policy is applied; a deployment whose keys deny columns is measured as if they did not. `--single-shot` makes only one model call and no correction (the old behaviour): a "model only" number, lower than the analyst's number, that is useful for seeing what the correction loop is worth. The mode is printed in the report, appears as `pipeline` in the `--out` report and as `run.pipeline` in the share file (`production` or `single_shot`); **a baseline (section 5.2) or `compare` across the two modes is refused**, because the difference comes from the production path and not from a fix. Keep one mode for all rounds. The offline `eval.cli run` (the CI's job) has not changed and no CI job runs `--live`.

**`--share-only-ids` keeps the content of the held-out half on the server.** With `eval_data/split/dev_ids.txt`, only the dev cases carry the question, the SQL, the error text and the diagnosis; every other case carries only `case_id`, `tags`, `passed` and `mismatch_class`, and the `summary` still counts all of them. The reviewer cannot tune to something the file does not have, and `compare --split test` still works on it. Use it in every round; without it the file has both halves in full.

`--share` without `--live --reference live` is refused with one line and exit code `2`, before anything is loaded or sent to the model (the diagnosis compares the generated result with the reference of the same run; an offline replay and a recorded fingerprint have no reference to compare with). The path must be outside the repository folder or under `eval_data/` (ignored by git); any other place in the repository (`docs/`, `eval_data.example/`, the root) is refused so that the file cannot reach a commit. The file is readable by the owner only and is written in UTF-8 with sorted keys and with cases sorted by id so that two rounds `diff` cleanly. The ordinary report (`--out`) is also written, and the options of section 5.2 work as before.

The SQL statements come into the file **unchanged**, with the filter and the value inside the filter, because a wrong filter cannot be diagnosed without seeing the filter. If the SQL itself must not leave either, add `--share-redact-literals`: every string and numeric literal in both SQL texts is replaced with a placeholder (`:str`, `:num`). This is done by parsing the SQL with sqlglot, not with a text pattern. The reviewer sees the structure of the query and not its filter, and can diagnose fewer failures; the header of the file says which mode was used.

**4. Look, then send that one file.** Open `eval_data/share/round-N.json` in an editor; the `privacy` block at the beginning says what is inside. Send **only** this file.

**Never send:** `eval_data/round-N.report.json` (`--out`: it holds unredacted error text, the fingerprint and the model's SQL), `golden.jsonl`, `candidates.jsonl`, `review.csv`, a baseline, any `.bak`, anything else from `eval_data/`, `.env`, `project_config/` and `logs/` (the full list is in part 6). Before sending, read the `question` fields: they are what your users wrote and they stay unchanged. If a question has the name of a person or a company, remove that case from the set and run the round again.

**Privacy: exactly what the share file holds.**

- **In it:** the header (the template version, the application version, `prefix_version`, the model name, the name and hash of the golden file, `PROMPT_RETRIEVAL_TOKEN_BUDGET`, each data source's prompt path (static prefix or retrieval), the start and end time, the `privacy` block); for each case, the id, tags, **question**, reference SQL, generated SQL, status, expected and selected data source, the reasons for the guard's rejections and the execution error **in every correction round** (the round number, the class name and the redacted message, so that a repeated rejection can be seen), the number of self-correction rounds, latency, error text, and a **diagnosis** computed on the server from the two result tables: a class (`match`, `row_count_differs`, `column_count_differs`, `column_names_differ`, `same_rows_different_order`, `values_differ_same_shape`, `actual_is_subset`, `actual_is_superset`, `actual_empty`, `expected_empty` or `not_executed`, plus `match_capped`), `capped`, the cap value, `uncapped_rows` and `reference_uncapped_rows` (a number) when a cap was applied, `capped_unverified`, the number of rows, the **column names** of both results and, for a single numeric cell, the relative-error bucket (`<1%`, `<10%`, `>=10%`); and a summary (overall execution accuracy and per tag and data source, the distribution of classes, correction rounds, p50 and p95 latency). With `--share-only-ids`, a case outside the list carries only the id, tags, `passed` and `mismatch_class`.
- **Changed on the way out:** in the error text and in each round's message, every quoted string is replaced with `'…'` and every run of four or more digits (Persian digits too) with `#`, and the text is cut at 600 characters; with `--share-redact-literals` the literals of both SQL texts are treated the same way.
- **Never in it:** a row or the value of any cell of either query, credentials, `.env` values, the address or host of the endpoint, an API key, a principal id, the notes (`notes`) and the recorded rows of a case. The model is named, not its address.
- **Two things stay readable and you should know it:** the questions, and the names of the result columns. A column name is part of the schema, but a `PIVOT` query turns data values into column names, and an alias can be whatever the model wrote; the diagnosis cannot tell the difference. If your SQL has a pivot, read the column names too.
- The golden file is identified by its base name and the SHA-256 of its text (line endings normalised), never by the path of its folder.

**The reviewer's rule.** See the failures of the **dev** ids and tune the fix to them, and use the **test** half only as a number. With `--share-only-ids` the file does not show the content of the test half; without it the discipline rests on the reviewer alone. A fix that raises dev and lowers test is overfitting, and `compare` shows it.

**How the fixes come back.** *Code* comes in a release; upgrade according to part 4, and if you also want the usual gate, record a baseline on the current release before that (section 5.2). *Configuration* comes as a reviewed patch to `business_rules.yaml`, `examples.yaml` or `aliases.yaml`; read it like any change to the prompt, because it is text the model sees on every request. Apply it either from the admin panel's «نسخه‌های پیکربندی» ("Configuration versions") (`POST /admin/config/versions`; every change is a version and `POST /admin/config/restore` rolls it back) or by editing the file by hand. Then run `python scripts/verify_deployment.py` and restart the server, because the files are read at start-up. A change to `business_rules.yaml` or `examples.yaml` changes `prefix_version` and the model server's prefix cache goes cold once (part 7); `POST /admin/llm/warmup` warms it. A patch is generic: it names the tables, columns and rules of your schema, never a value from a question. If it wins, send it back.

**Comparing with the previous round:**

```powershell
python -m eval.cli compare eval_data/share/round-1.json eval_data/share/round-2.json
python -m eval.cli compare eval_data/share/round-1.json eval_data/share/round-2.json `
    --split test --split-file eval_data/split/test_ids.txt --fail-on-regression
```

It reads the two files by `case_id` (the plain `--out` report works too) and prints only ids and counts, not questions, SQL or values: the cases that were **fixed**, **regressed**, **still failing**, **added** and **removed**; the accuracy before and after, overall, on the cases both rounds have, and per tag; and the shift in the count of each class. It needs no model, no database and no `project_config/`, so the reviewer can run it too. `--split dev|test` with `--split-file` limits the comparison to one half (the file is the `dev_ids.txt` or `test_ids.txt` of step 1). With `--fail-on-regression`, if a case was right in the previous round and is wrong now, the exit code is `1`; so `--split test --fail-on-regression` means a regression in the held-out half. Without that flag the exit code is `0`. **Two rounds that were made with a different production path (`run.pipeline`: `production` against `single_shot`) are refused with code `2`**; `--allow-mixed` compares them with a note saying that the differences include the effect of the production path. A file written for another golden file, or judged with another `--reference`, is compared with a note.

**When to stop.** The numbers that matter are measured on the **test** half and with the two unchanged halves of step 1; read them from the "Execution accuracy" and "Per-tag accuracy" lines of the output of `compare ... --split test`, and each source's number from the `summary` block of the share file.

- **Accuracy 7.5:** the execution accuracy of the held-out half (test) is **at least 75%**, with **no tag below 60%**, in **two consecutive rounds**. One good round is luck, or a fix tuned to those very cases; a second round with other fixes is not.
- **Accuracy 8** (the roadmap target): execution accuracy of **at least 80% on at least 150 cases**, with **every data source at 75% or more** (`summary.per_datasource`).

A tag that has few test cases jumps by tens of percent on one case: read each tag's denominator and, instead of ignoring its error, add cases to that tag (section 5.1). A set with fewer than 150 active cases reaches 7.5, not 8.

**What these numbers say and what they do not.** The default number is the execution accuracy of the path the analyst uses; so `corrections` are real, and a `guard_rejected` case means the correction rounds ran out. Read the file's `guard_rejections` round by round: one message twice means the correction text did not help; a new message in every round means the model is searching. What it does not cover: each key's `denied_columns`, the result cache and the interpretation step. **The row cap:** a question that asks for "all" gets the first rows and no more, like the analyst. A result with exactly the row cap's number of rows (the `TOP n` the server added, or `MAX_ROWS_RETURNED` if lower), fewer than the reference's rows, every row of which is a row of the reference (under the same rules as `compare`; for a reference with `ORDER BY ... TOP`, the first rows in the same order), is a candidate for `match_capped`, which only the count query below can confirm. A row that is not in the reference, or a result that ends before the cap, is not exempt. **A result that meets the conditions is confirmed with one more read-only query:** `SELECT COUNT(*) FROM (<the model's SQL before the cap was added>) AS q`, built from the syntax tree (an `ORDER BY` without `TOP`/`OFFSET`, a `WITH` clause, and repeated or unnamed columns, which SQL Server does not accept in a derived table, are removed, hoisted or named) and run with the same executor, so the query timeout and the rolled-back read-only transaction apply. A case stays `match_capped` only when this count equals the number of the reference's rows (`uncapped_rows` records the diagnosis). A query that filtered too much and returns only as many valid rows as the cap has a smaller count and is `actual_is_subset`, that is, a failure. If the count cannot be asked (the SQL does not parse into a derived table, the guard rejects it, time runs out) the case is **failed**, with `actual_is_subset` and `capped_unverified: true`; there is no silent pass. **When the reference result itself has reached `MAX_ROWS_RETURNED`**, its length is not its size; so the reference is counted in the same way (`SELECT COUNT(*) FROM (<reference SQL>) AS q`, the same builder, the same executor) and the two counts must be **equal** (`uncapped_rows == reference_uncapped_rows`; there is no "at least" rule). If the reference count cannot be asked, the case is `capped_unverified` and failed. If the model wrote its own `TOP` and only `MAX_ROWS_RETURNED` cut the result, the same check is made on its SQL as written. `--single-shot` has no cap, so `match_capped` never appears in it. Only counts are exported, never a row value. `--single-shot` gives the "model only" number; never compare it with a production round. How "the same result" is decided is in section 5.3.

### 5.6 The benchmark track (optional): a public benchmark beside your golden set

The golden set above is the only number that says how the system does on *your* questions and *your* schema. For the question a developer asks every week ("does this prompt, this model or this setting help?") it is small and private. Section 18.7 of `docs/deployment-runbook.md` describes a second, separate instrument: the public BIRD mini-dev benchmark (500 questions over 11 databases, released under CC BY-SA 4.0), loaded with an administrator login into a database of its own and run through the same production path with your model. Because the data is public, the full report can be sent as it is.

```powershell
python -m eval.cli bench-import bird --source D:\bird\MINIDEV --target-url env:BIRD_ADMIN_URL --out D:\bird\out --descriptions --with-evidence
python -m eval.cli bench-run bird --bench D:\bird\out --label model-a
python -m eval.cli compare D:\bird\out\runs\model-a\report.json D:\bird\out\runs\model-b\report.json
```

- **Its number is not your accuracy.** The questions are in English and about someone else's domain; the report says so in its first line, and it never replaces the golden set of this part.
- **Two logins.** The load creates schemas and tables, so it takes its own administrator login (`--target-url`); the application reads the benchmark database with its normal read-only login. Never give the application's login the right to create anything.
- **A case-sensitive database.** SQLite compares text case-sensitively; in a database that does not, `WHERE name = 'abc'` also matches `'ABC'` and a correct answer can differ from the reference. The import reads the database's collation and, unless you pass `--allow-ci-collation`, refuses one that is not case- and accent-sensitive.
- **The data is compared table by table.** After the load every table is compared with the SQLite file by content, not only by row count. Only the questions that read a table that differs, or whose reference fails or returns nothing, are excluded from the count, each with a reason, and `import_report.txt` lists them.
- **Read both accuracies.** Every percentage carries a 95% interval and its number of cases, and a group of fewer than 20 cases is marked "not interpretable". The report gives the accuracy on all scored cases and, beside it, without the cases where the T-SQL dialect itself cannot give the SQLite answer; `compare` adds the paired test (section 18.3 of the runbook) for two models or two settings.

---

## Part 6 — Sharing troubleshooting information safely

When something breaks they ask you for logs, configuration or command output. Send what helps and not what opens a door. Read each file before attaching it, and instead of attaching the whole file, paste a few important lines.

**Never send these with their real content:**

- `.env`. It holds the database password (`DB_PASSWORD` and every `DB_PASSWORD_*` or variable that a `password_env:` names), `OPENAI_API_KEY`, `API_KEYS_JSON` and possibly a password written inside `DB_CONNECTION_URL`, `APP_DB_URL` or a `url_env` variable. If someone must see your settings, copy the file, empty every secret in the **copy** (`DB_PASSWORD=`, `OPENAI_API_KEY=`, the password part of every URL, and the whole value of `API_KEYS_JSON`, which may span several lines), read the copy from start to finish and send that.
- `project_config/api_keys.json` (or the value of `API_KEYS_JSON`). It holds SHA-256 hashes, not keys, but it is the list of who may use the system and which columns each must not see. The output of any export of the keys is the same.
- A raw API key, and `VERIFY_API_KEY`. If a raw key went into a chat or a ticket, treat it as leaked: revoke it from the admin panel and issue a new key (section 2.2).
- `project_config_draft/.setup_log.json` (`project_config/.setup_log.json` before 6.13.0), which may hold the database URL with its password (section 0.3.1).
- `logs/audit_log.jsonl*` and `logs/query_log.jsonl*` (real questions and generated SQL), the SQLite files under `logs/` (`app.db`, `sessions.db` and their `-wal` and `-shm` files), `exports/` (results), `eval_data/` and any report made from it (part 5; the only exception is the share file of section 5.5), and `project_config_draft/` (drafts may quote real values, section 0.3.2).

**These are safe, after you have looked:**

- The output of `python -m scripts.verify_deployment`. It never prints a password: the connection target is shown with the password masked and `VERIFY_API_KEY` is not repeated. But the host name, the database and the tables are in it.
- The output of `python scripts/sync_schema.py`, `python scripts/assign_datasources.py` and `python scripts/prompt_budget.py`: table names and numbers; no credential or API key is printed.
- The aggregate report of `python scripts/analyze_audit_log.py` (section 8 of `docs/deployment-runbook.md`), not the `--include-examples` version.
- The share file of `python -m eval.cli run --share` (section 5.5), after you have read it: it holds your questions and both SQL texts and no result value.
- `project_config/schema.yaml`: table and column names and descriptions. Check that no description quotes a real value (the draft of section 0.3.2 can).
- `project_config/datasources.yaml`. A password cannot be in it: the key `password:`, `pwd:` or `url:` is refused and the file holds only the *name* of the variable that holds the secret (`password_env: DB_PASSWORD_SALES`). But the server name, the database and the login are in it; if the layout of your servers is confidential, empty those.
- The server's start-up lines (the banner, `CORS allowed origins`, `Prompt path for data source`) and the `[FAIL]` line you are asking about.

For everything you send, write what you removed, so that the reader does not mistake a value you emptied for one that was missing.


## Part 7 — Latency: warming the prefix cache and breaking down the `llm` stage

The `llm` stage is almost all of a question's time. Two things are now seen separately: the **prefill** of the prompt's fixed prefix (which is slow once after every restart) and **waiting versus generation**.

**Why is the first question after a restart slow?** For each data source, every question starts with the same text (the system prompt, the schema, the rules, the examples: the "fixed prefix"). A model server that has a prefix cache computes only the short tail; an empty cache computes the whole prefix. On a large local model this is about a minute, and the cache is empty after every restart of the model server. First turn on the prefix cache in the model server: for vLLM, `--enable-prefix-caching` (and `--enable-prompt-tokens-details` so that `cached_tokens` is reported). Without it, none of what follows makes a question faster.

**Warming at start-up** — `LLM_PREFIX_WARMUP_ON_STARTUP` (default `true`): at start-up the server sends the model, for each data source whose prompt has a fixed prefix, one request that is only the prefix itself (`max_tokens=1`, temperature 0), and it runs on a background thread; the server accepts requests at once and a question that arrives in the middle of the warm-up is answered normally. It is on by default because when there is nothing to warm it does nothing: a schema larger than `PROMPT_RETRIEVAL_TOKEN_BUDGET` (the retrieval path), `LLM_PROVIDER=mock`, or an untrusted endpoint without `LLM_ALLOW_REMOTE`. Every error is one log line (without the prompt text and without the key). If the model server is shared and billed per token, or start-up must send no traffic at all to the model, set it to `false`. The ceiling on the total time is `LLM_PREFIX_WARMUP_TIMEOUT_SECONDS` (default 180 seconds, must be greater than zero).

In the start-up log you see one line for each source:

```
LLM prefix warm-up: source=(whole schema) warmed in 41873 ms, prompt_tokens=4612
```

**Warming by hand** — after a separate restart of the model server (or after a change to the system prompt, rules, schema or examples in the admin panel, which changes the prefix):

```bash
curl -X POST -H "Authorization: Bearer $OPS_KEY" http://localhost:8000/admin/llm/warmup
```

It needs the `operations` capability, runs the same warm-up synchronously and returns, for each source, `status` (`warmed` / `skipped` / `failed`), `duration_ms` and `prompt_tokens`. If a warm-up is already in progress the answer is `409` and nothing is sent. This fills the cache only once: a model server that restarts itself, or evicts the prefix under memory pressure, or another machine behind a load balancer, is cold again.

**Keeping the cache warm** — `LLM_PREFIX_KEEPWARM_SECONDS` (default `0`, which is off): the warm-up at start-up fills the cache only once. On a model server that others use too, their traffic evicts your prefix again whenever it needs memory, and the next question pays the whole prefill (about a minute, and several times that when the server is busy). With a value greater than zero, a background thread sends each source's prefix again every N seconds; it is the same warm-up request (the prefix only, `max_tokens=1`, temperature 0) and is built by the same code. The rules are the warm-up's rules:

- **A source whose prefix a real question used within the last N seconds is skipped.** That question refreshed the cache itself, so when there is traffic keeping warm sends no request and only fills the idle gaps.
- For a source on the retrieval path, with `LLM_PROVIDER=mock`, and for an untrusted endpoint without `LLM_ALLOW_REMOTE`, nothing is sent.
- A failure is one `WARNING` line, not one line per round: the same result repeated goes to `DEBUG` until the result changes, and a round that succeeds again writes `(recovered after N failed pass(es))`. No line holds the prompt text, the reply text or a key, and a failure never affects questions.
- It pauses in maintenance mode and stops when the server shuts down.
- The first round is one interval after start-up, not at that moment.

The value must be `0` or at least `30`, or `Settings.validate()` fails in the preflight and at start-up. **The suggested value on a shared model server is `300`** (five minutes; twelve small requests an hour for each source). Only if the check below shows that the prefix is evicted between rounds, set `120`, and do not go below `60`. Leave it at `0` on a model server that only you use (nothing evicts the prefix), when the server has no prefix cache, and when all sources are on the retrieval path.

Cost: **under traffic, none**; a source that has had a question in the last interval is skipped. When idle, if the prefix is still in the cache, each round is one request per source that the server answers from the cache and generates one token for (milliseconds up to about a second of server time). If the prefix has been evicted, it is one full prefill, the very cost the next question would have paid; so the cost is taken off the analyst's question rather than added to it, unless nobody asks afterwards. A service that evicts the prefix between *every* two rounds pays a useless prefill each interval; the check below shows exactly that.

To check that it works (the `operations` or `security` capability):

```bash
curl -H "Authorization: Bearer $OPS_KEY" http://localhost:8000/admin/llm/warmup
```

In `keepwarm`: `enabled` and `interval_seconds`, `paused` (maintenance mode), `runs`, `last_run_at` (UTC) and `last_run_outcome`, which is one of `warmed`, `skipped` (each source either stayed warm through traffic, or is on the retrieval path, or is not allowed; each entry's `detail` says which), `failed`, `paused` or `busy`, and in `last_run` one entry for each source. If the model server runs with `--enable-prompt-tokens-details`, a `cached_tokens` close to `prompt_tokens` and a short `duration_ms` mean the prefix was still in the cache and the interval is short enough; a `cached_tokens` much lower than `prompt_tokens`, round after round, means the server evicts the prefix faster than the interval: shorten the interval or turn it off. The same object also appears under the key `keepwarm` in the reply of `POST /admin/llm/warmup`. When it is off, `enabled: false` comes back with a `reason`.

Limits: when other people's requests are queued ahead of yours, a warm prefix does not help either (a high `ttft_ms` with a small `prompt_tokens`); it keeps only one replica warm, the one that answers; and it keeps only the **fixed prefix** warm: a source on the retrieval path has no fixed prefix.

**When the model server reports it, `prefix_cache_hit` is what the server said.** The `llm` block of the audit record has three fields: `cached_tokens` (the same as `usage.prompt_tokens_details.cached_tokens`: how many prompt tokens the server served from its prefix cache; `null` means the server did not say, and in vLLM it is reported only with `--enable-prompt-tokens-details`; `0` is a real answer), `prefix_cache_hit_source` (`"server"` when `cached_tokens` decided, `"estimate"` when not), and `prefix_cache_hit` (with source `"server"`: true if `cached_tokens` covers at least half of the fixed prefix, or half of the whole prompt if that is shorter or there is no prefix; with source `"estimate"`: the old rule, `prompt_tokens` less than half of the prefix estimate). Older records have none of these and count as estimates.

**Do not read the estimate as the cache's result.** An OpenAI-compatible server such as vLLM counts the whole prompt in `usage.prompt_tokens`, whether most of it came from the cache or not; so an estimate of `true` means "the prompt was short" (a question that went through the retrieval path, or a small source) and `false` means "the prompt was nearly as large as the whole prefix", from the cache or cold. Without `cached_tokens`, put the time next to the size: a large prompt in about a second means it had the cache; a large prompt in tens of seconds means it was cold; a small prompt that took long means it is not prefill, and the request was waiting in other people's queue or the model generated for a long time (`ttft_ms` and `reasoning_tokens`).

**Latency by the real result.** Under "Cache behaviour", `python scripts/analyze_audit_log.py` prints the "real hit rate (server)" line only over the calls for which the server reported `cached_tokens` (`n/a` if none did), and the section "LLM stage by real cache hit" shows the `llm` stage's time for `hit`, `miss` and `unreported`; a call whose flag is only an estimate never goes into `hit` or `miss`. The `--json` report has the same with `latency_by_cache` and `cache_behaviour.server_hit_rate`, and the admin panel's «بازاستفادهٔ prefix» ("Prefix reuse") tile shows the server's rate (if there is one) and writes its source.

**The size of the prefix** — the prefix is most of the cost of a cold question, so its size is latency. `python scripts/prompt_budget.py --no-model` prints, for each source, the characters and the estimated tokens (`len(text) // 4`) without calling the model. In this release two formatting changes made the prefix smaller without removing anything that the model reads: each column's line is `- Name: description` and not `  - Name: description`, and the extra blank line between business rules (from each rule's trailing newline) was removed. The saving is two characters per column, so it grows with the schema (about 2% in a large schema); larger levers (a description repeated across very many columns, rules that the model rarely needs) change content and stay with the owner of `project_config/`.

**A correction round that says what exists** — a question whose SQL the guard rejects is given back to the model up to twice (`MAX_CORRECTION_ATTEMPTS`), that is, three model calls; `corrections` in the `llm` block counts the rounds, and `timings.llm_ms` has all the calls while `llm.total_ms` has only the last. In one real-use log, the requests that needed a correction were a large share of all model calls and nearly all of them were rejected: the guard's message ("Unknown column 'X' on table 'T'") says what is wrong and not what is right, and the model guessed again. Now the correction text for two rejections, and only those two, says from `schema.yaml` what is right: for an unknown column, the list of the real column names of that table (in the order of `schema.yaml`, names only, no descriptions); for an unknown table, the real tables closest to the name written, in the form in which they should be written in `FROM`. A column that the key's `denied_columns` bans completely does not appear in the list; a join-only column stays in the list and is marked `(join key only)` (the guard allows exactly this use, and the first prompt said so); and if the key's policy cannot be read, nothing is added. This text is added only to the question's section, not to the fixed prefix, so it does not disturb the prefix cache; a block that the model has already seen in the same request is not repeated; and every other error (syntax, execution) gets exactly the same text as before. Details: `docs/deployment-runbook.md` section 20.9.

**Breaking down the `llm` stage** — `LLM_STREAM_TIMINGS` (default `false`): the request is sent as a stream and rebuilt into the same reply (the same text, the same reasoning, the same `finish_reason`, the same `usage`), and the audit record's `llm` block gets these four fields: `ttft_ms` (from sending to the first token = queue + prefill), `generation_ms` (from the first token to the end), `reasoning_tokens` (how many tokens were reasoning; `null` means no reasoning was seen, not zero) and `reasoning_tokens_estimated` (`true` means an estimate from the length of the reasoning text, because not every server reports the exact number). The reasoning text is never stored. The first two fields are filled only with `LLM_STREAM_TIMINGS=true`; older records do not have these fields and reading them is no problem.

`python scripts/analyze_audit_log.py` prints a section called "LLM stage split" (the p50 and p95 of these numbers). How to read it:

- A high `ttft_ms`, a `prompt_tokens` close to the size of the prefix and `prefix_cache_hit: false` — the cache is cold; check the model server's prefix cache and warm by hand after a restart.
- A high `ttft_ms` but a small `prompt_tokens` and `prefix_cache_hit: true` — the model server held the request behind other requests; look at the load on the model server.
- A low `ttft_ms` and a high `generation_ms`, with `reasoning_tokens` being most of `completion_tokens` — the model is thinking; reduce or turn off reasoning with `LLM_EXTRA_BODY`. Raising `LLM_NUM_PREDICT` only makes the request longer.

One thing about `LLM_STREAM_TIMINGS`: the request timeout (120 seconds) now also bounds the whole call, and a proxy that buffers SSE will show `ttft_ms` equal to `total_ms`. The details and its cost are in `docs/deployment-runbook.md` section 20.

**The same numbers on real use: the «کیفیت در استفاده‌ی واقعی» ("Quality in real use") card** — the admin panel's card (and `GET /admin/quality?days=7`, with an `operations` or `security` key) shows, over the last 7, 30 or 90 days and over the whole period, both overall and for each data source: the share of questions that were executed, rejected by the guard, needed a correction, had an empty result (of the executed ones) and were out of scope; the error rate by category; the number of confirmed answers («پاسخ درست است» ("The answer is correct")) and flagged answers, and the confirmed share of those evaluated (**a lower bound, not accuracy**, because analysts evaluate only some of the answers); and the latency, overall and for each stage (p50 and p95).

The split of the `llm` stage into `ttft` and `generation` on this card exists only for calls made with `LLM_STREAM_TIMINGS=true`. To have it, put `LLM_STREAM_TIMINGS=true` in `.env` and restart the server; the two rows `llm · ttft` and `llm · generation` fill from the first streamed call and stay empty (0 samples) until then. This is normal, and the note under the table on the card says so. Records from before the restart do not get these fields, so wait a day before reading the 7-day numbers. Reading this split is the same as the table above.

Because this route reads the whole audit log, its result is cached for each period for `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` (the same cache as the deployment-checks card and the schema-drift card, but it makes no call to the data warehouse) and the card stays on the automatic 30-second refresh; the card's own refresh button recalculates at that moment. The same numbers are printed by `python scripts/analyze_audit_log.py` too (the "Real-use quality" section; `--quality-days N` sets the period and `--json` adds the `quality` object); both print only counts and percentiles, not questions.

**A response cache in front of the model** — a gateway or proxy between the application and the model server (an LLM gateway, an API management layer, a reverse proxy with a cache) can keep a response cache: when a request resembles an earlier one it returns the stored answer and the request never reaches the model. A *semantic* cache treats prompts of the same or similar meaning as one, and a *prefix* cache treats prompts that begin with the same text as one. This application's requests are a long fixed prefix with a short question at the end, so two different questions are almost the same prompt to such a cache. This is different from the model server's own KV prefix cache (this same part, above), which only saves the computation of the prefix and always generates a fresh answer.

*The sign:* the same answer for different prompts (the wizard writes one alias list for most of the tables of a database, and rules that read as nonsense); an answer in a few milliseconds (`llm.total_ms` equal to 8 with 512 `completion_tokens` in the audit log: no model makes 512 tokens in 8 milliseconds); and exactly the same SQL for different questions with a very short `llm.total_ms`.

*The risk:* the same endpoint also produces SQL. A semantic cache can answer "the number of active customers" with the SQL stored for "the number of inactive customers". That SQL is valid, the guard accepts it, the query runs and returns rows: a silent wrong answer with a clean audit record. It also spoils the accuracy measurement (part 5), because a run that sends the questions through the caching gateway measures the cache and not the model.

*Confirming it:* `python scripts/verify_deployment.py`, the check `Model endpoint answers each prompt itself`, from the same host and with the same `.env` as the server; the wizard stopping with `Step 3 stopped`; or, in the audit log, `suspected_cached_response: true` in the `llm` block and the warning `Suspected response cache in front of the model`.

*The fix:* turn off response caching for this client at the gateway (an exception for the application's key or path; a semantic cache has no safe setting for generating SQL), or point `OPENAI_BASE_URL` and `WIZARD_LLM_BASE_URL` straight at the model server. Run the check again until it is `PASS`. If you cannot change the gateway, do not use this endpoint to generate SQL.

*The run-time warning:* the server keeps in memory, for each process and for each data source, the last 200 pairs (question hash, SQL hash) of the questions that reached the model in `POST /query`. When a *different* question gets byte-identical SQL within two minutes and the model's reply took less than `LLM_CACHE_SUSPECT_MS` (default `50`; `0` turns it off), it writes one `WARNING` for each streak and puts `suspected_cached_response: true` in the `llm` block of that request's audit record. It blocks or changes no request, because two questions can legitimately reach one SQL; only the hash stays in memory and the question text is not recorded.

---

## Short checklist

**CLI**

- [ ] `.env` is filled in
- [ ] The nine `project_config/` files and `system_prompt.md` are in place
- [ ] `schema.yaml` has real descriptions: `python scripts/draft_descriptions.py` was run, its `schema.described.yaml` reviewed and put in place, and its `AUTO-GENERATED` first line deleted
- [ ] The first key was issued and is in `API_KEYS_FILE` *before* the preflight
- [ ] `python -m scripts.verify_deployment` is green
- [ ] `python app.py`

**Web**

- [ ] Everything above
- [ ] An API key has been issued and is in `API_KEYS_FILE` (or `API_KEYS_JSON`)
- [ ] Terminal 1: `uvicorn api.server:app --port 8000` **from the repository root**
- [ ] Terminal 2: `python -m http.server 8080` **from `web/`**
- [ ] Open **`http://localhost:8080`** in the browser (not 8000)
- [ ] The page is already live — just put in the **API key** and save
      (do not look for a backend address field; it was removed in 4.4.0 and
      is in `web/js/config.js`)

**Admin panel**

- [ ] A key has been issued with `--full-admin` (or `admin` plus at least one
      of `operations`/`security` — with `admin` alone, 9 of the 13 sections give 403)
- [ ] If the JSON spans several lines, the whole value is in single quotes
- [ ] The server has been restarted
- [ ] **`http://localhost:8080/admin/`** and the **raw key** (43 characters), not the hash

**Several data sources**

- [ ] `datasources.yaml` and one `DB_PASSWORD_*` for each source, written *before* any tool drafts a file
- [ ] `python scripts/sync_schema.py` has been run, `schema.yaml` (with the backup `schema.yaml.bak`) has been replaced by `schema.synced.yaml`, and `--check` says `CHECK OK`
- [ ] The columns and tables marked `# not in database` / `# not found in any data source` are down to zero
- [ ] `nolock: true` is only under the sources whose DBA asked for it (per source)
- [ ] `python -m scripts.verify_deployment` is green for each source
- [ ] `python scripts/prompt_budget.py` has been run and `PROMPT_RETRIEVAL_TOKEN_BUDGET` set
- [ ] The «انحراف شِما» ("Schema drift") and «تازگی واژگان ابعاد» ("Dimension vocabulary freshness") cards have been checked

**Latency** (part 7)

- [ ] The prefix cache is on in the model server (vLLM: `--enable-prefix-caching`)
- [ ] The line `LLM prefix warm-up: ... warmed in ...` is in the start-up log; after a restart of the model server: `POST /admin/llm/warmup`

**Before an upgrade** (part 5)

- [ ] A baseline has been recorded on the current release with `--reference live`
- [ ] After the upgrade, `python -m eval.cli run --live --reference live --golden eval_data/golden.jsonl --baseline eval_data/baseline.json` gave exit code `0`
