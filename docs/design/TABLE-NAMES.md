# Qualified table names — decision record

Status: **implemented**. This document records the rule, the security
hole it closes, the residual gap that remains, and why an unqualified
reference to a table in another database is refused rather than guessed.

## Context

`schema.yaml`'s `tables` map used a bare table name (`Customer`) as its
key, and the guard (`security.sql_guard.validate_sql`) resolved every SQL
table reference by its bare name alone — the schema/database qualifier a
query wrote in front of it (`[sales].[Customer]`, `[hr].[Customer]`) was
never looked at. Two consequences followed from that, one a missing
feature and one a real security hole:

- A warehouse with the same table name in more than one schema
  (`sales.Customer` and `ref.Customer`, a real, ordinary shape for a
  warehouse that separates transactional and reference data) had no way
  to describe both: whichever one was entered second either collided with
  the first `schema.yaml` key or silently shadowed it.
- Because the qualifier was never checked, a key of `Customer` with
  `db_schema: sales` also let `SELECT ... FROM [hr].[Customer]` through
  the guard — a query could reach a table that is **not** in the
  allowlist just by naming a different schema in front of an allowlisted
  bare name. The allowlist was, in effect, a list of bare names only.

## Decision

- A `schema.yaml` table key may be bare (`Customer`) or carry 1–3 leading
  qualifier parts (`sales.Customer`, `OtherDb.dbo.Customer`,
  `Linked.OtherDb.dbo.Customer`) — up to `catalog.database.schema.table`,
  SQL Server's own limit. Keys are parsed with
  `sqlglot.exp.to_table(key, dialect="tsql")`
  (`schema_data.registry.split_table_key`), so `[sales].[Customer]` and a
  doubled `]]` bracket escape both work exactly as they would in a real
  query. Comparisons are case-insensitive and bracket-insensitive.
- The key **as written** stays the table's identity everywhere — in
  `TABLE_COLUMNS`, `relationships`, the datasource mapping
  (`datasource:`), the audit trail's `tables_touched`
  (`security.sql_guard.extract_touched_tables`), and the prompt's schema
  block (`Table: sales.Customer`). There is no second, shorter "canonical"
  name a qualified key gets reduced to — a query result, an audit record,
  or a `schema.yaml` diff all name the same string.
- A table's **effective qualifier** is the key's own leading parts when it
  has any, else `db_schema` (`schema_data.registry.effective_qualifier`).
  If a key is qualified *and* `db_schema` is also given, the two must
  agree, case-insensitively, part for part — `schema.yaml` validation
  fails otherwise (`SchemaConfig`'s `_table_keys_are_consistent_and_unambiguous`
  validator). `resolvable_columns`/`prefetchable_columns` need a qualifier
  from either source — a qualified key with no `db_schema` at all still
  satisfies them.
- **Two keys that resolve to the same (qualifier, bare name) pair,
  case-insensitively, fail validation** — the same table entered twice
  under different spellings. Two *different* qualifiers sharing a bare
  name (`sales.Customer`/`ref.Customer`) are not a collision; that is
  exactly the case this feature exists to support.
- One place parses and normalises every table key: `schema_data.registry`'s
  `TableRef`, `split_table_key`, `effective_qualifier`, `bare_table_name`,
  and `table_reference_sql` (the quoted T-SQL reference, `[q1].[q2].[Name]`
  or `[Name]` alone). Every consumer — the guard, `retrieval.value_resolver`,
  `retrieval.dimension_vocabulary`, `schema_data.drift`, the prompt's schema
  block — goes through these, never a second, independently-written
  dotted-name parser.

### Guard resolution

`security.sql_guard.validate_sql` resolves a table reference in three
steps (`_match_table_ref`, on the bare name + qualifier
`_table_ref_name_and_qualifier` reads off the parsed `exp.Table` node
via the same `schema_data.registry.table_ref_parts` the key parser uses):

1. **Candidates** are every `schema.yaml` key whose bare name equals the
   reference's bare name.
2. **A qualified reference** — the candidate whose effective qualifier
   equals the SQL reference's qualifier exactly (every part,
   case-insensitive) wins. A candidate with **no known qualifier at all**
   (a bare key with no `db_schema`) accepts any qualifier — this is a
   documented, residual gap (see below). No candidate matches → refused
   as `unknown_table`, `is_refusal=True`, naming every candidate's real
   reference ("did you mean `[sales].[Customer]`?") so a self-correction
   retry can fix it.
3. **An unqualified reference** is accepted only when exactly one
   candidate exists **and** its effective qualifier is empty or a single
   part. Several candidates → the new guard reason `ambiguous_table`
   (`CorrectableRejection`, `is_refusal=True`, `subject` the bare name,
   message listing every qualified reference to disambiguate with). One
   candidate whose only qualifier is multi-part (another database on the
   same server) → refused the same way an unknown table is: an
   unqualified reference to it would silently read the wrong database,
   which is worse than refusing, so it is not accepted "for convenience".

Aliases and qualified column references go through the same resolution:
an alias always maps to its table; a **bare, unqualified** table name maps
to its table only when exactly one `FROM`/`JOIN` source in the query
writes that bare name — a self-join of `sales.Customer`/`ref.Customer`
writes `Customer` twice, so a later unaliased, unqualified `Customer.Name`
column reference is left to the guard's existing lenient
"unresolvable-qualifier" path rather than mis-attributed to one side.

## Security tightening

An explicitly qualified reference must now match a known qualifier. A
deployment whose `schema.yaml` gives every table a `db_schema` sees the
`[hr].[Customer]` hole above close outright: a query naming any schema
other than the configured one for an allowlisted bare name is refused,
where it previously passed. This is a **behaviour change**, called out in
`CHANGELOG.md` as a security fix, not a silent tightening.

## The residual gap, and why it is left open

A table key with **no** qualifier at all — bare, and no `db_schema` —
cannot have its qualifier checked: there is nothing configured to check
it against. A reference to that table with any qualifier, or none, still
resolves. This is not an oversight; closing it would mean either (a)
refusing every reference that doesn't repeat the bare name's own
(nonexistent) qualifier, which would reject the ordinary unqualified case
every such table's `schema.yaml` entry was written for, or (b) inventing
a qualifier nobody configured. Instead: **setting `db_schema` on a bare
key is now recommended** specifically because it is what turns this
qualifier check on for that table — a deployment that wants the
`[hr].[Customer]`-style hole fully closed for every table should give
every table a `db_schema`, even a single-schema one. `docs/deployment-runbook.md`
and `project_config.example/schema.yaml`'s own header carry this
recommendation next to where `db_schema` is described.

## Why an unqualified reference to another database is refused, not guessed

A table whose only known qualifier is multi-part (`OtherDb.dbo`) lives in
a **different database** on the same server. SQL Server resolves an
unqualified table name against the *current* database's default schema —
writing just `Customer` when the real table is `OtherDb.dbo.Customer`
either fails outright (no such table in the current database) or, worse,
silently resolves to an unrelated table of the same bare name that
happens to exist in the current database. Accepting the unqualified form
"for convenience" would mean guessing which of those two outcomes the
model meant, with no way to tell — so this case is refused the same way
an unknown table is, naming the correct three-part reference in the
rejection message, rather than executing something that might not read
the table the question was about.

## Consequences

- Existing single-source, unique-bare-key deployments are unaffected:
  identical prompts, identical guard behaviour, identical everything for
  a `schema.yaml` that never repeats a bare name — enforced by this
  phase's own tests (`tests/test_qualified_table_names.py`'s prompt-
  rendering case) and by every pre-existing guard/prompt test continuing
  to pass unchanged.
- The prompt's schema block (`schema_data.registry.SchemaRegistry.build_schema_context`)
  renders a `Reference as: [q1].[q2].[Name]` line whenever a table's
  effective qualifier is multi-part **or** its bare name is shared by
  another key — not only for the multi-part case the mechanism already
  covered before this phase.
- `retrieval.value_resolver` and `retrieval.dimension_vocabulary` build
  their fixed SQL templates from a table key's **bare** name and its
  effective qualifier, via `schema_data.registry.table_reference_sql` —
  not from the key text itself, which would wrongly quote a qualified key
  like `sales.Customer` as one bracketed identifier.
- `schema_data.drift.check_schema_drift` maps a live `(schema, bare_name)`
  pair scanned per schema back to the `schema.yaml` key it matches, so two
  same-bare-name tables in different schemas are kept apart rather than
  collapsed into one identity; a warehouse-only table matching no key at
  all is reported schema-qualified when its schema is known, and exactly
  as before (a bare identity) when it is not.
