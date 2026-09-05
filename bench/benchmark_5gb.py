#!/usr/bin/env python
"""The 5GB benchmark: the numbers behind the lock-safety claim `tests/test_locks.py`
proves qualitatively.

Seeds a real `events` table to a target size against a real, live Postgres,
then runs each of seven migration classes against it through the project's
own `planner.plan()` / `executor.run()` -- the exact code path production
traffic would go through, never a reimplementation or a mock -- timing two
things per migration:

  * total wall time (allowed to be long)
  * the longest continuous interval `events` spent under an
    `AccessExclusiveLock` (not allowed to be long)

Those two numbers tell opposite stories on purpose. A migration that takes
nine minutes but never holds `ACCESS EXCLUSIVE` for more than a couple of
seconds is exactly what this project exists to produce; a migration that
finishes in twenty seconds by holding `ACCESS EXCLUSIVE` throughout is
exactly what it exists to avoid. Reporting only elapsed time would erase
that distinction, so the report below puts both columns side by side and
calls out the ones that would have hurt on a naive planner.

Lock time is measured, not inferred: a background thread polls `pg_locks`
(joined to `pg_class`/`pg_namespace`) for a granted `AccessExclusiveLock` on
the target relation while `executor.run()` is in flight on its own
connections, at a fixed interval (`--lock-poll-interval`, default 2ms). A
lock held for less time than that interval can sample as zero -- the report
says so explicitly rather than pretending sub-interval precision it does
not have.

`executor.run()` opens its own connections internally and does not expose
their backend pid, so instead of calling `pg_backend_pid()` ourselves this
script reads the pid directly off each `pg_locks` row that names the target
table -- the same value `pg_backend_pid()` would have returned had it been
called on that connection, obtained the only way available without
modifying `executor.py` (out of scope for this task). Nothing else touches
this table while a migration runs, so any `AccessExclusiveLock` sampled on
it is unambiguously the migration's own.

Usage:
    .venv/bin/python bench/benchmark_5gb.py --rows 50000000
    .venv/bin/python bench/benchmark_5gb.py --rows 5000000 | tee bench/RESULTS.md

Standalone CLI -- not collected by pytest, not imported by anything else in
the project, and not on the pytest slow-test path.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from tributary import db
from tributary.executor import StepFailed
from tributary.executor import run as execute_plan
from tributary.introspect import table_stats
from tributary.model import (
    AddColumn,
    AlterColumnType,
    Column,
    CreateIndex,
    DropColumn,
    Index,
    RenameColumn,
    SetNotNull,
)
from tributary.planner import plan as build_plan

TABLE = "events"
_LOCK_MODE = "AccessExclusiveLock"


def _log(*args) -> None:
    """Progress/diagnostic output -- always stderr, never stdout. stdout is
    reserved for the markdown report itself, so `... | tee bench/RESULTS.md`
    captures a clean document with no interleaved progress noise."""
    print(*args, file=sys.stderr, flush=True)


# --- the lock watcher: pg_locks sampled from a background thread -----------


@dataclass
class LockWatcher:
    """Polls `pg_locks` for a granted `AccessExclusiveLock` on
    `schema.table`, at `poll_interval` seconds, until `stop()`. Tracks the
    longest continuous run of "yes, held" samples -- that is the number this
    whole benchmark exists to produce.
    """

    dsn: str
    schema: str
    table: str
    poll_interval: float
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = field(default=None, repr=False)
    max_held_s: float = 0.0
    samples: int = 0
    held_samples: int = 0
    pids_seen: set = field(default_factory=set)
    error: str | None = None

    _QUERY = """
        SELECT l.pid
        FROM pg_locks l
        JOIN pg_class c ON c.oid = l.relation
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
          AND l.mode = %s AND l.granted
    """

    def _run(self) -> None:
        try:
            conn = psycopg.connect(self.dsn, autocommit=True)
        except Exception as exc:  # noqa: BLE001
            self.error = f"watcher could not connect: {exc}"
            return
        interval_start: float | None = None
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                rows = conn.execute(
                    self._QUERY, (self.schema, self.table, _LOCK_MODE)
                ).fetchall()
                self.samples += 1
                if rows:
                    self.held_samples += 1
                    for (pid,) in rows:
                        self.pids_seen.add(pid)
                    if interval_start is None:
                        interval_start = now
                    self.max_held_s = max(self.max_held_s, now - interval_start)
                else:
                    interval_start = None
                time.sleep(self.poll_interval)
        except Exception as exc:  # noqa: BLE001
            self.error = f"watcher failed mid-run: {exc}"
        finally:
            conn.close()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)


# --- schema/table setup -----------------------------------------------------


def _qualified(schema: str, table: str) -> str:
    return sql.Identifier(schema, table).as_string(None)


def _reset_schema(conn, schema: str) -> None:
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))


def _create_table(conn, schema: str, table: str) -> None:
    """`events`, wide enough to reach real GB-scale size in a bounded number
    of rows, carrying one dedicated column per migration class below so
    every migration acts on its own column and none interferes with
    another's setup:

      * `rename_target` / `to_drop` -- untouched until their own step
      * `maybe_null` -- nullable in the schema, populated in every row (no
        actual NULLs), so `SetNotNull` succeeds
      * `seq4` -- `integer`, the `AlterColumnType(int4->int8)` target
      * `bench_note_nullable` / `bench_note_default` -- do not exist yet;
        added by their own `AddColumn` migration
    """
    qualified = _qualified(schema, table)
    conn.execute(
        f"""
        CREATE TABLE {qualified} (
            id bigint PRIMARY KEY,
            ts timestamptz NOT NULL,
            payload text NOT NULL,
            seq4 integer NOT NULL,
            maybe_null integer,
            rename_target text NOT NULL,
            to_drop text NOT NULL
        )
        """
    )


def _seed(conn, schema: str, table: str, target_rows: int, batch_size: int) -> None:
    qualified = _qualified(schema, table)
    done = 0
    started = time.monotonic()
    while done < target_rows:
        batch = min(batch_size, target_rows - done)
        conn.execute(
            f"""
            INSERT INTO {qualified}
                (id, ts, payload, seq4, maybe_null, rename_target, to_drop)
            SELECT
                %(offset)s + s,
                now() - ((%(offset)s + s) || ' seconds')::interval,
                repeat('x', 80),
                (%(offset)s + s) %% 2000000000,
                %(offset)s + s,
                'rn_' || (%(offset)s + s),
                'drop_' || (%(offset)s + s)
            FROM generate_series(1, %(batch)s) AS s
            """,
            {"offset": done, "batch": batch},
        )
        done += batch
        _log(f"  seeded {done:,}/{target_rows:,} rows "
             f"({time.monotonic() - started:.1f}s elapsed)")
    conn.execute(f"ANALYZE {qualified}")


# --- hardware / version header ----------------------------------------------


def _sysctl(name: str) -> str | None:
    try:
        out = subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _host_description() -> str:
    if platform.system() == "Darwin":
        chip = _sysctl("machdep.cpu.brand_string") or platform.processor() or "unknown CPU"
        mem_bytes = _sysctl("hw.memsize")
        mem_gb = f"{int(mem_bytes) / (1024**3):.0f}GB" if mem_bytes else "unknown RAM"
        return f"{chip}, {mem_gb} RAM, macOS {platform.mac_ver()[0]}"
    return platform.platform()


def _docker_engine_description() -> str:
    """Best-effort description of the Docker Desktop VM Postgres actually
    runs inside, when it looks like that's where `dsn` points (localhost).
    Purely informational -- absence of `docker` on PATH, or of a reachable
    daemon, degrades to a short note rather than failing the benchmark.
    """
    try:
        out = subprocess.run(
            ["docker", "info", "--format",
             "{{.OperatingSystem}} / {{.KernelVersion}} / {{.NCPU}} CPUs / "
             "{{.MemTotal}} bytes RAM"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return "not detected (docker CLI unavailable or daemon unreachable)"


# --- the seven migrations ----------------------------------------------------


def _migrations(schema: str) -> list[tuple[str, list]]:
    """`Index.definition` (real `introspect.snapshot`-produced ones are
    unqualified -- see `planner.py`'s R14 discussion) is built here already
    qualified with the real target schema, via `sql.Identifier` like every
    other identifier in this file -- not an f-string of a raw name, and not
    a placeholder substituted in after the fact.
    """
    index_def = (
        f"CREATE INDEX bench_ix_ts ON {_qualified(schema, TABLE)} USING btree (ts)"
    )
    return [
        ("AddColumn (nullable)",
         [AddColumn(TABLE, Column("bench_note_nullable", "text", True, None, 90))]),
        ("AddColumn (constant default)",
         [AddColumn(TABLE, Column("bench_note_default", "integer", True, "0", 91))]),
        ("RenameColumn",
         [RenameColumn(TABLE, "rename_target", "renamed_col")]),
        ("DropColumn",
         [DropColumn(TABLE, "to_drop")]),
        ("CreateIndex",
         [CreateIndex(TABLE, Index("bench_ix_ts", index_def, ("ts",)))]),
        ("SetNotNull",
         [SetNotNull(TABLE, "maybe_null")]),
        ("AlterColumnType (int4->int8)",
         [AlterColumnType(TABLE, "seq4", "integer", "bigint")]),
    ]


# --- run one migration, measuring both numbers ------------------------------


def _run_one(dsn: str, schema: str, table: str, changes: list, stats: dict,
             pk_columns: dict, poll_interval: float, retype_batch_size: int) -> dict:
    migration_plan = build_plan(changes, stats, schema, batch_size=retype_batch_size,
                                 pk_columns=pk_columns)
    watcher = LockWatcher(dsn=dsn, schema=schema, table=table, poll_interval=poll_interval)
    watcher.start()
    started = time.monotonic()
    error = None
    try:
        execute_plan(dsn, schema, migration_plan)
    except StepFailed as exc:  # noqa: BLE001 -- reported, not hidden
        error = str(exc)
    elapsed = time.monotonic() - started
    watcher.stop()
    return {
        "elapsed_s": elapsed,
        "max_lock_held_s": watcher.max_held_s,
        "lock_samples": watcher.samples,
        "held_samples": watcher.held_samples,
        "pids": sorted(watcher.pids_seen),
        "watcher_error": watcher.error,
        "step_count": len(migration_plan.steps),
        "warnings": migration_plan.warnings,
        "error": error,
    }


# --- report formatting -------------------------------------------------------


def _fmt_s(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds:.3f}s"


def _verdict(max_lock_held_s: float, elapsed_s: float, held_samples: int) -> str:
    if held_samples == 0:
        return "no ACCESS EXCLUSIVE sampled at all"
    if max_lock_held_s < 1.0:
        return "safe -- brief"
    if max_lock_held_s < elapsed_s * 0.1:
        return "safe -- short relative to total time"
    return "REVIEW -- exclusive lock is a large fraction of total time"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5_000_000,
                         help="target row count for the seeded events table "
                              "(default: 5,000,000; pass 50000000 for a real "
                              "5GB+ run on a machine with the disk for it)")
    parser.add_argument("--schema", default="bench5gb",
                         help="schema the benchmark seeds and migrates (default: bench5gb)")
    parser.add_argument("--seed-batch", type=int, default=2_000_000,
                         help="rows per seeding INSERT batch (default: 2,000,000)")
    parser.add_argument("--retype-batch-size", type=int, default=10_000,
                         help="rows per backfill batch for the shadow-column retype "
                              "(planner.plan's batch_size; default: 10,000, same as "
                              "the planner's own default)")
    parser.add_argument("--lock-poll-interval", type=float, default=0.0005,
                         help="seconds to sleep between pg_locks samples, on top of "
                              "the round trip itself (default: 0.0005 -- the achieved "
                              "average is reported per migration and is usually a "
                              "little higher than this once query/network latency is "
                              "added in; a lock held for less time than that achieved "
                              "average can sample as zero)")
    parser.add_argument("--dsn", default=None,
                         help="override the target Postgres DSN (default: tributary.db.dsn())")
    parser.add_argument("--cleanup", action="store_true",
                         help="drop the benchmark schema when done (default: leave it "
                              "for inspection)")
    args = parser.parse_args()

    dsn = args.dsn or db.dsn()
    schema = args.schema

    _log(f"connecting to {dsn}")
    setup_conn = psycopg.connect(dsn, autocommit=True)
    try:
        version_row = setup_conn.execute("SELECT version()").fetchone()
        pg_version = version_row[0] if version_row else "unknown"

        _log(f"resetting schema {schema!r}")
        _reset_schema(setup_conn, schema)
        _create_table(setup_conn, schema, TABLE)

        _log(f"seeding {TABLE!r} to {args.rows:,} rows "
             f"(batches of {args.seed_batch:,}) -- this is real data, real disk")
        _seed(setup_conn, schema, TABLE, args.rows, args.seed_batch)

        stats = table_stats(setup_conn, schema)
        events_stats = stats.get(TABLE)
        if events_stats is None:
            _log("FATAL: table_stats reports nothing for the seeded table")
            return 1
        actual_bytes = events_stats.bytes
        actual_rows = events_stats.rows
        _log(f"seeded and analysed: {actual_rows:,} rows, "
             f"{actual_bytes / (1024**3):.3f}GiB (pg_total_relation_size, real measurement)")
    finally:
        setup_conn.close()

    migrations = _migrations(schema)
    pk_columns = {TABLE: "id"}

    results = []
    for label, changes in migrations:
        _log(f"running: {label}")
        result = _run_one(
            dsn, schema, TABLE, changes, stats, pk_columns,
            args.lock_poll_interval, args.retype_batch_size,
        )
        result["label"] = label
        results.append(result)
        _log(f"  total {result['elapsed_s']:.3f}s, "
             f"longest ACCESS EXCLUSIVE held {result['max_lock_held_s']:.3f}s "
             f"({result['held_samples']}/{result['lock_samples']} samples), "
             f"pids {result['pids']}"
             + (f", ERROR: {result['error']}" if result["error"] else ""))
        # Refresh stats for the next migration -- a couple of these change
        # the table's shape (column count), and a stale `stats` dict is only
        # ever used to decide safety classification/size, not correctness,
        # but keeping it honestly current costs one query.
        with psycopg.connect(dsn, autocommit=True) as refresh_conn:
            stats = table_stats(refresh_conn, schema)

    if args.cleanup:
        _log(f"cleaning up: dropping schema {schema!r}")
        with psycopg.connect(dsn, autocommit=True) as cleanup_conn:
            cleanup_conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )

    # --- the report: stdout only, so `| tee bench/RESULTS.md` is exactly this ---
    host = _host_description()
    engine = _docker_engine_description()
    now = time.strftime("%Y-%m-%d %H:%M:%S %z")

    print("# Tributary 5GB Lock-Safety Benchmark")
    print()
    print(f"Run at: {now}")
    print()
    print(f"- **Postgres**: {pg_version}")
    print(f"- **Host**: {host}")
    print(f"- **Postgres runs inside**: Docker Desktop's Linux VM -- {engine}")
    print(f"- **events table** (`{schema}.{TABLE}`), measured via "
          f"`pg_total_relation_size` after `ANALYZE`: "
          f"**{actual_bytes / (1024**3):.3f} GiB** ({actual_bytes:,} bytes), "
          f"{actual_rows:,} rows")
    print(f"- **Lock watcher**: polls `pg_locks` on its own connection, configured "
          f"for a {args.lock_poll_interval * 1000:.2f}ms gap between samples on top "
          f"of the query round trip itself; the *achieved* average gap is reported "
          f"per row below (real, measured, not the configured target) -- a lock held "
          f"for less time than that achieved gap can sample as zero")
    print(f"- **Retype backfill batch size**: {args.retype_batch_size:,} rows/batch")
    print()
    print("| Migration | Total wall time | Longest ACCESS EXCLUSIVE held | Verdict | Notes |")
    print("|---|---|---|---|---|")
    for r in results:
        verdict = _verdict(r["max_lock_held_s"], r["elapsed_s"], r["held_samples"])
        notes = []
        if r["error"]:
            notes.append(f"FAILED: {r['error']}")
        if r["watcher_error"]:
            notes.append(f"watcher: {r['watcher_error']}")
        if r["warnings"]:
            notes.append("; ".join(r["warnings"]))
        avg_gap_ms = (r["elapsed_s"] / r["lock_samples"] * 1000) if r["lock_samples"] else 0.0
        notes.append(f"{r['step_count']} plan step(s), "
                     f"{r['held_samples']}/{r['lock_samples']} lock samples held "
                     f"(~{avg_gap_ms:.2f}ms/sample actually achieved), "
                     f"pids {r['pids']}")
        print(f"| {r['label']} | {_fmt_s(r['elapsed_s'])} | "
              f"**{_fmt_s(r['max_lock_held_s'])}** | {verdict} | {' -- '.join(notes)} |")

    print()
    any_error = any(r["error"] for r in results)
    return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
