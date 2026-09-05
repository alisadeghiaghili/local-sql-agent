# Admin panel — architecture

Status: **agreed, not implemented.** This is the design contract, in the
spirit of `api-contract-v2.md`: decisions with their reasons, so a later
implementation can be checked against intent rather than guessed at.

---

## 1. Why this exists

Today an operator manages this system by editing `.env` and YAML files
over SSH and restarting. Concretely, issuing one analyst key means: run a
CLI script, copy a JSON fragment into `.env`, restart the server. Nothing
about that is reviewable, reversible, or attributable.

Meanwhile the system already computes a great deal that nobody can see.
`scripts/analyze_audit_log.py` produces latency percentiles, a failure
taxonomy, cache behaviour, correction-round counts, SQL-shape clusters
and per-model breakdowns — all of it reachable only by running a script
on the host.

So the first version of this panel is mostly **surfacing analysis that
already exists**, not building new analysis.

### The one thing that is genuinely missing

The audit trail records what happened *technically*. A query that
executed successfully and returned a plausible **wrong** number is
indistinguishable, in every artefact this system produces, from a correct
one. There is no channel through which anyone can say "this answer was
wrong".

That is the most dangerous failure class this system has, and it is
invisible to every tool we own. Closing it is the panel's first purpose,
not a nice-to-have.

---

## 2. Two admin roles

`Principal` currently carries `id`, `name` and `denied_columns`. There is
no role concept at all. The panel introduces two, and the line between
them is drawn by one rule:

> **Anything that changes who can see what data is the security admin's.
> Everything else is the operations admin's.**

| | Operations | Security |
|---|---|---|
| Create / disable / revoke a key | ✅ | |
| `denied_columns` on any principal | | ✅ |
| `schema.yaml` | | ✅ |
| Grant either admin role | | ✅ |
| `DB_CONNECTION_URL` | | ✅ |
| LLM endpoint key and base URL | ✅ | |
| Domain knowledge (aliases, rules, examples, metrics) | ✅ | |
| Audit log — aggregate and individual records | ✅ | ✅ |
| Feedback triage | ✅ | ✅ |
| Maintenance mode, cache controls, `verify_deployment` | ✅ | |

### 2.1 The escalation paths this closes

A split that can be walked around is decoration. Four paths would have
collapsed it:

**Issuing a key must not let the issuer choose its ACL.** Otherwise an
operations admin issues a key with empty `denied_columns` and uses it.
New keys get a restrictive default; only a security admin loosens it.

**`schema.yaml` is a data-access change wearing operational clothes.** It
is not merely "which tables exist" — it is the guard's allowlist. Adding
a table there makes it queryable by everyone. It therefore belongs to the
security admin, despite looking like routine maintenance.

**`DB_CONNECTION_URL` likewise.** An operations admin who could change it
could point the system at a *more privileged* login on the same database,
and queries would then reach what the read-only login could not. The LLM
endpoint key is different — it points outward and touches no data access
— so it stays operational.

**Only a security admin grants roles.** Otherwise operations grants
itself security.

### 2.2 Why domain knowledge is safely operational

Someone could write a few-shot example designed to steer the model toward
a denied column. It would not work: `denied_columns` is enforced in
`validate_sql`, not in the prompt.

The whole split holds for that reason — **enforcement lives in the guard,
not in the text**. And that is also precisely why `schema.yaml` is the
exception: it moves the backstop itself.

### 2.3 Bootstrap, lockout, and dual capability

The first admin of each kind comes from the environment, never from a web
flow. An unauthenticated "create the first admin" page that nobody
remembers to close is a classic vulnerability, and an operations admin
must not be able to create the first security admin.

Removing the last admin of either kind is refused. Recovery from lockout
would otherwise be `.env` plus a restart.

One person may legitimately hold both capabilities. When they do, each
audit record must say **which capability authorised the action** —
otherwise "I was acting as operations" and "I was acting as security"
become the same entry, and the separation is invisible to whoever reviews
it later.

### 2.4 Mutual visibility, no mutual authority

Each role sees that the other acted, without being able to act. An
operations admin can read "this principal's `denied_columns` changed at
14:02, by this security admin" while being unable to change it.

Separation of duties is real only when each side observes the other.
Otherwise it is two locked doors in a dark room. This matters most for
the security admin, whose actions have no other supervisory mechanism.

---

## 3. What the panel contains

### Tier 1 — closes a loop nothing else closes

- **Wrong-answer feedback and triage.** An analyst flags an answer; an
  admin triages it into a golden-set case, a new few-shot example, or an
  alias fix.
- **Retrieval-miss review.** `scripts/analyze_misses.py` already derives
  synonym gaps from query logs. Surfacing them with a one-click "add this
  synonym" turns diagnosis into a fix.
- **Guard-rejection review.** Every rejection is either an attack, a
  mistake, or a **false positive that blocked a legitimate question**.
  Nobody reviews them today, which is why a too-narrow `schema.yaml`
  shows up as "some questions just never work" rather than as a finding.

### Tier 2 — already computed, currently invisible

- The reports `analyze_audit_log.py` produces.
- **Prefix-cache hit ratio over time.** The KV-cache architecture carries
  the latency budget and breaks silently: the suite stays green while
  every request pays full prefill. The status block already records the
  ratio; charting it makes an invisible regression visible.
- `verify_deployment`'s checks, on demand.
- Cache statistics and invalidation.

### Tier 3 — operations

- Key lifecycle and `denied_columns` (per §2).
- Per-analyst usage and rate-limit hits — the bucket is the
  `(principal, ip)` pair, and nobody can currently see whether its
  allowance is right.
- **Schema drift**: a read-only comparison of `schema.yaml` against the
  live database that *proposes* a diff. It never applies one.
- Vocabulary freshness: when each prefetched dimension column last
  refreshed, and a manual refresh. A hall added to the warehouse and
  never refreshed makes value resolution miss silently.
- Session and memory inspection, for support.

### 3.1 What the panel must never contain

- **A free-form SQL console.** Every admin panel is tempted by one. Here
  it means bypassing the guard entirely — undoing the security layer from
  inside the tool meant to administer it.
- **Result rows in the log viewer.** `observability/audit.py`'s first
  hard rule is that column *names* are recorded and row *values* never
  are, structurally: no field on the record can hold row data, and
  `__post_init__` rejects a `columns` list containing anything else. The
  panel inherits that. It must not become the place warehouse data leaks.
- **Reading a secret back.** See §4.3.
- **Deleting or editing an audit record.** An audit trail an admin can
  edit is not an audit trail, and it is the only supervisory mechanism
  over the security admin.

---

## 4. Three kinds of data, three homes

| data | home | why |
|---|---|---|
| Keys, ACLs, config versions, feedback | application database | durability, backup, stewardship |
| Audit log | append-only files on the app server | tamper-evidence |
| Secrets | environment | do not cross a new boundary |

### 4.1 Why the metadata belongs in a managed database

A SQLite file on the application server inherits that server's backup
posture, which is frequently none — nobody backs up a VM's `logs/`
directory. The data in question is not incidental: the key store is the
source of truth for authentication, the ACLs are a security control, and
the config history is the guard allowlist's provenance.

The presence of an organisation database is itself a signal that someone
owns that data: a DBA, a backup schedule, tested restores, a retention
policy, and an existing compliance perimeter.

So an external database is the **recommended** mode for a real
deployment. SQLite is the laptop and demo fallback, not the default
anyone should stay on.

### 4.2 Why the audit log does not go there

If the audit log moved into the organisation's database, anyone with
access to that database — including the DBA — could edit it. That
directly contradicts §3.1.

Append-only files on the application server are worse for backup and
better for tamper-evidence, and tamper-evidence is what a compliance
artefact is for. The consequence must be stated rather than discovered:
**the audit log needs its own backup arrangement**, or the durability
problem that motivated §4.1 simply reappears somewhere else.

### 4.3 Secrets stay in the environment

The panel may **set** a secret and must never **display** one. There is
no operational need to read a secret back: an operator who does not know
it rotates it. Displaying it puts a database credential into the DOM,
browser memory, screenshots and history.

`DB_CONNECTION_URL` is a special case and probably should not be
settable from the panel at all: reaching the database in order to learn
how to reach the database is circular, and storing one database's
credential inside another database is not an improvement. It stays in
the environment and accepts a restart.

The panel must be served over TLS or bound to loopback. A secret-setting
form over plain HTTP on an internal network is a secret on the wire.

---

## 5. The application database

### 5.1 It already exists

`logs/sessions.db` was introduced for session persistence. The panel adds
feedback, admin-action audit, the key store (see §5.5) and config
versions. An application database is happening either way; the only
choice is deliberate or accreted.

### 5.2 Backends

| configured | behaviour |
|---|---|
| nothing | SQLite, created automatically, one file |
| PostgreSQL / SQL Server / MySQL | tables created in a database that already exists |

**Only SQLite can be a genuine zero-configuration fallback.** PostgreSQL
is a server: making it appear automatically would mean shipping a binary
(and owning its patching), assuming a local instance and needing
`CREATE DATABASE` rights, or requiring a container runtime in an
environment that may be air-gapped. None of those is "handled
automatically". PostgreSQL is a first-class *configured* option instead.

### 5.3 Create tables, not databases

`CREATE DATABASE` requires rights a DBA will not grant an application.
The DBA provides an empty database and a login; the application creates
its tables inside it.

For organisations where schema changes go through change control, the
application must also be able to **emit the DDL for a DBA to apply**
rather than executing it.

### 5.4 Migration between backends is a tool, not a property

Starting on SQLite is only a trap if you cannot leave. And leaving is the
expected path, not an edge case: an organisation with no DBA today hires
one, gains a backup regime, and decides its metadata belongs on the
managed server. The design must assume that day arrives.

So this is a **first-class, shipped tool** — `SQLite → SQL Server`,
`SQLite → PostgreSQL`, and the reverse for taking a copy somewhere to
debug — not an emergent property of using SQLAlchemy. Backend-agnostic in
both directions falls out of one schema and one access layer, but only if
that is designed in from the start rather than retrofitted.

**What makes it more than copying rows:**

- **Identifiers must be preserved, not renumbered.** Audit records will
  carry a configuration version id (§6.2); feedback references a request
  id. Renumbering on insert silently turns those into dangling
  references, and nothing would fail loudly at the time.
- **Types must be mapped explicitly.** SQLite is dynamically typed and
  SQL Server is not. Timestamps, booleans and JSON columns each need a
  decided mapping rather than whatever the driver infers.
- **Insert order must respect referential integrity.**

**It must be verifiable.** A migration nobody can check is a migration
nobody should trust. Per table: row counts on both sides, plus a
content hash compared before and after — the same
order-insensitive-fingerprint idea `eval/fingerprint.py` already uses to
decide whether two result sets are "the same answer". A migration that
cannot prove equality is a failed migration, and must say so rather than
reporting success.

**It must never mutate the source.** Copy forward, verify, and only then
change the configuration to point at the new backend. The old database
stays untouched until an operator deletes it deliberately. That, not a
backup, is what makes the operation safe to attempt on a working system.

**It requires the application to be stopped or in maintenance mode.**
Otherwise writes land in the old database after the copy has read past
them and are lost with no error. This is a second use for the
maintenance mode in §3's tier 3.

**The export carries a schema-version stamp and the import refuses a
mismatch.** Exporting from one build and importing into a newer one means
the target's migrations must run first; silently loading an older shape
is how a subtly broken installation is created.

**The export is sensitive.** It contains key hashes and column ACLs. Not
secrets in the `.env` sense, but not something to leave in a shared
folder either — treat it like the `project_config` bundle.

**What it is not:** live or continuous replication. One shot, offline,
verified, reversible by virtue of the source being intact.

### 5.4.1 What actually forces the move

Not row volume. SQLite serialises writes, which is irrelevant at this
system's concurrency. It is:

- **a network filesystem** — SQLite's locking is unreliable on NFS/SMB
  and can corrupt the file. This is a common on-premise trap and belongs
  in the runbook.
- **more than one application instance** — two processes on two hosts
  cannot share the file. The moment a second instance exists for
  availability or load, migration is mandatory.
- **or simply that someone is now responsible for the data** — which is
  the reason in §4.1, and the most likely one in practice.

### 5.5 Two decisions that collide

Immediate revocation (§5.6) means keys are read at call time. Keys in an
organisation database then mean **a network round trip to another server
per request**.

Resolving this needs a short-TTL cache with explicit invalidation on
revocation. Left undesigned, one of the two decisions quietly becomes
false: either revocation is not immediate, or every question pays a hop.

### 5.6 Revocation must be immediate

`API_KEYS_JSON` is read at start-up today. A "disable" button that takes
effect at the next restart is not a disable — "tomorrow morning" is not
an answer for a leaked key.

Keys therefore move out of start-up configuration into the application
database, read at call time.

### 5.7 Consequences to accept

**Start-up now depends on the application database.** A new failure mode
exists: the app database is down and the server will not start even
though the warehouse and the model endpoint are healthy. Whether that
fails closed (consistent with this project's posture elsewhere) or
degrades is a decision to make explicitly, not to discover.

**The application database must never be the warehouse connection.**
`docs/db-hardening.md` specifies a read-only login and
`database/executor.py` rolls back every transaction. The application
database needs writes. Separate connection, separate credentials, and a
start-up check that refuses to run if the two are the same — otherwise
the read-only posture is undone by configuration.

**Migrations become a permanent cost.** `session/persistence.py` uses raw
`sqlite3` with `CREATE TABLE IF NOT EXISTS`. Multi-backend support means
porting it to SQLAlchemy *and* adopting a migration tool.

**A restore can un-revoke a key.** Restoring the application database to
an earlier point restores the key hashes that existed then — including
one revoked because it leaked. Backup is the cause here, not the cure.
Revocation is recorded as a tombstone with a timestamp, and a restore
must trigger a key review. That belongs in the runbook, not in someone's
memory.

---

## 6. Configuration versioning

Not git. The requirement is history, diff, rollback and attribution; git
is one implementation and it loses here:

- concurrency in a web panel becomes merge conflicts, which is worse than
  optimistic locking;
- "who changed this principal's `denied_columns`?" is a per-entity query,
  and git offers commits over files;
- it needs a binary and a remote, and a local-only history is not a
  backup;
- and `project_config/` is deliberately outside the repository. An
  in-application git must never touch the project repository.

### 6.1 Shape

Full snapshots, not diffs, so restoring any depth is a read rather than a
replay.

**Bundle-versioned with per-file restore.** The whole configuration set
is one version, so "the complete state at time T" is always one row — but
a single file may be restored from any version, and doing so creates a
new bundle version.

**Revert semantics, never reset.** Restoring an old version creates a
*new* version whose content equals the old one. Intervening history is
never destroyed, and the restore is itself recorded and itself
reversible — which matters, because restoring the wrong version is a
thing that happens.

### 6.2 What a table does better than git here

**Validated rollback.** Git restores a file blindly. A `schema.yaml` from
five versions ago may name tables the warehouse no longer has — and since
that file is the guard's allowlist, restoring it can make working
questions start failing. A rollback runs the same validation and dry-run
as a forward edit: show the diff, run the schema snapshot test, run the
golden set, then ask.

**Answer provenance.** If every audit record carries the configuration
version identifier, "which configuration produced this wrong answer?" and
"did rolling back fix it?" become queries. Git cannot know which answer
came from which commit. This is the instrumentation the improvement loop
in §3 needs.

**The prefix version can derive from the configuration version.** Domain
knowledge feeds the static prompt prefix. Today an innocuous-looking edit
changes that prefix, invalidates the KV cache, and halves throughput with
no error anywhere. With versioned configuration the change has an
identity and appears in the LLM status block: one of this system's worst
silent failures becomes visible.

### 6.3 Keeping what git genuinely offered

Offline inspection with familiar tools, and an off-box backup.

On every version, the YAML bundle is also written to a configured
directory. If that directory is a git repository — the operator's own,
never the project's — every change becomes a commit, and both properties
come back without the system's correctness depending on git being
installed.

Git as an **output**, not as the engine.

### 6.4 Propose and approve

`schema.yaml` belongs to the security admin, which leaves an operations
admin able to see that the warehouse gained a table but unable to add it.

Versioning resolves this: operations creates a **draft** version, with
its diff and dry-run results attached; security approves or rejects. The
review flow git would have provided, inside the same mechanism and
recorded in the same audit trail.

---

## 7. Prerequisites in today's code

Four concrete gaps, each of which blocks part of the above:

1. **`AuditRecord` has no `session_id` or `turn_id`.** Feedback triage
   cannot link a flagged answer back to the conversation it came from.
   Nothing else in §3 works without this.
2. **`Principal` has no role concept** — only `id`, `name`,
   `denied_columns`.
3. **`API_KEYS_JSON` is read at start-up**, so revocation cannot be
   immediate (§5.6).
4. **`session/persistence.py` is hardcoded to `sqlite3`**, so no other
   backend is reachable (§5.4).

---

## 8. Deliberately out of scope

- **Editing the audit trail.** Never, by anyone, through this panel.
- **A second shared "master password".** A shared secret is a weaker
  primitive than the per-principal keys this project already has, and it
  cannot answer *which* holder acted — the exact reason per-analyst keys
  exist. Extra friction for dangerous actions is step-up
  re-authentication with the admin's own key, not a second shared secret.
- **Cookie or browser-session authentication.** The API uses bearer
  tokens, which makes CSRF largely moot. Introducing cookies for panel
  convenience would reintroduce it on the highest-privilege surface in
  the system.
- **A parallel write path.** The panel is a client of the API, like
  `web/`. A second route to the same state will drift from the first, and
  drift in the layer that changes the security allowlist and the database
  credential is where it must not happen.

---

## 9. Still to be decided

- Does start-up fail closed or degrade when the application database is
  unreachable (§5.7)?
- Is the propose-and-approve flow (§6.4) worth its weight in the first
  version, or does it wait?
