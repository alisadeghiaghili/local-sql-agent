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

So a key holding only `admin` loads a panel where six of the ten sections
return 403 — which reads as a broken deployment rather than as a
permissions decision. For a deployment with a single operator, grant all
three at once:

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
flow (§2.3).

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
  `PROJECT_CONFIG_DIR` below). Create it, then set
  `API_KEYS_FILE=project_config/api_keys.json` in `.env`:

  ```json
  [
    {"id": "analyst-1", "name": "Jane Analyst", "key_sha256": "<64 hex>"},
    {"id": "admin-1", "name": "Admin", "key_sha256": "<64 hex>",
     "admin": true, "operations": true, "security": true}
  ]
  ```

  The file is read once at start-up, so **restart the server after editing
  it** (the preflight below runs in its own process and always sees the
  current file). A missing file, invalid JSON (the error gives the line and
  column) or a field repeated inside one entry stops startup.
  `API_KEYS_JSON` still works for a single key written on one line, but do
  not set both: that is refused. A multi-line `API_KEYS_JSON` must be wrapped
  in single quotes with no apostrophe anywhere inside it (a key named
  `Ali's key` breaks it), and double quotes around the array break the JSON
  — which is why the file is the recommended form.
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

Leave `RATE_LIMIT_*`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`, `AUTH_REQUIRED`,
and `MAX_CONCURRENT_REQUESTS` at their shipped defaults unless step 3 below
tells you otherwise for your specific expected concurrency — see
`config.Settings.rate_limit_requests` / `.log_backup_count` for the
reasoning behind each default before changing it.

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
Auction NLQ Engine — <version/licence line identifying this codebase>
```

Right after it, confirm the CORS line — the one place the effective,
post-`.env` allowlist is stated plainly, rather than left for the UI to
discover by failing:

```
CORS allowed origins: http://localhost:8080, http://127.0.0.1:8080
```

If the UI's own origin is not in that list (and the UI is not
same-origin with the API), that is the fix — see the CORS callout in
step 4 above before assuming anything else is wrong.

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
and re-run it. The two most common fail-closed exits, both intentional:

- `Invalid configuration: ...` — a `Settings.validate()` failure (a
  placeholder left in `.env`).
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
per-request round trip to the configured `DB_CONNECTION_URL` — kept here in
one place, with the setting that controls each, following the 2026
warehouse-load audit that traced a steady `SELECT 1` stream (plus DDL
attempts and catalogue scans) back to a few specific sources.

| Source | What it sends | How often | Controlled by |
|---|---|---|---|
| Connection-pool checkout (`database/connection.py`, `database/pool_ping.py`; `appdb/engine.py` too, for a non-SQLite application database) | An idle-aware liveness probe (`SELECT 1`) before handing a pooled connection to any caller | Only when the connection has sat idle in the pool for at least `DB_POOL_PING_IDLE_SECONDS` — i.e. roughly once per burst of activity after a gap, not once per query. `DB_POOL_PING_IDLE_SECONDS=0` reverts to the old ping-every-checkout behaviour | `DB_POOL_PRE_PING` (default `true`) — see §13 below for the trade-off; `DB_POOL_PING_IDLE_SECONDS` (default `60`) — the idle threshold; `DB_POOL_RECYCLE_SECONDS` (default `3600`) bounds how long a connection sits in the pool before being recycled regardless |
| `GET /health` (`api/health.py`) | Always exactly one explicit `SELECT 1` on the checked-out connection — checkout's own idle-aware probe no longer runs unconditionally, so `/health` cannot rely on it (see §13). In the rare case the checked-out connection had also gone idle long enough for checkout to probe it too, that is a second round trip on top of this one | At most once per `HEALTH_CACHE_TTL_SECONDS` (default `15`) no matter how often external monitors call this endpoint; concurrent callers within that window share one probe | `HEALTH_CACHE_TTL_SECONDS` |
| Admin panel — deployment checks, non-deep (`GET /admin/health/checks`, `scripts/verify_deployment.build_checks()` minus the two deep checks below, per data source) | `check_db_connectivity`'s `SELECT 1`, `check_row_cap`'s `SELECT TOP n name FROM sys.all_objects` | Once when the admin panel is opened, and again only when the operator presses that card's own refresh button — **not** on the 30-second auto-refresh. A repeat within `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` (default `300`) is served from cache instead of re-run; `?refresh=1` forces a fresh run | `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` |
| Admin panel — deployment checks, deep (`GET /admin/health/checks?deep=1`) | Everything above, **plus** `check_login_is_read_only`'s always-rolled-back `CREATE TABLE`/`DROP TABLE` attempt and `check_query_timeout`'s multi-second `WAITFOR DELAY` probe | Only when an operator explicitly presses the panel's "deep checks" button (confirmation dialog first) — never automatically, never on a timer. `python -m scripts.verify_deployment` (the CLI) still runs every check, deep included, every time it is invoked by hand or in CI | Not time-based — opt-in per click. Cached separately from the non-deep result under the same `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` |
| Admin panel — schema drift (`GET /admin/schema-drift`, `schema_data.drift.check_schema_drift`) | A full catalogue reflection: `get_table_names` + `get_columns` for every table of every schema | Once when the admin panel is opened, and again only on that card's own refresh button — not on the 30-second auto-refresh. Same cache/`?refresh=1` behaviour as above | `ADMIN_EXPENSIVE_CACHE_TTL_SECONDS` |
| Admin panel — every other card (audit summary, query cache stats, maintenance mode, feedback, keys, dimension-vocabulary status, per-analyst usage, auth failures) | No direct warehouse query — these read the audit log, the application database, or in-process bookkeeping | Every 30 seconds (`AUTO_REFRESH_MS` in `web/admin/main.js`) while the panel tab is visible, plus on open and on each card's own refresh button | Not warehouse-relevant; listed here only to be explicit about what the 30-second timer *does* still touch |
| Dimension-vocabulary refresh (`retrieval/dimension_vocabulary.py`) | A `DISTINCT`-style scan of one configured dimension column | On first use after startup if `DIMENSION_VOCABULARY_WARM_ON_STARTUP=true` (default `false`); otherwise lazily, at most once per column per TTL, triggered by the first `/query` request that needs a stale-or-missing entry (a background, non-blocking refresh — the triggering request itself is served from whatever was cached, stale or not) | `DIMENSION_VOCABULARY_TTL_SECONDS` (default `3600`), `DIMENSION_VOCABULARY_WARM_ON_STARTUP` |
| Relationship-map schema inspection (`database/relationship_map.py`) | A one-time reflection of foreign-key relationships, only if no `project_config/relationships.yaml` is present | At most once per process lifetime (result is cached in memory for the life of the process; never repeats on a timer) | `AUTO_DISCOVER_SCHEMA` (default `false`) |
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

## 16. Describing the connection in `datasources.yaml` (optional)

Every step above assumes the default, single-source shape: one
`DB_CONNECTION_URL` (plus `DB_PASSWORD`). Use `datasources.yaml` when this
deployment queries more than one database — a second database on the same
server, another SQL Server instance, a partner's warehouse on its own box
— or simply wants host, database and driver written in a reviewable file
instead of one long URL in `.env`. See `docs/design/DATASOURCES.md` for
the full design and why it is shaped this way.

**One server, two databases: two sources, or one.** Two databases on the
same server are a supported configuration as **two sources** (same `host`,
different `database`). Each source has its own connection pool and login,
and one query still runs on exactly one source. If questions need to
**join across** the two databases, use **one** source instead and give the
second database's tables a multi-part `db_schema` (`db_schema:
"OtherDb.dbo"`, step 3 below), which SQL Server can join with a
three-part name.

**Configuring it.**

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

   `port` defaults to 1433 and `driver` to `ODBC Driver 18 for SQL
   Server`. For Windows authentication write `trusted_connection: true`
   and leave out the username and `password_env`. This file is versioned
   like `schema.yaml` and must **never** hold a password; a `password:`
   key is refused, and so is a credential key under `options`.
2. Set each source's **raw** password in `.env`, under the variable
   `password_env` names:

   ```
   DB_PASSWORD_SALES=p@ss/w:rd#1
   DB_PASSWORD_INVENTORY=...
   ```

   Write the password exactly as the database knows it. Do **not**
   URL-encode it: the application builds the connection URL and escapes
   every special character itself. `DB_CONNECTION_URL` and `DB_PASSWORD`
   are then unused, and no longer required. A missing or empty variable is
   refused at start-up with a message naming the source and the variable.
3. In `project_config/schema.yaml`, give every table that is not on the
   default source a `datasource: <name>` key. A table in a second
   database on the **same** server that should stay joinable with an
   existing source's tables is not a new source — give it a multi-part
   `db_schema: "OtherDb.dbo"` instead and leave `datasource` unset.

   **If your warehouse has the same table name in more than one schema**
   (e.g. `sales.Customer` and `ref.Customer`), give each one its own
   qualified `schema.yaml` key (`sales.Customer:`, `ref.Customer:`)
   instead of colliding on `Customer:` — see `docs/design/TABLE-NAMES.md`.
   This applies whether or not you use `datasources.yaml` at all; it is
   worth doing even for a single-source deployment. Give **every** table a
   `db_schema`, even a bare-keyed one, while you're there — a table with
   no qualifier configured at all cannot have its schema checked by the
   guard, so a query naming the wrong schema for it still resolves.
4. Restart the server. Like `.env` itself, `datasources.yaml` is
   deployment config edited on disk, not one of the nine files the admin
   panel's versioned config bundle covers — a change to it needs a
   restart, the same as changing `DB_CONNECTION_URL` always did.

**Moving a `url_env` source to the structured form.** A source written
for 6.1 or 6.2 (`url_env: DB_URL_MAIN`, the variable holding a complete
URL) keeps working unchanged, and may sit beside structured sources in
one file. To move it: copy the host, port, database, login and query
parameters (`TrustServerCertificate=yes` and the like, which become
`options`) from the URL into the YAML; put only the raw, un-encoded
password in a new `DB_PASSWORD_*` variable; replace `url_env` with
`password_env`; restart and run `python -m scripts.verify_deployment`.
Remove the old `DB_URL_*` variable afterwards.

**Verifying it.** `python -m scripts.verify_deployment` (step 3 above)
runs every database check once per configured source automatically —
`Database connectivity [sales]`, `Database connectivity [inventory]`, and so
on for the read-only-login, row-cap and query-timeout checks — plus one
new check, `Tables map to data sources`, confirming every `schema.yaml`
table's `datasource:` (if any) actually names a configured source. The
admin panel's non-deep deployment checks (`GET /admin/health/checks`, and
its "deep checks" button) expand the same way.

**What `/health` shows.** With one source, `/health`'s `database_detail`
field is exactly what it always was (e.g. `"SELECT 1 succeeded"`). With
more than one, it names each source in turn:

```
sales: SELECT 1 succeeded; inventory: SELECT 1 succeeded
```

and `database` (the boolean) is `true` only when **every** configured
source answered — one source being down is enough to flip the whole
field to `false`, with the detail string still naming exactly which one.

**A query spanning two sources.** A generated query whose tables belong
to two different sources is refused before it ever opens a connection —
the guard rejects it with reason `cross_datasource`, and the web UI shows
an analyst-facing Persian sentence explaining that the question needs
data from two separate servers and asking for it to be split. This is
expected, not a bug to investigate: see `docs/design/DATASOURCES.md`'s
roadmap section for why combining sources in one answer is deliberately
not supported yet.
