# Warehouse-load diagnostic kit, for the DBA

This directory is for a DBA investigating load on the SQL Server warehouse
this application queries -- most commonly triggered by noticing a steady
stream of `SELECT 1` and wanting to know whether it, or something else, is
responsible for disk activity, CPU, or blocking observed on that server.

Everything in [`warehouse-load-diagnostics.sql`](warehouse-load-diagnostics.sql)
is **read-only**: `SELECT` against system catalog views, Dynamic Management
Views (DMVs), and Query Store views only. Nothing in it writes to a table,
changes a server or database setting, creates a trace, or runs a `DBCC`
command that changes state. It is safe to run against a production server
by any login with ordinary read access plus `VIEW SERVER STATE` for the
server-scoped DMVs (sections 2, 4, 6 below).

The file is a reference to copy individual statements out of, not a script
meant to run top to bottom in one batch -- some sections (§3) are
deliberately meant to be run twice, minutes apart.

## The one fact that motivates this whole document

**`SELECT 1` reads no data pages.** It touches no table, no index, no file.
If a DBA observes disk I/O, CPU, or blocking that *coincides* with a
`SELECT 1` stream, that activity is not being caused by the `SELECT 1`
statements themselves -- something else running at the same time is
responsible, and finding that something else is what §3 and §4 below are
for. Do not stop investigating once `SELECT 1` traffic is found; it
explains network round trips and session count, never disk activity.

See `docs/deployment-runbook.md` §12 ("What this application sends to the
warehouse, and how often") for exactly what this application sends and
under what settings, including `DB_POOL_PRE_PING` / `DB_POOL_PING_IDLE_SECONDS`
-- the two settings that control most of the `SELECT 1` volume a DBA will
observe from a normally-operating deployment of this application.

## When to run each section

| Section | Run it when... | What a result means |
|---|---|---|
| §1 AUTO_CLOSE | First thing, once, when investigating unexplained latency or disk activity on this application's queries | If `is_auto_close_on = 1`, the database fully closes and reopens whenever the last connection to it ends -- a connection pool that lets connections idle out and reconnect (or a short `DB_POOL_RECYCLE_SECONDS`) can turn what looks like a trivial query into a full close/reopen cycle. This is a database-level setting the DBA controls; this application cannot see or change it from a connection string. If it is on and load coincides with reconnects, turning it off (a DBA-side change) is usually the fix, not anything on the application side. |
| §2 Sessions/requests from this application | Any time -- this is the general-purpose "what is `local-sql-agent` doing right now" check | `program_name = 'local-sql-agent'` is how this application identifies itself (`DB_APPLICATION_NAME`, default `local-sql-agent` -- see `config.Settings.db_application_name`; a deployment that overrode that setting will show a different name here). §2a lists every open session; §2b shows only ones actively running a statement right now (should be empty or near-empty for a healthy, fast-query workload -- lingering entries are worth investigating); §2c checks specifically for blocking, in either direction. |
| §3 File I/O, two snapshots | When §1/§2 don't explain observed disk activity, or a DBA asks "is this data, log, or tempdb?" | Run the query, wait a few minutes, run it again, and diff the counters per file (they are cumulative since the last restart, not a live rate). A delta on tempdb's files with none of this application's queries running in that window means something else spilled to tempdb, not this application. A delta on this application's own database's **log** file during a period of mostly `SELECT` traffic is unusual -- reads should not generate meaningful log writes -- and worth asking about directly. |
| §4 Top queries by physical reads | When something is definitely consuming disk I/O and the question is "which query" | §4a ranks every cached query by physical reads, application-agnostic. §4b explains why there is no direct DMV join from a historical query back to "which application ran it" (plan cache and Query Store don't retain that), and gives the three real ways to attribute a §4a result to this application: it's still running (cross-reference §2b), it already finished (cross-reference this application's own audit log, `docs/deployment-runbook.md` §6, by timestamp and SQL text), or by shape (this application only ever runs guard-validated read-only `SELECT`s against warehouse tables -- never DDL/DML, never a `sys.*`/`INFORMATION_SCHEMA` query). §4c points at Query Store if it's enabled, which survives restarts and plan-cache eviction and is more reliable for "what has the workload looked like over the last N hours" than the plan cache. |
| §5 Login auditing / login triggers | When connection churn (many new logins, e.g. a short `DB_POOL_RECYCLE_SECONDS` or high `pool_size` turnover) coincides with load that doesn't match query volume | A logon trigger or SQL Server Audit that does real work (a lookup, a write) on every new connection adds cost per login, independent of what that connection goes on to query. More reconnects means more trigger executions. This is visible only from the SQL Server side -- the application has no way to see whether one is configured. |
| §6 Wait statistics | As a general "what kind of bottleneck is the server under" signal, or to corroborate a §4 finding | Cumulative since the last restart, like §3 -- most useful as two snapshots, or as a coarse signal. A high `PAGEIOLATCH_*` total alongside a §4 finding of high physical reads on the same query corroborates a genuinely disk-bound query, rather than something else being blamed for it. |

## What this document deliberately does not do

- **No table, database, or server name from any real deployment appears in
  either file.** Replace `<your_database>` (and any login/program name, if
  your deployment overrode `DB_APPLICATION_NAME`) with the real values
  before running.
- **No write, no DBCC that changes state, no trace creation.** If a deeper
  investigation genuinely needs one of those (e.g. a server-side trace,
  `DBCC FREEPROCCACHE` to force a clean plan-cache measurement), that is a
  DBA decision made deliberately, on its own, outside this kit -- not
  something to fold into a routine diagnostic run.
- **No login credentials, connection strings, or other secrets.** This kit
  assumes the DBA already has their own access to the server; it exists to
  save time deciding *what* to look at, not to grant access.

## Related reading

- `docs/deployment-runbook.md` §12–13 -- the full, itemised list of what
  this application sends to the warehouse and how often, and the
  `DB_POOL_PRE_PING` / `DB_POOL_PING_IDLE_SECONDS` trade-off that controls
  most of the steady `SELECT 1` volume.
- `docs/db-hardening.md` -- why the warehouse login this application uses
  should be read-only, and what that buys independently of anything in
  this kit.
