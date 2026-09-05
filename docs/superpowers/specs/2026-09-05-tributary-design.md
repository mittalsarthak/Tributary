# Tributary — Schema Version Control for Postgres

**Date:** 2026-09-05
**Status:** Approved, in implementation
**Time budget:** ~10-12 hours (single day)

---

## 1. Problem framing

The brief: *build branch, diff, and merge for database schemas — branch a schema, evolve it
independently (add, drop, rename, retype columns; change constraints and indexes; create and drop
tables), then see exactly what diverged and merge back.*

Two constraints attached to it:

1. Not just SQL as output — the product must apply schema changes to a real database.
2. It must work smoothly even when a table holds ~5GB of data.

### 1.1 Who this is for

A team sharing one Postgres database. Today, changing the schema means hand-writing a migration,
hoping it is correct, and hoping it does not take production down. Two things are missing:
a safe place to experiment, and a truthful account of what changed.

### 1.2 The interpretation that drives everything

This is **schema** version control, not **data** version control.

That single call decides the architecture. A branch clones structure only — zero rows — so
branching is instant whether the source table holds 5MB or 5GB. The 5GB constraint therefore
bites in exactly one place: **merge**, when DDL finally lands on the real, populated `main` table.

This is correct, not convenient. That is where it bites in production too.

### 1.3 The hard sub-problem being owned

A naive implementation emits `ALTER TABLE events ALTER COLUMN id TYPE bigint` and calls it done.
On a 5GB table that acquires an `ACCESS EXCLUSIVE` lock and rewrites every row — minutes of
downtime. Worse, that ALTER queues behind any in-flight query, and *while it waits it blocks every
query that arrives behind it*. One naive statement stalls the whole database.

Tributary's migration planner refuses to be naive. See §5.

The second hard sub-problem is renames (§4.3): a naive structural diff sees a renamed column as
`DROP a; ADD b`, which on a populated table silently destroys data.

---

## 2. Scope

### 2.1 In

- Introspect a live Postgres schema into a canonical snapshot.
- Branch: create a materialised, structure-only copy of a schema.
- Edit on a branch: create/drop table; add/drop/rename/retype column; nullability; defaults;
  constraints (PK, FK, UNIQUE, CHECK); indexes. Applied for real to the branch.
- Commit: immutable snapshot + operation log, parented into a DAG.
- Diff: structural, rename-aware, between any two commits.
- Merge: three-way against the merge base, with typed conflict detection and resolution.
- Plan: ordered, dependency-correct, safety-classified DDL, with unsafe forms auto-rewritten.
- Execute: against the real target schema, observable, cancellable, recoverable.
- Deployed web app, one-command local setup, meaningful tests, decisions.md.

### 2.2 Out (deliberate cuts, with reasons)

| Cut | Why it is right to cut for now |
|---|---|
| Row/data versioning | Different problem, an order of magnitude more work. The brief says schemas. |
| MySQL / other engines | No transactional DDL. The entire safety model differs; a half-done version is worse than an honest "Postgres only". |
| Rebase, cherry-pick, revert, history rewrite | Branch/diff/merge is the brief. These are git-completeness, not problem-completeness. |
| Auth, multi-tenancy | Demo is single-workspace. Adds no signal about the actual problem. |
| Free-text conflict resolution | take-ours / take-theirs covers every conflict class we detect. A SQL-text editor invites invalid states. |
| Real 5GB on the deployed instance | Host disk is smaller. Proven locally with committed benchmark output instead of faked. |

---

## 3. Architecture

One Postgres instance is the workspace. Three kinds of namespace inside it:

```
_tributary      metadata: commits, branches, merges, migration steps
main            the "production" schema — populated with real data
br_<name>       materialised branches — structure only, zero rows
```

Single FastAPI application, server-rendered with htmx. One container, one Railway service, one
Postgres addon. Under a one-day clock, "actually deployed" beats a nicer component model.

### 3.1 Module boundaries

Each module has one purpose and is testable alone.

| Module | Purpose | Depends on |
|---|---|---|
| `introspect` | live Postgres catalog → `Snapshot` | psycopg |
| `model` | `Snapshot`, `Change`, `Conflict`, `Plan` types + canonicalisation | — |
| `diff` | two snapshots (+ op log) → ordered `[Change]` | `model` |
| `merge` | LCA, three-way classify → merged changes + `[Conflict]` | `model`, `diff` |
| `planner` | `[Change]` + live table stats → safety-classified, ordered `Plan` | `model`, `introspect` |
| `executor` | run a `Plan` against a schema, observably and recoverably | `planner`, psycopg |
| `store` | commits, branches, merges — the metadata DAG | psycopg |
| `web` | FastAPI routes + htmx templates | all of the above |

The rule: `planner` never touches the network, `diff` and `merge` are pure functions over
snapshots. That is what makes the conflict and safety logic cheap to test exhaustively.

---

## 4. Snapshots, diff, and renames

### 4.1 Snapshot

A commit stores a canonical JSON snapshot of the whole schema:

```
tables: { name: { columns:     { name: {type, nullable, default, position} },
                  constraints: { name: {kind, definition, columns, refs} },
                  indexes:     { name: {definition, columns, unique, method, predicate} } } }
```

Read from `pg_catalog` (not `information_schema` alone) because we need `pg_get_constraintdef`,
`pg_get_indexdef`, and `format_type` to get definitions Postgres itself agrees with.

### 4.2 Canonicalisation

`varchar(50)` and `character varying(50)` are the same type. So are `int4`/`integer`,
`now()`/`CURRENT_TIMESTAMP`. Without normalisation the diff fills with phantom changes and nobody
trusts the tool. Normalisation happens once, at snapshot construction, so every downstream
consumer compares apples to apples.

### 4.3 Rename detection

Snapshots record **state**. Commits additionally record an **operation log** — the intent behind
the state change, captured when the user performs the edit in the UI.

Diff prefers intent when it has it. When diffing arbitrary snapshots with no op log, it falls back
to a heuristic: an unmatched dropped column and an unmatched added column with the same type and
adjacent position are reported as a probable rename, surfaced to the user for confirmation rather
than assumed.

The failure this prevents is not cosmetic. `DROP COLUMN email; ADD COLUMN email_address` on a
populated table destroys the data. `RENAME COLUMN` preserves it and is instant.

### 4.4 Change types

`CreateTable` `DropTable` `RenameTable` `AddColumn` `DropColumn` `RenameColumn`
`AlterColumnType` `SetNotNull` `DropNotNull` `SetDefault` `DropDefault`
`AddConstraint` `DropConstraint` `CreateIndex` `DropIndex`

---

## 5. The migration planner

Input: a list of changes. Output: an ordered, safety-classified plan of executable steps.

### 5.1 Ordering

Topological, by dependency: create tables before FKs that reference them; add columns before
indexes on them; drop dependents before dependencies. A plan that fails halfway because of
ordering is a plan that has already caused an outage.

### 5.2 Safety classification

Classified against the **actual measured table** — `pg_relation_size`, `reltuples` — not against
assumptions.

| Class | Meaning |
|---|---|
| `SAFE_METADATA` | catalog-only, instant at any size: add nullable column, add column with constant default (PG11+), drop column, any rename, drop constraint, drop index |
| `LOCK_BRIEF` | short lock, no full scan under it |
| `LOCK_HEAVY` | blocks writes for the duration — must be rewritten |
| `REWRITE` | full table rewrite under exclusive lock — must be rewritten |

### 5.3 Rewrites

| Naive DDL | Emitted instead |
|---|---|
| `ADD CHECK` | `ADD CHECK ... NOT VALID`, then `VALIDATE CONSTRAINT` in a separate transaction |
| `ADD FOREIGN KEY` | same NOT VALID / VALIDATE split |
| `CREATE INDEX` | `CREATE INDEX CONCURRENTLY`, outside any transaction, with INVALID-index cleanup on failure |
| `SET NOT NULL` | validated `CHECK (col IS NOT NULL)` first so PG12+ skips the scan, then `SET NOT NULL`, then drop the check |
| `ALTER COLUMN TYPE`, rewriting | shadow column + sync trigger + batched backfill by PK range + short swap transaction |
| `ALTER COLUMN TYPE`, binary-coercible | plain ALTER — `varchar(50)→varchar(100)`, `varchar→text` do not rewrite. Knowing the difference is the point. |

Every statement is preceded by `SET lock_timeout` with bounded retry and backoff, so a migration
never joins the lock queue and stalls the traffic behind it.

### 5.4 Pre-flight validation

Before a retype, probe real data for values that will not cast. Before `SET NOT NULL`, probe for
existing NULLs. Catch the failure in milliseconds instead of twenty minutes into a rewrite.

---

## 6. Merge semantics

Merge base = lowest common ancestor of the two branch heads over the commit DAG. For each object
path (`table`, `table.column`, `table.constraint`, `table.index`), compute base→ours and
base→theirs, then classify:

| ours | theirs | result |
|---|---|---|
| changed | unchanged | take ours |
| unchanged | changed | take theirs |
| changed identically | — | no-op |
| changed differently | | **conflict: modify/modify** |
| dropped | modified | **conflict: drop/modify** |
| added | added, different definition | **conflict: add/add** |
| renamed | modified | **conflict: rename/modify** |

All conflicts must be resolved (take-ours / take-theirs) before a plan will be generated.

### 6.1 Concurrency

Two simultaneous merges into `main` is a real failure mode, not a hypothetical.

- A merge into a target takes a Postgres **advisory lock** on that target.
- A merge is **rejected** if the target's head has moved since the diff was computed —
  *"main has 2 new commits; re-diff required."* Optimistic concurrency on the head commit.

---

## 7. Execution and failure

The merge runs as a job. Per-step status streams to the UI: step, safety class, elapsed, rows
backfilled / total. Cancellable.

- Transactional steps roll back atomically. Postgres has transactional DDL — a large part of why
  the engine choice is Postgres and not MySQL.
- Non-transactional steps (`CREATE INDEX CONCURRENTLY`, batched backfills) cannot roll back, so
  each has an explicit compensating cleanup: a failed CIC leaves an `INVALID` index behind, which
  is detected and dropped rather than left to rot in the catalog.
- A killed backfill is resumable — progress is checkpointed by PK range in `migration_steps`.
- Errors surface as sentences, not stack traces.

---

## 8. User journey

1. **Branch list** — branches with ahead/behind counts and last change. First-run empty state says
   what to do first.
2. **Schema editor** — edits apply to the real branch schema immediately and append to the op log;
   "3 uncommitted changes" → commit with a message.
3. **Diff view** — grouped by table, add/drop/modify, with a toggle revealing the generated DDL and
   its safety badge.
4. **Merge** — conflicts first, all must be resolved; then the safety report, stated plainly:
   *"⚠ This merge rewrites `events` (4.8 GB, 52M rows). Est. 6-9 min. Shadow-column backfill — no
   exclusive lock held longer than ~2s."*
5. **Live progress** — steps, states, backfill progress, cancel.
6. **History** — commit log per branch.

**First-run:** a pre-seeded workspace (`users`, `orders`, `events`) already populated, plus a
**grow `events` to 10M / 50M rows** control. The evaluator can inflate the table themselves and
watch the safety warnings change. The 5GB claim becomes testable rather than asserted.

---

## 9. Testing

Against a real Postgres via testcontainers. Mocks would prove nothing about lock behaviour.

**The centrepiece:** open a long-running read transaction against the target table, run a
migration concurrently, assert reads keep succeeding and the migration never blocks past
`lock_timeout`. That test is the thesis of the project, mechanised.

Also:

- every conflict class in §6, plus clean auto-merge cases
- LCA correctness over a genuinely branching DAG
- rename vs drop+add discrimination
- type-normalisation equivalence (`varchar(50)` ≡ `character varying(50)`)
- topological ordering with FK dependencies
- `CREATE INDEX CONCURRENTLY` never emitted inside a transaction
- kill a backfill mid-way → resume completes, no INVALID index left behind
- pre-flight catches a bad cast before the migration starts

---

## 10. Setup

`docker compose up` → app + Postgres + seeded demo workspace. One command, no steps.
Deployed to Railway from GitHub: Dockerfile, Postgres addon, `DATABASE_URL`.

---

## 11. Order of work

| Slice | Est. |
|---|---|
| Scaffold, compose, model, introspection | 1.5h |
| Snapshot canonicalisation, diff, op log | 1.5h |
| Branch create/materialise, editor UI | 2h |
| Three-way merge, LCA, conflicts | 2h |
| Planner, rewrites, executor, progress | 2.5h |
| Tests, Railway deploy, seed | 1.5h |
| decisions.md, README, polish | 1h |

**Degradation path if time slips:** the shadow-column backfill becomes *detected, warned, and
gated behind explicit confirmation* rather than fully automated. Everything else holds.
