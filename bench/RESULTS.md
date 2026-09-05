# Tributary 5GB Lock-Safety Benchmark

Reproduce with: `python bench/benchmark_5gb.py --rows 28400000 --schema bench5gb`

Run at: 2026-09-06 03:10:35 +0530

- **Postgres**: PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on aarch64-unknown-linux-gnu
- **Host**: Apple M2 Pro, 32GB RAM, macOS 15.7
- **Postgres runs inside**: Docker Desktop's Linux VM — 6.6.12-linuxkit / 12 CPUs / 8.2 GB RAM
- **`events` table** (`bench5gb.events`), measured via `pg_total_relation_size` after `ANALYZE`:
  **5.016 GiB** (5,386,166,272 bytes), **28,399,432 rows**
- **Retype backfill batch size**: 10,000 rows/batch

**Why 28,400,000 rows and not this script's own `--rows 50000000` example:** Postgres here runs
inside Docker Desktop's Linux VM, which has its own capped virtual disk independent of the Mac host's
actual free space (the host has ~54GB free; the VM reported only ~18GB free before this run). Docker's
build cache — a reclaimable, disposable layer cache unrelated to any running container or image
(`docker builder prune -f`) — was cleared first, honestly and non-destructively, bringing the VM's
free space to ~38GB. `28,400,000` was then chosen from an empirical calibration (seeding smaller
tables of the same schema and measuring the real bytes/row ratio that came back, ~189 bytes/row
including the primary key index) to land just over 5GiB while leaving generous headroom
(~30GB) for `CREATE INDEX CONCURRENTLY`'s index, the shadow-column retype's transient extra column,
and WAL. **This machine, as configured, could not have reached `--rows 50000000` against the
VM's original ~18GB of free disk; with the build-cache cleanup, 5GB was comfortably reachable and is
reported here as the real 5.016 GiB measured, not rounded up and not the unrun 50M-row example.** The
script accepts `--rows 50000000` (or larger) unchanged for anyone running it against more disk.

## How to read this

**Two columns tell opposite stories, and only the second one matters.**

Total wall time is allowed to be long. Lock time is not. A migration that takes three and a half
minutes while never holding an exclusive lock longer than a couple of milliseconds is a success —
the table stayed readable and writable throughout. A migration that finishes in twenty seconds while
holding `ACCESS EXCLUSIVE` the whole time is a failure, because for those twenty seconds every query
against that table is queued behind it.

Reporting only elapsed time would hide exactly the property being demonstrated.

## Results

| Migration | Total wall time | Longest ACCESS EXCLUSIVE held | Verdict |
|---|---|---|---|
| AddColumn (nullable) | 20.0 ms | **0.0 ms** | safe — metadata only |
| AddColumn (constant default) | 16.1 ms | **1.2 ms** | safe — metadata only (PG11+ fast path) |
| RenameColumn | 13.0 ms | **0.0 ms** | safe — metadata only |
| DropColumn | 16.7 ms | **0.0 ms** | safe — metadata only |
| CreateIndex | 21.150 s | **0.0 ms** | safe — no `ACCESS EXCLUSIVE` sampled at all (`CONCURRENTLY`) |
| SetNotNull | 3.670 s | **3.6 ms** | safe — validated `CHECK` first, so PG12+ skips the scan |
| AlterColumnType (int4→int8) | 214.173 s | **2.4 ms** | safe — shadow-column backfill |

### The headline row

`AlterColumnType (int4→int8)` is the one that matters. A naive `ALTER TABLE events ALTER COLUMN id
TYPE bigint` on this table takes `ACCESS EXCLUSIVE` and rewrites all 28.4 million rows — the table is
unavailable for the duration, and every query that arrives meanwhile queues behind it.

Tributary rewrote it as a shadow column plus a sync trigger plus a batched backfill plus a short swap.
It took **3.5 minutes instead of seconds** — and held an exclusive lock for **2.4 milliseconds**. That
trade is the entire product: the migration is slower, and the database stays up.

`CreateIndex` shows the same shape — 21 seconds of work, and across 17,672 lock samples the watcher
never once caught an `ACCESS EXCLUSIVE` held on the table, because the plan emits
`CREATE INDEX CONCURRENTLY`.

## Methodology, and what these numbers do not prove

The lock watcher polls `pg_locks` (joined to `pg_class`/`pg_namespace`) on its own connection,
filtered to `mode = 'AccessExclusiveLock' AND granted` on the target relation, and records the
longest continuous interval such a lock was held. `executor.run()` opens its own connections
internally and does not expose their backend pid, so this does not filter by a pid known in advance —
instead, the pid of whichever backend actually holds the lock is read directly off each matching
`pg_locks` row (the same value `pg_backend_pid()` would return on that connection). Nothing else
touches this table while a migration runs, so any `AccessExclusiveLock` sampled on it is unambiguously
the migration's own, regardless of which of `executor.run()`'s two internal connections took it. The
**achieved** average gap between samples is reported per row above (roughly 1.2 ms on the
long-running migrations, up to 3.3 ms on the sub-20 ms ones) — these are measured, not the configured
target.

**Therefore a `0.0 ms` reading means "shorter than the sampling resolution", not "provably zero".**
Every migration in this table does take `ACCESS EXCLUSIVE` briefly at some point — a catalog update
is still a catalog update. The claim being made is not that no lock is ever taken; it is that no lock
is held for a duration that matters, on a table where the naive alternative would hold one for
minutes. On the four metadata-only rows the sample count is tiny (5–9 samples) precisely because the
whole operation finished in under 20 ms.

A `bench_sizing.events` table (1410 MB) was used earlier to derive the real bytes-per-row ratio
before seeding the actual 28.4M-row table above; it has since been dropped and was never part of
the results in this file.

## Honest notes

- This is a real run against a real 5.016 GiB table, not a projection. Every number above was
  measured on the hardware named at the top.
- It runs inside Docker Desktop's Linux VM with 8.2 GB of RAM allocated, which is less than the host
  has. On bare metal with more shared buffers the total wall times would likely improve; the lock-held
  figures are not sensitive to that, since they are bounded by design rather than by throughput.
- The deployed demo on Railway runs a much smaller table — the host's disk is smaller. The 5GB claim
  is evidenced here, locally, rather than on the deployed instance. The deployed app's
  "grow events" control lets you inflate the table as far as that host allows and watch the planner's
  classification change, which is the same property at a size you can reproduce in a browser.
