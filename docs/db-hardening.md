# Database hardening — operator actions for the DBA

Status: **for DBA review, not yet applied**. Everything in this document is a
*server-side* change to the production SQL Server instance — creating a
login, granting/denying permissions, and configuring Resource Governor.
Phase 1 (`security/sql_guard.py`, `database/executor.py`) is explicitly out
of scope for making these changes itself: it can only refuse what the
*application* sends, never what a login is permitted to do once it has a
connection. None of the T-SQL below has been run against any server; a DBA
should read it, adjust names/quotas for the real environment, and apply it
through the normal change process.

## Why this matters even though `security/sql_guard.py` exists

`security/sql_guard.py::validate_sql` (Phase 1) parses every model-generated
query and refuses anything that is not a read-only `SELECT`/`WITH` over a
known table — see its module docstring for the mechanism. That is a strong
guard, but it is an **application-layer** control: it only sees SQL text
that flows through this codebase's own pipeline. It does not, and cannot,
protect against:

* A bug in this codebase, a future code path, or a completely different
  application that reuses the same database login and does not call
  `validate_sql` at all.
* A dependency compromise or a bug in `sqlglot` itself that lets a
  malicious query slip past the AST checks.
* The DB connection currently configured in `.env` — see
  `config.py`'s `db_connection_url` default,
  `mssql+pyodbc://username@server:1433/Auction_DM?...trusted_connection=yes`
  — which is a **trusted (Windows) connection**, not a dedicated SQL login.
  A trusted connection typically runs as whatever OS identity started the
  process, which in a shared server environment can carry far more
  privilege than "read a handful of reporting tables."

Defense in depth means the database itself should refuse a write even if
every application-layer control above it were bypassed or misconfigured.
That requires three server-side changes, in increasing order of
paranoia: a dedicated read-only login, explicit `DENY` grants on top of
that, and a Resource Governor workload group to bound the damage a single
runaway (but otherwise legitimate) query can do. `database/executor.py`
(Phase 1) already does its part at the application layer — see its
docstring for the driver-level query timeout, `SET LOCK_TIMEOUT`, and the
always-rolled-back transaction — but none of that substitutes for the
database-side controls below; it only reduces how bad a mistake can be
*before* the DBA has applied them.

---

## 1. Dedicated read-only login

Create a SQL login (or a Windows/AD login, if the environment requires
integrated auth) that is used **only** by this application, distinct from
any interactive/admin login, so its blast radius is fully described by the
grants below rather than by "whatever the app's service account happens to
also be used for elsewhere."

```sql
-- Run in the context of the SQL Server instance (master), then Auction_DM.
USE master;
GO

CREATE LOGIN [auction_nlq_reader]
    WITH PASSWORD = N'<generate a strong secret via the usual secrets process>',
    CHECK_POLICY = ON,
    CHECK_EXPIRATION = ON;
GO

USE [Auction_DM];
GO

CREATE USER [auction_nlq_reader] FOR LOGIN [auction_nlq_reader];
GO

-- db_datareader grants SELECT on every table/view in the database. That is
-- broader than this application needs (schema_data/columns.py::TABLE_COLUMNS
-- lists 12 tables), but is a reasonable starting point that is still far
-- narrower than the trusted-connection default this app currently ships
-- with. Tighten to per-table GRANT SELECT once the schema stabilises:
--
--   GRANT SELECT ON [Auction_Fact].[Contract]         TO [auction_nlq_reader];
--   GRANT SELECT ON [Auction_Fact].[CustomerContract] TO [auction_nlq_reader];
--   GRANT SELECT ON [Auction_Fact].[Offer]             TO [auction_nlq_reader];
--   GRANT SELECT ON [Auction_Fact].[Order]             TO [auction_nlq_reader];
--   GRANT SELECT ON [Auction_Dim].[Customer]           TO [auction_nlq_reader];
--   GRANT SELECT ON [Auction_Dim].[Supplier]           TO [auction_nlq_reader];
--   GRANT SELECT ON [Auction_Dim].[Broker]             TO [auction_nlq_reader];
--   GRANT SELECT ON [Auction_Dim].[Symbol]              TO [auction_nlq_reader];
--   GRANT SELECT ON [Auction_Dim].[Ring]                TO [auction_nlq_reader];
--   GRANT SELECT ON [General_Dim].[Date]                TO [auction_nlq_reader];
--   GRANT SELECT ON [General_Dim].[Currency]            TO [auction_nlq_reader];
--   GRANT SELECT ON [General_Dim].[DeliveryPlace]       TO [auction_nlq_reader];
--
-- (schema names above follow eval_data.example/golden.jsonl's bracketed
-- references; confirm the real schema names against the live database
-- before running this.)
ALTER ROLE db_datareader ADD MEMBER [auction_nlq_reader];
GO
```

After this login exists, `DB_CONNECTION_URL` in `.env` should be changed
from the trusted-connection default to this login explicitly, e.g.:

```
DB_CONNECTION_URL=mssql+pyodbc://auction_nlq_reader:<password>@<host>:1433/Auction_DM?driver=ODBC+Driver+17+for+SQL+Server
```

## 2. Explicit `DENY` grants

`db_datareader` only grants `SELECT`; it does not grant `INSERT`/`UPDATE`/
`DELETE`/`DDL` in the first place, so the `DENY` statements below are
**belt-and-suspenders**, not compensating for a gap in step 1 — the goal is
that a future well-meaning `ALTER ROLE ... ADD MEMBER` (e.g. someone later
adding this login to `db_datawriter` "just to fix one thing quickly") is
still caught by an explicit deny, since `DENY` always wins over `GRANT` in
SQL Server's permission evaluation regardless of which role granted it.

```sql
USE [Auction_DM];
GO

DENY INSERT, UPDATE, DELETE, EXECUTE, ALTER, REFERENCES
    ON DATABASE::[Auction_DM] TO [auction_nlq_reader];
GO

-- Stored-procedure / extended-procedure execution and cross-server /
-- file access: security/sql_guard.py already refuses these by AST node
-- type (EXEC/EXECUTE, xp_*/sp_*, OPENROWSET/OPENQUERY/OPENDATASOURCE — see
-- its module docstring), but denying the underlying server permission
-- means the database refuses them too, independent of the app.
DENY EXECUTE ANY EXTERNAL SCRIPT TO [auction_nlq_reader];
GO
REVOKE EXECUTE ON SCHEMA::[sys] FROM [auction_nlq_reader];
GO

-- CREATE/ALTER/DROP of any kind, and permission changes, at the database
-- level (belt-and-suspenders alongside the DATABASE-scoped DENY above,
-- which already covers ALTER; these name the remaining DDL/permission
-- verbs explicitly for auditability).
DENY CREATE TABLE, CREATE VIEW, CREATE PROCEDURE, CREATE FUNCTION,
     ALTER ANY SCHEMA, CONTROL, ALTER ANY USER, ALTER ANY ROLE
    ON DATABASE::[Auction_DM] TO [auction_nlq_reader];
GO
```

`INFORMATION_SCHEMA` views and `sys.*` catalog views cannot be individually
`DENY`'d the way a base table can (they are system views owned by a
different schema with their own visibility rules), which is exactly why
`security/sql_guard.py::validate_sql` refuses any table reference whose
schema is `INFORMATION_SCHEMA` or `SYS` at the application layer instead
(see its `_SYSTEM_SCHEMAS` check). The DBA-side equivalent is narrowing
`VIEW DEFINITION` and `VIEW DATABASE STATE`, which `db_datareader` does not
grant by default — confirm that stays true after any future role changes:

```sql
USE [Auction_DM];
GO
DENY VIEW DEFINITION TO [auction_nlq_reader];
DENY VIEW DATABASE STATE TO [auction_nlq_reader];
GO
```

## 3. Resource Governor workload group

`database/executor.py`'s driver-level `.timeout` and `SET LOCK_TIMEOUT` (see
its docstring) bound a *single connection's* wait, but nothing at the
application layer stops this login, in aggregate, from consuming a
disproportionate share of the server's CPU/memory/IO if the NLQ endpoint
gets a burst of expensive traffic (e.g. several unbounded aggregations over
the fact tables at once). Resource Governor lets the DBA cap that at the
server level, independent of anything the application does or fails to do:

```sql
USE master;
GO

-- Classify connections from this login into a dedicated workload group so
-- its resource caps don't affect any other application's connections.
CREATE FUNCTION dbo.AuctionNlqClassifier()
RETURNS sysname
WITH SCHEMABINDING
AS
BEGIN
    IF SUSER_SNAME() = 'auction_nlq_reader'
        RETURN N'AuctionNlqGroup';
    RETURN N'default';
END;
GO

CREATE RESOURCE POOL AuctionNlqPool
    WITH (
        MIN_CPU_PERCENT = 0,  MAX_CPU_PERCENT = 25,
        MIN_MEMORY_PERCENT = 0, MAX_MEMORY_PERCENT = 20,
        CAP_CPU_PERCENT = 25    -- hard cap, unlike MAX_CPU_PERCENT alone
    );
GO

CREATE WORKLOAD GROUP AuctionNlqGroup
    WITH (
        -- Belt-and-suspenders alongside database/executor.py's driver
        -- timeout: this bounds CPU time server-side, independent of
        -- whether the application's own timeout logic is ever reached.
        REQUEST_MAX_CPU_TIME_SEC = 60,
        REQUEST_MAX_MEMORY_GRANT_PERCENT = 10,
        MAX_DOP = 4
    )
    USING AuctionNlqPool;
GO

ALTER RESOURCE GOVERNOR WITH (CLASSIFIER_FUNCTION = dbo.AuctionNlqClassifier);
ALTER RESOURCE GOVERNOR RECONFIGURE;
GO
```

`REQUEST_MAX_CPU_TIME_SEC` above is a starting point, not a tuned value —
set it in line with `QUERY_TIMEOUT_SECONDS` in `.env` (`config.py`'s
`Settings.query_timeout_seconds`, default 60s) plus headroom for legitimate
large aggregations, and adjust `MAX_CPU_PERCENT`/`MAX_DOP` against the
server's actual core count and what else runs on it.

## Verification checklist for the DBA

After applying the above:

- [ ] Confirm `auction_nlq_reader` can run every query in
      `eval_data.example/golden.jsonl`'s `expected_sql` values successfully
      against the real database.
- [ ] Confirm `auction_nlq_reader` gets a permission error (not a syntax
      error) attempting `INSERT`/`UPDATE`/`DELETE`/`DROP`/`CREATE`/`EXEC` on
      any table in `Auction_DM`.
- [ ] Confirm `SELECT * FROM sys.tables` and
      `SELECT * FROM INFORMATION_SCHEMA.TABLES` both fail for this login
      (this is redundant with `security/sql_guard.py`'s application-layer
      block, by design — the point of this checklist item is confirming
      the database-side control works *independently* of the app).
- [ ] Confirm `sys.dm_resource_governor_workload_groups` shows
      `AuctionNlqGroup` receiving connections from `auction_nlq_reader`
      (i.e. the classifier function is actually being applied) once the
      application's `.env` is switched over to the new login.
- [ ] Update `.env`'s `DB_CONNECTION_URL` to the new login and remove
      `trusted_connection=yes` from the connection string.
