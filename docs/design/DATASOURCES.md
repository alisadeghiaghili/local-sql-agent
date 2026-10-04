# Multiple warehouse data sources — decision record

Status: **implemented**. This document records why the design looks the
way it does, what was rejected and why, what is deliberately still out of
scope, and how the current shape leaves room for it.

## Context

Every earlier release assumed exactly one warehouse connection
(`DB_CONNECTION_URL`): one server, one login, one connection pool. A
deployment that needs to answer questions against more than one database —
a second SQL Server instance holding an archive, a partner's warehouse on
its own box, a reporting replica kept separate from the transactional
server — had no way to express that. The only workaround was running a
second, entirely separate instance of this application per server, which
duplicates every piece of shared configuration (aliases, business rules,
examples, the system prompt) and gives an analyst no single place to ask a
question that might touch either one.

## Decision

- `project_config/datasources.yaml` is an **optional** file. Its absence
  means exactly one data source, named `"default"`, using
  `DB_CONNECTION_URL` — byte-for-byte what every earlier release did
  (the raw password may now be given in `DB_PASSWORD` instead of inside
  the URL; unset, nothing changes). A deployment that never adds this file
  is completely unaffected by anything below.
- When present, it lists named sources, and each source **describes its
  connection**: `host`, `port`, `database`, `driver`, a login and optional
  ODBC `options` (the structured form, below). Only the secret stays in
  the environment, named by `password_env`. A source may instead use the
  legacy `url_env` form (below). Either form may add an optional
  `description`, optional `keywords` (see "Choosing a source per question"
  below) and a per-source `dialect`/`application_name` override.
  One `default` key says which source a table with no explicit assignment
  belongs to (required once more than one source is listed; inferred when
  there is only one).
- **Secrets never go in `datasources.yaml`.** That file is versioned and
  reviewed like the rest of `project_config/` (it sits next to
  `schema.yaml`, gets read by `git diff`, and is meant to be committed to a
  deployment's own private fork of the config). A password belongs in
  `.env` or a secret store, referenced only by the name of the variable
  that holds it. A `password:`, `pwd:` or `url:` key in the file is refused
  with a message saying so, and so is a credential-like key under
  `options`.

- Each table in `schema.yaml` may set `datasource: <name>`, or a list of
  distinct names for a table that exists in several sources (see "Tables
  that live in several sources" below); a table without one belongs to
  the default source. Table names stay globally
  unique across every source — there is one flat allowlist, not one per
  source — so a question never has to know which source a table lives on
  before it can ask about it.
- Several databases on **one** SQL Server instance can be configured two
  ways, and both are supported:
  - **Two sources** (same `host`, different `database`). Each has its own
    connection pool and its own login. One query still runs on exactly one
    source, so a question that needs tables from both databases is refused
    (`cross_datasource`).
  - **One source** with a multi-part `db_schema: "OtherDb.dbo"` on the
    second database's tables, rendered `[OtherDb].[dbo].[Table]`
    (`security.dialects.quote_tsql_qualifier`). SQL Server can join across
    databases on one instance with a three-part name, so this is the way
    to keep cross-database joins working. It is one connection, one pool,
    one login.

  Choose by whether questions join across the databases: if they do, use
  one source and `db_schema`; if they do not, two sources give each
  database its own pool and login.
- Table names are globally unique **as `schema.yaml` keys**, not
  necessarily as bare names: a warehouse with the same table name in two
  schemas (`sales.Customer`, `ref.Customer`) gives each one its own
  qualified key rather than colliding on `Customer` — see
  `docs/design/TABLE-NAMES.md`. This is orthogonal to data sources: two
  same-bare-name tables can be on the same source or on two different
  ones, and `database.routing`/`extract_touched_tables` key everything off
  the full `schema.yaml` key either way.
- The data source a query runs on is **derived from the tables it
  references**, never chosen by the model
  (`database.routing.resolve_datasource`, fed by
  `security.sql_guard.extract_touched_tables` — the same table resolution
  the guard's own allowlist check already does, reused rather than
  duplicated). One query runs on exactly one source. A query no single
  source can run (no source has every table it reads) is refused before it
  ever reaches a connection: the SQL
  guard raises `CorrectableRejection(reason="cross_datasource",
  is_refusal=True)` (`security.sql_guard._require_single_datasource`), and
  the web UI shows an analyst-facing Persian sentence for it, the same way
  it does for every other guard rejection reason.
- Only `SQL_DIALECT` is supported across all sources for now. A per-source
  `dialect:` key is accepted (so a `datasources.yaml` written today does
  not need editing when mixed dialects eventually land) but a value other
  than the deployment's own `SQL_DIALECT` is refused at start-up
  (`database.datasources.validate_datasource_urls`).

- A source may set `nolock: true` to have every table it reads carry
  `WITH (NOLOCK)`. It is a per-source switch because it changes what a
  query can see (dirty reads), and it is T-SQL only, so
  `database.datasources.load_datasources_config` refuses it when
  `SQL_DIALECT` has no table hints
  (`DialectProfile.supports_table_hints`). The executor applies it to the
  routed source's statement just before execution
  (`database.table_hints.add_nolock_hints`, which inserts text at offsets
  found with sqlglot rather than regenerating the SQL); the audit trail
  keeps the validated statement.

## Tables that live in several sources

A real warehouse pair shares at least one table: a date dimension
replicated into each database, which the fact tables of both join to. With
one `datasource:` per table that table had to be assigned to one source,
and every question joining it to the other source's facts was refused even
though the other source has its own copy.

`datasource:` therefore also accepts a **non-empty list of distinct names**
(`datasource: [sales, inventory]`). The declaration is a claim about
the database, not a preference: *the same table exists, with the same
shape, in each of those sources.* It is stored as a tuple
(`TableDefinition.datasource`, `()` for "not set"), and a name listed twice,
an empty list or a blank entry is refused when `schema.yaml` loads. Every
place that validates source names (`check_table_datasources`, start-up, the
admin panel's draft validation, `verify_deployment.py`) checks every name.

**Routing rule** (`database.routing.choose_datasource`, one function used
by both the executor and the guard, so they cannot disagree):

1. The candidate sources for a statement are the **intersection** of the
   source sets of every table it reads. A table `schema.yaml` does not
   list counts as living in the default source only, as before.
2. An empty intersection is the `cross_datasource` refusal. Its text still
   names every source and its tables, with a shared table listed under each
   of its sources, and adds `Available in several data sources: Date
   (sales, inventory)` so the reader sees why combining it with two
   tables from different sources does not help.
3. Otherwise the statement runs on the **default source if it is a
   candidate, else the first candidate in `datasources.yaml` order**
   (`database.datasources.pick_datasource`) — deterministic across calls
   and processes. A statement reading only a shared table therefore runs on
   the default source when the table lives there.

`table_datasources()` keeps returning `{table: one source}` — rule 3 applied
to the table alone — for callers that want a single name;
`table_datasource_sets()` returns every source of every table (default
first, then file order) and is what anything whose correctness depends on
the full set uses.

**What else follows from the full set**

- *Prompt.* The schema block prints `Data source: sales, inventory`
  for a shared table. The closing rule gains one sentence (a table listed
  under several sources exists in each and combines with tables from any
  one of them) only when the block mixes tables with different source sets;
  a block where every table has the same sources prints no rule, and a
  deployment without shared tables prints the rule byte-for-byte as before.
- *Dimension vocabulary and value resolver.* Each is one single-table
  statement, so it is routed by rule 3: the default source if the table
  lives there, else the first listed in `datasources.yaml` order. A
  replicated dimension is read from one copy, not from each; that is the
  point of declaring the copies the same.
- *Schema drift.* A shared table is compared on **each** of its sources. A
  column missing from one copy is reported as `Table.Column [source]`
  (single-source tables keep their plain `Table.Column`).
- *Where a table really is.* When every `schema.yaml` column of a table is
  missing from a source it is assigned to and another configured source has
  the table, the drift report's `misplaced_tables` gives the sentence and
  the value to write, e.g. `stock_dim.Broker: not in sales, found in
  inventory — set datasource: inventory`. The other sources are asked with
  one `INFORMATION_SCHEMA.TABLES` query each, and only when such a table
  exists; the result is cached with the rest of the drift report. The admin
  panel's schema-drift card shows it, and `scripts/verify_deployment.py`
  fails `Tables are in their data source` with the same text (that check
  reflects every catalogue, so the panel runs it under "deep checks").

**Generating the assignments.** A deployment that never set `datasource:`
runs everything on the default source (`Invalid object name ...` for the
tables that are elsewhere) and its drift panel lists every column of the
other source as missing. `scripts/assign_datasources.py` is the one-time
generator, curated afterwards: it reads each source's tables, views and
columns through the application's own engines (two `INFORMATION_SCHEMA`
queries per source; never a row), matches each `schema.yaml` table by
schema and name (case-insensitively; a multi-part `db_schema` such as
`OtherDb.dbo` by its schema part, because the catalogue views describe the
connected database), and writes `schema.with_datasources.yaml` next to
`schema.yaml` — every original line and comment kept, one `datasource:`
line inserted (or an existing one replaced) under each table key: a name,
`[A, B]` for a table in both, or `# not found in any data source` under a
table found in neither. It checks the file with
`schema_data.registry.validate_schema_yaml_text` and proves that nothing but
`datasource` differs from `schema.yaml` before writing, and it stops
without writing if any source's catalogue could not be read (a source it
cannot see would make a shared table look single). The report lists, per
source, the tables found only there, the shared tables, the tables found
nowhere, the tables whose current `datasource:` disagrees, and the columns
`schema.yaml` lists that the database lacks. `--check` writes nothing and
exits 1 when any `datasource:` disagrees with what was found.

## Choosing a source per question

A statement can only ever run on one source, so a prompt that describes
every table of every source asks the model to read, and the application to
send, far more than any one answer can use. With several sources the
application therefore **picks one source per question first, and shows the
model that source's whole schema**. With one source none of this runs: every
prompt, prompt prefix, cache key and code path is what it was before.

**Why not keep one prompt for everything.** The whole-schema static prefix
(`prompt_engine/static_prefix.py`) is cacheable and the model sees every
table, which is the best case, but it must fit
`PROMPT_RETRIEVAL_TOKEN_BUDGET`. A schema that is comfortable for one source
can be several times over the budget once a second source is added. Every
request then takes the retrieval path, which picks a handful of tables by
alias and TF-IDF. That is a good tool for a small schema and a poor one for
dozens of tables: it can choose dimension tables from both sources and miss
the fact table that holds the measure the question names, so the model sees
no table that answers it and says `OUT_OF_SCOPE`, or a self-correction round
is spent. When retrieval found nothing it used to show *every* table, a
prompt several times the size of the budget. Choosing the source first
removes most of that: the model sees one source's complete schema, and each
source gets its own cacheable prefix again.

### How the source is chosen

`retrieval.source_selector.select_source` is deterministic and makes no
model call. The signals are tried in this order and the first that picks a
winner decides:

| Order | Signal | Wins when | Reason |
|---|---|---|---|
| 1 | **Keywords** | One source has more distinct matching `keywords:` phrases than every other | `keyword` |
| 2 | **Session** | A follow-up turn, and the keywords do not point at another source: the previous turn's source is kept | `session` |
| 3 | **Retrieval evidence** | One source has the highest score from the tables retrieval selected and the tables whose warehouse values matched | `retrieval` |
| 4 | **Default** | Nothing above decided: the default source (or, among sources still tied, the first in `datasources.yaml` order) | `default` |

*Keywords.* `keywords:` in `datasources.yaml` is a list of words or phrases,
Persian or English, that mark a question as being about the source. A
phrase counts when it occurs in the question as **whole words**, after the
same folding every other comparison in the retrieval layer applies
(`core.persian.normalize_for_matching`): Arabic `ي`/`ك` fold to `ی`/`ک`,
Persian and Arabic digits to `0-9`, the ZWNJ is removed, case is ignored.
`stock` does not match inside `stockholder`, and a plural or a prefixed
form is another word, so list each form you expect. A phrase counts once
however often it occurs. Several sources with the same, highest number of
matching phrases is a tie, not a decision (below). The file is refused at
start-up if `keywords` is not a list of non-empty strings with no repeats
(two spellings that fold to the same text are a repeat).

*Session.* Only for a turn that continues the previous question (the v2
engine's `refines` basis, `session.refinement`). A question classified as
`fresh` is a new topic, so retrieval decides. A refinement composed over the
previous turn's SQL (`composition: cte`) reads whatever that statement read
and stays on the previous source with no selection of its own. The source a
turn was answered from is kept in the turn's private memory
(`TurnMemory.datasource`) and persisted with the session, so a reopened
conversation continues from it; a turn stored before the field existed
falls back to the source its SQL routes to.

*Retrieval evidence.* `ContextRetriever.retrieve(question)` already runs
over the whole schema for every request. Each source scores one point for
every selected table that lives in it, and one point for every table whose
resolved dimension values matched (`context.resolved_values`), where a
table that lives in several sources gives each of them its share of the
point (half a point each for two sources). A shared table does not
discriminate, so it is weak evidence for any one source.

*Ties.* Sources tied on the highest keyword count are decided by the
previous source if it is one of them, then by retrieval evidence among them
only, then by `datasources.yaml` order.

The result also lists **every** source in fallback order, the chosen one
first, the rest by strength of evidence (keyword matches, then retrieval
score, then configuration order).

### What the prompt for a source contains

- The tables whose source set includes the chosen source, shared tables
  included, under one line near the top of the schema block:
  `Data source: sales — Sales warehouse` (the `description` of the source;
  just the name when it has none). The per-table `Data source:` lines and
  the closing "tables in different sources cannot be combined" rule are not
  printed: the model sees one source.
- The relationships whose two ends are both among those tables.
- The few-shot examples whose SQL reads only tables available in that
  source (`security.sql_guard.extract_touched_tables` parses the example).
  An example whose tables cannot be determined is kept.
- Business rules and metrics, whole: they are not table-scoped.

`SchemaRegistry.build_schema_context(..., source=)`,
`build_static_prefix(system_prompt, source)` and
`llm.router.build_prompt_segments(..., source=)` are the one place this is
done, and both engines go through the last.

### One path per source, and the token budget

`should_use_static_prefix(system_prompt, source)` asks the budget question
**for that source's prefix**:

- If the source's estimated static prefix is at most
  `PROMPT_RETRIEVAL_TOKEN_BUDGET`, its prompts use that prefix. It is cached
  per `(system_prompt, source)` and byte-identical across requests for the
  same source, so each source has its own prefix cache (vLLM prefix caching,
  llama.cpp KV reuse) exactly as the single-source deployment had.
- Otherwise the retrieval path runs **restricted to the source's tables**:
  only retrieved tables that live in the source are shown, and when none are
  left every table *of that source* is shown, never every table of the
  deployment. Relationships come from the tables shown and the examples are
  filtered the same way.

The default budget is unchanged (6000). **With several sources the budget
applies to each source, not to their sum**, so a schema whose whole prefix is
many times the budget can still leave each source under it. At start-up (or
at first use, whichever comes first) one INFO line per source says its table
count, its estimate, the budget and the path it will take:

```
Prompt path for data source 'sales': static prefix (cacheable) -- 26 table(s), static prefix estimate 5100 tokens, PROMPT_RETRIEVAL_TOKEN_BUDGET 6000 (per source)
```

*Sizing the budget.* `estimate_tokens` is `len(text) // 4`, a heuristic with
no tokenizer behind it. For English it is close; **for Persian it
undercounts by roughly 15%**: measured on a whole-schema prompt whose
descriptions are mostly Persian, the model's real `prompt_tokens` were about
1.14 times the estimate. Choose the budget from the logged estimates like
this:

1. A source stays on the static path when `budget >= its estimate`. Its real
   size is then about `estimate x 1.15` for Persian-heavy text, plus the
   variable suffix (filters, session context, the question) and the model's
   output. Check that sum against the model's context window and against
   how long a cold prefill may take, which is paid once per source for as
   long as the prefix stays in the model server's cache.
2. The budget is a threshold on the *estimate*, not a promise about tokens:
   a source whose estimate is just under it is a little over it in real
   tokens. Leave that margin when choosing the number.
3. To keep every source on the static path, set the budget to at least the
   largest source's estimate. To send a source down the retrieval path,
   leave its estimate above the budget, or split its tables by moving some
   to another source.
4. Making a source smaller usually helps more than raising the budget: a
   source with half the tables prefills in about half the time and leaves
   room for the answer.

### When the model says OUT_OF_SCOPE

Selection is a heuristic, and a model that is shown the wrong source's
tables can only decline. If it answers `OUT_OF_SCOPE` and another candidate
source exists, the request is retried **once** with the next candidate in
the fallback order: **one extra model call at most, never more**, outside
the correction budget. The correction history is dropped, because it
describes the other source's tables, and corrections after the retry keep
the new source's prefix. A second `OUT_OF_SCOPE` is returned exactly as
before. A deployment with one source never retries, and neither does a
refinement composed over the previous turn's SQL: its prompt carries no
schema, so there is no other source to show. The retry is one helper,
`llm.source_routing.generate_with_source_fallback`, called from
`llm.sql_agent.SQLAgent` and `api.runner` (result, full and SQL-only
modes) and from `session.engine.TurnEngine`, so the two engines cannot
drift.

### What it does not change

- **The guard and the router stay authoritative.** The selection decides
  what the model sees, never where a statement runs. The generated SQL must
  still pass `security.sql_guard.validate_sql`, and its source is still
  derived from the tables it reads (`database.routing.choose_datasource`).
  If that differs from the selected source, that is fine and no new refusal
  is added: the audit record keeps both.
- **The result cache key.** `api.query_cache` keys on the whole-schema
  prefix fingerprint, which does not depend on a source. For `/query` the
  source is a function of the question and of configuration that cannot
  change while the process runs, so a cached answer cannot belong to a
  different source than the one a fresh request would choose.
- **Business rules, metrics, and every single-source prompt.** Byte for
  byte; `tests/test_source_prompts.py` pins them.

### Observability

The audit record gains `datasource_selection`:

```json
{"chosen": "inventory", "reason": "keyword",
 "candidates": ["inventory", "sales", "archive"], "fallback_from": null}
```

`chosen` is the source whose tables the model last saw; `reason` is the
signal that ordered the candidates (`keyword`, `session`, `retrieval`,
`default`); `fallback_from` names the source that declined when the request
retried. It is `null` for a deployment with one source and for a result
served from the cache (no selection ran). It is separate from `datasource`,
which is where the *generated SQL* routes. The audit file is the only place
records are persisted, and the field is additive: a reader that does not
know it ignores it, and a record written before it has no key.
`prefix_cache_hit` in the audit `llm` block is measured against the chosen
source's own prefix estimate.

### Alternatives considered

**Asking the model which source a question is about.** Rejected for the
reason given above for routing itself, and for latency: it is one more call
before the real one. The selection is deterministic, auditable and free;
the single retry covers the cases it gets wrong.

**Keeping every source in the prompt and improving retrieval.** The problem
is not only retrieval quality: a whole-schema prompt cannot be cached once
it is over the budget, and a prompt that mixes sources invites a statement
that mixes them. One source per prompt makes both go away.

**Embedding or classifier models for the selection.** More moving parts and
a nondeterminism this project avoids elsewhere; keywords plus the retrieval
evidence already computed for the request are enough to start with, and
`keywords:` is the knob an operator controls.

## Structured sources: what changed and why

The first version of this file mapped a source name to `url_env` and
nothing else. Every real connection detail (host, port, database, driver,
ODBC options) was still one long URL in `.env`, so the YAML said almost
nothing, and operators had to percent-encode special characters in the
password by hand (`@` as `%40`), which they got wrong.

The division of labour is now: **the YAML describes the connection, `.env`
holds only the secret, and the code builds and escapes the URL.**

```yaml
default: sales
datasources:
  sales:
    description: Sales warehouse
    host: 10.0.0.5
    port: 1433                         # optional, default 1433
    database: SalesDW
    driver: ODBC Driver 18 for SQL Server   # optional, this is the default
    username: nlq_reader               # or username_env: VAR_NAME
    password_env: DB_PASSWORD_SALES    # NAME of the variable holding the RAW password
    options:                           # optional extra ODBC keywords
      TrustServerCertificate: true     # true/false -> yes/no, numbers -> text
  reports:
    host: 10.0.0.5
    database: ReportsDW
    trusted_connection: true           # Windows authentication
```

- **One URL-building function.** `database.datasources.build_url` calls
  `sqlalchemy.engine.URL.create("mssql+pyodbc", username=..., password=<raw
  value>, host=..., port=..., database=..., query={"driver": ..., **options,
  "trusted_connection": "yes"})`. SQLAlchemy escapes each part when the URL
  is rendered, so a password containing `@ % ] : / ? # & = + ;` or spaces
  needs nothing from the operator; `tests/test_datasources.py` proves the
  round trip through the rendered URL, the application-name rewrite and the
  ODBC connection string the pyodbc dialect produces. The dialect picks the
  SQLAlchemy driver name from a small mapping, so a future dialect is one
  entry (only `tsql` exists today). A per-source `dialect` must still equal
  `SQL_DIALECT`.
- **`DataSource.url` stays a string**, rendered with
  `URL.render_as_string(hide_password=False)`. Every consumer already
  takes a string (`with_application_name`, `_check_warehouse_url`,
  `appdb.engine.raise_if_same_database`, `create_engine`), SQLAlchemy's
  own rendering round-trips every password, and the unchanged legacy path
  produces strings too. The password is therefore in that string, which is
  why `DataSource.__repr__` omits it, `DataSource.redacted_url` is what
  anything displayed uses, and the same-database refusal and
  `scripts/verify_deployment.py` print the redacted form.
- **Rules, refused when the file loads (the message names the source):** a
  source is structured (`host` and `database` required) or `url_env`,
  never both; `trusted_connection: true` forbids `username`,
  `username_env` and `password_env`; otherwise exactly one of `username` /
  `username_env` plus `password_env`; the `*_env` values must be variable
  names, not values; `options` keys must not be credentials (`pwd`,
  `password`, `uid`, `user`, `user id`, case-insensitive) or one of the
  keys that have their own field (`driver`, `trusted_connection`,
  `server`, `database`, `odbc_connect`).
- **Start-up checks.** A `password_env` or `username_env` variable that is
  unset or empty is refused naming the source and the variable, never the
  value. A structured source's host, database, user name and password are
  compared with the unfilled-placeholder tokens (`your_server_here`,
  `change_me`, ...), because a built URL never contains the
  `username@server` text a hand-written one does.
- **The single-source path gets the same convenience.** Without
  `datasources.yaml`, `DB_PASSWORD` holds the raw password for
  `DB_CONNECTION_URL`; the code sets it with `URL.set(password=...)`. A
  password in both places is refused as ambiguous. A raw password written
  *inside* `DB_CONNECTION_URL` is deliberately not auto-detected or
  re-encoded: a password containing `/ ? # :` or a literal `%40` cannot be
  told apart from an already encoded one, and guessing would silently
  break configurations that work today.

### `url_env` (legacy)

`url_env: DB_URL_MAIN` names a variable holding a complete SQLAlchemy URL.
It keeps working exactly as in 6.1 and 6.2: the URL is used as written,
so a password inside it must still be percent-encoded by hand. It can sit
beside structured sources in one file. To move a source over, copy the
host, port, database, login and any query parameters out of the URL into
the YAML fields, put only the raw password in a new `DB_PASSWORD_*`
variable, and point `password_env` at it.

## Alternatives considered and rejected

**A password, or a whole connection string, inside `datasources.yaml`.**
The obvious shape — put the SQLAlchemy URL next to the source's name — was
rejected outright because `datasources.yaml` is meant to be committed and
code-reviewed the same way `schema.yaml` is. A file that is safe to put in
a pull request must never be able to carry a password. The structured form
splits the connection along that line: host, port, database, driver and
options are not secret and are the reviewable, versioned part ("what
sources exist and what do they mean"), while the password is only ever the
*name* of an environment variable. The two concerns (topology vs. secrets)
live in the two places that already own them (`project_config/` vs. `.env`).

**Letting the model choose the data source.** A tempting shortcut is to
ask the model to name the source alongside the SQL it generates, the same
way it names the SQL itself. This was rejected for the same reason the
table/column allowlist is never model-chosen: a data source assignment is
a *security and topology* fact (which server a query's traffic goes to),
not a language-understanding one, and trusting a small model's own claim
about it would mean a prompt-injected or simply mistaken generation could
silently send a query — and the schema/business context that goes with it
— to the wrong server. Deriving the source from the tables a query
actually references, the same way the guard already knows which tables
are real, keeps this a structural fact the model cannot override, matching
this project's existing rule that anything security-relevant (the table
allowlist, the column ACL) is decided by configuration and AST inspection,
never by trusting the model's own output.

**A separate connection pool per table instead of per source.** Rejected
as needless indirection: nothing about a table's connection behaviour
(pool size, recycle interval, application name) differs from another
table on the same server. A source is the right unit for a connection
pool; a table is the right unit for an allowlist entry and a
`datasource:` assignment. Collapsing the two would mean either a
combinatorial number of pools for a warehouse with many tables on one
server, or reintroducing exactly the "which pool does this table use"
bookkeeping a named source already exists to answer once, per server.

**Making `datasources.yaml` part of the admin panel's versioned config
bundle.** The panel's `config_bundle_versions` (`appdb/config_versions.py`,
`CONFIG_FILENAMES`) exists so an operations principal can propose a change
that is validated, diffed, dry-run against the golden set, and applied
without a restart or a deploy. `datasources.yaml` was deliberately left
out of that bundle: which servers exist and what connects to them is
deployment topology, not domain knowledge an analyst-facing admin should
be editing through a web form, and every engine this application holds is
already cached per source for the life of the process
(`database.connection.get_engine`) — changing which servers exist safely,
mid-request, would mean either accepting connections built against a
config that no longer matches disk, or tearing down and rebuilding pools
under live traffic. Restart-required for this one file is a smaller,
safer contract than "some config changes need a restart and some don't,
and this is one of them" would be from inside the version-bundle system
itself.

## Consequences

- Existing single-source deployments are unaffected: no `datasources.yaml`
  means identical prompts, identical `/health` output shape, identical
  `Settings.validate()` behaviour, identical everything — enforced by
  `tests/test_datasources.py`'s fallback tests and
  `tests/test_schema_registry.py`'s `TestSchemaContextDataSources`
  (`Data source:` lines and the closing rule appear ONLY once a second
  source is configured).
- Every place that used to reach for "the one engine" now reaches for
  "the engine for this source" — `database.connection.get_engine`,
  `database.executor.execute_sql`/`execute_sql_params`, `/health`'s
  `_ping_db`, `scripts/verify_deployment.py`'s per-source checks, and
  `schema_data.drift.check_schema_drift` — each keeping the exact same
  single-source behaviour as a special case of the general, per-source one
  rather than as a separately maintained code path.
- Start-up now refuses two additional misconfigurations that used to be
  impossible to even express: the application database colliding with
  *any* configured source (`api/server.py`'s `lifespan`, looping over
  every source instead of checking `DB_CONNECTION_URL` alone), and a
  `schema.yaml` table naming a `datasource:` that is not configured
  (`database.datasources.check_table_datasources`, called both at
  start-up and from the admin panel's own schema-draft validation so a bad
  assignment cannot even be approved).
- The audit trail additively records which source a query ran on
  (`observability.audit.AuditRecord.datasource`, re-derived from the
  executed SQL the same way routing itself works) — an old record simply
  has no key for it, and every reader treats that the same as `None`.
- With several sources the audit trail also records how the source was
  chosen for the prompt (`AuditRecord.datasource_selection`, see "Choosing a
  source per question") — additive and `None` with one source.

## Result cache — no key change needed

`api.query_cache.QueryCache` was audited for whether adding a data source
component to its cache key is required. **Conclusion: it is not**, for two
independent reasons, either one of which would be sufficient on its own:

1. **A cache hit never routes anything.** The cache stores the *result*
   of a query that already ran — a hit returns those stored rows directly
   and never calls `database.routing.resolve_datasource` or
   `database.executor.execute_sql` again. Which source the same SQL text
   *would* route to today, if it were run again right now, is simply not
   consulted on a hit, so it cannot make a hit serve the wrong data.
2. **Which source a hit's SQL was routed to when it was first written
   cannot change out from under it.** A table's source assignment
   (`schema.yaml`'s `datasource:`) needs a process restart to change at
   all — `schema_data.registry`'s allowlist is resolved once, at
   `security.sql_guard`'s own import time, exactly like the rest of the
   guard's frozen table/column allowlist (see
   `appdb/config_versions.py`'s module docstring, "What takes effect
   immediately, and what needs a restart"). `datasources.yaml` itself is
   documented as restart-required too (see "Alternatives considered"
   above). Either way, a change that could move a table's source only
   takes effect at exactly the moment (`api.query_cache.query_cache`, a
   module-level singleton) starts back up empty — so there is no window,
   live in one process, where the same cache key could correctly mean two
   different sources.

No test needed to be added to prove a negative design decision; this
section — and `api/query_cache.py`'s own module docstring, which carries
the same argument next to the code it describes — is the record of the
conclusion.

## Roadmap (not implemented)

Two extensions are explicitly out of scope for this change, and the
design above leaves room for both without a rewrite:

1. **Combining data from different servers in one answer.** Today a query
   that needs tables from two sources is refused outright
   (`cross_datasource`). A future "federated" mode — fetch from each
   source separately and combine client-side, or route through a linked
   server a DBA has configured — has a natural seam to grow from:
   `database.routing.group_tables_by_datasource` already computes exactly
   the `{source: tables}` partition such a mode would need as its input (a
   table in several sources is listed under each),
   it simply currently feeds that partition to a refusal instead of to a
   second code path that runs one query per group and joins the results
   in the application layer.
2. **Mixing database *types*, e.g. PostgreSQL alongside SQL Server.**
   `DataSourceDefinition.dialect` already exists as a per-source field for
   exactly this reason — it is accepted and validated today, but pinned
   to equal the deployment's single `SQL_DIALECT`
   (`database.datasources.validate_datasource_urls`). Lifting that
   constraint means the SQL generation prompt, the guard's transpile step,
   and value resolution would all need to become source-aware rather than
   deployment-wide, which is a materially larger change than adding a
   second SQL Server; the per-source `dialect:` field is deliberately
   already in the schema so that day does not also require a
   `datasources.yaml` migration.
