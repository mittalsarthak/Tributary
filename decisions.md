# Decisions

This is not a changelog. It is a log of the real calls made while building Tributary, in roughly
the order they were made, each with what was considered, what was chosen, and what it cost. Where
a call turned out wrong, that is recorded too — the wrong calls are as much a part of the record as
the right ones.

Tributary is branch/diff/merge version control for Postgres schemas, applied to a real database.
The brief: build branch, diff, merge for schemas; make it actually run against a real database; make
it not fall over on a table with ~5GB of data. Everything below is in service of that last clause,
because that is the part a demo can fake and a real database cannot.

---

## 1. Schema version control, not data version control

**Decision.** A branch clones structure only — zero rows. `CREATE TABLE` + constraints + indexes,
replayed from the snapshot; never `INSERT ... SELECT`, `CREATE TABLE ... AS`, or any row copy.

**Alternatives considered.** Copying rows into each branch (full data versioning), or copy-on-write
row versioning (a changelog of row-level deltas per branch).

**Reasoning and tradeoff accepted.** The brief says schemas, and data versioning is a different,
much larger problem — an order of magnitude more work, and orthogonal to what's being asked. The
decision that actually shapes the architecture is this one: because branching touches no rows,
branching is instant whether the source table holds 5MB or 5GB. That pushes the entire 5GB problem
to exactly one place — merge, when a schema change from a branch finally lands as DDL on the real,
populated `main` table. That is also where it bites in production, so the architecture matches the
real failure mode instead of a convenient one.

**What it cost.** A branch is not a fork of the data for testing migrations against realistic
volumes — you can't branch, load fixture rows, and see how a query performs on your own copy. That
capability doesn't exist. Given the time budget, that's the right thing to not have.

---

## 2. Postgres only

**Decision.** No engine abstraction. Every DDL string, every catalog query (`pg_catalog`, not
`information_schema` alone), every lock-safety guarantee is Postgres-specific.

**Alternatives considered.** An abstraction layer supporting Postgres and MySQL, or Postgres now
with MySQL "later."

**Reasoning and tradeoff accepted.** Transactional DDL is the entire safety model. A failed
transactional migration in Postgres rolls back atomically — nothing is left half-applied. MySQL
does not have transactional DDL (a `CREATE TABLE` or `ALTER TABLE` commits immediately and cannot
be rolled back mid-sequence), so a MySQL port isn't a driver swap, it's a different product with a
different failure model, needing its own recovery story for partial application. Committing to
"Postgres, and doing it right" over "both engines, done shallowly" is the same judgment as the cut
below: a half-done version of the safety claim is worse than an honest single-engine one, because a
half-done version would still market its safety guarantee to a user who is quietly not covered by
it.

**What it cost.** No MySQL users. Given the take-home's framing (evaluated on whether the safety
claim is real), this is the right trade — a MySQL-compatible version with a weaker safety model would
have undercut the one thing being demonstrated.

---

## 3. Branch = a Postgres schema, not a database or an instance

**Decision.** A branch materialises as a schema (`br_<name>`) inside the same Postgres instance and
database as `main`, not as a separate database or a separate server.

**Alternatives considered.** A branch as a separate Postgres database (`CREATE DATABASE`), or a
separate instance/container per branch.

**Reasoning and tradeoff accepted.** A schema is namespace-cheap: creating one is a metadata
operation, not a resource allocation. A separate database or instance per branch would mean
provisioning cost per branch, no ability to reference `main` from a branch context in the same
connection, and infrastructure this project doesn't need to own (connection pooling per database,
instance lifecycle). A single instance with `_tributary` (metadata), `main` (production), and
`br_*` (branches) as schemas is the entire footprint, and it is what makes `docker compose up` the
whole setup.

**What it cost.** All branches share one instance's resources (disk, connections, WAL) — there's no
per-branch resource isolation. For a demo and for the stated scope (a team sharing one database),
that's the right level of isolation, not a missing one.

---

## 4. Snapshot + operation log, not snapshot alone

**Decision.** Every commit stores two things: a canonical structural snapshot (state) and an
operation log (intent — the sequence of edits the user actually performed, e.g.
`{"op": "rename_column", "table": "users", "old": "email", "new": "email_address"}`). Diff prefers
the op log when it has one and falls back to a heuristic only when it doesn't.

**Alternatives considered.** State-only diffing: compare two snapshots structurally with no
knowledge of how one turned into the other.

**Reasoning and tradeoff accepted.** State alone cannot distinguish a rename from a drop-and-add.
`users.email` disappearing and `users.email_address` appearing, with the same type, in the same
commit, is structurally identical whether the user renamed a column or dropped one and added an
unrelated one. The two read the same in a bare structural comparison, and they are not remotely the
same operation against a populated table: `DROP COLUMN email; ADD COLUMN email_address` destroys
every value that was in `email` and creates an empty column; `RENAME COLUMN email TO
email_address` preserves every value and is instant, a catalog-only change. Recording intent when
it's captured (in the editor, at the moment of the edit) means the tool never has to guess when it
already knows.

**What it cost.** The heuristic fallback (used only for diffing arbitrary snapshots that have no op
log — e.g. a schema introspected from outside Tributary) has to be conservative: it pairs an
unmatched dropped column with an unmatched added column only when the canonical type matches *and*
exactly one candidate exists on each side. Anything ambiguous is reported as a plain drop+add rather
than guessed, because a wrong rename guess is worse than an honest "these look unrelated." That
means some real renames, diffed without an op log, are reported as drop+add and a human has to
notice. Accepted: guessing wrong on a populated table is data loss; declining to guess is at worst an
annoying diff.

---

## 5. Canonicalising types (and defaults) at introspection, once

**Decision.** Every type and default string is normalised to one canonical spelling
(`canonical.norm_type`/`norm_default`) at the moment a snapshot is built, in `introspect.py`. Nothing
downstream (diff, merge, planner) re-normalises anything — it trusts that canonicalisation already
happened.

**Alternatives considered.** Comparing raw Postgres output directly, or normalising at diff time
instead of at introspection time.

**Reasoning and tradeoff accepted.** Postgres reports the same type or default under multiple
spellings depending on context: `character varying(50)` vs `varchar(50)`, `integer` vs `int4`,
`now()` vs `CURRENT_TIMESTAMP`. Without normalisation, diffing two structurally identical schemas
introspected through slightly different paths produces phantom changes — every index, every column,
every default reported as "modified" when nothing actually differs. A diff tool that's wrong that
often is a diff tool nobody trusts, which is worse than a diff tool with narrower scope. Normalising
once, at construction, rather than at every comparison site, means every downstream consumer can use
plain `==` and be correct, instead of every comparison needing to know about aliasing rules.

**What it cost.** The alias table (`canonical.py`) has to be maintained by hand and is necessarily
incomplete — `numeric`/`decimal`, the timestamp-with-precision family, and so on had to be worked
through explicitly; an exotic type spelling not in the table would still cause a phantom diff. The
type-modifier regex was tightened mid-build (R8) specifically because the first cut silently dropped
the tail of `timestamp(3) with time zone`, which would have been exactly this failure mode.

---

## 6. Three-way merge over object maps, not replay of change lists

**Decision.** `merge.py` flattens each of base/ours/theirs to a map keyed by object path
(`("users",)`, `("users","col","email")`, `("users","con","users_pkey")`, `("users","idx","ix_x")`)
and classifies each path independently by comparing the three values at that path.

**Alternatives considered.** Replaying and reconciling the two branches' `diff.py` change *lists*
against each other — i.e., treating "ours" and "theirs" as ordered sequences of edits and merging
the sequences.

**Reasoning and tradeoff accepted.** Change lists are order-dependent, and reconciling two of them
requires reasoning about how edits at different positions interact — a rename on one side against a
type change on the other, diffed independently, produce two edits that don't obviously refer to the
same object unless you re-derive that link. An object map sidesteps that: every conflict class in
the design (modify/modify, drop/modify, add/add, rename/modify) falls out of a three-way comparison
at a single stable key, with no ordering to reconcile at all. This was rejected as the naive
first idea (replay) once it became clear that rename-vs-modify was the exact case the design most
needed to get right, and replay was the case where that reasoning got hardest, not easiest.

**What it cost.** Nothing structural — object maps are strictly simpler to reason about and to test
(one test per conflict class, plus the merge-base test, cover the space cleanly). The cost, if any,
is that a change's *history* within a branch (the sequence of edits that produced the current state)
is not visible to the merge — only the net state at base/ours/theirs. That's consistent with
decision #1 (schema, not data, and here: net state, not edit history, is what merge needs).

---

## 7. Conflict suppression beneath a conflicted table path

**Decision.** When a table-level path itself conflicts (e.g. dropped on one side, modified on the
other), every conflict that would otherwise be reported for that table's columns, constraints, and
indexes is suppressed. Only the table-level conflict is shown.

**Reasoning and tradeoff accepted.** Without suppression, dropping a 30-column table on one branch
while another branch modifies one of its columns reports 31 conflicts: one for the table and one for
every column beneath it, all really the same disagreement ("does this table still exist"). A UI
listing 31 conflicts for one real disagreement is a UI nobody can use — the reviewer would have to
resolve 31 radio buttons to express one decision. This was caught during Task 7's own build: the
brief's own suppression test used `any(...)` over the conflict list, which passes whether there are
1 conflict or 31 — it could never actually detect the regression it was meant to catch. A stricter
"exactly one conflict" test was added alongside it, unprompted, because a test that can't fail is
worse than no test.

**What it cost.** Resolving the table-level conflict (take-ours/take-theirs) implicitly resolves
everything beneath it — you cannot, in the current design, say "keep the table but cherry-pick which
column change wins" when the table itself is in dispute. That's an acceptable simplification: the
table-level conflict is rare, and the sub-conflicts it would otherwise generate carry no information
a human actually needs beyond "which side wins."

---

## 8. Shadow-column backfill over plain `ALTER COLUMN TYPE`, with a size threshold

**Decision.** A column retype that is not binary-coercible (`is_binary_coercible` recognises only a
narrow safe set — identical types; `varchar(n) → varchar(m)` widening or dropping the limit;
`varchar → text`; `text → varchar` with no length; `numeric(p,s) → numeric` with no modifier —
everything else, *including* `int4 → int8`, changes the base type and is not coercible) is classified
`REWRITE` only above a size threshold (`_LARGE_ROWS = 1_000_000` rows, `_LARGE_BYTES = 100MB`,
whichever the table exceeds), and only then is it rewritten into shadow column + sync trigger +
batched backfill (10,000 rows/batch by default) + a short swap transaction, instead of a single
blocking `ALTER`. The benchmark's headline number (`bench/RESULTS.md`) is exactly this path: a real
`int4 → int8` retype on a 28.4M-row table, rewritten into the shadow-column dance because that
retype is not coercible.

**Alternatives considered.** Always shadow-backfill any non-coercible retype, regardless of size;
or never shadow-backfill and just warn.

**Reasoning and tradeoff accepted.** A plain `ALTER TABLE ... ALTER COLUMN TYPE` that isn't
binary-coercible takes `ACCESS EXCLUSIVE` and rewrites the whole table under that lock. On a small
table that's milliseconds and not worth the complexity of a shadow column, a trigger, checkpointed
batches, and a swap. On a multi-GB table it's minutes of total unavailability. The threshold makes
the plan match the actual risk instead of always paying the shadow-dance's complexity tax or always
accepting the naive risk.

**What it cost.** The threshold is a judgment call, not a law of physics — a table just under
100MB/1M rows with a slow disk could still stall meaningfully under a plain `ALTER`, and a table just
over the threshold pays the shadow-dance's overhead (extra column, trigger maintenance, batched
backfill, a second swap transaction) when the plain form might have been fine on a fast box. Erring
toward the size-based split rather than tuning it per-hardware was deliberate: the number is visible
and adjustable (`_LARGE_ROWS`/`_LARGE_BYTES` in `planner.py`), and the plan the UI shows before
anything runs states which path was chosen and why.

---

## 9. `lock_timeout` + bounded retry, not "just wait"

**Decision.** Every DDL-kind step is preceded by `SET lock_timeout`, and the executor retries a
`LockNotAvailable` up to 3 times with 1s/2s/4s backoff before failing with a named-table error. It
never waits indefinitely for a lock.

**Reasoning and tradeoff accepted.** Postgres's lock manager is a fair FIFO queue: once a migration's
`ACCESS EXCLUSIVE` request is queued, every read or write that arrives *after* it also queues behind
it, even though those queries don't conflict with each other. A migration that blocks on an
already-held lock for however long it takes doesn't just wait — it turns into a dam that backs up
every subsequent query on that table, exactly the outage this project exists to prevent. Failing
fast and retrying with backoff means the worst case is a failed migration (visible, retriable,
bounded), not a stalled database (invisible until someone notices every query is hanging).

Task 10's `test_migration_gives_up_rather_than_holding_the_lock_queue` states this directly: an
`ACCESS EXCLUSIVE` lock held hostage causes the migration to fail within bounded time (well under
the 20s the holder is willing to wait), never to hang. R4 (see §"bugs the process caught" below) is
the flip side of the same property.

**What it cost.** A migration can fail transiently under real, benign contention — a long-running
report query holding a conflicting lock briefly — and needs to be retried by a human or a script,
rather than Tributary silently waiting it out. That's the trade the whole project is built around:
slower/more visible failure, never silent stalling.

---

## 10. Fail safe on unknown table size

**Decision.** `TableStats.rows` is `int | None`, where `None` means "never analysed," never `0`. The
planner's size decision is driven primarily by `bytes` (`pg_total_relation_size` — real, exact,
on-disk usage, never an estimate), with `rows` as a secondary signal, and `rows is None` must never
classify a table as small.

**Reasoning and tradeoff accepted.** `pg_class.reltuples` is `-1` until a table has been `ANALYZE`d
at least once. The brief's own reference implementation clamps this with `GREATEST(reltuples, 0)`,
which turns "unknown" into "zero." A freshly restored, never-analysed 5GB table — arguably the most
realistic setup of all, since a table restored from a dump or freshly loaded hasn't been analysed yet
— would read as 0 rows, be classified "small," and receive the exact naive rewrite this project
exists to prevent. This was caught as R12 during Task 8's dispatch, before any code shipped it, and
is exactly the outage-shaped bug the whole project is supposed to make impossible.

The asymmetry is the entire argument, and it's worth stating plainly: over-classifying a small table
as large costs a slower migration — one visible in the plan the UI shows before anything runs, and
nothing more. Under-classifying a large table as small costs an outage — an `ACCESS EXCLUSIVE` lock
held for the full duration of a multi-GB rewrite, with every query behind it queued. Those two costs
are not remotely the same size, so ties must resolve toward "large."

**What it cost.** A table that's genuinely small but never analysed pays the more conservative
(slower) migration path unnecessarily until it's analysed. Cheap, and visible in the plan.

---

## 11. htmx over React

**Decision.** Server-rendered Jinja2 templates plus htmx (and Tailwind via CDN) for the whole UI, not
a client-side framework with its own build step.

**Reasoning and tradeoff accepted.** The project has to actually deploy, in one container. htmx needs no build step, no bundler, no separate frontend/backend deploy — the same
`pip install .` that installs the Python package is the entire frontend toolchain. A React SPA would
need its own build pipeline (baked into the Dockerfile or run ahead of time and committed), a second
language's dependency tree, and a client/server API contract to design and keep in sync. None of that
buys anything the demo needs; it only adds surface area to get wrong under time pressure. "One
container, no build step" is a real architectural constraint here, not a stylistic preference.

**What it cost.** No rich client-side interactivity — the merge conflict form, the live progress
panel, and the grow-table poll are all either plain form posts or htmx's own polling
(`hx-trigger="every 2s"`) and SSE, not a reactive client state store.

---

## 12. What was cut, and why each cut is right for now

| Cut | Why it is right to cut here |
|---|---|
| Row/data versioning | A different, larger problem (see #1). The brief asks for schemas. |
| MySQL / other engines | No transactional DDL — the whole safety model changes (see #2). A shallow multi-engine version would be worse than an honest single-engine one. |
| Rebase, cherry-pick, revert, history rewrite | Branch/diff/merge is the actual brief. These are git-completeness, not problem-completeness — they don't touch the 5GB safety claim at all. |
| Auth, multi-tenancy | The brief's demo is single-workspace. Adds surface area, not signal about the actual problem being evaluated. |
| Free-text conflict resolution | take-ours/take-theirs covers every conflict class the merge actually detects. A SQL-text editor for resolving a conflict invites hand-written DDL that never goes through the planner's safety classification at all — the one thing this project is supposed to guarantee. |
