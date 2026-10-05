-- Warehouse-load diagnostic kit -- read-only DMV/catalog queries only.
--
-- Every query below SELECTs from a system catalog view, a Dynamic
-- Management View (DMV), or a Query Store view. None of them write to
-- any table, change any server or database setting, create a trace, or
-- run a DBCC command that changes state. Safe to run against a
-- production warehouse by any login with VIEW SERVER STATE (for the
-- server-scoped DMVs) and ordinary read access to the target database.
--
-- See docs/dba/README.md for what each section is for, when to run it,
-- and how to read the result. Run sections one at a time -- this file is
-- a reference to copy individual statements from, not a script meant to
-- be executed top to bottom in one batch.
--
-- Generic by design: no table, database, or server name from any real
-- deployment appears below. Replace <your_database> / <this_application's
-- database> with the actual name before running.


-- =============================================================================
-- 1. Is AUTO_CLOSE on?
-- =============================================================================
-- AUTO_CLOSE closes the database completely (releasing its resources) once
-- the last connection to it ends, then pays a cold-start cost to reopen it
-- on the next connection. A connection pool that lets connections idle out
-- and reconnect can turn what looks like a trivial query into a full
-- close/reopen cycle. See docs/dba/README.md §1 for what to do if this is on.

SELECT
    name                                 AS database_name,
    is_auto_close_on,
    is_auto_shrink_on,                   -- a second, related setting worth
                                          -- knowing about while you're here
    state_desc,
    recovery_model_desc
FROM sys.databases
WHERE name = N'<your_database>';


-- =============================================================================
-- 2. Sessions and requests from this application
-- =============================================================================
-- local-sql-agent identifies itself via DB_APPLICATION_NAME (default
-- "local-sql-agent"; see config.Settings.db_application_name), which SQL
-- Server surfaces as program_name here. Filtering on it separates this
-- application's traffic from everything else talking to the same server.

-- 2a. Every current session opened by this application, with its login
-- and host, and whether it currently holds an open transaction.
SELECT
    s.session_id,
    s.login_name,
    s.host_name,
    s.program_name,
    s.status,
    s.last_request_start_time,
    s.last_request_end_time,
    s.open_transaction_count,
    s.total_elapsed_time,
    s.cpu_time,
    s.reads,
    s.writes,
    s.logical_reads
FROM sys.dm_exec_sessions AS s
WHERE s.program_name = N'local-sql-agent'
ORDER BY s.last_request_start_time DESC;

-- 2b. Requests from this application that are ACTIVELY RUNNING right now
-- (empty most of the time for a healthy, fast-query workload -- a
-- non-empty result sitting here for more than a few seconds is worth a
-- second look). Joins sys.dm_exec_sql_text to see the actual statement
-- text via the request's sql_handle.
SELECT
    r.session_id,
    r.status,
    r.command,
    r.wait_type,
    r.wait_time,
    r.blocking_session_id,
    r.total_elapsed_time,
    r.cpu_time,
    r.logical_reads,
    r.reads,
    r.writes,
    t.text AS sql_text
FROM sys.dm_exec_requests AS r
INNER JOIN sys.dm_exec_sessions AS s
    ON s.session_id = r.session_id
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) AS t
WHERE s.program_name = N'local-sql-agent'
ORDER BY r.total_elapsed_time DESC;

-- 2c. Is anything from this application currently BLOCKED, or BLOCKING
-- something else? blocking_session_id <> 0 means "waiting on session
-- <that id>"; a session id appearing as someone else's blocking_session_id
-- means it is the one holding things up.
SELECT
    r.session_id,
    r.blocking_session_id,
    r.wait_type,
    r.wait_time,
    r.status,
    s.program_name
FROM sys.dm_exec_requests AS r
INNER JOIN sys.dm_exec_sessions AS s
    ON s.session_id = r.session_id
WHERE r.blocking_session_id <> 0
   OR r.session_id IN (
        SELECT blocking_session_id
        FROM sys.dm_exec_requests
        WHERE blocking_session_id <> 0
   );


-- =============================================================================
-- 3. File I/O -- two snapshots, a few minutes apart
-- =============================================================================
-- sys.dm_io_virtual_file_stats gives CUMULATIVE counters since the last
-- SQL Server service restart (or since the file was added), not a
-- point-in-time rate -- run this query, wait a few minutes, run it again,
-- and subtract to see what changed IN THAT WINDOW. A single snapshot only
-- tells you total lifetime I/O, which says nothing about what is
-- happening right now.
--
-- Run this against <your_database> to see which of its own files (data
-- vs. log) is busiest, and separately against tempdb (database_id = 2,
-- always) since a query that spills to tempdb (a sort, a hash join that
-- doesn't fit in memory) shows up there, not against your database's own
-- files.
--
-- IMPORTANT, and the reason this section exists at all: a plain
-- "SELECT 1" reads no data pages -- it touches no table, no index, no
-- file. If file I/O moves in the same window a SELECT-1 stream is
-- observed, that I/O is coming from something else running at the same
-- time (a real query from this or another application, a maintenance job,
-- a backup), not from the pings themselves. Use this section to find what
-- that something else actually is, rather than assuming the pings caused it.

-- Snapshot A -- run this now, note the time.
SELECT
    DB_NAME(vfs.database_id)      AS database_name,
    mf.name                       AS logical_file_name,
    mf.type_desc,                 -- ROWS (data) or LOG
    vfs.num_of_reads,
    vfs.num_of_bytes_read,
    vfs.io_stall_read_ms,
    vfs.num_of_writes,
    vfs.num_of_bytes_written,
    vfs.io_stall_write_ms,
    vfs.size_on_disk_bytes,
    GETDATE()                     AS snapshot_taken_at
FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
INNER JOIN sys.master_files AS mf
    ON mf.database_id = vfs.database_id
   AND mf.file_id = vfs.file_id
WHERE vfs.database_id IN (DB_ID(N'<your_database>'), 2 /* tempdb */)
ORDER BY vfs.database_id, mf.type_desc;

-- Snapshot B -- run the SAME query again a few minutes later. Subtract
-- Snapshot A's num_of_reads/num_of_bytes_read/num_of_writes/
-- num_of_bytes_written per (database_name, logical_file_name) from
-- Snapshot B's to get the delta for that window. A delta concentrated on
-- tempdb's data file with none of this application's own queries running
-- points at something else's spill, not at this application; a delta
-- concentrated on <your_database>'s LOG file with mostly SELECT traffic
-- running is unusual and worth asking the DBA about directly (SELECT
-- traffic should not generate meaningful log-file writes at all).


-- =============================================================================
-- 4. Top queries by physical reads, and which come from this application
-- =============================================================================
-- 4a. Plan-cache based (works without Query Store, but is reset whenever a
-- plan is evicted from cache -- e.g. a restart, memory pressure, or a
-- recompile). Ranked by total physical reads, so the query actually
-- responsible for disk I/O rises to the top regardless of how often it runs.
SELECT TOP (25)
    qs.total_physical_reads,
    qs.total_physical_reads / qs.execution_count AS avg_physical_reads,
    qs.execution_count,
    qs.total_logical_reads,
    qs.total_elapsed_time / qs.execution_count   AS avg_elapsed_time_us,
    qs.last_execution_time,
    SUBSTRING(
        st.text,
        (qs.statement_start_offset / 2) + 1,
        (
            (CASE qs.statement_end_offset
                WHEN -1 THEN DATALENGTH(st.text)
                ELSE qs.statement_end_offset
             END - qs.statement_start_offset
            ) / 2
        ) + 1
    )                                             AS statement_text
FROM sys.dm_exec_query_stats AS qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) AS st
ORDER BY qs.total_physical_reads DESC;

-- 4b. Attributing 4a's ranking to THIS application specifically.
--
-- Neither the plan cache nor Query Store retains program_name against a
-- historical query the way sys.dm_exec_sessions does for a still-open
-- session -- there is no direct DMV join from sys.dm_exec_query_stats
-- back to "which application ran this" once the session that first
-- compiled it has closed. Two honest ways to attribute 4a's results
-- to this application specifically, in order of reliability:
--
--   1. For a query CURRENTLY RUNNING, section 2b above already gives you
--      program_name directly (sys.dm_exec_requests is live, per-session
--      data) -- if the expensive statement in 4a is still running, it
--      will show up there with its session's program_name attached.
--   2. For a query that already finished, match 4a's `last_execution_time`
--      against this application's own audit log (docs/deployment-runbook.md
--      §6: every executed query is recorded there with its timestamp and
--      the generated SQL text) -- a timestamp-and-text match against the
--      audit log is a reliable historical attribution; a DMV-side join is
--      not available for this.
--   3. Recognise this application's queries by shape, as a heuristic: every
--      query local-sql-agent runs for a user's question is a
--      guard-validated read-only SELECT against this application's
--      warehouse tables (never a system catalog view, never DDL/DML) --
--      see docs/db-hardening.md. A statement in 4a that writes data did
--      not come from this application. One that is DDL, or that reads
--      INFORMATION_SCHEMA or sys.*, may have: when an operator runs them,
--      the deep deployment checks attempt one rolled-back CREATE TABLE,
--      and schema-drift checks and scripts/assign_datasources.py read the
--      catalogue (docs/deployment-runbook.md section 12).

-- 4c. If Query Store is enabled on <your_database> (check with the query
-- just below), it survives restarts and plan-cache eviction, and is the
-- more reliable source for "what has this database's workload looked
-- like over the last N hours/days" than the plan-cache queries above.
SELECT actual_state_desc, desired_state_desc, current_storage_size_mb, max_storage_size_mb
FROM sys.database_query_store_options;

-- If actual_state_desc is not 'OFF', Query Store's own top-resource-
-- consumers report (SSMS: Query Store > Top Resource Consuming Queries,
-- sorted by "Physical Reads") is the recommended way to read it --
-- reproducing that report as raw SQL against
-- sys.query_store_runtime_stats / sys.query_store_query / sys.query_store_plan
-- is possible but verbose; the GUI report is the same data, faster to read.


-- =============================================================================
-- 5. Login auditing / login triggers
-- =============================================================================
-- A SERVER AUDIT or a logon trigger that runs its own logic (a lookup, a
-- write, a check) on every new login event adds real work per new
-- connection, independent of what that connection goes on to query. This
-- compounds with a short connection-pool recycle interval or high churn:
-- more new connections means more login events means more trigger
-- executions.

-- 5a. Server-level logon triggers.
SELECT
    name,
    is_disabled,
    create_date,
    modify_date
FROM sys.server_triggers
WHERE parent_class_desc = 'SERVER'
  AND type_desc = 'SQL_TRIGGER';
-- (A logon trigger's definition text is visible via
--  sys.server_sql_modules / OBJECT_DEFINITION() for its object_id, for
--  whoever has permission to read it -- omitted here since reading
--  trigger *logic* is a one-time investigation, not a repeatable
--  diagnostic query.)

-- 5b. Server audits and their specifications (SQL Server Audit, distinct
-- from a logon trigger -- a server can have either, both, or neither).
SELECT
    a.name          AS audit_name,
    a.is_state_enabled,
    a.type_desc     AS target_type,
    s.name          AS audit_specification_name,
    s.is_state_enabled AS specification_enabled
FROM sys.server_audits AS a
LEFT JOIN sys.server_audit_specifications AS s
    ON s.audit_guid = a.audit_guid;


-- =============================================================================
-- 6. Wait statistics -- what the server has spent time waiting on
-- =============================================================================
-- Cumulative since the last restart (like the file-I/O counters in §3) --
-- most useful as a coarse "what kind of bottleneck is this server under
-- in general" signal, or compared as two snapshots the same way §3 is.
-- PAGEIOLATCH_* waits mean waiting on physical page reads; a high count
-- alongside the physical-reads ranking in §4 corroborates a genuine
-- disk-bound query, rather than something else being blamed for it.
SELECT TOP (20)
    wait_type,
    waiting_tasks_count,
    wait_time_ms,
    max_wait_time_ms,
    signal_wait_time_ms
FROM sys.dm_os_wait_stats
WHERE wait_type NOT LIKE 'SLEEP_%'         -- idle background-task waits,
  AND wait_type NOT LIKE 'BROKER_%'        -- not workload-relevant
  AND wait_type NOT IN (
        'CLR_SEMAPHORE', 'LAZYWRITER_SLEEP', 'RESOURCE_QUEUE',
        'SQLTRACE_BUFFER_FLUSH', 'WAITFOR', 'LOGMGR_QUEUE',
        'CHECKPOINT_QUEUE', 'REQUEST_FOR_DEADLOCK_SEARCH',
        'XE_TIMER_EVENT', 'BROKER_TASK_STOP', 'CLR_MANUAL_EVENT',
        'CLR_AUTO_EVENT', 'DISPATCHER_QUEUE_SEMAPHORE',
        'FT_IFTS_SCHEDULER_IDLE_WAIT', 'XE_DISPATCHER_WAIT',
        'XE_DISPATCHER_JOIN', 'BROKER_RECEIVE_WAITFOR',
        'ONDEMAND_TASK_QUEUE', 'BROKER_TRANSMITTER', 'SQLTRACE_INCREMENTAL_FLUSH_SLEEP'
    )
ORDER BY wait_time_ms DESC;
