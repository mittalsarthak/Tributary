# Tributary

Tributary is branch/diff/merge version control for Postgres schemas, applied to a real database.
You branch a schema, edit it independently (add/drop/rename/retype columns, constraints, indexes),
diff it against `main`, and merge it back — and the merge actually runs as DDL against a real,
populated Postgres table, with a migration planner that refuses to take the database down doing it.

## Live demo

**Deployed URL: <https://tributary-6j5y.onrender.com/>**

> ### ⏳ Please give it 30-60 seconds on the first load
>
> This runs on Render's **free** tier, which puts the service to sleep after about 15 minutes of
> inactivity. The first request after that has to wake the container back up.
>
> **A blank page or a slow spinner on your first visit is the service starting, not a broken
> deployment.** Leave the tab for up to a minute and it will load. Every request after that is
> fast until it goes idle again.
>
> Two other free-tier notes, so nothing reads as a defect:
>
> - **The demo caps table growth at 1M rows.** Render's free Postgres allows 1GB; 1M rows measures
>   ~125MB, and the 10M/50M options would exceed it, so they are hidden and refused there. That is a
>   limit of the free database, not of the tool — the 5GB evidence is in
>   [`bench/RESULTS.md`](bench/RESULTS.md), measured on a real 5.016 GiB / 28.4M-row table.
> - **The free database expires 90 days after creation.** This deployment exists for a review
>   window, not permanently.
>
> Prefer not to wait? `docker compose up` runs the whole thing locally in one command, with no cold
> start and no row cap — see [Setup](#setup) below.

---

## Setup

```
docker compose up
```

That's it. It brings up Postgres, builds and starts the app, and seeds a demo workspace (`main`
branch with `users`, `orders`, and a growable `events` table) automatically
(`TRIBUTARY_AUTOSEED=1` is set for the `app` service in `docker-compose.yml`). Visit
`http://localhost:8000`.

### Running the tests

```
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Tests run against a **real** Postgres — nothing about lock behaviour, DDL execution, or concurrency
is mocked, because none of that can be meaningfully mocked. Two ways to supply that Postgres:

- **Nothing set:** `tests/conftest.py`'s `pg_dsn` fixture spins up a real Postgres 16 container via
  `testcontainers`, once per session.
- **`TRIBUTARY_TEST_DSN` set:** the fixture skips the container and points straight at whatever DSN
  you give it (e.g. the `db` service from `docker compose up -d db`) — container startup is the
  slowest part of the loop, so this is the fast path for repeated local runs:
  ```
  docker compose up -d db
  TRIBUTARY_TEST_DSN=postgresql://tributary:tributary@localhost:5433/tributary .venv/bin/pytest
  ```

**On Docker Desktop for Mac, also set `TESTCONTAINERS_RYUK_DISABLED=true`.** Testcontainers' Ryuk
orphan-container reaper doesn't work reliably against Docker Desktop's Mac VM, and without this the
suite hangs waiting on it:
```
TESTCONTAINERS_RYUK_DISABLED=true .venv/bin/pytest
```
(Not needed at all when `TRIBUTARY_TEST_DSN` is set, since no container gets started in the first
place.) The cost of disabling Ryuk is a stopped container occasionally left behind after a hard-killed
run — cheap, and visible in `docker ps -a`, versus a test suite that doesn't run for a stranger.

Current state: **282 passed**, 0 failed.

**What `tests/test_locks.py` proves.** This is the file that matters most — the mechanised version of
the project's central claim. Three tests, all against a real live table, none of them mocked:

- `test_migration_does_not_starve_short_lived_readers` — a background thread hammers the table with
  back-to-back short transactions (no sleep) while a migration runs concurrently. Asserts *both*
  halves of the property: the migration finishes promptly, and the worst single read latency observed
  anywhere during the run stays low — a migration that quietly queued behind traffic and then blocked
  every read behind *it* would fail this.
- `test_migration_gives_up_rather_than_holding_the_lock_queue` — a second connection holds an
  `ACCESS EXCLUSIVE` lock hostage. Asserts the migration fails within bounded time (`lock_timeout` +
  retries with backoff), rather than hanging indefinitely.
- `test_concurrent_index_build_leaves_writes_available` — inserts continue from a background thread
  while `CREATE INDEX CONCURRENTLY` builds; asserts the write count genuinely increased during the
  build, not just that the writer thread survived.

Together: a migration either gets its lock quickly or gives up quickly; it never stalls the queue of
work behind it.

---

## Guided tour

1. **Create a branch.** From the home page, name a branch (e.g. `retype-demo`). It materialises
   instantly as a real Postgres schema (`br_retype_demo`) with `main`'s structure — no rows copied,
   regardless of how large `main.events` is.
2. **Add a column.** Open the branch, use "Add a column" in the editor — it applies immediately to
   the branch's live schema and records the edit in an uncommitted-changes list.
3. **Retype `events.event_type`.** Use **"Retype a column"** in the editor: table `events`, column
   `event_type`, new type `varchar(20)`. (`event_type` seeds as plain `text`; narrowing it to a
   bounded `varchar` is *not* binary-coercible — unlike widening `varchar`→`text` the other way,
   which the planner treats as metadata-only — so it is exactly the kind of retype the safety report
   exists to catch.) Then click **Commit**.

   A branch is an ordinary Postgres schema, so you can equally connect and run the DDL by hand
   (`docker compose exec db psql -U tributary -d tributary -c 'ALTER TABLE br_retype_demo.events
   ALTER COLUMN event_type TYPE varchar(20)'`) — committing snapshots whatever the branch's live
   schema actually looks like, whether or not the change went through the editor's forms.
4. **See the safety warning.** Open **diff vs main** on the branch. The diff lists the retype and
   shows the generated plan with its safety badge. The plan is classified against **`main`'s actual
   measured size**, not the branch's (branches are structure-only, so the branch's own `events` table
   is always empty) — which is why the home page's **grow `events`** control (buttons for 1M / 10M /
   50M rows) matters: on the freshly-seeded, ~7,500-row table the same retype shows a brief-lock
   warning ("a brief `ACCESS EXCLUSIVE` lock costs less than the shadow-column dance would"); grow
   `main.events` past 1M rows first, and the identical retype's badge switches to a rewrite warning
   naming the shadow-column backfill instead. The 5GB claim becomes something you can watch happen,
   not just read about.
5. **Merge.** Start a merge from the branch into `main`. Any conflicts must be resolved (take
   ours/take theirs) before the run button exists at all — it's structurally absent from the page
   while conflicts remain, not just disabled. Once clear, the merge screen leads with the same safety
   report from step 4.
6. **Watch progress.** Run the merge. It executes in a background thread and streams per-step
   progress over SSE — step number, status, table, and a plain-sentence note — into a live log,
   ending in "Merge complete" or a named failure. A large retype's shadow-column backfill shows up
   here as a sequence of steps, not one long silent wait.

---

## The benchmark

The 5GB constraint from the brief is proven once, locally, against a real table — see
[`bench/RESULTS.md`](bench/RESULTS.md) for the full methodology, numbers, and what they do and don't
claim. The headline:

> Retyping a column (`int4` → `int8`) on a real **5.016 GiB**, **28,399,432-row** Postgres table took
> **214.173s of total wall time**, while the longest `ACCESS EXCLUSIVE` lock held on the table during
> the whole operation was **2.4 milliseconds**.

The naive `ALTER TABLE events ALTER COLUMN id TYPE bigint` on that same table would hold
`ACCESS EXCLUSIVE` for the entire rewrite — minutes of the table being completely unavailable, and
every query that arrives during that window queuing up behind it. Tributary's planner rewrites that
into a shadow column, a sync trigger, a batched backfill, and a short swap instead: slower overall,
and the database stays up throughout. Reproduce with:
```
python bench/benchmark_5gb.py --rows 28400000 --schema bench5gb
```

The deployed instance itself runs a much smaller table — the host's disk is smaller than what this
benchmark needed — so the 5GB claim is evidenced here, in a committed local run, rather than asserted
on the deployed demo. The "grow events" control (step 4 above) is how the same property is made
reproducible at whatever size the deployed host's disk actually allows.

---

## Architecture

One Postgres instance is the entire workspace: `_tributary` (metadata — the commit DAG, branches,
merges, migration steps), `main` (the populated "production" schema), and `br_<name>` (structure-only
materialised branches). A single FastAPI + htmx application serves the whole thing from one
container.

| Module | Responsibility |
|---|---|
| `model` | The shared types every other module speaks: `Snapshot`/`Table`/`Column`/`Constraint`/`Index`, the `Change` union (`AddColumn`, `RenameColumn`, `AlterColumnType`, ...), `Safety`, `Step`/`Plan`, `Conflict`, `TableStats`. |
| `canonical` | Normalises Postgres's many spellings of the same type/default (`character varying(50)` ≡ `varchar(50)`, `CURRENT_TIMESTAMP` ≡ `now()`) onto one canonical form, once, so nothing downstream has to know about aliasing. |
| `introspect` | Reads a live schema out of `pg_catalog` (not `information_schema` alone, so `pg_get_constraintdef`/`pg_get_indexdef`/`format_type` produce definitions Postgres itself agrees with) into a canonical `Snapshot`, plus real per-table size stats (`table_stats`). |
| `ddl` | Renders a single `Change` into executable, correctly quoted SQL (`psycopg.sql.Identifier` for every identifier — never an f-string). |
| `diff` | Structural, rename-aware diff between two snapshots. Prefers an operation log's recorded intent when present; falls back to a conservative heuristic (exact type match, unambiguous pairing) otherwise — a wrong guess destroys data, so ambiguity means no guess. |
| `merge` | Three-way merge over object maps keyed by path (table/column/constraint/index), classified against the lowest common ancestor; typed conflicts (modify/modify, drop/modify, add/add, rename/modify) with table-level conflicts suppressing the noise beneath them. |
| `planner` | Turns a list of changes into an ordered, safety-classified `Plan` against the *actual measured* table size — the module the whole project exists for. Rewrites unsafe naive DDL (retypes, `SET NOT NULL`, index/constraint creation) into safe multi-step forms. |
| `executor` | Runs a `Plan` against real Postgres: `lock_timeout` + bounded retry on every DDL step, `CREATE INDEX CONCURRENTLY` and batched backfills outside any transaction, checkpointed and resumable, observable step-by-step. |
| `store` | The commit DAG: branches, commits, merges, and structure-only branch materialisation (never copies a row). |
| `seed` | The demo workspace (`users`/`orders`/`events` with realistic constraints and an FK) and the growable `events` table that makes the safety-classification story demonstrable in a browser. |
| `web` | The FastAPI + htmx application tying all of the above into branch/diff/merge/progress screens, never letting a domain error reach a visitor as a stack trace. |

The rule the design holds to: `planner` never touches the network, and `diff`/`merge` are pure
functions over snapshots — that's what makes the conflict and safety logic cheap to test
exhaustively against every case by hand, without a database in the loop for most of them.

`tributary/db.py` is the one-line connection factory (`DATABASE_URL` env var, falling back to a
local default) everything else depends on. `tributary/sql/schema.sql` defines `_tributary`'s tables.

---

## Deployment

Deployed on [Render](https://render.com) via the `render.yaml` blueprint in this repo: one web
service plus one Postgres, both on the free plan.

**Dashboard → New → Blueprint → select this repository.** Render reads `render.yaml`, provisions
both services, and wires `DATABASE_URL` between them. No other configuration is needed.

Two things a visitor should know, because neither is a bug:

- **The first visit after a quiet period takes ~30-60 seconds.** Render's free web services sleep
  after about 15 minutes idle. It is waking up, not broken.
- **The free Postgres expires 90 days after creation.** This deployment exists for a review window,
  not permanently.

The blueprint sets `TRIBUTARY_MAX_GROW_ROWS=1000000`, so the deployed demo offers only the 1M-row
growth target — that lands the `events` table at ~125MB, inside the free plan's 1GB. The 10M and
50M buttons are hidden *and* refused server-side there. That is a limit of the free database, not
of the tool: the 5GB evidence is in [`bench/RESULTS.md`](bench/RESULTS.md), measured on a real
5.016 GiB table.

Earlier in the build nothing was pushed to GitHub (a binding instruction — see `decisions.md`), which
ruled out repo-based deploys and pointed at Railway's local-directory flow. The repository has since
been published, and Railway has no free tier, so Render is the host that actually fits the
constraints.

Running elsewhere: the app needs only a `DATABASE_URL` and a Postgres it can reach. Set
`TRIBUTARY_AUTOSEED=1` so the URL is never an empty screen, and `PORT` is honoured automatically.
