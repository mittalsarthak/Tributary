# SDD ledger — plan: docs/superpowers/plans/2026-09-05-tributary.md

Spec: docs/superpowers/specs/2026-09-05-tributary-design.md (read, binding authority)
Branch: build-tributary
Started: 2026-09-05

## BINDING USER INSTRUCTION (2026-09-05, mid-build)

**Nothing is pushed to GitHub.** No `gh repo create`, no `git remote add`, no `git push`, no PR.
This overrides the plan as written and overrides the assignment's own "GitHub repository with the
code" deliverable — the user publishes when and how they choose. Work stays local on
`build-tributary`.

Consequences already applied:
- Plan Task 13 Step 3 rewritten from "push to GitHub" to an explicit do-not-push.
- Railway's GitHub-source deploy flow is therefore unavailable. Task 13 now deploys from the local
  directory via `railway up`, which uploads the working directory and never touches GitHub.
- `railway` CLI is not installed and the user is not authenticated, so Task 13 needs the user in the
  loop for `railway login`. Not something to work around silently.
- Keep the history clean and one-commit-per-task so a single `git push` later is all it takes.

## Pre-flight scan

### Cross-task interface pairs (shared file or shared symbol)

| Pair | Produced → consumed | Finding |
|---|---|---|
| T2 → T3,4,5,7,8,9 | `Column(name,type,nullable,default,position)` | OK — all call sites use 5 positional args |
| T2 → T5,8,9,10 | `Index(name,definition,columns,unique,method,predicate)` | OK — defaults cover every 3-arg call site |
| T2 → T3,8 | `Constraint(name,kind,definition,columns)` | OK |
| T2 → T3,8 | `TableStats(rows,bytes)` | OK |
| T2 → T8,9 | `Step(...)`, `Plan(steps,warnings)`, `Safety` | OK — T9's 6-positional `Step` relies on declared defaults |
| T3 → T8 | `table_stats` → `plan(stats=...)` | OK — both `dict[str, TableStats]` |
| T8 → T9,10 | `plan(changes, stats, schema, *, lock_timeout, batch_size)` | OK — all call sites match |
| T9 → T10 | `executor.run(dsn, schema, plan, merge_id, on_progress)` | OK |
| T6 → T7 | `store.ancestors` → `merge.merge_base` | OK |
| T6 → T7 | `ws` pytest fixture | **CONFLICT — R2** fixture defined in `tests/test_store.py`, used by `tests/test_merge.py`. Not importable across modules. |
| T6 → T12 | `store.ensure_main` | **CONFLICT — R3** used by T6 fixture and T12 impl, absent from T6's Produces list |
| T5 → T11 | op-log entry shape `{"op","table",...}` | OK — `add_column`/`rename_column` naming consistent across both |
| T4 → T8 | `ddl.render` vs planner's avoidance of plain ALTER TYPE | OK — not contradictory; planner chooses, ddl renders |

### Per-task self-consistency

| Task | Finding |
|---|---|
| T1 | **CONFLICT — R1** `pg_dsn` fixture mixes `return existing` with `yield`; in a generator, `return` yields nothing and the fixture breaks |
| T2 | **CONFLICT — R8** one-line `class X: a: str; b: str` is valid but unreadable; unused `replace` import; `norm_type` drops the tail of `timestamp(3) with time zone` |
| T3 | OK — `_IDX` correctly excludes constraint-backed indexes; test uses a standalone unique index, which is included |
| T4 | OK — tests assert exact quoted SQL; round-trip test executes it |
| T5 | **CONFLICT — R9** op-log test and heuristic test assert the same outcome on the same input, so the op log is never shown to be load-bearing |
| T6 | OK apart from R2/R3 |
| T7 | OK — one test per conflict class, plus real-DAG merge-base test |
| T8 | **CONFLICT — R5** `test_every_ddl_step_is_preceded_by_a_lock_timeout` is an `or` of two weak clauses and can pass vacuously |
| T9 | **CONFLICT — R6** `__import__("tributary.model", fromlist=[...])` inline instead of a normal import |
| T10 | **CONFLICT — R4** test 1 is factually wrong (see ruling) |
| T11 | **CONFLICT — R7** `client` fixture never defined; `/merges/latest/run` is not a route the plan declares |
| T12 | OK |
| T13 | OK — `${PORT:-8000}` correct for Railway |
| T14 | OK |

### Rulings

**R1 — T1 `pg_dsn` fixture.** A pytest fixture containing `yield` is a generator; `return existing`
terminates it without producing a value, so every test would error with "fixture returned None".
Ruling: the env-var branch must be `yield existing; return`. Cost if wrong: none — this is a plain
Python semantics bug, not a judgment call.

**R2 — `ws` fixture placement.** Ruling: T6 defines `ws` in `tests/conftest.py`, not in
`tests/test_store.py`, so T7's merge-base test can use it. Cost if wrong: a slightly broader
conftest than strictly needed.

**R3 — `store.ensure_main`.** Ruling: added to T6's produced interface as
`store.ensure_main(conn) -> Branch`, registering an existing `main` schema as a branch with an
initial commit. It is what makes an already-populated database adoptable rather than requiring a
greenfield one, and both T6's fixture and T12's seeder already call it. Cost if wrong: none, it was
an omission in the Produces list rather than a design question.

**R4 — T10 test 1 is factually wrong, and this is the important one.** As written, a reader holds
`ACCESS SHARE` for the life of its transaction; `ADD COLUMN` needs `ACCESS EXCLUSIVE`, which
conflicts. The migration therefore *cannot* succeed while that transaction stays open — it will
exhaust its retries and fail. The test asserts it completes in under 10s, so it would fail against a
correct executor, and the tempting "fix" is to remove `lock_timeout`, which destroys the property
the whole project exists to demonstrate.
Ruling: reframe test 1 to model realistic OLTP traffic — a reader issuing *short* transactions in a
loop rather than one long-open transaction. Assert both halves of the real property: the migration
completes promptly, and no individual read is starved. The long-held-lock case is already covered by
test 2, which correctly asserts bounded failure. Together they state the true claim: *a migration
either gets its lock quickly or gives up quickly; it never stalls the queue behind it.*
Cost if wrong: if idle-in-transaction readers are in fact the case worth proving, the suite proves
the bounded-failure half but not a success path under them — no success path exists under them, so
this is the honest framing.

**R5 — T8 lock_timeout assertion.** Ruling: assert concretely — every step with
`kind == "ddl"` carries `lock_timeout` in its emitted SQL. Vacuous tests are worse than absent ones
because they claim coverage they do not have. Cost if wrong: none.

**R6 — T9 import style.** Ruling: normal module-level `from tributary.model import TableStats`.
Cost if wrong: none.

**R7 — T11 fixtures and routes.** Ruling: T11 adds a `client` fixture to `tests/conftest.py` wiring
`TestClient` to a `DATABASE_URL` pointed at the test Postgres with a seeded demo; the merge test
captures the real merge id from the create-merge response rather than the invented `latest` alias.
Cost if wrong: minor test-harness rework.

**R8 — T2 style and `norm_type` completeness.** Ruling: expand the one-line dataclasses to normal
multi-line form, drop the unused `replace` import, and require `norm_type` to preserve type tails so
`timestamp(3) with time zone` → `timestamptz(3)`. Cost if wrong: none.

**R9 — T5 op-log coverage.** Ruling: add a test where the heuristic *cannot* infer the rename but the
op log can — a column renamed and retyped in the same commit. Without it the op log's reason for
existing is untested, and a later refactor could delete it with the suite still green.
Cost if wrong: one extra test.

---

## Progress

Task 1: complete (commits 21e0562..ea33d98, review clean — spec ✅, quality Approved, 0 Critical, 0 Important)
Task 1: minor (deferred): `conn` fixture is documented "rolled back" in the brief's interface line but
  implemented autocommit — brief-internal inconsistency, no functional impact, `fresh_schema` is the
  real isolation primitive. **Carry into later dispatches: no test may assume `conn` rolls back.**
Task 1: minor (deferred): `filterwarnings` regex leaves `.` unescaped in `testcontainers.postgres`.
Task 3: complete (commits c674432..b48a40b — a62fc54 impl, 8621a13 R12, 0644f16 R13, fd68ede R14,
  b48a40b R17; 14/14 green). Review clean; R13/R14/R17 each passed a scoped re-review. R17's tests
  confirmed to carry real negative assertions (`"WHERE (status" not in ...`, `"(((email" not in ...`)
  so reverting `pretty=true` to the 1-arg form fails them — not a fake improvement.
Task 6: complete (commits b48a40b..b868ba5 — f489556 impl + b868ba5 fix round 1; 12/12, suite 141/141,
  review Approved, fix round re-review: all 3 addressed, no new breakage).
  Reviewer confirmed R15 genuinely captures main's index/constraint sets before and compares after;
  materialisation contains no INSERT/SELECT INTO/CREATE TABLE AS at all (zero rows structurally
  guaranteed, not just observed); branch sequences are schema-qualified so an experiment cannot share
  production's sequence.
Task 8: implemented (3030c25) + R19/R20/R21 (f6c7101). 34 tests, non-DB suite 119/119. Under review.

**R22 — MUST carry into Task 9's dispatch. Task 8's implementer caught this; it would have made
Task 9's headline test vacuous.** `task-9-brief.md`'s own `test_shadow_backfill_preserves_every_value`
calls `plan(...)` **without** `pk_columns`. After R19, a plan with no known PK deliberately takes the
conservative no-backfill fallback — so that test would run, pass, and exercise **none** of the
shadow-column backfill it is named for. A green test asserting nothing about the mechanism it claims
to cover is worse than no test, and it is the exact mechanism the whole project is built to
demonstrate. Task 9's dispatch must instruct passing `pk_columns={"events": "id"}` and must assert
the plan actually contains a `backfill` step before asserting the data survived.
Cost if wrong: none — this is strictly a correction to a brief written before R19 existed.

Task 8: implementer disclosed updating 2 assertions in `tests/test_diff.py` to include the new
  `nullable`/`default` fields on `AlterColumnType`. Claimed updated-not-weakened; flagged to the
  reviewer for specific verification, since a loosened assertion disguised as an update is exactly
  what review exists to catch.
Task 7: implemented (6a92fb5, 13 tests). Under review. Implementer independently spotted that the
  brief's own suppression test uses `any(...)` — which passes with 31 conflicts just as happily as
  with 1, missing the entire point — and added a stricter exactly-one test alongside rather than
  quietly satisfying the letter of the brief. Same instinct as R5 and R22, reached unprompted.
Task 11: complete (a6243e0..807f34a — fdcd654 impl, 807f34a fix round 1; 225 tests, all findings
  addressed, no new breakage).
  **Two Criticals, and the second only appeared because we closed a coverage gap.** (a) Every
  conflict's radio pair shared `name="side"`; HTML radio grouping is form-scoped, not fieldset-scoped,
  so 2+ conflicts collapsed into one group, the browser submitted a single `side`, and `zip(path,
  side)` silently truncated — dropping every conflict after the first, or worse, mispairing a "take
  theirs" decision onto a column the user never saw. All three existing conflict tests used one column
  named `x`, so the suite was structurally incapable of seeing it. (b) Writing the two-conflict test
  exposed that `can_run`/`run_merge` gated on the *frozen* `state.result.conflicts` rather than
  mutable `state.status` — **a resolved merge could never run at all.** The core workflow was broken
  end to end and nothing noticed, because no test had ever called the resolve route.
  Re-review confirmed the new test fails against either the old template or the old server
  independently, and that an *unresolved* merge remains unrunnable after the gating change (the
  regression that would have been worse than the bug).

**R24 — the UI is missing operations the problem statement explicitly names. My plan's error.**
Task 14's agent, writing the guided tour, discovered the schema editor has no "retype column" control
and that the tour step I specified (retype `events.id`) would be a no-op since the seed already makes
it `bigserial`. Verified: `apply_change` in `tributary/web/app.py` supports exactly four ops —
`add_column`, `drop_column`, `rename_column`, `rename_table`.
The problem statement says: *"add, drop, rename, and retype columns; change constraints and indexes;
create and drop tables."* So the product cannot do, through its own UI, three of the four things the
brief enumerates. The **engine** handles all 15 change types — `ddl.render` covers them, `diff`
detects them, the planner plans them, and they are tested — but a user cannot reach them.
This is a gap in my plan, not in anyone's implementation. The plan's Task 11 route table said
"POST /branches/{name}/changes — apply one edit + append to op log" and never enumerated which edits,
so the implementer built four and no review could catch a requirement the brief never stated. The
pre-flight scan should have cross-checked the route table against the problem statement's own list of
operations; it checked tasks against each other instead.
Ruling: fix it. Retype first (explicitly named and the one that exercises the shadow-column path the
whole project is built around), then create/drop table, then constraints and indexes.

**R25 — Task 13 was never dispatched. Also mine.** No `Dockerfile`, no `.dockerignore`, no
`railway.json`. `docker compose up` — documented in the README as the entire local setup — cannot
work, because the compose file's `app` service does `build: .` against a Dockerfile that does not
exist. Caught by Task 14's agent flagging that it was documenting a designed setup rather than a
verified one. Dispatching now.

**R23 — residual 2 `on_event` deprecation warnings: deferred, not fixed.** Test output is not
literally zero-warning. They come from FastAPI deprecating `@app.on_event` in favour of a lifespan
handler, not from any defect in this code. Fixing means an `@asynccontextmanager async def lifespan`,
and while a lifespan handler is not a request endpoint (so it does not really violate the
sync-only rule), rewriting app startup on the last task of a build that has already been interrupted
three times by spend limits is a bad trade for two cosmetic lines. Ruling: leave them, document them.
Cost if wrong: a reviewer counts two deprecation warnings against "pristine test output". Accepted
knowingly rather than missed.

Task 12: implemented (cc4bd92, 19 tests, suite 182/182). Under review.
Task 12: **carry into Task 11** — `TRIBUTARY_AUTOSEED` startup wiring was correctly left out (no app
  module existed yet, and it was outside the seeder's file list). Task 11 owns it: the deployed URL
  must never open on an empty screen.
Task 9: complete (926c45d..a6243e0, 10 tests, review Approved, 0 Critical). Reviewer independently
  forced `_run_nontransactional` to fail on a non-lock error and confirmed `search_path` was still
  restored — verified the highest-stakes claim rather than trusting it. Decoy `public.events` test
  confirmed non-vacuous; kill/resume test asserts first post-resume `rows_done` exceeds the
  checkpoint, proving resumption rather than final state.
Task 9: minor (deferred): f-string SQL in the backfill cursor bootstrap (`_backfill_target` regex-
  parses planner text rather than taking a structural `pk_column` field on `Step`); retry/backoff
  logic duplicated 3× nearly verbatim; no regression test for a *failed* CIC restoring `search_path`
  (verified by probe only); `run()` can leak the first connection if the second `connect()` fails.

Task 10: complete (0dec859 lock tests + b1fbfcd bench + 4b900b6 methodology correction).
  **The 5GB constraint is now evidence, not a claim:** real 5.016 GiB table, 28,399,432 rows.
  `AlterColumnType(int4→int8)` — 214.173s wall, **2.4ms** longest ACCESS EXCLUSIVE.
  `CreateIndex` — 21.15s, zero exclusive locks across 17,672 samples. Metadata ops <20ms.
  **The Task 10 agent corrected an error I introduced into RESULTS.md**: I wrote that the watcher
  filters by the executing backend's PID, but `executor.run()` opens its connections internally and
  never exposes them — it actually reads the pid off each matching `pg_locks` row. My description was
  wrong; the correction is accurate. It also documented why 28.4M rows and not 50M (Docker's VM disk
  is capped independently of the host; only the reclaimable build cache was cleared) rather than
  quietly running a smaller size.
  RESULTS.md is explicit that `0.0 ms` means "below sampling resolution", not "provably zero".

**Second spend-limit interruption (reset 9:50pm).** Four agents killed. Recovery: Task 7's fix was
COMPLETE (4 tests, green) — committed as-is rather than re-run. Task 12's fix was HALF-DONE (helper
defined, 3 call sites unconverted, below-target test missing) — finished in-controller, since
re-dispatching an agent for three substitutions was not worth it. Task 9 had written nothing —
re-dispatched fresh. Both committed in 5ebf667. Suite 187/187.
  Lesson: check what each killed agent actually wrote. Re-running Task 7 would have duplicated
  finished work; assuming Task 12 was done would have shipped the rule violation review had flagged.

Task 8: complete (commits 9c2d88a..926c45d — 3030c25 impl, f6c7101 R19-R21, 926c45d fix round 1;
  42 tests, suite 195/195). Review found 1 Critical + 1 Important the 34 passing tests never touched;
  both fixed and re-reviewed with adversarial probes.
  **The Critical is the best catch of the build:** `classify()` returned `SAFE_METADATA` for
  `AddColumn` with a *volatile* default. PG11+'s fast ADD COLUMN skips the rewrite only for
  non-volatile defaults, so `gen_random_uuid()`/`nextval()`/`random()` force a full rewrite under
  ACCESS EXCLUSIVE. The code tested `default is not None`; the brief's own test name said "constant".
  Non-None ≠ constant. Since `Step.safety` is what the UI shows before a human approves a merge, this
  turned the safety report into false confidence — someone takes production down *because* the tool
  said it was safe. Fixed fail-closed (literal-only allowlist, explicitly no volatile-function
  denylist, since a denylist fails open on every function nobody thought of).
  Re-review probed with unanticipated inputs — `my_schema.my_func()`, `(SELECT 1)`, `CURRENT_USER`,
  `1 + 1`, `'a' || 'b'` — all correctly REWRITE; all literal forms still SAFE_METADATA; dedupe
  verified order-independent across 3 orderings and correctly scoped (not applied on small-table or
  no-PK paths, keyed on (table,column) so an unrelated column's SetNotNull survives).
Task 8: minor (deferred): `_is_constant_default` does not recognise scientific notation (`1e10`) as a
  literal, so such a default is over-flagged as REWRITE. Errs fail-closed; harmless false alarm.

**R18 — `db.py` was committed unimportable, and my own Task 1 ruling is why it survived.**
Task 6's implementer found `tributary/db.py` had a stray four-space indent before `import os` on
line 1 — an `IndentationError` making the module impossible to import. It sat green through Task 1's
review and every suite run since, because **nothing imported it yet**.
The Task 1 reviewer raised precisely this as a ⚠️: "no test exercises `tributary/db.py`'s
`connect()`/`dsn()`". **I ruled it was not a gap** — trivial wrappers, and I deferred an override
test to Task 11. That ruling was wrong. A single test that imported the module would have caught it
on the spot; instead it would have surfaced at Task 11 (web app) or Task 13 (Railway deploy), where
a deploy-time `IndentationError` reads as an infrastructure problem rather than a one-character typo.
Fixed directly in the controller session rather than via a subagent round-trip: a one-character
whitespace fix is unambiguous, and I am rationing tokens after the spend cap. Commit 9c2d88a adds
the fix plus three tests — the module imports, `DATABASE_URL` overrides the default (the behaviour
Railway depends on), and it falls back when unset. **Lesson recorded against my own judgment: "too
trivial to test" is exactly the code that fails silently, because nothing else exercises it.**

Task 6: implementer found and fixed a real gap during TDD — `serial`/`bigserial` defaults capture as
  unqualified `nextval(...)` and fail to materialise unless the backing sequence exists in the target
  schema first. Solved with 4-phase materialisation: sequences → tables → constraints (PK/UNIQUE/CHECK
  before FK) → indexes. Good catch; that is the kind of real-world detail that only shows up against
  a real database.
Task 6: flagged for review — unrequested guard making `delete_branch("main")` raise `ValueError`.

Task 5: complete (commits fd68ede..a7fe717, spec ✅, Approved, 0 Critical, 0 Important). Reviewer
  traced execution paths rather than reading assertions, and confirmed R9's test genuinely fails
  without the op-log path (different type buckets ⇒ heuristic cannot pair a renamed+retyped column),
  that the ambiguous two-dropped/two-added same-type case really refuses to guess, and that ordering
  is guaranteed by construction (list concatenation order) rather than by data-dependent luck.
Task 5: minor (deferred): R16's `op_renames.get((old_table, old_col))` fallback — fires only when a
  table rename and a column rename land in the same commit — has zero test coverage. Traced correct
  by the reviewer, but it is new logic with no test.
Task 5: minor (deferred): `DropNotNull`, `SetDefault`, `DropDefault` branches are implemented but
  never exercised by any test.
Task 5: minor (deferred): no RED capture for the R16 addendum (self-disclosed); the three new tests
  were manually traced to exercise real new paths, so rigor gap only.

**Spend limit hit at 4:50pm local; Tasks 6 and 8 were killed mid-flight.** Verified via `git log` and
`ls`: neither wrote anything (`tributary/store.py` and `tributary/planner.py` absent, `tributary/sql/`
empty, working tree clean apart from the pre-existing `.gitignore` edit). No partial work to
reconcile, no rollback needed. Re-dispatched both on sonnet rather than opus to stay clear of the cap.
Task 3: R14 fix committed (fd68ede). **Implementer corrected my ruling's mechanism:** `search_path`
  alone unqualifies `pg_get_constraintdef` but NOT `pg_get_indexdef`'s 1-arg form, which hardcodes
  schema-qualification in Postgres's ruleutils. My fix would have left every index definition still
  qualified — a half-fix that looks like a different bug. They combined `search_path` with
  `pg_get_indexdef`'s 3-arg `pretty=true` form. Scoped re-review dispatched with instructions to
  scrutinise that mechanism specifically, since it is a correction to my own ruling and has had less
  scrutiny than anything else: in particular whether `pretty=true` alters whitespace/parenthesisation
  in a way that is deterministic (needed for byte-equality) AND still executable (needed because
  materialisation replays these strings as DDL).

**R16 — diff never emits `RenameTable`, so renaming a table would drop it and every row in it.**
`RenameTable` exists in the model, `ddl.render` handles it, and the Task 11 editor will offer the
operation — but Task 5 treated table renames as out of scope, so a renamed table diffs as
`DropTable` + `CreateTable`. Against a populated table that is not a rename; it destroys the whole
table. Same class as the column-rename failure the task was built to prevent, one level up and worse.
Ruling: honour `{"op": "rename_table", ...}` from the op log, and diff the renamed table's contents
so rename+alter in one commit yields `RenameTable` + the column change rather than a spurious
recreate. **Explicitly no heuristic table-rename inference**, with the reasoning left in a comment:
for a column a wrong guess costs one column; for a table it destroys a table or renames the wrong
one, and two structurally identical tables are common enough in real schemas that structure alone
cannot justify the guess. Cost if wrong: table renames need the UI to record intent, which it does.

Task 5: noted, carried to Task 8: a modified index/constraint is correctly represented as drop+create
  (Postgres has no definition-altering `ALTER INDEX`), but on a large table the recreate must go
  through `CREATE INDEX CONCURRENTLY` or it blocks writes for the rebuild.

Task 4: complete (commits 0644f16..dd995ef, spec ✅, Approved, 0 Critical; 1 Important ruled below)

**R15 — `render(CreateIndex, schema)` ignores its `schema` argument, and R14 alone does not close it.**
Reviewer finding, labelled plan-mandated (the brief mandates literal insertion of `pg_get_indexdef`
output), so it is mine to rule on. Even once R14 makes stored definitions unqualified,
`render(CreateIndex(...), target_schema)` injects `target_schema` nowhere — where the index actually
lands depends entirely on the executing connection's `search_path`. That is an unwritten caller
contract, and if a caller gets it wrong the index is created on the production table in `main`.
Considered and rejected: reconstructing `CREATE INDEX` from the structured `Index` model (we hold
columns/unique/method/predicate) — it would make `schema` meaningful, but silently loses expression
indexes such as `ON t (lower(email))`, whose expressions `indkey` does not capture. Trading a
documented contract for silent data loss on a whole class of index is a bad trade.
Ruling: keep literal insertion; make the `search_path` obligation an explicit, documented caller
contract in `ddl.py`; and put the real guard in Task 6 as a mandatory test — materialise a branch
from a `main` that has an index and an FK, assert the index exists in the branch **and that main's
index set is unchanged**. The "main unchanged" half is the assertion that actually catches the
catastrophic case; the rest is bookkeeping. Cost if wrong: expression indexes keep working, and a
caller who ignores the contract is caught by Task 6's test rather than in production.

Task 4: minor (deferred): repeated `ALTER TABLE ... ALTER COLUMN` prefix across 5 render arms could
  share a helper. Reasonable as-is at 15 branches.
Task 4: minor (deferred): `ddl.py` docstring forward-references `store.py`, a module that does not
  exist yet.
Task 4: implemented (commit dd995ef, 41 tests in test_ddl.py, suite 81/81). BASE 0644f16.
  Implementer verified empirically that `pg_get_*def` embeds the source schema → R14 below.
Task 3: R13 scoped re-review — ADDRESSED, no new breakage.

**R14 — `pg_get_indexdef`/`pg_get_constraintdef` embed the source schema name. Two failures, one
of them the worst in the project so far.**
  (a) *Phantom diffs on every index and FK.* Those definition strings are stored in the snapshot and
      compared by diff. Carrying a schema name means `main`'s snapshot and `br_x`'s snapshot differ
      on every index and every foreign key purely because the schema names differ. Diff would report
      every index modified on every branch and merge would conflict on all of them — the tool fails
      at its only job.
  (b) *Branch materialisation writing into `main`.* Task 6 replays these definitions to rebuild a
      table in the branch schema. `CREATE INDEX ix_users_email ON main.users ...` replayed while
      materialising `br_x` does not index the branch — it indexes the production table in `main`.
      A branch operation silently mutating the source schema is fatal for a tool whose premise is
      safe isolated experimentation.
**This got past R11(2) because my own instruction told the test to normalise the schema name before
comparing — my escape hatch masked the defect.** Ruling: introspect with `search_path` set to the
target schema so Postgres itself emits unqualified names (its own machinery, not regex surgery on
the returned text); store definitions unqualified; delete the normalisation escape hatch and replace
it with the real property — two structurally identical schemas containing a table, a standalone
index, a unique constraint and an FK must produce byte-equal snapshots with no normalisation at all;
plus a test asserting no schema qualifier appears in any stored definition.
**Obligation carried to Tasks 6 and 9:** definitions are stored unqualified, so whoever executes them
must set `search_path` to the target schema or the unqualified references resolve to the wrong place.
Cost if wrong: if `search_path` interacts badly with the fixtures, the fallback is stripping the
qualifier in code — more fragile, but recoverable.

Task 3: implemented (a62fc54) + R12 fix (8621a13). Review: spec ✅, Approved, 0 Critical, 0 Important.
  BASE c674432. R13 fix dispatched before marking complete.

**R13 — zero-column tables vanish from the snapshot, and a read gap becomes a destructive write.**
`Table` entries are created only as a side effect of the column/constraint/index loops, so a table
with no columns (legal: `CREATE TABLE t()`, or dropping every column) never appears in the snapshot.
Diff compares snapshots, so a table *present in the database* but *absent from the snapshot* is
indistinguishable from one that was dropped — the diff emits `DropTable` and the executor drops a
real table nobody asked it to drop. Ruling: add a seed query enumerating tables independently of
their columns, plus two tests, one of which pins the actual consequence (drop the last column →
table still present, no spurious DropTable). Cost if wrong: one extra catalog query per snapshot.

**Correction to R11 (recorded for honesty).** The reviewer verified empirically that R11(2) does not
exercise `norm_type`'s alias table as I claimed. Postgres resolves type aliases to the same OID at
parse time, so `format_type` returns identical text whether a column was declared `int4` or
`integer` — the alias table never sees differing input from introspection. The genuinely differing
input in that test is the default expression (`CURRENT_TIMESTAMP` vs `now()`), normalised by
`norm_default`. The test remains a valid end-to-end regression test; my framing of *what* it proved
was wrong. `norm_type`'s alias table earns its keep on user-supplied type text from the schema
editor (Task 11), and is directly covered by the parametrised unit tests in test_canonical.py.
Task 3: minor (deferred): none outstanding beyond the above.

**R12 — never-analysed tables report `rows=0`, and that is the project's worst-case failure.**
`pg_class.reltuples` is `-1` until a table is analysed; the brief's `GREATEST(reltuples, 0)` clamp
turns that into `0`. Task 8's planner uses row count to decide whether an `ALTER COLUMN TYPE` needs
the shadow-column backfill, taking the plain `ALTER` below a 1M-row threshold. So a freshly restored,
never-analysed 5GB table would report 0 rows, read as "small", and receive a naive `ALTER TABLE` —
a full rewrite under `ACCESS EXCLUSIVE`. That is precisely the outage this entire project exists to
prevent, and it would fire on the most realistic setup of all: a table restored from a dump and not
yet analysed.
Ruling, three parts:
  1. `TableStats.rows` becomes `int | None`; `None` means never analysed. Nothing may silently read
     an unknown row count as zero.
  2. The planner's size decision is driven primarily by `bytes` (`pg_total_relation_size` is real
     disk usage, always accurate, never an estimate), with rows as a secondary signal.
  3. Fail safe on uncertainty: `rows is None` must never classify as small. The asymmetry is the
     whole argument — needlessly shadow-backfilling a small table costs a slower migration; skipping
     it on a large one costs an outage.
Task 8 gets an explicit test: a never-analysed table above the byte threshold still takes the safe path.
Cost if wrong: migrations on small unanalysed tables take the slower path unnecessarily. Cheap, and
visible in the plan the UI shows before anything runs.

Task 2: complete (commits ea33d98..c674432, review clean — spec ✅, quality Approved, 0 Critical, 0 Important)
Task 2: minor (deferred): `_column_to_json` etc. emit fixed-schema keys in declaration order rather
  than alphabetically. Deterministic across runs, so byte-stability holds; wording-only gap.
Task 2: minor (deferred): frozen dataclasses carrying `dict` fields auto-generate `__hash__` but
  raise `TypeError: unhashable type: 'dict'` if hashed. Inherited from the plan's own code. **Carry
  into later dispatches: never put `Change`/`Table`/`Snapshot` instances in a set or use them as
  dict keys — use `ObjectPath` tuples, which are hashable.**

**R11 — nested key-sort is untested, and it guards an invariant I declared load-bearing.** The
reviewer confirmed `to_json` sorts at every level, but only the top level is covered by a committed
test; the nested behaviour was verified by an uncommitted ad-hoc script. Byte-stable snapshots are
what make the commit DAG trustworthy, so an untested invariant here is the wrong one to leave bare.
Ruling: rather than a unit test on dict ordering, Task 3 gets the *stronger* property test — introspect
the same live schema twice and assert the serialised JSON is byte-identical, and introspect two
separately-created but identically-defined schemas and assert their snapshots compare equal. That
tests the real invariant (same schema ⇒ same bytes) instead of an implementation detail of sorting.
Cost if wrong: none; it is strictly more coverage than the Minor asked for.

Task 2: implementer again reported needing the Ryuk workaround — R10 confirmed as a real, repeatable
  environment requirement, not a one-off. Fold into Task 14 (README) at the latest.

Task 1: ⚠️ resolved — `db.connect()`/`db.dsn()` are untested by this task's diff. **Ruling: not a gap
  for Task 1.** Both are trivial wrappers, and the behaviour that actually matters — `DATABASE_URL`
  overriding the default DSN — is what Railway depends on at deploy time. Folding an explicit
  `DATABASE_URL`-override test into Task 11, which has to wire `DATABASE_URL` for its `client`
  fixture anyway (see R7). Cost if wrong: an untested env-var override reaches Task 13, where a
  deploy failure would surface it immediately and loudly.
Task 1: implementer reported 3 deviations — `filterwarnings` added to pyproject to silence a
testcontainers deprecation; version-floor assertion changed from string compare to
`int(...) >= 120000`; `TESTCONTAINERS_RYUK_DISABLED=true` needed on this Docker Desktop for Mac.

**R10 (pending confirmation of Task 1 review) — testcontainers Ryuk on macOS.** The suite currently
passes on this machine only if `TESTCONTAINERS_RYUK_DISABLED=true` is exported by hand. A test suite
that needs undocumented per-machine env vars is a setup-experience failure, and "Setup experience"
is an explicit evaluation criterion. Ruling: `tests/conftest.py` sets the variable itself when it is
unset, and the README documents both fixture paths (`docker compose up -d db` +
`TRIBUTARY_TEST_DSN` for speed; bare `pytest` for isolation). Fold into the Task 1 fix loop if the
reviewer raises it, otherwise into Task 14. Cost if wrong: Ryuk's orphan-container reaper stays off,
so a hard-killed run can leave a stopped container behind — cheap and visible, versus a suite that
does not run for a stranger.

