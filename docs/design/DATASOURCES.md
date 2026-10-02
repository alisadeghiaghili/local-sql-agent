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
  `description` and a per-source `dialect`/`application_name` override.
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

- Each table in `schema.yaml` may set `datasource: <name>`; a table
  without one belongs to the default source. Table names stay globally
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
  duplicated). One query runs on exactly one source. A query whose tables
  span two sources is refused before it ever reaches a connection: the SQL
  guard raises `CorrectableRejection(reason="cross_datasource",
  is_refusal=True)` (`security.sql_guard._require_single_datasource`), and
  the web UI shows an analyst-facing Persian sentence for it, the same way
  it does for every other guard rejection reason.
- Only `SQL_DIALECT` is supported across all sources for now. A per-source
  `dialect:` key is accepted (so a `datasources.yaml` written today does
  not need editing when mixed dialects eventually land) but a value other
  than the deployment's own `SQL_DIALECT` is refused at start-up
  (`database.datasources.validate_datasource_urls`).

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
   the `{source: tables}` partition such a mode would need as its input,
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
